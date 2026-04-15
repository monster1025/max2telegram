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
        # PyMax часто использует enum-ы вида ChatType.CHANNEL, поэтому проверяем и точные значения, и подстроки.
        if v in {"channel", "канал"} or "channel" in v or "канал" in v:
            return "канал"
        if v in {"group", "supergroup", "группа"} or "group" in v or "груп" in v:
            return "группа"
        if v in {"direct", "dm", "private"} or "direct" in v or "private" in v or "dm" == v:
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


def _normalize(value: str) -> str:
    return str(value or "").strip().casefold()


def _deduplicate_chats(chats: list[Any]) -> list[Any]:
    """
    Возвращает уникальные чаты с сохранением исходного порядка.
    Сначала пытаемся уникализировать по chat.id, затем по нормализованному title.
    """
    unique: list[Any] = []
    seen_ids: set[str] = set()
    seen_titles: set[str] = set()

    for chat in chats:
        chat_id = getattr(chat, "id", None)
        if chat_id is not None:
            key_id = str(chat_id).strip()
            if key_id in seen_ids:
                continue
            seen_ids.add(key_id)
            unique.append(chat)
            continue

        key_title = _normalize(_max_chat_title(chat))
        if not key_title:
            unique.append(chat)
            continue
        if key_title in seen_titles:
            continue
        seen_titles.add(key_title)
        unique.append(chat)

    return unique


async def _refresh_chats_best_effort(max_client: MaxClient) -> None:
    # group.py: fetch_chats(marker=None) заполняет max_client.chats
    try:
        fetch = getattr(max_client, "fetch_chats", None)
        if fetch is not None:
            await fetch()
    except Exception:
        logger.debug("MAX fetch_chats failed (non-fatal)", exc_info=True)


def _find_chat_by_title(max_client: MaxClient, title: str) -> Any | None:
    wanted = _normalize(title)
    if not wanted:
        return None
    chats = _deduplicate_chats(list(getattr(max_client, "chats", []) or []))
    for c in chats:
        if _normalize(_max_chat_title(c)) == wanted:
            return c
    return None


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
            "/join <LINK> — присоединиться к группе/каналу по ссылке\n"
            "/leave <НАЗВАНИЕ> — покинуть указанный канал\n"
            "/last_messages <НАЗВАНИЕ> — последние 10 сообщений из канала\n"
            "/bind_max <НАЗВАНИЕ> — привязать текущий Telegram-чат к чату MAX"
        )

    if cmd == "/list":
        await _refresh_chats_best_effort(max_client)

        chats = _deduplicate_chats(list(getattr(max_client, "chats", []) or []))
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

    if cmd == "/leave":
        if not arg:
            return "Использование: /leave <НАЗВАНИЕ>"

        await _refresh_chats_best_effort(max_client)
        chat = _find_chat_by_title(max_client, arg)
        if chat is None:
            return f"Чат/канал не найден в активных: {arg}"

        chat_id = getattr(chat, "id", None)
        if chat_id is None:
            return "Не смог определить id чата."

        ctype = _infer_max_chat_type(chat)
        if ctype != "канал":
            return f"'{_max_chat_title(chat)}' — это не канал (тип: {ctype})."

        method = getattr(max_client, "leave_channel", None)
        if method is None:
            return "У клиента MAX нет метода leave_channel(chat_id)."

        try:
            await method(int(chat_id))
            return f"Ок: покинул канал '{_max_chat_title(chat)}'."
        except Exception as e:
            logger.exception("MAX leave_channel failed")
            return f"Ошибка при выходе: {e}"

    if cmd == "/last_messages":
        if not arg:
            return "Использование: /last_messages <НАЗВАНИЕ>"

        await _refresh_chats_best_effort(max_client)
        chat = _find_chat_by_title(max_client, arg)
        if chat is None:
            return f"Канал не найден в активных: {arg}"

        chat_id = getattr(chat, "id", None)
        if chat_id is None:
            return "Не смог определить id канала."

        try:
            # PyMax: MessageMixin.fetch_history(chat_id, from_time=None, forward=0, backward=200)
            # https://github.com/MaxApiTeam/PyMax/blob/041dedeb9f9461b3360e2881a8a18a767de74871/src/pymax/mixins/message.py#L594
            history = await max_client.fetch_history(chat_id=int(chat_id), forward=0, backward=10)
        except AttributeError:
            return "У клиента MAX нет метода fetch_history(chat_id, from_time=None, forward=0, backward=200)."
        except Exception as e:
            logger.exception("MAX fetch_history failed")
            return f"Ошибка при получении истории: {e}"

        messages = list(history or [])
        if not messages:
            return f"В канале '{_max_chat_title(chat)}' нет сообщений (или история недоступна)."

        lines: list[str] = []
        for m in messages[:10]:
            text_value = str(getattr(m, "text", "") or "").strip()
            mid = getattr(m, "id", None)
            if text_value:
                lines.append(f"- {mid}: {text_value}")
            else:
                lines.append(f"- {mid}: <без текста>")
        return f"Последние сообщения из '{_max_chat_title(chat)}':\n" + "\n".join(lines)

    return (
        "Неизвестная команда.\n"
        "Набери /help чтобы увидеть доступные команды."
    )

