"""MCP (Model Context Protocol) 核心实现: 工具定义 + JSON-RPC 消息处理, 与传输无关。

三种传输复用本模块:
- stdio:           客户端拉起 server/mcp_server.py 子进程, 每行一条 JSON-RPC
- SSE:             GET /mcp/sse 建立下行通道, POST /mcp/messages?session_id= 提交消息
- Streamable HTTP: POST/GET/DELETE /mcp (Mcp-Session-Id 会话头)

只依赖标准库: 工具经 HTTP 回环调用本服务既有 REST 接口 (与直连客户端完全同构),
每次调用带 X-Client-Source: mcp:<工具名>, 服务端控制台「音频产出」日志即可区分来源类型;
本模块同时向 cosyvoice.mcp logger 输出每次工具调用审计 (stdio 下打到 stderr)。
"""
from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
import time
import urllib.error
import urllib.request
from pathlib import Path

from .storage import DATA_DIR

logger = logging.getLogger("cosyvoice.mcp")

SERVER_INFO = {"name": "cosyvoice-tts", "version": "1.0.0"}
# 支持的协议版本: 回应客户端请求的版本, 不认识则用服务端最新
SUPPORTED_VERSIONS = {"2024-11-05", "2025-03-26", "2025-06-18"}
LATEST_VERSION = "2025-06-18"

INSTRUCTIONS = (
    "CosyVoice TTS 服务: 文本转语音、音色库管理、播客工坊 (对话稿→逐句合成→成品音频)。\n"
    "常用路径: server_status 确认模型就绪 → list_voices 选音色 → tts_synthesize 合成短句; "
    "长对话用 podcast_create_episode + podcast_synthesize 逐句合成。"
)

# 默认服务地址 (stdio 模式用环境变量指定); HTTP 托管模式由路由按请求传入
_DEFAULT_BASE = os.environ.get("COSYVOICE_MCP_BASE", "http://127.0.0.1:8000").rstrip("/")
# 工具产出音频的本地落盘目录 (合成/下载类工具都会存一份, 方便客户端直接读文件)
DOWNLOAD_DIR = Path(os.environ.get("COSYVOICE_MCP_DOWNLOAD_DIR")
                    or Path(__file__).resolve().parents[1] / "data" / "mcp")


# ---------- HTTP 回环调用 ----------

def http_req(base: str, path: str, *, method: str = "GET", body=None, source: str = "",
             timeout: int = 60) -> tuple[int, dict, bytes]:
    """调服务 REST 接口, 返回 (status, headers, body_bytes)。带 X-Client-Source 来源标识。"""
    url = base.rstrip("/") + path
    headers = {"Accept": "application/json"}
    if source:
        headers["X-Client-Source"] = f"mcp:{source}"
    data = None
    if body is not None:
        data = json.dumps(body, ensure_ascii=False).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, dict(r.headers), r.read()
    except urllib.error.HTTPError as e:
        return e.code, dict(e.headers), e.read()


def json_of(status: int, payload: bytes):
    try:
        return json.loads(payload.decode("utf-8"))
    except Exception:
        raise RuntimeError(f"HTTP {status}: {payload.decode('utf-8', 'replace')[:300]}")


def check(status: int, payload: bytes, what: str) -> None:
    if status >= 400:
        try:
            detail = json.loads(payload.decode("utf-8")).get("detail", "")
        except Exception:
            detail = payload.decode("utf-8", "replace")[:200]
        raise RuntimeError(f"{what}失败 (HTTP {status}): {detail}")


def _save_audio(payload: bytes, name: str, ext: str) -> Path:
    DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)
    out = DOWNLOAD_DIR / f"{name}.{ext}"
    out.write_bytes(payload)
    return out


def _fmt_duration(s) -> str:
    return f"{s:.2f}s" if isinstance(s, (int, float)) else "-"


# ---------- 工具实现 (同步, 由调用方丢线程池; base=服务地址) ----------

_MODELS_CACHE: dict[str, tuple[float, dict]] = {}


