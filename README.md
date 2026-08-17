# CyberEditor-Agent

[简体中文](README.md) | [English](README_EN.md)

## Windows 桌面 UI / Desktop UI

安装依赖后，双击仓库根目录的 `launch_ui.bat` 即可启动图形界面，也可以在
PowerShell 中运行：

```powershell
.\.venv\Scripts\python.exe gui.py
```

## 本地网页版 / Web Studio

安装完成后可以双击 `launch_web.bat`，或运行：

```powershell
.\.venv\Scripts\python.exe web.py
```

浏览器会自动打开 `http://127.0.0.1:8765/`。网页版与桌面版共用同一个
`WorkflowOptions` 和 `main.py` 串行调度器，支持多视频/文件夹选择、主题要求、自动硬件配置、
Ollama/Resolve 环境状态、实时日志、阶段进度、安全停止以及浏览器内播放审片预览和最终成片。
Web 服务本身只使用 Python 标准库，也不会导入 Torch、Whisper、OpenCV 或 Resolve，因此常驻
浏览器控制台不占用模型显存。

[tonytan.me/cybereditor/](https://tonytan.me/cybereditor/) 是浏览器版本的主界面。用户先自行启动 Ollama 和 `python3 web.py --no-browser`（Windows 可双击 `launch_companion.bat`），再回到网页点击连接；素材选择、环境检测、Ollama 模型列表、工作流启动/停止、日志和输出都在浏览器 UI 中操作，并来自本仓库真实的 `WorkflowManager`。`http://127.0.0.1:8765/` 仅作为浏览器本地网络权限异常时的同源备用界面。EC2 不接收 RAW、模型请求或执行指令。

浏览器出于安全原因不能直接读取任意本机绝对路径。本项目在本机 Windows 模式下通过服务端
打开原生多文件/文件夹选择器，不上传或复制数百 GB 的 4K 素材。若只需要在同一台电脑上使用，
Resolve、Ollama、FFmpeg 和素材都位于运行 `web.py` 的 Windows 主机。

默认只监听回环地址。若要部署到局域网、工作站或反向代理后方，必须设置至少 16 个字符的令牌：

```powershell
.\.venv\Scripts\python.exe web.py --host 0.0.0.0 --port 8765 --token "请替换为随机长令牌" --no-browser
```

首次从另一台设备访问时，在右上角“访问令牌”中输入相同令牌。请优先放在可信局域网、VPN 或
带 HTTPS 的认证反向代理后方；不要把 Resolve 控制端口和本服务直接暴露到公网。

## 云端网页 + 本机执行 / Remote Control Plane

部署版采用“云端控制面 + Windows 本机 Worker”，不是在部署主机上安装 Ollama 或 Resolve：

```text
任意浏览器 ─HTTPS─> 云端 Control Plane <─出站 HTTPS 轮询─ Windows Worker
                                                        ├─ 本机 RAW / 代理素材
                                                        ├─ 本机 Ollama / CUDA / FFmpeg
                                                        └─ 本机 DaVinci Resolve Studio
```

- 云端只保存任务、进度、有限日志和可选的 720p 低码率预览；RAW、关键帧、字幕与模型留在本机。
- Worker 主动向云端建立出站 HTTPS 连接，不需要家庭公网 IP、端口转发，也不暴露 Resolve API。
- 网页“选择多个视频/文件夹”会让原生选择框弹在选中的 Windows Worker 上；路径由本机使用。
- 网页顶部展示的 CUDA、Ollama 和 Resolve 状态均由本机 Worker 检测，不是部署服务器的状态。
- 这是需要 SQLite 与持久磁盘的轻量控制服务，适合 Docker VPS、Render/Railway 持久服务等，
  不是纯静态站或短生命周期 Serverless Function。

公开默认入口 [tonytan.me/cybereditor/](https://tonytan.me/cybereditor/) 是真实本机应用模式，直连这台电脑上只监听回环地址的源码 companion。需要从外部设备远程控制 Windows Worker 时，使用 `?remote=1` 进入受管理令牌保护的云端控制面；Worker 仍只主动发起出站 HTTPS 连接。旧的浏览器假数据 Demo 已删除。

公开入口和项目自有 404 都发布同一组 Open Graph/Twitter 元数据，便于 iPhone
和消息应用生成项目预览。界面变化时应同步更新 1200×630 的
`web/share-card.png`，且预览图不得包含访问令牌、素材路径或客户媒体。

`/cybereditor/` 下的页面导航由本仓库控制平面负责。未知浏览器页面返回
CyberEditor 自己的双语 HTML 404；未知 API、Worker 和静态资源请求继续返回
原生 JSON 404，不由 Caddy 维护或猜测应用路由。

该实例由 `Personal-Website/ops` 中的 Caddy、systemd、受限 sudo 和原子发布脚本管理。合并受保护的 `main` 后，独立 GitHub Runner 只部署审查过的控制面文件。服务器最多保留 512 MiB 预览，自动删除七天前或超额的文件；RAW、字幕、关键帧、Ollama 和 Resolve 永远不上传。

先在部署主机设置两个**不同**的随机密钥并启动控制面：

```bash
export CYBEREDITOR_ADMIN_TOKEN="replace-with-random-admin-token"
export CYBEREDITOR_WORKER_TOKEN="replace-with-a-different-long-worker-token"
docker compose -f docker-compose.control-plane.yml up -d --build
```

容器本身监听 HTTP，应由部署平台或 Caddy/Nginx 提供公开 HTTPS。然后在保存素材、安装了
Ollama/CUDA/Resolve 的 Windows 电脑运行：

控制面容器使用固定摘要的 Python 3.14 slim 镜像；Windows 编辑工作站继续使用经过
PyTorch、Whisper、OpenCV 与 Librosa 真实安装验证的 Python 3.12 环境。两套运行时由
独立 CI 检查，避免云端容器升级影响本机 CUDA 工作流。

```powershell
$env:CYBEREDITOR_WORKER_TOKEN = "replace-with-a-different-long-worker-token"
.\.venv\Scripts\python.exe worker.py --server "https://edit.example.com"
```

也可以设置环境变量后双击 `launch_worker.bat https://edit.example.com`。打开部署网页，在右上角
输入 `CYBEREDITOR_ADMIN_TOKEN`，选择在线电脑后即可远程发起本机工作流。管理密钥不会下发给
Worker，Worker 密钥也不进入浏览器。若不希望云端保留任何成片副本，启动 Worker 时添加
`--no-preview-upload`；此时日志与控制仍可用，但只能回到本机查看输出。

桌面界面可一次多选任意数量的视频，或递归读取一个素材文件夹；还提供 Whisper/Ollama
参数、断点续跑模式、Resolve 开关、可观看预览、实时日志、阶段进度、停止任务和打开
输出目录等功能。现代界面使用轻量级 CustomTkinter，并通过
子进程调用原有 `main.py`；常驻 UI 不导入 PyTorch，不会改变严格串行与显存释放策略。

界面顶部提供“成片主题 / AI 导演要求”输入框：可以输入希望表达的主题、受众、节奏或
结局（例如“从车友集合到共同出发的热血夜骑短片”）；留空则由 AI 根据全部素材自由发现
主题。目标时长是上限与创作目标，不是填充指标；本地守门只补齐开场、发展、高潮和结尾，
不会再强制每个源文件都进入成片，也不会用重复动作或倒计时凑时长。

完整流程不是把所有视频简单首尾相接。每个素材会被依次转写，并默认从头到尾每 0.5 秒保存
一张按时间排序的 JPEG 视觉证据（2fps），使视觉模型理解动作的入画、顶点、反应和结果，而不是只识别物体。
视觉模型先在不带预设主题的情况下审完全部证据，建立动作原子与连续性账本；再比较多个有证据的故事方向，
生成主题、叙事节拍、目标时长、调色圣经和音乐情绪。“完整覆盖”不等于把一小时影像塞进单次请求：Ollama 的图片数和上下文有上限，
因此 2fps 证据会先尝试按 10 秒核心、最多 1 秒重叠的窗口顺序传输，并通过滚动人物/地点/动作状态跨越边界。每次请求前会同时估算文字、Schema、输出余量与图片 token；若当前 Context 放不下，核心窗口自动缩短，且单次不超过 32 张图，而不是静默丢弃中间采样。中立账本完成后，系统会把不超过 8 秒的短动作原子重新抽取为最高 4fps 的连续帧，二次确认可读入点、动作顶点和干净出点；对白仍以 Whisper 时间码为准，不让无声视觉模型剪断字句。
第二遍全局文字导演只保留服务主题的少数镜头；本地校验器强制按真实素材顺序与素材内
时间码排列，并限制
单镜头与总成片时长。为保证 FFmpeg 审片、节目原声和 Resolve 成片逐帧同长，当前自动交付
统一执行硬切；模型若提出叠化/淡黑，会保留为审计提示但不会在一个引擎中单独执行。它还规划音频降噪、创意色彩、音量、防抖、跟踪、
轻微推镜，以及由双导演流程选择并预混的 1–3 段配乐。最终可同时输出 Resolve 可编辑时间线、
FFmpeg 1080p 审片预览，以及由 Resolve Deliver 页面真正渲染的最终成片。

在线找歌使用严格清单：旧下载缓存不能冒充用户本地音乐，访谈、播客、解说和节目类结果
会在下载与分析阶段被拒绝。整片采用一个全局创意色板，并允许开场、发展、高潮和结尾做
受限的曝光、对比度、饱和度与冷暖递进；S-Log 编码域内测得的曝光/白平衡统计不会在技术
还原后被错误套用，既保持素材一致，又让导演的情绪调色真正落到 FFmpeg 与 Resolve。

“性能配置”默认使用 `自动检测 / Auto`：界面以标准库读取 CPU、系统内存和 GPU，
通过 `nvidia-smi`（可用时）读取准确显存，并在一次性子进程中确认当前 PyTorch 是否
真的支持 CUDA。它会自动选择 Whisper 模型、设备模式、10–15 分钟分块时长、
Ollama 上下文，以及在内存预算内质量最合适的**已安装** Ollama 模型；模型排序会
考虑中文、长上下文、指令遵循和量化，不再只比较文件大小。自动档不会擅自下载十几
GB 的模型，也不会让硬件性能档修改素材属性。16GB VRAM + 64GB RAM 会默认进入
“高质量（较慢）”：Whisper `large-v3`、10 分钟分块、32K 上下文。

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

提取器现在优先读取每条 Sony `CxxxxM01.XML` 伴随文件，不再把整批素材强行当成
同一种 PP8。S-Log2/S-Gamut、S-Log3/S-Gamut3 和 S-Log3/S-Gamut3.Cine 会分别
映射为 Resolve 的逐素材输入变换；XML 缺失时才使用界面中的显式回退配置。工程工作
空间统一为：

```text
每条素材的 Sony XML 输入色彩空间 / Gamma
        → DaVinci Wide Gamut / Intermediate
        → Rec.709 Gamma 2.4
```

OpenCV 会在关键帧上估计中性像素、亮度与 RGB 平衡，按整批素材的中位参考生成受限
修正（曝光 ±1.5EV、RGB 增益 0.667–1.5）；低置信度结果自动向“不修正”收敛，避免
把有意的彩色灯光强行校白。任何 Log 素材的关键 Resolve 设置被拒绝都会停止执行，
不再静默输出灰片。FFmpeg 预览对 S-Log3 应用技术 LUT；混入 S-Log2 时以最终 Resolve
RCM 输出为准，绝不会错误套用 S-Log3 LUT。

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
Qwen3.8 中立全片审片（2fps 全覆盖 + 短动作最高 4fps 复审 + 台词）
          │ 写出 footage_ledger / treatment / music_brief，keep_alive=0 卸载
          ▼
CPU 联网/本地找歌 + Librosa/FFmpeg 音乐听诊
          │ 节拍、强拍、段落、调性、动态范围、LUFS；0 GPU
          ▼
Qwen3.8 最终导演（实测配乐 profile 参与画面节奏 + 锁画后精确 cue）
          │ 写出 timeline_cuts.json，keep_alive=0 完全卸载
          ▼
FFmpeg CPU 合成 music_bed.wav
          │ 换歌、淡化、响度统一、对白 ducking
          ▼
FFmpeg 预览渲染子进程
          │ 生成与 Resolve 同步的硬切、降噪与基础运动效果
          ▼
DaVinci Resolve 执行子进程
          │ 原生效果 / DRX / 可选受保护 UI 宏 / 最终渲染
```

- 父调度器不导入 PyTorch、Whisper、OpenCV、requests 或 Resolve API。
- 任意时刻最多存在一个项目重型子进程。
- Ollama `/api/ps` 若发现其他模型驻留，会拒绝加载导演模型。
- 导演每次只处理一个 10–15 分钟分析窗口（默认 12 分钟），这不是素材数量或
  总时长上限。多个视频及一小时以上素材会拆成多个窗口串行审片，再统一编排。
- `timeline_cuts.json` 只在所有分块成功、校验和合并后原子写入。

## 为什么分开 Qwen3.8 视觉审片与文字导演

- 16GB VRAM 机器的 2fps 视觉审片优先 `hf.co/ggml-org/Qwen3.8-27B-GGUF:Q4_K_M`；约 19GB 的 Q4_K_M 权重能减少高密度传图时的 RAM/PCIe 抖动。
- 视觉阶段完全卸载后，全局文字导演优先 `hf.co/ggml-org/Qwen3.8-27B-GGUF:Q8_0`；约 28.6GB，Q8 用于保留最终故事比较与结构决策精度。
- 官方视频理解评测中，27B 在 VideoMME、VideoMMMU、MLVU、MVBench 上整体高于
  只激活约 3B 参数的 35B-A3B。后者更快，但本项目优先剪辑判断质量。
- 旧 `Qwen2.5 72B Q5` 虽有更多参数，但模型代际更早、纯文本且约 54GB；参数数量
  不能直接抵消 3.8 的新训练、原生视觉能力与指令遵循优势，因此不再作为默认导演。

自动配置只会从 Ollama 实际已安装的标签中分别选择两个角色；若只安装一个兼容模型，则两阶段串行复用它。Qwen3.8-27B 已于 2026-08-14 发布；本项目通过 ggml-org 的 GGUF 直接引用运行，因为 Ollama 公共模型库尚无可依赖的第一方短标签。参见 [Qwen3.8-27B 官方模型卡](https://huggingface.co/Qwen/Qwen3.8-27B) 与 [ggml-org GGUF](https://huggingface.co/ggml-org/Qwen3.8-27B-GGUF)。
分页文件脚本仍用于给 Windows 和长上下文保留安全余量：

```powershell
# 右键 scripts\configure_pagefile_admin.cmd，选择“以管理员身份运行”
# 默认保留 C: 4–8GB，并在 D: 创建 32–48GB；脚本不会自动重启。
```

### 当前视觉输入边界：完整有序采样，不是假装原生视频

当前 Windows/Ollama 后端调用的是 Ollama 的 `images` 数组，传入按时间排序的 JPEG，
**没有**把 MP4 作为连续视频流直接交给模型。这里的“完整审片”准确含义是：默认 2fps
从头到尾无时间空洞、所有台词均保留、窗口按 Context 自适应且短动作会以最高 4fps 加密复审；
它不等于观看原始 59.94fps 的每一帧，也不保留帧间光流或原始音轨作为模型视频输入。
[Ollama Vision 文档](https://docs.ollama.com/capabilities/vision)目前公开的是图片输入。

vLLM 已公开 OpenAI 兼容的 `video_url` 输入及视频解码后端，可作为未来的可选原生视频后端，
但本仓库尚未实现该适配，不能写成现有能力；而且 vLLM 官方 GPU 安装以 Linux 为主，Windows
需 WSL 或独立 Linux 模型节点。参见 [vLLM 多模态视频输入](https://docs.vllm.ai/en/latest/features/multimodal_inputs/)
和 [vLLM GPU 安装要求](https://docs.vllm.ai/en/latest/getting_started/installation/gpu/)。

## 八阶段证据优先自动成片能力

当前实现对应以下严格串行链路：

1. **提取期**：每个视频单独启动 Whisper/OpenCV 子进程，写入台词、真实 JPEG
   关键帧和素材元数据；每个子进程退出后，父调度器再执行显存释放屏障。
2. **中立全片审片与音乐初审**：Qwen3.8 先不带主题偏见地读完 2fps 画面、台词与拍摄顺序，再对不超过 8 秒的动作原子以最高 4fps 复看精确边界，落盘
   `footage_ledger.json`，再比较多个可被真实时间码证明的故事方向，生成 `director_treatment.json` 和
   `music_brief.json`。音乐简报包含情绪弧线、检索词、乐器、BPM、1–3 个 cue 和有意留白，随后模型完全卸载。
3. **候选获取与听诊（CPU）**：可选本地授权曲库、带许可证链接的 Jamendo，或显式
   确认后的 yt-dlp 任意在线模式。Librosa/FFmpeg 提取 BPM、全部节拍、强拍、近似
   downbeat、能量段落、整曲能量走势、峰值位置、调性、动态范围和 EBU R128 LUFS，写入
   `music_analysis.json`；候选会按 BPM、检索语义和是否满足“渐强/高潮”弧线综合排序。
4. **证据契约与全局编排**：文字导演加载后读取已通过指纹验证的完整证据账本、`music_analysis.json`
   中经过人声排除的候选及其 BPM、段落和能量 profile，不再用旧的 v2 候选缓存。
   正式选镜头前，独立的证据角色必须用真实 `candidate_id` 写出人物、目标、状态变化
   和最终可见状态；不足三个已审计状态变化时，不允许硬编因果/BTS 剧情，而会诚实选择人物
   小品或情绪蒙太奇。所有可剪候选都会进入文字导演，不再由
   Python 固定截成 Top 21/28；完整候选表能放入上下文时一次总审，超长项目则由同一导演
   按时间顺序分页审完全部候选后再汇总。`timeline_cuts.json` 会记录每页输入和晋级 ID，
   便于确认没有候选未经导演审阅。镜头时长按角色动态限制：B-roll 10 秒、
   桥段 12 秒、语境 20 秒、高潮 25 秒、结尾 30 秒、完整采访语义最长 45 秒。
   画面编排不是先忽略音乐：导演先根据可用曲目的实测 `score_profiles` 设计镜长、递进与高潮，
   镜头在选中时就标记自然声、踩拍、乐句开头、递进、高潮命中或释放；锁画后第二个受约束请求再从
   真实强拍/downbeat 补齐 2–6 个有效同步点。一般纯画面在 ±0.45 秒、高潮在最多 ±0.75 秒
   内吸附，采访对白绝不为卡点截断。
5. **画面编排与陌生观众盲审**：画面导演和总剪辑师完成方案后，另一个隔离 Prompt 只看到
   实际镜头画面摘要与可听对白，不会看到主题、导演理由或目标结论。它必须独立复述“谁、想
   做什么、发生了什么变化、为何这样结束”；连贯性与对应的因果/视觉兑现均至少 7/10 才通过。
6. **音乐床与低清成片**：模型卸载后，FFmpeg 在 CPU 上按 cue 表裁切 1–3 首音乐、
   淡入淡出、响度匹配、对白 ducking 并生成确定性的 `music_bed.wav`。Resolve 只需
   将这一条音乐床导入 A2；在进入 Resolve 前，FFmpeg 先渲染低清粗剪。
7. **真实粗剪回看与自动重剪**：视觉模型按时间顺序重新观看低清粗剪的全部自适应采样帧，
   再结合字面可听对白和音乐位置做陌生观众测试。失败意见会自动交回导演重剪，默认最多两次；
   仍看不懂时阻止 Resolve 输出坏成片，并保留 `review/rough_cut_review.json` 供排查。
8. **Resolve 执行与导出**：Resolve 原生 API 完成素材导入、按源素材 FPS 换帧、拼接、Voice
   Isolation、基础 CDL、`Stabilize()`、`CreateMagicMask()` 和 `SmartReframe()`；
   用户导出的 DRX 通过节点图 `ApplyGradeFromDRX()` 注入。Resolve 21 已原生公开
   防抖与 Magic Mask 接口，因此默认不使用脆弱的坐标宏。
   字卡不走未公开、不可稳定自动插入的 Text+ 路径；每一项会渲染成经帧数、分辨率与 alpha
   校验的短 ProRes 4444 透明片段，并按精确 record frame 放到 V2。
   启用 UI 中“Resolve 导出最终成片”后，执行器创建 Render Job、启动
   渲染、持续报告百分比并校验完成状态；默认沿用当前 Deliver 页面格式/编码设置，
   也可填写一个现有的 Resolve 渲染预设名。

### 先以实测配乐设计画面节奏，锁画后再精确 cue 与字卡

配乐与画面采用两级耦合，而不是“完全锁画后才第一次看音乐”。文字导演在选镜头之前先读取
候选曲目的实测 `score_profiles`（BPM、段落、能量走势与是否真的存在高潮），据此设计镜长、
递进、自然声留白和 `music_edit_role`。画面通过证据契约、总剪辑复审和陌生观众盲审后形成
可校验的 **picture lock（画面锁定）**；随后单独的 Schema 受约束请求才根据最终时间线边界、
精确对白区间、强拍和 downbeat 写出 0–3 段 cue。这样既让镜头节奏从一开始就能服务真实音乐，
又避免后续画面变动让高潮命中点落到不存在的剪点上。每个入选镜头都要给出精确的源素材裁切
范围、叙事功能、观众从该镜头获得的新信息、声音意图和音乐剪辑角色。
系统会把关于麦克风、滤镜、站位和重复拍摄的对白标记为“可能的制作现场语境”，但该标记
只提供给导演参考，绝不自动裁切或静音。导演会结合主题自行决定排除、保留对白、作为环境
质感、与音乐混合或完全静音；执行层只忠实落实导演写入 `audio_intent` 的选择。

最终锁画前先建立逐事件证据契约，再进行“总剪辑师复审”和隔离的“陌生观众盲审”。后者看不到
导演阐述、镜头功能标签和目标主题，不能用导演自己的解释替成片辩护。审查会区分画面真实发生
的动作与模型推测的下一步，
例如“列队准备”不等于“已经出发”，“倒计时/前倾”也不等于“驶离”。用户要求的事件没有被
素材拍到时，导演必须在 `absent_or_unproven_events` 中明确记录，并把主题诚实改写为素材能够
证明的集结仪式、人物关系、蓄势或其他叙事。每个镜头都要提供 `evidence_claim` 和
`connection_to_previous`。总剪辑师确认后，Python 只校验时间与格式，不再自动补镜头、语义
去重、重排或按分数删减导演锁画。

复审模型还会收到初稿的量化剪辑报告，包括对白占比、平均镜长、静态镜头占比、连续重复的
叙事功能/音乐角色和字卡数量，并必须列出具体问题及实际改动，不能原样批准初稿。对两分钟
以内、并非明确以对白为主的短片，默认只保留真正改变理解或揭示人物的台词，常规技术沟通
通常一个简洁片段就足够；字卡默认 0–2 个。若画面多样性不足，导演应主动缩短成片，而不是
用现场闲聊、重复动作或包装性标题凑时长。Python 仍不替导演挑选镜头，但会把没有证据支持的
对白主导方案或盲审失败方案退回模型重剪。

复审不是只相信模型自评。程序会重新测量模型实际返回的镜头表；如果明确选择纯视觉/音乐
蒙太奇的方案仍有超过 55% 的对白、任何非访谈/非证据化对白结构有超过 72% 可听语音、制作
现场闲聊与选择的叙事形式冲突、动态候选充足却有超过 75% 静态镜头、连续四个镜头仍是
同一叙事/音乐功能、缺少递进/反差/兑现，或景别严重单一，方案会连同实测问题退回同一个
导演模型重剪。采访、观察式纪录片、幕后协作等由现场对话推动的结构不会被机械套用 55%
阈值；`teaser_then_chronological` 也允许开场预告与结尾高潮相互呼应。

Python 不替模型选择、删除或静音镜头，而是在多个完整的 AI 导演方案中保留实测最好的一个。
只有上一轮确实改善时才继续第二轮重剪。JSON 阶段仍会记录尚存建议，但真实低清成片随后必须
通过隔离盲审；默认两次成片反馈重剪后仍失败，工作流会停止在 Resolve 之前。候选表会向导演提供
精确合并的 `speech_ranges` 与 `silent_ranges`；静音画面裁切不会再被误算成整段对白。所有真正
未静音的语音仍会计入比例，`natural_texture` 不能被用来伪装已经保留的人声。

Whisper 的结构性静音幻听也会在新提取及读取旧 `raw_data.json` 时过滤。例如三个字被错误
标成十几秒对白的片段，不再让整段画面被判定为对白，也不会让音乐 ducking 无故持续十几秒。

27B Q8 与 70B 在显存/RAM 混合推理时，单次全局编排可能超过 30 分钟。调度器默认将单次
Ollama 读取超时设为 7200 秒（2 小时），期间每 15 秒发送真实耗时心跳；慢推理不再因旧的
1800 秒上限被误判失败。命令行用户仍可通过 `--ollama-timeout` 明确调整。

锁画后的 cue 导演只负责精确选曲段、进出点与同步点；画面节奏在更早的镜头编排阶段已经参考
实测配乐 profile。对白 ducking 按台词的精确时间区间执行，不再把整条素材都压低。CPU 音乐床渲染后还会测量实际峰值，
全静音或近似静音的 WAV 会令阶段明确失败，不能悄悄进入 Resolve。导演可输出标题卡、
章节、人物下三分之一和结尾卡；同一份 `graphics_plan` 会在 FFmpeg 预览中绘制，并在
Resolve 中渲染为短 ProRes 4444 透明片段并放置到 V2。该路径保证预览与最终时间线的字卡
画面和帧位置一致，但它不是可编辑 Text+；要修改文字，应修改 `graphics_plan` 后重新执行。
字卡用于明确主题或主动选择另类风格，不能用来掩盖无意义的镜头选择。

### 任意在线音频模式与版权

UI 默认提供“任意在线音频 · 效果优先”，但**每次运行都必须重新确认**；确认不会保存。
该模式使用 yt-dlp 搜索和下载候选，不使用 Cookie、DRM 绕过或登录自动化。项目会保存
来源 URL、用户本次声明、许可证字段和文件 SHA-256 到审计 JSON。可下载或确认弹窗都不
等于获得版权：用户必须自行拥有下载、改编、与画面同步和发布所需的权利，并遵守来源
平台条款；商用前必须取得覆盖商用、改编和同步的明确授权。公开项目更稳妥的默认做法是
改选本地授权曲库或 Jamendo 可验证许可证模式。

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
├─ launch_web.bat                # 双击启动本地 Web Studio
├─ launch_companion.bat / .command # 供 tonytan.me 直连的仅回环启动器
├─ web.py                        # 浏览器控制台入口
├─ control_plane.py              # 可部署的轻量云端控制面
├─ worker.py                     # 主动出站连接的 Windows 本机 Worker
├─ launch_worker.bat             # 双击启动 Worker
├─ Dockerfile
├─ docker-compose.control-plane.yml
├─ web/                          # 零依赖 HTML/CSS/JavaScript 前端
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
│  ├─ control_plane.py           # SQLite 任务队列、认证与预览中继
│  ├─ remote_worker.py           # 本机环境检测、命令执行与状态上报
│  ├─ runtime_services.py        # 跨盘软件发现与 Ollama/Resolve 自动启动
│  ├─ media_manifest.py          # 多视频/文件夹发现、去重与代理映射
│  ├─ ui_i18n.py                 # 不依赖 GUI 包的中英文文案
│  ├─ extractor.py               # Whisper + OpenCV 数据提取
│  ├─ director.py                # 双导演初审、分块审片、多 cue 全局编排
│  ├─ music_analyzer.py          # 本地/Jamendo/yt-dlp 获取与 CPU 音乐听诊
│  ├─ music_bed.py               # 多曲裁切、淡化、ducking 与音乐床合成
│  ├─ review_renderer.py         # FFmpeg 可观看预览与效果渲染
│  ├─ rough_cut_reviewer.py      # 实际低清成片逐段视觉盲审与重剪反馈
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
   ├─ test_music_analyzer.py
   ├─ test_music_bed.py
   ├─ test_orchestrator.py
   ├─ test_runtime_services.py
   └─ test_resolve_executor.py
```

## 硬件与软件要求

### 最低建议

- Windows 10/11 64 位
- Python 3.11 或 3.12 64 位
- 16GB 系统内存只适合开发、测试或更小模型；本项目默认的 27B Q4/Q8 串行质量档建议 64GB，
  70B 混合推理建议 128GB 以上
- 至少 20GB 可用磁盘空间，另加代理素材和模型空间
- FFmpeg
- Ollama
- DaVinci Resolve Studio，且 External Scripting 已设为 `Local`

### 模型建议

| 设备 | Whisper | 视觉审片 | 全局文字导演 | 定位 |
|---|---|---|---|---|
| 当前 Quadro RTX 5000 Max-Q 16GB + 64GB RAM | `large-v3` | Qwen3.8-27B Q4_K_M | Qwen3.8-27B Q8_0 | 能跑完整串行路径；两者都需 RAM/VRAM 混合，长片会很慢；当前后端不是原生连续 MP4 输入 |
| 单 RTX 5090 32GB + 128/192GB RAM | `large-v3` | Qwen3.8-27B Q8 | Qwen3.8-27B Q8；可评测更大纯文字模型 | 消费级 Windows 首选；32GB 仍需给视觉编码器、KV cache 和 Context 留空间，不能假定 28.6GB 权重始终全驻显存 |
| RTX PRO 6000 Blackwell 96GB + 256GB RAM | `large-v3` | Qwen3.8-27B 高精度权重 | 经本项目评测后再启用更大文字导演 | 目前最稳妥的单卡 Windows 工作站上限；容量显著更宽裕，成本和 600W 供电/散热也显著更高 |
| Mac Studio M3 Ultra 512GB 统一内存 | 独立提取或模型节点 | 可容纳更大 MLX 多模态权重 | 可容纳更大本地文字权重 | 容量优先的局域网模型节点；当前注册表、Worker 与 Resolve 自动化是 Windows 路径，不能直接替换而无需后端迁移 |

你的 16GB Quadro + 64GB RAM 不需要为验证这次架构立即换机；默认就是 Qwen3.8-27B
Q4_K_M 视觉审片 + Qwen3.8-27B Q8_0 最终文字导演，代价是大量混合内存推理和等待。
若购买新的单机 Windows 主机，单 RTX 5090 32GB + 128GB（更推荐 192GB）RAM 是消费级首选；
若预算优先于价格，RTX PRO 6000 Blackwell 96GB + 256GB RAM 才是更合适的单卡 Windows
本地模型/Resolve 一体工作站。双 5090 没有 NVLink，64GB 合计显存不等于一张 64GB 显卡，
跨卡推理会受 PCIe 和推理后端的切分策略限制。

更快/更大的硬件只会扩大可用模型、Context 和吞吐，**不会自动修复错误的叙事流程、素材中未拍到的
事件、错误音乐候选或缺少人工参考评测**。本次架构中，中立证据账本、实测配乐参与画面节奏、
锁画后 cue、陌生观众盲审和人工参考 benchmark，比单纯把 27B 换成更大模型更直接地决定成片是否
像人剪的；任何配置都不承诺自动达到专业人类剪辑师水平。

模型说明：[Qwen3.8-27B 官方权重](https://huggingface.co/Qwen/Qwen3.8-27B) 和
[ggml-org Qwen3.8-27B GGUF](https://huggingface.co/ggml-org/Qwen3.8-27B-GGUF)。硬件规格参见
[GeForce RTX 5090](https://www.nvidia.com/en-us/geforce/graphics-cards/50-series/rtx-5090/)、
[RTX PRO 6000 Blackwell](https://www.nvidia.com/en-us/products/workstations/professional-desktop-gpus/rtx-pro-6000/)
和 [Mac Studio](https://www.apple.com/mac-studio/specs/)。

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

质量优先且分离视觉/文字角色（16GB VRAM + 64GB RAM，合计约 47GB）：

```powershell
ollama pull hf.co/ggml-org/Qwen3.8-27B-GGUF:Q4_K_M
ollama pull hf.co/ggml-org/Qwen3.8-27B-GGUF:Q8_0
```

只保留一个较小模型时，两阶段可串行复用 Q4（约 17GB，最终文字导演精度低于 Q8）：

```powershell
ollama pull hf.co/ggml-org/Qwen3.8-27B-GGUF:Q4_K_M
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
  --ollama-model "hf.co/ggml-org/Qwen3.8-27B-GGUF:Q4_K_M" `
  --director-model "hf.co/ggml-org/Qwen3.8-27B-GGUF:Q8_0" `
  --project-fps 23.976 `
  --chunk-minutes 10
```

也可以按选择顺序重复传入 `--video`；文件夹与显式文件可以同时使用，重复项会去除：

```powershell
python main.py `
  --video "D:\Shoot\A001.mp4" `
  --video "D:\Shoot\B001.mp4" `
  --input-folder "D:\Shoot\B-roll" `
  --ollama-model "hf.co/ggml-org/Qwen3.8-27B-GGUF:Q4_K_M" `
  --director-model "hf.co/ggml-org/Qwen3.8-27B-GGUF:Q8_0"
```

默认输出：

```text
data/raw_data.json
data/assets/<asset_id>/transcript.srt
data/assets/<asset_id>/keyframes/*.jpg
data/footage_ledger.json
data/director_treatment.json
data/music_brief.json
data/music_analysis.json
data/timeline_cuts.json
data/music/music_bed.wav
data/music/music_bed.audit.json
data/review/CyberEditor_preview.mp4
data/cybereditor.log
```

### 断点续跑

长视频不应因 Resolve 未启动而重新转写：

```powershell
# 已有 raw_data.json，只重跑导演与 Resolve
python main.py --skip-extraction `
  --proxy "D:\Documentary\proxy\source_1080p.mp4" `
  --ollama-model "hf.co/ggml-org/Qwen3.8-27B-GGUF:Q4_K_M" `
  --director-model "hf.co/ggml-org/Qwen3.8-27B-GGUF:Q8_0" `
  --creative-brief "按拍摄时间讲述团队从准备到完成动作的过程" `
  --target-duration-sec 80 `
  --camera-profile sony_pp8_slog3_sgamut3cine `
  --music-provider local `
  --music-folder "D:\Music\Licensed"

# 任意在线候选（更推荐在 UI 中运行，以看到完整、不可跳过的警告）
python main.py --skip-extraction --skip-resolve `
  --ollama-model "hf.co/ggml-org/Qwen3.8-27B-GGUF:Q4_K_M" `
  --director-model "hf.co/ggml-org/Qwen3.8-27B-GGUF:Q8_0" `
  --music-provider yt_dlp --music-candidate-limit 8 `
  --music-rights-confirmed `
  --music-rights-claim "我拥有下载、改编、同步和使用候选音频所需的权利，并会遵守来源平台条款"

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
python -m src.music_analyzer --help
python -m src.music_bed --help
python -m src.review_renderer --help
python -m src.resolve_executor --help
```

三个核心类也可被外部 Python 模块直接导入：

```python
from src.extractor import MediaExtractor
from src.director import AIDirector
from src.music_analyzer import LicensedMusicAnalyzer
from src.music_bed import MusicBedRenderer
from src.resolve_executor import DaVinciExecutor
```

## JSON 契约

导演输出兼容以下核心结构：

```json
{
  "schema_version": "3.0",
  "project_fps": 59.94,
  "director_treatment": {
    "title": "从准备到同步",
    "central_theme": "混乱的准备最终凝聚成准确的团队动作",
    "chronology_policy": "strict_chronological",
    "target_duration_sec": 82.5,
    "creative_look": "cinematic_warm"
  },
  "color_pipeline": {
    "camera_profile": "sony_pp8_slog3_sgamut3cine",
    "input_color_space": "Sony S-Gamut3.Cine",
    "input_gamma": "S-Log3",
    "timeline_color_space": "DaVinci WG",
    "timeline_gamma": "DaVinci Intermediate",
    "output_color_space": "Rec.709",
    "output_gamma": "Gamma 2.4"
  },
  "music_plan": {
    "mode": "multi_cue_pre_mix",
    "bed_file": "D:\\Project\\data\\music\\music_bed.wav",
    "strategy": "克制开场，发展段渐强，结尾留白",
    "silence_regions": [
      {"timeline_in_sec": 36.0, "timeline_out_sec": 39.0, "reason": "保留关键对白"}
    ],
    "cues": [
      {
        "cue_id": "M1",
        "track_file": "restrained-build.wav",
        "timeline_in_sec": 0.0,
        "timeline_out_sec": 36.0,
        "track_in_sec": 12.0,
        "track_out_sec": 48.0,
        "target_lufs": -24.0,
        "duck_under_dialogue_db": -9.0,
        "source_url": "https://example.invalid/source",
        "license": "用户提供的授权记录"
      }
    ]
  },
  "clips": [
    {
      "clip_id": 1,
      "file_name": "D:\\Documentary\\proxy\\source_1080p.mp4",
      "cut_in_sec": 12.5,
      "cut_out_sec": 18.2,
      "reason_for_cut": "完整表达主题观点",
      "confidence": 0.9,
      "story_role": "interview",
      "transition_to_next": "cut",
      "transition_duration_sec": 0.0,
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

- “完整审片”指默认从头到尾审看 2fps 视觉证据和全部台词，不是让 VLM 解码原始
  59.94 fps 的每一帧；一小时素材约审看 7200 张图。“完整”表示时间轴无空洞，不表示单次请求；
  初始 10 秒核心窗口会按实际文字/Schema/图片 token 预算自适应缩短，单次最多 32 图，并用滚动
  连续性账本串起全片。随后所有不超过 8 秒的候选动作会以最高 4fps 二次复审入点/顶点/出点，
  长对白则保留 Whisper 边界。这是 Ollama 图片序列方案，不是 MP4 原生连续视频理解。
- Resolve 公共脚本 API 没有稳定的通用转场插入方法。为避免 FFmpeg 预览、节目原声与 Resolve
  成片因重叠时长不同而失步，当前执行路径统一使用硬切；模型请求的叠化/淡黑只保留为审计提示。
  FFmpeg 预览仍执行降噪/运动计划，Resolve 时间线应用 Voice Isolation、CDL、缩放等受支持效果，
  并用片段标记保留完整计划，供后续人工精修。
- Resolve 的公开脚本 API 也没有可靠的 Text+ 创建/文本写入契约；当前实现把每项字卡渲染成
  独立、短时长、已校验 alpha 的 ProRes 4444 片段放到 V2。因此成片可复现，但字卡不是原生
  可编辑 Text+。
- 普通 Rec.709 计划可复用当前空工程；需要 PP8 技术还原且当前工程已有时间线时，脚本
  会创建独立 `Director Cut` 工程，防止全局色彩设置污染已有项目。
- 如果中途某个 Resolve 片段追加失败，API 没有可靠事务回滚；日志会指出已追加
  数量，用户需要检查时间线并撤销。
- 视觉理解由支持图片输入的 Ollama 模型完成。普通 `qwen2.5:3b` 是纯文本烟雾测试
  模型，不能用于此多模态流程；16GB VRAM 高密度视觉审片建议 Qwen3.8-27B Q4_K_M，文字总导演建议 Qwen3.8-27B Q8_0。

## 测试

测试不需要 GPU、Ollama 或 Resolve：

```powershell
python -m unittest discover -s tests -v
python -m compileall -q main.py src tests
```

项目还提供 [人工参考剪辑评测](evals/README.md)，可在固定测试素材上比较素材选择
F1、剪辑边界误差、镜头顺序、重复镜头和时长偏差。它用于量化版本回退，不能代替
故事清晰度、情绪、音乐和表演选择的人工盲测。

## 隐私

媒体、台词、关键帧和剪辑 JSON 默认只写入本机 `data/`，该目录内容已被
`.gitignore` 排除。不要把真实客户素材、模型权重、Resolve 工程或日志提交到 issue。
详见 [SECURITY.md](SECURITY.md)。

## 贡献

欢迎提交兼容不同显卡、Ollama 模型和 Resolve 版本的改进。任何新 AI 模块必须
证明不会与其他重型阶段并发。参见 [CONTRIBUTING.md](CONTRIBUTING.md)。

## License

[MIT](LICENSE)
