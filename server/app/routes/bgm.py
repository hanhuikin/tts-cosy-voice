"""背景音乐管理路由: 列表 / 上传 / 改名 / 删除 / 试听。

BGM 文件保存在 server/data/bgm/; 删除与改名会同步更新引用它的剧集,
避免剧集合成时引用失效 (引用缺失时混音器静默跳过)。
"""
from __future__ import annotations

import re
from pathlib import Path

from fastapi import APIRouter, File, HTTPException, UploadFile
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from ..audio import decode_bgm
from ..storage import BGM_DIR, storage

router = APIRouter(prefix="/api")

# 常见音频容器; mp4/webm 等视频容器只要含音轨也可用 (解码校验兜底)
ALLOWED_EXTS = {".wav", ".mp3", ".flac", ".m4a", ".mp4", ".aac", ".ogg", ".opus", ".webm"}
MAX_BYTES = 20 * 1024 * 1024
MEDIA_TYPES = {".wav": "audio/wav", ".mp3": "audio/mpeg", ".flac": "audio/flac",
               ".m4a": "audio/mp4", ".mp4": "video/mp4", ".aac": "audio/aac",
               ".ogg": "audio/ogg", ".opus": "audio/ogg", ".webm": "video/webm"}


def _safe_name(filename: str) -> str:
    """只保留文件名主体并过滤非法字符 (Windows 名称规则)。"""
    stem = re.sub(r'[\\/:*?"<>|]+', "_", Path(filename).stem).strip() or "bgm"
    return stem[:60]


def _resolve(name: str) -> Path:
    """按文件名解析 BGM 路径 (拒绝路径穿越), 不存在返回 None。"""
    p = BGM_DIR / Path(name).name
    return p if p.exists() and p.is_file() else None


def _probe_duration(p: Path) -> float | None:
    try:
        import soundfile as sf

        info = sf.info(str(p))
        return round(info.frames / info.samplerate, 1)
    except Exception:
        return None


@router.get("/bgm", summary="背景音乐列表")
def list_bgm():
    items = []
    for p in sorted(BGM_DIR.iterdir(), key=lambda x: x.name):
        if p.suffix.lower() not in ALLOWED_EXTS:
            continue
        items.append({"file": p.name, "name": p.stem,
                      "size": p.stat().st_size, "duration": _probe_duration(p)})
    return {"items": items}


@router.post("/bgm", summary="上传背景音乐 (multipart)")
async def upload_bgm(file: UploadFile = File(...)):
    ext = Path(file.filename or "").suffix.lower()
    if ext not in ALLOWED_EXTS:
        raise HTTPException(400, f"不支持的格式 '{ext or '(无扩展名)'}', 可用: {sorted(ALLOWED_EXTS)}")
    raw = await file.read()
    if len(raw) > MAX_BYTES:
        raise HTTPException(400, "文件过大 (max 20MB)")
    stem = _safe_name(file.filename or "bgm")
    p = BGM_DIR / f"{stem}{ext}"
    i = 1
    while p.exists():   # 重名自动加序号, 不覆盖已有曲目
        p = BGM_DIR / f"{stem}-{i}{ext}"
        i += 1
    p.write_bytes(raw)
    if not decode_bgm(p, 24000):   # 解码校验 (同时预热缓存)
        p.unlink(missing_ok=True)
        raise HTTPException(400, "无法解码该音频文件")
    return {"file": p.name, "name": p.stem, "size": p.stat().st_size,
            "duration": _probe_duration(p)}


class BgmRenameBody(BaseModel):
    name: str = Field(min_length=1, max_length=60)


@router.put("/bgm/{name}", summary="重命名背景音乐 (同步剧集引用)")
def rename_bgm(name: str, body: BgmRenameBody):
    p = _resolve(name)
    if not p:
        raise HTTPException(404, "背景音乐不存在")
    new_stem = _safe_name(body.name)
    target = BGM_DIR / f"{new_stem}{p.suffix.lower()}"
    if target == p:
        return {"ok": True, "file": p.name}
    if target.exists():
        raise HTTPException(409, f"同名文件已存在: {target.name}")
    p.rename(target)
    storage.rename_episode_bgm(p.name, target.name)   # 剧集引用同步
    return {"ok": True, "file": target.name, "name": target.stem}


@router.delete("/bgm/{name}", summary="删除背景音乐 (引用它的剧集自动关闭 BGM)")
def delete_bgm(name: str):
    p = _resolve(name)
    if not p:
        raise HTTPException(404, "背景音乐不存在")
    p.unlink()
    storage.clear_episode_bgm(p.name)
    return {"ok": True}


@router.get("/bgm/{name}/audio", summary="试听背景音乐")
def bgm_audio(name: str):
    p = _resolve(name)
    if not p:
        raise HTTPException(404, "背景音乐不存在")
    return FileResponse(p, media_type=MEDIA_TYPES.get(p.suffix.lower(), "application/octet-stream"),
                        filename=p.name)
