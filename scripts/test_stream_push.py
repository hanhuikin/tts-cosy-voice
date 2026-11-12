"""流式播放 / 直播推流 专项用例。

覆盖:
  STREAM-01  流式首包为 44B WAV 头, 采样率/声道/位深正确
  STREAM-02  分块增量到达 (至少 3 块, 首块远早于末块)
  STREAM-03  流式 PCM 字节数与采样率推算时长一致
  STREAM-04  客户端提前断开 -> 任务不残留 running, 队列恢复
  STREAM-05  播客流式: 分块下发 + 完成后成品落盘且时长一致
  STREAM-06  播客流式中断 -> 剧集 failed, 之后可重新合成成功
  PUSH-01    HLS 推流全流程 + m3u8/ts 分片可访问
  PUSH-02    推流中冲突: 再次推流 409, 合成 409
  PUSH-03    停止推流 -> stopped, 可重新开始
  PUSH-04    RTMP 地址不可达 -> failed 且快速结束, 不卡死
  PUSH-05    已有成品时推流走 file 模式
  PUSH-06    协议/地址/剧集参数校验

用法: .venv/Scripts/python.exe scripts/test_stream_push.py [--base URL]
"""
from __future__ import annotations

import argparse
import io
import struct
import sys
import time
import wave

import requests

BASE = "http://127.0.0.1:8001"
RESULTS: list[tuple[str, str, str, str]] = []
CASES: list[tuple[str, str, object]] = []
EPISODES: list[str] = []
VOICE = ""
CUR_MODEL = "cosyvoice3"


# ---------------------------------------------------------------- helpers
def api(path: str, method: str = "GET", **kw) -> requests.Response:
    return requests.request(method, BASE + path, timeout=kw.pop("timeout", 180), **kw)


def wav_header_info(buf: bytes) -> dict:
    assert buf[:4] == b"RIFF" and buf[8:12] == b"WAVE", f"非 WAV: {buf[:12]!r}"
    rate, = struct.unpack("<I", buf[24:28])
    ch, = struct.unpack("<H", buf[22:24])
    bits, = struct.unpack("<H", buf[34:36])
    return {"rate": rate, "ch": ch, "bits": bits}


def wav_duration(data: bytes) -> float:
    with wave.open(io.BytesIO(data)) as w:
        return w.getnframes() / float(w.getframerate())


def pick_voice() -> str:
    voices = api("/api/voices").json()
    v = next((x for x in voices if x.get("prompt_text")), None)
    assert v, "没有可用音色(需 prompt_text)"
    return v["id"]


def ensure_ready() -> str:
    h = api("/api/health").json()
    assert h["state"] == "ready", f"服务未就绪: {h['state']} {h.get('error')}"
    return h["model"]


def create_episode(title: str, lines: list[dict]) -> str:
    body = {"title": title, "source_type": "text", "source_ref": "",
            "lines": lines, "voice_host": VOICE, "voice_guest": VOICE}
    r = api("/api/podcast/episodes", "POST", json=body)
    assert r.ok, f"创建剧集失败 {r.status_code} {r.text[:120]}"
    pid = r.json()["id"]
    EPISODES.append(pid)
    return pid


def wait_episode(pid: str, done=("completed", "failed", "stopped"), timeout=180) -> dict:
    st = {}
    t0 = time.time()
    while time.time() - t0 < timeout:
        st = api(f"/api/podcast/episodes/{pid}/status").json()
        if st["status"] in done:
            return st
        time.sleep(1.5)
    return st


def wait_push(done=("completed", "failed", "stopped"), timeout=180) -> dict:
    st = {}
    t0 = time.time()
    while time.time() - t0 < timeout:
        st = api("/api/podcast/push/status").json()
        if st.get("status") in done:
            return st
        time.sleep(1.5)
    return st


def stream_task(task_text: str) -> dict | None:
    tasks = api("/api/tasks?limit=30").json()["tasks"]
    return next((t for t in tasks if (t.get("text") or "").startswith(task_text)), None)


def case(cid: str, name: str):
    def deco(fn):
        CASES.append((cid, name, fn))
        return fn
    return deco


