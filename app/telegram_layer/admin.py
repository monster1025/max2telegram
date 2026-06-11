from aiogram import Dispatcher, F
from aiogram.enums import ChatType
from aiogram.filters import Command, CommandObject
from aiogram.types import Message

from pymax.types.domain.enums import ChatType as MaxChatType

from app.config import Settings
from app.max_layer.client_holder import MaxClientHolder
from app.max_layer.formatter import resolve_sender_name
from app.storage.protocols import StoragePort


def register_admin_handlers(
  dp: Dispatcher,
  settings: Settings,
  storage: StoragePort,
  max_holder: MaxClientHolder,
) -> None:
  fallback = (
    F.chat.type == ChatType.PRIVATE,
    F.from_user.id == settings.fallback_user_id,
  )

  @dp.message(Command("start"), *fallback)
  async def cmd_start(message: Message) -> None:
    connected = max_holder.ready.is_set()
    status = "подключён" if connected else "ожидание подключения"
    await message.answer(
      "max2telegram bridge\n\n"
      f"Статус MAX: {status}\n\n"
      "Команды: /help, /list, /join, /leave, /last_messages"
    )

  @dp.message(Command("help"), *fallback)
  async def cmd_help(message: Message) -> None:
    await message.answer(
      "/start — статус\n"
      "/help — справка\n"
      "/list — активные маппинги\n"
      "/join <ссылка> — вступить в MAX-группу\n"
      "/leave <id или название> — выйти из MAX-чата\n"
      "/last_messages <id или название> — последние 10 сообщений"
    )

  @dp.message(Command("list"), *fallback)
  async def cmd_list(message: Message) -> None:
    mappings = await storage.list_mappings()
    if not mappings:
      await message.answer("Маппинги не найдены.")
      return
    lines = [
      f"• {m.display_name}\n  MAX: {m.max_chat_id} → TG thread: {m.tg_thread_id}"
      for m in mappings
    ]
    await message.answer("Активные маппинги:\n\n" + "\n".join(lines))

  @dp.message(Command("join"), *fallback)
  async def cmd_join(message: Message, command: CommandObject) -> None:
    if not command.args:
      await message.answer("Использование: /join <ссылка>")
      return
    client = await max_holder.wait_client()
    try:
      chat = await client.join_group(command.args.strip())
      await message.answer(f"Вступили в «{chat.title}» (id={chat.id})")
    except Exception as exc:
      await message.answer(f"Ошибка: {exc}")

  @dp.message(Command("leave"), *fallback)
  async def cmd_leave(message: Message, command: CommandObject) -> None:
    if not command.args:
      await message.answer("Использование: /leave <id или название>")
      return
    client = await max_holder.wait_client()
    target = command.args.strip()
    chat = await _resolve_chat(client, target)
    if chat is None:
      await message.answer("Чат не найден.")
      return
    if chat.type == MaxChatType.CHANNEL:
      await client.leave_channel(chat.id)
    else:
      await client.leave_group(chat.id)
    await message.answer(f"Вышли из «{chat.title}» (id={chat.id})")

  @dp.message(Command("last_messages"), *fallback)
  async def cmd_last_messages(message: Message, command: CommandObject) -> None:
    if not command.args:
      await message.answer("Использование: /last_messages <id или название>")
      return
    client = await max_holder.wait_client()
    target = command.args.strip()
    chat = await _resolve_chat(client, target)
    if chat is None:
      await message.answer("Чат не найден.")
      return
    history = await client.fetch_history(chat_id=chat.id, backward=10)
    if not history:
      await message.answer("Сообщений нет.")
      return
    lines = []
    for msg in reversed(history):
      sender = f"User {msg.sender}" if msg.sender else "?"
      if msg.sender:
        try:
          user = await client.get_user(msg.sender)
          sender = resolve_sender_name(user)
        except Exception:
          pass
      text = (msg.text or "")[:200]
      lines.append(f"[{msg.id}] {sender}: {text}")
    await message.answer("\n".join(lines) or "Пусто.")


async def _resolve_chat(client, target: str):
  if target.isdigit():
    try:
      return await client.get_chat(int(target))
    except Exception:
      return None
  if client.chats:
    for chat in client.chats:
      if chat.title and target.lower() in chat.title.lower():
        return chat
  return None
