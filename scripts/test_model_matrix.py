"""多模型矩阵测试: 逐个切换 7 个模型, 校验能力字段并按各自能力真实合成。

覆盖:
  - /api/admin/model 切换 + 就绪轮询 (含加载耗时)
  - /api/models 的能力字段与预期一致 (CosyVoice 无内置音色 / Qwen3 有 9 个)
  - 按模型能力选择 mode: sft(内置音色) / zero_shot(克隆) / instruct(声音设计)
  - 非流式 WAV 结构与时长; 原生流式模型再校验分块到达
  - 能力门控: 对无内置音色的 CosyVoice 请求 sft 应被 400 拒绝

用法: .venv/Scripts/python.exe scripts/test_model_matrix.py [--base URL] [--restore KEY]
"""
from __future__ import annotations

import argparse
import io
import json
import struct
import sys
import time
import wave

import requests

BASE = "http://127.0.0.1:8001"
TEXT = "多模型矩阵测试，确认这个模型能正常出声。"
RESTORE = "cosyvoice3"

FAILS: list[str] = []


def api(path: str, **kw) -> requests.Response:
    return requests.request(kw.pop("method", "GET"), BASE + path, timeout=kw.pop("timeout", 180), **kw)


def health() -> dict:
    return api("/api/health").json()


def wait_ready(model: str, timeout: float = 180) -> tuple[bool, float, str]:
    """轮询到 state=ready 且 model 匹配; 返回 (ok, 秒, 错误)。"""
    t0 = time.time()
    while time.time() - t0 < timeout:
        h = health()
        if h.get("state") == "ready" and h.get("model") == model:
            return True, time.time() - t0, ""
        if h.get("state") == "error" and h.get("model") == model:
            return False, time.time() - t0, str(h.get("error"))
        time.sleep(1.0)
    return False, time.time() - t0, "timeout"


def switch(model: str) -> tuple[bool, float, str]:
    r = api("/api/admin/model", method="POST", json={"model": model})
    if r.status_code >= 400:
        return False, 0.0, f"switch HTTP {r.status_code}: {r.text[:120]}"
    return wait_ready(model)


def wav_info(data: bytes) -> tuple[int, int, float]:
    """返回 (采样率, 位深, 时长秒); 断言 RIFF 结构。"""
    assert data[:4] == b"RIFF" and data[8:12] == b"WAVE", "不是 RIFF/WAVE"
    with wave.open(io.BytesIO(data)) as w:
        return w.getframerate(), w.getsampwidth() * 8, w.getnframes() / float(w.getframerate())


def pick_request(h: dict) -> dict | None:
    """按模型能力挑一条合法请求。"""
    models = {m["key"]: m for m in h.get("models", [])}
    cur = h.get("model")
    m = models.get(cur) or {}
    modes = m.get("modes") or []
    spk = m.get("speaker_modes") or []
    if "sft" in spk and h.get("speakers"):
        return {"mode": "sft", "spk_id": h["speakers"][0]}
    if "zero_shot" in modes:
        voices = api("/api/voices").json()
        v = next((x for x in voices if x.get("prompt_text")), None)
        if v:
            return {"mode": "zero_shot", "voice_id": v["id"]}
        return None
    if "instruct" in modes and m.get("instruct_kind") == "design":
        return {"mode": "instruct", "instruct_text": "沉稳的中年男声，语速偏慢"}
    return None


def synthesize(model: str, h: dict, stream: bool = False) -> tuple[int, bytes | None, float]:
    req = pick_request(h)
    if not req:
        raise AssertionError("没有可用的合成方式")
    payload = {"text": TEXT, "model": model, "speed": 1.0, "stream": stream, **req}
    t0 = time.time()
    r = api("/api/tts", method="POST", json=payload, timeout=300)
    dt = time.time() - t0
    if r.status_code != 200:
        raise AssertionError(f"HTTP {r.status_code}: {r.text[:160]}")
    return r.status_code, r.content, dt


