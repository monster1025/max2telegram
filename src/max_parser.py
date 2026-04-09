from typing import Any

from models import ParsedMessage


def _stringify(value: Any) -> str:
    if value is None:
        return ""
    return str(value).strip()


def _get_attr(obj: Any, names: list[str], default: Any = None) -> Any:
    for name in names:
        if hasattr(obj, name):
            value = getattr(obj, name)
            if value is not None:
                return value
    return default


def _as_dict(obj: Any) -> dict[str, Any]:
    if isinstance(obj, dict):
        return obj
    if hasattr(obj, "__dict__"):
        return vars(obj)
    return {}


def _is_image(media_type: str) -> bool:
    value = media_type.lower()
    return "image" in value or "photo" in value or value in {"jpg", "jpeg", "png", "webp"}


def _is_video(media_type: str) -> bool:
    value = media_type.lower()
    return "video" in value or value in {"mp4", "mov", "mkv", "avi"}


def _extract_media_urls(message: Any) -> tuple[list[str], list[str]]:
    image_urls: list[str] = []
    video_urls: list[str] = []

    # В PyMax рабочее поле для вложений обычно называется attaches.
    raw_attachments = _get_attr(message, ["attaches", "attachments", "media", "files"], default=[]) or []
    for item in raw_attachments:
        data = _as_dict(item)
        media_type = _stringify(data.get("type") or data.get("media_type") or data.get("kind"))
        url = _stringify(
            data.get("base_url")
            or data.get("url")
            or data.get("link")
            or data.get("download_url")
            or data.get("src")
        )

        if not url:
            nested = data.get("file") or data.get("payload")
            nested_data = _as_dict(nested)
            url = _stringify(
                nested_data.get("base_url")
                or nested_data.get("url")
                or nested_data.get("link")
                or nested_data.get("download_url")
                or nested_data.get("src")
            )
            if not media_type:
                media_type = _stringify(nested_data.get("type") or nested_data.get("media_type"))

        if not url:
            continue

        if _is_image(media_type):
            image_urls.append(url)
        elif _is_video(media_type):
            video_urls.append(url)

    return image_urls, video_urls


def _extract_max_reply(message: Any) -> tuple[str | None, str | None]:
    """Извлекает id сообщения MAX, на которое ответили, и короткий превью-текст (если есть)."""
    link = _get_attr(message, ["link"], default=None)
    if link is None:
        return None, None
    inner = _get_attr(link, ["message"], default=None)
    if inner is None:
        return None, None
    mid = _stringify(_get_attr(inner, ["id", "message_id", "mid"]))
    if not mid:
        return None, None
    preview = _stringify(_get_attr(inner, ["text", "message", "body"]))
    if preview and len(preview) > 500:
        preview = preview[:497] + "..."
    return mid, preview or None


def parse_message(message: Any) -> ParsedMessage:
    sender = _get_attr(message, ["sender", "sender_name", "author"], default="unknown")
    sender_data = _as_dict(sender)
    sender_name = (
        _stringify(sender_data.get("nickname"))
        or _stringify(sender_data.get("username"))
        or _stringify(sender_data.get("name"))
        or _stringify(sender)
        or "unknown"
    )

    chat_name = (
        _stringify(_get_attr(message, ["chat_title", "chat_name", "group_name"]))
        or _stringify(_get_attr(message, ["chat"], default=""))
        or "direct"
    )

    message_id = _stringify(_get_attr(message, ["id", "message_id", "mid"])) or "unknown-id"
    chat_id = _stringify(_get_attr(message, ["chat_id", "dialog_id", "peer_id"])) or "unknown-chat"
    text = _stringify(_get_attr(message, ["text", "message", "body"]))

    image_urls, video_urls = _extract_media_urls(message)
    reply_mid, reply_preview = _extract_max_reply(message)
    return ParsedMessage(
        message_id=message_id,
        chat_id=chat_id,
        sender_name=sender_name,
        chat_name=chat_name,
        text=text,
        image_urls=image_urls,
        video_urls=video_urls,
        reply_to_max_message_id=reply_mid,
        reply_preview_text=reply_preview,
    )
