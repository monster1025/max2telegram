import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Any

from pymax import MaxClient
from pymax.files import Photo, Video

from storage import BridgeStorage
from telegram_api import TelegramClient
from health import HealthState

logger = logging.getLogger(__name__)


def _normalize_title(value: str) -> str:
    return str(value or "").strip().casefold()


def _telegram_chat_title(chat: dict[str, Any]) -> str:
    # для каналов обычно есть title; для групп тоже; в крайнем случае — username
    return str(chat.get("title") or chat.get("username") or "").strip()

def _format_sender_line(sender: dict[str, Any] | None) -> str:
    if not isinstance(sender, dict):
        return "Unknown:"

    first = str(sender.get("first_name") or "").strip()
    last = str(sender.get("last_name") or "").strip()
    username = str(sender.get("username") or "").strip()

    full_name = " ".join([p for p in (first, last) if p])
    if not full_name:
        full_name = "Unknown"

    if username:
        return f"{full_name} (@{username}):"
    return f"{full_name}:"


def _format_forward_text(*, sender: dict[str, Any] | None, text: str) -> str:
    header = _format_sender_line(sender)
    body = str(text or "").strip()
    if body:
        return f"{header}\n{body}"
    return header


@dataclass
class _MediaGroupBuffer:
    first_seen_monotonic: float
    updates: list[dict[str, Any]] = field(default_factory=list)


