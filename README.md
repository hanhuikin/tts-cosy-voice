# 多模型本地 TTS 服务

FastAPI 语音合成服务 + Web 控制台（合成工作台 / 音色库 / 播客工坊 / 历史记录 / 系统状态）。
内置 [CosyVoice](https://github.com/QwenAudio/CosyVoice) 3.0 / 2.0，并支持本地加载
**IndexTTS 2.0 / 2.5** 与 **Qwen3-TTS**（CustomVoice / Base / VoiceDesign）。

> **日常启动 / 停止 / 排错速查 → [STARTUP.md](STARTUP.md)**（本文只管首次部署）

## 功能

- **四种合成模式**：预置音色（sft）、声音克隆（zero_shot）、跨语言（cross_lingual）、指令控制（instruct，方言/语速/情绪）
- **流式合成**：边合成边播放（WebAudio，2.5s 预缓冲），显示首包延迟；**流式/非流式均支持语速**（v3）
- **音色管理**：上传 3~30s 参考音频克隆音色（Whisper 自动转写辅助）、重命名与参考文本编辑
- **🎙 播客工坊**：三种内容源（粘贴/上传文件/网页 URL 总结）→ 大模型生成双人对对话稿 → 逐句编辑 → 整段合成或推流（RTMP/SRT/RTSP/HLS，见 `播客设计文档.html`）
- **七种本地模型互斥加载**：CosyVoice 3.0/2.0、IndexTTS 2.0/2.5、Qwen3-TTS CustomVoice/Base/VoiceDesign，界面一键切换
- **依赖隔离**：IndexTTS / Qwen3-TTS 通过常驻子进程 worker 运行，可指向独立虚拟环境的 Python，不污染 CosyVoice 主环境
- **能力自适应 UI**：按模型声明显示可用模式、内置说话人、语言标签与指令面板
- **任务历史**：每次合成落盘 WAV，可回放/下载/重载参数
- **OpenAI 兼容端点**：`POST /v1/audio/speech`
- **单 worker GPU 队列**：并发请求自动排队，超限返回 429

## 目录结构

```
server/           # FastAPI 服务 + Vue3 控制台（提交）
  app/model_registry.py   # 模型注册表 / 能力 / 环境变量（新增模型从这里开始）
  app/backends.py         # 推理后端统一接口
  app/tts_worker.py       # 外部模型常驻 worker（独立解释器运行）
scripts/          # 环境与启动脚本（提交）
CosyVoice/        # CosyVoice 上游仓库 —— 不提交，部署时 clone
index-tts/        # IndexTTS 上游仓库 —— 不提交，按需 clone
models/           # 模型权重 —— 不提交，部署时脚本下载（~15GB 起）
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

# 4. 附加依赖（播客工坊: 文档解析 + 推流）
uv pip install --python .venv pypdf python-docx beautifulsoup4
winget install --id Gyan.FFmpeg --accept-package-agreements   # 推流需要 ffmpeg

# 5. 启动（默认 8000，可 -Port 指定）
powershell -ExecutionPolicy Bypass -File scripts\start.ps1 -Port 8001
# 打开 http://127.0.0.1:8001
```

## 可选：接入 IndexTTS 2.0 / 2.5 与 Qwen3-TTS

两套模型依赖较重（Qwen3-TTS 官方建议 Python 3.12，而 CosyVoice 环境是 3.10），因此
**服务端用常驻子进程 worker 调用它们**：一次加载、多次合成，子进程退出即释放显存，
主环境完全不受影响。

```powershell
# 1) 权重 (按需挑选；qwen3tts_tokenizer 供离线部署使用)
.venv\Scripts\python.exe scripts\download_models.py --models indextts2 indextts25 qwen3tts qwen3tts_base qwen3tts_design qwen3tts_tokenizer

# 2) uv (主 venv 由 uv 创建, 默认不带 pip)
.venv\Scripts\python.exe -m ensurepip --upgrade
.venv\Scripts\python.exe -m pip install uv

# 3) IndexTTS 源码 + 独立环境
#    只装基础依赖即可; --all-extras 会在 Windows 上编译 deepspeed / flash-attn, 容易失败
git clone https://github.com/index-tts/index-tts.git
cd index-tts
..\.venv\Scripts\uv.exe sync        # 官方路径: 按 .python-version 自动建 .venv, 含 CUDA 版 torch
# Windows 上若 antlr4-python3-runtime 源码构建失败, 用下面的等效手动安装:
# ..\.venv\Scripts\uv.exe venv --python 3.11 .venv
# ..\.venv\Scripts\uv.exe export --format requirements-txt --no-hashes --no-emit-project -o req.txt
# 删掉 req.txt 中 torch / torchaudio / torchvision 三行 (它们带 +cu128 后缀, 默认源解析不到)
# ..\.venv\Scripts\uv.exe pip install --python .venv\Scripts\python.exe "torch==2.8.*" "torchaudio==2.8.*" --index-url https://download.pytorch.org/whl/cu128
# ..\.venv\Scripts\uv.exe pip install --python .venv\Scripts\python.exe -r req.txt
# ..\.venv\Scripts\uv.exe pip install --python .venv\Scripts\python.exe -e . --no-deps
cd ..
$env:INDEXTTS_REPO   = "D:\work\zcode\tts\index-tts"
$env:INDEXTTS_PYTHON = "D:\work\zcode\tts\index-tts\.venv\Scripts\python.exe"

# 4) Qwen3-TTS 独立环境 (Python 3.12 + CUDA 版 torch; PyPI 默认 torch 是 CPU 版)
.venv\Scripts\uv.exe venv --python 3.12 .venv-qwen3
.venv\Scripts\uv.exe pip install --python .venv-qwen3\Scripts\python.exe "torch==2.8.*" "torchaudio==2.8.*" --index-url https://download.pytorch.org/whl/cu128
.venv\Scripts\uv.exe pip install --python .venv-qwen3\Scripts\python.exe qwen-tts soundfile
$env:QWEN3TTS_PYTHON = "D:\work\zcode\tts\.venv-qwen3\Scripts\python.exe"

# 5) 同一会话内启动，指定本次预载的模型
powershell -ExecutionPolicy Bypass -File scripts\start.ps1 -Port 8001 -Model indextts25
```

> 上面 3 个 `$env:` 也可以写进仓库根目录 `.env`（复制 `.env.example`），
> 之后每次启动都会自动生效，不必再在会话里逐个设置；改完记得重启服务。

> 首次运行 IndexTTS 会按需拉取 w2v-bert-2.0 / MaskGCT / CAMPPlus / BigVGAN 到
> `checkpoints/hf_cache/`；Qwen3-TTS 权重目录内已自带 `speech_tokenizer/`，
> 额外下载的 Tokenizer 仓库仅供离线备用。下载脚本**直接按文件下载**（ModelScope API +
> Range 续传），不走 `snapshot_download` 的分片临时文件，避免受限环境下的批量删除拦截；
> 就绪判定检查权重文件本身，中断后重跑自动续传，不会把只有 `config.yaml` 的半截目录
> 当成已下载。

### 环境变量（服务启动时读取，改完需重启）

| 变量 | 作用 | 默认值 |
|---|---|---|
| `COSYVOICE_MODELS_DIR` | 所有权重根目录 | `<repo>/models` |
| `COSYVOICE3_DIR` / `COSYVOICE2_DIR` | 单独覆盖两个 CosyVoice 权重目录 | `<根目录>/Fun-CosyVoice3-0.5B` / `CosyVoice2-0.5B` |
| `INDEXTTS_REPO` | index-tts 源码目录 | `<repo>/index-tts` |
| `INDEXTTS_PYTHON` / `QWEN3TTS_PYTHON` | worker 解释器 | 服务当前解释器 |
| `INDEXTTS2_DIR` / `INDEXTTS25_DIR` | 两个 IndexTTS 权重目录 | `models/IndexTTS-2` / `models/IndexTTS-2.5` |
| `QWEN3TTS_DIR` / `QWEN3TTS_BASE_DIR` / `QWEN3TTS_DESIGN_DIR` | Qwen3-TTS 三个变体权重目录 | `models/Qwen3-TTS-12Hz-1.7B-*` |
| `QWEN3TTS_DTYPE` / `QWEN3TTS_DEVICE` / `QWEN3TTS_ATTN` | 精度 / 设备 / 注意力实现 | `bfloat16` / 自动 / 不指定 |
| `INDEXTTS2_FP16` / `INDEXTTS25_BF16` | IndexTTS 半精度开关 | `0` / `1` |
| `COSYVOICE_DEFAULT_MODEL` | 启动预载的模型 key | `cosyvoice3` |

**推荐用 `.env` 持久化配置**：把 [`.env.example`](.env.example) 复制为仓库根目录 `.env`，
按需填写后重启服务即可；`server/app/env_config.py` 在导入时读取，`model_registry.py`
据此确定 7 个权重目录与两套 worker 解释器。优先级为「进程环境变量 > `.env`」，
所以调试时仍可用 `$env:XXX = ...` 临时覆盖。`.env` 已被 `.gitignore` 忽略，不要提交。

`GET /api/admin/config` 可只读查看当前生效的配置（含每个模型的实际 `dir`、解释器与
权重就绪状态）；`GET /api/models/{key}/doctor` 做逐模型环境自检。配置在导入时固定，
**修改 `.env` 后必须重启服务**。界面「系统状态与设置」的权重目录弹窗对应这组字段，
原型用本地存储演示交互，正式接线时由服务端写 `.env` 并触发重启。

系统状态页每个模型卡片都有「环境自检」按钮（等价于 `GET /api/models/{key}/doctor`），
会检查权重标记文件、解释器路径和 `indextts` / `qwen_tts` 是否可导入，不占用显存。

### 模型能力对照

| key | 可用模式 | 参考音频 | 内置说话人 | 语速 | 流式 |
|---|---|---|---|---|---|
| `cosyvoice3` / `cosyvoice2` | 预置 / 克隆 / 跨语言 / 指令 | 需要 | 有 | 原生（v2 流式除外） | 原生逐块 |
| `indextts2` | 克隆 / 跨语言 | 需要 | 无 | 音频后处理变速 | 整段后分块 |
| `indextts25` | 克隆 / 跨语言 | 需要 | 无 | 原生 `duration_factor`（中/英/日/西/阿） | 整段后分块 |
| `qwen3tts` | 预置 / 指令（风格） | 不需要 | 9 个 | 音频后处理变速 | 整段后分块 |
| `qwen3tts_base` | 克隆 / 跨语言 | 需要（含参考文本） | 无 | 音频后处理变速 | 整段后分块 |
| `qwen3tts_design` | 指令（声音描述） | 不需要 | 无 | 音频后处理变速 | 整段后分块 |

所有外部模型输出统一重采样为 **24 kHz 单声道 int16 PCM**，与前端流式播放器、
播客合成与推流链路的采样率一致。前端「目标语言」标签 `<|xx|>` 会被转换为各模型的
`lang` / `language` 参数。播客工坊自动跟随当前已加载的模型。

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
| POST | `/api/admin/model` | 切换模型 `{model: "<模型key>"}`；外部模型权重未就绪时返回 400 并附安装提示 |
| GET | `/api/models` · `/api/models/{key}/doctor` | 模型能力列表 / 权重与依赖环境自检 |
| GET | `/api/events` | SSE：引擎状态 / 队列事件 |
| GET/PUT | `/api/llm/config` · `POST /api/llm/test` | 播客大模型配置（OpenAI 兼容）与连通测试 |
| POST | `/api/podcast/source/extract` | 文件/URL → 纯文本 |
| POST | `/api/podcast/script` · `/api/podcast/parse-script` | LLM 生成对话稿（流式）与解析 |
| GET/POST/PUT/DELETE | `/api/podcast/episodes…` | 剧集 CRUD / 合成 `{mode:"final"}` / 推流 `{protocol,url}` / 状态轮询 |
| GET | `/live/{id}.m3u8` | HLS 拉流播放列表 |
| GET | `/docs` | Swagger UI |

## 测试

```powershell
.venv\Scripts\python.exe scripts\test_api.py            # 51 例接口回归
.venv\Scripts\python.exe scripts\test_stream_push.py    # 流式播放 / 直播推流 12 例
.venv\Scripts\python.exe scripts\test_model_matrix.py   # 7 模型逐个切换并合成
.venv\Scripts\python.exe scripts\add_preset_voices.py   # 导入开源预置音色
```

## 音色库随仓库分发

`server/data/voices/` 下的参考音频（11 个，约 3MB）与 `manifest.json` 一起入库，
换机器/新克隆后重建音色库：

```powershell
.venv\Scripts\python.exe scripts\import_voices.py           # 按 manifest 导入（已存在的 id 跳过）
.venv\Scripts\python.exe scripts\import_voices.py --export  # 新增/改名音色后刷新 manifest 再提交
```

除 `voices/` 外的运行时数据（数据库、任务音频、播客成品、推流分片、`llm_config.json`）
仍被 `.gitignore` 排除，不会入库。

## 说明

- CosyVoice3 的指令前缀 `You are a helpful assistant.<|endofprompt|>` 由服务端按模式自动拼接
- 流式语速（v3）通过运行时补丁实现（`server/app/cosyvoice_patch.py`，上游文件零改动）；v2 流式不支持变速
- 推理队列默认上限 16，排队期间 UI 实时显示队列长度
- 上游仓库（`CosyVoice/`、`index-tts/`）与模型权重均保持零改动；新模型适配集中在
  `server/app/model_registry.py` + `server/app/tts_worker.py`，新增一个模型只需各加一个分支
- 外部模型的「流式」是整段生成完成后按 0.2s 分块下发（官方 Python SDK 未暴露逐块推理），
  首包延迟约等于整段生成耗时；CosyVoice 仍为原生逐块流式