def _model_cfg(base: str, model: str) -> dict:
    cached = _MODELS_CACHE.get(base)
    if cached is None or time.time() - cached[0] > 60:
        st, _, pl = http_req(base, "/api/models")
        check(st, pl, "读取模型列表")
        _MODELS_CACHE[base] = (time.time(), {m["key"]: m for m in json_of(st, pl)["models"]})
    cfg = _MODELS_CACHE[base][1].get(model)
    if not cfg:
        raise RuntimeError(f"未知模型 '{model}', 可选: {sorted(_MODELS_CACHE[base][1])}")
    return cfg


def t_server_status(args: dict, base: str) -> list[dict]:
    st, _, pl = http_req(base, "/api/health")
    check(st, pl, "读取服务状态")
    d = json_of(st, pl)
    models = [{"key": m["key"], "label": m["label"], "available": m["available"],
               "loaded": m["loaded"]} for m in d.get("models", [])]
    text = (f"服务状态: {d.get('state')} | 当前模型: {d.get('model')} ({d.get('model_label')})\n"
            f"采样率: {d.get('sample_rate')} | GPU: {d.get('gpu') or '-'}\n"
            f"队列: {d.get('queue_len')}/{d.get('queue_max')} | 音色数: {d.get('voices_count')}\n"
            f"模型: {json.dumps(models, ensure_ascii=False)}")
    return [{"type": "text", "text": text}]


def t_list_voices(args: dict, base: str) -> list[dict]:
    st, _, pl = http_req(base, "/api/voices")
    check(st, pl, "读取音色列表")
    voices = json_of(st, pl)
    if not voices:
        return [{"type": "text", "text": "音色库为空, 可经 Web UI 或 POST /api/voices 上传参考音频克隆。"}]
    lines = [f"- {v['name']} (voice_id={v['id']}, 参考音频 {v['duration']:.1f}s): {v['prompt_text'][:60]}"
             for v in voices]
    return [{"type": "text", "text": f"共 {len(voices)} 个音色:\n" + "\n".join(lines)}]


def t_tts_synthesize(args: dict, base: str) -> list[dict]:
    text = (args.get("text") or "").strip()
    if not text:
        raise RuntimeError("text 不能为空")
    model = args.get("model") or "cosyvoice3"
    cfg = _model_cfg(base, model)
    mode = args.get("mode")
    body: dict = {"text": text, "model": model, "speed": args.get("speed", 1.0), "stream": False}
    if args.get("voice_id"):
        # 参考音频克隆: 模型不支持 zero_shot 时退回 cross_lingual 承载
        if not mode:
            mode = next((m for m in ("zero_shot", "cross_lingual") if m in cfg["modes"]), None)
        if not mode:
            raise RuntimeError(f"模型 {model} 不支持参考音频克隆")
        body.update(mode=mode, voice_id=args["voice_id"])
    elif args.get("spk_id"):
        if "sft" not in cfg.get("speaker_modes", []):
            raise RuntimeError(f"模型 {model} 无内置说话人, 请传 voice_id 或 instruct_text")
        body.update(mode="sft", spk_id=args["spk_id"])
    elif args.get("instruct_text"):
        if "instruct" not in cfg["modes"]:
            raise RuntimeError(f"模型 {model} 不支持 instruct 模式")
        body.update(mode="instruct", instruct_text=args["instruct_text"])
    else:
        raise RuntimeError("需要 voice_id / spk_id / instruct_text 之一来指定声音")
    if not mode:
        raise RuntimeError(f"无法为模型 {model} 解析合成模式, 请显式传 mode")

    st, hd, payload = http_req(base, "/api/tts", method="POST", body=body, source="tts_synthesize",
                               timeout=600)
    check(st, payload, "合成")
    tid = hd.get("X-Task-Id") or hd.get("x-task-id") or ""
    sr = hd.get("X-Sample-Rate") or hd.get("x-sample-rate") or "?"
    dur = hd.get("X-Duration-Ms") or hd.get("x-duration-ms") or "?"
    wav = _save_audio(payload, f"tts_{tid or int(time.time())}", "wav")
    logger.info("[MCP] tts_synthesize 完成: task=%s 时长=%sms 文件=%s", tid, dur, wav)
    dur_s = int(dur) / 1000 if str(dur).isdigit() else "?"
    out = [{"type": "text",
            "text": (f"合成完成: 任务={tid} 时长={dur_s}s 采样率={sr}\n"
                     f"本地文件: {wav}\n"
                     f"下载: {base}/api/tasks/{tid}/audio (WAV) 或 "
                     f"{base}/api/tasks/{tid}/audio?format=mp3 (MP3)")}]
    if args.get("include_base64"):
        out.append({"type": "audio", "data": base64.b64encode(payload).decode("ascii"),
                    "mimeType": "audio/wav"})
    return out


