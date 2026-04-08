import sqlite3
from contextlib import closing


class BridgeStorage:
    def __init__(self, db_path: str) -> None:
        self._db_path = db_path
        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        return sqlite3.connect(self._db_path)

    def _init_db(self) -> None:
        with closing(self._connect()) as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS forwarded_messages (
                    message_id TEXT NOT NULL,
                    chat_id TEXT NOT NULL,
                    forwarded_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                    PRIMARY KEY (message_id, chat_id)
                )
                """
            )
            conn.commit()

    def was_forwarded(self, message_id: str, chat_id: str) -> bool:
        with closing(self._connect()) as conn:
            row = conn.execute(
                "SELECT 1 FROM forwarded_messages WHERE message_id = ? AND chat_id = ?",
                (message_id, chat_id),
            ).fetchone()
            return row is not None

    def mark_forwarded(self, message_id: str, chat_id: str) -> None:
        with closing(self._connect()) as conn:
            conn.execute(
                "INSERT OR IGNORE INTO forwarded_messages (message_id, chat_id) VALUES (?, ?)",
                (message_id, chat_id),
            )
            conn.commit()
