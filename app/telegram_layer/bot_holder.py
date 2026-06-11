import asyncio
from typing import TYPE_CHECKING

if TYPE_CHECKING:
  from aiogram import Bot


class BotHolder:
  def __init__(self) -> None:
    self.bot: Bot | None = None
    self.ready = asyncio.Event()

  def set_bot(self, bot: "Bot") -> None:
    self.bot = bot
    self.ready.set()

  async def wait_bot(self) -> "Bot":
    await self.ready.wait()
    assert self.bot is not None
    return self.bot
