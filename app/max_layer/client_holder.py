import asyncio
from typing import TYPE_CHECKING

if TYPE_CHECKING:
  from pymax import WebClient


class MaxClientHolder:
  def __init__(self) -> None:
    self.client: WebClient | None = None
    self.ready = asyncio.Event()
    self.my_user_id: int | None = None

  def set_client(self, client: "WebClient", my_user_id: int | None) -> None:
    self.client = client
    self.my_user_id = my_user_id
    self.ready.set()

  async def wait_client(self) -> "WebClient":
    await self.ready.wait()
    assert self.client is not None
    return self.client
