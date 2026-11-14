# CosyVoice TTS Server — HTTP API 与 MCP 接口文档

> 交互式调试: 服务启动后打开 `http://127.0.0.1:<port>/docs` (Swagger UI, 全部端点带中文摘要)。
> 本文档与代码同步维护; 端点实现见 `server/app/routes/`。

## 目录

- [基础约定](#基础约定)
- [调用来源标识与控制台日志](#调用来源标识与控制台日志)
- [系统状态与管理](#系统状态与管理)
- [TTS 合成](#tts-合成)
- [OpenAI 兼容接口](#openai-兼容接口)
- [音色管理](#音色管理)
- [任务历史](#任务历史)
- [播客工坊](#播客工坊)
- [MCP 接入 (stdio / SSE / Streamable HTTP)](#mcp-接入)
- [错误码约定](#错误码约定)

## 基础约定

| 项 | 值 |
|---|---|
| Base URL | `http://127.0.0.1:8001` (默认端口 8000, 本仓库以 8001 运行) |
| 音频编码 | WAV (PCM int16), 采样率随模型 (CosyVoice 24000Hz) |
| 请求编码 | `application/json; charset=utf-8` (音色上传为 multipart/form-data) |
| 响应编码 | UTF-8 JSON; 音频二进制以 `audio/wav` / `audio/mpeg` 返回 |
| 鉴权 | 无 (内网工具定位); 不要暴露到公网 |
| 并发 | 推理单 worker 串行 + 队列上限 16, 排队满返回 429 |
| 成品落盘 | 每次合成自动落盘 `server/data/tasks/<task_id>.wav`, 可随时回捞 |

通用响应头:

| 响应头 | 说明 |
|---|---|
| `X-Task-Id` | 任务 id (任务历史/停止/回捞音频都靠它) |
| `X-Sample-Rate` | 本任务实际输出采样率 |
| `X-Duration-Ms` | 音频时长 (毫秒, 非流式) |
| `X-Infer-Ms` | 服务端推理总耗时 |

## 调用来源标识与控制台日志

每个合成任务都会记录**调用来源类型** `source`, 并在服务控制台打印一条「音频产出」审计日志:

```
🔊 [音频产出] 来源=mcp:tts_synthesize | 任务=f64ff6d38eff | 状态=ok | 模型=cosyvoice3 | 模式=zero_shot | 音色=05c4d05b680d | 时长=3.84s | 推理=4.52s | 首块=4506ms | 采样率=24000 | 文件=D:\...\tasks\f64ff6d38eff.wav
```

| 调用途径 | source 值 |
|---|---|
| `POST /api/tts` (默认) | `api:/api/tts` |
| Web 前端 | 请求头 `X-Client-Source: web-ui` (自定义值直接透传) |
| `POST /v1/audio/speech` | `openai:/v1/audio/speech` |
| MCP 工具调用 | `mcp:<工具名>` (如 `mcp:tts_synthesize`) |
| 播客合成 (final/stream) | `播客合成(final)` / `播客合成(stream)` (剧集级日志, 无任务记录) |

- 客户端可通过请求头 `X-Client-Source` 自定义来源 (≤120 字符, 超长截断)
- `source` 字段随 `GET /api/tasks`、`GET /api/tasks/{id}` 返回, 可用于区分「音频是哪种调用产生的」
- 播客合成不走 `/api/tts` (逐句直入引擎队列), 故打剧集级日志

## 系统状态与管理

### GET /api/health — 服务健康状态

```bash
curl http://127.0.0.1:8001/api/health
```

```json
{
  "state": "ready",              // idle | loading | ready | failed
  "model": "cosyvoice3",
  "model_label": "CosyVoice 3.0 (0.5B)",
  "backend": "inprocess",
  "stream_mode": "native",       // native=逐块流式; chunked=整段后分块
  "sample_rate": 24000,
  "gpu": {"name": "NVIDIA GeForce RTX 5070 Ti", "vram_free_gb": 11.27, "vram_total_gb": 15.92},
  "queue_len": 0, "queue_max": 16,
  "speakers": [],                // 内置说话人 (sft 模型才有)
  "voices_count": 14,
  "models": [ /* 每个模型的 key/label/modes/available/loaded/... */ ]
}
```

### 其他系统端点

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/models` | 模型注册表与加载状态 (modes/speaker_modes/stream_mode/instruct_kind…) |
| POST | `/api/admin/model` | 切换/加载模型, body `{"model": "cosyvoice2"}`; 加载期间再切返回 409 |
| GET | `/api/models/{key}/doctor` | 模型就绪诊断: 权重文件/解释器/依赖 (不加载、不占显存) |
| GET | `/api/admin/config` | 权重目录/运行环境只读视图 (改配置需重启, 见 STARTUP.md) |
| GET | `/api/events` | SSE 实时事件流: 引擎状态/队列变化 (浏览器 EventSource 可直接接) |

## TTS 合成

### POST /api/tts — 统一合成入口

```bash
curl -X POST http://127.0.0.1:8001/api/tts \
  -H "Content-Type: application/json" \
  -H "X-Client-Source: my-app" \
  -d '{"text": "你好,欢迎使用语音合成。", "voice_id": "05c4d05b680d", "mode": "zero_shot"}' \
  --output out.wav
```

请求体:

| 字段 | 类型 | 默认 | 说明 |
|---|---|---|---|
| `text` | string | 必填 | 待合成文本, 1~2000 字 |
| `model` | string | `cosyvoice3` | 模型 key: `cosyvoice3` / `cosyvoice2` / `indextts2` / `indextts25` / `qwen3tts` / `qwen3tts_base` / `qwen3tts_design` |
| `mode` | string | `sft` | `sft`(内置说话人) / `zero_shot`(参考音频克隆) / `cross_lingual`(跨语言克隆) / `instruct`(声音描述) |
| `voice_id` | string | null | 音色库音色 id (GET /api/voices 获取); 提供时忽略 spk_id |
| `spk_id` | string | null | 内置说话人名 (仅 sft 模型) |
| `prompt_text` | string | null | 参考音频文本 (voice_id 有存档时自动读取) |
| `instruct_text` | string | null | 声音描述 (instruct 模式), 如「用四川话说这句话」 |
| `speed` | float | 1.0 | 0.5~2.0; v2 模型流式下强制 1.0 |
| `stream` | bool | false | true 时流式输出 (WAV 头 + 逐块 PCM) |

> 模式与模型的兼容性见 `GET /api/models` 的 `modes` / `speaker_modes`。
> 指定声音三选一: `voice_id` / `spk_id` / `instruct_text`。

响应:

- **非流式**: 完整 WAV 二进制 + 通用响应头; 任务自动落盘
- **流式**: `audio/wav` 流 (WAV 头占位 600s + 逐块 PCM); 断开时已生成部分仍落盘, 任务标 failed/`client disconnected`
- 请求头 `X-Client-Task-Id` 可预生成任务 id (生成中即可调停止接口)

### POST /api/tasks/{task_id}/stop — 停止合成

排队中的直接跳过; 推理中的在下一个分块边界停止。已生成部分保留落盘, 任务标 failed/`已停止`。

```bash
curl -X POST http://127.0.0.1:8001/api/tasks/f64ff6d38eff/stop
```

## OpenAI 兼容接口

### POST /v1/audio/speech

```bash
curl -X POST http://127.0.0.1:8001/v1/audio/speech \
  -H "Content-Type: application/json" \
  -d '{"model": "cosyvoice3", "input": "今天天气不错。", "voice": "05c4d05b680d"}' \
  --output speech.wav
```

| 字段 | 说明 |
|---|---|
| `input` | 待合成文本 (≤2000 字) |
| `voice` | 内置说话人名 / 音色库 voice_id (裸 id 或 `voice:<id>`、`v:<id>` 前缀均可, 自动走克隆) |
| `model` | 模型 key, 默认 `cosyvoice3` |
| `speed` | 0.5~2.0, 默认 1.0 |
| `response_format` | 仅支持 `wav` |

## 音色管理

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/voices` | 音色列表: `{id, name, prompt_text, duration, sample_rate, created_at}` |
| POST | `/api/voices` | 上传参考音频克隆音色 (multipart): `name`, `prompt_text`(参考音频的文字转写), `file`(wav/mp3/flac/m4a/ogg/opus, 1~30s, ≤50MB) |
| PUT | `/api/voices/{vid}` | 编辑名称/参考文本, body `{"name"?, "prompt_text"?}` |
| GET | `/api/voices/{vid}/prompt` | 试听参考音频 (WAV) |
| DELETE | `/api/voices/{vid}` | 删除音色 |
| GET | `/api/speakers` | 当前已加载模型的内置说话人列表 |

```bash
curl -X POST http://127.0.0.1:8001/api/voices \
  -F "name=我的音色" -F "prompt_text=参考音频里说的话" -F "file=@ref.wav"
```

## 任务历史

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/tasks?limit=100&offset=0&mode=&model=` | 任务列表 (含 `source` 来源字段), 最新在前 |
| GET | `/api/tasks/{tid}` | 单条详情 (状态/耗时/参数/来源) |
| GET | `/api/tasks/{tid}/audio?format=wav` | 下载音频; `format=mp3` 服务端转码 (需 ffmpeg, 缺失返回 503) |
| DELETE | `/api/tasks/{tid}` | 删除任务记录与音频文件 |

任务对象: `{id, text, model, mode, voice_id, speed, stream, status(ok/failed/running),
duration, sample_rate, infer_ms, first_chunk_ms, error, source, created_at}`。

## 播客工坊

### LLM 配置与对话稿生成

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/llm/config` | 读取 LLM 配置 (api_key 脱敏) |
| PUT | `/api/llm/config` | 保存: `base_url` / `api_key` / `model` / `temperature` / `rounds_hint` / `style_prompt` |
| POST | `/api/llm/test` | 测试连通性 |
| POST | `/api/podcast/source/extract` | 内容提取: multipart 文件 (≤20MB) 或 JSON `{"url"}`, 返回 `{text, chars}` |
| POST | `/api/podcast/script` | LLM 流式生成对话稿: `{"text", "speakers":[{role,name,desc}]}`, 响应为纯文本增量 |
| POST | `/api/podcast/parse-script` | 原始 LLM 输出 → 结构化对话稿行 (含说话人归一化) |

### 剧集管理

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/api/podcast/episodes` | 创建剧集: `{title, lines:[{role,name,text,voice_id?}], speakers?, speed?}` |
| GET | `/api/podcast/episodes?limit=50` | 列表 (含 lines_count / 实时推流状态) |
| GET | `/api/podcast/episodes/{pid}` | 详情 (script 逐句 / speakers / push 状态) |
| PUT | `/api/podcast/episodes/{pid}` | 更新 |
| DELETE | `/api/podcast/episodes/{pid}` | 删除 (推流中返回 409) |

对话稿行: `{"role": "host"|"guest", "name": "林夕", "text": "…", "voice_id": "可选, 缺省按 speakers"}`。

### 合成与推流

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/api/podcast/episodes/{pid}/synthesize` | body `{"mode": "final"}` 后台落盘合成 / `{"mode": "stream"}` 本地流式 (WAV 头+逐句 PCM, 带 LNSYNC 行标记) |
| GET | `/api/podcast/episodes/{pid}/status` | `{status, progress, duration, error, lines_total}` |
| POST | `/api/podcast/episodes/{pid}/stop` | 停止合成 (当前句分块边界结束, 已合成句保留) |
| GET | `/api/podcast/episodes/{pid}/audio?format=wav` | 成品音频 (mp3 需 ffmpeg) |
| POST | `/api/podcast/episodes/{pid}/push` | 推流: `{"protocol": "rtmp|srt|rtsp|hls", "url": "..."}` |
| DELETE | `/api/podcast/episodes/{pid}/push` | 停止推流 |
| GET | `/api/podcast/push/status` | 推流状态 |
| GET | `/api/podcast/ffmpeg` | ffmpeg 可用性 |

剧集状态机: `draft → synthesizing → completed / stopped / failed`。

## MCP 接入

服务内置 **Model Context Protocol** 支持, 三种传输共用同一套 13 个工具
(实现: `server/app/mcp_core.py`):

### 工具目录

| 工具 | 参数 | 说明 |
|---|---|---|
| `server_status` | — | 引擎状态/模型/GPU/队列/音色数 |
| `list_voices` | — | 音色库列表 (id/名称/参考文本) |
| `tts_synthesize` | `text*`, `voice_id`, `spk_id`, `instruct_text`, `model`, `speed`, `mode`, `include_base64` | 同步合成整段 (≤2000 字); 声音三选一; 返回本地文件路径+下载链接 |
| `list_tasks` | `limit?`, `status?` | 任务历史 (含 source 来源) |
| `get_task` | `task_id*` | 单任务详情 |
| `get_task_audio` | `task_id*`, `format?` | 下载任务音频到本地 |
| `stop_task` | `task_id*` | 停止进行中的合成 |
| `podcast_list_episodes` | `limit?` | 剧集列表 |
| `podcast_get_episode` | `episode_id*` | 剧集详情 (对话稿/阵容/进度) |
| `podcast_create_episode` | `title*`, `lines*`, `speakers?`, `speed?` | 从对话稿创建剧集 |
| `podcast_synthesize` | `episode_id*`, `wait?=true`, `timeout_s?=900` | 触发合成; 默认阻塞等待完成 |
| `podcast_get_audio` | `episode_id*`, `format?` | 下载成品音频 |
| `podcast_stop` | `episode_id*` | 停止合成 |

工具产出音频统一落盘 `server/data/mcp/` (可用环境变量 `COSYVOICE_MCP_DOWNLOAD_DIR` 改向)。
所有 MCP 工具调用 REST 时带 `X-Client-Source: mcp:<工具名>` → 服务控制台「音频产出」日志可区分来源。

### 传输 1: stdio (客户端拉起子进程)

入口 `server/mcp_server.py` (零第三方依赖); 环境变量 `COSYVOICE_MCP_BASE` 指定服务地址。

```jsonc
// 通用 stdio 配置 (ZCode / Claude Desktop 等)
{
  "mcpServers": {
    "cosyvoice": {
      "command": "D:\\work\\zcode\\tts-cosy-voice\\.venv\\Scripts\\python.exe",
      "args": ["D:\\work\\zcode\\tts-cosy-voice\\server\\mcp_server.py"],
      "env": { "COSYVOICE_MCP_BASE": "http://127.0.0.1:8001" }
    }
  }
}
```

### 传输 2: SSE (旧版 2024-11-05)

- 下行: `GET http://127.0.0.1:8001/mcp/sse` — 首条事件 `event: endpoint` 告知提交地址
- 上行: `POST /mcp/messages?session_id=<id>` — HTTP 202, 响应经 SSE 通道回送

```jsonc
{ "mcpServers": { "cosyvoice": { "type": "sse", "url": "http://127.0.0.1:8001/mcp/sse" } } }
```

### 传输 3: Streamable HTTP (2025-03-26+)

- 单端点 `POST /mcp`: JSON-RPC 请求 → application/json 响应; 通知返回 202
- `initialize` 响应下发 `Mcp-Session-Id` 头 (不强制校验, 兼容无状态客户端)
- `GET /mcp`: 服务端下行 SSE 通道 (Accept 需含 `text/event-stream`)
- `DELETE /mcp`: 终止会话 (204)

```jsonc
{ "mcpServers": { "cosyvoice": { "type": "http", "url": "http://127.0.0.1:8001/mcp" } } }
```

### 协议细节

- 协议版本: `2024-11-05` / `2025-03-26` / `2025-06-18` (initialize 时按客户端选择回显)
- capabilities: `tools.listChanged=false` (无资源/提示词能力)
- 工具报错通过 `isError: true` + 文本内容返回 (不断开连接)
- stdio: 每行一条 JSON-RPC, UTF-8; 服务日志走 stderr (`[MCP]` 前缀), stdout 仅承载协议报文
- 批量请求 (JSON 数组) 按规范逐条处理并合并应答

## 错误码约定

| HTTP | 场景 |
|---|---|
| 400 | 参数校验失败 / 模式不支持 / 音频无法解码等 |
| 404 | 任务/音色/剧集不存在, 或音频尚未生成 |
| 409 | 冲突: 队列加载中 / 合成进行中 / 已有推流 |
| 422 | pydantic 字段校验失败 (detail 含字段路径) |
| 429 | 推理队列已满 (上限 16) |
| 503 | MP3 导出时 ffmpeg 不可用 |

错误体统一为 `{"detail": "中文说明"}`。
