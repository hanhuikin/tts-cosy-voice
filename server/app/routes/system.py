"""系统路由: 健康状态 / 模型管理 / 环境自检 / SSE 事件流。"""
from __future__ import annotations

import asyncio
import json
import subprocess
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import StreamingResponse

from ..engine import engine
from ..model_registry import MODEL_REGISTRY, is_available, runtime_python
from ..schemas import ModelSwitchRequest
from ..storage import storage

router = APIRouter(prefix="/api")


@router.get("/health")
def health():
    st = engine.status()
    st["voices_count"] = len(storage.list_voices())
    st["models"] = engine.models_info()
    return st


@router.get("/models")
def models():
    return {"models": engine.models_info()}


@router.get("/admin/config")
def admin_config():
    """「系统状态与设置」读取当前生效的权重目录 / 运行环境 (只读)。

    配置由进程环境变量或仓库根目录 .env 提供, model_registry 在导入时固定,
    因此修改后必须重启服务; 这里不提供写入接口, 避免开放未鉴权的路径写操作。
    """
    from ..env_config import describe

    data = describe()
    data["models"] = {
        key: {
            "dir": str(cfg["dir"]),
            "available": is_available(key),
            "runtime": "in-process" if cfg["backend"] == "inprocess" else runtime_python(key),
        }
        for key, cfg in MODEL_REGISTRY.items()
    }
    return data


@router.get("/models/{key}/doctor")
def model_doctor(key: str):
    """诊断某个本地模型的权重 / 解释器 / 依赖是否就绪 (不加载模型、不占显存)。"""
    if key not in MODEL_REGISTRY:
        raise HTTPException(404, f"unknown model: {key}")
    cfg = MODEL_REGISTRY[key]
    result = {
        "key": key,
        "label": cfg["label"],
        "dir": str(cfg["dir"]),
        "weights": is_available(key),
        "missing_files": [m for m in cfg["markers"] if not (cfg["dir"] / m).exists()],
        "backend": cfg["backend"],
        "runtime": "in-process" if cfg["backend"] == "inprocess" else runtime_python(key),
        "install_hint": cfg["install_hint"],
    }
    if cfg["backend"] == "inprocess":
        result["dependencies"] = {"ok": True, "detail": "随服务主环境加载"}
        return result

    py = runtime_python(key)
    probe = {
        "indextts": ("indextts", "import indextts"),
        "qwen3tts": ("qwen_tts", "import qwen_tts"),
    }.get(cfg["family"], ("", ""))
    if Path(py).exists() or not Path(py).is_absolute():
        try:
            cmd = [py, "-c", probe[1]]
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
            result["dependencies"] = {
                "ok": r.returncode == 0,
                "detail": (r.stderr or r.stdout or "").strip()[-400:]
                           or f"{probe[0]} 可导入",
            }
        except Exception as e:
            result["dependencies"] = {"ok": False, "detail": f"{type(e).__name__}: {e}"}
    else:
        result["dependencies"] = {"ok": False, "detail": f"解释器不存在: {py}"}
    if cfg["family"] == "indextts":
        from ..model_registry import INDEXTTS_REPO
        result["repo"] = {"ok": INDEXTTS_REPO.exists(), "dir": str(INDEXTTS_REPO)}
    return result


@router.post("/admin/model")
async def switch_model(body: ModelSwitchRequest, request: Request):
    if engine.state == "loading":
        raise HTTPException(409, "another model is loading, please wait")
    cfg = MODEL_REGISTRY[body.model]
    if cfg["backend"] == "external" and not is_available(body.model):
        raise HTTPException(400, f"{cfg['label']} 权重未就绪: {cfg['dir']}。"
                                 f" {cfg['install_hint']}")
    if body.model == engine.status()["model"] and engine.state == "ready":
        return {"ok": True, "state": "ready", "model": body.model}
    loop = asyncio.get_running_loop()
    engine.load_async(body.model, loop)
    return {"ok": True, "state": "loading", "model": body.model}


@router.get("/events")
async def events(request: Request):
    """SSE 事件流: engine 状态 / queue 变化。"""
    q = engine.events.subscribe()

    async def gen():
        try:
            # 连接建立即推送当前状态
            yield f"data: {json.dumps({'type': 'hello', **engine.status()}, ensure_ascii=False)}\n\n"
            while True:
                if await request.is_disconnected():
                    break
                try:
                    event = await asyncio.wait_for(q.get(), timeout=15)
                    yield f"data: {json.dumps(event, ensure_ascii=False)}\n\n"
                except asyncio.TimeoutError:
                    yield ": keep-alive\n\n"  # 心跳, 防代理断连
        finally:
            engine.events.unsubscribe(q)

    return StreamingResponse(gen(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})