def t_list_tasks(args: dict, base: str) -> list[dict]:
    limit = min(int(args.get("limit") or 20), 200)
    st, _, pl = http_req(base, f"/api/tasks?limit={limit}")
    check(st, pl, "读取任务历史")
    tasks = json_of(st, pl)["tasks"]
    status = args.get("status")
    if status:
        tasks = [t for t in tasks if t.get("status") == status]
    if not tasks:
        return [{"type": "text", "text": "没有匹配的任务。"}]
    lines = []
    for t in tasks:
        seg = (f"- {t['id']} [{t['status']}] 来源={t.get('source') or '-'} "
               f"模型={t['model']} 时长={_fmt_duration(t.get('duration'))}")
        txt = (t.get("text") or "")[:40].replace("\n", " ")
        lines.append(f"{seg} | {txt}")
    return [{"type": "text", "text": f"最近 {len(tasks)} 条任务:\n" + "\n".join(lines)}]


def t_get_task(args: dict, base: str) -> list[dict]:
    tid = args.get("task_id") or ""
    st, _, pl = http_req(base, f"/api/tasks/{tid}")
    if st == 404:
        raise RuntimeError(f"任务不存在: {tid}")
    check(st, pl, "读取任务")
    t = json_of(st, pl)
    return [{"type": "text", "text": json.dumps(t, ensure_ascii=False, indent=1)
             + f"\n音频: {base}/api/tasks/{tid}/audio"}]


def t_get_task_audio(args: dict, base: str) -> list[dict]:
    tid = args.get("task_id") or ""
    fmt = (args.get("format") or "wav").lower()
    st, _, pl = http_req(base, f"/api/tasks/{tid}/audio?format={fmt}", source="get_task_audio",
                         timeout=120)
    if st == 404:
        raise RuntimeError(f"任务不存在或音频未生成: {tid}")
    if st == 503:
        raise RuntimeError("MP3 转码不可用: 服务端未安装 ffmpeg")
    check(st, pl, "下载任务音频")
    out = _save_audio(pl, f"task_{tid}", fmt)
    return [{"type": "text", "text": f"已保存 {len(pl)} 字节: {out}"}]


def t_stop_task(args: dict, base: str) -> list[dict]:
    tid = args.get("task_id") or ""
    st, _, pl = http_req(base, f"/api/tasks/{tid}/stop", method="POST", body={},
                         source="stop_task")
    check(st, pl, "停止任务")
    return [{"type": "text", "text": f"已请求停止任务 {tid} (推理在下一个分块边界结束)。"}]


def t_podcast_list_episodes(args: dict, base: str) -> list[dict]:
    limit = min(int(args.get("limit") or 20), 200)
    st, _, pl = http_req(base, f"/api/podcast/episodes?limit={limit}")
    check(st, pl, "读取剧集列表")
    eps = json_of(st, pl)["episodes"]
    if not eps:
        return [{"type": "text", "text": "暂无剧集。可先用 podcast_create_episode 从对话稿创建。"}]
    lines = [f"- {e['id']} 「{e['title']}」 状态={e['status']} 进度={e['progress']}/{e['lines_count']}"
             f" 成品={'有' if e['has_audio'] else '无'}"
             for e in eps]
    return [{"type": "text", "text": f"共 {len(eps)} 个剧集:\n" + "\n".join(lines)}]


