# CyberEditor-Agent

[简体中文](README.md) | [English](README_EN.md)

## Windows 桌面 UI / Desktop UI

安装依赖后，双击仓库根目录的 `launch_ui.bat` 即可启动图形界面，也可以在
PowerShell 中运行：

```powershell
.\.venv\Scripts\python.exe gui.py
```

桌面界面可一次多选任意数量的视频，或递归读取一个素材文件夹；还提供 Whisper/Ollama
参数、断点续跑模式、Resolve 开关、可观看预览、实时日志、阶段进度、停止任务和打开
输出目录等功能。现代界面使用轻量级 CustomTkinter，并通过
子进程调用原有 `main.py`；常驻 UI 不导入 PyTorch，不会改变严格串行与显存释放策略。

完整流程不是把所有视频简单首尾相接。每个素材会被依次转写并抽取时间分布均匀的真实
画面；视觉模型对每个 10–15 分钟窗口结合画面和台词挑选候选片段，第二遍全局导演再从
全部素材的候选中决定使用哪些、如何跨文件排序，以及硬切/叠化/淡黑、音频降噪、基础
色彩风格、音量、防抖、跟踪和轻微推镜。最终可同时输出 Resolve 可编辑时间线、
FFmpeg 1080p 审片预览，以及由 Resolve Deliver 页面真正渲染的最终成片。

“性能配置”默认使用 `自动检测 / Auto`：界面以标准库读取 CPU、系统内存和 GPU，
通过 `nvidia-smi`（可用时）读取准确显存，并在一次性子进程中确认当前 PyTorch 是否
真的支持 CUDA。它会自动选择 Whisper 模型、设备模式、10–15 分钟分块时长、
Ollama 上下文，以及在内存预算内质量最合适的**已安装** Ollama 模型；模型排序会
考虑中文、长上下文、指令遵循和量化，不再只比较文件大小。自动档不会擅自下载十几
GB 的模型，也不会让硬件性能档修改素材属性。16GB VRAM + 64GB RAM 会默认进入
“高质量（较慢）”：Whisper `large-v3`、10 分钟分块、16K 上下文。

UI 启动时会检测本机 Ollama；若已安装但服务未运行，会自动启动后台 API，但不会
加载模型或占用模型显存。Resolve 支持 C/D/其他盘的自定义安装位置，只在严格串行
流程进入最后的 Resolve 阶段后自动启动并等待脚本 API，因此不会提前与 Whisper/Ollama 抢显存。

“工程 FPS”默认是**自动读取源素材**，不是固定 25。选择原片后，界面使用短生命周期
`ffprobe` 进程读取 `avg_frame_rate` / `r_frame_rate`，并支持 23.976、29.97、59.94
等分数帧率；没有可读取素材时，会依次尝试 `timeline_cuts.json` 和 `raw_data.json`。
只有检测失败才会在启动前提示用户，仍可从现代下拉菜单手动选择常用工程帧率。

右上角主题菜单支持跟随系统、深色和浅色；语言菜单支持跟随 Windows、中文和 English，
两者都可即时切换并保存。界面启用 Per-Monitor DPI Awareness V2、Windows 11 Mica
标题背景与圆角，同时保留原生标题栏，因此贴靠布局、任务栏预览和 Alt+Tab 仍然可用。
4K 几何尺寸会按 Windows 工作区和 CustomTkinter 的实际缩放比例计算，避免重复缩放。

## Resolve 版本与 Sony A7M4 PP8 素材

本项目的最终执行阶段需要 **DaVinci Resolve Studio**（付费版，官方名称不是
“Resolve Pro”）。原因不是简单的 4K 分辨率，而是：

- 本项目从独立 Python/UI 进程连接 Resolve，所需的 External Scripting/API 访问是
  Studio 功能。免费版只能在 Resolve 内部的 Console 或 Scripts 菜单运行脚本。
- Resolve 免费版官方定位为处理最高 UHD 3840×2160、60 fps 的大多数 8-bit 格式；
  Studio 支持专业 10-bit 格式、最高 120 fps、超过 4K 的分辨率，并提供 H.264/H.265
  硬件编解码加速。

如果“M4”指 Sony α7 IV（A7M4），PP8 默认是 **S-Log3 + S-Gamut3.Cine 图片配置**，
不是 RAW 视频格式。相机可能按所选记录格式生成 XAVC S、XAVC HS 或 XAVC S-I，
并可能为 8-bit 或 10-bit 4:2:2。对于常见的 PP8 4K 10-bit 4:2:2 素材以及本项目的
外部自动组装流程，建议直接安装 Resolve Studio。

