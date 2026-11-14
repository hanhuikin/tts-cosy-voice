#!/usr/bin/env python
"""生成预置背景音乐 (免版权, 本地合成) → server/data/bgm/*.wav

三首 24s 循环乐, 24kHz 单声道 16bit: 柔和正弦和弦垫 + 轻微 detune 合唱,
按乐句 (6s/和弦) 首尾余弦淡入淡出 —— 循环接缝落在幅度为零的气口上, 无爆音。
用法: .venv/Scripts/python.exe scripts/gen_preset_bgm.py
"""
from __future__ import annotations

import numpy as np
import soundfile as sf
from pathlib import Path

SR = 24000
CHORD_DUR = 6.0      # 每个和弦 (乐句) 时长
FADE = 0.5           # 乐句首尾余弦淡入淡出 → 循环接缝在零幅度气口
PEAK = 0.42          # 归一化峰值

NOTE = {"C": 261.63, "D": 293.66, "E": 329.63, "F": 349.23,
        "G": 392.00, "A": 440.00, "Bb": 466.16, "B": 493.88}


def freq(n: str, oct: int = 0) -> float:
    return NOTE[n] * (2 ** oct)


# 三首曲子: (和弦序列[每项为频率列表], 是否加琶音, 琶音八度)
TRACKS = [
    ("晨光漫步.wav", False, [
        [freq("C", -1), freq("E"), freq("G")],
        [freq("G", -1), freq("B"), freq("D", 1)],
        [freq("A", -1), freq("C"), freq("E")],
        [freq("F", -1), freq("A"), freq("C", 1)],
    ], 0),
    ("雨夜灯下.wav", True, [
        [freq("D", -1), freq("F"), freq("A")],
        [freq("Bb", -1), freq("D"), freq("F")],
        [freq("F", -1), freq("A"), freq("C", 1)],
        [freq("C", -1), freq("E"), freq("G")],
    ], -1),
    ("山间清晨.wav", True, [
        [freq("F", -1), freq("A"), freq("C", 1)],
        [freq("C", -1), freq("E"), freq("G")],
        [freq("G", -1), freq("B"), freq("D", 1)],
        [freq("A", -1), freq("C"), freq("E")],
    ], 1),
]


def pad_chord(freqs: list[float], dur: float, rng: np.random.Generator) -> np.ndarray:
    """柔和和弦垫: 每音 = 基频 + 2/3 次谐波, 双路 detune 合唱; 首尾余弦淡入淡出。"""
    t = np.arange(int(dur * SR)) / SR
    out = np.zeros_like(t)
    for f in freqs:
        for mul, amp in ((1, 1.0), (2, 0.30), (3, 0.10)):
            for det in (1.0 - 0.0012, 1.0 + 0.0012):
                out += amp * np.sin(2 * np.pi * f * mul * det * t + rng.uniform(0, 6.28))
    out /= (len(freqs) * 2.9)
    n = len(t)
    fade = int(FADE * SR)
    env = np.ones(n, dtype=np.float32)
    env[:fade] = 0.5 - 0.5 * np.cos(np.pi * np.arange(fade) / fade)
    env[-fade:] = 0.5 + 0.5 * np.cos(np.pi * np.arange(fade) / fade)
    return out * env


def pluck(f: float, dur: float) -> np.ndarray:
    """琶音点缀: 正弦 + 指数衰减。"""
    t = np.arange(int(dur * SR)) / SR
    return 0.5 * np.sin(2 * np.pi * f * t) * np.exp(-t * 5.0)


def gen_track(chords: list[list[float]], arpeggio: bool, arp_oct: int) -> np.ndarray:
    rng = np.random.default_rng(42)
    step = int(CHORD_DUR * SR)
    buf = np.zeros(step * len(chords), dtype=np.float32)
    pent = [0, 2, 4, 7, 9]   # 大调五声 (半音数), 用于琶音选音
    for ci, ch in enumerate(chords):
        seg = pad_chord(ch, CHORD_DUR, rng)
        if arpeggio:
            # 每小节按 0.75s 触发琶音音符 (和弦音上五声音阶), 幅度 0.35
            root = min(ch)
            for k in range(8):
                semi = pent[int(rng.integers(0, len(pent)))]
                f = root * (2 ** (semi / 12)) * (2 ** arp_oct)
                note = pluck(f, 0.7) * 0.35
                s = int(k * 0.75 * SR)
                e = min(s + len(note), len(seg))
                if s < len(seg):
                    seg[s:e] += note[:e - s]
        buf[ci * step:(ci + 1) * step] += seg
    peak = float(np.abs(buf).max()) or 1.0
    return (buf / peak * PEAK).astype(np.float32)


def main() -> None:
    out_dir = Path(__file__).resolve().parents[1] / "server" / "data" / "bgm"
    out_dir.mkdir(parents=True, exist_ok=True)
    for name, arpeggio, chords, arp_oct in TRACKS:
        wav = gen_track(chords, arpeggio, arp_oct)
        out = out_dir / name
        sf.write(str(out), wav, SR, subtype="PCM_16")
        print(f"[bgm] {out.name}: {len(wav) / SR:.1f}s, {out.stat().st_size // 1024} KB")


if __name__ == "__main__":
    main()
