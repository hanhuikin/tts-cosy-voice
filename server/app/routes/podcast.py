"""播客工坊路由: LLM 配置 / 内容提取 / 对话稿生成 / 剧集管理与合成推流。"""
from __future__ import annotations

import asyncio
import json

from fastapi import APIRouter, File, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel, Field

from .. import llm, podcast
from ..storage import storage

router = APIRouter(prefix="/api")

PROTOCOLS = {"rtmp", "srt", "rtsp", "hls"}


# ---------- 大模型配置 ----------
@router.get("/llm/config")
def llm_get_config():
    return llm.masked(llm.load_config())


class LLMConfigBody(BaseModel):
    base_url: str | None = None
    api_key: str | None = None
    model: str | None = None
    temperature: float | None = None
    rounds_hint: str | None = None
    style_prompt: str | None = None


@router.put("/llm/config")
def llm_save_config(body: LLMConfigBody):
    return llm.save_config(body.model_dump())


@router.post("/llm/test")
def llm_test():
    try:
        return {"ok": True, **llm.test_connect()}
    except llm.LLMError as e:
        raise HTTPException(400, str(e))


# ---------- 内容提取 ----------
@router.post("/podcast/source/extract")
async def podcast_extract(request: Request, file: UploadFile | None = File(None)):
    """文件上传 (multipart) 或 JSON {url} 二选一, 返回提取的纯文本。"""
    if file is not None:
        raw = await file.read()
        if len(raw) > 20 * 1024 * 1024:
            raise HTTPException(400, "文件过大 (max 20MB)")
        try:
            text = podcast.extract_from_file(file.filename or " unnamed.txt", raw)
        except ValueError as e:
            raise HTTPException(400, str(e))
        except Exception as e:
            raise HTTPException(400, f"解析失败: {type(e).__name__}: {e}")
        return {"text": text, "chars": len(text)}
    body = await request.json()
    url = (body.get("url") or "").strip()
    if not url.startswith(("http://", "https://")):
        raise HTTPException(400, "请提供有效的 http(s) URL")
    try:
        text = podcast.extract_from_url(url)
    except requests.RequestException as e:
        raise HTTPException(400, f"抓取失败: {type(e).__name__}: {e}")
    except ValueError as e:
        raise HTTPException(400, str(e))
    return {"text": text, "chars": len(text)}


# ---------- 对话稿生成 (流式返回增量) ----------
class ScriptBody(BaseModel):
    text: str = Field(min_length=10, max_length=20000)


@router.post("/podcast/script")
async def podcast_script(body: ScriptBody):
    async def gen():
        buf = []
        try:
            for delta in llm.chat_stream([
                {"role": "system", "content": llm.SCRIPT_SYSTEM_PROMPT},
                {"role": "user", "content": f"请把以下内容改写为 10~16 轮左右的双人播客对话稿。\n\n"
                                            f"【内容开始】\n{body.text[:12000]}\n【内容结束】"},
            ]):
                buf.append(delta)
                yield delta
        except llm.LLMError as e:
            yield f"\n__LLM_ERROR__{e}"
    return StreamingResponse(gen(), media_type="text/plain; charset=utf-8")


@router.post("/podcast/parse-script")
async def podcast_parse(request: Request):
    """解析前端累积的原始 LLM 输出 -> 结构化对话稿行。"""
    body = await request.json()
    raw = body.get("raw") or ""
    try:
        return {"lines": podcast.parse_script(raw)}
    except ValueError as e:
        raise HTTPException(400, str(e))


# ---------- 剧集管理 ----------
class EpisodeBody(BaseModel):
    title: str = Field(min_length=1, max_length=100)
    source_type: str = "text"
    source_ref: str = ""
    lines: list[dict]
    voice_host: str
    voice_guest: str
    speed: float = 1.0


@router.post("/podcast/episodes")
def create_episode(body: EpisodeBody):
    if not body.lines:
        raise HTTPException(400, "对话稿为空")
    ep = storage.add_episode(body.title, body.source_type, body.source_ref,
                             json.dumps(body.lines, ensure_ascii=False),
                             body.voice_host, body.voice_guest)
    return ep


@router.get("/podcast/episodes")
def list_episodes(limit: int = 50):
    eps = storage.list_episodes(limit)
    for e in eps:
        e.pop("file", None)
        e["lines_count"] = len(json.loads(e.pop("script_json") or "[]"))
    return {"episodes": eps}


@router.get("/podcast/episodes/{pid}")
def get_episode(pid: str):
    ep = storage.get_episode(pid)
    if not ep:
        raise HTTPException(404, "episode not found")
    ep["script"] = json.loads(ep.pop("script_json") or "[]")
    ep.pop("file", None)
    ep["push"] = podcast.push_manager.status() if ep["id"] == podcast.push_manager.info.get("episode_id") else None
    return ep


