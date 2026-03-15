import aiosqlite
import os


class Database:
    def __init__(self, db_path: str):
        self.db_path = db_path
        self.db: aiosqlite.Connection | None = None

    async def init(self):
        os.makedirs(os.path.dirname(self.db_path), exist_ok=True)
        self.db = await aiosqlite.connect(self.db_path)
        self.db.row_factory = aiosqlite.Row
        await self._create_tables()

    async def _create_tables(self):
        await self.db.executescript("""
            CREATE TABLE IF NOT EXISTS tasks (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                type TEXT NOT NULL,
                source_chat_id INTEGER NOT NULL,
                source_topic_id INTEGER,
                target_chat_id INTEGER NOT NULL,
                target_topic_id INTEGER,
                mode TEXT DEFAULT 'copy',
                status TEXT DEFAULT 'running',
                last_synced_msg_id INTEGER DEFAULT 0,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS message_map (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                task_id INTEGER NOT NULL REFERENCES tasks(id),
                source_msg_id INTEGER NOT NULL,
                target_msg_id INTEGER NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
            CREATE INDEX IF NOT EXISTS idx_message_map_task
                ON message_map(task_id, source_msg_id);
        """)
        await self.db.commit()

    async def close(self):
        if self.db:
            await self.db.close()
