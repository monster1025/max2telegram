# Техническое задание: двунаправленный мост MAX <-> Telegram

## 1. Назначение системы

Система должна обеспечивать непрерывную двунаправленную синхронизацию сообщений между мессенджером MAX и Telegram:

- направление A: входящие сообщения из MAX пересылаются в Telegram;
- направление B: входящие сообщения из Telegram пересылаются в MAX;
- поддерживаются текст, фото, видео, документы/файлы и ответы (reply) в пределах доступных API;
- реализованы дедупликация, хранение связей между сообщениями в обоих направлениях и базовый health-monitoring;
- предусмотрен fallback-маршрут в Telegram при отсутствии явного соответствия чатов.

Система реализуется как один сервисный процесс (или контейнер), работающий постоянно.

## 2. Контекст и границы

### 2.1 Внешние зависимости

Обязательные внешние системы:

- API MAX (через клиентскую библиотеку или собственный API-клиент);
- Telegram Bot API (через клиентскую библиотеку);
- SQLite (или совместимое хранилище) для локального состояния;
- переменные окружения для конфигурации.

### 2.2 Что входит в систему

- запуск и аутентификация клиента MAX;
- polling Telegram `getUpdates` (без webhook);
- обработка входящих событий в обоих направлениях;
- маршрутизация по названию чата и по явным биндам;
- хранение mapping/дедупликации/маршрутов;
- HTTP health endpoints (`/livez`, `/healthz`).

### 2.3 Что НЕ входит в систему

- UI/панель администрирования;
- сложная очередь сообщений (Kafka/RabbitMQ и т.п.);
- гарантированная exactly-once доставка между платформами;
- миграции БД с версионированием (в базовой реализации только `CREATE TABLE IF NOT EXISTS`);
- хранение медиа в собственной файловой инфраструктуре.

## 3. Функциональные требования

## 3.1 MAX -> Telegram

При получении сообщения из MAX система должна:

1. Распарсить сообщение:
   - `message_id`, `chat_id`, `sender_name`, `chat_name`, текст;
   - список `image_urls`, `video_urls`, `file_urls`;
   - список неизвестных вложений;
   - данные reply-контекста (`reply_to_max_message_id`, preview).
2. Обогатить данные через API MAX (best effort):
   - попытаться получить человекочитаемое имя отправителя;
   - попытаться получить реальное название чата;
   - извлечь дополнительные вложения через типизированные attach-объекты.
3. Проверить дедупликацию по паре `(message_id, chat_id)`; дубликаты не отправлять.
4. Выбрать Telegram-чат:
   - сначала через явный route (bind) по нормализованному названию MAX-чата;
   - затем через кэш чатов Telegram по совпадению заголовка;
   - если не найдено — отправка в `fallback_user_id`.
5. Сформировать текст:
   - если целевой Telegram-чат найден (не fallback): `"{sender}:\n{text}"`;
   - если fallback: `"{sender} / {chat}:\n{text}"`;
   - при неизвестных вложениях добавить уведомление в конец.
6. Обработать reply:
   - попытаться найти Telegram `reply_to_message_id` через mapping;
   - не дублировать вложения из исходного сообщения, на которое отвечают;
   - если mapping не найден — добавить текстовый контекст ответа.
7. Отправить контент:
   - при `image+video > 1` — отправить единым альбомом (`sendMediaGroup`);
   - иначе отправить текст/медиа/файлы поштучно;
   - при полностью пустом payload отправить служебный fallback-текст.
8. Сохранить mapping отправленных сообщений.
9. Пометить сообщение как forwarded.
10. При ошибке основного пути сделать аварийное best-effort уведомление в fallback-чат Telegram.

## 3.2 Telegram -> MAX

Система должна запускать единственный polling-цикл `getUpdates` и:

