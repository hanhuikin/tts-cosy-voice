"""播客服务: 内容提取 / 对话稿生成 / 逐句合成(final+stream) / ffmpeg 推流管理。

合成走引擎的单 worker 队列 (asyncio), 以 asyncio.Task 在主循环上运行;
推流以 ffmpeg 子进程为出口, 逐句产出的 PCM 经 stdin 喂入。
"""
from __future__ import annotations

import asyncio
import io
import json
import logging
import re
import shutil
import subprocess
import threading
import time
from pathlib import Path
from typing import Callable, Optional

import requests

from .audio import pcm_to_wav, wav_header
from .engine import TTSRequest, engine
from .llm import SCRIPT_SYSTEM_PROMPT, chat_stream
from .storage import storage

logger = logging.getLogger("cosyvoice-server.podcast")

DATA_DIR = Path(__file__).resolve().parents[1] / "data"
LIVE_DIR = DATA_DIR / "live"
SAMPLE_RATE = 24000


# ---------- 内容提取 ----------

def extract_from_url(url: str, timeout: int = 30) -> str:
    """抓取网页正文 (bs4 去除脚本/样式), 返回纯文本。"""
    headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) CosyVoicePodcast/1.0"}
    r = requests.get(url, headers=headers, timeout=timeout)
    r.raise_for_status()
    r.encoding = r.apparent_encoding or "utf-8"
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(r.text, "html.parser")
    for tag in soup(["script", "style", "nav", "footer", "header", "aside", "noscript"]):
        tag.decompose()
    main = soup.find("article") or soup.find("main") or soup.body or soup
    text = re.sub(r"\n{3,}", "\n\n", main.get_text("\n", strip=True))
    if len(text) < 50:
        raise ValueError("页面正文提取失败 (可能为动态渲染页面), 请复制正文使用「粘贴文本」")
    return text[:20000]


def extract_from_file(filename: str, raw: bytes) -> str:
    """按扩展名解析文件纯文本 (txt/md 直接读; pdf 用 pypdf; docx 用 python-docx)。"""
    ext = Path(filename).suffix.lower()
    if ext in (".txt", ".md"):
        return raw.decode("utf-8", errors="replace")[:20000]
    if ext == ".pdf":
        from pypdf import PdfReader

        reader = PdfReader(io.BytesIO(raw))
        if reader.is_encrypted:
            raise ValueError("PDF 已加密, 无法解析")
        text = "\n".join((page.extract_text() or "") for page in reader.pages)
        if not text.strip():
            raise ValueError("PDF 无文本层 (可能为扫描件), 请改用粘贴文本")
        return text[:20000]
    if ext == ".docx":
        import docx

        doc = docx.Document(io.BytesIO(raw))
        text = "\n".join(p.text for p in doc.paragraphs if p.text.strip())
        if not text.strip():
            raise ValueError("DOCX 无文本内容")
        return text[:20000]
    raise ValueError(f"不支持的文件类型 {ext}")


# ---------- 对话稿生成 ----------

def generate_script(source_text: str, on_delta: Callable[[str], None]) -> list[dict]:
    """调用 LLM 将内容改写为对话稿; on_delta 接收增量输出 (供前端流式渲染)。
    返回解析后的行列表 [{role, text}]; 解析失败的行跳过。"""
    cfg_rounds = "10~16"
    user_prompt = (
        f"请把以下内容改写为 {cfg_rounds} 轮左右的双人播客对话稿。\n\n"
        f"【内容开始】\n{source_text[:12000]}\n【内容结束】"
    )
    messages = [
        {"role": "system", "content": SCRIPT_SYSTEM_PROMPT},
        {"role": "user", "content": user_prompt},
    ]
    raw = ""
    for delta in chat_stream(messages):
        raw += delta
        on_delta(delta)
    return parse_script(raw)


