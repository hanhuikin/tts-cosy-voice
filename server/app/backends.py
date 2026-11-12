"""推理后端: 把不同模型的差异收敛为统一接口。

Backend 契约:
    sample_rate: int | None      实际输出采样率 (加载后或首次合成后可得)
    speakers() -> list[str]      内置说话人
    synthesize(spec) -> Iterator[bytes]   单声道 int16 PCM 分块
    close()                      释放显存 / 结束子进程

- CosyVoiceBackend: 进程内 AutoModel, 保留原生流式与 v3 指令前缀规则
- ExternalTTSBackend: 常驻子进程 worker, 用于 IndexTTS / Qwen3-TTS 等依赖较重的本地模型。
  子进程可用独立虚拟环境的解释器运行, 避免与主环境 (Python 3.10 + CosyVoice) 冲突。
"""
from __future__ import annotations

import json
import logging
import os
import queue
import subprocess
import tempfile
import threading
import uuid
from pathlib import Path
from typing import Iterator, Optional

import numpy as np
import soundfile as sf

from .model_registry import runtime_python, worker_load_config

logger = logging.getLogger("cosyvoice-server.backends")

WORKER_SCRIPT = Path(__file__).with_name("tts_worker.py")
TMP_DIR = Path(tempfile.gettempdir()) / "cosyvoice-server-tts-worker"
LOAD_TIMEOUT = 900      # 首次加载含权重初始化 / 辅助模型下载
SYNTH_TIMEOUT = 900
PCM_CHUNK_SECONDS = 0.2
# 与 CosyVoice / 播客 / 流式播放器保持一致, 外部模型统一重采样到 24k 单声道
TARGET_SAMPLE_RATE = 24000


# ---------------------------------------------------------------- CosyVoice
class CosyVoiceBackend:
    def __init__(self, model, key: str, cfg: dict) -> None:
        self.model = model
        self.key = key
        self.cfg = cfg
        self.sample_rate = int(getattr(model, "sample_rate", 24000) or 24000)

    def speakers(self) -> list[str]:
        try:
            return list(self.model.list_available_spks())
        except Exception:
            return []

    def close(self) -> None:
        self.model = None

    def synthesize(self, spec) -> Iterator[bytes]:
        import torch

        for chunk in self._generator(spec):
            speech = chunk["tts_speech"]
            yield (
                speech.squeeze(0).clamp(-1.0, 1.0).mul(32767.0)
                .to(torch.int16).cpu().numpy().tobytes()
            )

    def _generator(self, spec):
        """映射请求到 AutoModel 推理 (含 CosyVoice3 prompt 前缀规则)。"""
        m, key = self.model, self.key
        prefix = self.cfg["assistant_prefix"]
        # v3 流式语速由 cosyvoice_patch 支持; v2 流式保持 1.0 (engine.validate 已拦截)
        speed = spec.speed if (not spec.stream or key == "cosyvoice3") else 1.0
        if spec.mode == "sft":
            return m.inference_sft(spec.text, spec.spk_id, stream=spec.stream, speed=speed)
        if spec.mode == "zero_shot":
            prompt_text = spec.prompt_text or ""
            if key == "cosyvoice3":
                prompt_text = f"{prefix}<|endofprompt|>{prompt_text}"
            return m.inference_zero_shot(spec.text, prompt_text, spec.prompt_wav,
                                         stream=spec.stream, speed=speed)
        if spec.mode == "cross_lingual":
            text = spec.text
            if key == "cosyvoice3":
                text = f"{prefix}<|endofprompt|>{text}"
            return m.inference_cross_lingual(text, spec.prompt_wav, stream=spec.stream, speed=speed)
        if spec.mode == "instruct":
            instruct = spec.instruct_text or ""
            if key == "cosyvoice3":
                instruct = f"{prefix} {instruct}<|endofprompt|>"
            elif "<|endofprompt|>" not in instruct:
                instruct = f"{instruct}<|endofprompt|>"
            return m.inference_instruct2(spec.text, instruct, spec.prompt_wav,
                                         stream=spec.stream, speed=speed)
        raise ValueError(f"unsupported mode: {spec.mode}")