def t_podcast_get_episode(args: dict, base: str) -> list[dict]:
    pid = args.get("episode_id") or ""
    st, _, pl = http_req(base, f"/api/podcast/episodes/{pid}")
    if st == 404:
        raise RuntimeError(f"剧集不存在: {pid}")
    check(st, pl, "读取剧集")
    ep = json_of(st, pl)
    info = {k: ep.get(k) for k in ("id", "title", "status", "progress", "lines_count",
                                   "duration", "error", "has_audio", "speed", "created_at")}
    info["speakers"] = [f"{s.get('name')}({'主持' if s.get('role') == 'host' else '嘉宾'}, "
                        f"voice_id={s.get('voice_id') or '-'})" for s in ep.get("speakers", [])]
    script = [{"role": ln.get("role"), "name": ln.get("name"), "text": ln.get("text")}
              for ln in ep.get("script", [])]
    body = {"info": info, "script": script,
            "audio_url": f"{base}/api/podcast/episodes/{pid}/audio"}
    return [{"type": "text", "text": json.dumps(body, ensure_ascii=False, indent=1)}]


def t_podcast_create_episode(args: dict, base: str) -> list[dict]:
    title = (args.get("title") or "").strip()
    if not title:
        raise RuntimeError("title 不能为空")
    lines = args.get("lines") or []
    if not isinstance(lines, list) or not lines:
        raise RuntimeError("lines 不能为空: [{role, name, text, voice_id?}, ...]")
    clean = []
    for i, ln in enumerate(lines):
        if not isinstance(ln, dict) or not (ln.get("text") or "").strip():
            raise RuntimeError(f"lines[{i}] 缺少 text")
        clean.append({"role": ln.get("role", "host"), "name": ln.get("name", ""),
                      "text": ln["text"], "voice_id": ln.get("voice_id", "")})
    speakers = [s for s in (args.get("speakers") or []) if isinstance(s, dict)]
    body = {"title": title[:100], "source_type": "text", "source_ref": "",
            "lines": clean, "speakers": speakers, "speed": args.get("speed", 1.0)}
    st, _, pl = http_req(base, "/api/podcast/episodes", method="POST", body=body,
                         source="podcast_create_episode")
    check(st, pl, "创建剧集")
    ep = json_of(st, pl)
    logger.info("[MCP] podcast_create_episode: id=%s 标题=%s %d 句", ep["id"], title, len(clean))
    return [{"type": "text",
             "text": f"剧集已创建: id={ep['id']} 共 {len(clean)} 句。\n"
                     f"下一步: podcast_synthesize(episode_id=\"{ep['id']}\") 触发合成。"}]


def t_podcast_synthesize(args: dict, base: str) -> list[dict]:
    pid = args.get("episode_id") or ""
    wait = bool(args.get("wait", True))
    timeout_s = min(int(args.get("timeout_s") or 900), 3600)
    st, _, pl = http_req(base, f"/api/podcast/episodes/{pid}/synthesize", method="POST",
                         body={"mode": "final"}, source="podcast_synthesize", timeout=60)
    check(st, pl, "触发合成")
    if not wait:
        return [{"type": "text", "text": f"已触发合成 (后台进行), 用 podcast_get_episode 查进度: {pid}"}]
    t0 = time.time()
    while time.time() - t0 < timeout_s:
        time.sleep(2)
        st2, _, pl2 = http_req(base, f"/api/podcast/episodes/{pid}/status")
        if st2 >= 400:
            continue
        d = json_of(st2, pl2)
        if d["status"] in ("completed", "failed", "stopped"):
            if d["status"] == "completed":
                logger.info("[MCP] podcast_synthesize 完成: episode=%s 时长=%ss",
                            pid, d.get("duration"))
                return [{"type": "text",
                         "text": (f"合成完成: 时长={_fmt_duration(d.get('duration'))} "
                                  f"共 {d.get('lines_total')} 句\n"
                                  f"本地文件: {DATA_DIR / 'podcasts' / (pid + '.wav')}\n"
                                  f"下载: {base}/api/podcast/episodes/{pid}/audio")}]
            raise RuntimeError(f"合成结束但状态为 {d['status']}: {d.get('error') or '手动停止/部分完成'}")
    raise RuntimeError(f"等待超时 ({timeout_s}s), 合成仍在进行, 稍后用 podcast_get_episode 查询")


