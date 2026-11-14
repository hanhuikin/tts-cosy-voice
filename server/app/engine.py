"""TTS 推理引擎封装。

职责:
- 管理模型生命周期: 多模型互斥加载、后台线程加载、状态广播 (模型配置见 model_registry.py)
- 把不同推理后端 (CosyVoice / IndexTTS / Qwen3-TTS) 统一为 int16 PCM 分块
- 单 worker GPU 任务队列, 防止并发推理 OOM
- 流式输出: 推理线程 -> asyncio.Queue 桥接
"""
from __future__ import annotations

import asyncio
import logging
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import AsyncIterator, Optional

from .backends import CosyVoiceBackend, ExternalTTSBackend
from .model_registry import MODEL_KEYS, MODEL_REGISTRY, MODELS_DIR, effective_modes, is_available, public_info

REPO_ROOT = Path(__file__).resolve().parents[2]
COSYVOICE_ROOT = REPO_ROOT / "CosyVoice"

logger = logging.getLogger("cosyvoice-server.engine")

QUEUE_MAX = 16  # 排队上限, 超出返回 429


def setup_sys_path() -> None:
    """引入上游仓库与 Matcha-TTS third_party。"""
    for p in (str(COSYVOICE_ROOT), str(COSYVOICE_ROOT / "third_party" / "Matcha-TTS")):
        if p not in sys.path:
            sys.path.insert(0, p)


def apply_upstream_patches() -> None:
    """运行时补丁 (v3 流式语速), 需在 cosyvoice 可导入后调用。"""
    from . import cosyvoice_patch

    cosyvoice_patch.apply()


@dataclass
class TTSRequest:
    text: str
    model: str
    mode: str
    spk_id: Optional[str] = None
    voice_id: Optional[str] = None
    prompt_text: Optional[str] = None
    prompt_wav: Optional[str] = None  # 参考音频绝对路径
    instruct_text: Optional[str] = None
    speed: float = 1.0
    stream: bool = False
    bgm_file: Optional[str] = None    # 背景音乐文件名 (混流由路由层做, 引擎不感知)
    bgm_volume: float = 0.18
    bgm_duck: bool = True             # 自动闪避: 说话时压低背景乐


@dataclass(eq=False)  # 按身份哈希: 播客停止链路要把 Job 放进 set
class Job:
    spec: TTSRequest
    out_q: asyncio.Queue
    loop: asyncio.AbstractEventLoop
    task_id: Optional[str] = None
    first_chunk_at: Optional[float] = None
    done: bool = False
    error: Optional[str] = None
    sample_rate: int = 24000
    cancelled: bool = False  # 置 True 后: 排队中直接跳过; 推理中在下一个分块边界停止


class EventBus:
    """轻量 SSE 广播: 订阅者为 asyncio.Queue。"""

    def __init__(self) -> None:
        self._subs: set[asyncio.Queue] = set()
        self._lock = threading.Lock()

    def subscribe(self) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=256)
        with self._lock:
            self._subs.add(q)
        return q

    def unsubscribe(self, q: asyncio.Queue) -> None:
        with self._lock:
            self._subs.discard(q)

    def publish(self, event: dict) -> None:
        with self._lock:
            subs = list(self._subs)
        for q in subs:
            try:
                q.put_nowait(event)
            except asyncio.QueueFull:
                pass