def read_stream(text: str, max_seconds: float = 40, stop_after_chunks: int | None = None):
    """读取 /api/tts 流式响应; 返回 (到达时刻[], 分块大小[], 首块字节, 是否提前关闭)。"""
    body = {"text": text, "model": CUR_MODEL, "mode": "zero_shot",
            "voice_id": VOICE, "stream": True, "speed": 1.0}
    t0 = time.time()
    times, sizes, first, closed = [], [], b"", False
    with api("/api/tts", "POST", json=body, stream=True, timeout=300) as r:
        assert r.status_code == 200, f"HTTP {r.status_code}: {r.text[:120]}"
        for chunk in r.iter_content(16384):
            if not first:
                first = chunk[:44]
            times.append(time.time() - t0)
            sizes.append(len(chunk))
            if stop_after_chunks and len(sizes) >= stop_after_chunks:
                closed = True
                break          # 关闭连接, 模拟播放器中途退出
            if time.time() - t0 > max_seconds:
                closed = True
                break
    return times, sizes, first, closed


def make_lines(n: int = 3) -> list[dict]:
    base = [("host", "欢迎收听本期流式与推流专项测试。"),
            ("guest", "我们逐句验证音频分块与推流状态机。"),
            ("host", "最后一句用来确认成品完整落盘。")]
    return [{"role": role, "text": text, "speed": 1.0} for role, text in base[:n]]


# ---------------------------------------------------------------- stream
@case("STREAM-01", "流式首包为 44B WAV 头且字段正确")
def _():
    times, sizes, first, _ = read_stream("首包结构测试，检查 WAV 头字段是否完整。")
    assert sizes[0] == 44, f"首块应为 44B, 实际 {sizes[0]}B"
    h = wav_header_info(first)
    assert h["rate"] >= 16000 and h["ch"] == 1 and h["bits"] == 16, h
    return f"rate={h['rate']} ch={h['ch']} bits={h['bits']}"


@case("STREAM-02", "分块增量到达 (≥3 块, 首块远早于末块)")
def _():
    times, sizes, _, _ = read_stream("第一句用来观察分块节奏，这一句稍长一些，好让分块间隔明显。第二句继续。")
    assert len(sizes) >= 3, f"分块过少: {len(sizes)}"
    spread = times[-1] - times[0]
    assert spread > 0.25, f"分块几乎同时到达, 疑似整段下发: spread={spread:.2f}s"
    assert times[0] < times[-1] * 0.75, f"首块太晚: first={times[0]:.2f}s last={times[-1]:.2f}s"
    return f"{len(sizes)} 块, first={times[0]*1000:.0f}ms, spread={spread:.2f}s"


@case("STREAM-03", "流式 PCM 字节与采样率推算时长一致")
def _():
    _, sizes, _, _ = read_stream("字节一致性测试，时长由 PCM 字节数推算。")
    pcm = sum(sizes) - 44
    dur = pcm / 2 / 24000
    assert pcm % 2 == 0, f"PCM 字节非偶数: {pcm}"
    assert dur > 0.5, f"时长过短 {dur:.2f}s"
    return f"{pcm}B -> {dur:.1f}s"


@case("STREAM-04", "客户端提前断开: 任务不残留 running")
def _():
    marker = "断开测试标记，播放器只读两块就退出。"
    read_stream(marker, stop_after_chunks=2)
    t = None
    for _ in range(24):
        time.sleep(0.5)
        t = stream_task(marker)
        if t and t["status"] != "running":
            break
    assert t, "未找到该任务"
    assert t["status"] != "running", f"客户端断开后任务仍在 running: {t}"
    h = api("/api/health").json()
    assert h["queue_len"] == 0, f"队列未恢复: {h['queue_len']}"
    r = api("/api/tts", "POST", json={"text": "断开后的恢复验证。", "model": CUR_MODEL,
                                      "mode": "zero_shot", "voice_id": VOICE})
    assert r.ok, f"断开后服务不可用: {r.status_code} {r.text[:120]}"
    return f"task={t['status']}, 后续合成正常"