参考：
[Blackmagic Design 版本对比](https://www.blackmagicdesign.com/products/davinciresolve)、
[Resolve Studio 脚本与自动化](https://www.blackmagicdesign.com/products/davinciresolve/studio)、
[Sony α7 IV PP8 说明](https://helpguide.sony.net/ilc/2110/v1/en/contents/TP1000649066.html)、
[Sony α7 IV 记录格式](https://helpguide.sony.net/ilc/2110/v1/en/contents/TP1000640834.html)。

完全本地、隐私优先、面向 Windows 的 AI 长视频自动剪辑 MVP。

CyberEditor-Agent 使用 Whisper 提取带时间戳台词，以 OpenCV 抽取真实视觉帧，
让支持视觉的本地 Ollama 模型先分块审片、再跨素材全局编排，最后生成可直接观看的
MP4，并调用 DaVinci Resolve Python API 组装可继续精修的时间线。

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
FFmpeg 预览渲染子进程
          │ 生成真实转场、降噪与基础运动效果
          ▼
DaVinci Resolve 执行子进程
          │ 原生效果 / DRX / 可选受保护 UI 宏 / 最终渲染
```

- 父调度器不导入 PyTorch、Whisper、OpenCV、requests 或 Resolve API。
- 任意时刻最多存在一个项目重型子进程。
- Ollama `/api/ps` 若发现其他模型驻留，会拒绝加载导演模型。
- 导演每次只处理 10–15 分钟数据，默认 12 分钟。
- `timeline_cuts.json` 只在所有分块成功、校验和合并后原子写入。

## 四阶段自动成片能力

当前实现对应以下严格串行链路：

1. **提取期**：每个视频单独启动 Whisper/OpenCV 子进程，写入台词、真实 JPEG
   关键帧和素材元数据；每个子进程退出后，父调度器再执行显存释放屏障。
2. **思考期**：视觉 Ollama 模型以 10–15 分钟窗口审片，再进行一次跨全部素材的
   全局编排，原子生成 `timeline_cuts.json`；随后发送 `keep_alive: 0` 并由父进程
   二次确认卸载。UI 会自动列出本机已安装模型；当前推荐的
   `qwen3.5:35b-a3b` 是 36B Q4 MoE 视觉推理模型，不会被错误标成 70B。
3. **执行期**：Resolve 原生 API 完成素材导入、按源素材 FPS 换帧、拼接、Voice
   Isolation、基础 CDL、`Stabilize()`、`CreateMagicMask()` 和 `SmartReframe()`；
   用户导出的 DRX 通过节点图 `ApplyGradeFromDRX()` 注入。Resolve 21 已原生公开
   防抖与 Magic Mask 接口，因此默认不使用脆弱的坐标宏。
4. **导出期**：启用 UI 中“Resolve 导出最终成片”后，执行器创建 Render Job、启动
   渲染、持续报告百分比并校验完成状态；默认沿用当前 Deliver 页面格式/编码设置，
   也可填写一个现有的 Resolve 渲染预设名。

### DRX、Fairlight 与 UI 宏

- 将 Resolve 中导出的调色预设放入 `config/drx/`，文件名只能是
  `interview_clean.drx`、`cinematic.drx` 或 `low_light_cleanup.drx`。模型只能选择
  这些逻辑名，不能构造任意磁盘路径。
- UI 可填写一个已经存在于 Resolve 中的 Fairlight 预设名；找不到时任务明确失败，
  避免无声漏掉高级音频处理。
- `src/resolve_macro.py` 是 API 未覆盖操作的**可选** PyAutoGUI 后备层。默认完全关闭；
  只有填写宏配置才会执行。它会校验 4K 分辨率、强制 Resolve 位于前台、开启鼠标
  移到屏幕角落急停，并且只接受等待、快捷键、按键和归一化点击四种动作。复制
  `config/resolve_macro_profile.example.json` 后按自己的 Resolve 工作区校准；不要在
  未校准的电脑上复用坐标。

Resolve 21 官方文档没有保证通用“逐片段音量”属性。执行器会先动态探测当前版本；
若存在则写入，否则把 dB 决策保存在 AI 标记中并告警。FFmpeg 审片预览会始终真实
应用该音量。正式输出需要逐片段增益时，应使用已校准宏或 Fairlight 预设复核。

Ollama 官方 API 支持以 JSON Schema 约束输出，并以 `keep_alive: 0` 立即卸载模型：
[Generate API](https://docs.ollama.com/api/generate)、
[Structured Outputs](https://docs.ollama.com/capabilities/structured-outputs)、
[模型卸载说明](https://docs.ollama.com/faq#how-do-i-keep-a-model-loaded-in-memory-or-make-it-unload-immediately)。

## 项目结构

```text
CyberEditor-Agent/
├─ gui.py                        # Windows 桌面 UI 入口
├─ launch_ui.bat                 # 双击启动 UI
├─ main.py                       # 严格串行工作流调度器
├─ scripts/
│  └─ install_windows.ps1        # 自动选择 CPU/CUDA 的 Windows 安装器
├─ requirements.txt
├─ config/
│  ├─ drx/                      # 用户导出的受限 DRX 预设
│  └─ resolve_macro_profile.example.json
├─ README.md
├─ README_EN.md
├─ LICENSE
├─ CONTRIBUTING.md
├─ SECURITY.md
├─ src/
│  ├─ __init__.py
│  ├─ gui.py                     # 无额外 UI 依赖的后备控制器与公共检测逻辑
│  ├─ modern_gui.py              # Windows 11 / 4K / 中英文现代界面
│  ├─ runtime_services.py        # 跨盘软件发现与 Ollama/Resolve 自动启动
│  ├─ media_manifest.py          # 多视频/文件夹发现、去重与代理映射
│  ├─ ui_i18n.py                 # 不依赖 GUI 包的中英文文案
│  ├─ extractor.py               # Whisper + OpenCV 数据提取
│  ├─ director.py                # 多模态分块审片 + 跨素材全局导演
│  ├─ review_renderer.py         # FFmpeg 可观看预览与效果渲染
│  ├─ resolve_macro.py           # 受保护的可选 PyAutoGUI 后备层
│  └─ resolve_executor.py        # DaVinci Resolve 自动组装与效果映射
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
   ├─ test_gui.py
   ├─ test_orchestrator.py
   ├─ test_runtime_services.py
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
- DaVinci Resolve Studio，且 External Scripting 已设为 `Local`

### 模型建议

| 设备 | Whisper | Ollama 建议 | 说明 |
|---|---|---|---|
| 核显/纯 CPU | `tiny` / `base` | Qwen 3.5 4B/9B Q4 | 慢，但整个流程仍可运行 |
| 8–12GB VRAM | `small` / `turbo` | `qwen3.5:9b-q4_K_M` | 6.6GB，中文与长上下文表现好 |
| 16GB VRAM | `large-v3` | `qwen3.5:9b-q8_0` | 约 11GB，可完整放入显存，推荐日常高质量档 |
| 16GB VRAM + 64GB RAM | `large-v3` | `qwen3.5:35b-a3b` | 约 24GB，CPU/GPU 混合，质量优先且更慢 |
| 24GB+ VRAM | `large-v3` | Qwen 3.5 27B/35B | 仍建议保留串行策略 |

对本项目而言，当前的 `qwen2.5:3b` 适合功能测试，不是高质量纪录片剪辑模型。
你的 16GB Quadro + 64GB RAM 可优先选择 `qwen3.5:35b-a3b`；若希望更稳定地
全 GPU 运行，则选择 `qwen3.5:9b-q8_0`。混合卸载的实际速度取决于量化、内存带宽
和上下文长度。Qwen 3.5 官方模型同时支持视觉、工具、推理与 201 种语言：
[Qwen 3.5 on Ollama](https://ollama.com/library/qwen3.5)。

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

安装 **DaVinci Resolve Studio** 后，在 Resolve 的偏好设置中将 External Scripting
设为 `Local`，然后重启 Resolve。免费版可以运行本项目的提取与 AI 导演阶段，但不能
让独立 UI/Python 进程自动执行最终时间线组装。

### 2. 克隆并自动安装（推荐）

```powershell
git clone https://github.com/widmonstertony/CyberEditor-Agent.git
cd CyberEditor-Agent
Set-ExecutionPolicy -Scope Process Bypass
.\scripts\install_windows.ps1
```

安装器会创建 `.venv`、安装依赖、检测 `nvidia-smi` 与驱动版本，并在兼容的 NVIDIA
电脑上用 PyTorch 官方索引替换通用 CPU wheel，最后实际运行 CUDA 张量计算。没有
NVIDIA GPU 时会安全保留 CPU 版。也可以强制指定：

```powershell
.\scripts\install_windows.ps1 -ComputePlatform cpu
.\scripts\install_windows.ps1 -ComputePlatform cu126
.\scripts\install_windows.ps1 -ComputePlatform cu130
```

如果希望手动安装，请先创建虚拟环境，再按
[PyTorch 官方安装器](https://pytorch.org/get-started/locally/)选择 Windows、Pip
和与驱动兼容的 CUDA 版本，然后运行 `pip install -r requirements.txt`。完成后可用
以下命令验证；只有返回 `True` 才代表当前虚拟环境真正启用了 CUDA：

```powershell
.\.venv\Scripts\python.exe -c "import torch; print(torch.__version__, torch.version.cuda, torch.cuda.is_available(), torch.cuda.get_device_name(0) if torch.cuda.is_available() else 'CPU')"
```

Whisper 需要系统 FFmpeg；其官方安装和 Python 调用方式见
[OpenAI Whisper](https://github.com/openai/whisper#setup)。

### 3. 准备 Ollama

质量优先（16GB VRAM + 64GB RAM，下载约 24GB，运行较慢）：

```powershell
ollama pull qwen3.5:35b-a3b
```

更快且可完整放入 16GB 显存的高质量选择（约 11GB）：

```powershell
ollama pull qwen3.5:9b-q8_0
```

模型只需下载一次。之后 UI 会自动启动 Ollama 服务并优先选择已安装的高质量模型，
但不会在未经确认时自动下载大模型。

建议在启动 Ollama 前限制并行与同时驻留模型数，然后重启 Ollama：

```powershell
setx OLLAMA_MAX_LOADED_MODELS 1
setx OLLAMA_NUM_PARALLEL 1
```

### 4. 代理（可选）

不准备代理也可以直接使用原片。长视频或 4K 10-bit 素材建议在 Resolve 或 FFmpeg 中
生成 1080p 代理，并确保代理与源素材起始时间和持续时间一致。导演 JSON 使用秒；
Resolve 执行层会优先读取每个媒体池片段自己的原生 FPS 进行源帧换算，而不是假设所有
素材都等于工程 FPS。

## 一键运行

无需手动启动 Ollama 或 Resolve：程序会启动本机 Ollama 服务但延迟到导演阶段才
加载模型；Ollama 模型卸载后，执行器才自动启动 Resolve 并等待脚本 API。Resolve
Studio 的 External Scripting 仍需预先设置一次为 `Local`。

```powershell
python main.py `
  --input-folder "D:\Documentary\Camera originals" `
  --ollama-model "qwen3.5:35b-a3b" `
  --project-fps 23.976 `
  --chunk-minutes 10
```

也可以按选择顺序重复传入 `--video`；文件夹与显式文件可以同时使用，重复项会去除：

```powershell
python main.py `
  --video "D:\Shoot\A001.mp4" `
  --video "D:\Shoot\B001.mp4" `
  --input-folder "D:\Shoot\B-roll" `
  --ollama-model "qwen3.5:35b-a3b"
```

默认输出：

```text
data/raw_data.json
data/assets/<asset_id>/transcript.srt
data/assets/<asset_id>/keyframes/*.jpg
data/timeline_cuts.json
data/review/CyberEditor_preview.mp4
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
python -m src.review_renderer --help
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
      "confidence": 0.9,
      "story_role": "interview",
      "transition_to_next": "cross_dissolve",
      "transition_duration_sec": 0.5,
      "audio_cleanup": "strong",
      "color_look": "warm",
      "motion": "gentle_push_in"
    }
  ]
}
```

`cut_in_sec` 为包含式入点，`cut_out_sec` 为不包含式出点。Resolve 执行层按每个源片段
的原生 FPS 将入点向下取整、出点向上取整后减一，以适配包含式 `endFrame`。

## 鲁棒性边界

- 视觉模型不会解码每一帧 4K 像素；提取层遍历所有视频并保存时间分布与场景变化兼顾
  的代表帧，再由模型逐窗口审阅。这样才能在长片规模和本地内存限制之间取得平衡。
- Resolve 公共脚本 API 没有稳定的通用转场插入方法。FFmpeg 预览会真正执行 AI 选择
  的转场/降噪/运动效果；Resolve 时间线应用 Voice Isolation、CDL、缩放等受支持效果，
  并用片段标记保留完整效果计划，供后续人工精修。
- 脚本会复用当前 Resolve 时间线；如果没有当前时间线，才创建
  `CyberEditor Timeline`。
- 如果中途某个 Resolve 片段追加失败，API 没有可靠事务回滚；日志会指出已追加
  数量，用户需要检查时间线并撤销。
- 视觉理解由支持图片输入的 Ollama 模型完成。普通 `qwen2.5:3b` 是纯文本烟雾测试
  模型，不能用于此多模态流程；推荐 Qwen 3.5 或其他 Ollama 报告 `vision` 能力的模型。

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