# ------------------------------------------------------------ 外部模型 worker
class ExternalTTSBackend:
    """通过 JSON Lines 与常驻 tts_worker.py 通信, 一次加载、多次合成。"""

    def __init__(self, key: str, cfg: dict) -> None:
        self.key = key
        self.cfg = cfg
        self.sample_rate: Optional[int] = None
        self.speakers_list: list[str] = []
        self.proc: Optional[subprocess.Popen] = None
        self._responses: queue.Queue = queue.Queue()
        self._io_lock = threading.Lock()
        self._closed = False

    def speakers(self) -> list[str]:
        return list(self.speakers_list)

    def load(self) -> None:
        TMP_DIR.mkdir(parents=True, exist_ok=True)
        python = runtime_python(self.key)
        # 绝对路径必须存在; PATH 上的命令名交给 Popen 解析
        if not Path(python).exists() and os.path.basename(python) != python:
            raise RuntimeError(f"worker 解释器不存在: {python}")
        cmd = [python, "-u", str(WORKER_SCRIPT)]
        logger.info("starting %s worker: %s", self.key, " ".join(cmd))
        creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0
        self.proc = subprocess.Popen(
            cmd,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=None,  # 继承服务端 stderr, 模型日志直接进 server_run.log
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
            creationflags=creationflags,
        )
        threading.Thread(target=self._reader_loop, daemon=True).start()
        try:
            resp = self._request({"cmd": "load", "config": worker_load_config(self.key)},
                                 timeout=LOAD_TIMEOUT)
        except Exception as e:
            msg = (f"{e}\n模型: {self.cfg['label']}\n"
                   f"解释器: {python}\n权重目录: {self.cfg['dir']}\n"
                   f"安装提示: {self.cfg['install_hint']}")
            self.close()
            raise RuntimeError(msg) from e
        self.speakers_list = list(resp.get("speakers") or [])
        self.sample_rate = TARGET_SAMPLE_RATE

    def _reader_loop(self) -> None:
        assert self.proc is not None and self.proc.stdout is not None
        try:
            for line in self.proc.stdout:
                line = line.strip()
                if not line:
                    continue
                try:
                    self._responses.put(json.loads(line))
                except json.JSONDecodeError:
                    logger.warning("[%s] worker 非协议输出: %s", self.key, line[:200])
        finally:
            self._responses.put({"ok": False, "error": "worker 进程已退出"})

    def _request(self, payload: dict, timeout: int) -> dict:
        with self._io_lock:
            if self.proc is None or self.proc.poll() is not None:
                raise RuntimeError("worker 进程未运行 (可能加载失败或已退出)")
            assert self.proc.stdin is not None
            self.proc.stdin.write(json.dumps(payload, ensure_ascii=False) + "\n")
            self.proc.stdin.flush()
            try:
                resp = self._responses.get(timeout=timeout)
            except queue.Empty as e:
                raise RuntimeError(f"worker {payload['cmd']} 超时 ({timeout}s)") from e
        if not resp.get("ok"):
            raise RuntimeError(resp.get("error") or "worker 返回未知错误")
        return resp

    def synthesize(self, spec) -> Iterator[bytes]:
        out = TMP_DIR / f"{self.key}_{uuid.uuid4().hex[:12]}.wav"
        payload = {
            "cmd": "synth",
            "spec": {
                "text": spec.text, "model": spec.model, "mode": spec.mode,
                "spk_id": spec.spk_id, "prompt_wav": spec.prompt_wav,
                "prompt_text": spec.prompt_text, "instruct_text": spec.instruct_text,
                "speed": spec.speed, "stream": spec.stream,
            },
            "out": str(out),
        }
        resp = self._request(payload, timeout=SYNTH_TIMEOUT)
        try:
            data, sr = sf.read(str(out), dtype="float32", always_2d=True)
            sr = int(sr or resp.get("sample_rate") or TARGET_SAMPLE_RATE)
            mono = data.mean(axis=1)
            if sr != TARGET_SAMPLE_RATE:
                mono = self._resample(mono, sr, TARGET_SAMPLE_RATE)
                sr = TARGET_SAMPLE_RATE
            if abs(spec.speed - 1.0) > 1e-6 and self.cfg["speed_mode"] == "post":
                mono = self._time_stretch(mono, spec.speed)
            self.sample_rate = sr
            pcm = (np.clip(mono, -1.0, 1.0) * 32767.0).astype(np.int16).tobytes()
            step = max(2, int(sr * PCM_CHUNK_SECONDS) * 2)
            for i in range(0, len(pcm), step):
                yield pcm[i:i + step]
        finally:
            out.unlink(missing_ok=True)

    @staticmethod
    def _resample(samples: np.ndarray, orig_sr: int, target_sr: int) -> np.ndarray:
        try:
            import librosa
            return np.asarray(librosa.resample(samples, orig_sr=orig_sr, target_sr=target_sr),
                              dtype=np.float32)
        except Exception:  # pragma: no cover
            n = max(1, round(len(samples) * target_sr / orig_sr))
            return np.interp(np.linspace(0, len(samples) - 1, n),
                             np.arange(len(samples)), samples).astype(np.float32)

    @staticmethod
    def _time_stretch(samples: np.ndarray, speed: float) -> np.ndarray:
        """变速不变调 (librosa); 缺少 librosa 时退化为线性重采样。"""
        try:
            import librosa
            return np.asarray(librosa.effects.time_stretch(samples, rate=float(speed)),
                              dtype=np.float32)
        except Exception as e:  # pragma: no cover
            logger.warning("librosa time_stretch 不可用(%s), 退化为线性重采样", e)
            n = max(1, int(len(samples) / float(speed)))
            return np.interp(np.linspace(0, len(samples) - 1, n),
                             np.arange(len(samples)), samples).astype(np.float32)

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        proc, self.proc = self.proc, None
        if proc is None:
            return
        try:
            if proc.stdin and not proc.stdin.closed:
                proc.stdin.write(json.dumps({"cmd": "quit"}) + "\n")
                proc.stdin.flush()
                proc.stdin.close()
        except Exception:
            pass
        try:
            proc.wait(timeout=5)
        except Exception:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except Exception:
                proc.kill()
        logger.info("%s worker stopped", self.key)
