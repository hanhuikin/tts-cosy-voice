"""统一模型注册表: 权重路径 / 运行环境 / 能力声明。

本模块不依赖 torch 或任何推理库, 可在服务启动时安全导入。
新增一个本地模型只需在这里加一项配置, 并在 tts_worker.py 里实现对应分支。

配置来源: 进程环境变量 > 仓库根目录 .env (由 env_config.load_dotenv 在导入时加载)
          两者都在服务启动时读取一次, 修改后需重启服务。
环境变量:
    COSYVOICE_MODELS_DIR   所有本地模型权重的根目录, 默认 <repo>/models
    COSYVOICE3_DIR / COSYVOICE2_DIR  单独覆盖两个 CosyVoice 权重目录
    INDEXTTS_REPO          index-tts 源码仓库路径, 默认 <repo>/index-tts
    INDEXTTS_PYTHON        运行 IndexTTS 的解释器, 默认当前服务解释器
    QWEN3TTS_PYTHON        运行 Qwen3-TTS 的解释器, 默认当前服务解释器
    INDEXTTS2_DIR / INDEXTTS25_DIR
    QWEN3TTS_DIR / QWEN3TTS_BASE_DIR / QWEN3TTS_DESIGN_DIR
    QWEN3TTS_DTYPE         bfloat16 (默认) / float16 / float32
    QWEN3TTS_DEVICE        cuda:0 (默认, 无 GPU 时 cpu)
    QWEN3TTS_ATTN          可选 attn_implementation, 例如 flash_attention_2
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

from .env_config import load_dotenv

load_dotenv()  # 必须先于下面所有路径计算; 进程里已设置的变量优先

REPO_ROOT = Path(__file__).resolve().parents[2]
MODELS_DIR = Path(os.environ.get("COSYVOICE_MODELS_DIR", REPO_ROOT / "models"))
INDEXTTS_REPO = Path(os.environ.get("INDEXTTS_REPO", REPO_ROOT / "index-tts"))

# 与 Qwen3TTSModel.get_supported_speakers() 实际返回值一致 (小写 id)
QWEN3_SPEAKERS = [
    "aiden", "dylan", "eric", "ono_anna", "ryan",
    "serena", "sohee", "uncle_fu", "vivian",
]

# 前端 <|xx|> 语言标签 -> 各模型 API 的 language 取值
QWEN3_LANG_MAP = {
    "zh": "Chinese", "en": "English", "ja": "Japanese", "ko": "Korean",
    "de": "German", "fr": "French", "ru": "Russian", "es": "Spanish",
    "it": "Italian", "pt": "Portuguese", "yue": "Chinese",
}
INDEXTTS25_LANG_MAP = {
    "zh": "ZH", "en": "EN", "ja": "JA", "es": "ES", "ar": "AR", "yue": "ZH",
}


def _dir(env_name: str, default_name: str) -> Path:
    return Path(os.environ.get(env_name, MODELS_DIR / default_name))


def _common(label: str, dir_path: Path, family: str, backend: str,
            modes: list[str], **extra) -> dict:
    cfg = {
        "label": label,
        "dir": dir_path,
        "family": family,
        "backend": backend,             # inprocess | external
        "modes": modes,
        "reference_modes": [m for m in modes if m != "sft"],
        "speaker_modes": ["sft"],
        "requires_prompt_text": True,   # zero_shot 是否强制参考文本
        "instruct_kind": "cosyvoice",   # cosyvoice | style | design
        "default_instructs": [],
        "speed_mode": "post",           # native(模型原生) | post(音频后处理) | cosyvoice
        "stream_mode": "buffered",      # native(逐块推理) | buffered(整段生成后分块下发)
        "lang_map": {},
        "default_lang": None,
        # 就绪判定必须包含权重文件: 小文件先落盘, 中途断网不能被误判为"已下载"
        "markers": ["config.json", "model.safetensors"],
        "assistant_prefix": "",
        "install_hint": "",
    }
    cfg.update(extra)
    return cfg


MODEL_REGISTRY: dict[str, dict] = {
    # ---------------- CosyVoice (进程内, 原有逻辑) ----------------
    "cosyvoice3": _common(
        "CosyVoice 3.0 (0.5B)", _dir("COSYVOICE3_DIR", "Fun-CosyVoice3-0.5B"), "cosyvoice", "inprocess",
        ["sft", "zero_shot", "cross_lingual", "instruct"],
        assistant_prefix="You are a helpful assistant.",
        default_instructs=["请用四川话说这句话。", "请用广东话表达。",
                           "请用尽可能快的语速说一句话。", "请用开心的情绪说。"],
        speed_mode="cosyvoice", stream_mode="native", markers=["llm.pt"],
        install_hint="python scripts/download_models.py --models cosyvoice3",
    ),
    "cosyvoice2": _common(
        "CosyVoice 2.0 (0.5B)", _dir("COSYVOICE2_DIR", "CosyVoice2-0.5B"), "cosyvoice", "inprocess",
        ["sft", "zero_shot", "cross_lingual", "instruct"],
        default_instructs=["用四川话说这句话", "用开心的语气说", "用慢一点的语速说"],
        speed_mode="cosyvoice", stream_mode="native", markers=["llm.pt"],
        install_hint="python scripts/download_models.py --models cosyvoice2",
    ),
    # ---------------- IndexTTS (外部解释器 worker) ----------------
    "indextts2": _common(
        "IndexTTS 2.0", _dir("INDEXTTS2_DIR", "IndexTTS-2"), "indextts", "external",
        ["zero_shot", "cross_lingual"],
        reference_modes=["zero_shot", "cross_lingual"], speaker_modes=[],
        requires_prompt_text=False, variant="2.0",
        markers=["config.yaml", "gpt.pth", "s2mel.pth"],
        default_instructs=["用悲伤的情绪说", "用开心的语气说"],
        install_hint=("下载权重: python scripts/download_models.py --models indextts2；"
                      "源码: git clone https://github.com/index-tts/index-tts.git；"
                      "依赖较多时建议独立 venv 并设 INDEXTTS_PYTHON"),
        hf_repo="IndexTeam/IndexTTS-2",
    ),
    "indextts25": _common(
        "IndexTTS 2.5 (多语言)", _dir("INDEXTTS25_DIR", "IndexTTS-2.5"), "indextts", "external",
        ["zero_shot", "cross_lingual"],
        reference_modes=["zero_shot", "cross_lingual"], speaker_modes=[],
        requires_prompt_text=False, variant="2.5",
        markers=["config.yaml", "gpt.pth", "s2mel.pth", "codec.pth"],
        speed_mode="native", lang_map=INDEXTTS25_LANG_MAP, default_lang="ZH",
        default_instructs=["用悲伤的情绪说", "用开心的语气说"],
        install_hint=("下载权重: python scripts/download_models.py --models indextts25；"
                      "源码: git clone https://github.com/index-tts/index-tts.git；"
                      "依赖较多时建议独立 venv 并设 INDEXTTS_PYTHON"),
        hf_repo="IndexTeam/IndexTTS-2.5",
    ),
    # ---------------- Qwen3-TTS (外部解释器 worker) ----------------
    "qwen3tts": _common(
        "Qwen3-TTS CustomVoice (1.7B)", _dir("QWEN3TTS_DIR", "Qwen3-TTS-12Hz-1.7B-CustomVoice"),
        "qwen3tts", "external", ["sft", "instruct"],
        reference_modes=[], speaker_modes=["sft", "instruct"], requires_prompt_text=False,
        variant="custom_voice", instruct_kind="style",
        lang_map=QWEN3_LANG_MAP, default_lang="Chinese",
        default_instructs=["用特别愤怒的语气说", "温柔、轻快、带微笑", "语速放慢，像讲睡前故事"],
        install_hint=("下载权重: python scripts/download_models.py --models qwen3tts；"
                      "依赖: pip install -U qwen-tts（建议 Python 3.12 独立 venv，设 QWEN3TTS_PYTHON）"),
        hf_repo="Qwen/Qwen3-TTS-12Hz-1.7B-CustomVoice",
    ),
    "qwen3tts_base": _common(
        "Qwen3-TTS Base (声音克隆)", _dir("QWEN3TTS_BASE_DIR", "Qwen3-TTS-12Hz-1.7B-Base"),
        "qwen3tts", "external", ["zero_shot", "cross_lingual"],
        reference_modes=["zero_shot", "cross_lingual"], speaker_modes=[],
        variant="base", lang_map=QWEN3_LANG_MAP, default_lang="Chinese",
        install_hint=("下载权重: python scripts/download_models.py --models qwen3tts_base；"
                      "依赖: pip install -U qwen-tts（建议 Python 3.12 独立 venv，设 QWEN3TTS_PYTHON）"),
        hf_repo="Qwen/Qwen3-TTS-12Hz-1.7B-Base",
    ),
    "qwen3tts_design": _common(
        "Qwen3-TTS VoiceDesign (声音设计)", _dir("QWEN3TTS_DESIGN_DIR", "Qwen3-TTS-12Hz-1.7B-VoiceDesign"),
        "qwen3tts", "external", ["instruct"],
        reference_modes=[], speaker_modes=[], requires_prompt_text=False,
        variant="voice_design", instruct_kind="design",
        lang_map=QWEN3_LANG_MAP, default_lang="Chinese",
        default_instructs=["沉稳的中年男声，语速偏慢，适合纪录片旁白",
                           "撒娇稚嫩的萝莉女声，音调偏高且起伏明显",
                           "清亮的年轻女声，语气轻快、带笑意"],
        install_hint=("下载权重: python scripts/download_models.py --models qwen3tts_design；"
                      "依赖: pip install -U qwen-tts（建议 Python 3.12 独立 venv，设 QWEN3TTS_PYTHON）"),
        hf_repo="Qwen/Qwen3-TTS-12Hz-1.7B-VoiceDesign",
    ),
}

MODEL_KEYS = tuple(MODEL_REGISTRY)


def runtime_python(key: str) -> str:
    """返回该模型 worker 使用的解释器路径。

    优先级: 环境变量 > 约定位置的专用 venv > 主解释器。
    约定位置: <index-tts 仓库>/.venv (IndexTTS)、<repo>/.venv-qwen3 (Qwen3-TTS),
    与 STARTUP.md 方式 D 的手工配置一致, 避免忘设环境变量时回退到主 venv
    导致「No module named indextts/qwen_tts」。
    """
    cfg = MODEL_REGISTRY[key]
    env_name = "INDEXTTS_PYTHON" if cfg["family"] == "indextts" else "QWEN3TTS_PYTHON"
    env = os.environ.get(env_name)
    if env:
        return env
    if cfg["family"] == "indextts":
        candidates = [INDEXTTS_REPO / ".venv" / "Scripts" / "python.exe",
                      INDEXTTS_REPO / ".venv" / "bin" / "python"]
    else:
        candidates = [REPO_ROOT / ".venv-qwen3" / "Scripts" / "python.exe",
                      REPO_ROOT / ".venv-qwen3" / "bin" / "python"]
    for c in candidates:
        if c.exists():
            return str(c)
    return sys.executable


def is_available(key: str) -> bool:
    """权重目录是否就绪 (以关键标记文件判断, 不触发任何下载)。"""
    cfg = MODEL_REGISTRY[key]
    return all((cfg["dir"] / marker).exists() for marker in cfg["markers"])


def effective_modes(key: str) -> tuple[list[str], list[str]]:
    """按功能返回实际可用的 (modes, speaker_modes)。

    CosyVoice 系列的内置音色存放在模型目录的 spk2info.pt (官方权重不附带,
    仅在注册克隆音色后生成), 因此 sft 是否可用以该文件是否存在为准,
    而不是静态声明; 其余家族 (如 Qwen3-TTS 内置说话人) 不受影响。
    """
    cfg = MODEL_REGISTRY[key]
    modes, spk_modes = cfg["modes"], cfg["speaker_modes"]
    if cfg["family"] == "cosyvoice" and not (cfg["dir"] / "spk2info.pt").exists():
        modes = [m for m in modes if m != "sft"]
        spk_modes = [m for m in spk_modes if m != "sft"]
    return modes, spk_modes


def worker_load_config(key: str) -> dict:
    """组装传给 tts_worker.py 的 load 参数 (worker 不读取本模块)。"""
    cfg = MODEL_REGISTRY[key]
    base = {
        "key": key,
        "family": cfg["family"],
        "variant": cfg.get("variant"),
        "model_dir": str(cfg["dir"]),
        "lang_map": cfg["lang_map"],
        "default_lang": cfg["default_lang"],
        "speed_mode": cfg["speed_mode"],
        "default_speakers": QWEN3_SPEAKERS if cfg["family"] == "qwen3tts" else [],
    }
    if cfg["family"] == "indextts":
        base.update({
            "repo_dir": str(INDEXTTS_REPO),
            "use_fp16": os.environ.get("INDEXTTS2_FP16", "0") == "1",
            "use_bf16": os.environ.get("INDEXTTS25_BF16", "1") == "1",
        })
    elif cfg["family"] == "qwen3tts":
        base.update({
            "dtype": os.environ.get("QWEN3TTS_DTYPE", "bfloat16"),
            "device": os.environ.get("QWEN3TTS_DEVICE", ""),  # 空 -> worker 自动判断
            "attn_implementation": os.environ.get("QWEN3TTS_ATTN", ""),
        })
    return base


def public_info(key: str, loaded: bool = False) -> dict:
    """给 /api/health、/api/models 与前端使用的模型描述。"""
    cfg = MODEL_REGISTRY[key]
    modes, spk_modes = effective_modes(key)
    return {
        "key": key,
        "label": cfg["label"],
        "family": cfg["family"],
        "backend": cfg["backend"],
        "modes": modes,
        "available": is_available(key),
        "loaded": loaded,
        "default_instructs": cfg["default_instructs"],
        "instruct_kind": cfg["instruct_kind"],
        "reference_modes": cfg["reference_modes"],
        "speaker_modes": spk_modes,
        "requires_prompt_text": cfg["requires_prompt_text"],
        "stream_mode": cfg["stream_mode"],
        "stream_speed_supported": not (key == "cosyvoice2"),
        "lang_codes": sorted(cfg["lang_map"].keys()),
        "dir": str(cfg["dir"]),
        "runtime": runtime_python(key) if cfg["backend"] == "external" else "in-process",
        "install_hint": cfg["install_hint"],
    }
