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

该实例由 `Personal-Website/ops` 中的 Caddy、systemd、受限 sudo 和原子发布脚本管理。合并受保护的 `main` 后，独立 GitHub Runner 只部署审查过的控制面文件。服务器最多保留 512 MiB 预览，自动删除七天前或超额的文件；RAW、字幕、关键帧、Ollama 和 Resolve 永远不上传。

先在部署主机设置两个**不同**的随机密钥并启动控制面：

```bash
export CYBEREDITOR_ADMIN_TOKEN="replace-with-random-admin-token"
export CYBEREDITOR_WORKER_TOKEN="replace-with-a-different-long-worker-token"
docker compose -f docker-compose.control-plane.yml up -d --build
```

容器本身监听 HTTP，应由部署平台或 Caddy/Nginx 提供公开 HTTPS。然后在保存素材、安装了
Ollama/CUDA/Resolve 的 Windows 电脑运行：

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

完整流程不是把所有视频简单首尾相接。每个素材会被依次转写，并默认从头到尾每秒保存
一张连续视觉证据，使视觉模型理解动作的开始、发展、反应和结果，而不是只识别物体。
AI 先根据全部素材生成包含主题、四段叙事节拍、目标时长、可执行调色圣经和音乐情绪的
导演阐述，再让视觉模型按时间顺序审看全部一帧/秒证据和台词。由于 Ollama 无法在一次
请求中容纳一小时的数千张图，内部使用 16 秒、两侧各 2 秒重叠的传输批次；批次不再二次
挑代表帧，并携带累积的角色、地点、动作、情绪和未完成意图摘要，所以不会在边界重置剧情。
第二遍 70B 全局导演只保留服务主题的少数镜头；本地校验器强制按真实素材顺序与素材内
时间码排列，并限制
单镜头与总成片时长。它同时规划硬切/叠化/淡黑、音频降噪、创意色彩、音量、防抖、跟踪、
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
Qwen3.6 音乐导演初审（代表帧 + 台词 + 剧情）
          │ 写出 music_brief.json，keep_alive=0 完全卸载
          ▼
CPU 联网/本地找歌 + Librosa/FFmpeg 音乐听诊
          │ 节拍、强拍、段落、调性、动态范围、LUFS；0 GPU
          ▼
Qwen3.6 最终导演（镜头 + 1–3 段 cue + 留白 + 高潮命中点）
          │ 写出 timeline_cuts.json，keep_alive=0 完全卸载
          ▼
FFmpeg CPU 合成 music_bed.wav
          │ 换歌、淡化、响度统一、对白 ducking
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
- 导演每次只处理一个 10–15 分钟分析窗口（默认 12 分钟），这不是素材数量或
  总时长上限。多个视频及一小时以上素材会拆成多个窗口串行审片，再统一编排。
- `timeline_cuts.json` 只在所有分块成功、校验和合并后原子写入。

## 为什么默认改用 Qwen3.6-27B Dense Q8

- `qwen3.6:27b-mtp-q8_0` 是 27B 全量稠密、原生图文模型，约 30GB；Q8 接近原始
  权重精度，适合 16GB VRAM + 64GB RAM 混合推理。
- 官方视频理解评测中，27B 在 VideoMME、VideoMMMU、MLVU、MVBench 上整体高于
  只激活约 3B 参数的 35B-A3B。后者更快，但本项目优先剪辑判断质量。
- 旧 `Qwen2.5 72B Q5` 虽有更多参数，但模型代际更早、纯文本且约 54GB；参数数量
  不能直接抵消 3.6 的新训练、原生视觉与指令遵循优势，因此不再作为默认导演。

同一 Qwen3.6 模型先结合关键帧与台词生成候选，再进行纯文字全局编排；始终只有一个
模型驻留，也避免卸载、加载另一套 54GB 权重。分页文件脚本仍用于给 Windows 和长上下文
保留安全余量：

