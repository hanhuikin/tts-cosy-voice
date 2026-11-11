"""系统路由: 健康状态 / 模型管理 / SSE 事件流。"""
from __future__ import annotations

import asyncio
import json

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import StreamingResponse

from ..engine import engine
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


@router.post("/admin/model")
async def switch_model(body: ModelSwitchRequest, request: Request):
    if engine.state == "loading":
        raise HTTPException(409, "another model is loading, please wait")
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
