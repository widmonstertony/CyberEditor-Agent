# CyberEditor-Agent

完全本地、隐私优先、面向 Windows 的 AI 长视频自动剪辑 MVP。

CyberEditor-Agent 使用 Whisper 提取带时间戳台词，以 OpenCV 生成轻量场景打点，
让本地 Ollama 模型分块做导演决策，最后调用 DaVinci Resolve Python API 将
1080p 代理素材自动组装到时间线。

> 项目状态：可运行 MVP。首次用于正式工程前，请用素材副本验证时间码、代理链接
> 和 Resolve 版本兼容性。本项目与 OpenAI、Ollama、Blackmagic Design 无隶属关系。

## 为什么是严格串行

很多“一键 AI 剪辑”原型会同时保留 ASR、LLM 和 NLE 进程，16GB 显存很快耗尽。
本项目把进程退出当作硬性显存屏障：

```text
Whisper + OpenCV 子进程
          │ 完全退出，Windows 回收 CUDA 上下文
          ▼
Ollama 分块导演子进程
          │ keep_alive=0 + 父进程二次卸载
          ▼
DaVinci Resolve 执行子进程
```

- 父调度器不导入 PyTorch、Whisper、OpenCV、requests 或 Resolve API。
- 任意时刻最多存在一个项目重型子进程。
- Ollama `/api/ps` 若发现其他模型驻留，会拒绝加载导演模型。
- 导演每次只处理 10–15 分钟数据，默认 12 分钟。
- `timeline_cuts.json` 只在所有分块成功、校验和合并后原子写入。

