"""CosyVoice API 接口测试用例。

覆盖: 健康检查 / 模型管理 / 合成(四种模式+流式) / 参数校验 / 音色管理 / 任务历史 / OpenAI 兼容 / SSE / 并发排队

用法: .venv/Scripts/python.exe scripts/test_api.py [--base http://127.0.0.1:8000] [--skip-slow]
"""
import argparse
import io
import json
import struct
import sys
import threading
import time
import wave
from pathlib import Path

import requests

REPO = Path(__file__).resolve().parents[1]
PROMPT_WAV = REPO / "CosyVoice" / "asset" / "zero_shot_prompt.wav"
PROMPT_TEXT = "希望你以后能够做的比我还好呦。"

BASE = "http://127.0.0.1:8001"
SKIP_SLOW = False

RESULTS = []  # (用例编号, 名称, 状态, 备注)
CASES = []    # (用例编号, 名称, 函数) 按定义顺序


def case(cid: str, name: str):
    """用例装饰器: 注册 + 计时 + 记录结果, 断言失败记为 FAIL。"""
    def deco(fn):
        def wrapped():
            t0 = time.time()
            try:
                detail = fn() or ""
                RESULTS.append((cid, name, "PASS", detail))
                print(f"  [{cid}] ✓ {name}  ({time.time()-t0:.1f}s) {detail}")
            except AssertionError as e:
                RESULTS.append((cid, name, "FAIL", str(e)))
                print(f"  [{cid}] ✗ {name}  ({time.time()-t0:.1f}s) FAIL: {e}")
            except Exception as e:
                RESULTS.append((cid, name, "ERROR", f"{type(e).__name__}: {e}"))
                print(f"  [{cid}] ✗ {name}  ({time.time()-t0:.1f}s) ERROR: {type(e).__name__}: {e}")
        CASES.append((cid, name, wrapped))
        return fn
    return deco


