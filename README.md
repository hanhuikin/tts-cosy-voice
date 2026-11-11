# CosyVoice FastAPI 服务

基于 [QwenAudio/CosyVoice](https://github.com/QwenAudio/CosyVoice)（CosyVoice 3.0）构建的 FastAPI 语音合成服务，带 Web 控制台（合成工作台 / 音色库 / 历史记录 / 系统状态）。

## 功能

- **四种合成模式**：预置音色（sft）、声音克隆（zero_shot）、跨语言（cross_lingual）、指令控制（instruct，方言/语速/情绪）
- **流式合成**：边合成边播放（WebAudio，2.5s 预缓冲），显示首包延迟；**流式/非流式均支持语速**（v3）
- **音色管理**：上传 3~30s 参考音频克隆音色（Whisper 自动转写辅助）、重命名与参考文本编辑
- **双模型可切换**：Fun-CosyVoice3-0.5B 与 CosyVoice2-0.5B 互斥加载，界面一键切换
- **任务历史**：每次合成落盘 WAV，可回放/下载/重载参数
- **OpenAI 兼容端点**：`POST /v1/audio/speech`
- **单 worker GPU 队列**：并发请求自动排队，超限返回 429

## 目录结构

```
server/           # FastAPI 服务 + Vue3 控制台（提交）
scripts/          # 环境与启动脚本（提交）
CosyVoice/        # 上游仓库 clone —— 不提交，部署时 clone（见下）
models/           # 模型权重 —— 不提交，部署时脚本下载（~15GB）
server/data/      # 运行时数据（db + 用户音频）—— 不提交
```

## 部署步骤

```powershell
# 1. 上游仓库（含 Matcha-TTS submodule）
git clone --recursive https://github.com/QwenAudio/CosyVoice

# 2. Python 3.10 环境
uv venv --python 3.10 .venv
uv pip install --python .venv --index-strategy unsafe-best-match -r CosyVoice/requirements.txt
uv pip install --python .venv "setuptools<81" wheel
uv pip install --python .venv --no-build-isolation openai-whisper==20231117

# 3. 模型权重（ModelScope, ~15GB；默认下到 models/，可用环境变量 COSYVOICE_MODELS_DIR 覆盖）
.venv\Scripts\python.exe scripts\download_models.py

# 4. 启动（默认 8000，可 -Port 指定）
powershell -ExecutionPolicy Bypass -File scripts\start.ps1 -Port 8001
# 打开 http://127.0.0.1:8001
```

## API 摘要

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/api/tts` | 统一合成：`{text, model, mode, spk_id/voice_id, prompt_text, instruct_text, speed, stream}`；`stream=true` 返回分块 WAV 流 |
| POST | `/v1/audio/speech` | OpenAI 兼容：`{input, voice, speed}`，`voice` 传内置说话人名或 `voice:<音色id>` |
| GET/POST | `/api/voices` | 音色列表 / 上传（multipart: name, prompt_text, file） |
| PUT | `/api/voices/{id}` | 编辑音色名称 / 参考文本 |
| GET | `/api/voices/{id}/prompt` | 试听参考音频 |
| DELETE | `/api/voices/{id}` `/api/tasks/{id}` | 删除 |
| GET | `/api/tasks` `/api/tasks/{id}/audio` | 历史列表 / 音频 |
| GET | `/api/health` | 模型状态 / GPU 显存 / 队列 |
| POST | `/api/admin/model` | 切换模型 `{model: "cosyvoice3"|"cosyvoice2"}` |
| GET | `/api/events` | SSE：引擎状态 / 队列事件 |
| GET | `/docs` | Swagger UI |

## 测试

```powershell
.venv\Scripts\python.exe scripts\test_api.py          # 41 例接口测试
.venv\Scripts\python.exe scripts\add_preset_voices.py # 导入开源预置音色
```

## 说明

- CosyVoice3 的指令前缀 `You are a helpful assistant.<|endofprompt|>` 由服务端按模式自动拼接
- 流式语速（v3）通过运行时补丁实现（`server/app/cosyvoice_patch.py`，上游文件零改动）；v2 流式不支持变速
- 推理队列默认上限 16，排队期间 UI 实时显示队列长度
