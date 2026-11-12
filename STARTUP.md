# 启动指南（日常速查）

> 部署/首次安装见 [README.md](README.md)；本文只管「每天怎么把它跑起来」。

## 一、启动

### 方式 A：启动脚本（推荐）

```powershell
powershell -ExecutionPolicy Bypass -File scripts\start.ps1 -Port 8001
# 可选: -Model <模型key>   指定本次启动预载的模型 (默认 cosyvoice3)
#       cosyvoice3 / cosyvoice2 / indextts2 / indextts25 /
#       qwen3tts / qwen3tts_base / qwen3tts_design
```

前台运行，`Ctrl+C` 停止。日志直接打印在控制台。

### 方式 B：命令行手动启动（后台/重定向日志）

```bash
cd server
../.venv/Scripts/python.exe -m uvicorn app.main:app --host 0.0.0.0 --port 8001 > ../server_run.log 2>&1 &
```

### 方式 C：切换默认模型启动

```powershell
$env:COSYVOICE_DEFAULT_MODEL = "cosyvoice2"; powershell -ExecutionPolicy Bypass -File scripts\start.ps1 -Port 8001
```
（启动时按「指定模型 → 注册表顺序」找第一个权重就绪的模型预载；都没有则保持 idle，等界面手动切换。）

### 方式 D：启动并加载 IndexTTS / Qwen3-TTS

这些模型跑在独立解释器 worker 里。**推荐把配置写入仓库根目录 `.env`**（复制 `.env.example`），
服务启动时自动读取，之后每次启动都生效：

```ini
INDEXTTS_REPO=D:/work/zcode/tts/index-tts
INDEXTTS_PYTHON=D:/work/zcode/tts/index-tts/.venv/Scripts/python.exe
QWEN3TTS_PYTHON=D:/work/zcode/tts/.venv-qwen3/Scripts/python.exe
```

```powershell
powershell -ExecutionPolicy Bypass -File scripts\start.ps1 -Port 8001 -Model indextts25
```

临时覆盖时仍可在**同一个 PowerShell 会话**里用 `$env:XXX = ...`（进程环境变量优先于 `.env`）；
修改 `.env` 后必须重启服务。用 `curl http://127.0.0.1:8001/api/admin/config` 可查看当前生效的
权重目录与解释器，逐模型自检用 `GET /api/models/{key}/doctor`。

完整安装步骤与全部环境变量见 [README.md](README.md) 的「可选：接入 IndexTTS 2.0 / 2.5 与 Qwen3-TTS」一节。

## 二、验证启动成功

```bash
# 1) 健康检查: state 从 loading 变 ready 即就绪 (加载约 20~40s)
curl http://127.0.0.1:8001/api/health
#    关注字段: state / model / gpu.vram_used_gb (~4.5GB 正常) / queue_len

# 2) Web 控制台
http://127.0.0.1:8001/static/index.html

# 3) Swagger API 文档
http://127.0.0.1:8001/docs
```

就绪后左侧导航应显示「● 就绪 NVIDIA GeForce RTX 4060 Ti」。

## 三、停止

```bash
# 找到监听 8001 的进程并结束 (Windows)
netstat -ano | grep ":8001" | grep LISTEN     # 取最后一列 PID
taskkill /PID <PID> /F
```

前台方式 A 直接 `Ctrl+C`。

## 四、启动后检查清单（改代码 / 拉新代码后）

| 检查 | 方法 |
|---|---|
| 服务就绪 | `/api/health` state=ready |
| 音色还在 | 控制台「音色库」应有 11 个音色 |
| 大模型可用 | 播客工坊「⚙ 大模型」显示 deepseek-chat；「配置/测试连通」返回 ok（key 存于 `server/data/llm_config.json`，已 gitignore） |
| ffmpeg 可用 | 播客工坊推流开关能打开；`curl http://127.0.0.1:8001/api/podcast/ffmpeg` → `{"available":true}` |
| 历史数据完整 | 侧栏「播客列表」能看到之前的剧集 |

## 五、常见问题

