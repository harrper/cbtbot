# КПТ Telegram Bot

Минимальный тестовый Telegram-бот для проверки деплоя на хостинге.

## Переменные окружения

Обязательная:

```text
TELEGRAM_BOT_TOKEN=токен_из_BotFather
OPENAI_API_KEY=ключ_OpenAI
GOOGLE_SHEET_ID=ID_Google_Таблицы
GOOGLE_SHEET_RANGE=необязательно_например_Лист1!A:D
GOOGLE_SERVICE_ACCOUNT_JSON=JSON_сервисного_аккаунта_Google
# или GOOGLE_SERVICE_ACCOUNT_JSON_BASE64=JSON_сервисного_аккаунта_в_base64
```

Необязательная, но полезная для личного бота:

```text
ALLOWED_TELEGRAM_USER_ID=ваш_telegram_id
OPENAI_TRANSCRIPTION_MODEL=gpt-4o-mini-transcribe
TIMEZONE=Europe/Moscow
```

Для сохранения в Google Sheets нужно создать сервисный аккаунт в Google Cloud,
включить Google Sheets API и дать email сервисного аккаунта доступ на редактирование
к нужной Google Таблице.

## Локальный запуск

```bash
pip install -r requirements.txt
TELEGRAM_BOT_TOKEN=... python main.py
```
