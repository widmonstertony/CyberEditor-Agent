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

## Local Web Studio

After installation, double-click `launch_web.bat` or run:

```powershell
.\.venv\Scripts\python.exe web.py
```

The browser opens `http://127.0.0.1:8765/`. Web Studio shares the desktop
application's `WorkflowOptions` and strict-serial `main.py` orchestrator. It
supports multi-video/folder selection, a creative brief, automatic hardware
tuning, Ollama/Resolve health, live logs, stage progress, safe cancellation,
and in-browser playback of review and final renders. The resident server uses
only the Python standard library and never imports Torch, Whisper, OpenCV, or
Resolve, so it retains no model VRAM.

[tonytan.me/cybereditor/](https://tonytan.me/cybereditor/) is the primary browser UI. The user starts Ollama and `python3 web.py --no-browser` (or double-clicks `launch_companion.bat` on Windows), returns to the website, and connects. Native media selection, environment detection, Ollama models, workflow start/stop, logs, and outputs remain browser-operated and come from this repository's real `WorkflowManager`. `http://127.0.0.1:8765/` is only the same-origin fallback for browser local-network permission issues. EC2 receives neither RAW media, model traffic, nor execution commands.

Browsers cannot read arbitrary local absolute paths. On a local Windows host,
CyberEditor opens a native server-side multi-file/folder picker, avoiding a
second upload or copy of hundreds of gigabytes of 4K media. In this single-PC
mode, Resolve, Ollama, FFmpeg, and the media live on the Windows host running
`web.py`.

The default bind is loopback-only. LAN, workstation, or reverse-proxy
deployments require a token of at least 16 characters:

```powershell
.\.venv\Scripts\python.exe web.py --host 0.0.0.0 --port 8765 --token "replace-with-a-long-random-token" --no-browser
```

Enter the same token through the top-right **Access token** control on the first
remote visit. Prefer a trusted LAN, VPN, or authenticated HTTPS reverse proxy;
never expose Resolve scripting or this service directly to the public Internet.

## Cloud Web UI + Local Windows Execution

The deployable mode is a cloud control plane paired with an outbound Windows
worker. It does **not** require Ollama or Resolve on the deployment host:

```text
Any browser ─HTTPS─> Cloud Control Plane <─outbound HTTPS polling─ Windows Worker
                                                                  ├─ local RAW/proxies
                                                                  ├─ local Ollama/CUDA/FFmpeg
                                                                  └─ local Resolve Studio
```

- The cloud stores jobs, progress, bounded logs, and an optional low-bitrate
  720p preview. RAW media, keyframes, transcripts, and models stay local.
- The worker initiates every connection, so there is no home port forwarding,
  public IP requirement, or exposed Resolve scripting port.
- **Choose videos/folder** opens the native picker on the selected Windows PC.
- CUDA, Ollama, and Resolve cards report the worker PC, not the cloud host.
- The control plane needs a persistent SQLite volume. It fits a Docker VPS or a
  persistent Render/Railway-style service, not a static site or ephemeral
  serverless function.

The default public entry, [tonytan.me/cybereditor/](https://tonytan.me/cybereditor/), is the real Local App mode and connects directly to the loopback companion on this computer. Use `?remote=1` for the token-protected cloud control plane when controlling an outbound Windows worker from another device. The old browser fake-data demo has been removed.

The control plane in this repository owns page navigation below
`/cybereditor/`. Unknown browser documents receive CyberEditor's bilingual HTML
404, while unknown API, worker, and static-asset requests retain the native JSON
404 contract. Caddy does not duplicate or guess the application route table.

That instance is managed by the Caddy, systemd, restricted-sudo, and atomic-release definitions in `Personal-Website/ops`. A merge to protected `main` lets its isolated GitHub runner deploy only the reviewed control-plane package. The host caps previews at 512 MiB and prunes artifacts after seven days; RAW media, transcripts, evidence frames, Ollama, and Resolve stay on the workstation.

Set two different random secrets on the deployment host and launch the control
plane:

```bash
export CYBEREDITOR_ADMIN_TOKEN="replace-with-random-admin-token"
export CYBEREDITOR_WORKER_TOKEN="replace-with-a-different-long-worker-token"
docker compose -f docker-compose.control-plane.yml up -d --build
```

The container listens on HTTP internally; terminate public HTTPS through the
hosting platform, Caddy, or Nginx. On the Windows PC containing the media and
local applications, run:

The control-plane container uses a digest-pinned Python 3.14 slim image. The
Windows editing workstation stays on Python 3.12, where CI performs a real
install and API smoke test for PyTorch, Whisper, OpenCV, and Librosa. Separate
checks keep cloud runtime upgrades from silently breaking the CUDA workflow.

```powershell
$env:CYBEREDITOR_WORKER_TOKEN = "replace-with-a-different-long-worker-token"
.\.venv\Scripts\python.exe worker.py --server "https://edit.example.com"
```

You can instead set the environment variable and double-click
`launch_worker.bat https://edit.example.com`. Open the deployed site, enter
`CYBEREDITOR_ADMIN_TOKEN` through **Access token**, and select the online PC.
The admin secret is never sent to the worker and the worker secret never enters
the browser. Add `--no-preview-upload` if no rendered copy may be stored in the
cloud; remote control and logs still work, while output playback remains local.

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
transcribed and sampled continuously as a time-ordered JPEG every 0.5 seconds (2 fps),
allowing the vision model to identify an action's entry, apex, reaction, and
result rather than merely naming objects. The vision model first reviews all
evidence without a predetermined theme and builds a neutral action/continuity
ledger. It then compares several evidence-backed story concepts and writes the
treatment, story beats, runtime, executable color bible, and music mood.
“Complete coverage” does not mean one enormous model request. Ollama cannot
accept thousands of images from an hour at once, so the reviewer first tries
chronological 10-second core windows with up to one second of overlap. Before every
request it budgets text, JSON Schema, output headroom, and estimated image tokens;
the core shrinks when the configured context cannot hold it, and one request never
exceeds 32 images. A rolling identity, location, action, emotion, and
unresolved-intention state crosses every boundary without silently dropping samples.
Each window records action entry, apex, and exit. After the neutral ledger is
complete, every short event atom up to eight seconds is extracted again as
consecutive frames at up to 4 fps to refine readable entry, action apex, and clean
exit. Dialogue retains Whisper timing; the silent vision model cannot trim words.
A second global-director pass keeps only the
minority of shots that serve that treatment. Local validation enforces actual
source order and in-file timecode, plus per-shot and total-runtime limits. To keep
the FFmpeg review, program audio, and Resolve output frame-aligned, the current
automated deliverable executes hard cuts in every engine; requested dissolves or
fades remain audit hints rather than running in only one renderer. It plans audio cleanup, creative looks, gentle push-ins,
gain, stabilization, tracking, and a one-to-three-cue pre-mixed score chosen by
the two-director pipeline. The result can include an editable
Resolve timeline, an immediately watchable FFmpeg 1080p review, and a final
movie rendered by Resolve's Deliver pipeline.

### Score-informed picture rhythm, then exact post-lock cueing and graphics

Music and picture use a two-level coupling, not “ignore music until picture lock.”
Before selecting shots, the text director reads measured candidate `score_profiles`
(BPM, sections, energy curve, and whether a real climax exists) and designs shot
length, escalation, natural-sound gaps, and `music_edit_role` around feasible music.
Evidence review, supervising review, and the blind viewer then produce a validated
**picture lock**. Only then does a separate schema-constrained request place zero to
three exact cues against final timeline boundaries, dialogue ranges, strong beats,
and downbeats. This lets picture rhythm serve real music from the start without
leaving payoff hits attached to boundaries that later disappeared. Every selected
shot must state its narrative function, the new information it gives the viewer,
its exact source trim, audio intent, and music-edit role. Dialogue
about microphones, filters, blocking, or repeated takes receives an advisory
`production_context_hint`, but is never automatically cut or muted. The director
alone decides whether to exclude it, preserve it, use it as natural texture, mix
it with music, or mute it; the executor faithfully applies `audio_intent`.
An evidence-first event contract, a supervising-editor pass, and a context-isolated
blind viewer now audit the picture before lock. The blind viewer never receives the
treatment, intended takeaway, shot-role labels, or editor rationale, so it cannot use
the director's explanation to excuse an unintelligible cut. These reviews
separates observed action from an inferred next event: lining up is not a
departure, and a countdown or forward lean is not proof that riders left. If a
requested event was never filmed, it is recorded in
`absent_or_unproven_events` and the director must adapt the thesis to an honest
story the footage can prove. Every shot supplies an `evidence_claim` and a
`connection_to_previous`. After this review, Python validates bounds and format
only; it no longer pads beats, semantically removes shots, reorders the edit, or
score-trims the director's picture lock.

The supervising model also receives a quantitative draft report: dialogue share,
average shot length, static-shot share, repeated narrative/music roles, and graphic
count. It must name concrete problems and actual changes instead of rubber-stamping
the first assembly. For a sub-two-minute film that is not explicitly dialogue-led,
only dialogue that changes viewer understanding or reveals character should normally
survive; one concise exchange is usually enough for routine technical logistics, and
zero to two graphics is the default. When visual variety is weak, the director should
make a shorter film instead of padding it with production chatter, repeated actions,
or packaging titles. These remain professional review signals for the supervising
editor. Python still does not choose replacement shots, but it rejects a plan whose
declared form, evidence, audible speech, and blind-viewer result contradict one another.

The review does not trust the model's prose self-assessment. The program measures
the sequence it actually returned. A plan explicitly authored as a visual/music
montage is sent back to the same AI director when preserved speech still exceeds
55%; any non-interview, non-evidence-supported dialogue structure is rejected above
72% audible speech; production chatter must match a proven BTS contract; more than
75% of selected shots remain static despite available movement,
four consecutive shots repeat one narrative/music function, no
escalation/contrast/payoff exists, or shot scale is severely monotonous. The 55%
cap is not mechanically applied to interviews, observational documentaries, or
behind-the-scenes stories genuinely driven by conversation. A
`teaser_then_chronological` treatment may also echo its climax in the opening tease.

Python never chooses, deletes, or mutes replacement shots. It retains the best
complete AI-authored proposal and requests a second recut only when the preceding
one made measurable progress. JSON-stage advisories may still be recorded, but the
actual rendered rough cut must subsequently pass the isolated viewer test. After two
failed rendered-film feedback recuts, the workflow stops before Resolve instead of
publishing another known-incoherent final movie. Candidate ledgers
now expose exact merged `speech_ranges` and usable `silent_ranges`; a silent trim
inside a dialogue-bearing source is no longer miscounted as full-length speech.
All actually audible speech still counts: `natural_texture` preserves voices and
cannot be used to disguise retained dialogue.

Structural Whisper silence hallucinations are filtered during new extraction and
when loading an existing `raw_data.json`. A three-character phrase incorrectly
stretched across many seconds no longer classifies the whole range as dialogue or
ducks the music bed for that false duration.
Mixed VRAM/RAM inference with 27B Q8 or 70B can take more than 30 minutes for a
single global assembly request. The orchestrator therefore defaults to a
7,200-second (two-hour) Ollama read timeout while emitting honest elapsed-time
heartbeats every 15 seconds. CLI users may override it with
`--ollama-timeout`.
After lock, the cue pass is responsible for exact track sections, in/out points,
and sync points; the earlier picture pass has already used measured score profiles.
The CPU renderer then performs sample-accurate ducking and refuses to publish a
silent or nearly silent music bed. Optional title cards, chapters, lower thirds,
and end cards from `graphics_plan` are drawn in the FFmpeg review. Resolve receives
each item as a short, frame-counted ProRes 4444 alpha clip at the exact V2 record
frame, with dimensions and visible alpha verified before import. This reproducible
path is **not editable Text+**; edit `graphics_plan` and rerun to change the words.
Typography may clarify or deliberately stylize a coherent edit, but cannot disguise
weak shot selection.

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
profile: Whisper `large-v3`, 10-minute chunks, and a 32K context. Every editable
candidate is considered by the text director: the complete compact ledger is sent
in one request when it fits, while longer projects use chronological director-review
pages followed by global assembly. Python no longer discards candidates with a fixed
Top-21/28 shortlist, and `timeline_cuts.json` records the ids reviewed on each page.

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
Qwen3.8 neutral whole-footage review (2-fps coverage + up-to-4-fps short-atom rewatch + dialogue)
          │ writes footage ledger, treatment, and music brief; then unloads
          ▼
CPU local/network retrieval + Librosa/FFmpeg music analysis
          │ beats, strong beats, sections, key, dynamics, LUFS; zero GPU
          ▼
Qwen3.8 final director (score-informed picture rhythm + exact post-lock cues)
          │ writes timeline_cuts.json, then fully unloads
          ▼
FFmpeg CPU render of music_bed.wav
          │ cue changes, fades, loudness matching, dialogue ducking
          ▼
FFmpeg review-render child process
          │ renders frame-matched hard cuts, denoise, looks, and basic motion
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

## Why vision review and text direction use separate Qwen3.8 roles

- On a 16 GB GPU, dense 2-fps visual review prefers the installed
  `hf.co/ggml-org/Qwen3.8-27B-GGUF:Q4_K_M` model. Its roughly
  19 GB Q4_K_M weights reduce RAM/PCIe churn
  while thousands of images are inspected.
- After the vision model is fully unloaded, the global text director prefers
  `hf.co/ggml-org/Qwen3.8-27B-GGUF:Q8_0`. Its roughly 28.6 GB Q8 weights retain
  more fidelity for final story comparison and structure decisions.
- Official video benchmarks generally place dense 27B above 35B-A3B on
  VideoMME, VideoMMMU, MLVU, and MVBench. The sparse model is faster; this
  project prioritizes editorial judgment.
- The older text-only Qwen2.5 72B Q5 is about 54 GB. Parameter count alone does
  not guarantee better directing than Qwen3.8's newer native vision and instruction
  post-training.

Automatic configuration chooses both roles only from models that Ollama actually
reports as installed. If only one compatible model exists, the two serial stages
reuse it. Qwen3.8-27B was released on 2026-08-14. This project runs the ggml-org
GGUF by direct Hugging Face reference because Ollama's public library does not yet
provide a first-party short tag that the project can depend on. See the
[official Qwen3.8-27B model card](https://huggingface.co/Qwen/Qwen3.8-27B) and
[ggml-org GGUF repository](https://huggingface.co/ggml-org/Qwen3.8-27B-GGUF).

A page-file safety margin remains recommended. Right-click
`scripts\configure_pagefile_admin.cmd` and run it as Administrator to keep a
4–8 GB C: page file and add a 32–48 GB D: page file. It never reboots Windows.

### Current vision boundary: complete ordered sampling, not pretend-native video

The present Windows/Ollama backend calls Ollama's `images` array with chronological
JPEGs; it does **not** hand an MP4 to the model as a continuous video stream. Here,
“full review” means gap-free 2-fps survey coverage from start to finish, all dialogue
preserved, context-adaptive request windows, and up-to-4-fps reinspection of short
actions. It does not mean every original 59.94-fps frame, inter-frame optical flow,
or the source audio track is available as native model video input. Ollama's current
public [Vision API](https://docs.ollama.com/capabilities/vision) documents image input.

vLLM documents OpenAI-compatible `video_url` input and selectable video decoders, so
it is a credible future optional native-video backend. This repository has not yet
implemented that adapter and does not claim it as current functionality. Official
vLLM GPU installation is Linux-first; Windows requires WSL or a separate Linux model
node. See [vLLM multimodal video input](https://docs.vllm.ai/en/latest/features/multimodal_inputs/)
and [vLLM GPU requirements](https://docs.vllm.ai/en/latest/getting_started/installation/gpu/).

## Eight-stage evidence-first finishing pipeline

The implementation now follows this strict serial chain:

1. **Extraction**: each video gets its own Whisper/OpenCV child process, which
   writes dialogue, real JPEG keyframes, and media metadata. The process exits
   before the parent crosses the VRAM-release barrier.
2. **Neutral full review and first music pass**: Qwen3.8 first reads all 2-fps
   evidence, dialogue, and shoot order without a predetermined theme, then
   rewatches every event atom up to eight seconds at up to 4 fps and writes
   `footage_ledger.json`. It compares multiple story concepts that cite real
   timestamps, then writes `director_treatment.json` and `music_brief.json` with
   the emotional arc, search terms, instrumentation, BPM range, one to three cues,
   and intentional silence. The model then unloads completely.
3. **Retrieval and analysis (CPU)**: choose a local licensed library, Jamendo with
   verifiable license URLs, or the explicitly confirmed yt-dlp any-online mode.
   Librosa/FFmpeg extracts BPM, beats, strong beats, approximate downbeats, energy
   sections, whole-track energy trend, peak position, key, dynamic range, and EBU
   R128 LUFS into `music_analysis.json`. Ranking combines BPM, semantic relevance,
   and whether the measured curve can deliver the requested build or climax.
4. **Evidence contract and global assembly**: the text director loads after the
   visual reviewer has exited and reads the complete, fingerprint-validated evidence
   ledger plus vocal-filtered candidates and their measured BPM, sections, and energy
   profiles from `music_analysis.json`. Legacy v2 candidate caches are never reused. Before shot selection, an
   evidence role must cite real `candidate_id` values for the subject,
   goal, state changes, and final observed state. Fewer than three audited state changes
   cannot support a causal/BTS claim; the system must choose an honest character vignette
   or mood montage instead.
   Dynamic shot limits range from 10-second B-roll to complete 45-second interview
   thoughts. Picture is not designed in musical ignorance: the director first uses
   measured `score_profiles` to shape shot length, escalation, and payoff. Picture
   selection assigns natural-sound, on-beat, phrase-start, build, payoff-hit, or
   release roles; after picture lock, a second constrained request grounds two to six sync points in
   measured strong beats/downbeats. Normal visual shifts are bounded to ±0.45 seconds
   and payoff hits to ±0.75 seconds; interview dialogue is never truncated for rhythm.
5. **Picture assembly and blind-viewer gate**: after picture assembly and supervising
   review, a context-isolated prompt sees only literal shot observations and audible
   dialogue. It does not see the treatment, role labels, intended takeaway, or editor
   rationale. It must independently explain subject, goal, progression, changed ending
   state, and meaning; coherence and the relevant causal/visual payoff score must reach 7/10.
6. **Music-bed conform and rough-cut render**: after Qwen unloads, FFmpeg trims
   one to three cues, applies fades, loudness matching and dialogue ducking, and
   writes deterministic `music_bed.wav`, then renders a low-resolution review movie.
7. **Rendered-film review and automatic recut**: the vision model watches the real
   rough cut in chronological adaptive-frame batches, then combines literal visual
   observations with audible-dialogue and music-placement maps. A failed review is
   returned to the director for up to two automatic recuts. Continued failure blocks
   Resolve and leaves `review/rough_cut_review.json` for diagnosis.
8. **Resolve execution and export**: Resolve imports the final music bed on A2, then
   native APIs import picture media, use each source's native FPS,
   assemble clips, and apply Voice Isolation, basic CDL, `Stabilize()`,
   `CreateMagicMask()`, and `SmartReframe()`. User-exported DRX grades are applied
   through the node graph's `ApplyGradeFromDRX()`. Resolve 21 exposes stabilization
   and Magic Mask natively, so fragile coordinate macros are not the default.
   Graphics avoid the undocumented, unstable Text+ insertion path: each item is a
   short ProRes 4444 alpha clip whose frame count, dimensions, and alpha are verified
   before exact placement on V2.
   Enable **Export final movie in Resolve** in the UI to create
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
├─ launch_web.bat                # Double-click local Web Studio launcher
├─ launch_companion.bat / .command # Loopback companion for the hosted UI
├─ web.py                        # Browser controller entry point
├─ control_plane.py              # Deployable cloud control-plane entry point
├─ worker.py                     # Outbound Windows worker entry point
├─ launch_worker.bat             # Double-click worker launcher
├─ Dockerfile
├─ docker-compose.control-plane.yml
├─ web/                          # Dependency-free HTML/CSS/JavaScript client
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
│  ├─ control_plane.py           # SQLite queue, authentication, preview relay
│  ├─ remote_worker.py           # Local probes, command execution, status relay
│  ├─ runtime_services.py        # Cross-drive discovery and service auto-start
│  ├─ media_manifest.py          # Multi-video/folder discovery and proxy mapping
│  ├─ ui_i18n.py                 # GUI-independent bilingual strings
│  ├─ extractor.py               # Whisper + OpenCV extraction
│  ├─ director.py                # Two-pass treatment, review, and multi-cue direction
│  ├─ music_analyzer.py          # Local/Jamendo/yt-dlp retrieval and CPU analysis
│  ├─ music_bed.py               # Cue conform, fades, ducking, and bed render
│  ├─ review_renderer.py         # Watchable FFmpeg preview and effects
│  ├─ rough_cut_reviewer.py      # Rendered-film visual blind review and recut feedback
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
- Python 3.11 or 3.12 64-bit
- 16 GB system RAM is suitable only for development, tests, or smaller models;
  the default 27B Q4/Q8 quality path recommends 64 GB, and mixed 70B inference
  recommends at least 128 GB
- At least 20 GB of free disk space, plus space for proxies and model weights
- FFmpeg
- Ollama
- DaVinci Resolve Studio with External Scripting set to `Local`

### Model recommendations

| Hardware | Whisper | Vision reviewer | Global text director | Positioning |
|---|---|---|---|---|
| Current Quadro RTX 5000 Max-Q 16 GB + 64 GB RAM | `large-v3` | Qwen3.8-27B Q4_K_M | Qwen3.8-27B Q8_0 | Runs the serial path, but both use mixed RAM/VRAM and long projects are slow; the current backend is not native continuous-MP4 input |
| One RTX 5090 32 GB + 128/192 GB RAM | `large-v3` | Qwen3.8-27B Q8 | Qwen3.8-27B Q8; benchmark a larger text model optionally | Best consumer Windows choice; 32 GB must still hold vision/KV/context overhead, so the 28.6 GB weight file is not guaranteed to remain entirely in VRAM |
| RTX PRO 6000 Blackwell 96 GB + 256 GB RAM | `large-v3` | high-precision Qwen3.8-27B | enable a larger text director only after project-specific evaluation | Most practical single-GPU Windows workstation ceiling; much more capacity at far higher cost and 600 W power/cooling requirements |
| Mac Studio M3 Ultra with 512 GB unified memory | separate extraction or model node | can hold much larger MLX multimodal weights | can hold much larger local text weights | Capacity-first LAN model node; current registry, worker, and Resolve automation are Windows paths, so this is not a drop-in replacement without a backend migration |

The current 16 GB Quadro + 64 GB RAM system does not need immediate replacement
to validate this redesign. Its default is Qwen3.8-27B Q4_K_M vision review followed
serially by Qwen3.8-27B Q8_0 final text direction; the tradeoff is extensive
mixed-memory inference and waiting. For a new single-machine Windows build, one
RTX 5090 32 GB with 128 GB RAM (192 GB preferred) is the consumer recommendation.
When cost is secondary, RTX PRO 6000 Blackwell 96 GB plus 256 GB RAM is the more
appropriate single-GPU Windows model/Resolve workstation. Two RTX 5090 cards have
no NVLink: 64 GB aggregate VRAM is not one 64 GB pool, and performance depends on
PCIe plus the inference backend's partitioning strategy.

Faster or larger hardware expands model choice, usable context, and throughput; it
**cannot repair a flawed narrative process, an event that was never filmed, a bad
music candidate set, or missing human-reference evaluation**. In this redesign,
the neutral evidence ledger, measured-score-informed picture rhythm, post-lock cueing,
isolated blind-viewer review, and human-reference benchmark are more directly tied
to human-like results than merely swapping 27B for a larger checkpoint. No hardware
tier promises professional-human-editor quality automatically.

Model details: [official Qwen3.8-27B weights](https://huggingface.co/Qwen/Qwen3.8-27B)
and [ggml-org Qwen3.8-27B GGUF](https://huggingface.co/ggml-org/Qwen3.8-27B-GGUF). Hardware specifications:
[GeForce RTX 5090](https://www.nvidia.com/en-us/geforce/graphics-cards/50-series/rtx-5090/),
[RTX PRO 6000 Blackwell](https://www.nvidia.com/en-us/products/workstations/professional-desktop-gpus/rtx-pro-6000/),
and [Mac Studio](https://www.apple.com/mac-studio/specs/).

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

Quality-first with separate visual/text roles on 16 GB VRAM + 64 GB RAM
(about 47 GB total download):

```powershell
ollama pull hf.co/ggml-org/Qwen3.8-27B-GGUF:Q4_K_M
ollama pull hf.co/ggml-org/Qwen3.8-27B-GGUF:Q8_0
```

To retain only one smaller model, both serial stages can reuse Q4 (about 17 GB,
with lower final text-director fidelity than Q8):

```powershell
ollama pull hf.co/ggml-org/Qwen3.8-27B-GGUF:Q4_K_M
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
  --ollama-model "hf.co/ggml-org/Qwen3.8-27B-GGUF:Q4_K_M" `
  --director-model "hf.co/ggml-org/Qwen3.8-27B-GGUF:Q8_0" `
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
  --ollama-model "hf.co/ggml-org/Qwen3.8-27B-GGUF:Q4_K_M" `
  --director-model "hf.co/ggml-org/Qwen3.8-27B-GGUF:Q8_0"
```

Default outputs:

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

### Resume from a checkpoint

A long video should not be transcribed again merely because Resolve was not
running:

```powershell
# Reuse raw_data.json and rerun only the director and Resolve
python main.py --skip-extraction `
  --proxy "D:\Documentary\proxy\source_1080p.mp4" `
  --ollama-model "hf.co/ggml-org/Qwen3.8-27B-GGUF:Q4_K_M" `
  --director-model "hf.co/ggml-org/Qwen3.8-27B-GGUF:Q8_0" `
  --creative-brief "Tell the preparation-to-payoff story in shooting order" `
  --target-duration-sec 80 `
  --camera-profile sony_pp8_slog3_sgamut3cine `
  --music-provider local `
  --music-folder "D:\Music\Licensed"

# Any-online candidates (the UI is preferred because it shows the full warning)
python main.py --skip-extraction --skip-resolve `
  --ollama-model "hf.co/ggml-org/Qwen3.8-27B-GGUF:Q4_K_M" `
  --director-model "hf.co/ggml-org/Qwen3.8-27B-GGUF:Q8_0" `
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
      "transition_to_next": "cut",
      "transition_duration_sec": 0.0,
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

- “Full review” means every saved 2-fps visual sample plus all dialogue from
  beginning to end, not every original frame of a 59.94 fps stream. A one-hour
  source therefore sends about 7,200 images. “Complete” means no temporal holes,
  not a single request: the initial 10-second cores shrink against the real
  text/Schema/image-token budget, each request is capped at 32 images, and a rolling
  continuity ledger covers the film. Every candidate action up to eight seconds is
  then rewatched at up to 4 fps while long dialogue retains Whisper boundaries.
  This is an Ollama image-sequence path, not native continuous MP4 understanding.
- Resolve's public scripting API has no stable general transition-insertion
  method. To prevent overlap-duration drift between the FFmpeg review, program
  audio, and Resolve output, the current execution path uses matched hard cuts;
  requested dissolves/fades remain audit hints. FFmpeg still renders denoise,
  look, and motion plans, while the editable Resolve timeline applies supported
  Voice Isolation, CDL, and transform properties and stores the plan in markers.
- Resolve's public scripting contract also does not reliably create and write
  Text+ titles. The current implementation renders each graphic as an independent,
  short, alpha-verified ProRes 4444 clip on V2. The result is reproducible but the
  title is not native editable Text+.
- A normal Rec.709 plan may reuse an empty current project. When a PP8
  transform is required and the current project already contains a timeline,
  the script creates an isolated `Director Cut` project so existing global
  color settings are not changed.
- If one Resolve append operation fails, the API offers no reliable
  transaction rollback. The log reports how many clips were appended so the
  user can inspect and undo the partial timeline.
- Semantic review requires an Ollama model reporting the `vision` capability.
  Plain `qwen2.5:3b` is a text-only smoke-test model and cannot run this
  multimodal path. On 16 GB VRAM, prefer Qwen3.8-27B Q4_K_M for dense visual
  review and Qwen3.8-27B Q8_0 for the serial text director.

## Tests

Tests require no GPU, Ollama, or Resolve:

```powershell
python -m unittest discover -s tests -v
python -m compileall -q gui.py main.py src tests
```

The project also includes a [human-reference editorial benchmark](evals/README.md)
for source-selection F1, trim-boundary error, shot order, repetition, and runtime
deviation on a frozen test set. It catches regressions; it does not replace blinded
human scoring of story clarity, emotion, music, or performance choice.

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
