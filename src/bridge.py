import logging
from collections.abc import Awaitable, Callable
from typing import Any

from max_parser import parse_message
from models import ParsedMessage
from pymax import MaxClient
from pymax.types import AudioAttach, FileAttach, Message, PhotoAttach, StickerAttach, VideoAttach
from storage import BridgeStorage
from telegram_api import TelegramApiError, TelegramClient

logger = logging.getLogger(__name__)


class MaxToTelegramBridge:
    def __init__(self, max_client: MaxClient, telegram: TelegramClient, storage: BridgeStorage) -> None:
        self._max_client = max_client
        self._telegram = telegram
        self._storage = storage

    async def forward_message(self, max_message: Any) -> None:
        #if self._is_self_message(max_message):
        #    logger.debug("Skip self message %s/%s", getattr(max_message, "chat_id", "?"), getattr(max_message, "id", "?"))
        #    return

        parsed = parse_message(max_message)
        parsed = await self._enrich_from_max(max_message, parsed)

        if self._storage.was_forwarded(parsed.message_id, parsed.chat_id):
            logger.debug("Skip duplicated message %s/%s", parsed.chat_id, parsed.message_id)
            return

        # Формат зависит от маршрута:
        # - если найден целевой Telegram-чат (не fallback): "Ирина:\n<текст>"
        # - если fallback: "Ирина / Свободный микрофон:\n<текст>"
        # Решение о том, включать ли название чата, принимаем после определения маршрута.
        normalized = parsed.chat_name.strip().casefold()
        routed = self._storage.get_chat_route(max_chat_title_norm=normalized)
        if routed:
            target_chat_id = routed
            is_fallback = False
            logger.info("Route Max chat '%s' to Telegram chat %s (bound)", parsed.chat_name, target_chat_id)
        else:
            target_chat_id, matched_by_title = await self._telegram.resolve_target_chat_id(parsed.chat_name)
            if matched_by_title:
                is_fallback = False
                logger.info("Route Max chat '%s' to Telegram chat %s", parsed.chat_name, target_chat_id)
            else:
                is_fallback = True
                logger.info(
                    "Telegram chat '%s' not found, route to fallback user %s",
                    parsed.chat_name,
                    target_chat_id,
                )

        text = self._format_caption(
            sender_name=parsed.sender_name,
            chat_name=parsed.chat_name,
            text=parsed.text,
            include_chat_name=is_fallback,
        )
        text = self._append_unknown_attachment_notice(parsed=parsed, text=text)

        reply_telegram_mid = self._resolve_telegram_reply_to(
            telegram_chat_id=str(target_chat_id),
            max_chat_id=str(parsed.chat_id),
            parsed=parsed,
        )
        if parsed.reply_to_max_message_id and reply_telegram_mid is None:
            text = self._prepend_max_reply_context(parsed, text)

        has_any_payload = bool(text.strip()) or bool(parsed.image_urls) or bool(parsed.video_urls) or bool(parsed.file_urls)
        if not has_any_payload:
            text = self._build_fallback_unknown_notice(parsed)

        total_media = len(parsed.image_urls) + len(parsed.video_urls)
        sent_any = False
        if total_media > 1:
            # Отправляем одним альбомом в Telegram (единое сообщение).
            target_chat_id, sent_messages = await self._send_with_migration_retry(
                target_chat_id=target_chat_id,
                max_chat_title_norm=normalized,
                max_chat_title=parsed.chat_name,
                send_action=lambda chat_id: self._telegram.send_media_group(
                    chat_id=chat_id,
                    image_urls=parsed.image_urls,
                    video_urls=parsed.video_urls,
                    caption=text,
                    reply_to_message_id=reply_telegram_mid,
                ),
            )
            for sent in sent_messages:
                mid = sent.get("message_id")
                if mid is None:
                    continue
                self._storage.save_mapping(
                    telegram_chat_id=str(target_chat_id),
                    telegram_message_id=str(mid),
                    max_chat_id=str(parsed.chat_id),
                    max_message_id=str(parsed.message_id),
                )
                sent_any = True
            self._storage.mark_forwarded(parsed.message_id, parsed.chat_id)
            logger.info(
                "Forwarded media group %s/%s (images=%s, videos=%s)",
                parsed.chat_id,
                parsed.message_id,
                len(parsed.image_urls),
                len(parsed.video_urls),
            )
            return

        should_send_plain_text = total_media == 0 and not parsed.file_urls and bool(text.strip())
        if should_send_plain_text:
            target_chat_id, sent = await self._send_with_migration_retry(
                target_chat_id=target_chat_id,
                max_chat_title_norm=normalized,
                max_chat_title=parsed.chat_name,
                send_action=lambda chat_id: self._telegram.send_text(
                    chat_id, text, reply_to_message_id=reply_telegram_mid
                ),
            )
            mid = sent.get("result", {}).get("message_id") if isinstance(sent.get("result"), dict) else None
            if mid is not None:
                self._storage.save_mapping(
                    telegram_chat_id=str(target_chat_id),
                    telegram_message_id=str(mid),
                    max_chat_id=str(parsed.chat_id),
                    max_message_id=str(parsed.message_id),
                )
            sent_any = True

        for index, image_url in enumerate(parsed.image_urls):
            caption = text if not sent_any and index == 0 else None
            target_chat_id, sent = await self._send_with_migration_retry(
                target_chat_id=target_chat_id,
                max_chat_title_norm=normalized,
                max_chat_title=parsed.chat_name,
                send_action=lambda chat_id: self._telegram.send_photo(
                    chat_id,
                    image_url,
                    caption=caption,
                    reply_to_message_id=reply_telegram_mid if not sent_any and index == 0 else None,
                ),
            )
            mid = sent.get("result", {}).get("message_id") if isinstance(sent.get("result"), dict) else None
            if mid is not None:
                self._storage.save_mapping(
                    telegram_chat_id=str(target_chat_id),
                    telegram_message_id=str(mid),
                    max_chat_id=str(parsed.chat_id),
                    max_message_id=str(parsed.message_id),
                )
            sent_any = True

        for index, video_url in enumerate(parsed.video_urls):
            caption = text if not sent_any and index == 0 else None
            target_chat_id, sent = await self._send_with_migration_retry(
                target_chat_id=target_chat_id,
                max_chat_title_norm=normalized,
                max_chat_title=parsed.chat_name,
                send_action=lambda chat_id: self._telegram.send_video(
                    chat_id,
                    video_url,
                    caption=caption,
                    reply_to_message_id=reply_telegram_mid if not sent_any and index == 0 else None,
                ),
            )
            mid = sent.get("result", {}).get("message_id") if isinstance(sent.get("result"), dict) else None
            if mid is not None:
                self._storage.save_mapping(
                    telegram_chat_id=str(target_chat_id),
                    telegram_message_id=str(mid),
                    max_chat_id=str(parsed.chat_id),
                    max_message_id=str(parsed.message_id),
                )
            sent_any = True

        for index, file_url in enumerate(parsed.file_urls):
            caption = text if not sent_any and index == 0 else None
            target_chat_id, sent = await self._send_with_migration_retry(
                target_chat_id=target_chat_id,
                max_chat_title_norm=normalized,
                max_chat_title=parsed.chat_name,
                send_action=lambda chat_id: self._telegram.send_document(
                    chat_id,
                    file_url,
                    caption=caption,
                    reply_to_message_id=reply_telegram_mid if not sent_any and index == 0 else None,
                ),
            )
            mid = sent.get("result", {}).get("message_id") if isinstance(sent.get("result"), dict) else None
            if mid is not None:
                self._storage.save_mapping(
                    telegram_chat_id=str(target_chat_id),
                    telegram_message_id=str(mid),
                    max_chat_id=str(parsed.chat_id),
                    max_message_id=str(parsed.message_id),
                )
            sent_any = True

        if not sent_any:
            # Последняя страховка: гарантируем уведомление в Telegram даже для пустых/неизвестных payload.
            fallback_text = text.strip() or self._build_fallback_unknown_notice(parsed)
            target_chat_id, sent = await self._send_with_migration_retry(
                target_chat_id=target_chat_id,
                max_chat_title_norm=normalized,
                max_chat_title=parsed.chat_name,
                send_action=lambda chat_id: self._telegram.send_text(
                    chat_id, fallback_text, reply_to_message_id=reply_telegram_mid
                ),
            )
            mid = sent.get("result", {}).get("message_id") if isinstance(sent.get("result"), dict) else None
            if mid is not None:
                self._storage.save_mapping(
                    telegram_chat_id=str(target_chat_id),
                    telegram_message_id=str(mid),
                    max_chat_id=str(parsed.chat_id),
                    max_message_id=str(parsed.message_id),
                )
            sent_any = True

        self._storage.mark_forwarded(parsed.message_id, parsed.chat_id)
        logger.info(
            "Forwarded message %s/%s (images=%s, videos=%s, files=%s, unknown=%s)",
            parsed.chat_id,
            parsed.message_id,
            len(parsed.image_urls),
            len(parsed.video_urls),
            len(parsed.file_urls),
            len(parsed.unknown_attachments),
        )

    async def notify_delivery_failure(self, max_message: Any, error: Exception) -> None:
        """Best-effort аварийное уведомление, если основной форвардинг упал."""
        try:
            parsed = parse_message(max_message)
            parsed = await self._enrich_from_max(max_message, parsed)
            body = self._build_fallback_unknown_notice(parsed)
            body = f"{body}\n\n[bridge-error] {type(error).__name__}: {error}"
        except Exception:
            body = f"[!] Сообщение из MAX не доставлено в Telegram из-за ошибки bridge: {type(error).__name__}: {error}"

        fallback_chat_id = self._telegram.fallback_user_id
        if not fallback_chat_id:
            logger.error("Cannot send emergency notice: Telegram fallback user id is empty")
            return
        try:
            await self._telegram.send_text(chat_id=fallback_chat_id, text=body)
            logger.warning("Sent emergency notice to Telegram fallback chat %s", fallback_chat_id)
        except Exception:
            logger.exception("Cannot send emergency notice to Telegram")

    async def _send_with_migration_retry(
        self,
        *,
        target_chat_id: str,
        max_chat_title_norm: str,
        max_chat_title: str,
        send_action: Callable[[str], Awaitable[Any]],
    ) -> tuple[str, Any]:
        try:
            sent = await send_action(target_chat_id)
            return target_chat_id, sent
        except TelegramApiError as exc:
            migrated_chat_id = exc.migrate_to_chat_id
            if not migrated_chat_id or migrated_chat_id == str(target_chat_id):
                raise
            logger.warning(
                "Telegram chat %s upgraded to %s for MAX chat '%s'; update route and retry",
                target_chat_id,
                migrated_chat_id,
                max_chat_title,
            )
            self._storage.set_chat_route(
                max_chat_title_norm=max_chat_title_norm,
                telegram_chat_id=migrated_chat_id,
                telegram_chat_title=max_chat_title,
            )
            sent = await send_action(migrated_chat_id)
            return migrated_chat_id, sent

    def _resolve_telegram_reply_to(
        self, *, telegram_chat_id: str, max_chat_id: str, parsed: ParsedMessage
    ) -> int | None:
        if not parsed.reply_to_max_message_id:
            return None
        raw = self._storage.get_telegram_message_id_for_max(
            telegram_chat_id=telegram_chat_id,
            max_chat_id=max_chat_id,
            max_message_id=str(parsed.reply_to_max_message_id),
        )
        if not raw:
            return None
        try:
            return int(raw)
        except ValueError:
            return None

    @staticmethod
    def _prepend_max_reply_context(parsed: ParsedMessage, body: str) -> str:
        """Если в Telegram нет исходного сообщения — сохраняем контекст ответа текстом."""
        prev = (parsed.reply_preview_text or "").strip()
        if prev:
            quoted = "\n".join(f"> {line}" for line in prev.splitlines()[:25])
            return f"↪ ответ в MAX:\n{quoted}\n\n{body}"
        return f"↪ ответ в MAX (сообщение id={parsed.reply_to_max_message_id})\n\n{body}"

    @staticmethod
    def _format_caption(*, sender_name: str, chat_name: str, text: str, include_chat_name: bool) -> str:
        sender_name = (sender_name or "").strip() or "unknown"
        chat_name = (chat_name or "").strip() or "direct"

        if include_chat_name:
            header = f"{sender_name} / {chat_name}:"
        else:
            header = f"{sender_name}:"

        body = (text or "").strip()
        if body:
            return f"{header}\n{body}"
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

        await self._collect_message_attachments(
            message=max_message,
            parsed=parsed,
            source_tag="root",
        )

        link = getattr(max_message, "link", None)
        linked_message = getattr(link, "message", None)
        if linked_message is not None:
            # Для reply не копируем текст исходного сообщения в тело:
            # иначе получаем дубль (цитата + тот же текст как новое сообщение).
            is_reply = bool(parsed.reply_to_max_message_id)
            if not is_reply and not (parsed.text or "").strip():
                linked_text = str(getattr(linked_message, "text", "") or "").strip()
                if linked_text:
                    parsed.text = linked_text
            await self._collect_message_attachments(
                message=linked_message,
                parsed=parsed,
                source_tag="forward",
            )

        # Убираем дубли URL, если парсер и enrich нашли одинаковые вложения.
        parsed.image_urls = list(dict.fromkeys(parsed.image_urls))
        parsed.video_urls = list(dict.fromkeys(parsed.video_urls))
        parsed.file_urls = list(dict.fromkeys(parsed.file_urls))
        parsed.unknown_attachments = list(dict.fromkeys(parsed.unknown_attachments))
        return parsed

    async def _collect_message_attachments(
        self,
        *,
        message: Any,
        parsed: ParsedMessage,
        source_tag: str,
    ) -> None:
        attaches = getattr(message, "attaches", None) or []
        message_chat_id = getattr(message, "chat_id", None)
        message_id = getattr(message, "id", None)
        if message_chat_id is None:
            message_chat_id = parsed.chat_id

        for attach in attaches:
            if isinstance(attach, PhotoAttach):
                parsed.image_urls.extend(self._extract_photo_urls(attach))
                continue

            if isinstance(attach, VideoAttach):
                try:
                    video = await self._max_client.get_video_by_id(
                        chat_id=message_chat_id,
                        message_id=message_id,
                        video_id=attach.video_id,
                    )
                    video_url = getattr(video, "url", None)
                    if video_url:
                        parsed.video_urls.append(str(video_url))
                except Exception:
                    logger.exception("Cannot resolve video URL from Max (%s)", source_tag)
                continue

            if isinstance(attach, FileAttach):
                resolved = await self._resolve_file_attach_url(
                    message_chat_id=message_chat_id,
                    message_id=message_id,
                    attach=attach,
                )
                if resolved:
                    parsed.file_urls.append(resolved)
                else:
                    fallback = str(getattr(attach, "name", "") or "").strip()
                    if fallback:
                        parsed.text = self._append_missing_file_note(parsed.text, fallback)
                    else:
                        parsed.unknown_attachments.append(type(attach).__name__)
                continue

            if isinstance(attach, AudioAttach):
                audio_url = str(getattr(attach, "url", "") or "").strip()
                if audio_url:
                    parsed.file_urls.append(audio_url)
                else:
                    parsed.unknown_attachments.append(type(attach).__name__)
                continue

            if isinstance(attach, StickerAttach):
                sticker_url = str(getattr(attach, "url", "") or "").strip()
                if sticker_url:
                    parsed.image_urls.append(sticker_url)
                else:
                    parsed.unknown_attachments.append(type(attach).__name__)
                continue

            # Fallback на случай сырого Attach/нестандартного типа:
            if await self._resolve_generic_file_attach(
                message_chat_id=message_chat_id,
                message_id=message_id,
                attach=attach,
                parsed=parsed,
            ):
                continue

            urls = self._extract_any_urls(attach)
            if urls:
                parsed.file_urls.extend(urls)
                continue

            parsed.unknown_attachments.append(type(attach).__name__)

    async def _resolve_file_attach_url(self, *, message_chat_id: Any, message_id: Any, attach: FileAttach) -> str | None:
        file_id = getattr(attach, "file_id", None)
        if file_id is None or message_id is None:
            return None
        try:
            file_info = await self._max_client.get_file_by_id(
                chat_id=message_chat_id,
                message_id=message_id,
                file_id=file_id,
            )
            url = getattr(file_info, "url", None)
            return str(url) if url else None
        except Exception:
            logger.exception("Cannot resolve file URL from Max (file_id=%s)", file_id)
            return None

    async def _resolve_generic_file_attach(
        self,
        *,
        message_chat_id: Any,
        message_id: Any,
        attach: Any,
        parsed: ParsedMessage,
    ) -> bool:
        file_id = getattr(attach, "file_id", None)
        if file_id is None or message_id is None:
            return False
        try:
            file_info = await self._max_client.get_file_by_id(
                chat_id=message_chat_id,
                message_id=message_id,
                file_id=file_id,
            )
            url = getattr(file_info, "url", None)
            if url:
                parsed.file_urls.append(str(url))
                return True
        except Exception:
            logger.debug("Cannot resolve generic file attach from Max", exc_info=True)
        return False

    def _is_self_message(self, max_message: Any) -> bool:
        sender = getattr(max_message, "sender", None)
        me = getattr(self._max_client, "me", None)
        my_id = getattr(me, "id", None)
        if sender is None or my_id is None:
            return False
        return str(sender) == str(my_id)

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

    def _extract_any_urls(self, node: Any) -> list[str]:
        urls: list[str] = []
        seen_ids: set[int] = set()

        def walk(value: Any) -> None:
            if value is None:
                return
            obj_id = id(value)
            if obj_id in seen_ids:
                return
            seen_ids.add(obj_id)

            if isinstance(value, str):
                if value.startswith("http://") or value.startswith("https://"):
                    urls.append(value)
                return
            if isinstance(value, (list, tuple, set)):
                for item in value:
                    walk(item)
                return
            if isinstance(value, dict):
                for nested in value.values():
                    walk(nested)
                return
            if hasattr(value, "__dict__"):
                walk(vars(value))

        walk(node)
        return list(dict.fromkeys(urls))

    @staticmethod
    def _is_forward_attach_like(attach: Any) -> bool:
        name = type(attach).__name__.lower()
        if "forward" in name or "share" in name or "quote" in name:
            return True
        if hasattr(attach, "__dict__"):
            keys = {str(k).lower() for k in vars(attach).keys()}
            if {"forward", "forwarded", "forwards", "link", "message", "messages", "origin", "payload"} & keys:
                return True
        return False

    @staticmethod
    def _append_missing_file_note(current_text: str, file_name: str) -> str:
        text = (current_text or "").strip()
        note = f"[MAX forwarded file without direct URL] {file_name}"
        if not text:
            return note
        return f"{text}\n{note}"

    @staticmethod
    def _append_unknown_attachment_notice(*, parsed: ParsedMessage, text: str) -> str:
        if not parsed.unknown_attachments:
            return text
        unknown_preview = ", ".join(parsed.unknown_attachments[:5])
        suffix = f"\n\n[!] Неизвестный тип вложения из MAX: {unknown_preview}"
        return f"{text}{suffix}" if text else suffix.strip()

    @staticmethod
    def _build_fallback_unknown_notice(parsed: ParsedMessage) -> str:
        base = MaxToTelegramBridge._format_caption(
            sender_name=parsed.sender_name,
            chat_name=parsed.chat_name,
            text=parsed.text,
            include_chat_name=True,
        )
        unknown = ", ".join(parsed.unknown_attachments[:5]) if parsed.unknown_attachments else "unknown"
        return f"{base}\n\n[!] Неизвестный или пустой тип сообщения из MAX (attachments={unknown})."
