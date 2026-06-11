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
  logger.info(
    "storage_ready",
    sqlite_path=settings.sqlite_path,
    mapping_count=len(mappings),
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