class Engine:
    def __init__(self) -> None:
        self.events = EventBus()
        self._backend: Optional[object] = None
        self._key: Optional[str] = None
        self._state = "idle"  # idle | loading | ready | error
        self._error: Optional[str] = None
        self._load_lock = threading.Lock()
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self.task_queue: Optional[asyncio.Queue] = None
        self._last_infer_ms: Optional[int] = None

    # ---------- 状态 ----------
    @property
    def state(self) -> str:
        return self._state

    def status(self) -> dict:
        import torch

        backend = self._backend if self._state == "ready" else None
        info = {
            "state": self._state,
            "model": self._key,
            "model_label": MODEL_REGISTRY[self._key]["label"] if self._key else None,
            "family": MODEL_REGISTRY[self._key]["family"] if self._key else None,
            "backend": MODEL_REGISTRY[self._key]["backend"] if self._key else None,
            "stream_mode": MODEL_REGISTRY[self._key]["stream_mode"] if self._key else None,
            "sample_rate": getattr(backend, "sample_rate", None) if backend else None,
            "speakers": backend.speakers() if backend else [],
            "queue_len": self.task_queue.qsize() if self.task_queue else 0,
            "queue_max": QUEUE_MAX,
            "last_infer_ms": self._last_infer_ms,
            "error": self._error,
            "models": {},
        }
        try:
            if torch.cuda.is_available():
                free_b, total_b = torch.cuda.mem_get_info()
                info["gpu"] = {
                    "name": torch.cuda.get_device_name(0),
                    "vram_free_gb": round(free_b / 1024**3, 2),
                    "vram_total_gb": round(total_b / 1024**3, 2),
                    "vram_used_gb": round((total_b - free_b) / 1024**3, 2),
                }
            else:
                info["gpu"] = {"name": "CPU", "vram_used_gb": None, "vram_total_gb": None}
        except Exception:  # pragma: no cover
            info["gpu"] = {"name": "unknown"}
        return info

    def models_info(self) -> list[dict]:
        return [public_info(key, loaded=(self._key == key and self._state == "ready"))
                for key in MODEL_KEYS]

    def _broadcast(self, **event) -> None:
        if self._loop and self._loop.is_running():
            self._loop.call_soon_threadsafe(self.events.publish, event)

    # ---------- 模型加载 ----------
    def load_async(self, key: str, loop: asyncio.AbstractEventLoop) -> None:
        """后台线程加载模型, 状态经 SSE 广播。"""
        if key not in MODEL_REGISTRY:
            raise ValueError(f"unknown model: {key}")
        if self._state == "loading":
            raise RuntimeError("model is loading")
        threading.Thread(target=self._load_sync, args=(key, loop), daemon=True).start()

    def _load_sync(self, key: str, loop: asyncio.AbstractEventLoop) -> None:
        self._loop = loop
        with self._load_lock:
            self._state, self._error, self._key = "loading", None, key
            cfg = MODEL_REGISTRY[key]
            self._broadcast(type="engine", state="loading", model=key, label=cfg["label"])
            t0 = time.time()
            try:
                import torch  # noqa: F401

                # 释放旧模型: 外部 worker 进程退出后其显存自动归还
                if self._backend is not None:
                    self._backend.close()
                self._backend = None
                if torch.cuda.is_available():
                    torch.cuda.empty_cache()

                if cfg["backend"] == "inprocess":
                    self._backend = self._load_cosyvoice(key, cfg)
                else:
                    if not is_available(key):
                        raise RuntimeError(
                            f"权重目录未就绪: {cfg['dir']} (缺少 {', '.join(cfg['markers'])})。"
                            f"\n{cfg['install_hint']}"
                        )
                    backend = ExternalTTSBackend(key, cfg)
                    backend.load()
                    self._backend = backend
                self._state = "ready"
                logger.info("model %s loaded in %.1fs, sample_rate=%s, speakers=%s",
                            key, time.time() - t0, self._backend.sample_rate,
                            self._backend.speakers())
            except Exception as e:  # pragma: no cover
                logger.exception("model load failed")
                if self._backend is not None:
                    try:
                        self._backend.close()
                    except Exception:
                        pass
                self._backend = None
                self._key = None
                self._state, self._error = "error", f"{type(e).__name__}: {e}"
            backend = self._backend
            self._broadcast(
                type="engine", state=self._state, model=self._key, label=cfg["label"],
                sample_rate=getattr(backend, "sample_rate", None),
                speakers=backend.speakers() if backend else [],
                error=self._error, load_ms=int((time.time() - t0) * 1000),
            )

    @staticmethod
    def _load_cosyvoice(key: str, cfg: dict) -> CosyVoiceBackend:
        from cosyvoice.cli.cosyvoice import AutoModel

        apply_upstream_patches()
        model_dir = cfg["dir"]
        if not model_dir.exists():
            # 未预下载时回退: AutoModel 会走 modelscope snapshot_download
            model_dir = {
                "cosyvoice3": "FunAudioLLM/Fun-CosyVoice3-0.5B-2512",
                "cosyvoice2": "iic/CosyVoice2-0.5B",
            }[key]
        return CosyVoiceBackend(AutoModel(model_dir=str(model_dir), fp16=True), key, cfg)

    # ---------- 请求校验 ----------
    def validate(self, spec: TTSRequest) -> Optional[str]:
        if self._state != "ready":
            return f"model not ready (state={self._state})"
        if spec.model != self._key:
            return f"model '{spec.model}' is not loaded (loaded: {self._key})"
        cfg = MODEL_REGISTRY[spec.model]
        modes, spk_modes = effective_modes(spec.model)
        if spec.mode not in modes:
            return f"mode '{spec.mode}' not supported by {spec.model}"
        if not spec.text.strip():
            return "text is empty"
        if len(spec.text) > 2000:
            return "text too long (max 2000 chars)"

        backend = self._backend
        if spec.mode in spk_modes:
            if not spec.spk_id:
                return f"{spec.mode} mode requires spk_id"
            available = backend.speakers() if backend else []
            if spec.spk_id not in available:
                # Qwen3-TTS SDK 返回小写 id, 兼容文档/历史里的大写写法并就地归一化
                match = next((s for s in available
                              if s.casefold() == (spec.spk_id or "").casefold()), None)
                if match:
                    spec.spk_id = match
                else:
                    return (f"speaker '{spec.spk_id}' not available "
                            f"(available: {', '.join(available) or 'none'})")
        if spec.mode in cfg["reference_modes"]:
            if not spec.prompt_wav:
                return f"{spec.mode} mode requires a reference voice (voice_id or prompt_wav)"
            if cfg["requires_prompt_text"] and spec.mode == "zero_shot" \
                    and not (spec.prompt_text or "").strip():
                return "zero_shot mode requires prompt_text (the transcript of the reference audio)"
        if cfg["instruct_kind"] == "design" and not (spec.instruct_text or "").strip():
            return "voice design mode requires instruct_text (声音描述)"
        if spec.stream and abs(spec.speed - 1.0) > 1e-6 and spec.model == "cosyvoice2":
            return "cosyvoice2 streaming speed not supported (use non-stream or cosyvoice3)"
        return None

    # ---------- 合成 ----------
    def _infer_worker(self, job: Job) -> None:
        """在工作线程中执行推理, 分块经 call_soon_threadsafe 送回事件循环。"""
        def put(item: tuple) -> None:
            job.loop.call_soon_threadsafe(job.out_q.put_nowait, item)

        try:
            # 排队期间模型可能已被切换, 重新校验
            if self._state != "ready" or self._key != job.spec.model:
                put(("error", f"model switched while job queued (loaded: {self._key})"))
                return
            if job.cancelled:
                put(("cancelled", None))
                job.done = True
                return
            t0 = time.time()
            first = True
            for pcm in self._backend.synthesize(job.spec):
                if job.cancelled:
                    # 分块边界停止: 生成器随之关闭, 上游流式推理循环立即退出
                    put(("cancelled", None))
                    job.done = True
                    return
                job.sample_rate = getattr(self._backend, "sample_rate", None) or job.sample_rate
                now = time.time()
                if first:
                    job.first_chunk_at = now
                    first = False
                put(("chunk", pcm))
            if job.cancelled:
                # 非流式推理不可中断 (单次前向): 结果作废, 按取消处理
                put(("cancelled", None))
                job.done = True
                return
            job.done = True
            job.error = None
            self._last_infer_ms = int((time.time() - t0) * 1000)
            put(("end", None))
        except Exception as e:
            logger.exception("inference failed")
            job.error = f"{type(e).__name__}: {e}"
            put(("error", job.error))
            # CUDA 上下文损坏时标记引擎进入 error 状态, 提示重载模型恢复
            if "CUDA error" in str(e) and self._state == "ready":
                self._state = "error"
                self._error = "CUDA context broken, click '加载此模型' to reload"
                logger.error("CUDA context corrupted, engine enters error state")
                self._broadcast(type="engine", state="error", model=self._key, error=self._error)

    async def submit(self, spec: TTSRequest) -> Job:
        """入队 (排队上限控制)。调用方通过 drain(job) 消费推理分块。"""
        assert self.task_queue is not None and self._loop is not None
        if self.task_queue.qsize() >= QUEUE_MAX:
            raise QueueFullError()
        job = Job(spec=spec, out_q=asyncio.Queue(), loop=self._loop)
        try:
            self.task_queue.put_nowait(job)
        except asyncio.QueueFull:
            raise QueueFullError()
        self.events.publish({"type": "queue", "queue_len": self.task_queue.qsize()})
        return job

    async def drain(self, job: Job) -> AsyncIterator[tuple[str, Optional[str]]]:
        """产出 ('chunk', pcm_bytes), 终止于 ('end', None) / ('error', msg) / ('cancelled', None)。"""
        while True:
            kind, payload = await job.out_q.get()
            if kind == "chunk":
                yield "chunk", payload
            elif kind == "end":
                yield "end", None
                break
            elif kind == "cancelled":
                yield "cancelled", None
                break
            else:
                yield "error", payload
                break

    async def worker_loop(self) -> None:
        """单 worker 串行消费 job 队列 (GPU 推理互斥), 推理在 executor 线程执行。"""
        while True:
            job = await self.task_queue.get()
            try:
                if job.cancelled:
                    # 停止时任务还在排队: 不再送入推理
                    job.done = True
                    job.loop.call_soon_threadsafe(job.out_q.put_nowait, ("cancelled", None))
                else:
                    await self._loop.run_in_executor(None, self._infer_worker, job)
            except Exception:  # pragma: no cover
                logger.exception("worker crashed on job")
                job.loop.call_soon_threadsafe(job.out_q.put_nowait, ("error", "internal worker error"))
            finally:
                self.task_queue.task_done()
                if self.task_queue:
                    self.events.publish({"type": "queue", "queue_len": self.task_queue.qsize()})


class QueueFullError(RuntimeError):
    pass


engine = Engine()
