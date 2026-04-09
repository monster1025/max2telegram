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
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS message_mapping (
                    telegram_chat_id TEXT NOT NULL,
                    telegram_message_id TEXT NOT NULL,
                    max_chat_id TEXT NOT NULL,
                    max_message_id TEXT NOT NULL,
                    media_group_id TEXT,
                    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                    PRIMARY KEY (telegram_chat_id, telegram_message_id)
                )
                """
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_message_mapping_max ON message_mapping (max_chat_id, max_message_id)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_message_mapping_media_group ON message_mapping (telegram_chat_id, media_group_id)"
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS chat_routes (
                    max_chat_title_norm TEXT PRIMARY KEY,
                    telegram_chat_id TEXT NOT NULL,
                    telegram_chat_title TEXT,
                    created_at DATETIME DEFAULT CURRENT_TIMESTAMP
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

    def save_mapping(
        self,
        *,
        telegram_chat_id: str,
        telegram_message_id: str,
        max_chat_id: str,
        max_message_id: str,
        media_group_id: str | None = None,
    ) -> None:
        with closing(self._connect()) as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO message_mapping
                    (telegram_chat_id, telegram_message_id, max_chat_id, max_message_id, media_group_id)
                VALUES (?, ?, ?, ?, ?)
                """,
                (telegram_chat_id, telegram_message_id, max_chat_id, max_message_id, media_group_id),
            )
            conn.commit()

    def get_max_message_id_for_telegram(
        self, *, telegram_chat_id: str, telegram_message_id: str
    ) -> str | None:
        with closing(self._connect()) as conn:
            row = conn.execute(
                """
                SELECT max_message_id
                FROM message_mapping
                WHERE telegram_chat_id = ? AND telegram_message_id = ?
                """,
                (telegram_chat_id, telegram_message_id),
            ).fetchone()
            if not row:
                return None
            return str(row[0])

    def get_telegram_message_id_for_max(
        self, *, telegram_chat_id: str, max_chat_id: str, max_message_id: str
    ) -> str | None:
        """Обратный поиск: какое сообщение в Telegram соответствует паре MAX chat/message."""
        with closing(self._connect()) as conn:
            row = conn.execute(
                """
                SELECT telegram_message_id
                FROM message_mapping
                WHERE telegram_chat_id = ? AND max_chat_id = ? AND max_message_id = ?
                ORDER BY telegram_message_id
                LIMIT 1
                """,
                (telegram_chat_id, max_chat_id, max_message_id),
            ).fetchone()
            if not row:
                return None
            return str(row[0])

    def set_chat_route(
        self,
        *,
        max_chat_title_norm: str,
        telegram_chat_id: str,
        telegram_chat_title: str | None = None,
    ) -> None:
        with closing(self._connect()) as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO chat_routes (max_chat_title_norm, telegram_chat_id, telegram_chat_title)
                VALUES (?, ?, ?)
                """,
                (max_chat_title_norm, telegram_chat_id, telegram_chat_title),
            )
            conn.commit()

    def get_chat_route(self, *, max_chat_title_norm: str) -> str | None:
        with closing(self._connect()) as conn:
            row = conn.execute(
                "SELECT telegram_chat_id FROM chat_routes WHERE max_chat_title_norm = ?",
                (max_chat_title_norm,),
            ).fetchone()
            if not row:
                return None
            return str(row[0])
