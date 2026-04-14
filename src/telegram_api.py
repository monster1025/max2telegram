import asyncio
import json
from typing import Any

import requests


class TelegramApiError(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        method: str | None = None,
        status_code: int | None = None,
        error_code: int | None = None,
        description: str | None = None,
        parameters: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.method = method
        self.status_code = status_code
        self.error_code = error_code
        self.description = description
        self.parameters = parameters or {}

    @property
    def migrate_to_chat_id(self) -> str | None:
        value = self.parameters.get("migrate_to_chat_id")
        if value is None:
            return None
        return str(value)


class TelegramClient:
    def __init__(self, bot_token: str, fallback_user_id: str, timeout: int = 30) -> None:
        self._base_url = f"https://api.telegram.org/bot{bot_token}"
        self._file_base_url = f"https://api.telegram.org/file/bot{bot_token}"
        self._fallback_user_id = fallback_user_id
        self._timeout = timeout
        self._chat_title_to_id: dict[str, str] = {}
        self._me: dict[str, Any] | None = None

    @property
    def fallback_user_id(self) -> str:
        return str(self._fallback_user_id or "")

    async def resolve_target_chat_id(self, max_chat_name: str) -> tuple[str, bool]:
        chat_id = await self._find_chat_id_by_title(max_chat_name)
        if chat_id:
            return chat_id, True
        return self._fallback_user_id, False

    async def send_text(
        self,
        chat_id: str,
        text: str,
        *,
        reply_to_message_id: int | None = None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "chat_id": chat_id,
            "text": text,
            "disable_web_page_preview": True,
        }
        if reply_to_message_id is not None:
            payload["reply_to_message_id"] = reply_to_message_id
        return await self._request("sendMessage", payload)

    async def send_photo(
        self,
        chat_id: str,
        photo_url: str,
        caption: str | None = None,
        *,
        reply_to_message_id: int | None = None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "chat_id": chat_id,
            "photo": photo_url,
        }
        if caption:
            payload["caption"] = caption
        if reply_to_message_id is not None:
            payload["reply_to_message_id"] = reply_to_message_id
        return await self._request("sendPhoto", payload)

    async def send_video(
        self,
        chat_id: str,
        video_url: str,
        caption: str | None = None,
        *,
        reply_to_message_id: int | None = None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "chat_id": chat_id,
            "video": video_url,
            "supports_streaming": True,
        }
        if caption:
            payload["caption"] = caption
        if reply_to_message_id is not None:
            payload["reply_to_message_id"] = reply_to_message_id
        return await self._request("sendVideo", payload)

    async def send_media_group(
        self,
        chat_id: str,
        image_urls: list[str],
        video_urls: list[str],
        caption: str | None = None,
        *,
        reply_to_message_id: int | None = None,
    ) -> list[dict[str, Any]]:
        media: list[dict[str, Any]] = []

        for url in image_urls:
            media.append({"type": "photo", "media": url})

        for url in video_urls:
            media.append({"type": "video", "media": url, "supports_streaming": True})

        if not media:
            return []

        if caption:
            media[0]["caption"] = caption

        mg_payload: dict[str, Any] = {
            "chat_id": chat_id,
            "media": media,
        }
        if reply_to_message_id is not None:
            mg_payload["reply_to_message_id"] = reply_to_message_id
        data = await self._request(
            "sendMediaGroup",
            mg_payload,
        )
        result = data.get("result", [])
        if not isinstance(result, list):
            return []
        return [m for m in result if isinstance(m, dict)]

    async def get_me(self) -> dict[str, Any]:
        if self._me is not None:
            return self._me
        data = await self._request("getMe", {})
        me = data.get("result")
        if not isinstance(me, dict):
            raise TelegramApiError(f"Telegram getMe: unexpected payload {data}")
        self._me = me
        return me

    async def get_updates(self, *, offset: int | None, timeout: int = 25, limit: int = 100) -> list[dict[str, Any]]:
        payload: dict[str, Any] = {
            "timeout": timeout,
            "limit": limit,
            # Реагируем только на новые сообщения/посты (текст, фото, видео).
            "allowed_updates": ["message", "channel_post"],
        }
        if offset is not None:
            payload["offset"] = offset
        data = await self._request("getUpdates", payload)
        result = data.get("result", [])
        if not isinstance(result, list):
            return []
        updates = [u for u in result if isinstance(u, dict)]
        # Важно: не делаем getUpdates нигде больше (иначе 409 Conflict).
        # Наполняем кэш чатов только из этого потока.
        for upd in updates:
            for container in ("message", "channel_post"):
                msg = upd.get(container)
                if not isinstance(msg, dict):
                    continue
                chat = msg.get("chat")
                if not isinstance(chat, dict):
                    continue
                self._cache_chat(chat)
        return updates

    async def get_file_url(self, file_id: str) -> str:
        data = await self._request("getFile", {"file_id": file_id})
        result = data.get("result")
        if not isinstance(result, dict):
            raise TelegramApiError(f"Telegram getFile: unexpected payload {data}")
        file_path = result.get("file_path")
        if not isinstance(file_path, str) or not file_path.strip():
            raise TelegramApiError(f"Telegram getFile: missing file_path {data}")
        return f"{self._file_base_url}/{file_path}"

    async def add_reaction(self, *, chat_id: str, message_id: str, emoji: str) -> None:
        # setMessageReaction доступен не везде/не всегда; ошибки реакции не должны ломать бридж
        await self._request(
            "setMessageReaction",
            {
                "chat_id": chat_id,
                "message_id": int(message_id),
                "reaction": [{"type": "emoji", "emoji": emoji}],
            },
        )

    def _cache_chat(self, chat: dict[str, Any]) -> None:
        title_value = self._extract_chat_title(chat)
        chat_id = chat.get("id")
        if title_value and chat_id is not None:
            self._chat_title_to_id[self._normalize_title(title_value)] = str(chat_id)

    async def _find_chat_id_by_title(self, chat_title: str) -> str | None:
        normalized = self._normalize_title(chat_title)
        if not normalized:
            return None

        cached = self._chat_title_to_id.get(normalized)
        if cached:
            return cached
        # Не дергаем getUpdates здесь — это вызовет конфликт с polling циклом.
        return None

    @staticmethod
    def _extract_chat_title(chat: dict[str, Any]) -> str:
        return str(chat.get("title") or chat.get("username") or "").strip()

    @staticmethod
    def _normalize_title(value: str) -> str:
        return value.strip().casefold()

    async def _request(self, method: str, payload: dict[str, Any]) -> dict[str, Any]:
        url = f"{self._base_url}/{method}"

        def _do_request() -> requests.Response:
            return requests.post(url, json=payload, timeout=self._timeout)

        response = await asyncio.to_thread(_do_request)
        if response.status_code >= 400:
            error_code: int | None = None
            description: str | None = None
            parameters: dict[str, Any] = {}
            try:
                payload_data = response.json()
                if isinstance(payload_data, dict):
                    if isinstance(payload_data.get("error_code"), int):
                        error_code = payload_data.get("error_code")
                    if isinstance(payload_data.get("description"), str):
                        description = payload_data.get("description")
                    if isinstance(payload_data.get("parameters"), dict):
                        parameters = payload_data.get("parameters", {})
            except (json.JSONDecodeError, ValueError):
                payload_data = None

            raise TelegramApiError(
                f"Telegram HTTP error on {method}: {response.status_code} {response.text}",
                method=method,
                status_code=response.status_code,
                error_code=error_code,
                description=description,
                parameters=parameters,
            )
        data = response.json()
        if not data.get("ok"):
            raise TelegramApiError(
                f"Telegram API error on {method}: {data}",
                method=method,
                error_code=data.get("error_code") if isinstance(data.get("error_code"), int) else None,
                description=data.get("description") if isinstance(data.get("description"), str) else None,
                parameters=data.get("parameters") if isinstance(data.get("parameters"), dict) else None,
            )
        return data
