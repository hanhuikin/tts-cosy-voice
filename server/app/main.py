"""CosyVoice FastAPI 服务入口。

启动: .venv/Scripts/python -m uvicorn app.main:app --host 0.0.0.0 --port 8000  (cwd=server/)
"""
from __future__ import annotations

import asyncio
import logging
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles

# 必须在导入 cosyvoice 之前设置 sys.path
from .engine import setup_sys_path

setup_sys_path()

from .engine import MODEL_REGISTRY, QUEUE_MAX, engine
from .model_registry import MODEL_KEYS, is_available
from .routes import podcast, system, tasks, tts, voices
from .storage import DATA_DIR, storage

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("cosyvoice-server")

STATIC_DIR = Path(__file__).resolve().parents[1] / "static"

app = FastAPI(title="CosyVoice Server", version="1.0.0", docs_url="/docs")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.middleware("http")
async def no_cache_static(request, call_next):
    """静态资源走协商缓存 (304), 改前端文件后刷新即生效。"""
    response = await call_next(request)
    if request.url.path.startswith("/static"):
        response.headers["Cache-Control"] = "no-cache"
    return response

app.include_router(tts.router)
app.include_router(voices.router)
app.include_router(tasks.router)
app.include_router(system.router)
app.include_router(podcast.router)


@app.get("/", include_in_schema=False)
def index():
    return RedirectResponse("/static/index.html")


app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")
# HLS 推流分片托管
_LIVE_DIR = DATA_DIR / "live"
_LIVE_DIR.mkdir(parents=True, exist_ok=True)
app.mount("/live", StaticFiles(directory=str(_LIVE_DIR)), name="live")

# 模块级导入: 供 uvicorn 字符串引用
import os

_default_model = os.environ.get("COSYVOICE_DEFAULT_MODEL", "cosyvoice3")


@app.on_event("startup")
async def startup() -> None:
    loop = asyncio.get_running_loop()
    engine._loop = loop
    engine.task_queue = asyncio.Queue(maxsize=QUEUE_MAX)
    asyncio.create_task(engine.worker_loop())
    # 复位上次进程中断遗留的中间状态
    for ep in storage.list_episodes(500):
        if ep["status"] in ("synthesizing", "pushing"):
            storage.update_episode(ep["id"], status="failed",
                                   error="服务重启中断, 请重新合成")
    for t in storage.list_tasks(500):
        if t["status"] == "running":
            storage.finish_task(t["id"], None, None, 0, "failed", "服务重启中断", 0, None)
    # 预载默认模型; 未下载时按注册表顺序找第一个权重就绪的模型, 都没有则 idle
    if _default_model not in MODEL_REGISTRY:
        logger.warning("unknown COSYVOICE_DEFAULT_MODEL=%s, falling back to registry order",
                       _default_model)
        order = list(MODEL_KEYS)
    else:
        order = [_default_model] + [k for k in MODEL_KEYS if k != _default_model]
    target = next((k for k in order if is_available(k)), None)
    if target:
        logger.info("preloading model: %s", target)
        engine.load_async(target, loop)
    else:
        logger.warning("no local model weights found under %s, run scripts/download_models.py "
                       "or set the *_DIR env vars; serving UI in idle state",
                       MODEL_REGISTRY["cosyvoice3"]["dir"].parent)


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8000)