@case("STREAM-05", "播客流式: 分块下发 + 成品落盘")
def _():
    pid = create_episode("流式专项-播客", make_lines())
    t0 = time.time()
    times, sizes = [], []
    with api(f"/api/podcast/episodes/{pid}/synthesize", "POST",
             json={"mode": "stream"}, stream=True, timeout=300) as r:
        assert r.status_code == 200, r.text[:120]
        for chunk in r.iter_content(16384):
            times.append(time.time() - t0)
            sizes.append(len(chunk))
    assert len(sizes) >= 3, f"分块过少: {len(sizes)}"
    assert sizes[0] == 44, f"首块应为 WAV 头, 实际 {sizes[0]}B"
    st = wait_episode(pid)
    assert st["status"] == "completed", f"剧集状态 {st}"
    audio = api(f"/api/podcast/episodes/{pid}/audio")
    assert audio.ok, "成品未落盘"
    dur = wav_duration(audio.content)
    stream_dur = (sum(sizes) - 44) / 2 / 24000
    assert abs(dur - stream_dur) < 0.5, f"成品 {dur:.1f}s 与流式 {stream_dur:.1f}s 不一致"
    return f"{len(sizes)} 块, 成品 {dur:.1f}s"


@case("STREAM-06", "播客流式中断 -> failed, 之后可重新合成")
def _():
    pid = create_episode("流式专项-中断", make_lines())
    with api(f"/api/podcast/episodes/{pid}/synthesize", "POST",
             json={"mode": "stream"}, stream=True, timeout=120) as r:
        next(r.iter_content(2048))   # 读一点就断开
    st = wait_episode(pid)
    assert st["status"] == "failed", f"中断后状态应为 failed, 实际 {st}"
    r = api(f"/api/podcast/episodes/{pid}/synthesize", "POST", json={"mode": "final"})
    assert r.ok, r.text[:120]
    st = wait_episode(pid)
    assert st["status"] == "completed", f"重新合成未完成: {st}"
    return f"中断后 failed, 重新合成 completed"


# ---------------------------------------------------------------- push
@case("PUSH-01", "HLS 推流全流程 + 分片可访问")
def _():
    pid = create_episode("推流专项-HLS", make_lines())
    r = api(f"/api/podcast/episodes/{pid}/push", "POST", json={"protocol": "hls"})
    assert r.ok, f"{r.status_code} {r.text[:120]}"
    st = r.json()
    assert st["status"] in ("preparing", "pushing"), st
    assert st["ffmpeg_available"] is True, "ffmpeg 不可用"
    st = wait_push(timeout=240)
    assert st["status"] == "completed", f"推流状态 {st}"
    m3u8 = api(f"/live/{pid}.m3u8")
    assert m3u8.ok and "#EXTM3U" in m3u8.text, "m3u8 未生成"
    segs = [ln.strip() for ln in m3u8.text.splitlines() if ln.strip().endswith(".ts")]
    assert segs, "m3u8 中没有 .ts 分片"
    seg = api(f"/live/{segs[0]}")
    assert seg.ok and len(seg.content) > 1000, f"分片不可用 {segs[0]}"
    return f"completed, {len(segs)} 片, 首片 {len(seg.content)//1024}KB"


@case("PUSH-02", "推流中冲突: 再推流 409 / 合成 409")
def _():
    pid = create_episode("推流专项-冲突", make_lines())
    r = api(f"/api/podcast/episodes/{pid}/push", "POST", json={"protocol": "hls"})
    assert r.ok, r.text[:120]
    try:
        r2 = api(f"/api/podcast/episodes/{pid}/push", "POST", json={"protocol": "hls"})
        assert r2.status_code == 409, f"重复推流应 409, 实际 {r2.status_code}"
        r3 = api(f"/api/podcast/episodes/{pid}/synthesize", "POST", json={"mode": "final"})
        assert r3.status_code == 409, f"推流中合成应 409, 实际 {r3.status_code}"
    finally:
        api(f"/api/podcast/episodes/{pid}/push", "DELETE")
        wait_push(timeout=60)
    return "409 冲突保护生效"


@case("PUSH-03", "停止推流 -> stopped, 可重新开始")
def _():
    pid = create_episode("推流专项-停止", make_lines())
    r = api(f"/api/podcast/episodes/{pid}/push", "POST", json={"protocol": "hls"})
    assert r.ok, r.text[:120]
    time.sleep(1.5)
    d = api(f"/api/podcast/episodes/{pid}/push", "DELETE")
    assert d.ok, f"停止失败 {d.status_code}"
    st = wait_push(timeout=90)
    assert st["status"] in ("stopped", "completed"), f"停止后状态 {st}"
    r2 = api(f"/api/podcast/episodes/{pid}/push", "POST", json={"protocol": "hls"})
    assert r2.ok, f"重新推流失败 {r2.status_code} {r2.text[:120]}"
    d2 = api(f"/api/podcast/episodes/{pid}/push", "DELETE")
    assert d2.ok
    wait_push(timeout=90)
    return f"停止={st['status']}, 重新开始成功"


