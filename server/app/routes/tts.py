"""TTS 合成路由: 统一入口 /api/tts + OpenAI 兼容 /v1/audio/speech。"""
from __future__ import annotations

import time
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import Response, StreamingResponse

from ..audio import pcm_duration_seconds, pcm_to_wav, wav_header
from ..engine import MODEL_REGISTRY, QueueFullError, TTSRequest as EngineSpec, engine
from ..schemas import OpenAISpeechRequest, TTSRequest  # pydantic 校验模型
from ..storage import TASKS_DIR, storage

router = APIRouter()

ESTIMATED_MAX_SECONDS = 600  # 流式 WAV 头里的占位时长


def _resolve_prompt(spec: EngineSpec) -> None:
    """把 voice_id 解析为参考音频路径与参考文本 (显式传入优先)。"""
    if spec.voice_id:
        voice = storage.get_voice(spec.voice_id)
        if not voice:
            raise HTTPException(404, f"voice '{spec.voice_id}' not found")
        if not spec.prompt_wav:
            spec.prompt_wav = voice["file"]
        if not spec.prompt_text:
            spec.prompt_text = voice["prompt_text"]
    if spec.prompt_wav and not Path(spec.prompt_wav).exists():
        raise HTTPException(404, f"prompt wav not found: {spec.prompt_wav}")


@router.post("/api/tts")
async def tts(body: TTSRequest):
    # pydantic 校验通过后转引擎内部 dataclass (含 prompt_wav 内部字段)
    spec = EngineSpec(**body.model_dump())
    _resolve_prompt(spec)
    err = engine.validate(spec)
    if err:
        raise HTTPException(400, err)

    task_id = storage.create_task(spec)
    try:
        job = await engine.submit(spec)
    except QueueFullError:
        storage.finish_task(task_id, None, None, 0, "failed", "queue full", 0, None)
        raise HTTPException(429, f"server busy: inference queue is full (max {engine.status()['queue_max']})")

    sample_rate = engine.status()["sample_rate"] or 24000

    if spec.stream:
        return _stream_response(job, task_id, sample_rate)
    return await _collect_response(job, task_id, spec)


async def _collect_response(job, task_id: str, spec: EngineSpec) -> Response:
    """非流式: 收齐所有分块, 打完整 WAV 返回 (采样率以本 job 实际输出为准)。"""
    parts: list[bytes] = []
    t0 = time.time()
    status, error = "ok", None
    async for kind, payload in engine.drain(job):
        if kind == "chunk":
            parts.append(payload)
        elif kind == "error":
            status, error = "failed", payload
            break
    sample_rate = job.sample_rate or 24000
    infer_ms = int((time.time() - t0) * 1000)
    if status != "ok" or not parts:
        storage.finish_task(task_id, None, None, sample_rate, status, error or "no audio produced", infer_ms, None)
        raise HTTPException(500, error or "no audio produced")

    wav = pcm_to_wav(parts, sample_rate)
    duration = pcm_duration_seconds(sum(len(p) for p in parts), sample_rate)
    storage.finish_task(task_id, TASKS_DIR / f"{task_id}.wav", duration, sample_rate, "ok", None,
                        infer_ms, int((job.first_chunk_at - t0) * 1000) if job.first_chunk_at else None)
    (TASKS_DIR / f"{task_id}.wav").write_bytes(wav)
    return Response(
        content=wav,
        media_type="audio/wav",
        headers={
            "X-Task-Id": task_id,
            "X-Sample-Rate": str(sample_rate),
            "X-Duration-Ms": str(int(duration * 1000)),
            "X-Infer-Ms": str(infer_ms),
            "Content-Disposition": f'inline; filename="{task_id}.wav"',
        },
    )


