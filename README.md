# КПТ Telegram Bot

Минимальный тестовый Telegram-бот для проверки деплоя на хостинге.

## Переменные окружения

Обязательная:

```text
TELEGRAM_BOT_TOKEN=токен_из_BotFather
OPENAI_API_KEY=ключ_OpenAI
GOOGLE_DOC_ID=ID_Google_Документа
GOOGLE_SERVICE_ACCOUNT_JSON=JSON_сервисного_аккаунта_Google
# или GOOGLE_SERVICE_ACCOUNT_JSON_BASE64=JSON_сервисного_аккаунта_в_base64
```

Необязательная, но полезная для личного бота:

```text
ALLOWED_TELEGRAM_USER_ID=ваш_telegram_id
OPENAI_TRANSCRIPTION_MODEL=gpt-4o-mini-transcribe
TIMEZONE=Europe/Moscow
```

Для сохранения в Google Docs нужно создать сервисный аккаунт в Google Cloud,
включить Google Docs API и дать email сервисного аккаунта доступ на редактирование
к нужному Google Документу.

## Локальный запуск

```bash
pip install -r requirements.txt
TELEGRAM_BOT_TOKEN=... python main.py
```