@case("PUSH-04", "RTMP 不可达 -> failed 且快速结束")
def _():
    pid = create_episode("推流专项-RTMP", make_lines(1))
    t0 = time.time()
    r = api(f"/api/podcast/episodes/{pid}/push", "POST",
            json={"protocol": "rtmp", "url": "rtmp://127.0.0.1:1/live/x"})
    assert r.ok, f"{r.status_code} {r.text[:120]}"
    st = wait_push(timeout=60)
    cost = time.time() - t0
    assert st["status"] == "failed", \
        f"不可达地址应 failed, 实际 {st.get('status')} {(st.get('stderr_tail') or '')[:120]}"
    assert cost < 60, f"失败耗时过长 {cost:.0f}s"
    return f"failed in {cost:.0f}s"


@case("PUSH-05", "已有成品时推流走 file 模式")
def _():
    pid = None
    for p in EPISODES:
        ep = api(f"/api/podcast/episodes/{p}").json()
        if ep.get("status") == "completed" and ep.get("duration"):
            pid = p
            break
    assert pid, "没有已完成剧集可用于 file 模式测试"
    r = api(f"/api/podcast/episodes/{pid}/push", "POST", json={"protocol": "hls"})
    assert r.ok, r.text[:120]
    assert r.json()["mode"] == "file", f"应走 file 模式, 实际 {r.json()['mode']}"
    st = wait_push(timeout=120)
    assert st["status"] == "completed", st
    return f"mode=file, duration={st.get('duration_s')}"


@case("PUSH-06", "协议/地址/剧集参数校验")
def _():
    pid = create_episode("推流专项-校验", make_lines(1))
    assert api(f"/api/podcast/episodes/{pid}/push", "POST",
               json={"protocol": "brotli"}).status_code == 422
    assert api(f"/api/podcast/episodes/{pid}/push", "POST",
               json={"protocol": "rtmp", "url": ""}).status_code == 400
    assert api("/api/podcast/episodes/not-exist/push", "POST",
               json={"protocol": "hls"}).status_code == 404
    assert api("/api/podcast/episodes/not-exist/push", "DELETE").status_code == 404
    return "422/400/404 全部正确"


# ---------------------------------------------------------------- main
def main() -> int:
    global BASE, VOICE, CUR_MODEL
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default=BASE)
    ap.add_argument("--only", default="", help="只跑指定前缀的用例, 如 STREAM-04 或 PUSH")
    args = ap.parse_args()
    BASE = args.base

    print("=" * 62)
    print("流式播放 / 直播推流 专项测试")
    print("=" * 62)
    try:
        CUR_MODEL = ensure_ready()
        VOICE = pick_voice()
        print(f"[setup] model={CUR_MODEL} voice={VOICE[:8]}…")
    except Exception as e:  # noqa: BLE001
        print(f"[setup] 失败: {e}")
        return 2

    for cid, name, fn in CASES:
        if args.only and not cid.startswith(args.only):
            continue
        t0 = time.time()
        try:
            detail = fn() or ""
            RESULTS.append((cid, name, "PASS", detail))
            print(f"  [{cid}] ✓ {name}  ({time.time()-t0:.1f}s) {detail}")
        except Exception as e:  # noqa: BLE001
            RESULTS.append((cid, name, "FAIL", str(e)))
            print(f"  [{cid}] ✗ {name}  ({time.time()-t0:.1f}s) {e}")

    for pid in EPISODES:
        try:
            api(f"/api/podcast/episodes/{pid}", "DELETE")
        except Exception:
            pass
    print(f"[cleanup] 删除 {len(EPISODES)} 个测试剧集")

    print("\n" + "=" * 62)
    passed = sum(1 for r in RESULTS if r[2] == "PASS")
    failed = len(RESULTS) - passed
    print(f"结果: {passed} PASS / {failed} FAIL  (共 {len(RESULTS)} 例)")
    for cid, name, status, detail in RESULTS:
        if status != "PASS":
            print(f"  [{cid}] {name} — {detail}")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
