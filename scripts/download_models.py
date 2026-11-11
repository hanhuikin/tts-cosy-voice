"""下载 CosyVoice 预训练模型到 models/ 目录（可用环境变量 COSYVOICE_MODELS_DIR 覆盖）。

用法: python scripts/download_models.py [--models cosyvoice3 cosyvoice2]
"""
import argparse
import os
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
MODELS_DIR = Path(os.environ.get("COSYVOICE_MODELS_DIR", REPO_ROOT / "models"))
MODELS = {
    "cosyvoice3": ("FunAudioLLM/Fun-CosyVoice3-0.5B-2512", "Fun-CosyVoice3-0.5B"),
    "cosyvoice2": ("iic/CosyVoice2-0.5B", "CosyVoice2-0.5B"),
}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--models", nargs="+", default=["cosyvoice3", "cosyvoice2"], choices=MODELS)
    args = parser.parse_args()

    from modelscope import snapshot_download

    for key in args.models:
        repo_id, local_name = MODELS[key]
        target = MODELS_DIR / local_name
        if (target / "llm.pt").exists():
            print(f"[skip] {key} already downloaded: {target}")
            continue
        print(f"[download] {key}: {repo_id} -> {target}", flush=True)
        snapshot_download(repo_id, local_dir=str(target))
        print(f"[done] {key}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