1. При старте получить `bot_id` через `getMe`.
2. Обновить локальный кэш чатов MAX (`title -> id`) на основе доступного списка.
3. В цикле получать updates с `offset` и `allowed_updates=["message","channel_post"]`.
4. Для каждого сообщения:
   - отбросить неподдерживаемые типы;
   - отбросить сообщения бота (защита от петель);
   - обработать команду `/bind_max` в приоритетном порядке;
   - обработать служебные команды управления MAX (только личка + только fallback user);
   - для обычных сообщений найти чат MAX по нормализованному названию Telegram-чата;
   - если чат MAX не найден — не пересылать, логировать ошибку.
5. Обработать `media_group_id`:
   - буферизовать элементы альбома по ключу `(telegram_chat_id, media_group_id)`;
   - после grace-паузы (около 1.2 сек) отправить одним сообщением в MAX с несколькими вложениями.
6. Для одиночных сообщений:
   - сформировать текст `"{sender}:\n{text_or_caption}"`;
   - для фото/видео получить URL через `getFile` и вложить в MAX как native media attach;
   - для document/audio/voice/animation/sticker/video_note добавить URL-список в текстовый блок;
   - при reply попытаться найти соответствующее сообщение MAX через mapping.
7. После успешной отправки:
   - поставить реакцию на Telegram-сообщение (emoji, best effort);
   - сохранить mapping `(telegram chat/message -> max chat/message)`.

## 3.3 Управляющие команды в Telegram

Команды обрабатываются только в приватном чате с ботом и только от пользователя `fallback_user_id`, кроме `/bind_max` (она может работать в целевом чате).

Поддерживаемые команды:

- `/help` — список команд;
- `/list` — список активных чатов MAX;
- `/join <LINK>` — вступление в группу/канал MAX по ссылке;
- `/leave <НАЗВАНИЕ>` — выход из канала MAX (только если тип чата определен как канал);
- `/last_messages <НАЗВАНИЕ>` — последние 10 сообщений;
- `/bind_max <точное название MAX-чата>` — привязка текущего Telegram-чата к MAX-чату.

Требования:

- у команд должны быть понятные текстовые ответы;
- ошибки внешних API должны возвращаться в ответе, не падая процессом;
- `/bind_max` должна валидировать существование MAX-чата перед сохранением маршрута.

## 4. Нефункциональные требования

- **Надежность:** сервис работает бесконечно, при ошибках polling применяет backoff.
- **Идемпотентность (частичная):** дедупликация MAX->Telegram через БД.
- **Наблюдаемость:** структурированные логи и health endpoint.
- **Портируемость:** реализация возможна на любом языке при соблюдении контрактов.
- **Производительность:** обработка событий в near real-time, без тяжелых batch-процессов.
- **Отказоустойчивость:** best-effort при частичных отказах API/медиа.

## 5. Конфигурация (env contract)

Обязательные параметры:

- `MAX_PHONE` — телефон аккаунта MAX (для авторизации/сессии);
- `MAX_WORK_DIR` — рабочая директория клиента MAX (по умолчанию `cache`);
- `TELEGRAM_BOT_TOKEN` — токен Telegram-бота;
- `TELEGRAM_FALLBACK_USER_ID` — Telegram user/chat id для fallback;
- `SQLITE_PATH` — путь до SQLite БД (по умолчанию `${MAX_WORK_DIR}/max2telegram.db`).

Дополнительно:

- `TZ` — таймзона окружения;
- `PYTHONUNBUFFERED` (или аналог) — политика буферизации логов.

Валидация:

- обязательные переменные должны проверяться при старте;
- при отсутствии обязательной переменной процесс завершает запуск с явной ошибкой.

## 6. Архитектура

### 6.1 Компоненты

1. **Bootstrap / Main**
   - загружает конфиг;
   - создает клиентов MAX и Telegram;
   - инициализирует storage;
   - запускает health server;
   - подключает обработчики входящих сообщений MAX;
   - запускает Telegram->MAX poller как фоновую задачу.