@router.put("/podcast/episodes/{pid}")
def update_episode(pid: str, body: EpisodeBody):
    if not storage.get_episode(pid):
        raise HTTPException(404, "episode not found")
    storage.update_episode(pid, title=body.title, script_json=json.dumps(body.lines, ensure_ascii=False),
                           voice_host=body.voice_host, voice_guest=body.voice_guest, speed=body.speed)
    return storage.get_episode(pid)


@router.delete("/podcast/episodes/{pid}")
def delete_episode(pid: str):
    if podcast.push_manager.info.get("episode_id") == pid and podcast.push_manager.active:
        raise HTTPException(409, "该剧集正在推流, 请先停止")
    if not storage.delete_episode(pid):
        raise HTTPException(404, "episode not found")
    return {"ok": True}


@router.get("/podcast/episodes/{pid}/audio")
def episode_audio(pid: str):
    ep = storage.get_episode(pid)
    if not ep or not ep.get("file"):
        raise HTTPException(404, "成品音频不存在")
    return FileResponse(ep["file"], media_type="audio/wav", filename=f"podcast_{pid}.wav")


@router.get("/podcast/episodes/{pid}/status")
def episode_status(pid: str):
    ep = storage.get_episode(pid)
    if not ep:
        raise HTTPException(404, "episode not found")
    return {"id": ep["id"], "status": ep["status"], "progress": ep["progress"],
            "duration": ep["duration"], "error": ep["error"],
            "lines_total": len(json.loads(ep["script_json"] or "[]"))}


# ---------- 合成 (final / stream+push) ----------
class SynthesizeBody(BaseModel):
    mode: str = "final"


@router.post("/podcast/episodes/{pid}/synthesize")
async def synthesize_episode(pid: str, body: SynthesizeBody):
    ep = storage.get_episode(pid)
    if not ep:
        raise HTTPException(404, "episode not found")
    if ep["status"] == "synthesizing":
        raise HTTPException(409, "合成进行中")
    if podcast.push_manager.active:
        raise HTTPException(409, "已有推流进行中")
    lines = json.loads(ep["script_json"])
    if not lines:
        raise HTTPException(400, "对话稿为空")
    try:
        # 预校验全部行可解析音色与参数 (提前暴露问题)
        for ln in lines:
            podcast.resolve_line_request(ln, {"host": ep["voice_host"], "guest": ep["voice_guest"]})
    except ValueError as e:
        raise HTTPException(400, str(e))

    loop = asyncio.get_running_loop()
    if body.mode == "final":
        storage.update_episode(pid, status="synthesizing", progress=0)
        loop.create_task(podcast.synth_episode_final(pid))
        return {"ok": True, "mode": "final", "status": "synthesizing"}
    if body.mode == "stream":
        # 本地流式: 与合成工作台一致, WAV 头 + 逐句 PCM 分块, 结束后落盘成品
        storage.update_episode(pid, status="synthesizing", progress=0)
        return StreamingResponse(
            podcast.stream_episode_gen(pid),
            media_type="audio/wav",
            headers={"X-Episode-Id": pid, "X-Sample-Rate": "24000",
                     "Content-Disposition": f'inline; filename="podcast_{pid}.wav"'},
        )
    raise HTTPException(400, f"未知 mode: {body.mode}")


class PushBody(BaseModel):
    protocol: str
    url: str = ""
    title: str | None = None


@router.post("/podcast/episodes/{pid}/push")
async def push_episode(pid: str, body: PushBody):
    if body.protocol not in PROTOCOLS:
        raise HTTPException(422, f"protocol 必须是 {sorted(PROTOCOLS)} 之一")
    if body.protocol != "hls" and not body.url.strip():
        raise HTTPException(400, "RTMP/SRT/RTSP 需要提供推流地址")
    ep = storage.get_episode(pid)
    if not ep:
        raise HTTPException(404, "episode not found")
    if ep["status"] == "synthesizing":
        raise HTTPException(409, "合成进行中")
    url = body.url.strip()
    if body.protocol == "hls":
        url = url or f"podcast_{pid}"  # hls 仅用于命名分片
    try:
        status = await podcast.push_manager.start(pid, body.protocol, url)
    except RuntimeError as e:
        raise HTTPException(409, str(e))
    except ValueError as e:
        raise HTTPException(400, str(e))
    return status


@router.delete("/podcast/episodes/{pid}/push")
def stop_push(pid: str):
    if podcast.push_manager.info.get("episode_id") != pid:
        raise HTTPException(404, "该剧集当前没有推流")
    return podcast.push_manager.stop()


@router.get("/podcast/push/status")
def push_status():
    return podcast.push_manager.status()


@router.get("/podcast/ffmpeg")
def ffmpeg_check():
    return {"available": podcast.ffmpeg_available()}
