import base64
import json
import logging
import os
import re
import tempfile
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import httpx
from dotenv import load_dotenv
from google.oauth2 import service_account
from googleapiclient.discovery import build
from telegram import Update
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)


load_dotenv()

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

TRANSCRIPTION_MODEL = "gpt-4o-transcribe"
OPENAI_TRANSCRIPTIONS_URL = "https://api.openai.com/v1/audio/transcriptions"
GOOGLE_SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]
TIMEZONE = os.getenv("TIMEZONE", "Europe/Moscow")
APP_VERSION = "v0.8.2-russian-transcription"
EXTRACTION_MODEL = os.getenv("OPENAI_EXTRACTION_MODEL", "gpt-4.1-mini")
OPENAI_RESPONSES_URL = "https://api.openai.com/v1/responses"
PENDING_ENTRY_KEY = "pending_journal_entry"
SHEET_VALUE_BY_HEADER = {
    "дата": "timestamp",
    "тест сообщения": "transcript",
    "текст сообщения": "transcript",
    "исходная расшифровка": "transcript",
    "расшифровка": "transcript",
    "интенсивность": "intensity",
    "ситуация": "situation",
    "мысли": "thoughts",
    "эмоции": "emotions",
    "ощущения": "sensations",
    "действия": "actions",
    "дейсвия": "actions",
}
JOURNAL_FIELD_LABELS = {
    "intensity": "интенсивность",
    "situation": "ситуация",
    "thoughts": "мысли",
    "emotions": "эмоции",
    "sensations": "ощущения в теле",
    "actions": "действия",
}
REQUIRED_JOURNAL_FIELDS = (
    "intensity",
    "situation",
    "thoughts",
    "emotions",
    "sensations",
    "actions",
)


def get_bot_token() -> str:
    token = os.getenv("TELEGRAM_BOT_TOKEN") or os.getenv("BOT_TOKEN")
    if not token:
        raise RuntimeError(
            "Set TELEGRAM_BOT_TOKEN or BOT_TOKEN in environment variables."
        )
    return token


def is_allowed_user(update: Update) -> bool:
    allowed_user_id = os.getenv("ALLOWED_TELEGRAM_USER_ID")
    if not allowed_user_id:
        return True

    user = update.effective_user
    return user is not None and str(user.id) == allowed_user_id


async def guard(update: Update) -> bool:
    if is_allowed_user(update):
        return True

    if update.effective_message:
        await update.effective_message.reply_text("Этот бот закрыт для личного использования.")
    return False


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await guard(update):
        return

    await update.message.reply_text(
        "Привет! Я тестовый КПТ-бот. "
        "Пока я проверяю, что хостинг работает: отправь мне текст или голосовое."
    )


async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await guard(update):
        return

    text = update.message.text or ""
    if text.strip() == "/?":
        await update.message.reply_text(format_status_message())
        return

    if context.user_data.get(PENDING_ENTRY_KEY):
        await handle_clarification(update, context, text)
        return

    await process_journal_text(update, context, text, should_echo_transcript=False)