def parse_script(raw: str) -> list[dict]:
    """宽松解析 JSON Lines 对话稿。"""
    lines = []
    for ln in raw.splitlines():
        ln = ln.strip().strip("`")
        if not ln.startswith("{"):
            continue
        try:
            obj = json.loads(ln)
            role, text = obj.get("role"), (obj.get("text") or "").strip()
            if role in ("host", "guest") and text:
                lines.append({"role": role, "text": text})
        except json.JSONDecodeError:
            continue
    if not lines:
        raise ValueError("LLM 未返回有效对话稿 (响应格式异常), 请重试或调整模型")
    return lines[:60]


# ---------- 逐句合成 ----------

def resolve_line_request(line: dict, voice_ids: dict) -> TTSRequest:
    """把对话稿一行转为引擎请求 (解析音色参考并校验)。"""
    role = line.get("role", "host")
    voice_id = voice_ids.get(role) or voice_ids.get("host")
    voice = storage.get_voice(voice_id) if voice_id else None
    if not voice:
        raise ValueError(f"角色 {role} 未配置有效音色")
    req = TTSRequest(
        text=line["text"], model="cosyvoice3", mode="zero_shot",
        voice_id=voice_id, speed=float(line.get("speed", 1.0)), stream=False,
    )
    req.prompt_wav = voice["file"]
    req.prompt_text = voice["prompt_text"]
    if not Path(req.prompt_wav).exists():
        raise ValueError(f"参考音频不存在: {req.prompt_wav}")
    err = engine.validate(req)
    if err:
        raise ValueError(err)
    return req


async def synth_line_pcm(req: TTSRequest) -> bytes:
    """经引擎队列合成单句, 返回 int16 PCM。"""
    job = await engine.submit(req)
    parts: list[bytes] = []
    async for kind, payload in engine.drain(job):
        if kind == "chunk":
            parts.append(payload)
        elif kind == "error":
            raise RuntimeError(payload)
    return b"".join(parts)


async def stream_episode_gen(ep_id: str):
    """本地流式合成 (与工作台一致的输出形态): WAV 头 + 逐句 PCM 分块;
    播放器边播边收; 结束后完整 WAV 落盘为成品。"""
    ep = storage.get_episode(ep_id)
    lines = json.loads(ep["script_json"])
    voice_ids = {"host": ep["voice_host"], "guest": ep["voice_guest"]}
    yield wav_header(SAMPLE_RATE, 600 * SAMPLE_RATE)
    parts: list[bytes] = []
    done = 0
    status, error = "ok", None
    t0 = time.time()
    first_ms = None
    try:
        for line in lines:
            req = resolve_line_request(line, voice_ids)
            job = await engine.submit(req)
            async for kind, payload in engine.drain(job):
                if kind == "chunk":
                    parts.append(payload)
                    if first_ms is None:
                        first_ms = int((time.time() - t0) * 1000)
                    yield payload
                elif kind == "error":
                    raise RuntimeError(payload)
            done += 1
            storage.update_episode(ep_id, progress=done)
        status = "completed"
    except asyncio.CancelledError:
        status, error = "failed", "client disconnected"
        raise
    except Exception as e:
        logger.exception("episode %s 本地流式失败", ep_id)
        status, error = "failed", f"{type(e).__name__}: {e}"
    finally:
        out = DATA_DIR / "podcasts" / f"{ep_id}.wav" if parts else None
        if parts:
            out.write_bytes(pcm_to_wav(parts, SAMPLE_RATE))
        storage.update_episode(ep_id, status=status, error=error,
                               file=str(out) if parts else None,
                               duration=sum(len(p) for p in parts) / 2 / SAMPLE_RATE if parts else None,
                               progress=done)
        logger.info("episode %s 本地流式结束: %s, %d 句, %d bytes", ep_id, status, done, sum(len(p) for p in parts))


