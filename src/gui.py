#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Native Windows desktop interface for CyberEditor-Agent.
CyberEditor-Agent 的 Windows 原生桌面界面。

The UI intentionally launches ``main.py`` as a child process instead of
importing any ML package. This preserves the project's strict serial-process
and VRAM-release guarantees.

界面刻意通过子进程启动 ``main.py``，而不导入任何机器学习依赖，从而继续保证项目的
严格串行进程与显存释放策略。
"""

from __future__ import annotations

import ctypes
import json
import os
from pathlib import Path
import platform
import queue
import re
import shutil
import subprocess
import sys
import threading
from dataclasses import asdict, dataclass
from fractions import Fraction
from typing import Dict, List, Optional, Sequence, Tuple
from urllib import error as urllib_error
from urllib import request as urllib_request

import tkinter as tk
from tkinter import filedialog, messagebox, ttk

from .runtime_services import (
    RuntimeServiceError,
    ensure_ollama_service,
    find_ollama_executables,
    find_resolve_executable,
)

try:
    import winreg
except ImportError:  # pragma: no cover - only relevant outside Windows.
    winreg = None  # type: ignore[assignment]

APP_TITLE = "CyberEditor Agent"
DARK_PALETTE = {
    "BG": "#09111F",
    "CARD": "#111C2D",
    "CARD_ALT": "#162338",
    "FIELD": "#0B1525",
    "TEXT": "#F3F7FC",
    "MUTED": "#8FA2BA",
    "ACCENT": "#22D3A6",
    "ACCENT_HOVER": "#17B890",
    "ACCENT_TEXT": "#05120F",
    "BLUE": "#4C8DFF",
    "WARNING": "#F7B955",
    "ERROR": "#FF6B7A",
    "BORDER": "#263750",
    "SECONDARY_HOVER": "#20314A",
    "DANGER_BG": "#3B1B28",
    "DANGER_FG": "#FF9AA6",
    "DANGER_HOVER": "#542235",
    "DISABLED_BG": "#315B54",
    "DISABLED_TEXT": "#8AA59F",
    "LOG_TEXT": "#C9D7E8",
    "LOG_SELECT": "#23466F",
}
LIGHT_PALETTE = {
    "BG": "#EAF0F7",
    "CARD": "#FFFFFF",
    "CARD_ALT": "#E6EDF6",
    "FIELD": "#F7F9FC",
    "TEXT": "#142033",
    "MUTED": "#5D7088",
    "ACCENT": "#087F6A",
    "ACCENT_HOVER": "#066B59",
    "ACCENT_TEXT": "#FFFFFF",
    "BLUE": "#2563C7",
    "WARNING": "#A86100",
    "ERROR": "#C83D4D",
    "BORDER": "#C5D1E0",
    "SECONDARY_HOVER": "#D8E3F0",
    "DANGER_BG": "#FBE9EC",
    "DANGER_FG": "#B52F40",
    "DANGER_HOVER": "#F5D7DD",
    "DISABLED_BG": "#A9C9C2",
    "DISABLED_TEXT": "#657E79",
    "LOG_TEXT": "#26364A",
    "LOG_SELECT": "#C8DCF5",
}


def _install_palette(palette: Dict[str, str]) -> None:
    """Expose one palette to existing widget-building code. / 将一套配色应用到控件构建代码。"""
    globals().update(palette)


_install_palette(DARK_PALETTE)

FLOW_LABELS = {
    "完整流程 / Full pipeline": "full",
    "从 AI 导演开始 / Resume at director": "director",
    "仅执行 Resolve / Resolve only": "resolve",
}
FLOW_NAMES = {value: key for key, value in FLOW_LABELS.items()}
THEME_LABELS = {
    "跟随系统 / System": "system",
    "深色 / Dark": "dark",
    "浅色 / Light": "light",
}
THEME_NAMES = {value: key for key, value in THEME_LABELS.items()}
PROFILE_LABELS = {
    "自动检测 / Auto": "auto",
    "节能 / Conservative": "conservative",
    "均衡 / Balanced": "balanced",
    "高质量（较慢） / Quality (slower)": "performance",
    "自定义 / Custom": "custom",
}
PROFILE_NAMES = {value: key for key, value in PROFILE_LABELS.items()}


def enable_windows_high_dpi() -> str:
    """
    Enable the sharpest DPI mode available before creating a Tk window.
    在创建 Tk 窗口前启用系统支持的最高级 DPI 模式。

    Returns / 返回:
        The selected awareness mode, mainly for diagnostics and tests.
        已选择的感知模式，主要用于诊断与测试。
    """
    if os.name != "nt":
        return "platform-default"

    # Windows 10 Creators Update+: crisp rendering across mixed-DPI monitors.
    try:
        user32 = ctypes.windll.user32
        setter = user32.SetProcessDpiAwarenessContext
        setter.argtypes = [ctypes.c_void_p]
        setter.restype = ctypes.c_bool
        per_monitor_v2 = ctypes.c_void_p(-4)
        if setter(per_monitor_v2):
            return "per-monitor-v2"
    except (AttributeError, OSError):
        pass

    # Windows 8.1 fallback.
    try:
        shcore = ctypes.windll.shcore
        result = shcore.SetProcessDpiAwareness(2)
        if result in (0, -2147024891):  # S_OK or already configured.
            return "per-monitor"
    except (AttributeError, OSError):
        pass

    # Vista/Windows 7 fallback.
    try:
        if ctypes.windll.user32.SetProcessDPIAware():
            return "system"
    except (AttributeError, OSError):
        pass
    return "unavailable"


def get_primary_work_area(
    screen_width: int, screen_height: int
) -> Tuple[int, int, int, int]:
    """
    Return the primary monitor area not occupied by the taskbar.
    返回主显示器中未被任务栏占用的工作区域。

    The screen dimensions are used as a cross-platform fallback.
    非 Windows 平台或系统查询失败时使用传入的屏幕尺寸。
    """
    if os.name == "nt":
        class Rect(ctypes.Structure):
            _fields_ = [
                ("left", ctypes.c_long),
                ("top", ctypes.c_long),
                ("right", ctypes.c_long),
                ("bottom", ctypes.c_long),
            ]

        rectangle = Rect()
        try:
            if ctypes.windll.user32.SystemParametersInfoW(
                0x0030, 0, ctypes.byref(rectangle), 0
            ):
                return (
                    int(rectangle.left),
                    int(rectangle.top),
                    int(rectangle.right - rectangle.left),
                    int(rectangle.bottom - rectangle.top),
                )
        except (AttributeError, OSError):
            pass
    return 0, 0, int(screen_width), int(screen_height)


def detect_system_theme() -> str:
    """
    Read the Windows application color preference.
    读取 Windows 应用颜色偏好。

    Returns / 返回:
        ``"light"`` or ``"dark"``. Non-Windows systems default to dark.
        ``"light"`` 或 ``"dark"``；非 Windows 系统默认深色。
    """
    if os.name != "nt" or winreg is None:
        return "dark"
    try:
        with winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize",
        ) as key:
            value, _ = winreg.QueryValueEx(key, "AppsUseLightTheme")
        return "light" if int(value) else "dark"
    except (OSError, TypeError, ValueError):
        return "dark"


def detect_system_memory_gb() -> float:
    """Return installed system memory in GiB. / 返回已安装系统内存（GiB）。"""
    if os.name == "nt":
        class MemoryStatus(ctypes.Structure):
            _fields_ = [
                ("length", ctypes.c_ulong),
                ("memory_load", ctypes.c_ulong),
                ("total_physical", ctypes.c_ulonglong),
                ("available_physical", ctypes.c_ulonglong),
                ("total_page_file", ctypes.c_ulonglong),
                ("available_page_file", ctypes.c_ulonglong),
                ("total_virtual", ctypes.c_ulonglong),
                ("available_virtual", ctypes.c_ulonglong),
                ("available_extended_virtual", ctypes.c_ulonglong),
            ]

        status = MemoryStatus()
        status.length = ctypes.sizeof(MemoryStatus)
        try:
            if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status)):
                return round(status.total_physical / (1024**3), 1)
        except (AttributeError, OSError):
            pass
    try:
        page_size = os.sysconf("SC_PAGE_SIZE")
        page_count = os.sysconf("SC_PHYS_PAGES")
        return round((page_size * page_count) / (1024**3), 1)
    except (AttributeError, OSError, ValueError):
        return 0.0


def _hidden_creation_flags() -> int:
    """Return Windows' no-console flag when available. / 在可用时返回 Windows 无控制台标志。"""
    if os.name == "nt" and hasattr(subprocess, "CREATE_NO_WINDOW"):
        return int(subprocess.CREATE_NO_WINDOW)
    return 0


