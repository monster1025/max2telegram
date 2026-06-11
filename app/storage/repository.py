from sqlalchemy import Column, Integer, String, Boolean, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.orm import DeclarativeBase

from app.models.domain import ChatMapping
from app.storage.protocols import StoragePort


class Base(DeclarativeBase):
  pass


class ChatMappingRow(Base):
  __tablename__ = "chat_mappings"

  max_chat_id = Column(Integer, primary_key=True)
  tg_chat_id = Column(Integer, nullable=False)
  tg_thread_id = Column(Integer, nullable=False)
  display_name = Column(String, nullable=False)
  is_dm = Column(Boolean, nullable=False, default=False)


class SyncMarkerRow(Base):
  __tablename__ = "sync_markers"

  max_chat_id = Column(Integer, primary_key=True)
  last_processed_message_id = Column(Integer, nullable=False, default=0)


class MessageLinkRow(Base):
  __tablename__ = "message_links"

  max_chat_id = Column(Integer, primary_key=True)
  max_message_id = Column(Integer, primary_key=True)
  tg_chat_id = Column(Integer, nullable=False)
  tg_thread_id = Column(Integer, nullable=False)
  tg_message_id = Column(Integer, nullable=False)


class SqliteStorage(StoragePort):
  def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
    self._session_factory = session_factory

  async def init(self) -> None:
    async with self._session_factory() as session:
      async with session.begin():
        conn = await session.connection()
        await conn.run_sync(Base.metadata.create_all)

  async def get_mapping_by_max_chat(self, max_chat_id: int) -> ChatMapping | None:
    async with self._session_factory() as session:
      row = await session.get(ChatMappingRow, max_chat_id)
      if row is None:
        return None
      return self._to_mapping(row)

  async def get_mapping_by_tg_thread(
    self, tg_chat_id: int, tg_thread_id: int
  ) -> ChatMapping | None:
    async with self._session_factory() as session:
      stmt = select(ChatMappingRow).where(
        ChatMappingRow.tg_chat_id == tg_chat_id,
        ChatMappingRow.tg_thread_id == tg_thread_id,
      )
      row = (await session.execute(stmt)).scalar_one_or_none()
      if row is None:
        return None
      return self._to_mapping(row)

  async def save_mapping(self, mapping: ChatMapping) -> None:
    async with self._session_factory() as session:
      async with session.begin():
        row = await session.get(ChatMappingRow, mapping.max_chat_id)
        if row is None:
          session.add(
            ChatMappingRow(
              max_chat_id=mapping.max_chat_id,
              tg_chat_id=mapping.tg_chat_id,
              tg_thread_id=mapping.tg_thread_id,
              display_name=mapping.display_name,
              is_dm=mapping.is_dm,
            )
          )
        else:
          row.tg_chat_id = mapping.tg_chat_id
          row.tg_thread_id = mapping.tg_thread_id
          row.display_name = mapping.display_name
          row.is_dm = mapping.is_dm

  async def list_mappings(self) -> list[ChatMapping]:
    async with self._session_factory() as session:
      rows = (await session.execute(select(ChatMappingRow))).scalars().all()
      return [self._to_mapping(row) for row in rows]

  async def get_sync_marker(self, max_chat_id: int) -> int | None:
    async with self._session_factory() as session:
      row = await session.get(SyncMarkerRow, max_chat_id)
      return row.last_processed_message_id if row else None

  async def update_sync_marker(self, max_chat_id: int, message_id: int) -> None:
    async with self._session_factory() as session:
      async with session.begin():
        await session.merge(
          SyncMarkerRow(max_chat_id=max_chat_id, last_processed_message_id=message_id)
        )

  async def save_message_link(
    self,
    max_chat_id: int,
    max_message_id: int,
    tg_chat_id: int,
    tg_thread_id: int,
    tg_message_id: int,
  ) -> None:
    async with self._session_factory() as session:
      async with session.begin():
        await session.merge(
          MessageLinkRow(
            max_chat_id=max_chat_id,
            max_message_id=max_message_id,
            tg_chat_id=tg_chat_id,
            tg_thread_id=tg_thread_id,
            tg_message_id=tg_message_id,
          )
        )

  async def get_tg_message_by_max(
    self, max_chat_id: int, max_message_id: int
  ) -> tuple[int, int, int] | None:
    async with self._session_factory() as session:
      row = await session.get(MessageLinkRow, (max_chat_id, max_message_id))
      if row is None:
        return None
      return row.tg_chat_id, row.tg_thread_id, row.tg_message_id

  async def get_max_message_by_tg(
    self, tg_chat_id: int, tg_message_id: int
  ) -> tuple[int, int] | None:
    async with self._session_factory() as session:
      stmt = select(MessageLinkRow).where(
        MessageLinkRow.tg_chat_id == tg_chat_id,
        MessageLinkRow.tg_message_id == tg_message_id,
      )
      row = (await session.execute(stmt)).scalar_one_or_none()
      if row is None:
        return None
      return row.max_chat_id, row.max_message_id

  @staticmethod
  def _to_mapping(row: ChatMappingRow) -> ChatMapping:
    return ChatMapping(
      max_chat_id=row.max_chat_id,
      tg_chat_id=row.tg_chat_id,
      tg_thread_id=row.tg_thread_id,
      display_name=row.display_name,
      is_dm=row.is_dm,
    )