async def synth_episode_final(ep_id: str) -> None:
    """非流式: 逐句合成整段, 拼接为单 WAV 并落盘。后台任务。"""
    ep = storage.get_episode(ep_id)
    if not ep:
        return
    lines = json.loads(ep["script_json"])
    voice_ids = {"host": ep["voice_host"], "guest": ep["voice_guest"]}
    storage.update_episode(ep_id, status="synthesizing", progress=0, error=None)
    all_parts: list[bytes] = []
    done = 0
    try:
        for line in lines:
            req = resolve_line_request(line, voice_ids)
            pcm = await synth_line_pcm(req)
            all_parts.append(pcm)
            done += 1
            storage.update_episode(ep_id, progress=done)
        wav = pcm_to_wav(all_parts, SAMPLE_RATE)
        out = DATA_DIR / "podcasts" / f"{ep_id}.wav"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_bytes(wav)
        duration = sum(len(p) for p in all_parts) / 2 / SAMPLE_RATE
        storage.update_episode(ep_id, status="completed", file=str(out),
                               duration=round(duration, 2), progress=len(lines))
        logger.info("episode %s final合成完成: %.1fs", ep_id, duration)
    except Exception as e:
        logger.exception("episode %s 合成失败", ep_id)
        storage.update_episode(ep_id, status="failed", error=f"{type(e).__name__}: {e}")


# ---------- 推流 ----------

def find_ffmpeg() -> Optional[str]:
    """定位 ffmpeg: PATH 优先, 其次 winget 标准安装目录兜底。"""
    p = shutil.which("ffmpeg")
    if p:
        return p
    pkg_dir = Path.home() / "AppData" / "Local" / "Microsoft" / "WinGet" / "Packages"
    hits = sorted(pkg_dir.glob("Gyan.FFmpeg*/**/bin/ffmpeg.exe")) if pkg_dir.exists() else []
    return str(hits[-1]) if hits else None


def ffmpeg_available() -> bool:
    return find_ffmpeg() is not None


def build_ffmpeg_cmd(protocol: str, url: str, ep_id: str) -> list[str]:
    ffmpeg = find_ffmpeg() or "ffmpeg"
    base = [ffmpeg, "-hide_banner", "-loglevel", "warning",
            "-f", "s16le", "-ar", str(SAMPLE_RATE), "-ac", "1", "-i", "pipe:0",
            "-c:a", "aac", "-b:a", "128k"]
    if protocol == "rtmp":
        return base + ["-f", "flv", url]
    if protocol == "srt":
        return base + ["-f", "mpegts", url]
    if protocol == "rtsp":
        return base + ["-rtsp_transport", "tcp", "-f", "rtsp", url]
    if protocol == "hls":
        LIVE_DIR.mkdir(parents=True, exist_ok=True)
        seg = LIVE_DIR / f"{ep_id}_%03d.ts"
        out = LIVE_DIR / f"{ep_id}.m3u8"
        return base + ["-f", "hls", "-hls_time", "4", "-hls_list_size", "0",
                       "-hls_segment_filename", str(seg), str(out)]
    raise ValueError(f"不支持的推流协议: {protocol}")


