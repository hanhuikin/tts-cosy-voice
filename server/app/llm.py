"""大模型配置与 OpenAI 兼容调用。

配置持久化到 server/data/llm_config.json (已在 .gitignore 的 server/data/ 内),
api_key 仅存本机, 接口返回时脱敏。
"""
from __future__ import annotations

import json
import logging
import threading
import time
from pathlib import Path
from typing import Iterator, Optional

import requests

logger = logging.getLogger("cosyvoice-server.llm")

DATA_DIR = Path(__file__).resolve().parents[1] / "data"
CONFIG_PATH = DATA_DIR / "llm_config.json"

DEFAULT_CONFIG = {
    "base_url": "",
    "api_key": "",
    "model": "",
    "temperature": 0.7,
    "rounds_hint": "10~16",
    "style_prompt": "生成中文播客对话：一问一答、口语化、有观点交锋；开头向听众问好并概括本期主题，结尾引导订阅。",
}

_lock = threading.Lock()


def load_config() -> dict:
    if CONFIG_PATH.exists():
        try:
            cfg = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
            return {**DEFAULT_CONFIG, **cfg}
        except Exception:
            logger.warning("llm_config.json 解析失败, 使用默认配置")
    return dict(DEFAULT_CONFIG)


def save_config(cfg: dict) -> dict:
    with _lock:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        merged = {**load_config(), **{k: v for k, v in cfg.items() if v is not None}}
        CONFIG_PATH.write_text(json.dumps(merged, ensure_ascii=False, indent=2), encoding="utf-8")
    return masked(merged)


def masked(cfg: dict) -> dict:
    out = dict(cfg)
    key = out.get("api_key") or ""
    out["api_key_masked"] = (key[:4] + "•" * 8 + key[-4:]) if len(key) > 8 else ("•" * 8 if key else "")
    out.pop("api_key", None)
    return out


def get_api_key() -> str:
    return load_config().get("api_key", "")


class LLMError(RuntimeError):
    pass


def _headers(cfg: dict) -> dict:
    return {"Authorization": f"Bearer {cfg['api_key']}", "Content-Type": "application/json"}


def test_connect(timeout: int = 15) -> dict:
    """连通性测试: GET /models 或最小 chat 调用, 返回延迟与模型名。"""
    cfg = load_config()
    if not cfg["base_url"] or not cfg["api_key"]:
        raise LLMError("请先填写 API 地址与 API Key")
    t0 = time.time()
    base = cfg["base_url"].rstrip("/")
    try:
        r = requests.get(f"{base}/models", headers=_headers(cfg), timeout=timeout)
        if r.ok:
            models = r.json().get("data", [])
            name = cfg["model"] or (models[0]["id"] if models else "unknown")
            return {"latency_ms": int((time.time() - t0) * 1000), "model": name, "via": "/models"}
        raise LLMError(f"/models {r.status_code}: {r.text[:120]}")
    except requests.RequestException as e:
        raise LLMError(f"连接失败: {type(e).__name__}: {e}")


def chat_stream(messages: list[dict], max_tokens: int = 4096) -> Iterator[str]:
    """OpenAI 兼容流式对话, 逐段产出增量文本。"""
    cfg = load_config()
    if not (cfg["base_url"] and cfg["api_key"] and cfg["model"]):
        raise LLMError("请先完成大模型配置 (API 地址 / Key / 模型)")
    payload = {
        "model": cfg["model"],
        "messages": messages,
        "temperature": float(cfg.get("temperature", 0.7)),
        "max_tokens": max_tokens,
        "stream": True,
    }
    url = cfg["base_url"].rstrip("/") + "/chat/completions"
    with requests.post(url, headers=_headers(cfg), json=payload, stream=True, timeout=180) as r:
        if not r.ok:
            raise LLMError(f"LLM {r.status_code}: {r.text[:200]}")
        for line in r.iter_lines(decode_unicode=True):
            if not line or not line.startswith("data:"):
                continue
            data = line[5:].strip()
            if data == "[DONE]":
                break
            try:
                delta = json.loads(data)["choices"][0]["delta"].get("content")
                if delta:
                    yield delta
            except (json.JSONDecodeError, KeyError, IndexError):
                continue


SCRIPT_SYSTEM_PROMPT = (
    "你是一名中文播客策划与编剧。把用户给到的内容改写为双人播客对话稿(主播 host 与嘉宾 guest)。\n"
    "严格要求: 逐行输出 JSON, 每行一个对象, 格式为 {\"role\":\"host\"|\"guest\", \"text\":\"台词\"}, "
    "不要输出 JSON 数组符号, 不要输出任何解释文字或 markdown 代码块标记。\n"
    "风格: 口语化、自然、有来有回、有观点交锋; 开头向听众问好并概括主题, 结尾总结并引导订阅。\n"
)


def script_system_prompt(speakers: list[dict], style_prompt: str = "") -> str:
    """多人对话稿 system 提示: 内嵌说话人名单(姓名/角色/人设描述), 供大模型关联生成。
    speakers 为空时退回默认双人模板。每项: {role: host|guest, name, desc}。"""
    if not speakers:
        return SCRIPT_SYSTEM_PROMPT
    roster = []
    for s in speakers:
        tag = "主播" if s.get("role") == "host" else "嘉宾"
        name = (s.get("name") or "").strip() or tag
        desc = (s.get("desc") or "").strip()
        roster.append(f"- {name}（{tag}）{('：' + desc) if desc else ''}")
    example_guest = next((((s.get("name") or "").strip() or "嘉宾")
                          for s in speakers if s.get("role") != "host"), "嘉宾")
    example = json.dumps({"role": "guest", "name": example_guest, "text": "台词"}, ensure_ascii=False)
    return (
        "你是一名中文播客策划与编剧。把用户给到的内容改写为多人播客对话稿。\n"
        f"本期说话人阵容（共 {len(speakers)} 位）:\n" + "\n".join(roster) + "\n"
        "严格要求: 逐行输出 JSON, 每行一个对象, 形如 " + example + ", "
        "其中 role 只能取 \"host\" 或 \"guest\", name 必须从上方名单中选取且与身份一致, "
        "台词开头不要带姓名或冒号前缀; 不要输出 JSON 数组符号, 不要输出任何解释文字或 markdown 代码块标记。\n"
        "对话要求: 每位说话人都要有发言; 主播负责开场、串场与追问, 嘉宾围绕自己的人设定位输出专业观点; "
        "风格: 口语化、自然、有来有回、有观点交锋; 开头向听众问好并概括主题, 结尾总结并引导订阅。\n"
        + (f"额外风格要求: {style_prompt.strip()}\n" if style_prompt.strip() else "")
    )
