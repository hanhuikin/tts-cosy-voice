"""外部模型推理 worker (由 ExternalTTSBackend 以子进程方式常驻启动)。

协议: stdin / stdout 均为一行一个 JSON (JSON Lines)
    <- {"cmd":"load", "config": {...}}          -> {"ok":true,"speakers":[...],"sample_rate":24000}
    <- {"cmd":"synth","spec":{...},"out":"..."}-> {"ok":true,"sample_rate":24000}
    <- {"cmd":"quit"}                          -> {"ok":true}
失败统一返回 {"ok":false,"error":"..."}。

stdout 只用于协议输出: 启动时把它重定向到 stderr, 回复走 dup(1),
避免上游模型库 print / tqdm 污染协议流。所有诊断日志走 stderr。
可用独立虚拟环境的解释器运行, 从而与 CosyVoice 的依赖隔离:
    INDEXTTS_PYTHON / QWEN3TTS_PYTHON
"""
from __future__ import annotations

import json
import os
import re
import sys
import traceback
from pathlib import Path
from typing import Any

LANG_TAG_RE = re.compile(r"^<\|([a-z]{2})\|>\s*")

# 把 stdout 留给 JSON 协议回复, 第三方库的 print 一律改道 stderr
_reply = sys.stdout
try:
    _reply = os.fdopen(os.dup(sys.stdout.fileno()), "w", encoding="utf-8", buffering=1)
    sys.stdout = sys.stderr
except Exception:  # pragma: no cover - 极少数无 fileno 的环境
    _reply = sys.stdout


def log(msg: str) -> None:
    print(f"[tts-worker] {msg}", file=sys.stderr, flush=True)


def reply(payload: dict) -> None:
    _reply.write(json.dumps(payload, ensure_ascii=False) + "\n")
    _reply.flush()


