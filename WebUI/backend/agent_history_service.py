"""Agent 历史记录持久化服务。

使用 SQLite 存储智能 Agent 的会话历史，支持 CRUD 操作。
"""
from __future__ import annotations

import json
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

WORKSPACE_ROOT: Path = Path(__file__).resolve().parents[2]
AGENT_DB_FILE: Path = WORKSPACE_ROOT / "WebUI" / "backend" / "agent_history.sqlite3"

_db_initialized = False


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(str(AGENT_DB_FILE), timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def init_db() -> None:
    global _db_initialized
    if _db_initialized:
        return
    AGENT_DB_FILE.parent.mkdir(parents=True, exist_ok=True)
    conn = _connect()
    try:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS agent_sessions (
                session_id   TEXT PRIMARY KEY,
                mode         TEXT NOT NULL,
                title        TEXT NOT NULL DEFAULT '',
                messages     TEXT NOT NULL DEFAULT '[]',
                created_at   TEXT NOT NULL,
                updated_at   TEXT NOT NULL
            )
        """)
        conn.commit()
    finally:
        conn.close()
    _db_initialized = True


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def create_session(mode: str, title: str, messages: list[dict[str, Any]]) -> dict[str, Any]:
    init_db()
    session_id = str(uuid.uuid4())
    now = _now_iso()
    messages_json = json.dumps(messages, ensure_ascii=False)
    conn = _connect()
    try:
        conn.execute(
            "INSERT INTO agent_sessions (session_id, mode, title, messages, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?)",
            (session_id, mode, title, messages_json, now, now),
        )
        conn.commit()
    finally:
        conn.close()
    return {"session_id": session_id, "mode": mode, "title": title, "created_at": now, "updated_at": now}


def update_session(session_id: str, title: str | None, messages: list[dict[str, Any]] | None) -> dict[str, Any] | None:
    init_db()
    now = _now_iso()
    conn = _connect()
    try:
        row = conn.execute("SELECT * FROM agent_sessions WHERE session_id = ?", (session_id,)).fetchone()
        if not row:
            return None
        new_title = title if title is not None else row["title"]
        new_messages = json.dumps(messages, ensure_ascii=False) if messages is not None else row["messages"]
        conn.execute(
            "UPDATE agent_sessions SET title = ?, messages = ?, updated_at = ? WHERE session_id = ?",
            (new_title, new_messages, now, session_id),
        )
        conn.commit()
    finally:
        conn.close()
    return {"session_id": session_id, "title": new_title, "updated_at": now}


def get_session(session_id: str) -> dict[str, Any] | None:
    init_db()
    conn = _connect()
    try:
        row = conn.execute("SELECT * FROM agent_sessions WHERE session_id = ?", (session_id,)).fetchone()
        if not row:
            return None
        return {
            "session_id": row["session_id"],
            "mode": row["mode"],
            "title": row["title"],
            "messages": json.loads(row["messages"]),
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }
    finally:
        conn.close()


def list_sessions(limit: int = 50) -> list[dict[str, Any]]:
    init_db()
    safe_limit = max(1, min(int(limit), 500))
    conn = _connect()
    try:
        rows = conn.execute(
            "SELECT session_id, mode, title, created_at, updated_at FROM agent_sessions ORDER BY updated_at DESC LIMIT ?",
            (safe_limit,),
        ).fetchall()
        return [
            {
                "session_id": r["session_id"],
                "mode": r["mode"],
                "title": r["title"],
                "created_at": r["created_at"],
                "updated_at": r["updated_at"],
            }
            for r in rows
        ]
    finally:
        conn.close()


def delete_session(session_id: str) -> bool:
    init_db()
    conn = _connect()
    try:
        cursor = conn.execute("DELETE FROM agent_sessions WHERE session_id = ?", (session_id,))
        conn.commit()
        return cursor.rowcount > 0
    finally:
        conn.close()


def clear_all_sessions() -> int:
    init_db()
    conn = _connect()
    try:
        cursor = conn.execute("DELETE FROM agent_sessions")
        conn.commit()
        return cursor.rowcount
    finally:
        conn.close()
