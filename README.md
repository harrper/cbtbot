# КПТ Telegram Bot

Минимальный тестовый Telegram-бот для проверки деплоя на хостинге.

## Переменные окружения

Обязательная:

```text
TELEGRAM_BOT_TOKEN=токен_из_BotFather
OPENAI_API_KEY=ключ_OpenAI
```

Необязательная, но полезная для личного бота:

```text
ALLOWED_TELEGRAM_USER_ID=ваш_telegram_id
OPENAI_TRANSCRIPTION_MODEL=gpt-4o-mini-transcribe
```

## Локальный запуск

```bash
pip install -r requirements.txt
TELEGRAM_BOT_TOKEN=... python main.py
```