class Worker:
    def __init__(self) -> None:
        self.cfg: dict = {}
        self.model: Any = None
        self.variant: str = ""
        self.speakers: list[str] = []
        self.sample_rate: int | None = None

    # ---------- 加载 ----------
    def load(self, cfg: dict) -> dict:
        self.cfg = cfg
        self.variant = cfg.get("variant") or ""
        family = cfg["family"]
        model_dir = Path(cfg["model_dir"])
        if not model_dir.exists():
            raise RuntimeError(f"模型权重目录不存在: {model_dir}")
        cfg_path = model_dir / "config.yaml"

        if family == "indextts":
            repo = Path(cfg.get("repo_dir") or "")
            if not repo.exists():
                raise RuntimeError(
                    f"未找到 index-tts 源码: {repo or '(未配置)'}。"
                    "请 git clone https://github.com/index-tts/index-tts.git 并设置 INDEXTTS_REPO"
                )
            if str(repo) not in sys.path:
                sys.path.insert(0, str(repo))
            # 上游部分实现以仓库根目录为 cwd 查找资源; 输出路径均为绝对路径, 不受影响
            os.chdir(str(repo))
            try:
                if self.variant == "2.0":
                    from indextts.infer_v2 import IndexTTS2  # type: ignore
                    self.model = IndexTTS2(
                        cfg_path=str(cfg_path), model_dir=str(model_dir),
                        use_fp16=bool(cfg.get("use_fp16")),
                        use_cuda_kernel=False, use_deepspeed=False,
                    )
                else:
                    from indextts.infer_v2_5 import IndexTTS2  # type: ignore
                    self.model = IndexTTS2(
                        cfg_path=str(cfg_path), model_dir=str(model_dir),
                        use_bf16=bool(cfg.get("use_bf16", True)),
                    )
            except ImportError as e:
                raise RuntimeError(
                    f"导入 indextts 失败 ({e})。请确认 INDEXTTS_REPO 指向完整仓库, "
                    "并在该解释器环境执行 `uv sync --all-extras`；"
                    "或使用 INDEXTTS_PYTHON 指向已安装依赖的解释器"
                ) from e
            log(f"IndexTTS {self.variant} loaded from {model_dir}")
            return {"ok": True, "speakers": [], "sample_rate": None}

        if family == "qwen3tts":
            try:
                import torch
                from qwen_tts import Qwen3TTSModel  # type: ignore
            except ImportError as e:
                raise RuntimeError(
                    f"导入 qwen_tts 失败 ({e})。请在 worker 解释器执行 `pip install -U qwen-tts`, "
                    "或用 QWEN3TTS_PYTHON 指向已安装该包的 Python 3.12 环境"
                ) from e
            kwargs: dict[str, Any] = {
                "device_map": cfg.get("device") or ("cuda:0" if torch.cuda.is_available() else "cpu"),
                "dtype": getattr(torch, cfg.get("dtype") or "bfloat16"),
            }
            if cfg.get("attn_implementation"):
                kwargs["attn_implementation"] = cfg["attn_implementation"]
            log(f"loading Qwen3-TTS {self.variant}: {model_dir} {kwargs}")
            self.model = Qwen3TTSModel.from_pretrained(str(model_dir), **kwargs)
            defaults = cfg.get("default_speakers") or []
            try:
                self.speakers = list(self.model.get_supported_speakers())
            except Exception:
                self.speakers = list(defaults)
            if not self.speakers:
                self.speakers = list(defaults)
            log(f"Qwen3-TTS loaded, speakers={self.speakers}")
            return {"ok": True, "speakers": self.speakers, "sample_rate": None}

        raise RuntimeError(f"unknown model family: {family}")

    # ---------- 合成 ----------
    def _text_and_lang(self, spec: dict) -> tuple[str, str | None]:
        text = spec.get("text") or ""
        lang = self.cfg.get("default_lang")
        m = LANG_TAG_RE.match(text)
        if m:
            text = text[m.end():]
            lang = (self.cfg.get("lang_map") or {}).get(m.group(1), lang)
        return text, lang

    def synth(self, msg: dict) -> dict:
        import soundfile as sf

        spec = msg["spec"]
        out = Path(msg["out"])
        out.parent.mkdir(parents=True, exist_ok=True)
        text, lang = self._text_and_lang(spec)
        speed = float(spec.get("speed") or 1.0)
        instruct = (spec.get("instruct_text") or "").strip()
        prompt_wav = spec.get("prompt_wav")

        if self.cfg["family"] == "indextts":
            if not prompt_wav or not Path(prompt_wav).exists():
                raise RuntimeError(f"参考音频不存在: {prompt_wav}")
            kwargs: dict[str, Any] = {
                "spk_audio_prompt": prompt_wav,
                "text": text,
                "output_path": str(out),
                "verbose": False,
            }
            if self.variant == "2.5":
                kwargs["lang"] = lang or "ZH"
                # duration_factor >1 变慢 / <1 变快, 与前端 speed 语义相反
                if abs(speed - 1.0) > 1e-6:
                    kwargs["duration_factor"] = round(1.0 / speed, 3)
            self.model.infer(**kwargs)
        elif self.cfg["family"] == "qwen3tts":
            import numpy as np

            wavs, sr = self._qwen_generate(text, lang, spec, instruct)
            wav = wavs[0]
            if hasattr(wav, "detach"):
                wav = wav.detach().cpu().numpy()
            arr = np.asarray(wav, dtype=np.float32)
            if arr.ndim > 1:
                arr = arr.mean(axis=0 if arr.shape[0] < arr.shape[1] else 1)
            sf.write(str(out), arr, int(sr))
            self.sample_rate = int(sr)
        else:
            raise RuntimeError("worker not loaded")

        info = sf.info(str(out))
        self.sample_rate = int(info.samplerate)
        return {"ok": True, "sample_rate": self.sample_rate, "duration": round(info.duration, 3)}

    def _qwen_generate(self, text: str, lang: str | None, spec: dict, instruct: str):
        m = self.model
        language = lang or "Chinese"
        if self.variant == "base":
            ref_audio = spec.get("prompt_wav")
            ref_text = (spec.get("prompt_text") or "").strip()
            if ref_text:
                return m.generate_voice_clone(
                    text=text, language=language, ref_audio=ref_audio, ref_text=ref_text,
                )
            # 无参考文本时退化为 x-vector only 克隆
            try:
                prompt = m.create_voice_clone_prompt(
                    ref_audio=ref_audio, ref_text=None, x_vector_only_mode=True,
                )
                return m.generate_voice_clone(text=text, language=language,
                                              voice_clone_prompt=prompt)
            except Exception as e:  # 老版本 SDK 无该接口时, 直接给空 ref_text
                log(f"create_voice_clone_prompt fallback: {e}")
                return m.generate_voice_clone(
                    text=text, language=language, ref_audio=ref_audio, ref_text="",
                )
        if self.variant == "voice_design":
            return m.generate_voice_design(
                text=text, language=language,
                instruct=instruct or "自然、清晰的普通说话声",
            )
        speaker = ((spec.get("spk_id") or "").strip()
                   or (self.speakers[0] if self.speakers else "vivian")).casefold()
        kwargs: dict[str, Any] = {}
        if instruct:
            kwargs["instruct"] = instruct
        return m.generate_custom_voice(text=text, language=language, speaker=speaker, **kwargs)


def main() -> int:
    worker = Worker()
    log("ready, waiting for commands")
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
            cmd = msg.get("cmd")
            if cmd == "quit":
                reply({"ok": True})
                break
            if cmd == "load":
                reply(worker.load(msg["config"]))
            elif cmd == "synth":
                if worker.model is None:
                    reply({"ok": False, "error": "model not loaded in worker"})
                else:
                    reply(worker.synth(msg))
            else:
                reply({"ok": False, "error": f"unknown cmd: {cmd}"})
        except Exception as e:
            traceback.print_exc(file=sys.stderr)
            reply({"ok": False, "error": f"{type(e).__name__}: {e}"})
    return 0


if __name__ == "__main__":
    sys.exit(main())
