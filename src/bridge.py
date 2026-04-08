import logging
from typing import Any

from max_parser import parse_message
from models import ParsedMessage
from pymax import MaxClient
from pymax.types import PhotoAttach, VideoAttach
from storage import BridgeStorage
from telegram_api import TelegramClient

logger = logging.getLogger(__name__)


class MaxToTelegramBridge:
    def __init__(self, max_client: MaxClient, telegram: TelegramClient, storage: BridgeStorage) -> None:
        self._max_client = max_client
        self._telegram = telegram
        self._storage = storage

    async def forward_message(self, max_message: Any) -> None:
        parsed = parse_message(max_message)
        parsed = await self._enrich_from_max(max_message, parsed)

        if self._storage.was_forwarded(parsed.message_id, parsed.chat_id):
            logger.debug("Skip duplicated message %s/%s", parsed.chat_id, parsed.message_id)
            return

        text = self._format_caption(parsed.sender_name, parsed.chat_name, parsed.text)
        has_any_payload = bool(text.strip()) or bool(parsed.image_urls) or bool(parsed.video_urls)
        if not has_any_payload:
            logger.debug("Skip empty message %s/%s", parsed.chat_id, parsed.message_id)
            return

        target_chat_id, matched_by_title = await self._telegram.resolve_target_chat_id(parsed.chat_name)
        if matched_by_title:
            logger.info("Route Max chat '%s' to Telegram chat %s", parsed.chat_name, target_chat_id)
        else:
            logger.info(
                "Telegram chat '%s' not found, route to fallback user %s",
                parsed.chat_name,
                target_chat_id,
            )

        total_media = len(parsed.image_urls) + len(parsed.video_urls)
        if total_media > 1:
            # Отправляем одним альбомом в Telegram (единое сообщение).
            await self._telegram.send_media_group(
                chat_id=target_chat_id,
                image_urls=parsed.image_urls,
                video_urls=parsed.video_urls,
                caption=text,
            )
            self._storage.mark_forwarded(parsed.message_id, parsed.chat_id)
            logger.info(
                "Forwarded media group %s/%s (images=%s, videos=%s)",
                parsed.chat_id,
                parsed.message_id,
                len(parsed.image_urls),
                len(parsed.video_urls),
            )
            return

        sent_any = False
        if parsed.text.strip() and total_media == 0:
            await self._telegram.send_text(target_chat_id, text)
            sent_any = True

        for index, image_url in enumerate(parsed.image_urls):
            caption = text if not sent_any and index == 0 else None
            await self._telegram.send_photo(target_chat_id, image_url, caption=caption)
            sent_any = True

        for index, video_url in enumerate(parsed.video_urls):
            caption = text if not sent_any and index == 0 else None
            await self._telegram.send_video(target_chat_id, video_url, caption=caption)
            sent_any = True

        self._storage.mark_forwarded(parsed.message_id, parsed.chat_id)
        logger.info(
            "Forwarded message %s/%s (images=%s, videos=%s)",
            parsed.chat_id,
            parsed.message_id,
            len(parsed.image_urls),
            len(parsed.video_urls),
        )

    @staticmethod
    def _format_caption(sender_name: str, chat_name: str, text: str) -> str:
        header = f"MAX: {sender_name} / {chat_name}"
        if text.strip():
            return f"{header}\n\n{text}"
        return header

    async def _enrich_from_max(self, max_message: Any, parsed: ParsedMessage) -> ParsedMessage:
        # Имена отправителя и чата берем из API Max, чтобы всегда получить человекочитаемый формат.
        try:
            user = await self._max_client.get_user(user_id=max_message.sender)
            if user and getattr(user, "names", None):
                first_name = getattr(user.names[0], "name", "")
                if first_name:
                    parsed.sender_name = str(first_name)
        except Exception:
            logger.debug("Cannot resolve sender name", exc_info=True)

        try:
            chat = await self._max_client.get_chat(chat_id=max_message.chat_id)
            title = getattr(chat, "title", None)
            if title:
                parsed.chat_name = str(title)
        except Exception:
            logger.debug("Cannot resolve chat title", exc_info=True)

        attaches = getattr(max_message, "attaches", None) or []
        for attach in attaches:
            if isinstance(attach, PhotoAttach):
                parsed.image_urls.extend(self._extract_photo_urls(attach))
            elif isinstance(attach, VideoAttach):
                try:
                    video = await self._max_client.get_video_by_id(
                        chat_id=max_message.chat_id,
                        message_id=max_message.id,
                        video_id=attach.video_id,
                    )
                    video_url = getattr(video, "url", None)
                    if video_url:
                        parsed.video_urls.append(str(video_url))
                except Exception:
                    logger.exception("Cannot resolve video URL from Max")

        # Убираем дубли URL, если парсер и enrich нашли одинаковые вложения.
        parsed.image_urls = list(dict.fromkeys(parsed.image_urls))
        parsed.video_urls = list(dict.fromkeys(parsed.video_urls))
        return parsed

    def _extract_photo_urls(self, attach: PhotoAttach) -> list[str]:
        urls: list[str] = []
        seen_ids: set[int] = set()

        def walk(node: Any) -> None:
            if node is None:
                return

            obj_id = id(node)
            if obj_id in seen_ids:
                return
            seen_ids.add(obj_id)

            if isinstance(node, str):
                if node.startswith("http://") or node.startswith("https://"):
                    urls.append(node)
                return

            if isinstance(node, (list, tuple, set)):
                for item in node:
                    walk(item)
                return

            if isinstance(node, dict):
                for key, value in node.items():
                    # Поиск всех возможных URL полей, включая альбомы/варианты размеров.
                    if key in {"base_url", "url", "src", "download_url"} and isinstance(value, str):
                        if value.startswith("http://") or value.startswith("https://"):
                            urls.append(value)
                    else:
                        walk(value)
                return

            if hasattr(node, "__dict__"):
                walk(vars(node))

        walk(attach)
        return list(dict.fromkeys(urls))
