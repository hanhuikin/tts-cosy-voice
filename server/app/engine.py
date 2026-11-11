"""CosyVoice 推理引擎封装。

职责:
- 管理 AutoModel 生命周期: 双模型互斥加载、后台线程加载、状态广播
- 统一四种合成模式到 AutoModel 调用 (含 CosyVoice3 的 prompt 前缀规则)
- 单 worker GPU 任务队列, 防止并发推理 OOM
- 流式输出: 推理线程 -> asyncio.Queue 桥接, int16 PCM 分块
"""
from __future__ import annotations

import asyncio
import logging
import os
import sys
import threading
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import AsyncIterator, Optional

REPO_ROOT = Path(__file__).resolve().parents[2]
COSYVOICE_ROOT = REPO_ROOT / "CosyVoice"
# 模型权重目录: 默认 <repo>/models/, 可用环境变量 COSYVOICE_MODELS_DIR 覆盖
MODELS_DIR = Path(os.environ.get("COSYVOICE_MODELS_DIR", REPO_ROOT / "models"))

logger = logging.getLogger("cosyvoice-server.engine")

QUEUE_MAX = 16  # 排队上限, 超出返回 429

MODEL_REGISTRY: dict[str, dict] = {
    "cosyvoice3": {
        "label": "CosyVoice 3.0 (0.5B)",
        "dir": MODELS_DIR / "Fun-CosyVoice3-0.5B",
        "modes": ["sft", "zero_shot", "cross_lingual", "instruct"],
        "assistant_prefix": "You are a helpful assistant.",
        "default_instructs": [
            "请用四川话说这句话。",
            "请用广东话表达。",
            "请用尽可能快的语速说一句话。",
            "请用开心的情绪说。",
        ],
    },
    "cosyvoice2": {
        "label": "CosyVoice 2.0 (0.5B)",
        "dir": MODELS_DIR / "CosyVoice2-0.5B",
        "modes": ["sft", "zero_shot", "cross_lingual", "instruct"],
        "assistant_prefix": "",
        "default_instructs": ["用四川话说这句话", "用开心的语气说", "用慢一点的语速说"],
    },
}


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