2. **MAX->Telegram Bridge**
   - парсинг + enrich входящих MAX-сообщений;
   - маршрутизация в Telegram;
   - отправка текст/медиа/документы;
   - обработка миграции Telegram chat id;
   - сохранение mapping и dedup.

3. **Telegram API Client**
   - HTTP-обертка над Bot API;
   - методы отправки всех типов контента;
   - `getUpdates` + кэширование известных чатов;
   - `getFile` для медиа URL;
   - унифицированная модель ошибок с извлечением `migrate_to_chat_id`.

4. **Telegram->MAX Bridge**
   - polling updates;
   - фильтрация собственных сообщений;
   - обработка команд;
   - преобразование Telegram payload -> MAX message/attachments;
   - буферизация media group;
   - реакция и mapping.

5. **Parser MAX Message**
   - универсальный best-effort разбор разнородных форматов вложений;
   - классификация URL по типам;
   - извлечение reply-контекста.

6. **Storage**
   - таблица дедупликации;
   - таблица двустороннего mapping;
   - таблица явных маршрутов (binds).

7. **Health subsystem**
   - хранит отметки времени последнего успеха/ошибки по MAX и Telegram;
   - формирует snapshot;
   - HTTP endpoint отдает liveness/readiness.

8. **Auth utility**
   - отдельный скрипт первичной авторизации MAX;
   - сохраняет сессию в рабочем каталоге.

### 6.2 Логическая схема взаимодействия

- MAX event -> Parser -> MAX->TG Bridge -> Telegram API -> Storage update.
- Telegram update -> TG->MAX Bridge -> MAX API -> Storage update.
- TG->MAX Bridge единолично вызывает `getUpdates`, одновременно наполняя кэш чатов Telegram.
- Storage используется обоими мостами как разделяемый слой состояния.
- Health обновляется из polling-циклов и MAX событий.

## 7. Модель данных и БД

Используется SQLite (или эквивалент в другой СУБД).

### 7.1 Таблица `forwarded_messages`

Назначение: дедупликация MAX->Telegram.

Поля:

- `message_id TEXT NOT NULL`
- `chat_id TEXT NOT NULL`
- `forwarded_at DATETIME DEFAULT CURRENT_TIMESTAMP`

Ключ:

- `PRIMARY KEY (message_id, chat_id)`

### 7.2 Таблица `message_mapping`

Назначение: двусторонняя связка сообщений для reply и трассировки.

Поля:

- `telegram_chat_id TEXT NOT NULL`
- `telegram_message_id TEXT NOT NULL`
- `max_chat_id TEXT NOT NULL`
- `max_message_id TEXT NOT NULL`
- `media_group_id TEXT NULL`
- `created_at DATETIME DEFAULT CURRENT_TIMESTAMP`

Ключ:

- `PRIMARY KEY (telegram_chat_id, telegram_message_id)`

Индексы:

- `(max_chat_id, max_message_id)` — поиск Telegram-сообщения по MAX;
- `(telegram_chat_id, media_group_id)` — групповые операции альбомов.

### 7.3 Таблица `chat_routes`

Назначение: явные маршруты MAX chat title -> Telegram chat id.

Поля:

- `max_chat_title_norm TEXT PRIMARY KEY`
- `telegram_chat_id TEXT NOT NULL`
- `telegram_chat_title TEXT NULL`
- `created_at DATETIME DEFAULT CURRENT_TIMESTAMP`

Нормализация ключа:

- trim + casefold/lower (без учета регистра).

## 8. Алгоритмы и правила

## 8.1 Нормализация названий чатов

Во всех маршрутизирующих сравнениях:

- удалить крайние пробелы;
- привести к регистронезависимой форме (`casefold`/`lower`);
- сравнивать только в нормализованном виде.

## 8.2 Политика маршрутизации MAX -> Telegram

Порядок выбора:

