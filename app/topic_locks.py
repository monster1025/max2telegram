import asyncio


class TopicLockRegistry:
  def __init__(self) -> None:
    self._locks: dict[int, asyncio.Lock] = {}

  def lock(self, max_chat_id: int) -> asyncio.Lock:
    lock = self._locks.get(max_chat_id)
    if lock is None:
      lock = asyncio.Lock()
      self._locks[max_chat_id] = lock
    return lock
