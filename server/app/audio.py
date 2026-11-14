"""音频工具: WAV 头打包、PCM 块拼接、参考音频重采样校验、背景音乐混流。"""
from __future__ import annotations

import io
import logging
import struct
import wave
from pathlib import Path

import numpy as np
import soundfile as sf

logger = logging.getLogger("cosyvoice-server.audio")


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


# ---------- 背景音乐混流 ----------

_BGM_CACHE: dict[str, tuple[float, bytes]] = {}   # path -> (mtime, s16le mono PCM)


def decode_bgm(path: str | Path, sr: int = 24000) -> bytes:
    """把 BGM 音频解码为 s16le 单声道 PCM (重采样到 sr)。带 mtime 缓存; 失败返回空 bytes。"""
    p = Path(path)
    try:
        mtime = p.stat().st_mtime
    except OSError:
        return b""
    cached = _BGM_CACHE.get(str(p))
    if cached and cached[0] == mtime:
        return cached[1]
    pcm = b""
    try:
        data, file_sr = sf.read(str(p), dtype="float32", always_2d=True)
        mono = data.mean(axis=1)
        if file_sr != sr:
            import librosa

            mono = librosa.resample(mono, orig_sr=file_sr, target_sr=sr)
        pcm = np.clip(mono * 32767.0, -32768.0, 32767.0).astype(np.int16).tobytes()
    except Exception:
        pcm = b""   # libsndfile 不支持 (如部分 mp3) → ffmpeg 兜底
    if not pcm:
        try:
            from .podcast import find_ffmpeg

            ffmpeg = find_ffmpeg()
            if ffmpeg:
                import subprocess

                r = subprocess.run(
                    [ffmpeg, "-hide_banner", "-loglevel", "error", "-y", "-i", str(p),
                     "-f", "s16le", "-ac", "1", "-ar", str(sr), "-"],
                    capture_output=True, timeout=120)
                if r.returncode == 0 and r.stdout:
                    pcm = r.stdout
        except Exception as e:
            logger.warning("BGM 解码失败 %s: %s", p.name, e)
            pcm = b""
    if pcm:
        _BGM_CACHE[str(p)] = (mtime, pcm)
    return pcm


