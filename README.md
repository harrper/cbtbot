# КПТ Telegram Bot

Минимальный тестовый Telegram-бот для проверки деплоя на хостинге.

## Переменные окружения

Обязательная:

```text
TELEGRAM_BOT_TOKEN=токен_из_BotFather
```

Необязательная, но полезная для личного бота:

```text
ALLOWED_TELEGRAM_USER_ID=ваш_telegram_id
```

## Локальный запуск

```bash
pip install -r requirements.txt
TELEGRAM_BOT_TOKEN=... python main.py
```