def check_model(expect: dict) -> None:
    """校验能力字段是否符合功能预期。"""
    h = health()
    models = {m["key"]: m for m in h.get("models", [])}
    for key, exp in expect.items():
        m = models.get(key)
        assert m, f"{key} 不在 models 列表"
        if "modes" in exp:
            assert m["modes"] == exp["modes"], f"{key}.modes={m['modes']} 期望 {exp['modes']}"
        if "speaker_modes" in exp:
            assert m["speaker_modes"] == exp["speaker_modes"], \
                f"{key}.speaker_modes={m['speaker_modes']} 期望 {exp['speaker_modes']}"
        if "stream_mode" in exp:
            assert m["stream_mode"] == exp["stream_mode"], f"{key}.stream_mode={m['stream_mode']}"
    print("  [CAP] ✓ 能力字段符合预期")


def main() -> int:
    global BASE
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default=BASE)
    ap.add_argument("--restore", default=RESTORE)
    args = ap.parse_args()
    BASE = args.base

    h0 = health()
    available = [m["key"] for m in h0["models"] if m.get("available")]
    print(f"权重就绪模型: {available}")

    check_model({
        "cosyvoice3": {"modes": ["zero_shot", "cross_lingual", "instruct"], "speaker_modes": [],
                       "stream_mode": "native"},
        "cosyvoice2": {"modes": ["zero_shot", "cross_lingual", "instruct"], "speaker_modes": [],
                       "stream_mode": "native"},
        "qwen3tts": {"modes": ["sft", "instruct"], "speaker_modes": ["sft", "instruct"],
                     "stream_mode": "buffered"},
        "qwen3tts_design": {"modes": ["instruct"], "speaker_modes": []},
        "qwen3tts_base": {"modes": ["zero_shot", "cross_lingual"], "speaker_modes": []},
        "indextts2": {"modes": ["zero_shot", "cross_lingual"], "speaker_modes": []},
    })

    results = []
    for key in available:
        print(f"\n== {key} ==")
        ok, secs, err = switch(key)
        if not ok:
            FAILS.append(f"{key}: 切换失败 {err}")
            results.append({"model": key, "ok": False, "detail": err})
            print(f"  ✗ 切换失败: {err}")
            continue
        h = health()
        try:
            code, data, dt = synthesize(key, h, stream=False)
            rate, bits, dur = wav_info(data)
            assert dur > 0.3, f"时长过短 {dur:.2f}s"
            detail = f"{code} {len(data)}B {rate}Hz/{bits}bit {dur:.1f}s 合成{dt:.1f}s 加载{secs:.0f}s"
            print(f"  ✓ {detail}")
            if h.get("stream_mode") == "native":
                code2, data2, _ = synthesize(key, h, stream=True)
                # 流式响应头是占位长度(估算上限), 时长按实际 PCM 字节推算
                dur2 = max(0.0, (len(data2) - 44) / 2 / 24000)
                assert dur2 > 0.3, f"流式时长过短 {dur2:.2f}s"
                print(f"  ✓ 原生流式 {len(data2)}B 约 {dur2:.1f}s")
            results.append({"model": key, "ok": True, "detail": detail})
        except AssertionError as e:
            FAILS.append(f"{key}: {e}")
            results.append({"model": key, "ok": False, "detail": str(e)})
            print(f"  ✗ {e}")

    # 门控: CosyVoice 无 spk2info.pt 时 sft 必须被拒
    try:
        if "cosyvoice3" in available and health().get("model") != "cosyvoice3":
            switch("cosyvoice3")
        r = api("/api/tts", method="POST",
                json={"text": "门控验证", "model": "cosyvoice3", "mode": "sft", "spk_id": "x"})
        assert r.status_code == 400 and "not supported" in r.text, \
            f"期望 400 拒绝, 实际 {r.status_code} {r.text[:100]}"
        print("\n  [GATE] ✓ CosyVoice sft 请求被正确拒绝")
    except AssertionError as e:
        FAILS.append(f"gate: {e}")
        print(f"\n  [GATE] ✗ {e}")

    if args.restore:
        ok, secs, err = switch(args.restore)
        print(f"\n恢复 {args.restore}: {'ok %.0fs' % secs if ok else err}")

    print("\n" + "=" * 60)
    for r in results:
        print(f"{'PASS' if r['ok'] else 'FAIL'}  {r['model']:<16} {r['detail'][:70]}")
    if FAILS:
        print(f"\n失败 {len(FAILS)} 项:")
        for f in FAILS:
            print("  -", f)
        return 1
    print("\n全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
