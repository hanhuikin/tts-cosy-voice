"""SQLite 元数据存储 + 音频文件管理。"""
from __future__ import annotations

import sqlite3
import threading
import time
import uuid
from pathlib import Path
from typing import Optional

DATA_DIR = Path(__file__).resolve().parents[1] / "data"
VOICES_DIR = DATA_DIR / "voices"
TASKS_DIR = DATA_DIR / "tasks"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS voices (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    prompt_text TEXT NOT NULL DEFAULT '',
    file TEXT NOT NULL,
    duration REAL NOT NULL DEFAULT 0,
    sample_rate INTEGER NOT NULL DEFAULT 16000,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS tasks (
    id TEXT PRIMARY KEY,
    text TEXT NOT NULL,
    model TEXT NOT NULL,
    mode TEXT NOT NULL,
    spk_id TEXT,
    voice_id TEXT,
    instruct_text TEXT,
    speed REAL NOT NULL DEFAULT 1.0,
    stream INTEGER NOT NULL DEFAULT 0,
    sample_rate INTEGER,
    duration REAL,
    file TEXT,
    status TEXT NOT NULL DEFAULT 'running',
    error TEXT,
    infer_ms INTEGER,
    first_chunk_ms INTEGER,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_tasks_created ON tasks(created_at DESC);
CREATE TABLE IF NOT EXISTS podcasts (
    id TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    source_type TEXT NOT NULL DEFAULT 'text',
    source_ref TEXT NOT NULL DEFAULT '',
    script_json TEXT NOT NULL DEFAULT '[]',
    voice_host TEXT NOT NULL DEFAULT '',
    voice_guest TEXT NOT NULL DEFAULT '',
    speed REAL NOT NULL DEFAULT 1.0,
    status TEXT NOT NULL DEFAULT 'draft',
    progress INTEGER NOT NULL DEFAULT 0,
    file TEXT,
    duration REAL,
    error TEXT,
    created_at TEXT NOT NULL
);
"""


def now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime())


def new_id() -> str:
    return uuid.uuid4().hex[:12]


class Storage:
    def __init__(self) -> None:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        VOICES_DIR.mkdir(parents=True, exist_ok=True)
        TASKS_DIR.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(str(DATA_DIR / "db.sqlite"), check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        self._lock = threading.Lock()
        with self._lock:
            self._db.executescript(_SCHEMA)
            self._db.commit()

    def _exec(self, sql: str, params: tuple = ()) -> None:
        with self._lock:
            self._db.execute(sql, params)
            self._db.commit()

    def _query(self, sql: str, params: tuple = ()) -> list[sqlite3.Row]:
        with self._lock:
            return self._db.execute(sql, params).fetchall()

    # ---------- voices ----------
    def add_voice(self, name: str, prompt_text: str, file: Path, duration: float, sample_rate: int,
                  vid: Optional[str] = None) -> dict:
        vid = vid or new_id()
        self._exec(
            "INSERT INTO voices(id,name,prompt_text,file,duration,sample_rate,created_at) VALUES(?,?,?,?,?,?,?)",
            (vid, name, prompt_text, str(file), duration, sample_rate, now_iso()),
        )
        return self.get_voice(vid)  # type: ignore[return-value]

    def get_voice(self, vid: str) -> Optional[dict]:
        rows = self._query("SELECT * FROM voices WHERE id=?", (vid,))
        return dict(rows[0]) if rows else None

    def update_voice(self, vid: str, name: Optional[str] = None, prompt_text: Optional[str] = None) -> Optional[dict]:
        """按需更新音色名称 / 参考文本, 返回更新后的记录。"""
        if self.get_voice(vid) is None:
            return None
        fields, params = [], []
        if name is not None:
            if not name.strip():
                raise ValueError("name cannot be empty")
            fields.append("name=?")
            params.append(name.strip())
        if prompt_text is not None:
            fields.append("prompt_text=?")
            params.append(prompt_text.strip())
        if fields:
            params.append(vid)
            self._exec(f"UPDATE voices SET {', '.join(fields)} WHERE id=?", tuple(params))
        return self.get_voice(vid)

    def list_voices(self) -> list[dict]:
        return [dict(r) for r in self._query("SELECT * FROM voices ORDER BY created_at DESC")]

    def delete_voice(self, vid: str) -> bool:
        v = self.get_voice(vid)
        if not v:
            return False
        self._exec("DELETE FROM voices WHERE id=?", (vid,))
        Path(v["file"]).unlink(missing_ok=True)
        return True

    # ---------- tasks ----------
    def create_task(self, spec) -> str:  # spec: engine.TTSRequest
        tid = new_id()
        self._exec(
            "INSERT INTO tasks(id,text,model,mode,spk_id,voice_id,instruct_text,speed,stream,created_at)"
            " VALUES(?,?,?,?,?,?,?,?,?,?)",
            (tid, spec.text, spec.model, spec.mode, spec.spk_id, spec.voice_id,
             spec.instruct_text, spec.speed, int(spec.stream), now_iso()),
        )
        return tid

    def finish_task(self, tid: str, file: Optional[Path], duration: Optional[float], sample_rate: int,
                    status: str, error: Optional[str], infer_ms: int, first_chunk_ms: Optional[int]) -> None:
        self._exec(
            "UPDATE tasks SET file=?, duration=?, sample_rate=?, status=?, error=?, infer_ms=?, first_chunk_ms=?"
            " WHERE id=?",
            (str(file) if file else None, duration, sample_rate, status, error, infer_ms, first_chunk_ms, tid),
        )

    def get_task(self, tid: str) -> Optional[dict]:
        rows = self._query("SELECT * FROM tasks WHERE id=?", (tid,))
        return dict(rows[0]) if rows else None

    def list_tasks(self, limit: int = 100, offset: int = 0, mode: Optional[str] = None, model: Optional[str] = None) -> list[dict]:
        sql, params = "SELECT * FROM tasks", []
        conds = []
        if mode:
            conds.append("mode=?")
            params.append(mode)
        if model:
            conds.append("model=?")
            params.append(model)
        if conds:
            sql += " WHERE " + " AND ".join(conds)
        sql += " ORDER BY created_at DESC, id DESC LIMIT ? OFFSET ?"
        params += [limit, offset]
        return [dict(r) for r in self._query(sql, tuple(params))]

    def delete_task(self, tid: str) -> bool:
        t = self.get_task(tid)
        if not t:
            return False
        self._exec("DELETE FROM tasks WHERE id=?", (tid,))
        if t.get("file"):
            Path(t["file"]).unlink(missing_ok=True)
        return True

    # ---------- podcasts ----------
    def add_episode(self, title: str, source_type: str, source_ref: str,
                    script_json: str, voice_host: str, voice_guest: str) -> dict:
        pid = new_id()
        self._exec(
            "INSERT INTO podcasts(id,title,source_type,source_ref,script_json,voice_host,voice_guest,created_at)"
            " VALUES(?,?,?,?,?,?,?,?)",
            (pid, title, source_type, source_ref, script_json, voice_host, voice_guest, now_iso()),
        )
        return self.get_episode(pid)  # type: ignore[return-value]

    def get_episode(self, pid: str) -> Optional[dict]:
        rows = self._query("SELECT * FROM podcasts WHERE id=?", (pid,))
        return dict(rows[0]) if rows else None

    def list_episodes(self, limit: int = 100) -> list[dict]:
        return [dict(r) for r in self._query(
            "SELECT * FROM podcasts ORDER BY created_at DESC, id DESC LIMIT ?", (limit,))]

    def update_episode(self, pid: str, **fields) -> None:
        if not fields:
            return
        allowed = {"title", "script_json", "voice_host", "voice_guest", "speed",
                   "status", "progress", "file", "duration", "error"}
        sets, params = [], []
        for k, v in fields.items():
            if k in allowed:
                sets.append(f"{k}=?")
                params.append(v)
        if sets:
            params.append(pid)
            self._exec(f"UPDATE podcasts SET {', '.join(sets)} WHERE id=?", tuple(params))

    def delete_episode(self, pid: str) -> bool:
        ep = self.get_episode(pid)
        if not ep:
            return False
        self._exec("DELETE FROM podcasts WHERE id=?", (pid,))
        if ep.get("file"):
            Path(ep["file"]).unlink(missing_ok=True)
        return True


storage = Storage()
