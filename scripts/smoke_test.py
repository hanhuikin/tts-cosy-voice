"""端到端冒烟测试: 音色上传 -> 非流式合成 -> 流式合成 -> OpenAI 兼容端点 -> 历史/删除。

用法: .venv/Scripts/python.exe scripts/smoke_test.py
"""
import io
import json
import struct
import sys
import time
import wave

import requests

BASE = "http://127.0.0.1:8000"
PROMPT_WAV = "CosyVoice/asset/zero_shot_prompt.wav"
PROMPT_TEXT = "希望你以后能够做的比我还好呦。"


def wav_duration(b: bytes) -> float:
    w = wave.open(io.BytesIO(b))
    return w.getnframes() / w.getframerate()


def main() -> int:
    ok = lambda label, extra="": print(f"[PASS] {label} {extra}")

    # 0. 清理旧测试数据
    for v in requests.get(f"{BASE}/api/voices").json():
        requests.delete(f"{BASE}/api/voices/{v['id']}")
    print("[clean] voices cleared")

    # 1. 健康检查 (等待模型就绪, 最长 5 分钟)
    for _ in range(150):
        h = requests.get(f"{BASE}/api/health").json()
        if h["state"] == "ready":
            break
        time.sleep(2)
    assert h["state"] == "ready", h
    ok("health", f"state={h['state']} model={h['model']} sr={h['sample_rate']}")

    # 2. 上传音色 (UTF-8 中文)
    with open(PROMPT_WAV, "rb") as f:
        r = requests.post(f"{BASE}/api/voices",
                          data={"name": "示例男声", "prompt_text": PROMPT_TEXT},
                          files={"file": ("prompt.wav", f, "audio/wav")})
    assert r.ok, r.text
    voice = r.json()
    assert voice["name"] == "示例男声", voice
    assert abs(voice["duration"] - 3.48) < 0.3, voice
    ok("voice upload", f"id={voice['id']} duration={voice['duration']}s")

    # 3. 参考音频试听
    r = requests.get(f"{BASE}/api/voices/{voice['id']}/prompt")
    assert r.ok and r.headers["content-type"] == "audio/wav"
    ok("voice prompt preview", f"{len(r.content)} bytes")

    # 4. 非流式 zero_shot
    t0 = time.time()
    r = requests.post(f"{BASE}/api/tts", json={
        "text": "今天天气真不错，我们一起去公园散步吧。",
        "model": "cosyvoice3", "mode": "zero_shot", "voice_id": voice["id"],
        "stream": False,
    })
    assert r.ok, r.text
    dur = wav_duration(r.content)
    task_id = r.headers.get("X-Task-Id")
    ok("non-stream zero_shot", f"{len(r.content)//1024}KB audio={dur:.1f}s "
       f"infer={r.headers.get('X-Infer-Ms')}ms total={time.time()-t0:.1f}s task={task_id}")

    # 5. 流式 zero_shot (观测首包)
    t0 = time.time()
    first, total, chunks = None, 0, 0
    with requests.post(f"{BASE}/api/tts", stream=True, json={
        "text": "春眠不觉晓，处处闻啼鸟。夜来风雨声，花落知多少。",
        "model": "cosyvoice3", "mode": "zero_shot", "voice_id": voice["id"],
        "stream": True,
    }) as r:
        assert r.ok, r.text
        for chunk in r.iter_content(8192):
            if first is None:
                first = time.time() - t0
            total += len(chunk)
            chunks += 1
    audio_sec = (total - 44) / 2 / 24000
    ok("stream zero_shot", f"first-byte={first*1000:.0f}ms chunks={chunks} audio={audio_sec:.1f}s")

    # 6. instruct 模式 (方言指令)
    r = requests.post(f"{BASE}/api/tts", json={
        "text": "你吃饭了没有？",
        "model": "cosyvoice3", "mode": "instruct", "voice_id": voice["id"],
        "instruct_text": "请用四川话说这句话。", "stream": False,
    })
    assert r.ok, r.text
    ok("instruct (sichuan)", f"audio={wav_duration(r.content):.1f}s")

    # 7. cross_lingual (英文)
    r = requests.post(f"{BASE}/api/tts", json={
        "text": "<|en|>Hello, this is CosyVoice three speaking.",
        "model": "cosyvoice3", "mode": "cross_lingual", "voice_id": voice["id"],
        "stream": False,
    })
    assert r.ok, r.text
    ok("cross_lingual (en)", f"audio={wav_duration(r.content):.1f}s")

    # 8. speed 参数 (非流式)
    r = requests.post(f"{BASE}/api/tts", json={
        "text": "语速测试，这段话会说得快一些。",
        "model": "cosyvoice3", "mode": "zero_shot", "voice_id": voice["id"],
        "speed": 1.5, "stream": False,
    })
    assert r.ok, r.text
    ok("speed=1.5", f"audio={wav_duration(r.content):.1f}s")

    # 9. 流式 + speed != 1 应被拒绝
    r = requests.post(f"{BASE}/api/tts", json={
        "text": "x", "model": "cosyvoice3", "mode": "zero_shot",
        "voice_id": voice["id"], "speed": 1.5, "stream": True,
    })
    assert r.status_code == 400, r.text
    ok("stream+speed rejection", f"400 as designed")

    # 10. OpenAI 兼容端点
    r = requests.post(f"{BASE}/v1/audio/speech", json={
        "input": "This is the OpenAI compatible endpoint.",
        "voice": f"voice:{voice['id']}", "model": "cosyvoice3",
    })
    assert r.ok and r.headers["content-type"] == "audio/wav", r.text
    ok("openai compat", f"{wav_duration(r.content):.1f}s")

    # 11. 任务历史
    tasks = requests.get(f"{BASE}/api/tasks").json()["tasks"]
    assert len(tasks) >= 5
    tid = tasks[0]["id"]
    r = requests.get(f"{BASE}/api/tasks/{tid}/audio")
    assert r.ok and len(r.content) > 1000
    ok("task history", f"{len(tasks)} tasks, first audio {len(r.content)//1024}KB")

    # 12. 错误路径: 不存在 voice_id
    r = requests.post(f"{BASE}/api/tts", json={
        "text": "x", "model": "cosyvoice3", "mode": "zero_shot", "voice_id": "nonexistent",
    })
    assert r.status_code == 404, r.text
    ok("error path", "404 for missing voice")

    print("\n=== ALL SMOKE TESTS PASSED ===")
    return 0


if __name__ == "__main__":
    sys.exit(main())
