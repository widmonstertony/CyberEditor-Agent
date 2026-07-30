# CyberEditor-Agent

[简体中文](README.md) | [English](README_EN.md)

A fully local, privacy-first automatic long-form video editing MVP for Windows.

CyberEditor-Agent uses Whisper to extract timestamped dialogue, OpenCV to create
lightweight scene markers, a local Ollama model to make chunked editorial
decisions, and the DaVinci Resolve Python API to assemble 1080p proxy media on a
timeline.

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

The bilingual desktop UI includes source/proxy selection, Whisper and Ollama
settings, resumable workflow modes, Resolve controls, live logs, stage
progress, safe cancellation, environment checks, and output shortcuts.

The UI uses only the standard-library Tkinter toolkit and launches the existing
`main.py` orchestrator as a child process. It never imports ML dependencies, so
the strict serial execution and VRAM-release guarantees remain intact. The UI
enables Windows Per-Monitor DPI Awareness V2 and scales itself dynamically for
4K and mixed-DPI displays.

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
Chunked Ollama director child process
          │ keep_alive=0 plus a second parent-process unload request
          ▼
DaVinci Resolve executor child process
```

- The parent orchestrator does not import PyTorch, Whisper, OpenCV, requests,
  or the Resolve API.
- At most one project-owned heavy child process runs at any time.
- The director refuses to load its model if Ollama `/api/ps` reports another
  resident model.
- The director handles only 10–15 minutes of data per request; the default is
  12 minutes.
- `timeline_cuts.json` is written atomically only after every chunk succeeds
  and all decisions are validated and merged.

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
├─ requirements.txt
├─ README.md                     # Simplified Chinese documentation
├─ README_EN.md                  # English documentation
├─ LICENSE
├─ CONTRIBUTING.md
├─ SECURITY.md
├─ src/
│  ├─ __init__.py
│  ├─ gui.py                     # High-DPI desktop workflow controller
│  ├─ extractor.py               # Whisper + OpenCV extraction
│  ├─ director.py                # Chunked Ollama director
│  └─ resolve_executor.py        # DaVinci Resolve assembly
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
| Integrated GPU / CPU only | `tiny` / `base` | 7B–14B Q4 | Slow, but the workflow remains usable |
| 8–12 GB VRAM | `small` | 14B/32B Q4 with mixed offload | Lower `--num-ctx` to save memory |
| 16 GB VRAM | `small` / `turbo` | 32B Q4 or mixed CPU/GPU 70B | The primary target for strict serialization |
| 24 GB+ VRAM | `turbo` / `large-v3` | Quantized 32B/70B | Serial execution is still recommended |

A 70B model cannot fit entirely in 16 GB of VRAM. “Compatible” means that
Ollama/llama.cpp offloads some layers to system RAM and the CPU. Real-world
speed depends on quantization, memory bandwidth, and context length.

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

### 2. Clone and create a virtual environment

```powershell
git clone https://github.com/widmonstertony/CyberEditor-Agent.git
cd CyberEditor-Agent
py -3.11 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
```

NVIDIA users should first use the
[official PyTorch installer](https://pytorch.org/get-started/locally/) to
install the CUDA build matching their driver, then install the project
dependencies:

```powershell
pip install -r requirements.txt
```

Whisper also requires the system FFmpeg executable. See
[OpenAI Whisper](https://github.com/openai/whisper#setup) for upstream setup
details.

### 3. Prepare Ollama

The following is only an example; use any compatible model already installed:

```powershell
ollama pull qwen2.5:32b
```

Limit Ollama to one resident model and one parallel request, then restart
Ollama:

```powershell
setx OLLAMA_MAX_LOADED_MODELS 1
setx OLLAMA_NUM_PARALLEL 1
```

### 4. Prepare proxies

Generate 1080p proxies in Resolve or with FFmpeg. The proxy and source must
share the same start time and duration. Director decisions use seconds, while
the executor converts them to frames using the active Resolve project FPS.

## One-command workflow

Start Ollama first. Before the Resolve stage begins, start Resolve Studio and
open the target project:

```powershell
python main.py `
  --video "D:\Documentary\source.mov" `
  --proxy "D:\Documentary\proxy\source_1080p.mp4" `
  --ollama-model "qwen2.5:32b" `
  --project-fps 25 `
  --chunk-minutes 12
```

Default outputs:

```text
data/raw_data.json
data/transcript.srt
data/keyframes/*.jpg
data/timeline_cuts.json
data/cybereditor.log
```

### Resume from a checkpoint

A long video should not be transcribed again merely because Resolve was not
running:

```powershell
# Reuse raw_data.json and rerun only the director and Resolve
python main.py --skip-extraction `
  --proxy "D:\Documentary\proxy\source_1080p.mp4" `
  --ollama-model "qwen2.5:32b"

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
python -m src.resolve_executor --help
```

The three core classes can also be imported by external Python modules:

```python
from src.extractor import MediaExtractor
from src.director import AIDirector
from src.resolve_executor import DaVinciExecutor
```

## JSON contract

The director produces this core structure:

```json
{
  "project_fps": 25.0,
  "clips": [
    {
      "clip_id": 1,
      "file_name": "D:\\Documentary\\proxy\\source_1080p.mp4",
      "cut_in_sec": 12.5,
      "cut_out_sec": 18.2,
      "reason_for_cut": "Complete statement of the main idea",
      "confidence": 0.9
    }
  ]
}
```

`cut_in_sec` is inclusive and `cut_out_sec` is exclusive. The Resolve executor
rounds the in-point down and the out-point up, then subtracts one frame to match
Resolve's inclusive `endFrame`.

## Robustness boundaries

- Proxy FPS should match project FPS. The MVP converts JSON seconds using the
  Resolve project FPS; mixed native frame rates should be normalized through
  proxies first.
- The script reuses the current Resolve timeline. It creates
  `CyberEditor Timeline` only when there is no current timeline.
- If one Resolve append operation fails, the API offers no reliable
  transaction rollback. The log reports how many clips were appended so the
  user can inspect and undo the partial timeline.
- Scene markers use lightweight grayscale frame differences, not semantic
  visual understanding. A visual model can replace this implementation later,
  but it must remain inside the extractor child process and preserve the exit
  barrier.

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