1. `chat_routes` по нормализованному MAX title;
2. локальный кэш Telegram title->id (наполняется из `getUpdates`);
3. fallback user/chat id.

## 8.3 Политика медиа

- MAX->Telegram:
  - если суммарно фото+видео больше одного, использовать album API;
  - документы отправлять отдельными сообщениями;
  - caption добавлять только к первому элементу/первому отправляемому сообщению.
- Telegram->MAX:
  - фото/видео отправлять как native attachments;
  - прочие типы файлов прикладывать ссылками в тексте.

## 8.4 Reply-семантика

- Для Telegram->MAX:
  - если Telegram message является reply, искать соответствующий `max_message_id` в mapping;
  - если найден, отправлять `reply_to` в MAX.
- Для MAX->Telegram:
  - если MAX message является reply, искать `telegram_message_id` в mapping;
  - если MAX message является reply, не копировать медиа из `link.message` (исходного сообщения);
  - если найден, отправлять `reply_to_message_id`;
  - если не найден, добавлять текстовую пометку с превью исходного сообщения.

## 8.5 Обработка migration в Telegram

Если Telegram API возвращает ошибку с `parameters.migrate_to_chat_id`:

1. обновить маршрут в `chat_routes` на новый chat id;
2. повторить отправку в новый chat id;
3. считать повтор успешным итоговым результатом.

## 8.6 Дедупликация

- применяется для MAX->Telegram по ключу `(max_message_id, max_chat_id)`;
- после успешной отправки обязательно mark-forwarded;
- на старте/рестарте состояния берутся из БД.

## 8.7 Buffering media group (Telegram)

- ключ буфера: `(telegram_chat_id, media_group_id)`;
- каждое сообщение альбома копится в списке;
- по истечении grace-периода группа отправляется одним вызовом в MAX;
- после отправки буфер очищается.

## 9. API-контракты внутренних модулей

Ниже абстрактные контракты, независимые от языка:

- `Settings load_settings()`
  - читает env;
  - валидирует обязательные поля;
  - возвращает immutable-конфигурацию.

- `ParsedMessage parse_message(MaxMessage msg)`
  - best-effort преобразование сырого MAX-сообщения в каноническую структуру.

- `BridgeStorage`
  - `was_forwarded(message_id, chat_id) -> bool`
  - `mark_forwarded(message_id, chat_id)`
  - `save_mapping(telegram_chat_id, telegram_message_id, max_chat_id, max_message_id, media_group_id?)`
  - `get_max_message_id_for_telegram(telegram_chat_id, telegram_message_id) -> str?`
  - `get_telegram_message_id_for_max(telegram_chat_id, max_chat_id, max_message_id) -> str?`
  - `set_chat_route(max_chat_title_norm, telegram_chat_id, telegram_chat_title?)`
  - `get_chat_route(max_chat_title_norm) -> str?`

- `TelegramClient`
  - `resolve_target_chat_id(max_chat_name) -> (chat_id, matched_by_title)`
  - `send_text/send_photo/send_video/send_document/send_media_group(...)`
  - `get_updates(offset, timeout, limit) -> updates[]`
  - `get_file_url(file_id) -> url`
  - `add_reaction(chat_id, message_id, emoji)`

- `MaxToTelegramBridge.forward_message(max_message)`
- `TelegramToMaxBridge.start()`
- `handle_control_command(message, max_client, telegram) -> str?`

## 10. Health-check модель

Должны храниться timestamps:

- telegram: `last_ok`, `last_error`;
- max: `last_ok`, `last_error`, `last_event`;
- `started_at`.

Параметр:

- `unhealthy_after_sec` (по умолчанию ~15 минут).

Правила:

- `telegram_healthy = last_ok exists && now-last_ok <= unhealthy_after_sec`;
- `max_healthy = last_ok exists && now-last_ok <= unhealthy_after_sec`;
- `overall_healthy = telegram_healthy && max_healthy`.

HTTP:

