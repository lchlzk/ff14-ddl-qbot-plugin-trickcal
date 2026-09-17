"""Plugin-owned tables; preserve the existing shared database and records."""
from bot_tools.storage import Store


def ensure_board_schema(store: Store) -> None:
    with store.connect() as db:
        db.executescript("""
            CREATE TABLE IF NOT EXISTS trickcal_board_progress (
                owner TEXT PRIMARY KEY,
                bot TEXT NOT NULL,
                payload TEXT NOT NULL,
                updated REAL NOT NULL);
            CREATE INDEX IF NOT EXISTS trickcal_board_updated
                ON trickcal_board_progress(updated);
            CREATE TABLE IF NOT EXISTS trickcal_board_owner_alias (
                alias TEXT PRIMARY KEY,
                owner TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS trickcal_board_import_sessions (
                owner TEXT PRIMARY KEY,
                bot TEXT NOT NULL,
                scope TEXT NOT NULL,
                token TEXT NOT NULL,
                expires REAL NOT NULL,
                claimed INTEGER NOT NULL DEFAULT 0);
            CREATE INDEX IF NOT EXISTS trickcal_board_import_expires
                ON trickcal_board_import_sessions(expires);
        """)
