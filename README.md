# CosyVoice FastAPI 服务

基于本地 [QwenAudio/CosyVoice](https://github.com/QwenAudio/CosyVoice)（CosyVoice 3.0）构建的 FastAPI 语音合成服务，带 Web 控制台（合成工作台 / 音色库 / 历史记录 / 系统状态）。

## 功能

- **四种合成模式**：预置音色（sft）、声音克隆（zero_shot）、跨语言（cross_lingual）、指令控制（instruct，方言/语速/情绪）
- **流式合成**：边合成边播放（WebAudio），显示首包延迟；也支持非流式整段返回
- **音色管理**：上传 3~30s 参考音频克隆音色，重采样归一化校验，入库复用
- **双模型可切换**：Fun-CosyVoice3-0.5B 与 CosyVoice2-0.5B 互斥加载，界面一键切换
- **任务历史**：每次合成落盘 WAV，可回放/下载/重载参数
- **OpenAI 兼容端点**：`POST /v1/audio/speech` 可接入现有生态
- **单 worker GPU 队列**：并发请求自动排队，超限返回 429

## 快速开始

```powershell
# 1. 环境 (Python 3.10 via uv; 首次)
uv venv --python 3.10 .venv
uv pip install --python .venv --index-strategy unsafe-best-match -r CosyVoice/requirements.txt
#    openai-whisper 需绕过新版 setuptools 构建隔离:
uv pip install --python .venv "setuptools<81" wheel
uv pip install --python .venv --no-build-isolation openai-whisper==20231117

# 2. 下载模型权重 (ModelScope, 约 6GB)
.venv\Scripts\python.exe scripts\download_models.py

# 3. 启动
powershell -ExecutionPolicy Bypass -File scripts\start.ps1
# 打开 http://127.0.0.1:8000

# 4. 跑接口测试 (38 例: 校验/正路径/WAV 结构/并发排队/脱敏)
.venv\Scripts\python.exe scripts\test_api.py
```

## API 摘要

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/api/tts` | 统一合成：`{text, model, mode, spk_id/voice_id, prompt_text, instruct_text, speed, stream}`；`stream=true` 返回分块 WAV 流 |
| POST | `/v1/audio/speech` | OpenAI 兼容：`{input, voice, speed}`，`voice` 传内置说话人名或 `voice:<音色id>` |
| GET/POST | `/api/voices` | 音色列表 / 上传（multipart: name, prompt_text, file） |
| GET | `/api/voices/{id}/prompt` | 试听参考音频 |
| DELETE | `/api/voices/{id}` `/api/tasks/{id}` | 删除 |
| GET | `/api/tasks` `/api/tasks/{id}/audio` | 历史列表 / 音频 |
| GET | `/api/health` | 模型状态 / GPU 显存 / 队列 |
| POST | `/api/admin/model` | 切换模型 `{model: "cosyvoice3"|"cosyvoice2"}` |
| GET | `/api/events` | SSE：引擎状态 / 队列事件 |
| GET | `/docs` | Swagger UI |

## 目录

```
CosyVoice/        # 上游仓库（clone --recursive，未修改）
server/
  app/            # FastAPI 服务（engine 推理封装+队列 / routes / storage）
  static/         # Vue3 单页控制台
  data/           # db.sqlite + voices/ + tasks/ 音频
scripts/          # 环境与启动脚本
```

## 说明

- CosyVoice3 的指令前缀 `You are a helpful assistant.<|endofprompt|>` 由服务端按模式自动拼接，前端无需关心
- `speed` 参数仅非流式模式支持（上游模型限制）
- 推理队列默认上限 16，排队期间 UI 显示实时队列长度
