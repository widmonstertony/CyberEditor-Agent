# CyberEditor-Agent

[简体中文](README.md) | [English](README_EN.md)

A fully local, privacy-first automatic long-form video editing MVP for Windows.

CyberEditor-Agent uses Whisper for timestamped dialogue, OpenCV for actual
visual samples, a vision-capable local Ollama model for chunk review and
cross-source story assembly, FFmpeg for a watchable result, and the DaVinci
Resolve Python API for an editable timeline.

> Project status: runnable MVP. Before using it on a production project, test
> timecode conversion, proxy relinking, and Resolve compatibility on a copy of
> your media. This project is not affiliated with OpenAI, Ollama, Blackmagic
> Design, or Sony.

## Windows desktop UI

After installing the dependencies, double-click `launch_ui.bat` in the
repository root. You can also launch it from PowerShell:

```powershell
.\.venv\Scripts\python.exe gui.py
```

The bilingual desktop UI can multi-select any number of videos or recursively
load a media folder. It also includes Whisper and Ollama settings, resumable
workflow modes, Resolve controls, a watchable preview render, live logs, stage
progress, safe cancellation, environment checks, and output shortcuts.

A prominent `Film theme / AI director brief` field accepts the intended theme,
audience, pace, or ending (for example, “an energetic night-ride short from
meeting up to riding out”). Leave it blank for AI free direction across all
footage. Target runtime is an editorial ceiling and goal, not a padding quota:
the local gate completes setup, development, payoff, and ending without forcing
every source file into the movie or repeating an action/countdown to fill time.

The full workflow does not merely concatenate every file. Each source is
transcribed and sampled from scene changes plus bounded 5–8 second temporal
evidence, allowing the vision model to understand setup, action, reaction, and
payoff rather than merely naming objects. A vision model first writes a
project-wide treatment with theme, four story beats, target runtime, an
executable color bible, and music mood. It then reviews long sources as sequential
visual micro-scenes of no more than three minutes with both images and speech.
A second global-director pass keeps only the
minority of shots that serve that treatment. Local validation enforces actual
source order and in-file timecode, plus per-shot and total-runtime limits. It
plans cuts/dissolves/fades, audio cleanup, creative looks, gentle push-ins,
gain, stabilization, tracking, and a one-to-three-cue pre-mixed score chosen by
the two-director pipeline. The result can include an editable
Resolve timeline, an immediately watchable FFmpeg 1080p review, and a final
movie rendered by Resolve's Deliver pipeline.

Online music uses an authoritative manifest: stale downloads cannot masquerade
as user-supplied tracks, and interview, podcast, narration, and show-like results
are rejected before download and again before analysis. One global palette is
applied across the film, with bounded exposure, contrast, saturation, and warmth
progression for opening, development, payoff, and ending. Exposure/white-balance measurements made in
encoded S-Log space are never incorrectly applied after the technical input
transform, preventing per-shot brightness and color drift.

The modern UI uses lightweight CustomTkinter and launches the existing
`main.py` orchestrator as a child process. The resident UI never imports
PyTorch, so the strict serial execution and VRAM-release guarantees remain
intact.

The default `Auto` hardware profile detects CPU threads, system RAM, GPU, and
VRAM using standard-library and operating-system interfaces. It uses
`nvidia-smi` when available for accurate NVIDIA memory and probes PyTorch CUDA
support in a disposable child process. The result selects a Whisper model,
device mode, 10–15 minute chunk size, Ollama context, and the best-quality
already-installed Ollama model within a safe memory budget. Model selection
accounts for Chinese, long context, instruction following, and quantization
instead of using file size alone. Auto mode never downloads a multi-gigabyte
model without consent or changes media properties based on hardware
performance. A 16 GB VRAM + 64 GB RAM machine defaults to the slower quality
profile: Whisper `large-v3`, 10-minute chunks, and a 16K context.

At UI startup, an installed but stopped local Ollama service is launched
automatically without loading a model or consuming model VRAM. Resolve is found
across C:, D:, and other custom install drives, but is launched only when the
strict serial workflow reaches its final Resolve stage. The executor then waits for the
scripting API, so Resolve cannot compete with Whisper or Ollama for VRAM.

`Project FPS` defaults to **Auto from source media**, not a fixed 25. After a
source is selected, a short-lived `ffprobe` process reads `avg_frame_rate` or
`r_frame_rate`, including fractional rates such as 23.976, 29.97, and 59.94.
If no readable source is available, the UI checks `timeline_cuts.json` and then
`raw_data.json`. It reports a clear error before starting only when every source
fails; common timeline rates remain available in the modern custom dropdown.