- `GET /livez` (и `/live`, `/`) -> 200, `{status:"live", uptime_sec}`.
- `GET /healthz` (и `/health`) -> 200 или 503, детальный JSON по компонентам.

## 11. Поведение при ошибках и устойчивость

- Любая ошибка обработки отдельного сообщения не должна останавливать сервис.
- Polling Telegram при ошибке уходит в backoff (например 10 секунд).
- Ошибка реакции в Telegram не влияет на основную доставку.
- Ошибка аварийного уведомления логируется, но не роняет процесс.
- Проблемы с разрешением URL медиа обрабатываются best-effort:
  - что удалось достать — отправляется;
  - что не удалось — отражается в тексте/unknown notices.

## 12. Логирование и диагностика

Обязательные события логов:

- старт/остановка компонентов;
- маршрутизация сообщений;
- обнаружение дубликатов;
- ошибки API и stacktrace;
- успешная пересылка с количеством вложений;
- операции bind и migration chat id.

Рекомендуемый формат:

- timestamp + level + logger + message.

## 13. Сценарии запуска и деплой

## 13.1 Локальный запуск

1. Подготовить `.env`.
2. Установить зависимости.
3. Один раз пройти auth MAX (интерактивно), сохранить сессию.
4. Запустить основной процесс.

## 13.2 Контейнерный запуск

- контейнер должен включать runtime + зависимости;
- каталог `cache` должен быть volume для сохранения сессии и SQLite;
- должен быть healthcheck через `GET /healthz`.

## 13.3 CI/CD (рекомендованно)

- сборка Docker image при push в основные ветки;
- публикация в registry с тегами для dev/release/latest;
- кэширование слоев сборки.

## 14. Требования к переносимой реализации (на любом языке)

Чтобы воссоздать проект в другом языке, необходимо сохранить:

1. Два независимых, но согласованных канала обработки:
   - event-driven для MAX сообщений;
   - polling-loop для Telegram updates.
2. Единое постоянное хранилище с тремя сущностями:
   - dedup;
   - mapping;
   - routes.
3. Идентичные правила нормализации названий чатов и выбора маршрута.
4. Reply-механику с fallback-текстом при отсутствии mapping.
5. Политику media group и порядок отправки вложений.
6. Обработку Telegram migration (`migrate_to_chat_id`) с обновлением маршрута.
7. Health-модель с независимыми метками MAX/Telegram.
8. Ограничение: только один consumer `getUpdates` на экземпляр бота.

## 15. Acceptance criteria

Система считается реализованной, если:

1. Текст/фото/видео/файлы корректно ходят в обе стороны.
2. MAX->Telegram не дублирует уже пересланные сообщения после рестарта.
3. Reply в обе стороны сохраняется, если mapping существует.
4. При отсутствии Telegram-совпадения сообщение уходит в fallback.
5. `/bind_max` меняет маршрут и влияет на последующие MAX->Telegram сообщения.
6. Telegram media group приходит в MAX как одно сообщение с множеством вложений.
7. `/healthz` возвращает 200 при рабочем MAX+Telegram и 503 при деградации.
8. Ошибки отдельных сообщений не приводят к остановке процесса.

## 16. Известные ограничения текущей логики

- Автопоиск Telegram-чата зависит от того, что чат уже встречался в `getUpdates`.
- Для нестандартных вложений возможна частичная деградация в plain text + ссылки.
- Дедупликация реализована только для потока MAX->Telegram.
- Конкурентный доступ к SQLite через множество соединений допустим для небольших нагрузок, но для high-load может потребоваться иной storage backend.

## 17. Рекомендации для расширения (необязательно)

- добавить миграции схемы БД;
- добавить retry policy с классификацией transient/permanent ошибок;
- добавить метрики (Prometheus/OpenTelemetry);
- добавить интеграционные тесты с моками API MAX/Telegram;
- добавить персистентный offset Telegram (если нужен recovery без повторов между рестартами).

