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
from typing import Optional

import requests

from .audio import pcm_to_wav, wav_header
from .engine import TTSRequest, engine
from .model_registry import MODEL_REGISTRY
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


# ---------- 对话稿解析 ----------

def parse_script(raw: str) -> list[dict]:
    """宽松解析 JSON Lines 对话稿; 行可携带说话人姓名 (多嘉宾场景)。"""
    lines = []
    for ln in raw.splitlines():
        ln = ln.strip().strip("`")
        if not ln.startswith("{"):
            continue
        try:
            obj = json.loads(ln)
            role, text = obj.get("role"), (obj.get("text") or "").strip()
            if role in ("host", "guest") and text:
                entry = {"role": role, "text": text}
                name = (obj.get("name") or "").strip()
                if name:
                    entry["name"] = name
                lines.append(entry)
        except json.JSONDecodeError:
            continue
    if not lines:
        raise ValueError("LLM 未返回有效对话稿 (响应格式异常), 请重试或调整模型")
    return lines[:60]


# ---------- 逐句合成 ----------

def episode_speakers(ep: dict) -> list[dict]:
    """剧集的说话人阵容: 优先 speakers_json; 旧数据从 voice_host/voice_guest 兼容构造。
    每项: {role: host|guest, name, desc, voice_id}。"""
    raw = ep.get("speakers_json")
    if raw:
        try:
            spks = json.loads(raw)
            if isinstance(spks, list) and spks:
                return spks
        except json.JSONDecodeError:
            pass
    out = []
    if ep.get("voice_host"):
        out.append({"role": "host", "name": "主播", "desc": "", "voice_id": ep["voice_host"]})
    if ep.get("voice_guest"):
        out.append({"role": "guest", "name": "嘉宾", "desc": "", "voice_id": ep["voice_guest"]})
    return out or [{"role": "host", "name": "主播", "desc": "", "voice_id": ""}]


def resolve_line_request(line: dict, speakers: list[dict]) -> TTSRequest:
    """把对话稿一行转为引擎请求: 说话人姓名精确匹配音色, 未命中回退同角色第一位, 再回退首位。"""
    name = (line.get("name") or "").strip()
    spk = None
    if name:
        spk = next((s for s in speakers if (s.get("name") or "").strip() == name), None)
    if spk is None:
        role = line.get("role", "host")
        spk = next((s for s in speakers if s.get("role") == role), None) or speakers[0]
    voice_id = (spk.get("voice_id") or "").strip()
    voice = storage.get_voice(voice_id) if voice_id else None

    # 播客跟随工作台当前已加载的模型 (而不是写死 cosyvoice3)
    status = engine.status()
    model = status["model"] or "cosyvoice3"
    cfg = MODEL_REGISTRY[model]
    clone_mode = next((m for m in ("zero_shot", "cross_lingual") if m in cfg["modes"]), None)
    spk_id = None
    if clone_mode:
        if not voice:
            raise ValueError(f"说话人「{spk.get('name') or line.get('role', 'host')}」未配置有效音色")
        mode = clone_mode
    else:
        # 没有克隆能力的模型 (如 Qwen3-TTS CustomVoice): 按角色分配内置说话人
        available = status.get("speakers") or []
        if not available:
            raise ValueError(f"当前模型「{cfg['label']}」既不支持声音克隆也没有内置说话人, "
                             "请切换到支持克隆的模型后再合成播客")
        idx = next((i for i, s in enumerate(speakers) if s is spk), 0)
        mode, spk_id = "sft", available[idx % len(available)]

    speed = float(line.get("speed", 1.0))
    # 原生流式模型 (如 CosyVoice 3) 边合成边逐块下发; 缓冲型模型 (IndexTTS/Qwen3-TTS)
    # 保持按句粒度整段下发 —— 若写死 stream=False, 一整句合成完之前前端收不到任何字节,
    # 表现为「流式播放没有波形、没有声音」
    stream = cfg["stream_mode"] == "native" and not (
        model == "cosyvoice2" and abs(speed - 1.0) > 1e-6)
    req = TTSRequest(
        text=line["text"], model=model, mode=mode, spk_id=spk_id,
        voice_id=voice_id if clone_mode else None,
        speed=speed, stream=stream,
    )
    if clone_mode:
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
    speakers = episode_speakers(ep)
    yield wav_header(SAMPLE_RATE, 600 * SAMPLE_RATE)
    parts: list[bytes] = []
    done = 0
    status, error = "ok", None
    t0 = time.time()
    first_ms = None
    try:
        for line in lines:
            req = resolve_line_request(line, speakers)
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
    speakers = episode_speakers(ep)
    storage.update_episode(ep_id, status="synthesizing", progress=0, error=None)
    all_parts: list[bytes] = []
    done = 0
    try:
        for line in lines:
            req = resolve_line_request(line, speakers)
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


