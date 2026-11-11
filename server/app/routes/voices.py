"""音色管理路由: 上传克隆音色 / 列表 / 编辑 / 删除 / 参考音频试听。"""
from __future__ import annotations

import tempfile
from pathlib import Path
from typing import Optional

import numpy as np
from fastapi import APIRouter, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from ..audio import estimate_wav_duration, load_reference_audio, save_wav
from ..storage import VOICES_DIR, storage

router = APIRouter(prefix="/api")

ALLOWED_EXTS = {".wav", ".mp3", ".flac", ".m4a", ".ogg", ".opus"}
MIN_SECONDS, MAX_SECONDS = 1.0, 30.0


class VoiceUpdate(BaseModel):
    name: Optional[str] = Field(default=None, min_length=1, max_length=50)
    prompt_text: Optional[str] = Field(default=None, min_length=1, max_length=500)


@router.get("/voices")
def list_voices():
    voices = storage.list_voices()
    for v in voices:
        v.pop("file", None)  # 不暴露服务器文件路径
    return voices


@router.get("/speakers")
def list_speakers():
    return {"speakers": engine_speakers()}


def engine_speakers() -> list[str]:
    from ..engine import engine

    st = engine.status()
    return st.get("speakers", []) if st["state"] == "ready" else []


@router.post("/voices")
async def create_voice(
    name: str = Form(...),
    prompt_text: str = Form(...),
    file: UploadFile = File(...),
):
    ext = Path(file.filename or "").suffix.lower()
    if ext not in ALLOWED_EXTS:
        raise HTTPException(400, f"unsupported audio format '{ext}', allowed: {sorted(ALLOWED_EXTS)}")
    if not prompt_text.strip():
        raise HTTPException(400, "prompt_text is required (transcript of the reference audio)")

    raw = await file.read()
    if len(raw) > 50 * 1024 * 1024:
        raise HTTPException(400, "file too large (max 50MB)")
    with tempfile.NamedTemporaryFile(suffix=ext, delete=False) as tmp:
        tmp.write(raw)
        tmp_path = Path(tmp.name)

    try:
        try:
            samples, sr = load_reference_audio(tmp_path, target_sr=16000, max_seconds=MAX_SECONDS)
        except Exception as e:
            raise HTTPException(400, f"cannot decode audio: {type(e).__name__}: {e}")
        duration = len(samples) / sr
        if duration < MIN_SECONDS:
            raise HTTPException(400, f"audio too short ({duration:.1f}s), need >= {MIN_SECONDS}s")
        # 归一化峰值, 避免参考音量过低影响克隆效果
        peak = float(np.abs(samples).max()) or 1.0
        if peak < 0.01:
            raise HTTPException(400, "audio is (almost) silent")
        samples = samples * (0.95 / peak)
        from ..storage import new_id

        vid = new_id()
        final_path = VOICES_DIR / f"{vid}.wav"
        save_wav(final_path, samples, sr)
        return storage.add_voice(name.strip(), prompt_text.strip(), final_path, duration, sr, vid=vid)
    finally:
        tmp_path.unlink(missing_ok=True)


@router.put("/voices/{vid}")
def update_voice(vid: str, body: VoiceUpdate):
    """编辑音色名称 / 参考文本 (至少提供一项)。"""
    if body.name is None and body.prompt_text is None:
        raise HTTPException(400, "nothing to update: provide name and/or prompt_text")
    try:
        voice = storage.update_voice(vid, name=body.name, prompt_text=body.prompt_text)
    except ValueError as e:
        raise HTTPException(400, str(e))
    if voice is None:
        raise HTTPException(404, "voice not found")
    voice.pop("file", None)
    return voice


@router.get("/voices/{vid}/prompt")
def voice_prompt(vid: str):
    voice = storage.get_voice(vid)
    if not voice:
        raise HTTPException(404, "voice not found")
    return FileResponse(voice["file"], media_type="audio/wav", filename=f"{vid}.wav")


@router.delete("/voices/{vid}")
def delete_voice(vid: str):
    if not storage.delete_voice(vid):
        raise HTTPException(404, "voice not found")
    return {"ok": True}