class TelegramToMaxBridge:
    def __init__(
        self,
        *,
        max_client: MaxClient,
        telegram: TelegramClient,
        storage: BridgeStorage,
        health: "HealthState | None" = None,
    ) -> None:
        self._max_client = max_client
        self._telegram = telegram
        self._storage = storage
        self._health = health
        self._max_title_to_id: dict[str, int] = {}

        self._bot_id: str | None = None
        self._offset: int | None = None

        self._media_groups: dict[tuple[str, str], _MediaGroupBuffer] = {}
        self._media_group_grace_sec = 1.2

    async def start(self) -> None:
        me = await self._telegram.get_me()
        self._bot_id = str(me.get("id") or "")
        if not self._bot_id:
            raise RuntimeError("Cannot resolve Telegram bot id (getMe)")

        self._refresh_max_chat_cache()
        logger.info("Telegram->Max bridge started (bot_id=%s)", self._bot_id)

        while True:
            try:
                updates = await self._telegram.get_updates(offset=self._offset, timeout=25, limit=100)
                if self._health:
                    self._health.mark_telegram_ok()
                await self._handle_updates(updates)
            except Exception:
                if self._health:
                    self._health.mark_telegram_error()
                logger.exception("Telegram polling loop error")
                await asyncio.sleep(2)

    async def _handle_updates(self, updates: list[dict[str, Any]]) -> None:
        max_update_id = None
        for upd in updates:
            upd_id = upd.get("update_id")
            if isinstance(upd_id, int):
                max_update_id = upd_id if max_update_id is None else max(max_update_id, upd_id)

            message = None
            for container in ("message", "edited_message", "channel_post", "edited_channel_post"):
                candidate = upd.get(container)
                if isinstance(candidate, dict):
                    message = candidate
                    break
            if not message:
                continue

            if self._is_own_telegram_message(message):
                continue

            await self._handle_message(message)

        if max_update_id is not None:
            self._offset = max_update_id + 1

        await self._flush_ready_media_groups()

    def _is_own_telegram_message(self, message: dict[str, Any]) -> bool:
        sender = message.get("from")
        if isinstance(sender, dict):
            if sender.get("is_bot") is True:
                # важно: не уйти в цикл на собственных постах бота
                return True
            if self._bot_id and str(sender.get("id") or "") == self._bot_id:
                return True
        return False

    async def _handle_message(self, message: dict[str, Any]) -> None:
        chat = message.get("chat")
        if not isinstance(chat, dict):
            return

        # Команда привязки чата Telegram к названию чата в MAX (для Max->Telegram маршрутизации).
        # Работает даже при privacy mode, т.к. команды приходят боту.
        text = str(message.get("text") or "").strip()
        if text.startswith("/bind_max"):
            await self._handle_bind_max_command(message, chat)
            return

        chat_title = _telegram_chat_title(chat)
        normalized = _normalize_title(chat_title)
        if not normalized:
            logger.error("Telegram chat without title/username, skip (chat=%s)", chat)
            return

        max_chat_id = self._resolve_max_chat_id_by_title(normalized)
        if max_chat_id is None:
            # требование: если в MAX нет канала/группы — ошибка и не пересылать
            logger.error("MAX чат с названием '%s' не найден — сообщение не пересылаю", chat_title)
            return

        telegram_chat_id = str(chat.get("id"))
        telegram_message_id = str(message.get("message_id"))

        media_group_id = message.get("media_group_id")
        if media_group_id is not None:
            key = (telegram_chat_id, str(media_group_id))
            buf = self._media_groups.get(key)
            if buf is None:
                buf = _MediaGroupBuffer(first_seen_monotonic=time.monotonic())
                self._media_groups[key] = buf
            buf.updates.append(message)
            return

        await self._forward_single_message(
            max_chat_id=max_chat_id,
            telegram_chat_id=telegram_chat_id,
            telegram_message_id=telegram_message_id,
            message=message,
            media_group_id=None,
        )

    async def _handle_bind_max_command(self, message: dict[str, Any], chat: dict[str, Any]) -> None:
        raw = str(message.get("text") or "").strip()
        # формат: /bind_max <точное название чата в MAX>
        parts = raw.split(maxsplit=1)
        if len(parts) < 2 or not parts[1].strip():
            logger.error("bind_max: missing MAX chat title. Use: /bind_max <MAX chat title>")
            return

        max_title = parts[1].strip()
        norm = _normalize_title(max_title)

        # проверяем, что чат существует в MAX
        max_chat_id = self._resolve_max_chat_id_by_title(norm)
        if max_chat_id is None:
            logger.error("bind_max: MAX чат '%s' не найден — привязку не сохраняю", max_title)
            return

        telegram_chat_id = str(chat.get("id"))
        telegram_title = _telegram_chat_title(chat)
        self._storage.set_chat_route(
            max_chat_title_norm=norm,
            telegram_chat_id=telegram_chat_id,
            telegram_chat_title=telegram_title,
        )
        logger.info(
            "bind_max: bound MAX '%s' -> Telegram '%s' (%s)",
            max_title,
            telegram_title,
            telegram_chat_id,
        )

    async def _flush_ready_media_groups(self) -> None:
        now = time.monotonic()
        ready: list[tuple[tuple[str, str], _MediaGroupBuffer]] = []
        for key, buf in self._media_groups.items():
            if (now - buf.first_seen_monotonic) >= self._media_group_grace_sec:
                ready.append((key, buf))

        for key, buf in ready:
            self._media_groups.pop(key, None)
            telegram_chat_id, media_group_id = key
            # сообщения альбома приходят отдельно; отправляем в MAX одним сообщением с несколькими attachments
            await self._forward_media_group(
                telegram_chat_id=telegram_chat_id,
                media_group_id=media_group_id,
                messages=buf.updates,
            )

    async def _forward_media_group(self, *, telegram_chat_id: str, media_group_id: str, messages: list[dict[str, Any]]) -> None:
        if not messages:
            return

        chat = messages[0].get("chat")
        if not isinstance(chat, dict):
            return
        chat_title = _telegram_chat_title(chat)
        normalized = _normalize_title(chat_title)
        max_chat_id = self._resolve_max_chat_id_by_title(normalized)
        if max_chat_id is None:
            logger.error("MAX чат с названием '%s' не найден — media group не пересылаю", chat_title)
            return

        # Telegram может прислать несколько элементов с caption только на первом. Берём text/caption с первого, где он есть.
        raw_text = ""
        for m in messages:
            cand = str(m.get("text") or m.get("caption") or "").strip()
            if cand:
                raw_text = cand
                break
        text = _format_forward_text(sender=messages[0].get("from"), text=raw_text)

        reply_to = self._resolve_reply_to_max_id(max_chat_id=max_chat_id, message=messages[0])

        attachments: list[Any] = []
        for m in messages:
            attachments.extend(await self._extract_attachments(m))

        if not text.strip() and not attachments:
            return

        sent = await self._max_client.send_message(
            chat_id=max_chat_id,
            text=text,
            attachments=attachments if attachments else None,
            reply_to=reply_to,
        )
        if not sent:
            logger.error("MAX send_message returned empty for media group (chat_id=%s)", max_chat_id)
            return

        max_message_id = str(getattr(sent, "id", "") or "")
        if not max_message_id:
            logger.error("Cannot resolve MAX message id after sending media group (chat_id=%s)", max_chat_id)
            return

        for m in messages:
            tid = str(m.get("message_id"))
            if tid:
                self._storage.save_mapping(
                    telegram_chat_id=telegram_chat_id,
                    telegram_message_id=tid,
                    max_chat_id=str(max_chat_id),
                    max_message_id=max_message_id,
                    media_group_id=media_group_id,
                )

        logger.info(
            "Forwarded Telegram media group %s (count=%s) -> MAX %s/%s",
            media_group_id,
            len(messages),
            max_chat_id,
            max_message_id,
        )

    async def _forward_single_message(
        self,
        *,
        max_chat_id: int,
        telegram_chat_id: str,
        telegram_message_id: str,
        message: dict[str, Any],
        media_group_id: str | None,
    ) -> None:
        raw_text = str(message.get("text") or message.get("caption") or "").strip()
        text = _format_forward_text(sender=message.get("from"), text=raw_text)
        attachments = await self._extract_attachments(message)
        if not text.strip() and not attachments:
            return

        reply_to = self._resolve_reply_to_max_id(max_chat_id=max_chat_id, message=message)

        sent = await self._max_client.send_message(
            chat_id=max_chat_id,
            text=text,
            attachments=attachments if attachments else None,
            reply_to=reply_to,
        )
        if not sent:
            logger.error("MAX send_message returned empty (chat_id=%s)", max_chat_id)
            return

        max_message_id = str(getattr(sent, "id", "") or "")
        if not max_message_id:
            logger.error("Cannot resolve MAX message id after sending (chat_id=%s)", max_chat_id)
            return

        self._storage.save_mapping(
            telegram_chat_id=telegram_chat_id,
            telegram_message_id=telegram_message_id,
            max_chat_id=str(max_chat_id),
            max_message_id=max_message_id,
            media_group_id=media_group_id,
        )
        logger.info("Forwarded Telegram %s/%s -> MAX %s/%s", telegram_chat_id, telegram_message_id, max_chat_id, max_message_id)

    def _resolve_reply_to_max_id(self, *, max_chat_id: int, message: dict[str, Any]) -> str | None:
        reply = message.get("reply_to_message")
        if not isinstance(reply, dict):
            return None
        reply_mid = reply.get("message_id")
        if reply_mid is None:
            return None

        chat = message.get("chat")
        if not isinstance(chat, dict):
            return None
        telegram_chat_id = str(chat.get("id"))
        mapped = self._storage.get_max_message_id_for_telegram(
            telegram_chat_id=telegram_chat_id,
            telegram_message_id=str(reply_mid),
        )
        # reply_to в MAX — это id сообщения; если не нашли, просто отправляем без reply
        return mapped

    async def _extract_attachments(self, message: dict[str, Any]) -> list[Any]:
        attachments: list[Any] = []

        # photo: массив размеров, берём последний (самый большой)
        photos = message.get("photo")
        if isinstance(photos, list) and photos:
            best = None
            for p in photos:
                if isinstance(p, dict) and p.get("file_id"):
                    best = p
            if best and isinstance(best, dict):
                file_id = str(best.get("file_id"))
                if file_id:
                    try:
                        url = await self._telegram.get_file_url(file_id)
                        attachments.append(Photo(url=url))
                    except Exception:
                        logger.exception("Cannot fetch Telegram photo URL")

        video = message.get("video")
        if isinstance(video, dict) and video.get("file_id"):
            file_id = str(video.get("file_id"))
            if file_id:
                try:
                    url = await self._telegram.get_file_url(file_id)
                    attachments.append(Video(url=url))
                except Exception:
                    logger.exception("Cannot fetch Telegram video URL")

        return attachments

    def _refresh_max_chat_cache(self) -> None:
        title_to_id: dict[str, int] = {}
        for chat in getattr(self._max_client, "chats", []) or []:
            title = getattr(chat, "title", None)
            chat_id = getattr(chat, "id", None)
            if title and chat_id is not None:
                title_to_id[_normalize_title(str(title))] = int(chat_id)
        self._max_title_to_id = title_to_id

    def _resolve_max_chat_id_by_title(self, normalized_title: str) -> int | None:
        chat_id = self._max_title_to_id.get(normalized_title)
        if chat_id is not None:
            return chat_id
        # на всякий случай обновим кэш (например, если добавили чат во время работы)
        self._refresh_max_chat_cache()
        return self._max_title_to_id.get(normalized_title)

