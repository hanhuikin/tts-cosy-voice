"""音频工具: WAV 头打包、PCM 块拼接、参考音频重采样校验。"""
from __future__ import annotations

import io
import struct
import wave
from pathlib import Path

import numpy as np
import soundfile as sf


def wav_header(sample_rate: int, num_samples: int, channels: int = 1, bits: int = 16) -> bytes:
    """生成 44 字节标准 RIFF 头。流式时 num_samples 传估算值(可被后续覆盖, 播放器按块读取即可)。"""
    data_len = num_samples * channels * bits // 8
    return struct.pack(
        "<4sI4s4sIHHIIHH4sI",
        b"RIFF", 36 + data_len, b"WAVE",
        b"fmt ", 16, 1, channels, sample_rate,
        sample_rate * channels * bits // 8, channels * bits // 8, bits,
        b"data", data_len,
    )


def pcm_to_wav(pcm_parts: list[bytes], sample_rate: int, channels: int = 1) -> bytes:
    """把 int16 PCM 分块拼成完整 WAV (用于非流式响应与流式结果落盘)。"""
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(channels)
        w.setsampwidth(2)
        w.setframerate(sample_rate)
        for part in pcm_parts:
            w.writeframes(part)
    return buf.getvalue()


def pcm_duration_seconds(pcm_len: int, sample_rate: int, channels: int = 1) -> float:
    return pcm_len / 2 / channels / sample_rate


def load_reference_audio(path: str | Path, target_sr: int = 16000, max_seconds: float = 30.0) -> tuple[np.ndarray, int]:
    """加载参考音频并重采样为单声道 16k float32 (-1..1)。超长截断。"""
    data, sr = sf.read(str(path), dtype="float32", always_2d=True)
    mono = data.mean(axis=1)
    max_len = int(target_sr * max_seconds)
    if sr != target_sr:
        # 线性插值重采样, 避免引入重依赖 (librosa 在此环境也可用, 但线性插值足够稳)
        import librosa

        mono = librosa.resample(mono, orig_sr=sr, target_sr=target_sr)
    if len(mono) > max_len:
        mono = mono[:max_len]
    return mono.astype(np.float32), target_sr


def save_wav(path: str | Path, samples: np.ndarray, sample_rate: int) -> Path:
    sf.write(str(path), samples, sample_rate, subtype="PCM_16")
    return Path(path)


def estimate_wav_duration(path: str | Path) -> float:
    info = sf.info(str(path))
    return info.frames / info.samplerate
