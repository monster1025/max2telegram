import asyncio
from collections.abc import Awaitable, Callable
from pathlib import Path

import structlog

from app.config import Settings
from app.logging_setup import setup_logging
from app.max_layer.client_holder import MaxClientHolder
from app.max_layer.listener import MaxListener
from app.max_layer.worker import MaxWorker
from app.queue.redis_queue import RedisQueue
from app.router.router import MessageRouter
from app.storage.database import create_engine, create_session_factory
from app.storage.repository import SqliteStorage
from app.telegram_layer.bot_holder import BotHolder
from app.telegram_layer.listener import TelegramListener
from app.telegram_layer.worker import TelegramWorker
from app.topic_locks import TopicLockRegistry

logger = structlog.get_logger(__name__)

SERVICE_RESTART_DELAY_SEC = 3.0


async def run_service(
  name: str,
  runner: Callable[[], Awaitable[None]],
) -> None:
  while True:
    try:
      await runner()
      logger.warning("service_stopped", service=name)
    except asyncio.CancelledError:
      logger.info("service_cancelled", service=name)
      raise
    except Exception:
      logger.exception("service_failed", service=name)
    await asyncio.sleep(SERVICE_RESTART_DELAY_SEC)


async def main() -> None:
  settings = Settings()
  setup_logging(settings.log_level)
  Path(settings.sqlite_path).parent.mkdir(parents=True, exist_ok=True)
  Path(settings.data_dir).mkdir(parents=True, exist_ok=True)

  engine = create_engine(settings.database_url)
  session_factory = create_session_factory(engine)
  storage = SqliteStorage(session_factory)
  await storage.init()
  mappings = await storage.list_mappings()
  sqlite_file = Path(settings.sqlite_path)
  db_exists = sqlite_file.is_file()
  db_size_bytes = sqlite_file.stat().st_size if db_exists else 0
  logger.info(
    "storage_ready",
    sqlite_path=settings.sqlite_path,
    database_url=settings.database_url,
    db_file_exists=db_exists,
    db_file_size_bytes=db_size_bytes,
    mapping_count=len(mappings),
  )
  if len(mappings) == 0:
    logger.info(
      "storage_no_mappings",
      reason="chat_mappings_table_empty",
      effect="new_tg_forum_topic_will_be_created_for_each_max_chat",
      hint="after_container_recreate_check_volume_mount_data_dir_and_sqlite_path",
      sqlite_path=settings.sqlite_path,
    )

  legacy_db = Path.cwd() / "app" / "data" / "bridge.db"
  if (
    legacy_db.is_file()
    and legacy_db.resolve() != sqlite_file.resolve()
  ):
    logger.warning(
      "sqlite_legacy_path_detected",
      legacy_path=str(legacy_db.resolve()),
      active_path=settings.sqlite_path,
      hint="old_database_url_app/data/bridge_db_writes_outside_volume_use_data/bridge_db",
    )

  queue = RedisQueue(settings.redis_url)
  await queue.connect()

  topic_locks = TopicLockRegistry()
  router = MessageRouter(
    storage=storage,
    queue=queue,
    forum_channel_id=settings.tg_forum_channel_id,
    topic_locks=topic_locks,
  )

  max_holder = MaxClientHolder()
  bot_holder = BotHolder()

  max_listener = MaxListener(settings, max_holder, router, storage)
  tg_listener = TelegramListener(
    settings, bot_holder, router, storage, max_holder
  )
  tg_worker = TelegramWorker(
    settings, bot_holder, max_holder, queue, storage, topic_locks
  )
  max_worker = MaxWorker(settings, max_holder, bot_holder, queue, router, storage)

  logger.info("max2telegram_starting")

  await asyncio.gather(
    run_service("max_listener", max_listener.run),
    run_service("tg_listener", tg_listener.run),
    run_service("tg_worker", tg_worker.run),
    run_service("max_worker", max_worker.run),
  )


if __name__ == "__main__":
  asyncio.run(main())