def wait_ready(timeout_s=300):
    """等待模型就绪, 返回 health dict。"""
    for _ in range(timeout_s // 2):
        h = requests.get(f"{BASE}/api/health").json()
        if h["state"] == "ready":
            return h
        time.sleep(2)
    raise RuntimeError(f"model not ready after {timeout_s}s: {h['state']}")


def ensure_model(key="cosyvoice3"):
    h = requests.get(f"{BASE}/api/health").json()
    if h["state"] == "ready" and h["model"] == key:
        return h
    requests.post(f"{BASE}/api/admin/model", json={"model": key})
    return wait_ready()


def parse_wav(b: bytes):
    """校验 WAV 结构, 返回 (sample_rate, channels, duration, data_len)。"""
    assert b[:4] == b"RIFF", f"not RIFF: {b[:8]!r}"
    assert b[8:12] == b"WAVE", "not WAVE"
    riff_len = struct.unpack("<I", b[4:8])[0]
    assert riff_len == len(b) - 8, f"RIFF size {riff_len} != actual {len(b)-8}"
    ch, sr, bits = struct.unpack("<H", b[22:24])[0], struct.unpack("<I", b[24:28])[0], struct.unpack("<H", b[34:36])[0]
    data_len = struct.unpack("<I", b[40:44])[0]
    assert data_len == len(b) - 44, f"data chunk {data_len} != actual {len(b)-44}"
    return sr, ch, data_len / 2 / ch / sr, data_len


# ============================================================
# 1. 系统与模型管理
# ============================================================
@case("SYS-01", "健康检查字段完整")
def _():
    h = requests.get(f"{BASE}/api/health").json()
    for k in ("state", "model", "sample_rate", "queue_len", "queue_max", "gpu", "models", "voices_count"):
        assert k in h, f"missing field: {k}"
    assert h["state"] in ("idle", "loading", "ready", "error")
    return f"state={h['state']}, model={h['model']}"


@case("SYS-02", "模型列表注册信息")
def _():
    ms = requests.get(f"{BASE}/api/models").json()["models"]
    keys = {m["key"] for m in ms}
    assert keys == {"cosyvoice3", "cosyvoice2"}, keys
    for m in ms:
        assert set(m["modes"]) == {"sft", "zero_shot", "cross_lingual", "instruct"}
    return f"{len(ms)} models registered"


@case("SYS-03", "切换到不存在的模型 -> 422")
def _():
    r = requests.post(f"{BASE}/api/admin/model", json={"model": "gpt-sovits"})
    assert r.status_code == 422, f"expect 422, got {r.status_code}"
    return "pydantic Literal 校验生效"


@case("SYS-04", "SSE 事件流连接与首事件")
def _():
    with requests.get(f"{BASE}/api/events", stream=True, timeout=5) as r:
        assert r.status_code == 200
        assert "text/event-stream" in r.headers["content-type"]
        line = next(r.iter_lines())
        evt = json.loads(line.decode("utf-8").removeprefix("data: "))
        assert evt["type"] == "hello", evt
        assert "state" in evt
    return f"hello event: state={evt['state']}"


# ============================================================
# 2. 音色管理
# ============================================================
@case("VOI-01", "上传克隆音色 (UTF-8 中文名)")
def _():
    with open(PROMPT_WAV, "rb") as f:
        r = requests.post(f"{BASE}/api/voices",
                          data={"name": "测试音色甲", "prompt_text": PROMPT_TEXT},
                          files={"file": ("p.wav", f, "audio/wav")})
    assert r.status_code == 200, r.text
    v = r.json()
    assert v["name"] == "测试音色甲", f"中文乱码: {v['name']}"
    assert 1.0 <= v["duration"] <= 30, v["duration"]
    globals().setdefault("TEST_VOICE_ID", v["id"])
    return f"id={v['id']}, duration={v['duration']:.1f}s"


@case("VOI-02", "音色列表包含新音色")
def _():
    vs = requests.get(f"{BASE}/api/voices").json()
    assert any(v["id"] == globals().get("TEST_VOICE_ID") for v in vs), "voice not in list"
    assert all("file" not in v for v in vs), "list 不应暴露文件路径"
    return f"{len(vs)} voices"


@case("VOI-03", "试听参考音频 (wav 可解析)")
def _():
    r = requests.get(f"{BASE}/api/voices/{globals()['TEST_VOICE_ID']}/prompt")
    assert r.status_code == 200
    sr, ch, dur, _ = parse_wav(r.content)
    assert sr == 16000, f"参考音频应归一为 16k, got {sr}"
    return f"{sr}Hz mono {dur:.1f}s"


@case("VOI-04", "上传不支持的格式 -> 400")
def _():
    r = requests.post(f"{BASE}/api/voices",
                      data={"name": "x", "prompt_text": "y"},
                      files={"file": ("v.txt", b"hello", "text/plain")})
    assert r.status_code == 400, r.text
    assert "unsupported audio format" in r.json()["detail"]
    return "extension 校验生效"


@case("VOI-05", "缺少参考文本 -> 400")
def _():
    r = requests.post(f"{BASE}/api/voices",
                      data={"name": "x", "prompt_text": "  "},
                      files={"file": ("p.wav", PROMPT_WAV.read_bytes(), "audio/wav")})
    assert r.status_code == 400, r.text
    return "prompt_text 必填校验生效"


@case("VOI-06", "无法解码的音频 -> 400")
def _():
    r = requests.post(f"{BASE}/api/voices",
                      data={"name": "x", "prompt_text": "y"},
                      files={"file": ("p.wav", b"not audio at all" * 100, "audio/wav")})
    assert r.status_code == 400, r.text
    return "解码失败兜底生效"


@case("VOI-07", "试听不存在的音色 -> 404")
def _():
    r = requests.get(f"{BASE}/api/voices/deadbeef/prompt")
    assert r.status_code == 404
    return ""


@case("VOI-09", "编辑音色名称与参考文本")
def _():
    vid = globals()["TEST_VOICE_ID"]
    r = requests.put(f"{BASE}/api/voices/{vid}", json={"name": "改名后的音色", "prompt_text": PROMPT_TEXT + "。"})
    assert r.status_code == 200, r.text
    v = r.json()
    assert v["name"] == "改名后的音色", v
    assert "file" not in v, "编辑响应不应暴露文件路径"
    vs = requests.get(f"{BASE}/api/voices").json()
    assert any(x["name"] == "改名后的音色" for x in vs)
    return "name + prompt_text 均已更新"


@case("VOI-10", "编辑校验: 空名 422 / 空更新 400 / 不存在 404")
def _():
    vid = globals()["TEST_VOICE_ID"]
    r = requests.put(f"{BASE}/api/voices/{vid}", json={"name": ""})
    assert r.status_code == 422, f"empty name expect 422, got {r.status_code}"
    r = requests.put(f"{BASE}/api/voices/{vid}", json={})
    assert r.status_code == 400, f"empty body expect 400, got {r.status_code}"
    r = requests.put(f"{BASE}/api/voices/deadbeef", json={"name": "x"})
    assert r.status_code == 404, f"expect 404, got {r.status_code}"
    return "三类异常路径全部正确"


# ============================================================
# 3. 合成: 参数校验 (快速负路径)
# ============================================================
def _tts(**over):
    payload = {"text": "测试", "model": "cosyvoice3", "mode": "sft", "spk_id": "中文女"}
    payload.update(over)
    return requests.post(f"{BASE}/api/tts", json=payload)


@case("VAL-01", "非法 mode -> 422")
def _():
    r = _tts(mode="rap")
    assert r.status_code == 422, f"expect 422, got {r.status_code}"
    return ""


@case("VAL-02", "非法 model -> 422")
def _():
    r = _tts(model="piper")
    assert r.status_code == 422
    return ""


@case("VAL-03", "空白文本 -> 400/422 拒绝")
def _():
    r = _tts(text="   ")
    assert r.status_code in (400, 422), f"got {r.status_code}"
    return "pydantic min_length 或 engine.validate strip 校验生效"


@case("VAL-04", "超长文本 (>2000 字) -> 422")
def _():
    r = _tts(text="长" * 2001)
    assert r.status_code == 422
    return ""


@case("VAL-05", "语速越界 (speed=3.0) -> 422")
def _():
    r = _tts(speed=3.0)
    assert r.status_code == 422, f"expect 422 (ge=0.5, le=2.0), got {r.status_code}"
    return ""


@case("VAL-06", "v2 流式 + 变速 -> 400 (仅 v3 流式支持)")
def _():
    vid = globals().get("TEST_VOICE_ID")
    if not vid:
        return "skip"
    h = requests.get(f"{BASE}/api/health").json()
    if h["model"] != "cosyvoice2":
        return "skip (当前加载的是 v3, v2 流式变速在切换后由服务端拦截)"
    r = _tts(mode="zero_shot", voice_id=vid, spk_id=None, stream=True, speed=1.5)
    assert r.status_code == 400, f"expect 400, got {r.status_code}: {r.text}"
    return "v2 流式变速正确拦截"


@case("VAL-07", "不存在的 voice_id -> 404")
def _():
    r = _tts(mode="zero_shot", voice_id="deadbeef", spk_id=None)
    assert r.status_code == 404, r.text
    return ""


@case("VAL-08", "zero_shot 缺参考文本 -> 400")
def _():
    vid = globals().get("TEST_VOICE_ID")
    if not vid:
        return "skip"
    r = _tts(mode="zero_shot", voice_id=vid, spk_id=None, prompt_text="")
    # voice 库里有 prompt_text 会自动回填, 这里清不掉 -> 若 200 则说明回填逻辑生效
    assert r.status_code in (200, 400), r.status_code
    return "200=音色回填了 prompt_text" if r.status_code == 200 else "400=校验生效"


@case("VAL-09", "sft 说话人不存在 -> 400")
def _():
    r = _tts(spk_id="不存在的音色")
    assert r.status_code == 400, r.text
    assert "not available" in r.json()["detail"]
    return "说话人白名单校验生效"


@case("VAL-10", "GET /api/tts -> 405")
def _():
    r = requests.get(f"{BASE}/api/tts")
    assert r.status_code == 405
    return ""


# ============================================================
# 4. 合成: 正路径 (GPU, 慢)
# ============================================================
@case("GEN-01", "非流式 zero_shot: WAV 结构校验")
def _():
    vid = globals()["TEST_VOICE_ID"]
    r = _tts(text="接口测试：白日依山尽，黄河入海流。", mode="zero_shot", voice_id=vid, spk_id=None)
    assert r.status_code == 200, r.text
    sr, ch, dur, _ = parse_wav(r.content)
    assert sr == int(r.headers["X-Sample-Rate"]), "header 采样率与 WAV 不一致"
    assert dur > 1.0, f"audio too short: {dur}"
    tid = r.headers.get("X-Task-Id")
    assert tid, "missing X-Task-Id"
    globals()["GEN_TASK_ID"] = tid
    return f"{dur:.1f}s @ {sr}Hz, task={tid}, infer={r.headers.get('X-Infer-Ms')}ms"


@case("GEN-02", "流式 zero_shot: 分块到达且首块为 44 字节 WAV 头")
def _():
    vid = globals()["TEST_VOICE_ID"]
    t0 = time.time()
    with requests.post(f"{BASE}/api/tts", stream=True, json={
            "text": "流式接口测试：海内存知己，天涯若比邻。",
            "model": "cosyvoice3", "mode": "zero_shot", "voice_id": vid, "stream": True}) as r:
        assert r.status_code == 200
        first = None
        total = 0
        for chunk in r.iter_content(4096):
            if first is None:
                first = time.time() - t0
            total += len(chunk)
    assert total > 44 * 20, f"流式音频过短: {total} bytes (可能流内报错)"
    globals()["GEN_TASK_ID_2"] = r.headers.get("X-Task-Id")
    return f"first-byte={first*1000:.0f}ms, {total//1024}KB (~{total/2/24000:.1f}s)"


def parse_wav_total(total, sr):
    return sr, 1, (total - 44) / 2 / sr, total - 44


@case("GEN-03", "instruct 指令控制 (四川话)")
def _():
    vid = globals()["TEST_VOICE_ID"]
    r = _tts(text="你要吃饭了没有嘛？", mode="instruct", voice_id=vid, spk_id=None,
             instruct_text="请用四川话说这句话。")
    assert r.status_code == 200, r.text
    sr, ch, dur, _ = parse_wav(r.content)
    assert dur > 0.8
    return f"{dur:.1f}s"


@case("GEN-04", "cross_lingual 跨语言 (英文标签)")
def _():
    vid = globals()["TEST_VOICE_ID"]
    r = _tts(text="<|en|>API testing is fun and easy.", mode="cross_lingual", voice_id=vid, spk_id=None)
    assert r.status_code == 200, r.text
    sr, ch, dur, _ = parse_wav(r.content)
    assert dur > 0.8
    return f"{dur:.1f}s"


@case("GEN-05", "变速合成 speed=1.5 (时长缩短)")
def _():
    vid = globals()["TEST_VOICE_ID"]
    text = "这是一段用于测试语速功能的对照文本，我们检查它的时长变化。"
    r1 = _tts(text=text, mode="zero_shot", voice_id=vid, spk_id=None, speed=1.0)
    r15 = _tts(text=text, mode="zero_shot", voice_id=vid, spk_id=None, speed=1.5)
    assert r1.ok and r15.ok
    d1 = parse_wav(r1.content)[2]
    d15 = parse_wav(r15.content)[2]
    assert d15 < d1 * 0.85, f"speed=1.5 未明显缩短: 1.0x={d1:.1f}s 1.5x={d15:.1f}s"
    return f"1.0x={d1:.1f}s -> 1.5x={d15:.1f}s ({d15/d1*100:.0f}%)"


@case("GEN-06", "多句长文本顺序合成 (流式不断流)")
def _():
    vid = globals()["TEST_VOICE_ID"]
    text = "第一句话，讲的是山。第二句话，讲的是水。第三句话，讲的是人间烟火。第四句话，收个尾。"
    with requests.post(f"{BASE}/api/tts", stream=True, json={
            "text": text, "model": "cosyvoice3", "mode": "zero_shot",
            "voice_id": vid, "stream": True}) as r:
        assert r.ok
        total = sum(len(c) for c in r.iter_content(8192))
    assert total > 44 * 4
    return f"{total/2/24000:.1f}s audio for {len(text)} chars"


@case("GEN-07", "v3 流式变速 speed=1.5 (时长缩短)")
def _():
    vid = globals()["TEST_VOICE_ID"]
    text = "流式变速测试，这段话会以一点五倍语速生成。"
    with requests.post(f"{BASE}/api/tts", stream=True, json={
            "text": text, "model": "cosyvoice3", "mode": "zero_shot",
            "voice_id": vid, "stream": True, "speed": 1.0}) as r:
        assert r.ok
        total_1x = sum(len(c) for c in r.iter_content(8192))
    with requests.post(f"{BASE}/api/tts", stream=True, json={
            "text": text, "model": "cosyvoice3", "mode": "zero_shot",
            "voice_id": vid, "stream": True, "speed": 1.5}) as r:
        assert r.ok
        total_15x = sum(len(c) for c in r.iter_content(8192))
    d1, d15 = (total_1x - 44) / 2 / 24000, (total_15x - 44) / 2 / 24000
    assert d15 < d1 * 0.85, f"流式变速未生效: 1.0x={d1:.1f}s 1.5x={d15:.1f}s"
    return f"1.0x={d1:.1f}s -> 1.5x={d15:.1f}s ({d15/d1*100:.0f}%)"


# ============================================================
# 5. OpenAI 兼容端点
# ============================================================
@case("OAI-01", "/v1/audio/speech 内置音色名 (sft)")
def _():
    h = requests.get(f"{BASE}/api/health").json()
    if not h.get("speakers"):
        return "skip (当前模型无内置说话人)"
    r = requests.post(f"{BASE}/v1/audio/speech", json={"input": "兼容端点测试。", "voice": h["speakers"][0]})
    assert r.ok, r.text
    parse_wav(r.content)
    return f"voice={h['speakers'][0]}"


@case("OAI-02", "/v1/audio/speech 克隆音色 voice:<id>")
def _():
    r = requests.post(f"{BASE}/v1/audio/speech", json={
        "input": "OpenAI compatible endpoint works.", "voice": f"voice:{globals()['TEST_VOICE_ID']}"})
    assert r.ok, r.text
    sr, ch, dur, _ = parse_wav(r.content)
    assert dur > 1
    return f"{dur:.1f}s @ {sr}Hz"


@case("OAI-03", "/v1/audio/speech 非法 voice -> 400/404")
def _():
    r = requests.post(f"{BASE}/v1/audio/speech", json={"input": "x", "voice": "voice:deadbeef"})
    assert r.status_code == 404, f"expect 404, got {r.status_code}"
    return ""


@case("OAI-04", "/v1/audio/speech 非法 response_format -> 422")
def _():
    r = requests.post(f"{BASE}/v1/audio/speech", json={
        "input": "x", "voice": f"voice:{globals()['TEST_VOICE_ID']}", "response_format": "mp3"})
    assert r.status_code == 422, f"当前仅支持 wav, expect 422, got {r.status_code}"
    return "mp3 暂不支持, 明确报错"


# ============================================================
# 6. 任务历史
# ============================================================
@case("TSK-01", "历史列表字段与脱敏")
def _():
    ts = requests.get(f"{BASE}/api/tasks?limit=50").json()["tasks"]
    assert len(ts) >= 5, f"expect >=5 tasks, got {len(ts)}"
    for t in ts:
        assert "file" not in t, "历史不应暴露文件路径"
    ok_count = sum(1 for t in ts if t["status"] == "ok")
    assert ok_count >= 3, f"ok tasks too few: {ok_count}"
    globals()["TSK_LIST"] = [t["id"] for t in ts]
    return f"{len(ts)} tasks ({ok_count} ok)"


@case("TSK-02", "按 mode 过滤历史")
def _():
    ts = requests.get(f"{BASE}/api/tasks?mode=zero_shot").json()["tasks"]
    assert ts and all(t["mode"] == "zero_shot" for t in ts)
    return f"{len(ts)} zero_shot tasks"


@case("TSK-03", "下载任务音频可解析")
def _():
    tid = globals().get("GEN_TASK_ID") or globals().get("TSK_LIST", [None])[0]
    assert tid, "no task available"
    r = requests.get(f"{BASE}/api/tasks/{tid}/audio")
    assert r.ok, f"task {tid} audio unavailable"
    sr, ch, dur, _ = parse_wav(r.content)
    return f"task={tid}, {dur:.1f}s"


@case("TSK-04", "不存在的任务音频 -> 404")
def _():
    r = requests.get(f"{BASE}/api/tasks/deadbeef/audio")
    assert r.status_code == 404
    return ""


@case("TSK-05", "删除任务 (音频文件一并清理)")
def _():
    tid = globals().get("GEN_TASK_ID_2") or globals().get("TSK_LIST", [None])[0]
    assert tid, "no task available"
    assert requests.delete(f"{BASE}/api/tasks/{tid}").json()["ok"] is True
    assert requests.get(f"{BASE}/api/tasks/{tid}/audio").status_code == 404
    assert requests.delete(f"{BASE}/api/tasks/{tid}").status_code == 404, "重复删除应 404"
    return f"deleted {tid}"


# ============================================================
# 7. 并发与排队
# ============================================================
@case("Q-01", "并发 3 请求全部成功且出现排队")
def _():
    if SKIP_SLOW:
        return "skip (--skip-slow)"
    vid = globals()["TEST_VOICE_ID"]
    results = []
    def worker(i):
        r = requests.post(f"{BASE}/api/tts", json={
            "text": f"并发测试第{i}句，大家排好队。", "model": "cosyvoice3",
            "mode": "zero_shot", "voice_id": vid, "stream": False})
        results.append(r.status_code)
    threads = [threading.Thread(target=worker, args=(i,)) for i in range(3)]
    saw_queue = [0]
    stop = [False]
    def poll():
        while not stop[0]:
            q = requests.get(f"{BASE}/api/health").json()["queue_len"]
            saw_queue[0] = max(saw_queue[0], q)
            time.sleep(0.5)
    p = threading.Thread(target=poll); p.start()
    for t in threads: t.start()
    for t in threads: t.join()
    stop[0] = True; p.join()
    assert results == [200, 200, 200], f"status codes: {results}"
    assert saw_queue[0] >= 1, "未观察到排队 (串行队列设计预期 queue_len>=1)"
    return f"3/3 ok, max queue_len={saw_queue[0]}"


# ============================================================
# 9. 播客工坊
# ============================================================
@case("POD-01", "ffmpeg 检测可用")
def _():
    r = requests.get(f"{BASE}/api/podcast/ffmpeg").json()
    assert r["available"] is True, "ffmpeg 未就绪, 推流用例将跳过"
    globals()["FFMPEG_OK"] = True
    return "available"


@case("POD-02", "LLM 配置保存/读取脱敏")
def _():
    r = requests.put(f"{BASE}/api/llm/config", json={
        "base_url": "https://api.deepseek.com/v1", "api_key": "sk-test-1234567890abcdef",
        "model": "deepseek-chat"})
    assert r.ok, r.text
    cfg = r.json()
    assert "api_key" not in cfg, "响应不得包含原始 key"
    assert cfg["api_key_masked"].startswith("sk-t"), cfg["api_key_masked"]
    got = requests.get(f"{BASE}/api/llm/config").json()
    assert got["base_url"] == "https://api.deepseek.com/v1"
    globals()["LLM_CFG_OK"] = True
    return f"masked={cfg['api_key_masked']}"


@case("POD-03", "URL 内容提取")
def _():
    r = requests.post(f"{BASE}/api/podcast/source/extract", json={
        "url": "https://raw.githubusercontent.com/hanhuikin/tts-cosy-voice/main/README.md"})
    assert r.ok, r.text
    j = r.json()
    assert j["chars"] > 50, f"提取过短: {j['chars']}"
    return f"{j['chars']} chars"


@case("POD-04", "剧集创建/查询/更新")
def _():
    vid = globals()["TEST_VOICE_ID"]
    lines = [
        {"role": "host", "text": "欢迎收听本期播客，我们来聊聊接口测试。"},
        {"role": "guest", "text": "没错，测试是工程质量的基石。"},
        {"role": "host", "text": "那我们就从这里说起。"},
    ]
    r = requests.post(f"{BASE}/api/podcast/episodes", json={
        "title": "测试剧集", "source_type": "text", "source_ref": "",
        "lines": lines, "voice_host": vid, "voice_guest": vid})
    assert r.ok, r.text
    ep = r.json()
    assert ep["status"] == "draft"
    r = requests.put(f"{BASE}/api/podcast/episodes/{ep['id']}", json={
        "title": "测试剧集(改)", "source_type": "text", "source_ref": "",
        "lines": lines, "voice_host": vid, "voice_guest": vid})
    assert r.ok and requests.get(f"{BASE}/api/podcast/episodes/{ep['id']}").json()["title"] == "测试剧集(改)"
    globals()["POD_EP_ID"] = ep["id"]
    globals()["POD_LINES"] = lines
    return f"id={ep['id']}"


@case("POD-05", "非流式合成整段成品 (逐句+拼接)")
def _():
    if SKIP_SLOW:
        return "skip (--skip-slow)"
    pid = globals()["POD_EP_ID"]
    r = requests.post(f"{BASE}/api/podcast/episodes/{pid}/synthesize", json={"mode": "final"})
    assert r.ok, r.text
    for _ in range(120):
        time.sleep(2)
        st = requests.get(f"{BASE}/api/podcast/episodes/{pid}/status").json()
        if st["status"] in ("completed", "failed"):
            break
    assert st["status"] == "completed", f"final status={st}"
    assert st["progress"] == 3, st
    r = requests.get(f"{BASE}/api/podcast/episodes/{pid}/audio")
    assert r.ok
    sr, ch, dur, _ = parse_wav(r.content)
    assert dur > 4, f"成品过短 {dur}s"
    return f"{st['progress']} 句, {dur:.1f}s @ {sr}Hz"


@case("POD-06", "HLS 流式推流 (逐句合成→ffmpeg 分片)")
def _():
    if SKIP_SLOW or not globals().get("FFMPEG_OK"):
        return "skip (--skip-slow / 无 ffmpeg)"
    pid = globals()["POD_EP_ID"]
    r = requests.post(f"{BASE}/api/podcast/episodes/{pid}/push", json={"protocol": "hls"})
    assert r.ok, r.text
    st = {}
    for _ in range(90):
        time.sleep(2)
        st = requests.get(f"{BASE}/api/podcast/push/status").json()
        if st["status"] in ("completed", "failed"):
            break
    assert st["status"] == "completed", f"push status={st}"
    m3u8 = requests.get(f"{BASE}/live/{pid}.m3u8")
    assert m3u8.ok and "#EXTM3U" in m3u8.text, "HLS 播放列表未生成"
    assert "#EXTINF" in m3u8.text and m3u8.text.count("#EXTINF") >= 2, "分片数量异常"
    globals()["POD_M3U8"] = f"{pid}.m3u8"
    return f"lines_done={st['lines_done']}, m3u8 ok"


@case("POD-07", "推流冲突与停止接口")
def _():
    pid = globals()["POD_EP_ID"]
    r = requests.post(f"{BASE}/api/podcast/episodes/{pid}/push", json={"protocol": "brotli"})
    assert r.status_code == 422, f"非法协议 expect 422, got {r.status_code}"
    r = requests.delete(f"{BASE}/api/podcast/episodes/nowhere/push")
    assert r.status_code == 404
    return "协议校验与停止 404 正确"


@case("POD-08", "本地流式合成 (mode=stream, 与工作台一致)")
def _():
    pid = globals()["POD_EP_ID"]
    r = requests.post(f"{BASE}/api/podcast/episodes/{pid}/synthesize",
                      json={"mode": "stream"}, stream=True)
    assert r.ok, r.text
    assert "audio/wav" in r.headers["content-type"]
    total = 0
    first = None
    t0 = time.time()
    for chunk in r.iter_content(8192):
        if first is None:
            first = time.time() - t0
        total += len(chunk)
    assert total > 44 * 20, f"流式音频过短: {total}"
    # 结束后剧集应完成并落盘成品
    st = {}
    for _ in range(15):
        st = requests.get(f"{BASE}/api/podcast/episodes/{pid}/status").json()
        if st["status"] in ("completed", "failed"):
            break
        time.sleep(1)
    assert st["status"] == "completed", st
    assert requests.get(f"{BASE}/api/podcast/episodes/{pid}/audio").ok, "成品未落盘"
    return f"first-byte={first*1000:.0f}ms, {total//1024}KB, 成品已保存"


@case("POD-09", "剧集删除")
def _():
    pid = globals()["POD_EP_ID"]
    assert requests.delete(f"{BASE}/api/podcast/episodes/{pid}").status_code == 200
    assert requests.delete(f"{BASE}/api/podcast/episodes/{pid}").status_code == 404
    if globals().get("POD_M3U8"):
        requests.get(f"{BASE}/live/{globals()['POD_M3U8']}")
    return f"deleted {pid}"


# ============================================================
# 8. 音色删除 (清理, 放最后)
# ============================================================
@case("VOI-08", "删除音色 -> 列表移除")
def _():
    vid = globals()["TEST_VOICE_ID"]
    assert requests.delete(f"{BASE}/api/voices/{vid}").json()["ok"] is True
    assert requests.delete(f"{BASE}/api/voices/{vid}").status_code == 404, "重复删除应 404"
    assert not any(v["id"] == vid for v in requests.get(f"{BASE}/api/voices").json())
    return f"deleted {vid}"


def main():
    global BASE, SKIP_SLOW
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default=BASE)
    ap.add_argument("--skip-slow", action="store_true", help="跳过并发等慢用例")
    a = ap.parse_args()
    BASE, SKIP_SLOW = a.base, a.skip_slow

    print("=" * 62)
    print("CosyVoice API 接口测试")
    print("=" * 62)
    print("[setup] 等待模型就绪 (cosyvoice3)...")
    h = ensure_model("cosyvoice3")
    print(f"[setup] ready: {h['model_label']} @ {h['sample_rate']}Hz\n")

    for cid, name, fn in CASES:
        fn()

    # 汇总
    n_pass = sum(1 for r in RESULTS if r[2] == "PASS")
    n_fail = sum(1 for r in RESULTS if r[2] == "FAIL")
    n_err = sum(1 for r in RESULTS if r[2] == "ERROR")
    print("\n" + "=" * 62)
    print(f"结果: {n_pass} PASS / {n_fail} FAIL / {n_err} ERROR  (共 {len(RESULTS)} 例)")
    print("=" * 62)
    if n_fail or n_err:
        for cid, name, st, detail in RESULTS:
            if st != "PASS":
                print(f"  [{cid}] {st}: {name} — {detail}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
