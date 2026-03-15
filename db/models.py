"""数据库操作方法，封装 tasks 和 message_map 的 CRUD。"""
from db.database import Database


async def create_task(db: Database, task_type: str, source_chat_id: int,
                      target_chat_id: int, mode: str = "copy",
                      source_topic_id: int | None = None,
                      target_topic_id: int | None = None) -> int:
    cursor = await db.db.execute(
        """INSERT INTO tasks (type, source_chat_id, source_topic_id,
           target_chat_id, target_topic_id, mode)
           VALUES (?, ?, ?, ?, ?, ?)""",
        (task_type, source_chat_id, source_topic_id,
         target_chat_id, target_topic_id, mode),
    )
    await db.db.commit()
    return cursor.lastrowid


async def update_task_status(db: Database, task_id: int, status: str):
    await db.db.execute(
        "UPDATE tasks SET status=?, updated_at=CURRENT_TIMESTAMP WHERE id=?",
        (status, task_id),
    )
    await db.db.commit()


async def update_last_synced(db: Database, task_id: int, msg_id: int):
    await db.db.execute(
        "UPDATE tasks SET last_synced_msg_id=?, updated_at=CURRENT_TIMESTAMP WHERE id=?",
        (msg_id, task_id),
    )
    await db.db.commit()


async def get_task(db: Database, task_id: int) -> dict | None:
    cursor = await db.db.execute("SELECT * FROM tasks WHERE id=?", (task_id,))
    row = await cursor.fetchone()
    return dict(row) if row else None


async def get_tasks_by_status(db: Database, status: str) -> list[dict]:
    cursor = await db.db.execute("SELECT * FROM tasks WHERE status=?", (status,))
    return [dict(r) for r in await cursor.fetchall()]


async def save_message_map(db: Database, task_id: int,
                           source_msg_id: int, target_msg_id: int):
    await db.db.execute(
        "INSERT INTO message_map (task_id, source_msg_id, target_msg_id) VALUES (?,?,?)",
        (task_id, source_msg_id, target_msg_id))
    await db.db.commit()


async def get_all_active_tasks(db: Database) -> list[dict]:
    cursor = await db.db.execute(
        "SELECT * FROM tasks WHERE status IN ('running', 'paused', 'failed') ORDER BY id DESC"
    )
    return [dict(r) for r in await cursor.fetchall()]