Ollama 官方 API 支持以 JSON Schema 约束输出，并以 `keep_alive: 0` 立即卸载模型：
[Generate API](https://docs.ollama.com/api/generate)、
[Structured Outputs](https://docs.ollama.com/capabilities/structured-outputs)、
[模型卸载说明](https://docs.ollama.com/faq#how-do-i-keep-a-model-loaded-in-memory-or-make-it-unload-immediately)。

## 项目结构

```text
CyberEditor-Agent/
├─ main.py                       # 严格串行工作流调度器
├─ requirements.txt
├─ README.md
├─ LICENSE
├─ CONTRIBUTING.md
├─ SECURITY.md
├─ src/
│  ├─ __init__.py
│  ├─ extractor.py               # Whisper + OpenCV 数据提取
│  ├─ director.py                # Ollama 分块导演
│  └─ resolve_executor.py        # DaVinci Resolve 自动组装
├─ data/
│  ├─ .gitkeep
│  └─ keyframes/.gitkeep
├─ logs/.gitkeep
├─ examples/
│  ├─ raw_data.example.json
│  └─ timeline_cuts.example.json
└─ tests/
   ├─ test_director.py
   ├─ test_extractor.py
   ├─ test_orchestrator.py
   └─ test_resolve_executor.py
```

## 硬件与软件要求

### 最低建议

- Windows 10/11 64 位
- Python 3.10 或 3.11 64 位
- 16GB 系统内存；运行 32B/70B 混合推理时建议 32–64GB
- 至少 20GB 可用磁盘空间，另加代理素材和模型空间
- FFmpeg
- Ollama
- DaVinci Resolve，且已允许本机 Python 脚本访问

### 模型建议

| 设备 | Whisper | Ollama 建议 | 说明 |
|---|---|---|---|
| 核显/纯 CPU | `tiny` / `base` | 7B–14B Q4 | 慢，但整个流程仍可运行 |
| 8–12GB VRAM | `small` | 14B/32B Q4 混合卸载 | 降低 `--num-ctx` 可节省内存 |
| 16GB VRAM | `small` / `turbo` | 32B Q4，或 70B CPU/GPU 混合 | 严格串行是核心目标 |
| 24GB+ VRAM | `turbo` / `large-v3` | 32B/70B 量化 | 仍建议保留串行策略 |

70B 模型不可能完整装入 16GB VRAM；“兼容”指 Ollama/llama.cpp 将部分层卸载到
CPU/RAM。实际速度取决于量化、内存带宽和上下文长度。

Whisper 官方列出的近似显存占用从 tiny/base 的约 1GB 到 turbo 的约 6GB：
[OpenAI Whisper README](https://github.com/openai/whisper#available-models-and-languages)。

## 安装

### 1. 安装系统组件

```powershell
winget install --id Gyan.FFmpeg
winget install --id Ollama.Ollama
```

安装后重新打开 PowerShell，验证：

```powershell
ffmpeg -version
ollama --version
```

安装 DaVinci Resolve 后，在 Resolve 的偏好设置中允许本机 External Scripting
访问（Local），然后重启 Resolve。

### 2. 克隆并创建虚拟环境

```powershell
git clone https://github.com/widmonstertony/CyberEditor-Agent.git
cd CyberEditor-Agent
py -3.11 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
```

NVIDIA 用户建议先按 [PyTorch 官方安装器](https://pytorch.org/get-started/locally/)
安装与驱动匹配的 CUDA 版本，再安装项目依赖：

```powershell
pip install -r requirements.txt
```

Whisper 需要系统 FFmpeg；其官方安装和 Python 调用方式见
[OpenAI Whisper](https://github.com/openai/whisper#setup)。

### 3. 准备 Ollama

以下只是默认示例，可替换为本机已有模型：

```powershell
ollama pull qwen2.5:32b
```

建议在启动 Ollama 前限制并行与同时驻留模型数，然后重启 Ollama：

```powershell
setx OLLAMA_MAX_LOADED_MODELS 1
setx OLLAMA_NUM_PARALLEL 1
```

### 4. 准备代理

在 Resolve 中为长视频生成 1080p 代理，或自行用 FFmpeg 生成。确保代理与源素材
起始时间和持续时间一致。导演 JSON 使用秒，执行层按 Resolve 当前工程 FPS 转为帧。

## 一键运行

先启动 Ollama；运行到 Resolve 阶段前还需启动 Resolve 并打开目标工程。

```powershell
python main.py `
  --video "D:\Documentary\source.mov" `
  --proxy "D:\Documentary\proxy\source_1080p.mp4" `
  --ollama-model "qwen2.5:32b" `
  --project-fps 25 `
  --chunk-minutes 12
```

默认输出：

```text
data/raw_data.json
data/transcript.srt
data/keyframes/*.jpg
data/timeline_cuts.json
data/cybereditor.log
```

### 断点续跑

长视频不应因 Resolve 未启动而重新转写：

```powershell
# 已有 raw_data.json，只重跑导演与 Resolve
python main.py --skip-extraction `
  --proxy "D:\Documentary\proxy\source_1080p.mp4" `
  --ollama-model "qwen2.5:32b"

# 已有 timeline_cuts.json，只执行 Resolve
python main.py --skip-extraction --skip-director `
  --proxy "D:\Documentary\proxy\source_1080p.mp4"

# 只生成 JSON，不启动 Resolve
python main.py --video "D:\source.mov" --skip-resolve
```

## 独立调用模块

```powershell
python -m src.extractor --help
python -m src.director --help
python -m src.resolve_executor --help
```

三个核心类也可被外部 Python 模块直接导入：

```python
from src.extractor import MediaExtractor
from src.director import AIDirector
from src.resolve_executor import DaVinciExecutor
```

## JSON 契约

导演输出兼容以下核心结构：

```json
{
  "project_fps": 25.0,
  "clips": [
    {
      "clip_id": 1,
      "file_name": "D:\\Documentary\\proxy\\source_1080p.mp4",
      "cut_in_sec": 12.5,
      "cut_out_sec": 18.2,
      "reason_for_cut": "完整表达主题观点",
      "confidence": 0.9
    }
  ]
}
```

`cut_in_sec` 为包含式入点，`cut_out_sec` 为不包含式出点。Resolve 执行层将入点
向下取整，将出点向上取整后减一，以适配包含式 `endFrame`。

## 鲁棒性边界

- 代理素材帧率最好与工程帧率一致。当前 MVP 按需求使用 Resolve 工程 FPS 换算
  JSON 秒数；混合原生帧率素材应先统一代理。
- 脚本会复用当前 Resolve 时间线；如果没有当前时间线，才创建
  `CyberEditor Timeline`。
- 如果中途某个 Resolve 片段追加失败，API 没有可靠事务回滚；日志会指出已追加
  数量，用户需要检查时间线并撤销。
- 场景打点是轻量灰度帧差，不是语义视觉理解。后续可在 extractor 子进程中替换
  为视觉模型，但必须继续遵守进程退出屏障。

## 测试

测试不需要 GPU、Ollama 或 Resolve：

```powershell
python -m unittest discover -s tests -v
python -m compileall -q main.py src tests
```

## 隐私

媒体、台词、关键帧和剪辑 JSON 默认只写入本机 `data/`，该目录内容已被
`.gitignore` 排除。不要把真实客户素材、模型权重、Resolve 工程或日志提交到 issue。
详见 [SECURITY.md](SECURITY.md)。

## 贡献

欢迎提交兼容不同显卡、Ollama 模型和 Resolve 版本的改进。任何新 AI 模块必须
证明不会与其他重型阶段并发。参见 [CONTRIBUTING.md](CONTRIBUTING.md)。

## License

[MIT](LICENSE)
