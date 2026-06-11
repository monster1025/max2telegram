import asyncio
import json

import redis.asyncio as redis
import structlog
from redis.exceptions import TimeoutError as RedisTimeoutError

from app.models.tasks import (
  Max2TgTask,
  NotifyFallbackTask,
  SetReactionTask,
  Tg2MaxTask,
  parse_queue_payload,
)
from app.queue.protocols import QueuePort

MAX2TG_QUEUE = "max2tg_queue"
TG2MAX_QUEUE = "tg2max_queue"

logger = structlog.get_logger(__name__)


class RedisQueue(QueuePort):
  def __init__(self, redis_url: str) -> None:
    self._redis_url = redis_url
    self._client: redis.Redis | None = None

  async def _blpop(self, queue: str, timeout: int) -> tuple[str, str] | None:
    assert self._client is not None
    try:
      return await self._client.blpop(queue, timeout=timeout or None)
    except asyncio.CancelledError:
      raise
    except RedisTimeoutError as exc:
      task = asyncio.current_task()
      if task is not None and task.cancelling():
        raise asyncio.CancelledError() from exc
      return None

  async def connect(self) -> None:
    self._client = redis.from_url(self._redis_url, decode_responses=True)

  async def close(self) -> None:
    if self._client is not None:
      await self._client.aclose()

  async def enqueue_max2tg(
    self, task: Max2TgTask | SetReactionTask | NotifyFallbackTask
  ) -> None:
    assert self._client is not None
    await self._client.rpush(MAX2TG_QUEUE, task.model_dump_json())
    logger.debug("queue_enqueued", queue=MAX2TG_QUEUE, kind=task.kind)

  async def dequeue_max2tg(
    self, timeout: int = 0
  ) -> Max2TgTask | SetReactionTask | NotifyFallbackTask | None:
    assert self._client is not None
    result = await self._blpop(MAX2TG_QUEUE, timeout=timeout)
    if result is None:
      return None
    _, payload = result
    task = parse_queue_payload(payload)
    logger.debug("queue_dequeued", queue=MAX2TG_QUEUE, kind=task.kind)
    return task

  async def enqueue_tg2max(self, task: Tg2MaxTask) -> None:
    assert self._client is not None
    await self._client.rpush(TG2MAX_QUEUE, task.model_dump_json())
    logger.debug(
      "queue_enqueued",
      queue=TG2MAX_QUEUE,
      kind=task.kind,
      tg_message_id=task.tg_message_id,
    )

  async def dequeue_tg2max(self, timeout: int = 0) -> Tg2MaxTask | None:
    assert self._client is not None
    result = await self._blpop(TG2MAX_QUEUE, timeout=timeout)
    if result is None:
      return None
    _, payload = result
    data = json.loads(payload)
    task = Tg2MaxTask.model_validate(data)
    logger.debug(
      "queue_dequeued",
      queue=TG2MAX_QUEUE,
      tg_message_id=task.tg_message_id,
    )
    return task
