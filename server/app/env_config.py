"""启动时加载仓库根目录 .env, 让权重目录 / worker 解释器无需每次手动设环境变量。

优先级: 进程里已存在的环境变量 > .env 文件。PowerShell 中显式 ``$env:XXX = ...``
仍然可以覆盖 .env, 便于临时切换配置而不改文件。

仅实现配置读取所需的 dotenv 子集, 不引入第三方依赖:
    # 注释 / 空行           -> 忽略
    export KEY=VALUE        -> 支持
    KEY="D:\\path with space" / KEY='...'  -> 去引号
    KEY=VALUE # 行尾注释     -> 去注释
不做 ${VAR} 展开 (路径配置不需要, 避免循环引用)。

注意: model_registry 在模块导入时读取环境变量并固定各模型的 dir, 所以改完 .env
必须重启服务才生效。
"""
from __future__ import annotations

import os
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
DOTENV_PATH = REPO_ROOT / ".env"

# 与「系统状态与设置」弹窗中的 11 项配置一一对应, /api/admin/config 也用这份清单
CONFIG_KEYS = [
    "COSYVOICE_MODELS_DIR",
    "COSYVOICE3_DIR",
    "COSYVOICE2_DIR",
    "INDEXTTS2_DIR",
    "INDEXTTS25_DIR",
    "QWEN3TTS_DIR",
    "QWEN3TTS_BASE_DIR",
    "QWEN3TTS_DESIGN_DIR",
    "INDEXTTS_REPO",
    "INDEXTTS_PYTHON",
    "QWEN3TTS_PYTHON",
]

# 不在界面上展示、但同样可从 .env 读取的推理参数
EXTRA_KEYS = [
    "QWEN3TTS_DTYPE", "QWEN3TTS_DEVICE", "QWEN3TTS_ATTN",
    "INDEXTTS2_FP16", "INDEXTTS25_BF16", "COSYVOICE_DEFAULT_MODEL",
]


def parse(text: str) -> dict[str, str]:
    """解析 .env 文本, 返回 KEY -> VALUE (不含未设置的默认值)。"""
    values: dict[str, str] = {}
    for raw in text.splitlines():
        line = raw.strip().lstrip("\ufeff")
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].lstrip()
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        if not key or not all(ch.isalnum() or ch == "_" for ch in key):
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
            value = value[1:-1]
        else:
            value = value.split(" #", 1)[0].rstrip()
        values[key] = value
    return values


def load_dotenv(path: Path | None = None) -> dict[str, str]:
    """把 .env 里尚未在环境变量中定义的键写入 os.environ, 返回本次实际生效的项。"""
    target = Path(path) if path else DOTENV_PATH
    if not target.is_file():
        return {}
    try:
        values = parse(target.read_text(encoding="utf-8-sig"))
    except OSError:
        return {}
    applied: dict[str, str] = {}
    for key, value in values.items():
        if key not in os.environ:
            os.environ[key] = value
            applied[key] = value
    return applied


def describe() -> dict:
    """给 /api/admin/config 用的当前生效配置快照。"""
    return {
        "env_file": str(DOTENV_PATH),
        "env_file_exists": DOTENV_PATH.is_file(),
        "values": {key: os.environ.get(key, "") for key in CONFIG_KEYS},
        "runtime": {key: os.environ.get(key, "") for key in EXTRA_KEYS},
        "source": "env-file-or-process-environment",
        "requires_restart": True,
    }
