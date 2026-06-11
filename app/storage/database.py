from sqlalchemy import event
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine


def create_engine(database_url: str) -> AsyncEngine:
  engine = create_async_engine(database_url, echo=False)

  @event.listens_for(engine.sync_engine, "connect")
  def _set_sqlite_pragma(dbapi_conn, _record) -> None:
    cursor = dbapi_conn.cursor()
    cursor.execute("PRAGMA journal_mode=WAL")
    cursor.execute("PRAGMA synchronous=NORMAL")
    cursor.close()

  return engine


def create_session_factory(engine: AsyncEngine) -> async_sessionmaker:
  return async_sessionmaker(engine, expire_on_commit=False)