The top-right menus switch theme (`System`, `Dark`, `Light`) and interface
language (`System`, `中文`, `English`) immediately and remember both choices.
The window uses Per-Monitor DPI Awareness V2, a Windows 11 Mica-backed title
surface, rounded corners, and a work-area-aware 4K layout. It keeps the native
title bar, so Snap Layouts, taskbar previews, Alt+Tab, and accessibility remain
available.

## Resolve edition and Sony A7 IV PP8 media

The final automation stage requires **DaVinci Resolve Studio**. The paid edition
is officially called “Studio,” not “Resolve Pro.” This requirement is not only
about 4K resolution:

- CyberEditor-Agent connects to Resolve from an independent Python/UI process.
  The required External Scripting/API access is a Studio feature. The free
  edition limits scripting to Resolve's internal Console or Scripts menu.
- Blackmagic Design positions the free edition for virtually all 8-bit formats
  up to 60 fps and Ultra HD 3840×2160. Studio adds professional 10-bit formats,
  up to 120 fps, resolutions above 4K, and accelerated H.264/H.265
  encoding/decoding.

If “M4” means the Sony α7 IV (A7M4), PP8 is an **S-Log3 +
S-Gamut3.Cine picture profile**, not a RAW video format. Depending on the camera
recording settings, the files may be XAVC S, XAVC HS, or XAVC S-I and may be
8-bit or 10-bit 4:2:2. Resolve Studio is the practical choice for common PP8 4K
10-bit 4:2:2 footage and is required for this project's external timeline
automation.

Extraction now reads each Sony `CxxxxM01.XML` sidecar first instead of forcing
one PP8 assumption across the batch. S-Log2/S-Gamut, S-Log3/S-Gamut3, and
S-Log3/S-Gamut3.Cine map to distinct per-source Resolve input transforms. The
UI camera profile is only an explicit fallback when a sidecar is missing:

```text
Per-source Sony XML input color space / gamma
        → DaVinci Wide Gamut / Intermediate
        → Rec.709 Gamma 2.4
```

OpenCV estimates neutral pixels, luminance, and RGB balance from keyframes,
then creates bounded per-source matching around the batch median (±1.5 EV and
0.667–1.5 RGB gains). Low-confidence estimates blend toward identity. A failed
Log input transform is fatal rather than producing a flat render. The FFmpeg
review transforms S-Log3; mixed S-Log2 footage is left to Resolve's verified
native RCM instead of receiving an incorrect S-Log3 LUT.

