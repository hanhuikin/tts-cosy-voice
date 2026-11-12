# 启动指南（日常速查）

> 部署/首次安装见 [README.md](README.md)；本文只管「每天怎么把它跑起来」。

## 一、启动

### 方式 A：启动脚本（推荐）

```powershell
powershell -ExecutionPolicy Bypass -File scripts\start.ps1 -Port 8001
# 可选: -Model cosyvoice2   指定本次启动加载的模型 (默认 cosyvoice3)
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
（启动时只会自动预载 cosyvoice3 / cosyvoice2 中已下载权重的一个；两个都没下载则保持 idle，等界面手动切换。）

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
| 显存不足 / 合成 OOM | 确认没有其他程序占用 GPU；必要时切 cosyvoice2（更省）或重启服务清显存 |

## 六、关键路径速查

| 内容 | 路径 |
|---|---|
| 虚拟环境 | `.venv/`（Python 3.10, torch cu121） |
| 服务代码 | `server/app/`（main.py 入口, engine.py 引擎, routes/ 路由） |
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
