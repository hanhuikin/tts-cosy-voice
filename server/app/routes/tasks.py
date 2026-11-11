"""任务历史路由。"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse

from ..storage import storage

router = APIRouter(prefix="/api")


@router.get("/tasks")
def list_tasks(limit: int = 100, offset: int = 0, mode: str | None = None, model: str | None = None):
    tasks = storage.list_tasks(limit=min(limit, 500), offset=offset, mode=mode, model=model)
    for t in tasks:
        t.pop("file", None)  # 不暴露绝对路径
    return {"tasks": tasks, "total": len(tasks)}


@router.get("/tasks/{tid}/audio")
def task_audio(tid: str):
    t = storage.get_task(tid)
    if not t:
        raise HTTPException(404, "task not found")
    if not t.get("file"):
        raise HTTPException(404, f"task has no audio (status={t['status']})")
    return FileResponse(t["file"], media_type="audio/wav", filename=f"{tid}.wav")


@router.delete("/tasks/{tid}")
def delete_task(tid: str):
    if not storage.delete_task(tid):
        raise HTTPException(404, "task not found")
    return {"ok": True}
