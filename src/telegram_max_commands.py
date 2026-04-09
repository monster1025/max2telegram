import logging
from typing import Any

from pymax import MaxClient

from telegram_api import TelegramClient

logger = logging.getLogger(__name__)


def _is_private_chat(message: dict[str, Any]) -> bool:
    chat = message.get("chat")
    if not isinstance(chat, dict):
        return False
    return str(chat.get("type") or "").strip().casefold() == "private"


def _sender_id(message: dict[str, Any]) -> str:
    sender = message.get("from")
    if not isinstance(sender, dict):
        return ""
    return str(sender.get("id") or "")


def _chat_id(message: dict[str, Any]) -> str:
    chat = message.get("chat")
    if not isinstance(chat, dict):
        return ""
    return str(chat.get("id") or "")


def _infer_max_chat_type(chat_obj: Any) -> str:
    """
    Возвращает один из: direct / группа / канал
    Best-effort: структура объектов pymax может отличаться между версиями.
    """
    # 1) Явные признаки по полям.
    for name in ("type", "chat_type", "kind"):
        value = getattr(chat_obj, name, None)
        if value is None:
            continue
        v = str(value).strip().casefold()
        if v in {"channel", "канал"}:
            return "канал"
        if v in {"group", "supergroup", "группа"}:
            return "группа"
        if v in {"direct", "dm", "private"}:
            return "direct"

    # 2) Флаги.
    for true_names, mapped in (
        (("is_channel", "channel"), "канал"),
        (("is_group", "group"), "группа"),
        (("is_direct", "direct"), "direct"),
    ):
        for n in true_names:
            try:
                if getattr(chat_obj, n, False) is True:
                    return mapped
            except Exception:
                continue

    # 3) Эвристика по количеству участников.
    for n in ("members_count", "participants", "participants_count", "member_count"):
        try:
            value = getattr(chat_obj, n, None)
        except Exception:
            value = None
        if isinstance(value, int):
            return "группа" if value > 2 else "direct"

    return "direct"


def _max_chat_title(chat_obj: Any) -> str:
    for n in ("title", "name", "chat_title"):
        try:
            value = getattr(chat_obj, n, None)
        except Exception:
            value = None
        if isinstance(value, str) and value.strip():
            return value.strip()
    # запасной вариант
    cid = getattr(chat_obj, "id", None)
    return f"chat {cid}" if cid is not None else "chat"


async def _join_by_link(max_client: MaxClient, link: str) -> str:
    """
    PyMax: вступление в группу по ссылке — join_group(link).
    См. исходники: https://github.com/MaxApiTeam/PyMax/blob/041dedeb9f9461b3360e2881a8a18a767de74871/src/pymax/mixins/group.py#L260
    """
    method = getattr(max_client, "join_group", None)
    if method is None:
        return "Не смог присоединиться: у клиента MAX нет метода join_group(link)."

    try:
        await method(link)
        return "Ок: вступил по ссылке."
    except Exception as e:
        logger.exception("MAX join_group failed")
        return f"Ошибка при присоединении: {e}"


async def handle_control_command(
    message: dict[str, Any],
    *,
    max_client: MaxClient,
    telegram: TelegramClient,
) -> str | None:
    """
    Возвращает текст ответа, если команда обработана.
    Если None — не команда/не наш случай.
    """
    text = str(message.get("text") or "").strip()
    if not text.startswith("/"):
        return None

    if not _is_private_chat(message):
        return None

    if _sender_id(message) != str(telegram.fallback_user_id):
        return None

    cmd, *rest = text.split(maxsplit=1)
    cmd = cmd.split("@", 1)[0].strip().casefold()
    arg = rest[0].strip() if rest else ""

    if cmd == "/help":
        return (
            "Команды управления MAX (только для fallback_user_id в личке):\n"
            "/help — справка\n"
            "/list — список активных чатов MAX\n"
            "/join <LINK> — присоединиться к группе/каналу по ссылке"
        )

    if cmd == "/list":
        # Обновим список чатов, чтобы /list был полезнее сразу после запуска.
        try:
            fetch = getattr(max_client, "fetch_chats", None)
            if fetch is not None:
                await fetch()
        except Exception:
            logger.debug("MAX fetch_chats failed (non-fatal)", exc_info=True)

        chats = list(getattr(max_client, "chats", []) or [])
        if not chats:
            return "Список чатов пуст (или клиент MAX ещё не успел их загрузить)."

        lines: list[str] = []
        for c in chats:
            title = _max_chat_title(c)
            ctype = _infer_max_chat_type(c)
            lines.append(f"- {title} ({ctype})")
        return "Активные чаты MAX:\n" + "\n".join(lines)

    if cmd == "/join":
        if not arg:
            return "Использование: /join <LINK>"
        return await _join_by_link(max_client, arg)

    return (
        "Неизвестная команда.\n"
        "Набери /help чтобы увидеть доступные команды."
    )