class BgmMixer:
    """把背景音乐混入语音 PCM 流 (24k 单声道 s16)。

    - 循环: BGM 比内容短时, 先把尾部与开头做 2s 交叉淡化拼成无缝循环体, 再按采样偏移平铺,
      因此循环接缝无爆音; 输出流第 k 个采样对应循环体第 (k % len) 采样, 分块边界无需对齐。
    - 自动闪避 (duck): 按语音 RMS 包络实时压低 BGM —— 说话时降到 base×duck_min,
      停顿时回升到 base, 快起慢收, 避免遮挡主讲; duck=False 时用固定音量。
    - mix(): 流式/推流逐块调用 (跨块保持包络与循环位置); mix_full(): 整段混音,
      额外做 0.8s 淡入 / 2.5s 淡出, 成品收尾更自然。
    """

    DUCK_MIN = 0.35      # 说话时 BGM 降到基础音量的比例
    RMS_LO = 0.012       # 低于此 RMS 视为停顿 (线性满幅=1)
    RMS_HI = 0.05        # 高于此 RMS 视为正常说话

    def __init__(self, path: str | Path, volume: float = 0.18, sr: int = 24000, duck: bool = True):
        self.sr = sr
        self.base = max(0.0, min(1.0, float(volume)))
        self.duck = duck
        raw = np.frombuffer(decode_bgm(path, sr), dtype=np.int16) if path else np.zeros(0, np.int16)
        self.loop = self._make_loop(raw, sr)
        self.pos = 0
        self._gain = self.base   # 逐块混音时携带的当前增益 (快起慢收状态)

    @property
    def ok(self) -> bool:
        return self.loop.size > 0 and self.base > 0

    @staticmethod
    def _make_loop(raw: np.ndarray, sr: int) -> np.ndarray:
        """尾部与开头交叉淡化, 消除循环接缝。"""
        n = raw.size
        if n < int(0.5 * sr):
            return raw
        fade = min(int(2.0 * sr), n // 4)
        body = raw[:n - fade].astype(np.float32).copy()
        tail = raw[n - fade:].astype(np.float32)
        head = raw[:fade].astype(np.float32)
        ramp = np.linspace(0.0, 1.0, fade, dtype=np.float32)
        body[-fade:] = tail * (1.0 - ramp) + head * ramp
        return np.clip(body, -32767.0, 32767.0).astype(np.int16)

    def _duck_gain_full(self, speech: np.ndarray) -> np.ndarray:
        """整段: 由语音 RMS 包络推导逐样本 BGM 增益曲线 (0..1 相对 base)。"""
        n = speech.size
        win = max(1, int(0.05 * self.sr))
        p = np.concatenate(([0.0], np.cumsum(speech.astype(np.float32) ** 2)))
        rms = np.zeros(n, dtype=np.float32)
        if n >= win:
            rms[win // 2:win // 2 + n - win + 1] = np.sqrt(
                np.maximum(0.0, (p[win:] - p[:-win]) / win))
        x = np.clip((rms - self.RMS_LO) / (self.RMS_HI - self.RMS_LO), 0.0, 1.0)
        target = self.DUCK_MIN + (1.0 - self.DUCK_MIN) * (1.0 - x)   # 说话→DUCK_MIN
        # ~120ms 滑动平均平滑增益曲线, 避免抽吸感
        sw = max(1, int(0.12 * self.sr))
        c = np.concatenate(([0.0], np.cumsum(target)))
        g = target.copy()
        if n >= sw:
            g[sw // 2:sw // 2 + n - sw + 1] = (c[sw:] - c[:-sw]) / sw
        return np.clip(g, self.DUCK_MIN, 1.0)

    def mix_full(self, pcm: bytes) -> bytes:
        """整段混音: 循环平铺 + 自动闪避 + 首尾淡入淡出。"""
        if not self.ok or len(pcm) < 2:
            return pcm
        n = len(pcm) // 2
        speech = np.frombuffer(pcm, dtype=np.int16).astype(np.float32)
        reps = int(np.ceil(n / self.loop.size))
        bg = np.tile(self.loop.astype(np.float32), reps)[:n]
        if self.duck:
            bg *= self._duck_gain_full(speech) * self.base
        else:
            bg *= self.base
        fin = min(int(0.8 * self.sr), n)
        fout = min(int(2.5 * self.sr), n)
        if fin > 0:
            bg[:fin] *= np.linspace(0.0, 1.0, fin, dtype=np.float32)
        if fout > 0:
            bg[-fout:] *= np.linspace(1.0, 0.0, fout, dtype=np.float32)
        return np.clip(speech + bg, -32767.0, 32767.0).astype(np.int16).tobytes()

    def mix(self, pcm: bytes) -> bytes:
        """逐块混音 (流式/推流): 与 mix_full 同一套循环体; 闪避增益跨块一阶平滑。"""
        if not self.ok or len(pcm) < 2:
            return pcm
        n = len(pcm) // 2
        speech = np.frombuffer(pcm, dtype=np.int16).astype(np.float32)
        idx = (self.pos + np.arange(n)) % self.loop.size
        bg = self.loop[idx].astype(np.float32)
        self.pos = int((self.pos + n) % self.loop.size)
        if self.duck:
            rms = float(np.sqrt(np.mean(speech ** 2))) if n else 0.0
            x = min(1.0, max(0.0, (rms - self.RMS_LO) / (self.RMS_HI - self.RMS_LO)))
            target = (self.DUCK_MIN + (1.0 - self.DUCK_MIN) * (1.0 - x)) * self.base
            # 一阶平滑: 说话快压 (tau≈60ms), 停顿慢升 (tau≈500ms)
            tau = 0.06 if target < self._gain else 0.5
            alpha = 1.0 - pow(2.718281828, -(n / self.sr) / tau)
            self._gain += (target - self._gain) * alpha
            bg *= self._gain
        else:
            bg *= self.base
        return np.clip(speech + bg, -32767.0, 32767.0).astype(np.int16).tobytes()
