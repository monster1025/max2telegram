from enum import Enum
from typing import Any

from pydantic import BaseModel, Field


class TaskKind(str, Enum):
  SEND_TO_TG = "send_to_tg"
  SET_REACTION = "set_reaction"
  NOTIFY_FALLBACK = "notify_fallback"
  SEND_TO_MAX = "send_to_max"


class MediaItem(BaseModel):
  kind: str
  url: str | None = None
  file_id: str | None = None
  file_name: str | None = None
  mime_type: str | None = None
  caption: str | None = None
  local_path: str | None = None
  max_chat_id: int | None = None
  max_message_id: int | None = None
  max_file_id: int | None = None
  max_video_id: int | None = None


class Max2TgTask(BaseModel):
  kind: TaskKind = TaskKind.SEND_TO_TG
  max_chat_id: int
  max_message_id: int
  text: str
  sender_name: str | None = None
  is_dm: bool = False
  chat_title: str
  chat_name: str = ""
  chat_icon_url: str | None = None
  chat_icon_local_path: str | None = None
  participants_count: int = 0
  max_chat_link: str | None = None
  needs_new_topic: bool = False
  reply_to_tg_message_id: int | None = None
  media: list[MediaItem] = Field(default_factory=list)


class SetReactionTask(BaseModel):
  kind: TaskKind = TaskKind.SET_REACTION
  tg_chat_id: int
  tg_message_id: int
  tg_thread_id: int | None = None
  emoji: str = "🦄"


class NotifyFallbackTask(BaseModel):
  kind: TaskKind = TaskKind.NOTIFY_FALLBACK
  title: str
  details: str


class Tg2MaxTask(BaseModel):
  kind: TaskKind = TaskKind.SEND_TO_MAX
  max_chat_id: int
  text: str
  tg_chat_id: int
  tg_message_id: int
  tg_thread_id: int | None = None
  reply_to_max_message_id: int | None = None
  media: list[MediaItem] = Field(default_factory=list)


def parse_queue_payload(data: str) -> Max2TgTask | SetReactionTask | NotifyFallbackTask | Tg2MaxTask:
  raw: dict[str, Any] = __import__("json").loads(data)
  kind = TaskKind(raw.get("kind", TaskKind.SEND_TO_TG))
  if kind == TaskKind.SEND_TO_TG:
    return Max2TgTask.model_validate(raw)
  if kind == TaskKind.SET_REACTION:
    return SetReactionTask.model_validate(raw)
  if kind == TaskKind.NOTIFY_FALLBACK:
    return NotifyFallbackTask.model_validate(raw)
  if kind == TaskKind.SEND_TO_MAX:
    return Tg2MaxTask.model_validate(raw)
  raise ValueError(f"Unknown task kind: {kind}")
