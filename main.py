import logging
import os

from dotenv import load_dotenv
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
    await update.message.reply_text(
        f"Получил голосовое на {duration} сек. "
        "Следующим шагом подключим расшифровку и КПТ-дневник."
    )


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