| 现象 | 原因与处理 |
|---|---|
| 启动报 `venv 不存在` | 未安装环境，按 README 部署步骤 1~4 执行 |
| `/api/health` 一直 `idle` | 模型权重未下载：`.venv\Scripts\python.exe scripts\download_models.py` |
| state 卡在 `loading` 很久 | 正常，首次加载 20~40s；超过 2 分钟看日志是否有 CUDA 报错 |
| `address already in use` / 端口被占 | 上一进程没退干净，按「三、停止」处理后再启 |
| 服务重启后队列里出现「服务重启中断」的失败任务 | 正常行为：启动时自动把上次中断的 synthesizing/pushing/running 任务落库为 failed，防止状态卡死 |
| 生成对话稿报 `LLM 401` | DeepSeek key 失效：播客工坊 → ⚙ 大模型 → 填新 key → 测试连通 |
| 推流启动失败提示无 ffmpeg | `winget install --id Gyan.FFmpeg`，装完重启服务（PATH 生效） |
| 显存不足 / 合成 OOM | 确认没有其他程序占用 GPU；必要时切 cosyvoice2（更省）或重启服务清显存；切换模型时旧 worker 进程会被结束，显存随之释放 |
| 切到 IndexTTS / Qwen3-TTS 时报「权重目录未就绪」 | 权重没下载或目录不对：`scripts/download_models.py --models <key>`，或用 `*_DIR` 环境变量指向已有权重；系统状态页「环境自检」可看到缺哪个文件 |
| state 变 error，日志含「导入 indextts / qwen_tts 失败」 | worker 解释器里没装依赖：确认 `INDEXTTS_PYTHON` / `QWEN3TTS_PYTHON` 指向正确的 venv，且 `INDEXTTS_REPO` 指向完整 index-tts 仓库 |
| 新模型「流式」首包很慢 | 预期行为：官方 Python SDK 未提供逐块推理，服务端整段生成后分块下发；关掉「流式合成」体验更一致 |
| 播客工坊合成报「当前模型不支持声音克隆」 | 当前模型（如 Qwen3-TTS CustomVoice / VoiceDesign）没有克隆模式，请切到 cosyvoice / indextts / qwen3tts_base |
| 装 IndexTTS 环境时 `uv sync` 在 `antlr4-python3-runtime` 构建失败 | 该依赖只有源码包。按 README 的「Windows 手动安装」路径：`uv export` 导出依赖 → 删掉 torch 行 → 从 cu128 源单独装 torch → 装其余依赖 → `-e . --no-deps`；antlr4 可先用 uv 缓存里已构建的 wheel 装上 |
| `uv pip install -r req.txt` 报 `torch==2.8.0+cu128` 无解 | 带 `+cu128` 的版本只在 PyTorch CUDA 源；从清单里移除 torch/torchaudio 行，用 `--index-url https://download.pytorch.org/whl/cu128` 单独安装 |
| 下载中断后重跑脚本提示 `[skip] already downloaded` | 已修复：就绪判定现在检查 `gpt.pth` / `model.safetensors` 等权重文件，只有 `config.yaml` 不会误判为完成 |
| 下载报 `[safe-delete][SAFE_DELETE_BULK_CONFIRM_REQUIRED]` 后失败 | `snapshot_download` 分片下载结束时会批量删除临时文件。下载脚本已改为直接按文件下载 + Range 续传，不产生批量删除 |
| `uv` 装大 wheel 报 `failed to rename ... 拒绝访问 (os error 5)` | 受限环境下 uv 缓存重命名被拦截。可用 `scripts/fetch_wheel.py` 把 wheel 下到本地，再按 wheel=zip 解压到 `Lib/site-packages`；其余小依赖改用 `python -m pip install -r req.txt` |

## 六、关键路径速查

| 内容 | 路径 |
|---|---|
| 本机配置 | `.env`（从 `.env.example` 复制，不入 Git；进程环境变量优先，改完需重启） |
| 虚拟环境 | `.venv/`（Python 3.10, torch cu121） |
| 服务代码 | `server/app/`（main.py 入口, engine.py 引擎队列, routes/ 路由） |
| 模型注册 / 能力 | `server/app/model_registry.py`（权重目录、环境变量、模式与提示） |
| 推理适配 | `server/app/backends.py`（后端接口 + 重采样/变速）、`server/app/tts_worker.py`（外部模型子进程） |
| 外部模型源码 | `index-tts/`（`INDEXTTS_REPO` 可覆盖）；Qwen3-TTS 只需 pip 包 |
| 前端（改完刷新即生效） | `server/static/`（index.html / app.js / style.css） |
| 运行时数据（不入 Git） | `server/data/`（db.sqlite + voices/ + tasks/ + podcasts/ + live/ + llm_config.json） |
| 模型权重 | `models/`（或 `COSYVOICE_MODELS_DIR` 指定的目录） |
| 日志 | 方式 A 看控制台；方式 B 看 `server_run.log` |
| 当前使用的 LLM key | `server/data/llm_config.json`（本机保存，接口返回时脱敏） |

## 七、快速冒烟（启动后 30 秒确认核心链路）

```bash
# TTS 非流式: 返回 wav 且响应头带 X-Task-Id (内置说话人为空时用 zero_shot + 克隆音色)
.venv/Scripts/python.exe -c "
import requests
vid = requests.get('http://127.0.0.1:8001/api/voices').json()[0]['id']
r = requests.post('http://127.0.0.1:8001/api/tts', json={
    'text': '你好，冒烟测试', 'model': 'cosyvoice3', 'mode': 'zero_shot',
    'voice_id': vid, 'prompt_text': '你好'}, timeout=120)
print(r.status_code, r.headers.get('X-Task-Id'), len(r.content), 'bytes')

# 剧集列表 (队列数据源)
import requests as rq; print(rq.get('http://127.0.0.1:8001/api/podcast/episodes').text[:200])
```