def detect_hardware() -> Dict[str, object]:
    """
    Detect CPU, RAM, and the most capable GPU without importing PyTorch.
    在不导入 PyTorch 的情况下检测 CPU、内存与能力最强的 GPU。

    ``nvidia-smi`` is preferred for accurate NVIDIA VRAM. A read-only Windows
    CIM query is used as a vendor-neutral fallback.
    NVIDIA 显存优先通过 ``nvidia-smi`` 精确读取，其他显卡回退到只读 CIM 查询。
    """
    hardware: Dict[str, object] = {
        "cpu": platform.processor() or platform.machine() or "Unknown CPU",
        "cpu_threads": int(os.cpu_count() or 1),
        "ram_gb": detect_system_memory_gb(),
        "gpu": "Integrated / unknown GPU",
        "vram_gb": 0.0,
        "gpu_vendor": "unknown",
    }
    environment = build_runtime_environment()
    nvidia_candidates = [
        shutil.which("nvidia-smi", path=environment.get("PATH")),
        str(Path(os.environ.get("WINDIR", r"C:\Windows")) / "System32" / "nvidia-smi.exe"),
        r"C:\Program Files\NVIDIA Corporation\NVSMI\nvidia-smi.exe",
    ]
    nvidia_smi = next(
        (
            candidate
            for candidate in nvidia_candidates
            if candidate and Path(candidate).is_file()
        ),
        None,
    )
    if nvidia_smi:
        try:
            result = subprocess.run(
                [
                    nvidia_smi,
                    "--query-gpu=name,memory.total",
                    "--format=csv,noheader,nounits",
                ],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=8,
                check=False,
                creationflags=_hidden_creation_flags(),
            )
            gpu_rows: List[Tuple[str, float]] = []
            for line in result.stdout.splitlines():
                name, separator, memory = line.rpartition(",")
                if not separator:
                    continue
                try:
                    gpu_rows.append((name.strip(), float(memory.strip()) / 1024.0))
                except ValueError:
                    continue
            if gpu_rows:
                name, vram = max(gpu_rows, key=lambda item: item[1])
                hardware.update(
                    {
                        "gpu": name,
                        "vram_gb": round(vram, 1),
                        "gpu_vendor": "nvidia",
                    }
                )
                return hardware
        except (OSError, subprocess.SubprocessError):
            pass

    if os.name == "nt":
        powershell = shutil.which("powershell.exe") or shutil.which("powershell")
        if powershell:
            command = (
                "$items=Get-CimInstance Win32_VideoController | "
                "Select-Object Name,AdapterRAM; "
                "$items | ConvertTo-Json -Compress"
            )
            try:
                result = subprocess.run(
                    [powershell, "-NoProfile", "-NonInteractive", "-Command", command],
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    timeout=10,
                    check=False,
                    creationflags=_hidden_creation_flags(),
                )
                payload = json.loads(result.stdout.strip() or "[]")
                rows = payload if isinstance(payload, list) else [payload]
                gpu_rows = []
                for row in rows:
                    if not isinstance(row, dict):
                        continue
                    name = str(row.get("Name", "")).strip()
                    try:
                        vram = float(row.get("AdapterRAM") or 0) / (1024**3)
                    except (TypeError, ValueError):
                        vram = 0.0
                    if name:
                        gpu_rows.append((name, vram))
                if gpu_rows:
                    name, vram = max(gpu_rows, key=lambda item: item[1])
                    vendor = (
                        "amd" if "amd" in name.casefold() or "radeon" in name.casefold()
                        else "intel" if "intel" in name.casefold()
                        else "unknown"
                    )
                    hardware.update(
                        {
                            "gpu": name,
                            "vram_gb": round(vram, 1),
                            "gpu_vendor": vendor,
                        }
                    )
            except (OSError, ValueError, subprocess.SubprocessError):
                pass
    return hardware


def detect_torch_runtime() -> Dict[str, object]:
    """
    Probe PyTorch CUDA support in a disposable child process.
    在一次性子进程中检测 PyTorch CUDA 支持。

    The UI process never imports PyTorch, so the probe cannot retain a CUDA
    context or consume workflow VRAM after it exits.
    UI 进程不会导入 PyTorch；检测子进程退出后不会保留 CUDA 上下文或占用工作流显存。
    """
    script = (
        "import json\n"
        "try:\n"
        " import torch\n"
        " ready=bool(torch.cuda.is_available())\n"
        " name=torch.cuda.get_device_name(0) if ready else ''\n"
        " print(json.dumps({'torch_available':True,'torch_version':"
        "str(torch.__version__),'torch_cuda':ready,'torch_device':name}))\n"
        "except Exception as exc:\n"
        " print(json.dumps({'torch_available':False,'torch_version':'',"
        "'torch_cuda':False,'torch_device':'','torch_error':str(exc)}))\n"
    )
    try:
        result = subprocess.run(
            [sys.executable, "-c", script],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=25,
            check=False,
            creationflags=_hidden_creation_flags(),
            env=build_runtime_environment(),
        )
        payload = json.loads(result.stdout.strip().splitlines()[-1])
        if isinstance(payload, dict):
            return payload
    except (OSError, ValueError, IndexError, subprocess.SubprocessError):
        pass
    return {
        "torch_available": False,
        "torch_version": "",
        "torch_cuda": False,
        "torch_device": "",
    }


def recommend_automatic_settings(
    hardware: Dict[str, object],
    ollama_models: Sequence[Dict[str, object]],
) -> Dict[str, object]:
    """
    Derive conservative serial-workflow settings from detected hardware.
    根据检测到的硬件生成保守的串行工作流配置。

    Only installed Ollama models are considered. Source/project FPS is never
    guessed because it is a media property, not a hardware capability.
    只考虑本机已安装的 Ollama 模型；素材/工程 FPS 属于媒体属性，绝不按硬件猜测。
    """
    ram_gb = float(hardware.get("ram_gb") or 0.0)
    vram_gb = float(hardware.get("vram_gb") or 0.0)
    cpu_threads = int(hardware.get("cpu_threads") or 1)
    torch_cuda = bool(hardware.get("torch_cuda"))

    if torch_cuda and vram_gb >= 12:
        # Accuracy is preferred over throughput.  The stages are serial, so a
        # 16 GiB GPU can safely devote itself to Whisper large-v3 and release
        # it before Ollama starts.
        # 优先保证识别准确率。各阶段严格串行，因此 16 GiB 显卡可独占运行
        # Whisper large-v3，并在启动 Ollama 前完整释放。
        whisper_model = "large-v3"
    elif torch_cuda and vram_gb >= 8:
        whisper_model = "turbo"
    elif (torch_cuda and vram_gb >= 4) or (
        ram_gb >= 24 and cpu_threads >= 8
    ):
        whisper_model = "small"
    else:
        whisper_model = "base"

    if ram_gb >= 64:
        # Shorter chunks preserve local narrative detail.  More model calls are
        # slower but produce better edit decisions for hour-long footage.
        # 更短的分块能保留局部叙事细节；调用次数更多但长视频剪辑决策更细致。
        chunk_minutes = 10.0
        num_ctx = 16384
        profile = "performance"
    elif ram_gb >= 24 or vram_gb >= 6:
        chunk_minutes = 12.0
        num_ctx = 8192
        profile = "balanced"
    else:
        chunk_minutes = 10.0
        num_ctx = 4096
        profile = "conservative"

    # Preserve at least ~10 GiB for Windows, Resolve, and orchestration.
    model_budget_gb = max(2.0, min(ram_gb * 0.55, max(2.0, ram_gb - 10.0)))
    candidates: List[Tuple[str, float, float]] = []
    all_models: List[Tuple[str, float, float]] = []
    for model in ollama_models:
        name = str(model.get("name", "")).strip()
        try:
            size_gb = float(model.get("size") or 0) / (1024**3)
        except (TypeError, ValueError):
            size_gb = 0.0
        if not name:
            continue
        normalized = name.casefold()
        quality_score = size_gb
        # Editing quality depends on instruction following, Chinese support,
        # long-context reasoning, and quantization—not simply file size.
        # 剪辑质量取决于指令遵循、中文、长上下文与量化，而非只看文件大小。
        family_scores = (
            ("qwen3.5:35b-a3b", 1000.0),
            ("qwen3.5:27b", 980.0),
            ("qwen3.5:9b", 940.0),
            ("qwen3:30b", 850.0),
            ("gpt-oss:20b", 820.0),
            ("qwen3:14b", 790.0),
            ("qwen2.5:32b", 740.0),
            ("qwen2.5:14b", 680.0),
            ("gemma3:12b", 640.0),
        )
        for marker, score in family_scores:
            if marker in normalized:
                quality_score = score
                break
        if "q8_0" in normalized or "q8-0" in normalized:
            quality_score += 35.0
        elif "q6_k" in normalized:
            quality_score += 25.0
        elif "q4_k_m" in normalized:
            quality_score += 15.0
        all_models.append((name, size_gb, quality_score))
        if size_gb <= model_budget_gb:
            candidates.append((name, size_gb, quality_score))
    selected_pool = candidates or all_models
    selected_model = (
        max(selected_pool, key=lambda item: (item[2], item[1]))[0]
        if selected_pool
        else ""
    )
    return {
        "profile": profile,
        "whisper_model": whisper_model,
        "whisper_device": "auto",
        "chunk_minutes": chunk_minutes,
        "num_ctx": num_ctx,
        "ollama_model": selected_model,
        "model_budget_gb": round(model_budget_gb, 1),
    }


def _absolute_path(value: str, project_root: Path) -> Path:
    """Resolve a user path against the repository. / 将用户路径解析为仓库内外的绝对路径。"""
    path = Path(value).expanduser()
    if not path.is_absolute():
        path = project_root / path
    return path.resolve()