@dataclass
class Job:
    spec: TTSRequest
    out_q: asyncio.Queue
    loop: asyncio.AbstractEventLoop
    first_chunk_at: Optional[float] = None
    done: bool = False
    error: Optional[str] = None
    sample_rate: int = 24000


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
        self._model = None
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

        info = {
            "state": self._state,
            "model": self._key,
            "model_label": MODEL_REGISTRY[self._key]["label"] if self._key else None,
            "sample_rate": getattr(self._model, "sample_rate", None) if self._model else None,
            "speakers": self._model.list_available_spks() if (self._model and self._state == "ready") else [],
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
        out = []
        for key, cfg in MODEL_REGISTRY.items():
            out.append(
                {
                    "key": key,
                    "label": cfg["label"],
                    "modes": cfg["modes"],
                    "available": (cfg["dir"] / "llm.pt").exists(),
                    "loaded": self._key == key and self._state == "ready",
                    "default_instructs": cfg["default_instructs"],
                }
            )
        return out

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
            self._broadcast(type="engine", state="loading", model=key, label=MODEL_REGISTRY[key]["label"])
            t0 = time.time()
            try:
                import torch  # noqa: F401  (cosyvoice 内部依赖)
                from cosyvoice.cli.cosyvoice import AutoModel

                apply_upstream_patches()

                # 释放旧模型显存, 避免双模型驻留
                self._model = None
                if torch.cuda.is_available():
                    torch.cuda.empty_cache()

                cfg = MODEL_REGISTRY[key]
                model_dir = cfg["dir"]
                if not model_dir.exists():
                    # 未预下载时回退: AutoModel 会走 modelscope snapshot_download
                    repo_ids = {"cosyvoice3": "FunAudioLLM/Fun-CosyVoice3-0.5B-2512", "cosyvoice2": "iic/CosyVoice2-0.5B"}
                    model_dir = repo_ids[key]
                self._model = AutoModel(model_dir=str(model_dir), fp16=True)
                self._state = "ready"
                logger.info("model %s loaded in %.1fs, sample_rate=%s, speakers=%s",
                            key, time.time() - t0, self._model.sample_rate, self._model.list_available_spks())
            except Exception as e:  # pragma: no cover
                logger.exception("model load failed")
                self._model = None
                self._key = None
                self._state, self._error = "error", f"{type(e).__name__}: {e}"
            self._broadcast(type="engine", state=self._state, model=self._key,
                            label=MODEL_REGISTRY[key]["label"],
                            sample_rate=getattr(self._model, "sample_rate", None),
                            speakers=self._model.list_available_spks() if self._model else [],
                            error=self._error, load_ms=int((time.time() - t0) * 1000))

    # ---------- 请求校验 ----------
    def validate(self, spec: TTSRequest) -> Optional[str]:
        if self._state != "ready":
            return f"model not ready (state={self._state})"
        if spec.model != self._key:
            return f"model '{spec.model}' is not loaded (loaded: {self._key})"
        cfg = MODEL_REGISTRY[spec.model]
        if spec.mode not in cfg["modes"]:
            return f"mode '{spec.mode}' not supported by {spec.model}"
        if not spec.text.strip():
            return "text is empty"
        if len(spec.text) > 2000:
            return "text too long (max 2000 chars)"
        if spec.mode == "sft":
            if not spec.spk_id:
                return "sft mode requires spk_id"
            if spec.spk_id not in (self._model.list_available_spks() if self._model else []):
                return f"speaker '{spec.spk_id}' not available"
        else:
            if not spec.prompt_wav:
                return f"{spec.mode} mode requires a reference voice (voice_id or prompt_wav)"
            if spec.mode in ("zero_shot",) and not (spec.prompt_text or "").strip():
                return "zero_shot mode requires prompt_text (the transcript of the reference audio)"
        if spec.stream and abs(spec.speed - 1.0) > 1e-6 and spec.model == "cosyvoice2":
            return "cosyvoice2 streaming speed not supported (use non-stream or cosyvoice3)"
        return None

    # ---------- 合成 ----------
    def _build_generator(self, spec: TTSRequest):
        """映射请求到 AutoModel 推理生成器 (含 v3 prompt 前缀规则)。

        stream 标志透传给模型: 上游仅在非流式分支应用 speed 参数,
        流式分支变速不生效 (token2wav 最终块调用不传 speed)。
        """
        m, cfg = self._model, MODEL_REGISTRY[self._key]
        prefix = cfg["assistant_prefix"]
        # v3 流式语速由 cosyvoice_patch 支持; v2 流式保持 1.0 (validate 已拦截)
        speed = spec.speed if (not spec.stream or self._key == "cosyvoice3") else 1.0
        if spec.mode == "sft":
            return m.inference_sft(spec.text, spec.spk_id, stream=spec.stream, speed=speed)
        if spec.mode == "zero_shot":
            prompt_text = spec.prompt_text or ""
            if self._key == "cosyvoice3":
                prompt_text = f"{prefix}<|endofprompt|>{prompt_text}"
            return m.inference_zero_shot(spec.text, prompt_text, spec.prompt_wav, stream=spec.stream, speed=speed)
        if spec.mode == "cross_lingual":
            text = spec.text
            if self._key == "cosyvoice3":
                text = f"{prefix}<|endofprompt|>{text}"
            return m.inference_cross_lingual(text, spec.prompt_wav, stream=spec.stream, speed=speed)
        if spec.mode == "instruct":
            instruct = spec.instruct_text or ""
            if self._key == "cosyvoice3":
                instruct = f"{prefix} {instruct}<|endofprompt|>"
            elif "<|endofprompt|>" not in instruct:
                instruct = f"{instruct}<|endofprompt|>"
            return m.inference_instruct2(spec.text, instruct, spec.prompt_wav, stream=spec.stream, speed=speed)
        raise ValueError(f"unsupported mode: {spec.mode}")

    def _infer_worker(self, job: Job) -> None:
        """在工作线程中执行推理, 分块经 call_soon_threadsafe 送回事件循环。"""
        import torch

        def put(item: tuple) -> None:
            job.loop.call_soon_threadsafe(job.out_q.put_nowait, item)

        try:
            # 排队期间模型可能已被切换, 重新校验
            if self._state != "ready" or self._key != job.spec.model:
                put(("error", f"model switched while job queued (loaded: {self._key})"))
                return
            t0 = time.time()
            first = True
            for chunk in self._build_generator(job.spec):
                speech = chunk["tts_speech"]
                pcm = (
                    speech.squeeze(0).clamp(-1.0, 1.0).mul(32767.0).to(torch.int16).cpu().numpy().tobytes()
                )
                job.sample_rate = self._model.sample_rate
                now = time.time()
                if first:
                    job.first_chunk_at = now
                    first = False
                put(("chunk", pcm))
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
        """产出 ('chunk', pcm_bytes), 终止于 ('end', None) 或 ('error', msg)。"""
        while True:
            kind, payload = await job.out_q.get()
            if kind == "chunk":
                yield "chunk", payload
            elif kind == "end":
                yield "end", None
                break
            else:
                yield "error", payload
                break

    async def worker_loop(self) -> None:
        """单 worker 串行消费 job 队列 (GPU 推理互斥), 推理在 executor 线程执行。"""
        while True:
            job = await self.task_queue.get()
            try:
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