async def handle_voice(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await guard(update):
        return

    voice = update.message.voice
    duration = voice.duration if voice else 0
    if not voice:
        await update.message.reply_text("Не получилось прочитать голосовое.")
        return

    await update.message.reply_text(
        f"Получил голосовое на {duration} сек. Расшифровываю...\n"
        f"Версия: {APP_VERSION}"
    )

    audio_path: Path | None = None
    try:
        audio_path = await download_voice_message(voice, context)
        transcript = await transcribe_audio(audio_path)
    except RuntimeError as error:
        logger.warning("Configuration error: %s", error)
        await update.message.reply_text(str(error))
        return
    except httpx.HTTPStatusError as error:
        openai_error = error.response.text
        logger.exception("OpenAI transcription failed: %s", openai_error)
        await update.message.reply_text(
            "Не получилось расшифровать аудио через OpenAI. "
            f"OpenAI вернул статус {error.response.status_code}.\n\n"
            f"Детали: {openai_error[:900]}"
        )
        return
    except Exception:
        logger.exception("Voice handling failed")
        await update.message.reply_text("Не получилось обработать голосовое. Ошибка записана в логи.")
        return
    finally:
        if audio_path:
            audio_path.unlink(missing_ok=True)

    if not transcript.strip():
        await update.message.reply_text("Расшифровка получилась пустой. Попробуй записать чуть громче.")
        return

    if context.user_data.get(PENDING_ENTRY_KEY):
        await update.message.reply_text(f"Расшифровка уточнения:\n\n{transcript}")
        await handle_clarification(update, context, transcript)
        return

    await process_journal_text(update, context, transcript, should_echo_transcript=True)


async def process_journal_text(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    transcript: str,
    should_echo_transcript: bool,
) -> None:
    if should_echo_transcript:
        await update.message.reply_text(f"Расшифровка:\n\n{transcript}")

    try:
        journal_entry = await extract_journal_entry(transcript)
    except httpx.HTTPStatusError as error:
        openai_error = error.response.text
        logger.exception("OpenAI extraction failed: %s", openai_error)
        await update.message.reply_text(
            "Расшифровка готова, но не получилось разобрать запись через OpenAI. "
            f"OpenAI вернул статус {error.response.status_code}.\n\n"
            f"Детали: {openai_error[:900]}"
        )
        return
    except Exception:
        logger.exception("Journal extraction failed")
        await update.message.reply_text("Расшифровка готова, но не получилось разобрать дневниковую запись.")
        return

    if not journal_entry["is_journal_entry"]:
        reason = journal_entry.get("reason") or "не является дневниковой записью об эмоциях"
        await update.message.reply_text(
            "Запись не может быть обработана, потому что не является дневниковой записью об эмоциях.\n\n"
            f"Причина: {reason}"
        )
        return

    await update.message.reply_text(format_journal_entry_summary(journal_entry))
    missing_fields = get_missing_journal_fields(journal_entry)
    if missing_fields:
        context.user_data[PENDING_ENTRY_KEY] = {
            "transcript": transcript,
            "journal_entry": journal_entry,
        }
        await update.message.reply_text(format_missing_fields_message(missing_fields))
        return

    await save_journal_entry_and_report(update, transcript, journal_entry)


async def handle_clarification(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    clarification: str,
) -> None:
    pending_entry = context.user_data.get(PENDING_ENTRY_KEY)
    if not pending_entry:
        await process_journal_text(
            update,
            context,
            clarification,
            should_echo_transcript=False,
        )
        return

    transcript = pending_entry["transcript"]
    journal_entry = pending_entry["journal_entry"]

    if is_no_clarification_answer(clarification):
        context.user_data.pop(PENDING_ENTRY_KEY, None)
        await update.message.reply_text("Понял, сохраняю запись без уточнений.")
        await save_journal_entry_and_report(update, transcript, journal_entry)
        return

    combined_transcript = (
        f"{transcript} || Уточнение к этой же дневниковой записи: {clarification}"
    )

    try:
        updated_entry = await extract_journal_entry(combined_transcript)
    except httpx.HTTPStatusError as error:
        openai_error = error.response.text
        logger.exception("OpenAI clarification extraction failed: %s", openai_error)
        await update.message.reply_text(
            "Не получилось разобрать уточнение через OpenAI. "
            f"OpenAI вернул статус {error.response.status_code}.\n\n"
            f"Детали: {openai_error[:900]}"
        )
        return
    except Exception:
        logger.exception("Clarification extraction failed")
        await update.message.reply_text("Не получилось разобрать уточнение.")
        return

    if not updated_entry["is_journal_entry"]:
        await update.message.reply_text(
            "Не получилось применить уточнение к дневниковой записи. "
            "Попробуй написать недостающие пункты явно."
        )
        return

    context.user_data.pop(PENDING_ENTRY_KEY, None)
    await update.message.reply_text(format_journal_entry_summary(updated_entry))
    await save_journal_entry_and_report(update, combined_transcript, updated_entry)


async def save_journal_entry_and_report(
    update: Update,
    transcript: str,
    journal_entry: dict,
) -> None:
    try:
        spreadsheet_url = save_transcript_to_google_sheet(transcript, journal_entry)
    except RuntimeError as error:
        logger.info("Google Sheets is not configured: %s", error)
        await update.message.reply_text(
            "Расшифровка готова, но сохранение в Google Sheets не настроено на сервере.\n\n"
            f"Детали: {error}"
        )
    except Exception:
        logger.exception("Failed to save transcript to Google Sheets")
        await update.message.reply_text("Расшифровка готова, но не получилось сохранить ее в Google Sheets.")
    else:
        await update.message.reply_text(f"Сохранил запись в Google Sheets:\n{spreadsheet_url}")


async def download_voice_message(voice, context: ContextTypes.DEFAULT_TYPE) -> Path:
    telegram_file = await context.bot.get_file(voice.file_id)
    with tempfile.NamedTemporaryFile(delete=False, suffix=".ogg") as temp_file:
        audio_path = Path(temp_file.name)

    await telegram_file.download_to_drive(custom_path=audio_path)
    return audio_path


async def transcribe_audio(audio_path: Path) -> str:
    api_key = os.getenv("OPENAI_API_KEY")
    if not api_key:
        raise RuntimeError("Set OPENAI_API_KEY in environment variables.")

    async with httpx.AsyncClient(timeout=120) as client:
        with audio_path.open("rb") as audio_file:
            response = await client.post(
                OPENAI_TRANSCRIPTIONS_URL,
                headers={"Authorization": f"Bearer {api_key}"},
                data={
                    "model": TRANSCRIPTION_MODEL,
                    "language": "ru",
                    "response_format": "text",
                },
                files={"file": ("voice.ogg", audio_file, "audio/ogg")},
            )
            response.raise_for_status()
            return response.text.strip()


async def extract_journal_entry(transcript: str) -> dict:
    api_key = os.getenv("OPENAI_API_KEY")
    if not api_key:
        raise RuntimeError("Set OPENAI_API_KEY in environment variables.")

    payload = {
        "model": EXTRACTION_MODEL,
        "input": [
            {
                "role": "system",
                "content": [
                    {
                        "type": "input_text",
                        "text": (
                            "Ты извлекаешь данные для КПТ-дневника из одной пользовательской записи. "
                            "Ничего не додумывай и не интерпретируй. Заполняй поле только если это прямо "
                            "сказано в тексте. Если запись не описывает эмоциональное состояние, переживание, "
                            "ситуацию с эмоциями или дневниковую запись о чувствах, верни is_journal_entry=false. "
                            "Поле reason всегда пиши по-русски. "
                            "Если пользователь в тексте сам себя исправляет или уточняет любое поле, бери "
                            "последнюю скорректированную версию: например '1 из 10, ладно, 2 из 10' значит "
                            "интенсивность '2 из 10'; 'слабость, хотя нет, пустота' значит 'пустота'. "
                            "Ситуация — только фактический контекст или триггер: что произошло, где/когда, "
                            "какое событие или телесный симптом запустил переживание. Не записывай в ситуацию "
                            "автоматические мысли, прогнозы, оценки, выводы и интерпретации — их записывай "
                            "в thoughts, если они прямо сказаны. "
                            "Интенсивность извлекай только если пользователь явно указал именно силу/оценку "
                            "эмоции. Записывай intensity только числом по шкале от 0 до 10 без слов и "
                            "без 'из 10': например '8 из 10' -> '8', '70%' -> '7', '2/10' -> '2'. "
                            "Если оценка дана словами, переведи ее в число 0–10: слабая -> 2, "
                            "умеренная -> 5, сильная -> 8, очень сильная -> 9. "
                            "Не записывай в интенсивность названия эмоций вроде 'тревога', 'паника', "
                            "'злость' и не делай вывод о силе по словам 'даже', 'очень испугался' или "
                            "по общему смыслу. Если явной оценки силы нет — оставь intensity пустым. "
                            "Эмоции записывай в именительном падеже, через запятую: 'тревога', 'гнев', "
                            "'отчаяние', а не 'тревогу', 'гнева' или 'отчаянием'. "
                            "Ощущения — только телесные реакции, которые возникли как следствие эмоций, "
                            "мыслей или переживания: например ком в горле, сжатие в груди, дрожь, жар, "
                            "напряжение, учащенное сердцебиение. Не записывай в ощущения физический симптом, "
                            "боль или телесное событие, если оно является триггером/частью ситуации: например "
                            "заболела коленка, заболела голова, усталость, травма. Такой триггер относится "
                            "к ситуации. Если телесная реакция на эмоцию прямо не названа — оставь sensations "
                            "пустым. Действия — только явно указанные предпринятые действия."
                        ),
                    }
                ],
            },
            {
                "role": "user",
                "content": [
                    {
                        "type": "input_text",
                        "text": transcript,
                    }
                ],
            },
        ],
        "text": {
            "format": {
                "type": "json_schema",
                "name": "cbt_journal_entry",
                "strict": True,
                "schema": {
                    "type": "object",
                    "additionalProperties": False,
                    "properties": {
                        "is_journal_entry": {"type": "boolean"},
                        "reason": {"type": "string"},
                        "intensity": {"type": "string"},
                        "situation": {"type": "string"},
                        "thoughts": {"type": "string"},
                        "emotions": {"type": "string"},
                        "sensations": {"type": "string"},
                        "actions": {"type": "string"},
                    },
                    "required": [
                        "is_journal_entry",
                        "reason",
                        "intensity",
                        "situation",
                        "thoughts",
                        "emotions",
                        "sensations",
                        "actions",
                    ],
                },
            }
        },
    }

    async with httpx.AsyncClient(timeout=120) as client:
        response = await client.post(
            OPENAI_RESPONSES_URL,
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            json=payload,
        )
        response.raise_for_status()
        data = response.json()

    return normalize_journal_entry(json.loads(extract_response_text(data)))


def extract_response_text(response_data: dict) -> str:
    for output_item in response_data.get("output", []):
        for content_item in output_item.get("content", []):
            if content_item.get("type") == "output_text":
                return content_item.get("text", "")

    raise RuntimeError("OpenAI response did not contain output_text.")


def get_missing_journal_fields(journal_entry: dict) -> list[str]:
    return [
        field_name
        for field_name in REQUIRED_JOURNAL_FIELDS
        if not str(journal_entry.get(field_name) or "").strip()
    ]


def format_missing_fields_message(missing_fields: list[str]) -> str:
    field_list = ", ".join(JOURNAL_FIELD_LABELS[field] for field in missing_fields)
    return (
        f"Не указаны: {field_list}.\n\n"
        "Пришли уточнение следующим сообщением. "
        "Если уточнений не будет, напиши: уточнений не будет"
    )


def is_no_clarification_answer(text: str) -> bool:
    normalized = normalize_header(text)
    return normalized in {
        "уточнений не будет",
        "нет уточнений",
        "без уточнений",
        "не буду уточнять",
        "не хочу уточнять",
        "пропустить",
        "сохрани как есть",
        "оставь как есть",
    }


def normalize_journal_entry(journal_entry: dict) -> dict:
    normalized = dict(journal_entry)
    for field_name in (
        "intensity",
        "situation",
        "thoughts",
        "emotions",
        "sensations",
        "actions",
    ):
        value = normalized.get(field_name)
        if isinstance(value, str):
            normalized[field_name] = strip_final_period_from_single_sentence(value)

    intensity = normalized.get("intensity")
    if isinstance(intensity, str):
        normalized["intensity"] = normalize_intensity(intensity)

    return normalized


def normalize_intensity(intensity: str) -> str:
    value = strip_final_period_from_single_sentence(intensity)
    if not value:
        return ""

    percent_match = re.search(r"(\d+(?:[.,]\d+)?)\s*%", value)
    if percent_match:
        return format_scale_number(float(percent_match.group(1).replace(",", ".")) / 10)

    scale_match = re.search(
        r"(\d+(?:[.,]\d+)?)\s*(?:из|/)\s*10",
        value,
        flags=re.IGNORECASE,
    )
    if scale_match:
        return format_scale_number(float(scale_match.group(1).replace(",", ".")))

    plain_number_match = re.fullmatch(r"\s*(\d+(?:[.,]\d+)?)\s*", value)
    if plain_number_match:
        return format_scale_number(float(plain_number_match.group(1).replace(",", ".")))

    return value


def format_scale_number(number: float) -> str:
    bounded = max(0, min(10, number))
    if bounded.is_integer():
        return str(int(bounded))
    return f"{bounded:.1f}".rstrip("0").rstrip(".")


def strip_final_period_from_single_sentence(text: str) -> str:
    stripped = text.strip()
    if not stripped.endswith("."):
        return stripped

    without_final_period = stripped[:-1]
    if has_sentence_ending_inside(without_final_period):
        return stripped

    return without_final_period


def has_sentence_ending_inside(text: str) -> bool:
    for index, char in enumerate(text):
        if char in ".!?":
            next_char = text[index + 1] if index + 1 < len(text) else ""
            if not next_char or next_char.isspace():
                return True
    return False


def format_journal_entry_summary(journal_entry: dict) -> str:
    def value(field_name: str) -> str:
        return journal_entry.get(field_name) or "не указано"

    return (
        "Разбор записи:\n\n"
        f"Интенсивность: {value('intensity')}\n"
        f"Ситуация: {value('situation')}\n"
        f"Мысли: {value('thoughts')}\n"
        f"Эмоции: {value('emotions')}\n"
        f"Ощущения в теле: {value('sensations')}\n"
        f"Действия: {value('actions')}"
    )


def format_status_message() -> str:
    allowed_user_id = os.getenv("ALLOWED_TELEGRAM_USER_ID")
    access_line = (
        f"Работает только для Telegram ID: {allowed_user_id}"
        if allowed_user_id
        else "ALLOWED_TELEGRAM_USER_ID не указан. Бот сейчас отвечает всем."
    )
    return f"Версия: {APP_VERSION}\n{access_line}"


def save_transcript_to_google_sheet(transcript: str, journal_entry: dict | None = None) -> str:
    spreadsheet_id = os.getenv("GOOGLE_SHEET_ID")
    if not spreadsheet_id:
        raise RuntimeError("Set GOOGLE_SHEET_ID to enable Google Sheets saving.")

    sheets_service = build_google_service("sheets", "v4")
    sheet_title = get_first_sheet_title(sheets_service, spreadsheet_id)
    headers = get_sheet_headers(sheets_service, spreadsheet_id, sheet_title)
    append_range = f"{sheet_title}!A:{column_letter(len(headers))}"
    timestamp = datetime.now(ZoneInfo(TIMEZONE)).strftime("%Y-%m-%d %H:%M:%S")
    row = build_sheet_row(headers, transcript, timestamp, journal_entry or {})

    sheets_service.spreadsheets().values().append(
        spreadsheetId=spreadsheet_id,
        range=append_range,
        valueInputOption="USER_ENTERED",
        insertDataOption="INSERT_ROWS",
        body={"values": [row]},
    ).execute()

    return f"https://docs.google.com/spreadsheets/d/{spreadsheet_id}/edit"


def get_first_sheet_title(sheets_service, spreadsheet_id: str) -> str:
    spreadsheet = sheets_service.spreadsheets().get(
        spreadsheetId=spreadsheet_id,
        fields="sheets.properties.title",
    ).execute()
    first_sheet_title = spreadsheet["sheets"][0]["properties"]["title"]
    return first_sheet_title


def get_sheet_headers(sheets_service, spreadsheet_id: str, sheet_title: str) -> list[str]:
    values = sheets_service.spreadsheets().values().get(
        spreadsheetId=spreadsheet_id,
        range=f"{sheet_title}!1:1",
    ).execute().get("values", [])

    if not values or not values[0]:
        raise RuntimeError("Google Sheet must have headers in the first row.")

    return values[0]


def build_sheet_row(
    headers: list[str],
    transcript: str,
    timestamp: str,
    journal_entry: dict | None = None,
) -> list[str]:
    journal_entry = journal_entry or {}
    values = {
        "timestamp": timestamp,
        "transcript": transcript,
        "intensity": journal_entry.get("intensity", ""),
        "situation": journal_entry.get("situation", ""),
        "thoughts": journal_entry.get("thoughts", ""),
        "emotions": journal_entry.get("emotions", ""),
        "sensations": journal_entry.get("sensations", ""),
        "actions": journal_entry.get("actions", ""),
    }

    row = []
    for header in headers:
        field_name = SHEET_VALUE_BY_HEADER.get(normalize_header(header), "")
        row.append(values.get(field_name, ""))

    return row


def normalize_header(header: str) -> str:
    return " ".join(header.strip().lower().split())


def column_letter(column_number: int) -> str:
    letters = ""
    while column_number:
        column_number, remainder = divmod(column_number - 1, 26)
        letters = chr(65 + remainder) + letters
    return letters or "A"


def build_google_service(service_name: str, version: str):
    credentials_path = os.getenv("GOOGLE_APPLICATION_CREDENTIALS")
    service_account_json = os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON")
    service_account_json_base64 = os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON_BASE64")

    if service_account_json:
        service_account_info = json.loads(service_account_json)
    elif service_account_json_base64:
        service_account_info = json.loads(base64.b64decode(service_account_json_base64))
    else:
        service_account_info = None

    if service_account_info:
        credentials = service_account.Credentials.from_service_account_info(
            service_account_info,
            scopes=GOOGLE_SCOPES,
        )
    elif credentials_path:
        credentials = service_account.Credentials.from_service_account_file(
            credentials_path,
            scopes=GOOGLE_SCOPES,
        )
    else:
        raise RuntimeError(
            "Set GOOGLE_SERVICE_ACCOUNT_JSON or GOOGLE_APPLICATION_CREDENTIALS."
        )

    return build(service_name, version, credentials=credentials)


async def handle_error(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    logger.exception("Unhandled bot error", exc_info=context.error)


def main() -> None:
    application = Application.builder().token(get_bot_token()).build()

    application.add_handler(CommandHandler("start", start))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    application.add_handler(MessageHandler(filters.VOICE, handle_voice))
    application.add_error_handler(handle_error)

    logger.info("Bot started")
    application.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
