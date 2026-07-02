import base64
import json
import logging
import os
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

TRANSCRIPTION_MODEL = os.getenv("OPENAI_TRANSCRIPTION_MODEL", "gpt-4o-mini-transcribe")
OPENAI_TRANSCRIPTIONS_URL = "https://api.openai.com/v1/audio/transcriptions"
GOOGLE_SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]
TIMEZONE = os.getenv("TIMEZONE", "Europe/Moscow")


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
    await update.message.reply_text(f"Получил текст: {text}")


async def handle_voice(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await guard(update):
        return

    voice = update.message.voice
    duration = voice.duration if voice else 0
    if not voice:
        await update.message.reply_text("Не получилось прочитать голосовое.")
        return

    await update.message.reply_text(f"Получил голосовое на {duration} сек. Расшифровываю...")

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

    await update.message.reply_text(f"Расшифровка:\n\n{transcript}")
    try:
            spreadsheet_url = save_transcript_to_google_sheet(transcript)
    except RuntimeError as error:
        logger.info("Google Sheets is not configured: %s", error)
    except Exception:
        logger.exception("Failed to save transcript to Google Sheets")
        await update.message.reply_text("Расшифровка готова, но не получилось сохранить ее в Google Sheets.")
    else:
        await update.message.reply_text(f"Сохранил расшифровку в Google Sheets:\n{spreadsheet_url}")


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
                    "response_format": "text",
                },
                files={"file": ("voice.ogg", audio_file, "audio/ogg")},
            )
            response.raise_for_status()
            return response.text.strip()


def save_transcript_to_google_sheet(transcript: str) -> str:
    spreadsheet_id = os.getenv("GOOGLE_SHEET_ID")
    if not spreadsheet_id:
        raise RuntimeError("Set GOOGLE_SHEET_ID to enable Google Sheets saving.")

    sheets_service = build_google_service("sheets", "v4")
    sheet_range = get_google_sheet_range(sheets_service, spreadsheet_id)
    timestamp = datetime.now(ZoneInfo(TIMEZONE)).strftime("%Y-%m-%d %H:%M:%S")

    sheets_service.spreadsheets().values().append(
        spreadsheetId=spreadsheet_id,
        range=sheet_range,
        valueInputOption="USER_ENTERED",
        insertDataOption="INSERT_ROWS",
        body={
            "values": [
                [
                    timestamp,
                    "voice",
                    transcript,
                    TRANSCRIPTION_MODEL,
                ]
            ]
        },
    ).execute()

    return f"https://docs.google.com/spreadsheets/d/{spreadsheet_id}/edit"


def get_google_sheet_range(sheets_service, spreadsheet_id: str) -> str:
    configured_range = os.getenv("GOOGLE_SHEET_RANGE")
    if configured_range:
        return configured_range

    spreadsheet = sheets_service.spreadsheets().get(
        spreadsheetId=spreadsheet_id,
        fields="sheets.properties.title",
    ).execute()
    first_sheet_title = spreadsheet["sheets"][0]["properties"]["title"]
    return f"{first_sheet_title}!A:D"


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