```powershell
# 右键 scripts\configure_pagefile_admin.cmd，选择“以管理员身份运行”
# 默认保留 C: 4–8GB，并在 D: 创建 32–48GB；脚本不会自动重启。
```

## 八阶段证据优先自动成片能力

当前实现对应以下严格串行链路：

1. **提取期**：每个视频单独启动 Whisper/OpenCV 子进程，写入台词、真实 JPEG
   关键帧和素材元数据；每个子进程退出后，父调度器再执行显存释放屏障。
2. **音乐导演初审**：Qwen3.6 读取跨素材代表帧、台词和拍摄顺序，先确定主题、情绪
   弧线、检索词、乐器、BPM 范围、人声策略、1–3 个 cue 和有意留白，分别写入
   `director_treatment.json` 与 `music_brief.json`，随后完全卸载。
3. **候选获取与听诊（CPU）**：可选本地授权曲库、带许可证链接的 Jamendo，或显式
   确认后的 yt-dlp 任意在线模式。Librosa/FFmpeg 提取 BPM、全部节拍、强拍、近似
   downbeat、能量段落、整曲能量走势、峰值位置、调性、动态范围和 EBU R128 LUFS，写入
   `music_analysis.json`；候选会按 BPM、检索语义和是否满足“渐强/高潮”弧线综合排序。
4. **全片事实审计与事件契约**：Qwen3.6 再次加载，按顺序审看全片每秒视觉证据与台词；内部 16 秒重叠
   批次只负责适配上下文，不抽掉中间采样，并通过滚动连续性摘要理解完整素材。过程会落盘
   检查点。正式选镜头前，独立的证据角色必须用真实 `candidate_id` 写出人物、目标、状态变化
   和最终可见状态；不足三个已审计状态变化时，不允许硬编因果/BTS 剧情，而会诚实选择人物
   小品或情绪蒙太奇。所有可剪候选都会进入文字导演，不再由
   Python 固定截成 Top 21/28；完整候选表能放入上下文时一次总审，超长项目则由同一导演
   按时间顺序分页审完全部候选后再汇总。`timeline_cuts.json` 会记录每页输入和晋级 ID，
   便于确认没有候选未经导演审阅。镜头时长按角色动态限制：B-roll 10 秒、
   桥段 12 秒、语境 20 秒、高潮 25 秒、结尾 30 秒、完整采访语义最长 45 秒。
   镜头在选中时就标记自然声、踩拍、乐句开头、递进、高潮命中或释放；本地校验器再从
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
   启用 UI 中“Resolve 导出最终成片”后，执行器创建 Render Job、启动
   渲染、持续报告百分比并校验完成状态；默认沿用当前 Deliver 页面格式/编码设置，
   也可填写一个现有的 Resolve 渲染预设名。

### 先锁画面，再配音乐与字卡

全局导演现在必须先完成可校验的 **picture lock（画面锁定）**。每个入选镜头都要给出
精确的源素材裁切范围、叙事功能、观众从该镜头获得的新信息、声音意图和音乐剪辑角色。
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

音乐导演只在最终镜头和时长确定后工作，cue 与同步点必须落在真实镜头边界；对白 ducking
按台词的精确时间区间执行，不再把整条素材都压低。CPU 音乐床渲染后还会测量实际峰值，
全静音或近似静音的 WAV 会令阶段明确失败，不能悄悄进入 Resolve。导演可输出标题卡、
章节、人物下三分之一和结尾卡；同一份 `graphics_plan` 会在 FFmpeg 预览中绘制，并在
Resolve 中插入可编辑 Text+。字卡用于明确主题或主动选择另类风格，不能用来掩盖无意义
的镜头选择。

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
- Python 3.10 或 3.11 64 位
- 16GB 系统内存；运行 32B/70B 混合推理时建议 32–64GB
- 至少 20GB 可用磁盘空间，另加代理素材和模型空间
- FFmpeg
- Ollama
- DaVinci Resolve Studio，且 External Scripting 已设为 `Local`

### 模型建议

