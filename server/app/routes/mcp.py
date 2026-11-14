"""MCP HTTP 传输路由: SSE (旧版 2024-11-05) + Streamable HTTP (2025-03-26+)。

- SSE:             GET /mcp/sse 建立下行通道 (首条 event: endpoint 告知提交地址),
                   POST /mcp/messages?session_id= 提交 JSON-RPC, 响应经 SSE 回送 (HTTP 202)
- Streamable HTTP: POST /mcp 单端点收发 (initialize 响应下发 Mcp-Session-Id),
                   GET /mcp 打开服务端下行 SSE 通道, DELETE /mcp 终止会话
两种传输与 stdio (server/mcp_server.py) 共用 app.mcp_core 的工具实现。
"""
from __future__ import annotations

import asyncio
import json
import logging
from uuid import uuid4

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse

from ..mcp_core import handle_payload

router = APIRouter()
logger = logging.getLogger("cosyvoice.mcp")

# SSE 会话注册表: session_id -> 下行队列 (断开即回收)
_sse_sessions: dict[str, asyncio.Queue] = {}


def _self_base(request: Request) -> str:
    return str(request.base_url).rstrip("/")


def _sse_response(gen) -> StreamingResponse:
    return StreamingResponse(gen(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache",
                                      "X-Accel-Buffering": "no",
                                      "Connection": "keep-alive"})


# ---------- SSE 传输 (2024-11-05) ----------

@router.get("/mcp/sse", summary="MCP SSE 传输: 建立下行事件流 (返回 endpoint 提交地址)")
async def mcp_sse(request: Request):
    sid = uuid4().hex
    q: asyncio.Queue = asyncio.Queue()
    _sse_sessions[sid] = q
    base = _self_base(request)

    async def gen():
        try:
            yield f"event: endpoint\ndata: {base}/mcp/messages?session_id={sid}\n\n"
            logger.info("[MCP] SSE 会话接入: %s", sid)
            while True:
                if await request.is_disconnected():
                    break
                try:
                    msg = await asyncio.wait_for(q.get(), timeout=15)
                    yield f"event: message\ndata: {json.dumps(msg, ensure_ascii=False)}\n\n"
                except asyncio.TimeoutError:
                    yield ": keep-alive\n\n"
        finally:
            _sse_sessions.pop(sid, None)
            logger.info("[MCP] SSE 会话断开: %s", sid)

    return _sse_response(gen)


@router.post("/mcp/messages", summary="MCP SSE 传输: 提交 JSON-RPC 消息 (响应经 SSE 回送)")
async def mcp_sse_messages(request: Request, session_id: str = ""):
    q = _sse_sessions.get(session_id)
    if q is None:
        raise HTTPException(404, "unknown or expired session (请重新连接 /mcp/sse)")
    resp = await handle_payload(await request.body(), base=_self_base(request))
    if resp is not None:
        q.put_nowait(resp)
    return Response(status_code=202)   # SSE 传输约定: 提交即受理, 结果走下行通道


# ---------- Streamable HTTP 传输 (2025-03-26+) ----------

@router.post("/mcp", summary="MCP Streamable HTTP: 提交 JSON-RPC (请求/响应单端点)")
async def mcp_streamable_post(request: Request):
    resp = await handle_payload(await request.body(), base=_self_base(request))
    headers = {}
    if isinstance(resp, dict) and isinstance(resp.get("result"), dict) \
            and "serverInfo" in resp["result"]:
        headers["Mcp-Session-Id"] = uuid4().hex   # initialize: 下发会话 id
    elif request.headers.get("mcp-session-id"):
        headers["Mcp-Session-Id"] = request.headers["mcp-session-id"]
    if resp is None:   # notification: 无需应答
        return Response(status_code=202, headers=headers)
    return JSONResponse(resp, headers=headers)


@router.get("/mcp", summary="MCP Streamable HTTP: 服务端下行 SSE 通道")
async def mcp_streamable_get(request: Request):
    accept = request.headers.get("accept", "")
    if "text/event-stream" not in accept:
        raise HTTPException(406, "GET /mcp 需要 Accept: text/event-stream")

    async def gen():
        while True:
            if await request.is_disconnected():
                break
            await asyncio.sleep(15)
            yield ": keep-alive\n\n"   # 当前无服务端主动推送事件, 保持长连接即可

    return _sse_response(gen)


@router.delete("/mcp", summary="MCP Streamable HTTP: 终止会话")
async def mcp_streamable_delete():
    # 无状态实现: 会话不持资源, 直接确认终止
    return Response(status_code=204)