def _stream_response(job, task_id: str, sample_rate: int) -> StreamingResponse:
    """流式: 先发 WAV 头 (占位长度), 再顺序发 PCM 分块; 结束后落盘完整 WAV 到历史。"""
    total_bytes = 0
    parts: list[bytes] = []
    t0 = time.time()
    state = {"first_ms": None}

    async def gen():
        nonlocal total_bytes
        yield wav_header(sample_rate, ESTIMATED_MAX_SECONDS * sample_rate)
        # 默认视为「客户端中途断开」: 生成器被取消/关闭时不会走到循环后,
        # 若把收尾写在循环之后, 任务会永远停在 running (播放器关页面即可复现)
        status, error = "failed", "client disconnected"
        try:
            async for kind, payload in engine.drain(job):
                if kind == "chunk":
                    parts.append(payload)
                    total_bytes += len(payload)
                    if state["first_ms"] is None:
                        state["first_ms"] = int((time.time() - t0) * 1000)
                    yield payload
                elif kind == "error":
                    status, error = "failed", payload
                    break
            else:
                status, error = "ok", None
        finally:
            duration = pcm_duration_seconds(total_bytes, sample_rate)
            infer_ms = int((time.time() - t0) * 1000)
            out_file = TASKS_DIR / f"{task_id}.wav" if parts else None
            if status == "ok" and parts:
                out_file.write_bytes(pcm_to_wav(parts, sample_rate))
            storage.finish_task(task_id, out_file, duration if parts else None,
                                sample_rate, status, error, infer_ms, state["first_ms"])

    return StreamingResponse(
        gen(),
        media_type="audio/wav",
        headers={
            "X-Task-Id": task_id,
            "X-Sample-Rate": str(sample_rate),
            "Content-Disposition": f'inline; filename="{task_id}.wav"',
        },
    )


# ---------- OpenAI 兼容端点 ----------
@router.post("/v1/audio/speech")
async def openai_speech(body: OpenAISpeechRequest):
    voice = body.voice
    cfg = MODEL_REGISTRY[body.model]
    spec_kwargs = dict(
        text=body.input,
        model=body.model,
        speed=body.speed,
        stream=False,
        spk_id=None,
        voice_id=None,
        prompt_text=None,
        instruct_text=None,
    )
    if voice.startswith("voice:") or voice.startswith("v:"):
        # 不支持 zero_shot 的模型用跨语言模式承载参考音频
        clone_mode = next((m for m in ("zero_shot", "cross_lingual") if m in cfg["modes"]), None)
        if clone_mode is None:
            raise HTTPException(400, f"模型 {body.model} 不支持参考音频克隆")
        spec_kwargs.update(mode=clone_mode, voice_id=voice.split(":", 1)[1])
    elif "sft" in cfg["speaker_modes"]:
        spec_kwargs.update(mode="sft", spk_id=voice)
    elif "instruct" in cfg["speaker_modes"]:
        spec_kwargs.update(mode="instruct", spk_id=voice)
    elif cfg["instruct_kind"] == "design":
        # VoiceDesign: voice 字段直接作为声音描述
        spec_kwargs.update(mode="instruct", instruct_text=voice)
    else:
        raise HTTPException(400, f"模型 {body.model} 无内置说话人, 请传 voice:<音色id>")

    try:
        spec = EngineSpec(**TTSRequest(**spec_kwargs).model_dump())
    except Exception as e:
        raise HTTPException(400, str(e))
    if spec.voice_id:
        # /v1 端点无 prompt_text 字段: 从已存音色读取
        _resolve_prompt(spec)
        if cfg["requires_prompt_text"] and not (spec.prompt_text or "").strip():
            raise HTTPException(400, "referenced voice has no prompt_text; use /api/voices to set it")
    err = engine.validate(spec)
    if err:
        raise HTTPException(400, err)

    task_id = storage.create_task(spec)
    try:
        job = await engine.submit(spec)
    except QueueFullError:
        raise HTTPException(429, "server busy")
    return await _collect_response(job, task_id, spec)
