#!/usr/bin/env python
"""CosyVoice MCP stdio 传输入口 (零第三方依赖, 仅标准库)。

客户端以子进程方式拉起:
    .venv/Scripts/python.exe server/mcp_server.py
协议: 每行一条 JSON-RPC 消息; stdin 进 / stdout 出 (UTF-8), 日志走 stderr。

环境变量:
    COSYVOICE_MCP_BASE          服务地址, 默认 http://127.0.0.1:8000
    COSYVOICE_MCP_DOWNLOAD_DIR  工具产出音频落盘目录, 默认 server/data/mcp

与 SSE / Streamable HTTP (app/routes/mcp.py) 共用 app.mcp_core 的工具实现。
"""
from __future__ import annotations

import asyncio
import json
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))   # 使 `app` 包可导入

from app.mcp_core import handle_payload   # noqa: E402

# stdout 只承载协议报文, 强制 UTF-8 (Windows 默认 GBK 会炸中文); 日志走 stderr
sys.stdout.reconfigure(encoding="utf-8")
sys.stdin.reconfigure(encoding="utf-8")
logging.basicConfig(level=logging.INFO, stream=sys.stderr,
                    format="%(asctime)s %(levelname)s %(name)s: %(message)s")


async def amain() -> int:
    while True:
        line = await asyncio.to_thread(sys.stdin.readline)
        if not line:   # EOF: 客户端关闭了管道
            return 0
        line = line.strip()
        if not line:
            continue
        try:
            resp = await handle_payload(line)
        except Exception:   # 协议层兜底: 任何异常都不能杀死服务循环
            logging.getLogger("cosyvoice.mcp").exception("处理消息失败")
            resp = {"jsonrpc": "2.0", "id": None,
                    "error": {"code": -32603, "message": "internal error"}}
        if resp is None:
            continue
        payloads = resp if isinstance(resp, list) else [resp]
        try:
            for p in payloads:
                sys.stdout.write(json.dumps(p, ensure_ascii=False) + "\n")
            sys.stdout.flush()
        except (BrokenPipeError, OSError):
            return 0   # 客户端已关闭 stdout: 正常退出


if __name__ == "__main__":
    try:
        sys.exit(asyncio.run(amain()))
    except KeyboardInterrupt:
        sys.exit(0)