def build_ffmpeg_cmd(protocol: str, url: str, ep_id: str, input_wav: Optional[str] = None) -> list[str]:
    """构建推流命令。-re 使 ffmpeg 按实时节拍读取: 产出不及时时输出流保持连续
    (延迟增长) 而非断续。input_wav 为空时从 stdin 读 PCM。"""
    ffmpeg = find_ffmpeg() or "ffmpeg"
    base = [ffmpeg, "-hide_banner", "-loglevel", "warning", "-re"]
    if input_wav:
        base += ["-i", input_wav]
    else:
        base += ["-f", "s16le", "-ar", str(SAMPLE_RATE), "-ac", "1", "-i", "pipe:0"]
    base += ["-c:a", "aac", "-b:a", "128k"]
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
    """全局单推流会话。

    三阶段 (防断续):
    1. preparing  — 开播前预缓冲: 先合成 ~20s 音频填满观众端播放缓冲
    2. pushing    — ffmpeg 以 -re 实时节拍消费, 逐句边合成边补货;
                    产出不及时时输出流保持连续 (延迟增长), 不产生静音缝隙
    3. completed  — 全部句合成完, 关闭管道收尾, 完整 WAV 落盘为成品

    若剧集已有成品 WAV (final 已合成过), 直接推成品文件 (零延迟增长)。
    """

    PREBUFFER_SECONDS = 20

    def __init__(self) -> None:
        self.proc: Optional[subprocess.Popen] = None
        self.info: dict = {}
        self._task: Optional[asyncio.Task] = None
        self._stderr_tail = ""
        self._stop_flag = False
        self._lock = asyncio.Lock()

    @property
    def active(self) -> bool:
        return (self._task is not None and not self._task.done()) or \
               (self.proc is not None and self.proc.poll() is None)

    def status(self) -> dict:
        info = dict(self.info)
        info["ffmpeg_available"] = ffmpeg_available()
        info["stderr_tail"] = self._stderr_tail[-300:]
        # 时间进度 (file 模式按实时速率, elapsed 即已推送秒数)
        if self.info.get("started_epoch") and self.info.get("duration"):
            if self.info.get("status") in ("preparing", "pushing"):
                info["elapsed_s"] = round(max(0, time.time() - self.info["started_epoch"]), 1)
            else:
                info["elapsed_s"] = round(min(self.info["duration"],
                                              self.info.get("elapsed_s") or self.info["duration"]), 1)
            info["duration_s"] = self.info["duration"]
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
            self._stop_flag = False
            self._stderr_tail = ""
            self.info = {"episode_id": ep_id, "protocol": protocol, "url": url,
                         "status": "preparing", "lines_done": 0,
                         "lines_total": len(json.loads(ep["script_json"])),
                         "duration": ep.get("duration") or 0,
                         "started_epoch": time.time(),
                         "mode": "file" if self._ep_has_final(ep) else "live"}
            storage.update_episode(ep_id, status="pushing", error=None)
            self._task = asyncio.create_task(self._run(ep_id, protocol, url))
            return self.status()

    @staticmethod
    def _ep_has_final(ep: dict) -> bool:
        return bool(ep.get("file") and Path(ep["file"]).exists())

    def _drain_stderr(self, proc: subprocess.Popen) -> None:
        try:
            for line in iter(proc.stderr.readline, b""):
                self._stderr_tail += line.decode("utf-8", errors="replace")
        except Exception:
            pass

    def _spawn(self, cmd: list[str]) -> subprocess.Popen:
        proc = subprocess.Popen(cmd, stdin=subprocess.PIPE,
                                stdout=subprocess.DEVNULL,
                                stderr=subprocess.PIPE)
        self.proc = proc
        threading.Thread(target=self._drain_stderr, args=(proc,), daemon=True).start()
        return proc

    async def _run(self, ep_id: str, protocol: str, url: str) -> None:
        loop = asyncio.get_running_loop()
        ep = storage.get_episode(ep_id)
        lines = json.loads(ep["script_json"])
        speakers = episode_speakers(ep)
        status, error = "completed", None
        proc: Optional[subprocess.Popen] = None
        all_parts: list[bytes] = []
        done = 0
        try:
            if self.info["mode"] == "file":
                # 成品直推: 完整文件 + -re, 平滑无缺口
                self.info["status"] = "pushing"
                cmd = build_ffmpeg_cmd(protocol, url, ep_id, input_wav=ep["file"])
                proc = self._spawn(cmd)
                self.proc = proc
                await loop.run_in_executor(None, proc.wait)
                if proc.returncode not in (0, None):
                    raise RuntimeError(f"ffmpeg 退出码 {proc.returncode}: {self._stderr_tail[-200:]}")
            else:
                # 阶段 1: 预缓冲
                reservoir: list[bytes] = []
                rsamples = 0
                need = self.PREBUFFER_SECONDS * SAMPLE_RATE
                idx = 0
                while idx < len(lines) and rsamples < need and not self._stop_flag:
                    pcm = await synth_line_pcm(resolve_line_request(lines[idx], speakers))
                    reservoir.append(pcm)
                    rsamples += len(pcm) // 2
                    idx += 1
                    done += 1
                    self.info["lines_done"] = done
                    storage.update_episode(ep_id, progress=done)
                # 阶段 2: 起播 + 边合成边补货
                self.info["status"] = "pushing"
                cmd = build_ffmpeg_cmd(protocol, url, ep_id)
                proc = self._spawn(cmd)
                self.proc = proc
                await loop.run_in_executor(None, self._write_stdin, proc, b"".join(reservoir))
                while idx < len(lines):
                    if self._stop_flag:
                        status, error = "stopped", "手动停止"
                        break
                    pcm = await synth_line_pcm(resolve_line_request(lines[idx], speakers))
                    all_parts.append(pcm)
                    await loop.run_in_executor(None, self._write_stdin, proc, pcm)
                    idx += 1
                    done += 1
                    self.info["lines_done"] = done
                    storage.update_episode(ep_id, progress=done)
                if status != "stopped":
                    if proc.stdin and not proc.stdin.closed:
                        proc.stdin.close()
                    await loop.run_in_executor(None, proc.wait)
                    if proc.returncode != 0:
                        raise RuntimeError(f"ffmpeg 退出码 {proc.returncode}: {self._stderr_tail[-200:]}")
                # 预缓冲部分计入成品
                all_parts = reservoir + all_parts
        except Exception as e:
            logger.exception("推流中断")
            status, error = "failed", f"{type(e).__name__}: {e}"
            storage.update_episode(ep_id, status="failed", error=error)
        finally:
            if proc is not None:
                try:
                    if proc.stdin and not proc.stdin.closed:
                        proc.stdin.close()
                    proc.wait(timeout=5)
                except Exception:
                    proc.kill()
                self.proc = None
            if all_parts:
                wav = pcm_to_wav(all_parts, SAMPLE_RATE)
                out = DATA_DIR / "podcasts" / f"{ep_id}.wav"
                out.parent.mkdir(parents=True, exist_ok=True)
                out.write_bytes(wav)
                storage.update_episode(ep_id, file=str(out),
                                       duration=sum(len(p) for p in all_parts) / 2 / SAMPLE_RATE,
                                       progress=done)
            # 手动停止: 成品直推的音频本身完整(仍算 completed), 实时推流则标记 stopped。
            # 无论如何都落库, 否则剧集状态会一直卡在 pushing (任务队列误显示推流中)。
            if self._stop_flag and status == "completed" and self.info.get("mode") != "file":
                status, error = "stopped", "手动停止"
            storage.update_episode(ep_id, status=status, error=error)
            self.info["status"] = status
            self.info["error"] = error
            self.proc = None

    def _write_stdin(self, proc: subprocess.Popen, data: bytes) -> None:
        proc.stdin.write(data)
        proc.stdin.flush()

    def stop(self) -> dict:
        self._stop_flag = True
        if self.proc:
            try:
                self.proc.stdin.close()
                self.proc.wait(timeout=1)   # 给 ffmpeg 1s 优雅退出, 否则强杀 (接口响应不被拖慢)
            except Exception:
                self.proc.kill()
            self.proc = None
        if self._task:
            self._task.cancel()
        self.info["status"] = "stopped"
        return self.status()


push_manager = PushManager()