def build_runtime_environment() -> Dict[str, str]:
    """
    Build a Windows-friendly child-process environment.
    构建兼容 Windows 常见安装位置的子进程环境。

    Returns / 返回:
        A copy of the current environment with common FFmpeg and Ollama
        directories added to ``PATH``.
        当前环境的副本，并将 FFmpeg 与 Ollama 的常见目录加入 ``PATH``。
    """
    environment = os.environ.copy()
    local_app_data = Path(
        environment.get("LOCALAPPDATA", Path.home() / "AppData" / "Local")
    )
    candidates = [
        local_app_data / "Microsoft" / "WinGet" / "Links",
        local_app_data / "Programs" / "Ollama",
        Path(r"C:\Program Files\GitHub CLI"),
    ]
    ollama_cli, ollama_app = find_ollama_executables()
    for executable in (ollama_cli, ollama_app):
        if executable is not None:
            candidates.append(executable.parent)
    existing = environment.get("PATH", "").split(os.pathsep)
    normalized = {item.casefold() for item in existing if item}
    prefixes = [
        str(path)
        for path in candidates
        if path.is_dir() and str(path).casefold() not in normalized
    ]
    if prefixes:
        environment["PATH"] = os.pathsep.join(prefixes + existing)
    environment["PYTHONUNBUFFERED"] = "1"
    environment["PYTHONIOENCODING"] = "utf-8"
    return environment


def parse_frame_rate(value: object) -> float:
    """
    Parse an FFprobe frame-rate value such as ``30000/1001``.
    解析 FFprobe 帧率值，例如 ``30000/1001``。

    Parameters / 参数:
        value:
            Number or rational string reported by FFprobe.
            FFprobe 返回的数字或有理数字符串。

    Returns / 返回:
        A positive FPS rounded to six decimals. / 四舍五入到六位小数的正 FPS。

    Raises / 异常:
        ValueError:
            If the value is missing, zero, negative, or implausible.
            当数值缺失、为零、为负数或明显不合理时抛出。
    """
    try:
        fps = float(Fraction(str(value).strip()))
    except (OverflowError, ValueError, ZeroDivisionError) as exc:
        raise ValueError(f"Invalid frame rate: {value!r}") from exc
    if not 1.0 <= fps <= 240.0:
        raise ValueError(f"Frame rate outside 1-240 fps: {fps}")
    return round(fps, 6)


