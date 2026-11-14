"""任务历史路由。"""
from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse

from .. import podcast
from ..storage import storage

router = APIRouter(prefix="/api")


@router.get("/tasks", summary="合成任务历史 (含调用来源 source)")
def list_tasks(limit: int = 100, offset: int = 0, mode: str | None = None, model: str | None = None):
    tasks = storage.list_tasks(limit=min(limit, 500), offset=offset, mode=mode, model=model)
    for t in tasks:
        t.pop("file", None)  # 不暴露绝对路径
    return {"tasks": tasks, "total": len(tasks)}


@router.get("/tasks/{tid}")
def get_task_detail(tid: str):
    """单条任务详情 (含状态/耗时/来源), 音频经 /tasks/{tid}/audio 下载。"""
    t = storage.get_task(tid)
    if not t:
        raise HTTPException(404, "task not found")
    t.pop("file", None)  # 不暴露绝对路径
    return t


@router.get("/tasks/{tid}/audio", summary="下载任务音频 (WAV / ?format=mp3)")
def task_audio(tid: str, format: str = "wav"):
    t = storage.get_task(tid)
    if not t:
        raise HTTPException(404, "task not found")
    if not t.get("file"):
        raise HTTPException(404, f"task has no audio (status={t['status']})")
    if (format or "wav").lower() == "mp3":
        try:
            mp3 = podcast.transcode_mp3(Path(t["file"]))
        except RuntimeError as e:
            raise HTTPException(503, str(e))
        return FileResponse(mp3, media_type="audio/mpeg", filename=f"{tid}.mp3")
    return FileResponse(t["file"], media_type="audio/wav", filename=f"{tid}.wav")


@router.delete("/tasks/{tid}", summary="删除任务记录")
def delete_task(tid: str):
    if not storage.delete_task(tid):
        raise HTTPException(404, "task not found")
    return {"ok": True}
