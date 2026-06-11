# max2telegram

Асинхронный мост для двусторонней синхронизации переписки между мессенджером **MAX** и **Telegram** (форум-канал с топиками).

## Архитектура

Приложение построено по слоям:

| Слой | Назначение |
|------|------------|
| `max_layer` | WebSocket-клиент MAX (PyMAX WebClient), listener и worker |
| `telegram_layer` | Aiogram dispatcher, TG worker, admin-команды |
| `router` | Маршрутизация по ID, дедупликация, постановка в очереди |
| `queue` | Redis-очереди `max2tg_queue` и `tg2max_queue` |
| `storage` | SQLite: маппинги, маркеры, связи сообщений |

## Быстрый старт

1. Скопируйте `.env.example` в `.env` и заполните переменные.
2. Создайте папку `data/` (монтируется для SQLite и сессии PyMAX).
3. Запустите:

```bash
docker compose up -d
```

## Переменные окружения

См. `tech-specs.md`, раздел 6, и `.env.example`.

## Команды (только FALLBACK_USER_ID в ЛС с ботом)

- `/start` — статус подключения
- `/help` — справка
- `/list` — список маппингов
- `/join <ссылка>` — вступить в MAX-группу
- `/leave <id или название>` — выйти из MAX-чата
- `/last_messages <id или название>` — последние 10 сообщений

## Локальная разработка

```bash
pip install -r requirements.txt
export $(cat .env | xargs)   # Linux/macOS
python -m app.main
```

Redis должен быть доступен по `REDIS_URL`.
