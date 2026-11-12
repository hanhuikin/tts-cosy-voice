"""下载本地 TTS 模型权重到 models/ 目录。

用法:
    python scripts/download_models.py                          # 默认两个 CosyVoice 模型
    python scripts/download_models.py --models indextts25 qwen3tts
    python scripts/download_models.py --all                    # 全部 (约 24 GiB)

目录规则:
    默认 <repo>/models/<local_name>, 可用 COSYVOICE_MODELS_DIR 改根目录,
    或对单个模型用 *_DIR 环境变量指定 (与服务读取的配置一致)。

实现说明:
    直接按文件从 ModelScope 下载并写入目标路径, 支持 Range 断点续传。
    不使用 modelscope.snapshot_download: 它的分片下载会在结束时批量删除
    临时文件, 在受限环境下容易被批量删除保护拦截, 导致大文件下载失败。
"""
import argparse
import os
import sys
from pathlib import Path
from urllib.parse import quote

import requests

REPO_ROOT = Path(__file__).resolve().parents[1]
MODELS_DIR = Path(os.environ.get("COSYVOICE_MODELS_DIR", REPO_ROOT / "models"))
API_BASE = "https://www.modelscope.cn/api/v1/models"

# key -> (ModelScope repo_id, 默认目录名, 就绪标记文件(全部存在才算完成), 单模型目录环境变量)
# 标记里必须包含大权重: config.yaml / config.json 会先落盘, 只检查它们会把半截下载误判为完成
_QWEN_MARKERS = ("config.json", "model.safetensors")
MODELS = {
    "cosyvoice3": ("FunAudioLLM/Fun-CosyVoice3-0.5B-2512", "Fun-CosyVoice3-0.5B", ("llm.pt",), ""),
    "cosyvoice2": ("iic/CosyVoice2-0.5B", "CosyVoice2-0.5B", ("llm.pt",), ""),
    "indextts2": ("IndexTeam/IndexTTS-2", "IndexTTS-2",
                  ("config.yaml", "gpt.pth", "s2mel.pth"), "INDEXTTS2_DIR"),
    "indextts25": ("IndexTeam/IndexTTS-2.5", "IndexTTS-2.5",
                   ("config.yaml", "gpt.pth", "s2mel.pth", "codec.pth"), "INDEXTTS25_DIR"),
    "qwen3tts": ("Qwen/Qwen3-TTS-12Hz-1.7B-CustomVoice",
                 "Qwen3-TTS-12Hz-1.7B-CustomVoice", _QWEN_MARKERS, "QWEN3TTS_DIR"),
    "qwen3tts_base": ("Qwen/Qwen3-TTS-12Hz-1.7B-Base",
                      "Qwen3-TTS-12Hz-1.7B-Base", _QWEN_MARKERS, "QWEN3TTS_BASE_DIR"),
    "qwen3tts_design": ("Qwen/Qwen3-TTS-12Hz-1.7B-VoiceDesign",
                        "Qwen3-TTS-12Hz-1.7B-VoiceDesign", _QWEN_MARKERS, "QWEN3TTS_DESIGN_DIR"),
    # 共享组件: 不是可加载模型, 但离线部署 Qwen3-TTS 时建议一并下载
    "qwen3tts_tokenizer": ("Qwen/Qwen3-TTS-Tokenizer-12Hz",
                           "Qwen3-TTS-Tokenizer-12Hz", _QWEN_MARKERS, ""),
}


def target_dir(key: str) -> Path:
    _, local_name, _, env_name = MODELS[key]
    return Path(os.environ[env_name]) if env_name and env_name in os.environ \
        else MODELS_DIR / local_name


def list_remote_files(repo_id: str) -> list[tuple[str, int]]:
    r = requests.get(f"{API_BASE}/{repo_id}/repo/files",
                     params={"Revision": "master", "Recursive": "true"}, timeout=30)
    r.raise_for_status()
    files = r.json().get("Data", {}).get("Files") or []
    out = []
    for f in files:
        path, size = f.get("Path"), int(f.get("Size") or 0)
        if path and size > 0 and not path.endswith("/"):
            out.append((path, size))
    return sorted(out)


def download_file(repo_id: str, rel: str, expected: int, dest: Path) -> str:
    """下载单个文件, 已完整则跳过, 存在但未完成则用 Range 续传。"""
    if dest.exists() and dest.stat().st_size == expected:
        return "skip"
    dest.parent.mkdir(parents=True, exist_ok=True)
    url = f"{API_BASE}/{repo_id}/repo?Revision=master&FilePath={quote(rel, safe='/')}"
    have = dest.stat().st_size if dest.exists() else 0
    headers = {"Range": f"bytes={have}-"} if 0 < have < expected else {}
    timeout = (20, 120)
    with requests.get(url, headers=headers, stream=True, timeout=timeout) as resp:
        resp.raise_for_status()
        mode = "ab" if (resp.status_code == 206 and headers) else "wb"
        # 服务端不支持 Range 时 wb 会直接截断旧文件, 无需显式删除
        written = have if mode == "ab" else 0
        next_report = written + 200 * 1024**2
        with open(dest, mode) as fh:
            for chunk in resp.iter_content(chunk_size=4 * 1024**2):
                if not chunk:
                    continue
                fh.write(chunk)
                written += len(chunk)
                if written >= next_report:
                    print(f"    {rel}: {written / 1024**3:.2f}/{expected / 1024**3:.2f} GiB",
                          flush=True)
                    next_report = written + 200 * 1024**2
    if dest.stat().st_size != expected:
        raise RuntimeError(f"{rel}: 大小不符 {dest.stat().st_size} != {expected}, 重跑可续传")
    return "done"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--models", nargs="+", default=["cosyvoice3", "cosyvoice2"],
                        choices=list(MODELS))
    parser.add_argument("--all", action="store_true", help="下载全部模型 (约 24 GiB)")
    args = parser.parse_args()

    keys = list(MODELS) if args.all else args.models
    for key in keys:
        repo_id, _, markers, _ = MODELS[key]
        target = target_dir(key)
        if all((target / m).exists() for m in markers):
            print(f"[skip] {key} already downloaded: {target}", flush=True)
            continue
        missing = [m for m in markers if not (target / m).exists()]
        if missing:
            print(f"[resume] {key}: 缺少 {', '.join(missing)}, 继续下载", flush=True)
        print(f"[download] {key}: {repo_id} -> {target}", flush=True)
        files = list_remote_files(repo_id)
        print(f"    {len(files)} files, {sum(s for _, s in files) / 1024**3:.2f} GiB", flush=True)
        for rel, size in files:
            state = download_file(repo_id, rel, size, target / rel)
            if state != "skip":
                print(f"    {state}: {rel} ({size / 1024**2:.1f} MiB)", flush=True)
        still_missing = [m for m in markers if not (target / m).exists()]
        if still_missing:
            print(f"[error] {key}: 仍缺少 {', '.join(still_missing)}", flush=True)
            return 1
        print(f"[done] {key}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
