"""从 GitHub 开源项目拉取精选音色, Whisper 自动转写, 批量入库。

音色来源 (均为公开项目示例/CC0 素材):
- QwenAudio/CosyVoice        asset/cross_lingual_prompt.wav   (官方跨语言示例)
- SWivid/F5-TTS              basic_ref_zh.wav / basic_ref_en.wav (推理示例, en 带逐字文本)
- OpenBMB/VoxCPM             reference_speaker.wav            (中文项目示例)
- OwenTyme/voice-zero        CC0 英文朗读样本 (LibriVox/Archive.org)

用法: .venv/Scripts/python.exe scripts/add_preset_voices.py [--transcribe-only] [--skip-existing]
"""
import argparse
import json
import sys
import tempfile
import time
from pathlib import Path

import requests

REPO = Path(__file__).resolve().parents[1]
BASE = "http://127.0.0.1:8000"
TMP = Path(tempfile.gettempdir()) / "cosyvoice_voices"

# (显示名, 下载 URL 或本地路径, 语言, 已知转写或 None=Whisper 转写)
VOICES = [
    ("官方示例女声ZH", "https://raw.githubusercontent.com/QwenAudio/CosyVoice/main/asset/cross_lingual_prompt.wav", "zh",
     "在那之后完全收购那家公司，因此保持管理层的一致性，利益与即将加入家族的资产保持一致，这就是我们有时不买下全部的原因。"),
    ("太乙真人ZH", "https://raw.githubusercontent.com/SWivid/F5-TTS/main/src/f5_tts/infer/examples/basic/basic_ref_zh.wav", "zh",
     "对，这就是我万人敬仰的太乙真人。"),
    ("自然女声EN", "https://raw.githubusercontent.com/SWivid/F5-TTS/main/src/f5_tts/infer/examples/basic/basic_ref_en.wav", "en",
     "Some call me nature, others call me mother nature."),
    ("吐槽男声EN", "https://raw.githubusercontent.com/OpenBMB/VoxCPM/main/examples/reference_speaker.wav", "en",
     "Hey, hey, hey! What's up with you? If you don't eat the ice cream on the table, I'm going to lose it! "
     "Don't think I don't know you played games all night again last night! If you do this again next time, I'm going to be mad!"),
    ("朗诵男声EN", "https://raw.githubusercontent.com/OwenTyme/voice-zero/main/voices/alan_davis_drake.flac", "en", None),
    ("温柔女声EN", "https://raw.githubusercontent.com/OwenTyme/voice-zero/main/voices/amy_koenig.flac", "en",
     "Read for LibreVox.org by Amy Koenig on July 23, 2011."),
    ("清亮女声EN", "https://raw.githubusercontent.com/OwenTyme/voice-zero/main/voices/alana_jordan.flac", "en", None),
]

WHISPER_MODEL = "base"


def download(url: str) -> Path:
    TMP.mkdir(exist_ok=True)
    out = TMP / Path(url).name
    if not out.exists():
        print(f"  [download] {Path(url).name} ...", flush=True)
        r = requests.get(url, timeout=120)
        r.raise_for_status()
        out.write_bytes(r.content)
    return out


def transcribe(path: Path, lang: str) -> tuple[str, float]:
    """Whisper 转写 (librosa 解码, 不依赖 ffmpeg), 返回 (文本, 时长秒)。"""
    import librosa
    import numpy as np
    import whisper

    audio, _ = librosa.load(str(path), sr=16000, mono=True)
    model = whisper.load_model(WHISPER_MODEL)
    result = model.transcribe(np.asarray(audio), language=lang)
    text = " ".join(seg["text"].strip() for seg in result["segments"]).strip()
    dur = result["segments"][-1]["end"] if result["segments"] else 0.0
    return text, dur


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--skip-existing", action="store_true", help="同名音色已存在则跳过")
    args = ap.parse_args()

    # 服务就绪
    for _ in range(60):
        try:
            if requests.get(f"{BASE}/api/health", timeout=3).json()["state"] == "ready":
                break
        except Exception:
            pass
        time.sleep(2)
    else:
        print("server not ready"); return 1

    existing = {v["name"] for v in requests.get(f"{BASE}/api/voices").json()}
    ok, fail = 0, 0
    for name, src, lang, known_text in VOICES:
        print(f"[voice] {name} ({lang})")
        if args.skip_existing and name in existing:
            print("  skip (already exists)")
            continue
        try:
            # 1. 获取音频
            path = Path(src) if Path(src).exists() else download(src)
            # 2. 转写 (已知文本则跳过)
            if known_text:
                text = known_text
            else:
                print("  [whisper] transcribing ...", flush=True)
                text, _ = transcribe(path, lang)
            if not text:
                print("  FAIL: empty transcript"); fail += 1; continue
            # 3. 入库
            with open(path, "rb") as f:
                r = requests.post(f"{BASE}/api/voices",
                                  data={"name": name, "prompt_text": text},
                                  files={"file": (path.name, f)})
            if not r.ok:
                print(f"  FAIL: {r.status_code} {r.text[:120]}"); fail += 1; continue
            v = r.json()
            print(f"  OK  id={v['id']} {v['duration']:.1f}s  text: {text[:60]}")
            ok += 1
        except Exception as e:
            print(f"  FAIL: {type(e).__name__}: {e}")
            fail += 1

    print(f"\ndone: {ok} added, {fail} failed, total voices: "
          f"{len(requests.get(f'{BASE}/api/voices').json())}")
    return 0 if fail == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