def detect_media_fps(path: Path) -> float:
    """
    Read the source video FPS with FFprobe without importing OpenCV.
    使用 FFprobe 读取源视频 FPS，且不导入 OpenCV。

    Parameters / 参数:
        path:
            Existing source or proxy media file. / 已存在的源素材或代理文件。

    Returns / 返回:
        The average video-stream frame rate. / 视频流平均帧率。

    The helper runs FFprobe as a short-lived process so the desktop UI remains
    free of media/ML imports and cannot retain GPU memory.
    本函数通过短生命周期进程运行 FFprobe，使桌面 UI 不导入媒体/机器学习包，
    也不会保留 GPU 内存。
    """
    media_path = Path(path).expanduser().resolve()
    if not media_path.is_file():
        raise ValueError(f"Media file not found: {media_path}")
    environment = build_runtime_environment()
    ffprobe = shutil.which("ffprobe", path=environment.get("PATH"))
    if not ffprobe:
        raise ValueError("ffprobe was not found on PATH.")
    try:
        result = subprocess.run(
            [
                ffprobe,
                "-v",
                "error",
                "-select_streams",
                "v:0",
                "-show_entries",
                "stream=avg_frame_rate,r_frame_rate",
                "-of",
                "json",
                str(media_path),
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=20,
            check=False,
            creationflags=_hidden_creation_flags(),
            env=environment,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise ValueError(f"Could not run ffprobe: {exc}") from exc
    if result.returncode != 0:
        detail = result.stderr.strip() or f"exit code {result.returncode}"
        raise ValueError(f"ffprobe failed: {detail}")
    try:
        payload = json.loads(result.stdout)
        stream = payload["streams"][0]
    except (ValueError, KeyError, IndexError, TypeError) as exc:
        raise ValueError("ffprobe did not report a video stream.") from exc
    for key in ("avg_frame_rate", "r_frame_rate"):
        try:
            return parse_frame_rate(stream.get(key))
        except (ValueError, AttributeError):
            continue
    raise ValueError("ffprobe did not report a valid video frame rate.")


@dataclass
class WorkflowOptions:
    """Serializable UI workflow configuration. / 可序列化的界面工作流配置。"""

    video: str = ""
    proxy: str = ""
    data_dir: str = "data/ui-run"
    flow: str = "full"
    hardware_profile: str = "auto"
    theme: str = "system"
    ui_language: str = "system"
    fps_mode: str = "auto"
    whisper_model: str = "small"
    whisper_device: str = "auto"
    language: str = ""
    ollama_model: str = "qwen2.5:3b"
    ollama_url: str = "http://localhost:11434"
    chunk_minutes: float = 12.0
    project_fps: float = 25.0
    num_ctx: int = 8192
    timeline_name: str = "CyberEditor Timeline"
    project_name: str = "CyberEditor Project"
    skip_resolve: bool = True
    strict_fps: bool = False

    def validate(self, project_root: Path) -> None:
        """
        Validate paths and numeric constraints before launching.
        在启动工作流前校验路径与数值约束。

        Parameters / 参数:
            project_root:
                Repository root used for relative paths.
                用于解析相对路径的仓库根目录。
        """
        if self.flow not in {"full", "director", "resolve"}:
            raise ValueError("未知工作流模式 / Unknown workflow mode.")
        data_path = _absolute_path(self.data_dir, project_root)
        if self.flow == "full":
            if not self.video:
                raise ValueError("请选择源视频 / Please select a source video.")
            if not _absolute_path(self.video, project_root).is_file():
                raise ValueError(f"源视频不存在 / Source video not found:\n{self.video}")
        elif self.flow == "director":
            raw_data = data_path / "raw_data.json"
            if not raw_data.is_file():
                raise ValueError(
                    "从导演阶段继续需要已有 raw_data.json / "
                    f"Resume at director requires:\n{raw_data}"
                )
        elif self.flow == "resolve":
            timeline = data_path / "timeline_cuts.json"
            if not timeline.is_file():
                raise ValueError(
                    "仅执行 Resolve 需要已有 timeline_cuts.json / "
                    f"Resolve-only requires:\n{timeline}"
                )
            if self.skip_resolve:
                raise ValueError(
                    "“仅执行 Resolve”不能同时跳过 Resolve / "
                    "Resolve-only cannot skip Resolve."
                )

        if self.proxy and not _absolute_path(self.proxy, project_root).is_file():
            raise ValueError(f"代理素材不存在 / Proxy not found:\n{self.proxy}")
        if self.flow != "resolve" and not self.ollama_model.strip():
            raise ValueError("请选择 Ollama 模型 / Please select an Ollama model.")
        if not 10.0 <= float(self.chunk_minutes) <= 15.0:
            raise ValueError("分块时长必须为 10–15 分钟 / Chunk size must be 10–15 minutes.")
        if float(self.project_fps) <= 0:
            raise ValueError("项目 FPS 必须大于 0 / Project FPS must be positive.")
        if int(self.num_ctx) < 2048:
            raise ValueError("Ollama 上下文至少为 2048 / Ollama context must be >= 2048.")

    def build_command(
        self, python_executable: str, project_root: Path
    ) -> List[str]:
        """
        Convert UI state into a safe argument list for ``main.py``.
        将界面状态转换为调用 ``main.py`` 的安全参数列表。

        Parameters / 参数:
            python_executable:
                Python interpreter used to launch the workflow.
                用于启动工作流的 Python 解释器。
            project_root:
                Repository root containing ``main.py``.
                包含 ``main.py`` 的仓库根目录。
        """
        self.validate(project_root)
        command = [
            python_executable,
            str(project_root / "main.py"),
            "--data-dir",
            str(_absolute_path(self.data_dir, project_root)),
            "--whisper-model",
            self.whisper_model,
            "--whisper-device",
            self.whisper_device,
            "--ollama-model",
            self.ollama_model,
            "--ollama-url",
            self.ollama_url,
            "--chunk-minutes",
            str(self.chunk_minutes),
            "--project-fps",
            str(self.project_fps),
            "--num-ctx",
            str(self.num_ctx),
            "--timeline-name",
            self.timeline_name,
            "--project-name",
            self.project_name,
            "--log-level",
            "INFO",
        ]
        if self.video:
            command.extend(
                ["--video", str(_absolute_path(self.video, project_root))]
            )
        if self.proxy:
            command.extend(
                ["--proxy", str(_absolute_path(self.proxy, project_root))]
            )
        if self.language.strip():
            command.extend(["--language", self.language.strip()])
        if self.flow in {"director", "resolve"}:
            command.append("--skip-extraction")
        if self.flow == "resolve":
            command.append("--skip-director")
        if self.skip_resolve:
            command.append("--skip-resolve")
        if self.strict_fps:
            command.append("--strict-fps")
        return command


class CyberEditorApp:
    """
    Responsive Tkinter desktop controller for the serial workflow.
    用于控制严格串行工作流的响应式 Tkinter 桌面界面。
    """

    def __init__(self, root: tk.Tk, project_root: Path) -> None:
        """Initialize state, widgets, and background checks. / 初始化状态、控件与后台检查。"""
        self.root = root
        self.project_root = project_root.resolve()
        self.settings_path = self.project_root / "data" / "ui-settings.json"
        self.saved_settings = self._read_settings_data()
        self.theme_mode = str(self.saved_settings.get("theme", "system"))
        if self.theme_mode not in {"system", "dark", "light"}:
            self.theme_mode = "system"
        self.active_theme = (
            detect_system_theme()
            if self.theme_mode == "system"
            else self.theme_mode
        )
        _install_palette(
            LIGHT_PALETTE if self.active_theme == "light" else DARK_PALETTE
        )
        self.process: Optional[subprocess.Popen[str]] = None
        self.active_options: Optional[WorkflowOptions] = None
        self.messages: "queue.Queue[Tuple[str, object]]" = queue.Queue()
        self.stop_requested = False
        self.status_labels: Dict[str, tk.Label] = {}
        self.detected_hardware: Dict[str, object] = {}
        self.available_ollama_models: List[Dict[str, object]] = []
        self.automatic_recommendation: Dict[str, object] = {}

        self._configure_window()
        self._configure_styles()
        self._create_variables()
        self._load_settings()
        self._build_layout()
        self._apply_flow_rules()
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)
        self.root.after(100, self._drain_messages)
        # Paint the window first; hardware/PyTorch probes stay in background.
        self.root.after(750, self._start_environment_check)
        self.root.after(2000, self._poll_system_theme)

    def _configure_window(self) -> None:
        """Configure the main window. / 配置主窗口。"""
        self.root.title(APP_TITLE)
        self.root.configure(bg=BG)
        try:
            dpi = float(self.root.winfo_fpixels("1i"))
        except tk.TclError:
            dpi = 96.0
        dpi_scale = max(1.0, dpi / 96.0)
        # Tk uses pixels for geometry and points for fonts. Match both to the
        # physical monitor DPI so Windows never bitmap-stretches the window.
        self.root.tk.call("tk", "scaling", dpi / 72.0)
        screen_width = self.root.winfo_screenwidth()
        screen_height = self.root.winfo_screenheight()
        area_left, area_top, area_width, area_height = get_primary_work_area(
            screen_width, screen_height
        )
        width = min(int(1240 * dpi_scale), int(area_width * 0.92))
        height = min(int(800 * dpi_scale), int(area_height * 0.88))
        minimum_width = min(int(1040 * dpi_scale), width)
        minimum_height = min(int(700 * dpi_scale), height)
        left = area_left + max(0, (area_width - width) // 2)
        top = area_top + max(0, (area_height - height) // 2)
        self.root.geometry(f"{width}x{height}+{left}+{top}")
        self.root.minsize(minimum_width, minimum_height)

    def _configure_styles(self) -> None:
        """Create or refresh the active visual system. / 创建或刷新当前视觉样式系统。"""
        style = ttk.Style(self.root)
        try:
            style.theme_use("clam")
        except tk.TclError:
            pass
        style.configure(".", font=("Segoe UI", 10))
        style.configure("Root.TFrame", background=BG)
        style.configure("Card.TFrame", background=CARD)
        style.configure("CardAlt.TFrame", background=CARD_ALT)
        style.configure("TLabel", background=CARD, foreground=TEXT)
        style.configure("Muted.TLabel", background=CARD, foreground=MUTED)
        style.configure(
            "Section.TLabel",
            background=CARD,
            foreground=TEXT,
            font=("Segoe UI Semibold", 11),
        )
        style.configure(
            "TEntry",
            fieldbackground=FIELD,
            foreground=TEXT,
            insertcolor=TEXT,
            bordercolor=BORDER,
            lightcolor=BORDER,
            darkcolor=BORDER,
            padding=7,
        )
        style.map("TEntry", fieldbackground=[("disabled", CARD_ALT)])
        style.configure(
            "TCombobox",
            fieldbackground=FIELD,
            background=FIELD,
            foreground=TEXT,
            arrowcolor=MUTED,
            bordercolor=BORDER,
            padding=6,
        )
        style.map(
            "TCombobox",
            fieldbackground=[("readonly", FIELD)],
            foreground=[("readonly", TEXT)],
            selectbackground=[("readonly", FIELD)],
            selectforeground=[("readonly", TEXT)],
        )
        style.configure(
            "TSpinbox",
            fieldbackground=FIELD,
            foreground=TEXT,
            arrowcolor=MUTED,
            bordercolor=BORDER,
            padding=6,
        )
        style.configure(
            "TCheckbutton",
            background=CARD,
            foreground=TEXT,
            indicatorbackground=FIELD,
            indicatorforeground=ACCENT,
            padding=2,
        )
        style.map(
            "TCheckbutton",
            background=[("active", CARD)],
            foreground=[("disabled", MUTED)],
        )
        style.configure(
            "Accent.TButton",
            background=ACCENT,
            foreground=ACCENT_TEXT,
            borderwidth=0,
            padding=(18, 10),
            font=("Segoe UI Semibold", 10),
        )
        style.map(
            "Accent.TButton",
            background=[("active", ACCENT_HOVER), ("disabled", DISABLED_BG)],
            foreground=[("disabled", DISABLED_TEXT)],
        )
        style.configure(
            "Secondary.TButton",
            background=CARD_ALT,
            foreground=TEXT,
            bordercolor=BORDER,
            padding=(12, 8),
        )
        style.map("Secondary.TButton", background=[("active", SECONDARY_HOVER)])
        style.configure(
            "Danger.TButton",
            background=DANGER_BG,
            foreground=DANGER_FG,
            borderwidth=0,
            padding=(14, 10),
        )
        style.map("Danger.TButton", background=[("active", DANGER_HOVER)])
        style.configure(
            "Cyber.Horizontal.TProgressbar",
            troughcolor=FIELD,
            background=ACCENT,
            bordercolor=FIELD,
            lightcolor=ACCENT,
            darkcolor=ACCENT,
            thickness=7,
        )
        self.root.option_add("*TCombobox*Listbox.background", FIELD)
        self.root.option_add("*TCombobox*Listbox.foreground", TEXT)
        self.root.option_add("*TCombobox*Listbox.selectBackground", BLUE)

    def _create_variables(self) -> None:
        """Create Tk variables with practical defaults. / 创建带有实用默认值的 Tk 变量。"""
        self.video_var = tk.StringVar()
        self.proxy_var = tk.StringVar()
        self.data_var = tk.StringVar(value="data/ui-run")
        self.flow_var = tk.StringVar(value=FLOW_NAMES["full"])
        self.profile_var = tk.StringVar(value=PROFILE_NAMES["auto"])
        self.theme_var = tk.StringVar(value=THEME_NAMES[self.theme_mode])
        self.whisper_var = tk.StringVar(value="small")
        self.device_var = tk.StringVar(value="auto")
        self.language_var = tk.StringVar()
        self.ollama_model_var = tk.StringVar(value="qwen2.5:3b")
        self.ollama_url_var = tk.StringVar(value="http://localhost:11434")
        self.chunk_var = tk.DoubleVar(value=12.0)
        self.fps_var = tk.DoubleVar(value=25.0)
        self.ctx_var = tk.IntVar(value=8192)
        self.timeline_var = tk.StringVar(value="CyberEditor Timeline")
        self.project_var = tk.StringVar(value="CyberEditor Project")
        self.skip_resolve_var = tk.BooleanVar(value=True)
        self.strict_fps_var = tk.BooleanVar(value=False)
        self.stage_var = tk.StringVar(value="准备就绪 / Ready")
        self.hardware_summary_var = tk.StringVar(
            value="正在检测硬件并计算自动配置 / Detecting hardware…"
        )
        self.progress_var = tk.DoubleVar(value=0)

    def _build_layout(self) -> None:
        """Build the complete responsive interface. / 构建完整的响应式界面。"""
        outer = ttk.Frame(self.root, style="Root.TFrame", padding=(24, 18, 24, 20))
        outer.grid(row=0, column=0, sticky="nsew")
        self.root.rowconfigure(0, weight=1)
        self.root.columnconfigure(0, weight=1)
        outer.columnconfigure(0, weight=1)
        outer.rowconfigure(2, weight=1)

        self._build_header(outer)
        self._build_status_strip(outer)

        content = ttk.Panedwindow(outer, orient=tk.HORIZONTAL)
        content.grid(row=2, column=0, sticky="nsew", pady=(14, 0))
        form_card = ttk.Frame(content, style="Card.TFrame", padding=18)
        log_card = ttk.Frame(content, style="Card.TFrame", padding=18)
        content.add(form_card, weight=5)
        content.add(log_card, weight=4)
        self._build_form(form_card)
        self._build_log(log_card)

    def _build_header(self, parent: ttk.Frame) -> None:
        """Build branding and global actions. / 构建品牌区与全局操作。"""
        header = ttk.Frame(parent, style="Root.TFrame")
        header.grid(row=0, column=0, sticky="ew")
        header.columnconfigure(1, weight=1)

        self.logo_canvas = tk.Canvas(
            header, width=48, height=48, bg=BG, highlightthickness=0
        )
        self.logo_canvas.grid(row=0, column=0, rowspan=2, padx=(0, 13))
        self.logo_accent_item = self.logo_canvas.create_polygon(
            24, 3, 43, 14, 43, 35, 24, 46, 5, 35, 5, 14,
            fill=ACCENT, outline=""
        )
        self.logo_cutout_item = self.logo_canvas.create_polygon(
            20, 15, 33, 24, 20, 33,
            fill=BG, outline=""
        )

        tk.Label(
            header,
            text="CYBEREDITOR AGENT",
            bg=BG,
            fg=TEXT,
            font=("Segoe UI Semibold", 19),
        ).grid(row=0, column=1, sticky="sw")
        tk.Label(
            header,
            text="LOCAL  •  PRIVATE  •  SERIAL AI WORKFLOW",
            bg=BG,
            fg=ACCENT,
            font=("Segoe UI Semibold", 9),
        ).grid(row=1, column=1, sticky="nw", pady=(1, 0))

        self.theme_combo = ttk.Combobox(
            header,
            textvariable=self.theme_var,
            values=list(THEME_LABELS),
            state="readonly",
            width=19,
        )
        self.theme_combo.grid(row=0, column=2, rowspan=2, padx=(8, 4))
        self.theme_combo.bind("<<ComboboxSelected>>", self._on_theme_change)

        self.open_output_button = ttk.Button(
            header,
            text="打开输出 / Open output",
            style="Secondary.TButton",
            command=self._open_output,
        )
        self.open_output_button.grid(row=0, column=3, rowspan=2, padx=(4, 0))

    def _build_status_strip(self, parent: ttk.Frame) -> None:
        """Build environment status chips. / 构建环境状态卡片。"""
        strip = ttk.Frame(parent, style="Root.TFrame")
        strip.grid(row=1, column=0, sticky="ew", pady=(15, 0))
        for index, key in enumerate(("Python", "FFmpeg", "Ollama", "Resolve")):
            strip.columnconfigure(index, weight=1)
            card = tk.Frame(strip, bg=CARD_ALT, padx=12, pady=7)
            card.grid(
                row=0,
                column=index,
                sticky="ew",
                padx=(0 if index == 0 else 5, 0 if index == 3 else 5),
            )
            tk.Label(
                card,
                text=key.upper(),
                bg=CARD_ALT,
                fg=MUTED,
                font=("Segoe UI Semibold", 8),
            ).pack(side=tk.LEFT)
            label = tk.Label(
                card,
                text="● 检测中",
                bg=CARD_ALT,
                fg=MUTED,
                font=("Segoe UI", 9),
            )
            label.pack(side=tk.RIGHT)
            self.status_labels[key] = label

    def _build_form(self, parent: ttk.Frame) -> None:
        """Build workflow input and model settings. / 构建工作流输入与模型设置。"""
        parent.columnconfigure(0, weight=1)
        parent.rowconfigure(10, weight=1)

        ttk.Label(parent, text="工作流配置 / Workflow", style="Section.TLabel").grid(
            row=0, column=0, sticky="w"
        )
        ttk.Label(
            parent,
            text="所有重型阶段仍由独立子进程严格串行执行。",
            style="Muted.TLabel",
        ).grid(row=1, column=0, sticky="w", pady=(2, 13))

        paths = ttk.Frame(parent, style="Card.TFrame")
        paths.grid(row=2, column=0, sticky="ew")
        paths.columnconfigure(1, weight=1)
        self._path_row(
            paths, 0, "源视频 / Source", self.video_var, self._choose_video, "选择"
        )
        self._path_row(
            paths, 1, "代理素材 / Proxy", self.proxy_var, self._choose_proxy, "选择"
        )
        self._path_row(
            paths, 2, "运行数据 / Data", self.data_var, self._choose_data_dir, "目录"
        )

        separator = tk.Frame(parent, bg=BORDER, height=1)
        separator.grid(row=3, column=0, sticky="ew", pady=15)

        grid = ttk.Frame(parent, style="Card.TFrame")
        grid.grid(row=4, column=0, sticky="ew")
        grid.columnconfigure(0, weight=1)
        grid.columnconfigure(1, weight=1)

        self.flow_combo = self._field(
            grid,
            0,
            0,
            "运行模式 / Mode",
            ttk.Combobox,
            textvariable=self.flow_var,
            values=list(FLOW_LABELS),
            state="readonly",
        )
        self.flow_combo.bind("<<ComboboxSelected>>", self._apply_flow_rules)
        self.profile_combo = self._field(
            grid,
            0,
            1,
            "性能配置 / Hardware profile",
            ttk.Combobox,
            textvariable=self.profile_var,
            values=list(PROFILE_LABELS),
            state="readonly",
        )
        self.profile_combo.bind("<<ComboboxSelected>>", self._on_profile_change)
        self.whisper_combo = self._field(
            grid,
            1,
            0,
            "Whisper 模型",
            ttk.Combobox,
            textvariable=self.whisper_var,
            values=("tiny.en", "tiny", "base", "small", "medium", "turbo"),
            state="readonly",
        )
        self._field(
            grid,
            1,
            1,
            "Whisper 设备 / Device",
            ttk.Combobox,
            textvariable=self.device_var,
            values=("auto", "cpu", "cuda"),
            state="readonly",
        )
        self._field(
            grid,
            2,
            0,
            "语言 / Language",
            ttk.Entry,
            textvariable=self.language_var,
        )
        self.ollama_combo = self._field(
            grid,
            2,
            1,
            "Ollama 模型",
            ttk.Combobox,
            textvariable=self.ollama_model_var,
            values=("qwen2.5:3b", "qwen2.5:14b", "qwen2.5:32b"),
        )
        self._field(
            grid,
            3,
            0,
            "Ollama URL",
            ttk.Entry,
            textvariable=self.ollama_url_var,
        )
        self._field(
            grid,
            3,
            1,
            "分块分钟 / Chunk",
            ttk.Spinbox,
            textvariable=self.chunk_var,
            from_=10,
            to=15,
            increment=0.5,
        )
        self._field(
            grid,
            4,
            0,
            "项目帧率 / FPS",
            ttk.Spinbox,
            textvariable=self.fps_var,
            from_=1,
            to=120,
            increment=0.001,
        )
        self._field(
            grid,
            4,
            1,
            "Ollama 上下文 / Context",
            ttk.Combobox,
            textvariable=self.ctx_var,
            values=(2048, 4096, 8192, 16384, 32768),
        )
        self._field(
            grid,
            5,
            0,
            "时间线 / Timeline",
            ttk.Entry,
            textvariable=self.timeline_var,
        )
        self._field(
            grid,
            5,
            1,
            "Resolve 工程 / Project",
            ttk.Entry,
            textvariable=self.project_var,
        )

        ttk.Label(
            parent,
            textvariable=self.hardware_summary_var,
            style="Muted.TLabel",
        ).grid(row=5, column=0, sticky="w", pady=(10, 0))

        checks = ttk.Frame(parent, style="Card.TFrame")
        checks.grid(row=6, column=0, sticky="ew", pady=(10, 0))
        self.skip_resolve_check = ttk.Checkbutton(
            checks,
            text="只生成 JSON（未装 Studio 时保持勾选）/ JSON only",
            variable=self.skip_resolve_var,
        )
        self.skip_resolve_check.pack(side=tk.LEFT)
        ttk.Checkbutton(
            checks,
            text="严格 FPS / Strict FPS",
            variable=self.strict_fps_var,
        ).pack(side=tk.LEFT, padx=(18, 0))
        ttk.Label(
            parent,
            text="提示：外部 Python 自动组装时间线需要 DaVinci Resolve Studio。",
            style="Muted.TLabel",
        ).grid(row=7, column=0, sticky="w", pady=(7, 0))

        footer = ttk.Frame(parent, style="Card.TFrame")
        footer.grid(row=11, column=0, sticky="ew", pady=(18, 0))
        footer.columnconfigure(0, weight=1)
        self.start_button = ttk.Button(
            footer,
            text="▶  开始运行 / START",
            style="Accent.TButton",
            command=self._start_workflow,
        )
        self.start_button.grid(row=0, column=0, sticky="ew", padx=(0, 8))
        self.stop_button = ttk.Button(
            footer,
            text="■  停止",
            style="Danger.TButton",
            command=self._stop_workflow,
            state=tk.DISABLED,
        )
        self.stop_button.grid(row=0, column=1)

    def _build_log(self, parent: ttk.Frame) -> None:
        """Build live progress and log console. / 构建实时进度与日志控制台。"""
        parent.columnconfigure(0, weight=1)
        parent.rowconfigure(4, weight=1)
        ttk.Label(parent, text="运行监控 / Live monitor", style="Section.TLabel").grid(
            row=0, column=0, sticky="w"
        )
        ttk.Label(
            parent,
            textvariable=self.stage_var,
            style="Muted.TLabel",
        ).grid(row=1, column=0, sticky="w", pady=(4, 8))
        ttk.Progressbar(
            parent,
            style="Cyber.Horizontal.TProgressbar",
            variable=self.progress_var,
            maximum=100,
        ).grid(row=2, column=0, sticky="ew")

        bar = ttk.Frame(parent, style="Card.TFrame")
        bar.grid(row=3, column=0, sticky="ew", pady=(14, 7))
        ttk.Label(bar, text="CONSOLE", style="Muted.TLabel").pack(side=tk.LEFT)
        ttk.Button(
            bar,
            text="清空",
            style="Secondary.TButton",
            command=lambda: self.log_text.delete("1.0", tk.END),
        ).pack(side=tk.RIGHT)

        console = tk.Frame(parent, bg=FIELD, highlightbackground=BORDER, highlightthickness=1)
        console.grid(row=4, column=0, sticky="nsew")
        console.rowconfigure(0, weight=1)
        console.columnconfigure(0, weight=1)
        self.log_text = tk.Text(
            console,
            bg=FIELD,
            fg=LOG_TEXT,
            insertbackground=TEXT,
            selectbackground=LOG_SELECT,
            relief=tk.FLAT,
            borderwidth=0,
            padx=12,
            pady=10,
            wrap=tk.WORD,
            font=("Cascadia Mono", 9),
            state=tk.NORMAL,
        )
        scroll = ttk.Scrollbar(console, command=self.log_text.yview)
        self.log_text.configure(yscrollcommand=scroll.set)
        self.log_text.grid(row=0, column=0, sticky="nsew")
        scroll.grid(row=0, column=1, sticky="ns")
        self.log_text.tag_configure("info", foreground=LOG_TEXT)
        self.log_text.tag_configure("success", foreground=ACCENT)
        self.log_text.tag_configure("warning", foreground=WARNING)
        self.log_text.tag_configure("error", foreground=ERROR)
        self._append_log(
            "界面已启动。请选择素材并开始运行。\n"
            "UI ready. Select media and start the workflow.\n",
            "success",
        )

        actions = ttk.Frame(parent, style="Card.TFrame")
        actions.grid(row=5, column=0, sticky="ew", pady=(10, 0))
        actions.columnconfigure((0, 1), weight=1)
        ttk.Button(
            actions,
            text="查看 timeline_cuts.json",
            style="Secondary.TButton",
            command=self._open_timeline,
        ).grid(row=0, column=0, sticky="ew", padx=(0, 5))
        ttk.Button(
            actions,
            text="重新检测环境",
            style="Secondary.TButton",
            command=self._start_environment_check,
        ).grid(row=0, column=1, sticky="ew", padx=(5, 0))

    def _path_row(
        self,
        parent: ttk.Frame,
        row: int,
        label: str,
        variable: tk.StringVar,
        command: object,
        button_text: str,
    ) -> None:
        """Create a labeled path input row. / 创建带标签的路径输入行。"""
        ttk.Label(parent, text=label, style="Muted.TLabel").grid(
            row=row, column=0, sticky="w", pady=5, padx=(0, 9)
        )
        ttk.Entry(parent, textvariable=variable).grid(
            row=row, column=1, sticky="ew", pady=5
        )
        ttk.Button(
            parent,
            text=button_text,
            style="Secondary.TButton",
            command=command,
        ).grid(row=row, column=2, padx=(8, 0), pady=5)

    def _field(
        self,
        parent: ttk.Frame,
        row: int,
        column: int,
        label: str,
        widget_class: object,
        **kwargs: object,
    ) -> object:
        """Create one compact labeled field. / 创建一个紧凑的带标签字段。"""
        container = ttk.Frame(parent, style="Card.TFrame")
        container.grid(
            row=row,
            column=column,
            sticky="ew",
            padx=(0, 7) if column == 0 else (7, 0),
            pady=5,
        )
        container.columnconfigure(0, weight=1)
        ttk.Label(container, text=label, style="Muted.TLabel").grid(
            row=0, column=0, sticky="w", pady=(0, 4)
        )
        widget = widget_class(container, **kwargs)
        widget.grid(row=1, column=0, sticky="ew")
        return widget

    def _choose_video(self) -> None:
        """Select a source media file. / 选择源媒体文件。"""
        path = filedialog.askopenfilename(
            title="选择源视频 / Select source video",
            filetypes=[
                ("Video", "*.mp4 *.mov *.mkv *.avi *.mxf *.mts *.m2ts"),
                ("All files", "*.*"),
            ],
        )
        if path:
            self.video_var.set(path)
            if not self.proxy_var.get().strip():
                self.proxy_var.set(path)

    def _choose_proxy(self) -> None:
        """Select a proxy media file. / 选择代理媒体文件。"""
        path = filedialog.askopenfilename(
            title="选择代理素材 / Select proxy media",
            filetypes=[
                ("Video", "*.mp4 *.mov *.mkv *.avi *.mxf"),
                ("All files", "*.*"),
            ],
        )
        if path:
            self.proxy_var.set(path)

    def _choose_data_dir(self) -> None:
        """Select the runtime data directory. / 选择运行数据目录。"""
        path = filedialog.askdirectory(
            title="选择运行数据目录 / Select runtime data directory",
            initialdir=str(self.project_root / "data"),
        )
        if path:
            self.data_var.set(path)

    def _collect_options(self) -> WorkflowOptions:
        """Read and normalize current UI values. / 读取并规范化当前界面值。"""
        project_fps = parse_frame_rate(self.fps_var.get())
        return WorkflowOptions(
            video=self.video_var.get().strip(),
            proxy=self.proxy_var.get().strip(),
            data_dir=self.data_var.get().strip() or "data/ui-run",
            flow=FLOW_LABELS.get(self.flow_var.get(), "full"),
            hardware_profile=PROFILE_LABELS.get(
                self.profile_var.get(), "auto"
            ),
            theme=THEME_LABELS.get(self.theme_var.get(), "system"),
            # The dependency-free fallback UI still exposes an explicit numeric
            # field, so preserve it as a manual choice when the modern UI later
            # reads the same settings file. / 后备界面仍使用数值输入框，因此把它
            # 保存为手动选择，避免现代界面将其误解为自动检测。
            fps_mode=self._format_fps_mode(project_fps),
            whisper_model=self.whisper_var.get().strip(),
            whisper_device=self.device_var.get().strip(),
            language=self.language_var.get().strip(),
            ollama_model=self.ollama_model_var.get().strip(),
            ollama_url=self.ollama_url_var.get().strip(),
            chunk_minutes=float(self.chunk_var.get()),
            project_fps=project_fps,
            num_ctx=int(self.ctx_var.get()),
            timeline_name=self.timeline_var.get().strip() or "CyberEditor Timeline",
            project_name=self.project_var.get().strip() or "CyberEditor Project",
            skip_resolve=bool(self.skip_resolve_var.get()),
            strict_fps=bool(self.strict_fps_var.get()),
        )

    @staticmethod
    def _format_fps_mode(fps: float) -> str:
        """Serialize a manual FPS without unnecessary zeros. / 序列化手动 FPS 并去除多余零。"""
        return f"{fps:.6f}".rstrip("0").rstrip(".")

    def _apply_flow_rules(self, _event: object = None) -> None:
        """Keep mode-dependent controls internally consistent. / 保持模式相关控件的一致性。"""
        flow = FLOW_LABELS.get(self.flow_var.get(), "full")
        if flow == "resolve":
            self.skip_resolve_var.set(False)
            self.skip_resolve_check.configure(state=tk.DISABLED)
        else:
            self.skip_resolve_check.configure(state=tk.NORMAL)

    def _on_profile_change(self, _event: object = None) -> None:
        """Apply a selected hardware preset and persist it. / 应用所选硬件预设并保存。"""
        profile = PROFILE_LABELS.get(self.profile_var.get(), "auto")
        self._apply_hardware_profile(profile)
        self._save_current_preferences()

    def _apply_hardware_profile(self, profile: str) -> None:
        """
        Apply automatic or named serial-workflow settings.
        应用自动或命名的串行工作流设置。

        FPS is intentionally untouched because it must match the source and
        Resolve project rather than the computer.
        FPS 不会被修改，因为它必须匹配素材和 Resolve 工程，而不是电脑配置。
        """
        if profile == "custom":
            self.hardware_summary_var.set(
                self._hardware_description("自定义参数 / Custom settings")
            )
            return
        if profile == "auto":
            if not self.automatic_recommendation:
                self.hardware_summary_var.set(
                    "正在检测硬件并计算自动配置 / Detecting hardware…"
                )
                return
            settings = self.automatic_recommendation
            label = "自动配置 / Auto"
        else:
            presets: Dict[str, Dict[str, object]] = {
                "conservative": {
                    "whisper_model": "base",
                    "whisper_device": "auto",
                    "chunk_minutes": 10.0,
                    "num_ctx": 4096,
                },
                "balanced": {
                    "whisper_model": "small",
                    "whisper_device": "auto",
                    "chunk_minutes": 12.0,
                    "num_ctx": 8192,
                },
                "performance": {
                    "whisper_model": "large-v3",
                    "whisper_device": "auto",
                    "chunk_minutes": 10.0,
                    "num_ctx": 16384,
                },
            }
            settings = presets.get(profile, presets["balanced"])
            label = PROFILE_NAMES.get(profile, profile)

        self.whisper_var.set(str(settings["whisper_model"]))
        self.device_var.set(str(settings["whisper_device"]))
        self.chunk_var.set(float(settings["chunk_minutes"]))
        self.ctx_var.set(int(settings["num_ctx"]))
        recommended_model = str(settings.get("ollama_model", "")).strip()
        if recommended_model:
            self.ollama_model_var.set(recommended_model)
        details = (
            f"{label}: Whisper {self.whisper_var.get()} • "
            f"Context {self.ctx_var.get()} • Chunk {self.chunk_var.get():g}m"
        )
        if recommended_model:
            details += f" • {recommended_model}"
        self.hardware_summary_var.set(self._hardware_description(details))

    def _hardware_description(self, suffix: str = "") -> str:
        """Format the detected hardware in one compact line. / 将检测硬件格式化为紧凑单行。"""
        if not self.detected_hardware:
            return suffix or "硬件尚未检测 / Hardware not detected"
        ram = float(self.detected_hardware.get("ram_gb") or 0)
        threads = int(self.detected_hardware.get("cpu_threads") or 1)
        gpu = str(self.detected_hardware.get("gpu") or "Unknown GPU")
        gpu = gpu.replace(" with Max-Q Design", " Max-Q").replace("NVIDIA ", "")
        vram = float(self.detected_hardware.get("vram_gb") or 0)
        gpu_text = f"{gpu} {vram:g}GB" if vram > 0 else gpu
        prefix = f"{gpu_text} • RAM {ram:g}GB • CPU {threads}T"
        torch_mode = (
            "PyTorch CUDA"
            if bool(self.detected_hardware.get("torch_cuda"))
            else "PyTorch CPU"
        )
        prefix = f"{prefix} • {torch_mode}"
        return f"{prefix}\n↳ {suffix}" if suffix else prefix

    def _on_theme_change(self, _event: object = None) -> None:
        """Apply and persist the selected theme mode. / 应用并保存所选主题模式。"""
        mode = THEME_LABELS.get(self.theme_var.get(), "system")
        self._apply_theme(mode)
        self._save_current_preferences()

    def _apply_theme(self, mode: str) -> None:
        """
        Switch palettes immediately without restarting the UI.
        无需重启界面即可立即切换配色。
        """
        if mode not in {"system", "dark", "light"}:
            mode = "system"
        actual = detect_system_theme() if mode == "system" else mode
        self.theme_mode = mode
        if actual == self.active_theme:
            return

        old_palette = (
            LIGHT_PALETTE if self.active_theme == "light" else DARK_PALETTE
        )
        new_palette = LIGHT_PALETTE if actual == "light" else DARK_PALETTE
        self.active_theme = actual
        _install_palette(new_palette)
        self._configure_styles()
        self._recolor_native_widgets(self.root, old_palette, new_palette)
        self.logo_canvas.itemconfigure(self.logo_accent_item, fill=ACCENT)
        self.logo_canvas.itemconfigure(self.logo_cutout_item, fill=BG)
        self.log_text.tag_configure("info", foreground=LOG_TEXT)
        self.log_text.tag_configure("success", foreground=ACCENT)
        self.log_text.tag_configure("warning", foreground=WARNING)
        self.log_text.tag_configure("error", foreground=ERROR)
        self.root.update_idletasks()

    def _recolor_native_widgets(
        self,
        widget: tk.Misc,
        old_palette: Dict[str, str],
        new_palette: Dict[str, str],
    ) -> None:
        """Map native Tk widget colors between palettes. / 在两套配色间映射原生 Tk 控件颜色。"""
        color_keys = {
            value.casefold(): key for key, value in old_palette.items()
        }
        updates: Dict[str, str] = {}
        for option in (
            "background",
            "foreground",
            "insertbackground",
            "selectbackground",
            "highlightbackground",
        ):
            try:
                current = str(widget.cget(option)).casefold()
            except tk.TclError:
                continue
            palette_key = color_keys.get(current)
            if palette_key and palette_key in new_palette:
                updates[option] = new_palette[palette_key]
        if updates:
            try:
                widget.configure(**updates)
            except tk.TclError:
                pass
        for child in widget.winfo_children():
            self._recolor_native_widgets(child, old_palette, new_palette)

    def _poll_system_theme(self) -> None:
        """Track Windows theme changes while System mode is active. / 在跟随系统模式下监测 Windows 主题变化。"""
        if self.theme_mode == "system":
            actual = detect_system_theme()
            if actual != self.active_theme:
                self._apply_theme("system")
        self.root.after(2000, self._poll_system_theme)

    def _save_current_preferences(self) -> None:
        """Persist current UI-only choices without validating media. / 无需校验素材即可保存当前界面选择。"""
        try:
            self._save_settings(self._collect_options())
        except (OSError, ValueError, tk.TclError):
            pass

    def _start_workflow(self) -> None:
        """Validate settings and launch the serial workflow. / 校验设置并启动串行工作流。"""
        if self.process is not None:
            return
        try:
            options = self._collect_options()
            command = options.build_command(sys.executable, self.project_root)
            self._save_settings(options)
        except (ValueError, tk.TclError, OSError) as exc:
            messagebox.showerror("无法启动 / Cannot start", str(exc), parent=self.root)
            return

        self.stop_requested = False
        self.active_options = options
        self.progress_var.set(5)
        self.stage_var.set("正在启动 / Starting")
        self.start_button.configure(state=tk.DISABLED)
        self.stop_button.configure(state=tk.NORMAL)
        self._append_log("\n" + "─" * 62 + "\n", "info")
        self._append_log("启动严格串行工作流 / Starting serial workflow\n", "success")
        self._append_log(
            "命令 / Command: " + subprocess.list2cmdline(command) + "\n", "info"
        )
        try:
            creation_flags = (
                subprocess.CREATE_NO_WINDOW
                if os.name == "nt" and hasattr(subprocess, "CREATE_NO_WINDOW")
                else 0
            )
            self.process = subprocess.Popen(
                command,
                cwd=str(self.project_root),
                env=build_runtime_environment(),
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="replace",
                bufsize=1,
                creationflags=creation_flags,
            )
        except OSError as exc:
            self.process = None
            self.start_button.configure(state=tk.NORMAL)
            self.stop_button.configure(state=tk.DISABLED)
            messagebox.showerror(
                "启动失败 / Launch failed", str(exc), parent=self.root
            )
            return
        threading.Thread(
            target=self._read_process_output,
            args=(self.process,),
            daemon=True,
        ).start()

    def _read_process_output(self, process: subprocess.Popen[str]) -> None:
        """Stream child output into the thread-safe UI queue. / 将子进程输出流送入线程安全队列。"""
        assert process.stdout is not None
        try:
            for line in process.stdout:
                self.messages.put(("log", line))
        finally:
            return_code = process.wait()
            self.messages.put(("done", return_code))

    def _stop_workflow(self) -> None:
        """Terminate the active workflow process tree. / 终止当前工作流进程树。"""
        process = self.process
        if process is None:
            return
        if not messagebox.askyesno(
            "停止任务 / Stop workflow",
            "确定停止当前任务吗？已完成的中间文件会保留。\n"
            "Stop now? Completed intermediate files will be kept.",
            parent=self.root,
        ):
            return
        self.stop_requested = True
        self.stage_var.set("正在停止 / Stopping")
        self._append_log("用户请求停止任务 / Stop requested by user\n", "warning")
        threading.Thread(
            target=self._terminate_process_tree,
            args=(process, self.active_options),
            daemon=True,
        ).start()

    def _terminate_process_tree(
        self,
        process: subprocess.Popen[str],
        options: Optional[WorkflowOptions] = None,
    ) -> None:
        """Stop parent and heavy child stages on Windows. / 在 Windows 上停止父进程与重型子阶段。"""
        if process.poll() is not None:
            return
        try:
            if os.name == "nt":
                creation_flags = (
                    subprocess.CREATE_NO_WINDOW
                    if hasattr(subprocess, "CREATE_NO_WINDOW")
                    else 0
                )
                subprocess.run(
                    ["taskkill", "/PID", str(process.pid), "/T", "/F"],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    check=False,
                    creationflags=creation_flags,
                )
            else:
                process.terminate()
        except OSError as exc:
            self.messages.put(("log", f"停止失败 / Stop failed: {exc}\n"))
        finally:
            if options is not None:
                self._unload_active_ollama(
                    options.ollama_model, options.ollama_url
                )

    def _unload_active_ollama(self, model: str, base_url: str) -> None:
        """
        Unload the selected model only when Ollama reports it as resident.
        仅当 Ollama 报告模型驻留时才发送卸载请求。

        This avoids accidentally loading a model when the user stops during
        Whisper extraction.
        这样可以避免用户在 Whisper 阶段停止时反而触发模型加载。
        """
        if not model or not base_url:
            return
        try:
            with urllib_request.urlopen(
                base_url.rstrip("/") + "/api/ps", timeout=3
            ) as response:
                payload = json.loads(response.read().decode("utf-8"))
            resident = {
                str(item.get("name") or item.get("model") or "").strip()
                for item in payload.get("models", [])
            }
            if model not in resident:
                return
            body = json.dumps(
                {
                    "model": model,
                    "prompt": "",
                    "stream": False,
                    "keep_alive": 0,
                }
            ).encode("utf-8")
            request = urllib_request.Request(
                base_url.rstrip("/") + "/api/generate",
                data=body,
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urllib_request.urlopen(request, timeout=15):
                pass
            self.messages.put(
                (
                    "log",
                    f"已安全卸载 Ollama 模型 {model} / Model unloaded\n",
                )
            )
        except (OSError, ValueError, urllib_error.URLError) as exc:
            self.messages.put(
                (
                    "log",
                    f"警告：停止后的 Ollama 卸载检查失败 / "
                    f"Post-stop unload check failed: {exc}\n",
                )
            )

    def _drain_messages(self) -> None:
        """Apply worker-thread messages on the Tk main thread. / 在 Tk 主线程处理后台消息。"""
        try:
            while True:
                kind, payload = self.messages.get_nowait()
                if kind == "log":
                    line = self._strip_ansi(str(payload))
                    self._append_log(line, self._log_tag(line))
                    self._update_stage_from_log(line)
                elif kind == "done":
                    self._finish_workflow(int(payload))
                elif kind == "environment":
                    self._apply_environment_result(payload)
        except queue.Empty:
            pass
        self.root.after(100, self._drain_messages)

    def _finish_workflow(self, return_code: int) -> None:
        """Restore controls and report the final exit state. / 恢复控件并报告最终退出状态。"""
        self.process = None
        self.active_options = None
        self.start_button.configure(state=tk.NORMAL)
        self.stop_button.configure(state=tk.DISABLED)
        if return_code == 0:
            self.progress_var.set(100)
            self.stage_var.set("全部完成 / Completed")
            self._append_log("✓ 工作流成功完成 / Workflow completed successfully\n", "success")
        elif self.stop_requested:
            self.progress_var.set(0)
            self.stage_var.set("已停止 / Stopped")
            self._append_log("任务已停止 / Workflow stopped\n", "warning")
        else:
            self.stage_var.set(f"运行失败（退出码 {return_code}）/ Failed")
            self._append_log(
                f"✕ 工作流失败，退出码 {return_code} / Workflow failed\n", "error"
            )

    def _update_stage_from_log(self, line: str) -> None:
        """Map orchestrator log markers to coarse progress. / 将调度器日志标记映射为阶段进度。"""
        if "Starting stage:" in line:
            if "Extract" in line:
                self.progress_var.set(12)
                self.stage_var.set("1/3 数据提取 / Extracting")
            elif "Direct" in line:
                self.progress_var.set(48)
                self.stage_var.set("2/3 AI 导演 / Directing")
            elif "Resolve" in line:
                self.progress_var.set(82)
                self.stage_var.set("3/3 Resolve 组装 / Assembling")
        elif "VRAM barrier passed: Whisper/OpenCV" in line:
            self.progress_var.set(42)
            self.stage_var.set("Whisper 已释放 / Whisper released")
        elif "VRAM barrier passed: Ollama" in line:
            self.progress_var.set(78)
            self.stage_var.set("Ollama 已释放 / Ollama released")
        elif "All selected stages completed" in line:
            self.progress_var.set(100)

    def _start_environment_check(self) -> None:
        """Start a probe using UI values captured on the main thread. / 使用主线程取得的界面值启动检测。"""
        ollama_url = self.ollama_url_var.get().strip()
        threading.Thread(
            target=self._check_environment,
            args=(ollama_url,),
            daemon=True,
        ).start()

    def _check_environment(self, ollama_url: str) -> None:
        """Probe dependencies without blocking the UI. / 在不阻塞界面的情况下检测依赖。"""
        environment = build_runtime_environment()
        results: Dict[str, Tuple[bool, str]] = {
            "Python": (
                sys.version_info >= (3, 10),
                f"{sys.version_info.major}.{sys.version_info.minor}",
            )
        }
        ffmpeg = shutil.which("ffmpeg", path=environment.get("PATH"))
        results["FFmpeg"] = (bool(ffmpeg), "就绪" if ffmpeg else "未找到")

        models: List[Dict[str, object]] = []
        try:
            models, ollama_started = ensure_ollama_service(
                ollama_url, timeout=30.0
            )
            results["Ollama"] = (
                True,
                (
                    f"{len(models)} 个模型 · 已自动启动"
                    if ollama_started and models
                    else "服务在线 · 已自动启动"
                    if ollama_started
                    else f"{len(models)} 个模型"
                    if models
                    else "服务在线"
                ),
            )
        except (
            OSError,
            ValueError,
            urllib_error.URLError,
            RuntimeServiceError,
        ):
            results["Ollama"] = (False, "未连接")

        resolve_ready = find_resolve_executable() is not None
        results["Resolve"] = (
            resolve_ready,
            "已安装 · 执行时自动启动" if resolve_ready else "未安装",
        )
        hardware = detect_hardware()
        hardware.update(detect_torch_runtime())
        recommendation = recommend_automatic_settings(hardware, models)
        self.messages.put(
            ("environment", (results, models, hardware, recommendation))
        )

    def _apply_environment_result(self, payload: object) -> None:
        """Render dependency checks and installed Ollama models. / 显示依赖检测与本地模型。"""
        results, models, hardware, recommendation = payload  # type: ignore[misc]
        self.detected_hardware = dict(hardware)
        self.available_ollama_models = list(models)
        self.automatic_recommendation = dict(recommendation)
        for key, (ready, text) in results.items():
            label = self.status_labels.get(key)
            if label is not None:
                label.configure(text=f"● {text}", fg=ACCENT if ready else ERROR)
        model_names = [str(item["name"]) for item in models]
        if model_names:
            self.ollama_combo.configure(values=tuple(model_names))
            current = self.ollama_model_var.get()
            if current not in model_names:
                self.ollama_model_var.set(model_names[0])
        profile = PROFILE_LABELS.get(self.profile_var.get(), "auto")
        self._apply_hardware_profile(profile)
        summary = "，".join(
            f"{name}: {'OK' if ready else text}"
            for name, (ready, text) in results.items()
        )
        self._append_log("环境检测 / Environment: " + summary + "\n", "info")
        self._append_log(
            "硬件检测 / Hardware: " + self._hardware_description() + "\n",
            "info",
        )
        if profile == "auto":
            self._append_log(
                "已应用自动配置 / Auto settings applied: "
                f"Whisper={self.whisper_var.get()}, "
                f"Ollama={self.ollama_model_var.get()}, "
                f"Context={self.ctx_var.get()}, "
                f"Chunk={self.chunk_var.get():g}m\n",
                "success",
            )

    def _append_log(self, text: str, tag: str = "info") -> None:
        """Append colored text and keep the newest line visible. / 追加彩色日志并保持最新行可见。"""
        self.log_text.insert(tk.END, text, tag)
        self.log_text.see(tk.END)

    @staticmethod
    def _strip_ansi(text: str) -> str:
        """Remove terminal escape sequences. / 移除终端 ANSI 转义序列。"""
        return re.sub(r"\x1b\[[0-?]*[ -/]*[@-~]", "", text)

    @staticmethod
    def _log_tag(line: str) -> str:
        """Choose a console color from log severity. / 根据日志级别选择控制台颜色。"""
        upper = line.upper()
        if "ERROR" in upper or "FAILED" in upper or "失败" in line:
            return "error"
        if "WARNING" in upper or "WARN" in upper or "警告" in line:
            return "warning"
        if "COMPLETED" in upper or "完成" in line or "RELEASED" in upper:
            return "success"
        return "info"

    def _open_output(self) -> None:
        """Open the selected runtime output directory. / 打开所选运行输出目录。"""
        path = _absolute_path(self.data_var.get() or "data/ui-run", self.project_root)
        path.mkdir(parents=True, exist_ok=True)
        self._open_path(path)

    def _open_timeline(self) -> None:
        """Open the generated timeline JSON with its default app. / 用默认程序打开剪辑 JSON。"""
        path = (
            _absolute_path(self.data_var.get() or "data/ui-run", self.project_root)
            / "timeline_cuts.json"
        )
        if not path.is_file():
            messagebox.showinfo(
                "尚无输出 / No output",
                f"尚未生成文件：\n{path}",
                parent=self.root,
            )
            return
        self._open_path(path)

    @staticmethod
    def _open_path(path: Path) -> None:
        """Open a local path using the platform shell. / 使用平台外壳打开本地路径。"""
        if os.name == "nt":
            os.startfile(str(path))  # type: ignore[attr-defined]
        elif sys.platform == "darwin":
            subprocess.Popen(["open", str(path)])
        else:
            subprocess.Popen(["xdg-open", str(path)])

    def _read_settings_data(self) -> Dict[str, object]:
        """Read local UI settings, returning an empty object on failure. / 读取本地界面设置，失败时返回空对象。"""
        if not self.settings_path.is_file():
            return {}
        try:
            payload = json.loads(self.settings_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        return payload if isinstance(payload, dict) else {}

    def _save_settings(self, options: WorkflowOptions) -> None:
        """Persist non-secret UI settings under ignored runtime data. / 在已忽略的运行目录保存非敏感设置。"""
        self.settings_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.settings_path.with_suffix(".tmp")
        payload = asdict(options)
        temporary.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        temporary.replace(self.settings_path)
        self.saved_settings = payload

    def _load_settings(self) -> None:
        """Restore the previous local UI session when available. / 在可用时恢复上次本地界面设置。"""
        data = self.saved_settings
        if not data:
            return
        mapping = {
            "video": self.video_var,
            "proxy": self.proxy_var,
            "data_dir": self.data_var,
            "whisper_model": self.whisper_var,
            "whisper_device": self.device_var,
            "language": self.language_var,
            "ollama_model": self.ollama_model_var,
            "ollama_url": self.ollama_url_var,
            "chunk_minutes": self.chunk_var,
            "project_fps": self.fps_var,
            "num_ctx": self.ctx_var,
            "timeline_name": self.timeline_var,
            "project_name": self.project_var,
            "skip_resolve": self.skip_resolve_var,
            "strict_fps": self.strict_fps_var,
        }
        for key, variable in mapping.items():
            if key in data:
                try:
                    variable.set(data[key])
                except tk.TclError:
                    pass
        flow = str(data.get("flow", "full"))
        self.flow_var.set(FLOW_NAMES.get(flow, FLOW_NAMES["full"]))
        profile = str(data.get("hardware_profile", "auto"))
        self.profile_var.set(PROFILE_NAMES.get(profile, PROFILE_NAMES["auto"]))
        self.theme_var.set(THEME_NAMES.get(self.theme_mode, THEME_NAMES["system"]))

    def _on_close(self) -> None:
        """Protect active work before closing the window. / 关闭窗口前保护正在运行的任务。"""
        if self.process is not None:
            if not messagebox.askyesno(
                "任务正在运行 / Workflow running",
                "关闭界面将停止当前任务，是否继续？\n"
                "Closing will stop the active workflow. Continue?",
                parent=self.root,
            ):
                return
            self.stop_requested = True
            self._terminate_process_tree(self.process, self.active_options)
        self.root.destroy()


def main(argv: Optional[Sequence[str]] = None) -> int:
    """
    Launch the desktop interface.
    启动桌面界面。

    Parameters / 参数:
        argv:
            Reserved for future command-line UI options.
            为未来的界面命令行选项预留。
    """
    del argv
    enable_windows_high_dpi()
    project_root = Path(__file__).resolve().parents[1]
    root = tk.Tk()
    CyberEditorApp(root, project_root)
    root.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
