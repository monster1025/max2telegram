from typing import Protocol

from app.models.domain import ChatMapping


class StoragePort(Protocol):
  async def init(self) -> None: ...

  async def get_mapping_by_max_chat(self, max_chat_id: int) -> ChatMapping | None: ...

  async def get_mapping_by_tg_thread(
    self, tg_chat_id: int, tg_thread_id: int
  ) -> ChatMapping | None: ...

  async def save_mapping(self, mapping: ChatMapping) -> None: ...

  async def list_mappings(self) -> list[ChatMapping]: ...

  async def get_sync_marker(self, max_chat_id: int) -> int | None: ...

  async def update_sync_marker(self, max_chat_id: int, message_id: int) -> None: ...

  async def save_message_link(
    self,
    max_chat_id: int,
    max_message_id: int,
    tg_chat_id: int,
    tg_thread_id: int,
    tg_message_id: int,
  ) -> None: ...

  async def get_tg_message_by_max(
    self, max_chat_id: int, max_message_id: int
  ) -> tuple[int, int, int] | None: ...

  async def get_max_message_by_tg(
    self, tg_chat_id: int, tg_message_id: int
  ) -> tuple[int, int] | None: ...
