import asyncio
from typing import Any

import requests


class TelegramApiError(RuntimeError):
    pass


class TelegramClient:
    def __init__(self, bot_token: str, fallback_user_id: str, timeout: int = 30) -> None:
        self._base_url = f"https://api.telegram.org/bot{bot_token}"
        self._fallback_user_id = fallback_user_id
        self._timeout = timeout
        self._chat_title_to_id: dict[str, str] = {}

    async def resolve_target_chat_id(self, max_chat_name: str) -> tuple[str, bool]:
        chat_id = await self._find_chat_id_by_title(max_chat_name)
        if chat_id:
            return chat_id, True
        return self._fallback_user_id, False

    async def send_text(self, chat_id: str, text: str) -> None:
        await self._request(
            "sendMessage",
            {
                "chat_id": chat_id,
                "text": text,
                "disable_web_page_preview": True,
            },
        )

    async def send_photo(self, chat_id: str, photo_url: str, caption: str | None = None) -> None:
        payload: dict[str, Any] = {
            "chat_id": chat_id,
            "photo": photo_url,
        }
        if caption:
            payload["caption"] = caption
        await self._request("sendPhoto", payload)

    async def send_video(self, chat_id: str, video_url: str, caption: str | None = None) -> None:
        payload: dict[str, Any] = {
            "chat_id": chat_id,
            "video": video_url,
            "supports_streaming": True,
        }
        if caption:
            payload["caption"] = caption
        await self._request("sendVideo", payload)

    async def send_media_group(
        self,
        chat_id: str,
        image_urls: list[str],
        video_urls: list[str],
        caption: str | None = None,
    ) -> None:
        media: list[dict[str, Any]] = []

        for url in image_urls:
            media.append({"type": "photo", "media": url})

        for url in video_urls:
            media.append({"type": "video", "media": url, "supports_streaming": True})

        if not media:
            return

        if caption:
            media[0]["caption"] = caption

        await self._request(
            "sendMediaGroup",
            {
                "chat_id": chat_id,
                "media": media,
            },
        )

    async def _find_chat_id_by_title(self, chat_title: str) -> str | None:
        normalized = self._normalize_title(chat_title)
        if not normalized:
            return None

        cached = self._chat_title_to_id.get(normalized)
        if cached:
            return cached

        response = await self._request("getUpdates", {"timeout": 0, "limit": 100})
        for update in response.get("result", []):
            for container in ("message", "edited_message", "channel_post", "edited_channel_post"):
                message = update.get(container)
                if not isinstance(message, dict):
                    continue
                chat = message.get("chat")
                if not isinstance(chat, dict):
                    continue

                title_value = self._extract_chat_title(chat)
                chat_id = chat.get("id")
                if title_value and chat_id is not None:
                    self._chat_title_to_id[self._normalize_title(title_value)] = str(chat_id)

        return self._chat_title_to_id.get(normalized)

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
            raise TelegramApiError(
                f"Telegram HTTP error on {method}: {response.status_code} {response.text}"
            )
        data = response.json()
        if not data.get("ok"):
            raise TelegramApiError(f"Telegram API error on {method}: {data}")
        return data