class PushManager:
    """全局单推流会话 (本地工具定位): 管理 ffmpeg 子进程与喂流任务。"""

    def __init__(self) -> None:
        self.proc: Optional[subprocess.Popen] = None
        self.info: dict = {}
        self._task: Optional[asyncio.Task] = None
        self._stderr_tail = ""
        self._lock = asyncio.Lock()

    @property
    def active(self) -> bool:
        return self.proc is not None and self.proc.poll() is None

    def status(self) -> dict:
        info = dict(self.info)
        info["ffmpeg_available"] = ffmpeg_available()
        info["stderr_tail"] = self._stderr_tail[-300:]
        return info

    async def start(self, ep_id: str, protocol: str, url: str) -> dict:
        async with self._lock:
            if self.active:
                raise RuntimeError("已有推流进行中, 请先停止")
            if not ffmpeg_available():
                raise RuntimeError("未检测到 ffmpeg, 请先安装 (winget install Gyan.FFmpeg)")
            ep = storage.get_episode(ep_id)
            if not ep or not ep["script_json"]:
                raise ValueError("episode 不存在或对话稿为空")
            cmd = build_ffmpeg_cmd(protocol, url, ep_id)
            LIVE_DIR.mkdir(parents=True, exist_ok=True)
            self.proc = subprocess.Popen(cmd, stdin=subprocess.PIPE,
                                         stdout=subprocess.DEVNULL,
                                         stderr=subprocess.PIPE)
            self._stderr_tail = ""
            threading.Thread(target=self._drain_stderr, args=(self.proc,), daemon=True).start()
            self.info = {"episode_id": ep_id, "protocol": protocol, "url": url,
                         "status": "pushing", "started_at": time.strftime("%H:%M:%S"),
                         "lines_done": 0, "lines_total": len(json.loads(ep["script_json"]))}
            storage.update_episode(ep_id, status="pushing", error=None)
            self._task = asyncio.create_task(self._feed(ep_id, protocol, url))
            return self.status()

    def _drain_stderr(self, proc: subprocess.Popen) -> None:
        try:
            for line in iter(proc.stderr.readline, b""):
                self._stderr_tail += line.decode("utf-8", errors="replace")
        except Exception:
            pass

    async def _feed(self, ep_id: str, protocol: str, url: str) -> None:
        """逐句合成并写入 ffmpeg stdin; 结束后关闭管道并落盘完整 WAV。"""
        proc = self.proc
        loop = asyncio.get_running_loop()
        ep = storage.get_episode(ep_id)
        lines = json.loads(ep["script_json"])
        voice_ids = {"host": ep["voice_host"], "guest": ep["voice_guest"]}
        all_parts: list[bytes] = []
        done = 0
        status, error = "completed", None
        try:
            for line in lines:
                if proc.poll() is not None:  # ffmpeg 提前退出
                    raise RuntimeError(f"ffmpeg 提前退出: {self._stderr_tail[-200:]}")
                req = resolve_line_request(line, voice_ids)
                pcm = await synth_line_pcm(req)
                all_parts.append(pcm)
                await loop.run_in_executor(None, self._write_stdin, proc, pcm)
                done += 1
                self.info["lines_done"] = done
                storage.update_episode(ep_id, progress=done)
        except Exception as e:
            logger.exception("推流中断")
            status, error = "failed", f"{type(e).__name__}: {e}"
            storage.update_episode(ep_id, status="failed", error=error)
        finally:
            try:
                if proc.stdin and not proc.stdin.closed:
                    proc.stdin.close()
                proc.wait(timeout=10)
            except Exception:
                proc.kill()
            if all_parts:
                wav = pcm_to_wav(all_parts, SAMPLE_RATE)
                out = DATA_DIR / "podcasts" / f"{ep_id}.wav"
                out.parent.mkdir(parents=True, exist_ok=True)
                out.write_bytes(wav)
                storage.update_episode(ep_id, status=status if status == "completed" else "failed",
                                       file=str(out), error=error,
                                       duration=sum(len(p) for p in all_parts) / 2 / SAMPLE_RATE,
                                       progress=done)
            self.info["status"] = status
            self.info["error"] = error
            self.proc = None

    def _write_stdin(self, proc: subprocess.Popen, data: bytes) -> None:
        proc.stdin.write(data)
        proc.stdin.flush()

    def stop(self) -> dict:
        if self.proc:
            try:
                self.proc.stdin.close()  # 优雅收尾: ffmpeg 写完 trailer 退出
                self.proc.wait(timeout=5)
            except Exception:
                self.proc.kill()
            self.proc = None
        if self._task:
            self._task.cancel()
        self.info["status"] = "stopped"
        return self.status()


push_manager = PushManager()