| 设备 | Whisper | Ollama 建议 | 说明 |
|---|---|---|---|
| 核显/纯 CPU | `tiny` / `base` | Qwen 3.6 27B Q4（很慢） | 可运行但建议更小视觉模型 |
| 8–12GB VRAM | `small` / `turbo` | `qwen3.6:27b-mtp-q4_K_M` | 约 18GB，RAM/GPU 混合 |
| 16GB VRAM | `large-v3` | `qwen3.6:27b-mtp-q4_K_M` | 约 18GB，大部分权重可卸载至 GPU |
| 16GB VRAM + 64GB RAM | `large-v3` | `qwen3.6:27b-mtp-q8_0` | 约 30GB，稠密 Q8，质量优先默认档 |
| 24GB+ VRAM | `large-v3` | Qwen 3.6 27B Q8 | 仍建议保留串行策略 |

你的 16GB Quadro + 64GB RAM 默认选择 `qwen3.6:27b-mtp-q8_0`；若更看重速度和
磁盘空间，则选择 Q4_K_M。两者都支持文字、图片与思考模式：
[Qwen 3.6 on Ollama](https://ollama.com/library/qwen3.6)。

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

质量优先（16GB VRAM + 64GB RAM，下载约 30GB，运行较慢）：

```powershell
ollama pull qwen3.6:27b-mtp-q8_0
```

更快、更省磁盘的高质量选择（约 18GB）：

```powershell
ollama pull qwen3.6:27b-mtp-q4_K_M
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
  --ollama-model "qwen3.6:27b-mtp-q8_0" `
  --project-fps 23.976 `
  --chunk-minutes 10
```

也可以按选择顺序重复传入 `--video`；文件夹与显式文件可以同时使用，重复项会去除：

```powershell
python main.py `
  --video "D:\Shoot\A001.mp4" `
  --video "D:\Shoot\B001.mp4" `
  --input-folder "D:\Shoot\B-roll" `
  --ollama-model "qwen3.6:27b-mtp-q8_0"
```

默认输出：

```text
data/raw_data.json
data/assets/<asset_id>/transcript.srt
data/assets/<asset_id>/keyframes/*.jpg
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
  --ollama-model "qwen3.6:27b-mtp-q8_0" `
  --creative-brief "按拍摄时间讲述团队从准备到完成动作的过程" `
  --target-duration-sec 80 `
  --camera-profile sony_pp8_slog3_sgamut3cine `
  --music-provider local `
  --music-folder "D:\Music\Licensed"

# 任意在线候选（更推荐在 UI 中运行，以看到完整、不可跳过的警告）
python main.py --skip-extraction --skip-resolve `
  --ollama-model "qwen3.6:27b-mtp-q8_0" `
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

- “完整审片”指默认从头到尾审看每秒一张视觉证据和全部台词，不是让 VLM 解码原始
  59.94 fps 的每一帧；一小时素材约审看 3600 张图。全部采样会按重叠批次传输且不再
  二次抽样，这是长片完整时间覆盖与本地上下文限制之间的明确边界。
- Resolve 公共脚本 API 没有稳定的通用转场插入方法。FFmpeg 预览会真正执行 AI 选择
  的转场/降噪/运动效果；Resolve 时间线应用 Voice Isolation、CDL、缩放等受支持效果，
  并用片段标记保留完整效果计划，供后续人工精修。
- 普通 Rec.709 计划可复用当前空工程；需要 PP8 技术还原且当前工程已有时间线时，脚本
  会创建独立 `Director Cut` 工程，防止全局色彩设置污染已有项目。
- 如果中途某个 Resolve 片段追加失败，API 没有可靠事务回滚；日志会指出已追加
  数量，用户需要检查时间线并撤销。
- 视觉理解由支持图片输入的 Ollama 模型完成。普通 `qwen2.5:3b` 是纯文本烟雾测试
  模型，不能用于此多模态流程；质量优先时建议使用 `qwen3.6:27b-mtp-q8_0`。

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