def t_podcast_get_audio(args: dict, base: str) -> list[dict]:
    pid = args.get("episode_id") or ""
    fmt = (args.get("format") or "wav").lower()
    st, _, pl = http_req(base, f"/api/podcast/episodes/{pid}/audio?format={fmt}",
                         source="podcast_get_audio", timeout=300)
    if st == 404:
        raise RuntimeError(f"剧集不存在或成品音频未生成: {pid}")
    if st == 503:
        raise RuntimeError("MP3 转码不可用: 服务端未安装 ffmpeg")
    check(st, pl, "下载剧集成品")
    out = _save_audio(pl, f"podcast_{pid}", fmt)
    return [{"type": "text", "text": f"已保存 {len(pl)} 字节: {out}"}]


def t_podcast_stop(args: dict, base: str) -> list[dict]:
    pid = args.get("episode_id") or ""
    st, _, pl = http_req(base, f"/api/podcast/episodes/{pid}/stop", method="POST", body={},
                         source="podcast_stop")
    check(st, pl, "停止合成")
    return [{"type": "text", "text": f"已请求停止剧集 {pid} 的合成 (当前句在分块边界结束)。"}]


# ---------- 工具目录 ----------

_TOOLS: list[dict] = [
    {"name": "server_status",
     "description": "查看 TTS 服务状态: 引擎状态/当前模型/GPU/队列/音色数。",
     "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False}},
    {"name": "list_voices",
     "description": "列出音色库全部克隆音色 (id/名称/参考文本)。合成时把 voice_id 传给 tts_synthesize。",
     "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False}},
    {"name": "tts_synthesize",
     "description": "文本转语音 (同步等待整段合成完成, ≤2000 字)。"
                    "指定声音三选一: voice_id(克隆音色) / spk_id(内置说话人) / instruct_text(声音描述)。"
                    "返回本地音频文件路径与下载链接。",
     "inputSchema": {"type": "object",
                     "properties": {
                         "text": {"type": "string", "description": "要合成的文本 (≤2000 字)"},
                         "voice_id": {"type": "string", "description": "克隆音色 id (list_voices 获取)"},
                         "spk_id": {"type": "string", "description": "内置说话人名 (仅 sft 模型)"},
                         "instruct_text": {"type": "string", "description": "声音描述, 如「用四川话说这句话」"},
                         "model": {"type": "string", "description": "模型 key, 默认 cosyvoice3",
                                   "enum": ["cosyvoice3", "cosyvoice2", "indextts2", "indextts25",
                                            "qwen3tts", "qwen3tts_base", "qwen3tts_design"]},
                         "speed": {"type": "number", "description": "语速 0.5~2.0, 默认 1.0"},
                         "mode": {"type": "string", "description": "合成模式, 一般留空自动解析",
                                  "enum": ["sft", "zero_shot", "cross_lingual", "instruct"]},
                         "include_base64": {"type": "boolean",
                                            "description": "返回值附带 base64 音频内容块, 默认 false"},
                     },
                     "required": ["text"], "additionalProperties": False}},
    {"name": "list_tasks",
     "description": "查询合成任务历史 (含来源类型 source: api/openai/mcp)。",
     "inputSchema": {"type": "object",
                     "properties": {"limit": {"type": "integer", "description": "条数, 默认 20"},
                                    "status": {"type": "string",
                                               "enum": ["ok", "failed", "running"]}},
                     "additionalProperties": False}},
    {"name": "get_task",
     "description": "查询单个合成任务详情 (状态/耗时/参数/来源)。",
     "inputSchema": {"type": "object",
                     "properties": {"task_id": {"type": "string"}},
                     "required": ["task_id"], "additionalProperties": False}},
    {"name": "get_task_audio",
     "description": "下载任务音频到本地文件 (默认 WAV, 可选 MP3)。",
     "inputSchema": {"type": "object",
                     "properties": {"task_id": {"type": "string"},
                                    "format": {"type": "string", "enum": ["wav", "mp3"]}},
                     "required": ["task_id"], "additionalProperties": False}},
    {"name": "stop_task",
     "description": "停止进行中的合成任务 (排队中的跳过; 推理中的在分块边界停止)。",
     "inputSchema": {"type": "object",
                     "properties": {"task_id": {"type": "string"}},
                     "required": ["task_id"], "additionalProperties": False}},
    {"name": "podcast_list_episodes",
     "description": "列出播客剧集 (标题/状态/进度/是否有成品音频)。",
     "inputSchema": {"type": "object",
                     "properties": {"limit": {"type": "integer"}},
                     "additionalProperties": False}},
    {"name": "podcast_get_episode",
     "description": "读取剧集详情: 对话稿逐句内容、主持阵容、合成进度。",
     "inputSchema": {"type": "object",
                     "properties": {"episode_id": {"type": "string"}},
                     "required": ["episode_id"], "additionalProperties": False}},
    {"name": "podcast_create_episode",
     "description": "从对话稿创建播客剧集。lines 为逐句数组 [{role: host|guest, name, text, voice_id?}]。"
                    "写稿前建议先 list_voices 取 voice_id 分配给各说话人。",
     "inputSchema": {"type": "object",
                     "properties": {
                         "title": {"type": "string", "description": "剧集标题 (≤100 字)"},
                         "lines": {"type": "array", "description": "对话稿, 每项 {role, name, text, voice_id?}",
                                   "items": {"type": "object",
                                             "properties": {"role": {"type": "string", "enum": ["host", "guest"]},
                                                            "name": {"type": "string"},
                                                            "text": {"type": "string"},
                                                            "voice_id": {"type": "string"}},
                                             "required": ["text"]}},
                         "speakers": {"type": "array",
                                      "description": "主持阵容 [{role, name, desc, voice_id}]; 省略时按 lines 内 voice_id 解析",
                                      "items": {"type": "object", "properties": {
                                          "role": {"type": "string", "enum": ["host", "guest"]},
                                          "name": {"type": "string"}, "desc": {"type": "string"},
                                          "voice_id": {"type": "string"}}}},
                         "speed": {"type": "number", "description": "全局语速 0.5~2.0, 默认 1.0"},
                     },
                     "required": ["title", "lines"], "additionalProperties": False}},
    {"name": "podcast_synthesize",
     "description": "触发剧集逐句合成 (final 模式)。默认阻塞等待完成并返回成品信息; wait=false 立即返回, 用 podcast_get_episode 查进度。",
     "inputSchema": {"type": "object",
                     "properties": {"episode_id": {"type": "string"},
                                    "wait": {"type": "boolean", "description": "等待完成, 默认 true"},
                                    "timeout_s": {"type": "integer", "description": "等待上限秒数, 默认 900"}},
                     "required": ["episode_id"], "additionalProperties": False}},
    {"name": "podcast_get_audio",
     "description": "下载剧集成品音频到本地文件 (默认 WAV, 可选 MP3, 需合成完成)。",
     "inputSchema": {"type": "object",
                     "properties": {"episode_id": {"type": "string"},
                                    "format": {"type": "string", "enum": ["wav", "mp3"]}},
                     "required": ["episode_id"], "additionalProperties": False}},
    {"name": "podcast_stop",
     "description": "停止剧集合成 (当前句在分块边界结束, 已合成句子保留为部分成品)。",
     "inputSchema": {"type": "object",
                     "properties": {"episode_id": {"type": "string"}},
                     "required": ["episode_id"], "additionalProperties": False}},
]

_HANDLERS = {
    "server_status": t_server_status,
    "list_voices": t_list_voices,
    "tts_synthesize": t_tts_synthesize,
    "list_tasks": t_list_tasks,
    "get_task": t_get_task,
    "get_task_audio": t_get_task_audio,
    "stop_task": t_stop_task,
    "podcast_list_episodes": t_podcast_list_episodes,
    "podcast_get_episode": t_podcast_get_episode,
    "podcast_create_episode": t_podcast_create_episode,
    "podcast_synthesize": t_podcast_synthesize,
    "podcast_get_audio": t_podcast_get_audio,
    "podcast_stop": t_podcast_stop,
}


# ---------- JSON-RPC / MCP 协议层 ----------

def _resp(mid, result: dict) -> dict:
    return {"jsonrpc": "2.0", "id": mid, "result": result}


def _err(mid, code: int, message: str) -> dict:
    return {"jsonrpc": "2.0", "id": mid, "error": {"code": code, "message": message}}


async def handle_message(msg, base: str | None = None) -> dict | None:
    """处理一条 JSON-RPC 消息, 返回响应 (通知类返回 None)。base 覆盖服务地址 (HTTP 托管时按请求传)。"""
    if not isinstance(msg, dict):
        return _err(None, -32600, "invalid request: not an object")
    method = msg.get("method")
    mid = msg.get("id")
    if method is None:
        return None   # 对端发来的 response, 忽略
    params = msg.get("params") or {}
    if mid is None:   # notification
        return None
    b = (base or _DEFAULT_BASE).rstrip("/")

    if method == "initialize":
        cv = params.get("protocolVersion")
        ver = cv if cv in SUPPORTED_VERSIONS else LATEST_VERSION
        logger.info("[MCP] initialize: client=%s 协议=%s",
                    (params.get("clientInfo") or {}).get("name", "?"), ver)
        return _resp(mid, {"protocolVersion": ver,
                           "capabilities": {"tools": {"listChanged": False}},
                           "serverInfo": SERVER_INFO,
                           "instructions": INSTRUCTIONS})
    if method == "ping":
        return _resp(mid, {})
    if method == "tools/list":
        return _resp(mid, {"tools": _TOOLS})
    if method == "tools/call":
        name = params.get("name") or ""
        fn = _HANDLERS.get(name)
        if fn is None:
            return _resp(mid, {"content": [{"type": "text", "text": f"未知工具: {name}"}],
                               "isError": True})
        args = params.get("arguments") or {}
        logger.info("[MCP] tools/call: %s %s", name,
                    json.dumps({k: (v if k != "text" else str(v)[:50] + "…")
                                for k, v in args.items()}, ensure_ascii=False)[:300])
        try:
            content = await asyncio.to_thread(fn, args, b)
            return _resp(mid, {"content": content, "isError": False})
        except Exception as e:
            logger.warning("[MCP] tools/call %s 失败: %s", name, e)
            return _resp(mid, {"content": [{"type": "text",
                                            "text": f"工具执行失败: {type(e).__name__}: {e}"}],
                               "isError": True})
    return _err(mid, -32601, f"method not found: {method}")


async def handle_payload(payload: bytes | str, base: str | None = None):
    """处理一段原始报文 (可能是批量/数组)。返回应发回的 JSON-RPC 响应 (None 表示无需应答)。"""
    try:
        data = json.loads(payload if isinstance(payload, str) else payload.decode("utf-8"))
    except Exception:
        return _err(None, -32700, "parse error: body is not valid JSON")
    msgs = data if isinstance(data, list) else [data]
    out = []
    for m in msgs:
        r = await handle_message(m, base=base)
        if r is not None:
            out.append(r)
    if not out:
        return None
    return out[0] if isinstance(data, dict) else out