References:
[Blackmagic Design edition comparison](https://www.blackmagicdesign.com/products/davinciresolve),
[Resolve Studio scripting and automation](https://www.blackmagicdesign.com/products/davinciresolve/studio),
[Sony α7 IV PP8 documentation](https://helpguide.sony.net/ilc/2110/v1/en/contents/TP1000649066.html),
and [Sony α7 IV movie formats](https://helpguide.sony.net/ilc/2110/v1/en/contents/TP1000640834.html).

## Why strict serial execution

Many one-click AI editing prototypes keep ASR, LLM, and NLE processes resident
at the same time, quickly exhausting a 16 GB GPU. CyberEditor-Agent treats
process exit as a hard VRAM barrier:

```text
Whisper + OpenCV child process
          │ exits completely; Windows releases the CUDA context
          ▼
Qwen3.6 first music-director pass (frames + dialogue + story)
          │ writes music_brief.json, then keep_alive=0 unload
          ▼
CPU local/network retrieval + Librosa/FFmpeg music analysis
          │ beats, strong beats, sections, key, dynamics, LUFS; zero GPU
          ▼
Qwen3.6 final director (shots + 1–3 cues + silence + climax sync)
          │ writes timeline_cuts.json, then fully unloads
          ▼
FFmpeg CPU render of music_bed.wav
          │ cue changes, fades, loudness matching, dialogue ducking
          ▼
FFmpeg review-render child process
          │ renders transitions, denoise, looks, and basic motion
          ▼
DaVinci Resolve executor child process
          │ native effects / DRX / optional guarded UI macro / final render
```

- The parent orchestrator does not import PyTorch, Whisper, OpenCV, requests,
  or the Resolve API.
- At most one project-owned heavy child process runs at any time.
- The director refuses to load its model if Ollama `/api/ps` reports another
  resident model.
- The director handles one 10–15 minute analysis window per request (12 minutes
  by default). This is not a source-count or total-runtime limit: multiple videos
  and hour-long footage are reviewed as sequential windows and then assembled
  globally.
- `timeline_cuts.json` is written atomically only after every chunk succeeds
  and all decisions are validated and merged.

## Why Qwen3.6-27B dense Q8 is now the default

- `qwen3.6:27b-mtp-q8_0` is a dense, native image-text model of about 30 GB.
  Q8 preserves substantially more weight fidelity and fits mixed 16 GB VRAM +
  64 GB RAM inference.
- Official video benchmarks generally place dense 27B above 35B-A3B on
  VideoMME, VideoMMMU, MLVU, and MVBench. The sparse model is faster; this
  project prioritizes editorial judgment.
- The older text-only Qwen2.5 72B Q5 is about 54 GB. Parameter count alone does
  not guarantee better directing than Qwen3.6's newer multimodal post-training.

The same Qwen3.6 model first reviews images and speech, then performs the
text-only global assembly. Only one model is resident, without loading a second
54 GB checkpoint. A page-file safety margin remains recommended. Right-click
`scripts\configure_pagefile_admin.cmd` and run it as Administrator to keep a
4–8 GB C: page file and add a 32–48 GB D: page file. It never reboots Windows.

## Six-stage two-director finishing pipeline

The implementation now follows this strict serial chain:

1. **Extraction**: each video gets its own Whisper/OpenCV child process, which
   writes dialogue, real JPEG keyframes, and media metadata. The process exits
   before the parent crosses the VRAM-release barrier.
2. **First music-director pass**: Qwen3.6 reads representative frames, dialogue,
   and shoot order, then defines theme, emotional arc, search terms,
   instrumentation, BPM range, vocal policy, one to three cues, and intentional
   silence. It writes `director_treatment.json` and `music_brief.json`, then unloads.
3. **Retrieval and analysis (CPU)**: choose a local licensed library, Jamendo with
   verifiable license URLs, or the explicitly confirmed yt-dlp any-online mode.
   Librosa/FFmpeg extracts BPM, beats, strong beats, approximate downbeats, energy
   sections, whole-track energy trend, peak position, key, dynamic range, and EBU
   R128 LUFS into `music_analysis.json`. Ranking combines BPM, semantic relevance,
   and whether the measured curve can deliver the requested build or climax.
4. **Final direction**: Qwen3.6 reloads, reviews image/speech micro-scenes of no more
   than three minutes, and performs cross-source story and multi-cue assembly against
   analyzed tracks.
   Dynamic shot limits range from 10-second B-roll to complete 45-second interview
   thoughts. Picture selection assigns natural-sound, on-beat, phrase-start, build,
   payoff-hit, or release roles; local validation grounds two to six sync points in
   measured strong beats/downbeats. Normal visual shifts are bounded to ±0.45 seconds
   and payoff hits to ±0.75 seconds; interview dialogue is never truncated for rhythm.
5. **Music-bed conform and Resolve execution**: after Qwen unloads, FFmpeg trims
   one to three cues, applies fades, loudness matching and dialogue ducking, and
   writes deterministic `music_bed.wav`. Resolve imports this one file on A2, then
   native APIs import picture media, use each source's native FPS,
   assemble clips, and apply Voice Isolation, basic CDL, `Stabilize()`,
   `CreateMagicMask()`, and `SmartReframe()`. User-exported DRX grades are applied
   through the node graph's `ApplyGradeFromDRX()`. Resolve 21 exposes stabilization
   and Magic Mask natively, so fragile coordinate macros are not the default.
6. **Final export**: enable **Export final movie in Resolve** in the UI to create
   a Render Job, start it, log percentage progress, and validate completion. The
   current Deliver format/codec is preserved unless an existing Resolve render
   preset is entered.

### Any-online audio and copyright

The UI offers **Any online audio · quality first**, but it requires a new explicit
confirmation for every run; consent is never persisted. This mode uses yt-dlp for
search/download and does not automate cookies, logins, or DRM circumvention. The
project records source URL, the per-run user statement, license fields, and SHA-256
in audit JSON. Availability and a confirmation dialog do not grant copyright. The
user must hold every right required to download, adapt, synchronize, and publish
the audio and must comply with source-platform terms. Commercial releases need an
explicit license covering commercial use, adaptation, and synchronization. Local
licensed libraries or Jamendo's verifiable-license mode remain the safer choices.

### DRX, Fairlight, and the UI fallback

- Export grades from Resolve into `config/drx/` using exactly
  `interview_clean.drx`, `cinematic.drx`, or `low_light_cleanup.drx`. The model
  selects logical names only and cannot construct arbitrary filesystem paths.
- The UI accepts an existing Resolve Fairlight preset name. A missing preset is
  a clear error rather than silently dropping requested audio processing.
- `src/resolve_macro.py` is an **optional**, disabled-by-default PyAutoGUI fallback
  for operations still absent from the API. When configured, it verifies the
  expected display resolution, requires Resolve to be foreground, enables the
  screen-corner fail-safe, and accepts only waits, hotkeys, key presses, and
  normalized clicks. Copy and calibrate
  `config/resolve_macro_profile.example.json` for your own Resolve workspace.

Resolve 21 does not document a universal per-clip volume property. The executor
probes the current build and writes gain when a supported property is exposed;
otherwise it logs a warning and preserves the dB decision in the AI marker. The
FFmpeg review always applies that gain. Use a calibrated fallback or verified
Fairlight workflow when exact per-clip gain is required in the final Resolve job.

The Ollama API supports JSON Schema constrained output and immediate model
unloading with `keep_alive: 0`:
[Generate API](https://docs.ollama.com/api/generate),
[Structured Outputs](https://docs.ollama.com/capabilities/structured-outputs),
and [model unloading](https://docs.ollama.com/faq#how-do-i-keep-a-model-loaded-in-memory-or-make-it-unload-immediately).

## Project structure

```text
CyberEditor-Agent/
├─ gui.py                        # Windows desktop UI entry point
├─ launch_ui.bat                 # Double-click UI launcher
├─ main.py                       # Strict serial workflow orchestrator
├─ scripts/
│  └─ install_windows.ps1        # Windows CPU/CUDA auto-installer
├─ requirements.txt
├─ config/
│  ├─ drx/                      # Constrained user-exported DRX grades
│  └─ resolve_macro_profile.example.json
├─ README.md                     # Simplified Chinese documentation
├─ README_EN.md                  # English documentation
├─ LICENSE
├─ CONTRIBUTING.md
├─ SECURITY.md
├─ src/
│  ├─ __init__.py
│  ├─ gui.py                     # Dependency-free fallback and shared probes
│  ├─ modern_gui.py              # Windows 11, 4K, Chinese/English UI
│  ├─ runtime_services.py        # Cross-drive discovery and service auto-start
│  ├─ media_manifest.py          # Multi-video/folder discovery and proxy mapping
│  ├─ ui_i18n.py                 # GUI-independent bilingual strings
│  ├─ extractor.py               # Whisper + OpenCV extraction
│  ├─ director.py                # Two-pass treatment, review, and multi-cue direction
│  ├─ music_analyzer.py          # Local/Jamendo/yt-dlp retrieval and CPU analysis
│  ├─ music_bed.py               # Cue conform, fades, ducking, and bed render
│  ├─ review_renderer.py         # Watchable FFmpeg preview and effects
│  ├─ resolve_macro.py           # Guarded optional PyAutoGUI fallback
│  └─ resolve_executor.py        # Resolve assembly and effect mapping
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

## Hardware and software

### Minimum recommendations

- Windows 10/11 64-bit
- Python 3.10 or 3.11 64-bit
- 16 GB system RAM; 32–64 GB is recommended for mixed 32B/70B inference
- At least 20 GB of free disk space, plus space for proxies and model weights
- FFmpeg
- Ollama
- DaVinci Resolve Studio with External Scripting set to `Local`

### Model recommendations

| Hardware | Whisper | Ollama | Notes |
|---|---|---|---|
| Integrated GPU / CPU only | `tiny` / `base` | Qwen 3.6 27B Q4 (very slow) | A smaller vision model may be more practical |
| 8–12 GB VRAM | `small` / `turbo` | `qwen3.6:27b-mtp-q4_K_M` | About 18 GB; mixed RAM/GPU |
| 16 GB VRAM | `large-v3` | `qwen3.6:27b-mtp-q4_K_M` | About 18 GB; mostly GPU-offloaded |
| 16 GB VRAM + 64 GB RAM | `large-v3` | `qwen3.6:27b-mtp-q8_0` | About 30 GB; dense Q8 quality default |
| 24 GB+ VRAM | `large-v3` | Qwen 3.6 27B Q8 | Serial execution is still recommended |

On a 16 GB Quadro with 64 GB RAM, prefer `qwen3.6:27b-mtp-q8_0` for quality or
Q4_K_M for speed and space. Both accept text and images and support thinking:
[Qwen 3.6 on Ollama](https://ollama.com/library/qwen3.6).

Whisper's published approximate VRAM requirements range from about 1 GB for
tiny/base to about 6 GB for turbo. See the
[OpenAI Whisper README](https://github.com/openai/whisper#available-models-and-languages).

## Installation

### 1. Install system components

```powershell
winget install --id Gyan.FFmpeg
winget install --id Ollama.Ollama
```

Open a new PowerShell window after installation and verify:

```powershell
ffmpeg -version
ollama --version
```

Install **DaVinci Resolve Studio**, open Preferences, set External Scripting to
`Local`, and restart Resolve. The free edition can run this project's extraction
and AI director stages, but an independent UI/Python process cannot use it to
assemble the final timeline automatically.

### 2. Clone and install automatically (recommended)

```powershell
git clone https://github.com/widmonstertony/CyberEditor-Agent.git
cd CyberEditor-Agent
Set-ExecutionPolicy -Scope Process Bypass
.\scripts\install_windows.ps1
```

The installer creates `.venv`, installs dependencies, checks `nvidia-smi` and
the NVIDIA driver, replaces the generic CPU wheel from the official PyTorch
CUDA index when compatible, and finishes with a real CUDA tensor calculation.
Systems without an NVIDIA GPU safely keep the CPU build. You can also override
the automatic choice:

```powershell
.\scripts\install_windows.ps1 -ComputePlatform cpu
.\scripts\install_windows.ps1 -ComputePlatform cu126
.\scripts\install_windows.ps1 -ComputePlatform cu130
```

For manual installation, create the virtual environment, use the
[official PyTorch installer](https://pytorch.org/get-started/locally/) to
select Windows, Pip, and a CUDA build supported by the installed driver, then
run `pip install -r requirements.txt`. Verify the actual environment with the
command below. Only `True` means CUDA is genuinely active:

```powershell
.\.venv\Scripts\python.exe -c "import torch; print(torch.__version__, torch.version.cuda, torch.cuda.is_available(), torch.cuda.get_device_name(0) if torch.cuda.is_available() else 'CPU')"
```

Whisper also requires the system FFmpeg executable. See
[OpenAI Whisper](https://github.com/openai/whisper#setup) for upstream setup
details.

### 3. Prepare Ollama

Quality-first for 16 GB VRAM + 64 GB RAM (about a 30 GB download and slower
mixed CPU/GPU inference):

```powershell
ollama pull qwen3.6:27b-mtp-q8_0
```

For a faster, smaller high-quality model (about 18 GB):

```powershell
ollama pull qwen3.6:27b-mtp-q4_K_M
```

Models are downloaded once. The UI subsequently starts Ollama automatically
and selects the best installed model, but never downloads a large model without
confirmation.

Limit Ollama to one resident model and one parallel request, then restart
Ollama:

```powershell
setx OLLAMA_MAX_LOADED_MODELS 1
setx OLLAMA_NUM_PARALLEL 1
```

### 4. Proxies (optional)

Originals work without proxies. For long-form or 4K 10-bit material, 1080p
proxies generated in Resolve or FFmpeg are recommended. Proxy and source must
share the same start time and duration. Director decisions use seconds; the
Resolve executor prefers each Media Pool item's native source FPS for frame
conversion instead of assuming every source matches the project FPS.

## One-command workflow

There is no need to start Ollama or Resolve manually. The program starts the
local Ollama service but does not load the model until the director stage.
After that model is unloaded, the executor launches Resolve and waits for its
API. Resolve Studio's External Scripting preference must still be set to
`Local` once beforehand.

```powershell
python main.py `
  --input-folder "D:\Documentary\Camera originals" `
  --ollama-model "qwen3.6:27b-mtp-q8_0" `
  --project-fps 23.976 `
  --chunk-minutes 10
```

You can also repeat `--video` in selection order and combine explicit files
with a folder; duplicate paths are removed:

```powershell
python main.py `
  --video "D:\Shoot\A001.mp4" `
  --video "D:\Shoot\B001.mp4" `
  --input-folder "D:\Shoot\B-roll" `
  --ollama-model "qwen3.6:27b-mtp-q8_0"
```

Default outputs:

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

### Resume from a checkpoint

A long video should not be transcribed again merely because Resolve was not
running:

```powershell
# Reuse raw_data.json and rerun only the director and Resolve
python main.py --skip-extraction `
  --proxy "D:\Documentary\proxy\source_1080p.mp4" `
  --ollama-model "qwen3.6:27b-mtp-q8_0" `
  --creative-brief "Tell the preparation-to-payoff story in shooting order" `
  --target-duration-sec 80 `
  --camera-profile sony_pp8_slog3_sgamut3cine `
  --music-provider local `
  --music-folder "D:\Music\Licensed"

# Any-online candidates (the UI is preferred because it shows the full warning)
python main.py --skip-extraction --skip-resolve `
  --ollama-model "qwen3.6:27b-mtp-q8_0" `
  --music-provider yt_dlp --music-candidate-limit 8 `
  --music-rights-confirmed `
  --music-rights-claim "I hold the rights required to download, adapt, synchronize, and use candidate audio and will follow source-platform terms"

# Reuse timeline_cuts.json and run only Resolve
python main.py --skip-extraction --skip-director `
  --proxy "D:\Documentary\proxy\source_1080p.mp4"

# Generate JSON without starting Resolve
python main.py --video "D:\source.mov" --skip-resolve
```

## Standalone modules

```powershell
python -m src.extractor --help
python -m src.director --help
python -m src.music_analyzer --help
python -m src.music_bed --help
python -m src.review_renderer --help
python -m src.resolve_executor --help
```

The three core classes can also be imported by external Python modules:

```python
from src.extractor import MediaExtractor
from src.director import AIDirector
from src.music_analyzer import LicensedMusicAnalyzer
from src.music_bed import MusicBedRenderer
from src.resolve_executor import DaVinciExecutor
```

## JSON contract

The director produces this core structure:

```json
{
  "schema_version": "3.0",
  "project_fps": 59.94,
  "director_treatment": {
    "title": "Preparation to synchronization",
    "central_theme": "Messy preparation resolves into precise teamwork",
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
    "strategy": "Restrained opening, rising development, silent ending breath",
    "silence_regions": [
      {"timeline_in_sec": 36.0, "timeline_out_sec": 39.0, "reason": "Protect key dialogue"}
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
        "license": "user-supplied rights record"
      }
    ]
  },
  "clips": [
    {
      "clip_id": 1,
      "file_name": "D:\\Documentary\\proxy\\source_1080p.mp4",
      "cut_in_sec": 12.5,
      "cut_out_sec": 18.2,
      "reason_for_cut": "Complete statement of the main idea",
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

`cut_in_sec` is inclusive and `cut_out_sec` is exclusive. Using each source
clip's native FPS, the Resolve executor rounds the in-point down and the
out-point up, then subtracts one frame to match Resolve's inclusive `endFrame`.

## Robustness boundaries

- The vision model does not decode every 4K pixel of every frame. Extraction
  traverses every video and retains time-distributed, scene-change-aware
  representative frames for windowed review. This balances long-form coverage
  with local context and memory limits.
- Resolve's public scripting API has no stable general transition-insertion
  method. The FFmpeg preview truly renders the AI transition, denoise, look,
  and motion plan. The editable Resolve timeline applies supported Voice
  Isolation, CDL, and transform properties and stores the full plan in markers.
- A normal Rec.709 plan may reuse an empty current project. When a PP8
  transform is required and the current project already contains a timeline,
  the script creates an isolated `Director Cut` project so existing global
  color settings are not changed.
- If one Resolve append operation fails, the API offers no reliable
  transaction rollback. The log reports how many clips were appended so the
  user can inspect and undo the partial timeline.
- Semantic review requires an Ollama model reporting the `vision` capability.
  Plain `qwen2.5:3b` is a text-only smoke-test model and cannot run this
  multimodal path; prefer `qwen3.6:27b-mtp-q8_0` when quality matters most.

## Tests

Tests require no GPU, Ollama, or Resolve:

```powershell
python -m unittest discover -s tests -v
python -m compileall -q gui.py main.py src tests
```

## Privacy

Media, transcripts, keyframes, and edit-decision JSON are written only to the
local `data/` directory by default. Its contents are excluded by `.gitignore`.
Never attach real client media, model weights, Resolve projects, or logs to a
public issue. See [SECURITY.md](SECURITY.md).

## Contributing

Contributions improving compatibility across GPUs, Ollama models, and Resolve
versions are welcome. Any new AI module must demonstrate that it cannot overlap
another heavy stage. See [CONTRIBUTING.md](CONTRIBUTING.md).

## License

[MIT](LICENSE)
