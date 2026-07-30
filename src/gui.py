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

import json
import os
from pathlib import Path
import queue
import re
import shutil
import subprocess
import sys
import threading
from dataclasses import asdict, dataclass
from typing import Dict, List, Optional, Sequence, Tuple
from urllib import error as urllib_error
from urllib import request as urllib_request

import tkinter as tk
from tkinter import filedialog, messagebox, ttk


APP_TITLE = "CyberEditor Agent"
BG = "#09111F"
CARD = "#111C2D"
CARD_ALT = "#162338"
FIELD = "#0B1525"
TEXT = "#F3F7FC"
MUTED = "#8FA2BA"
ACCENT = "#22D3A6"
ACCENT_HOVER = "#17B890"
BLUE = "#4C8DFF"
WARNING = "#F7B955"
ERROR = "#FF6B7A"
BORDER = "#263750"

FLOW_LABELS = {
    "完整流程 / Full pipeline": "full",
    "从 AI 导演开始 / Resume at director": "director",
    "仅执行 Resolve / Resolve only": "resolve",
}
FLOW_NAMES = {value: key for key, value in FLOW_LABELS.items()}


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


@dataclass
class WorkflowOptions:
    """Serializable UI workflow configuration. / 可序列化的界面工作流配置。"""

    video: str = ""
    proxy: str = ""
    data_dir: str = "data/ui-run"
    flow: str = "full"
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
        self.process: Optional[subprocess.Popen[str]] = None
        self.active_options: Optional[WorkflowOptions] = None
        self.messages: "queue.Queue[Tuple[str, object]]" = queue.Queue()
        self.stop_requested = False
        self.status_labels: Dict[str, tk.Label] = {}

        self._configure_window()
        self._configure_styles()
        self._create_variables()
        self._load_settings()
        self._build_layout()
        self._apply_flow_rules()
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)
        self.root.after(100, self._drain_messages)
        self.root.after(150, self._start_environment_check)

    def _configure_window(self) -> None:
        """Configure the main window. / 配置主窗口。"""
        self.root.title(APP_TITLE)
        self.root.geometry("1240x800")
        self.root.minsize(1040, 700)
        self.root.configure(bg=BG)
        try:
            self.root.tk.call("tk", "scaling", 1.15)
        except tk.TclError:
            pass

    def _configure_styles(self) -> None:
        """Create the dark visual system. / 创建深色视觉样式系统。"""
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
            foreground="#05120F",
            borderwidth=0,
            padding=(18, 10),
            font=("Segoe UI Semibold", 10),
        )
        style.map(
            "Accent.TButton",
            background=[("active", ACCENT_HOVER), ("disabled", "#315B54")],
            foreground=[("disabled", "#8AA59F")],
        )
        style.configure(
            "Secondary.TButton",
            background=CARD_ALT,
            foreground=TEXT,
            bordercolor=BORDER,
            padding=(12, 8),
        )
        style.map("Secondary.TButton", background=[("active", "#20314A")])
        style.configure(
            "Danger.TButton",
            background="#3B1B28",
            foreground="#FF9AA6",
            borderwidth=0,
            padding=(14, 10),
        )
        style.map("Danger.TButton", background=[("active", "#542235")])
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

        logo = tk.Canvas(
            header, width=48, height=48, bg=BG, highlightthickness=0
        )
        logo.grid(row=0, column=0, rowspan=2, padx=(0, 13))
        logo.create_polygon(
            24, 3, 43, 14, 43, 35, 24, 46, 5, 35, 5, 14,
            fill=ACCENT, outline=""
        )
        logo.create_polygon(
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

        self.open_output_button = ttk.Button(
            header,
            text="打开输出 / Open output",
            style="Secondary.TButton",
            command=self._open_output,
        )
        self.open_output_button.grid(row=0, column=2, rowspan=2, padx=(8, 0))

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
        self.whisper_combo = self._field(
            grid,
            0,
            1,
            "Whisper 模型",
            ttk.Combobox,
            textvariable=self.whisper_var,
            values=("tiny.en", "tiny", "base", "small", "medium", "turbo"),
            state="readonly",
        )
        self._field(
            grid,
            1,
            0,
            "Whisper 设备 / Device",
            ttk.Combobox,
            textvariable=self.device_var,
            values=("auto", "cpu", "cuda"),
            state="readonly",
        )
        self._field(
            grid,
            1,
            1,
            "语言 / Language",
            ttk.Entry,
            textvariable=self.language_var,
        )
        self.ollama_combo = self._field(
            grid,
            2,
            0,
            "Ollama 模型",
            ttk.Combobox,
            textvariable=self.ollama_model_var,
            values=("qwen2.5:3b", "qwen2.5:14b", "qwen2.5:32b"),
        )
        self._field(
            grid,
            2,
            1,
            "Ollama URL",
            ttk.Entry,
            textvariable=self.ollama_url_var,
        )
        self._field(
            grid,
            3,
            0,
            "分块分钟 / Chunk",
            ttk.Spinbox,
            textvariable=self.chunk_var,
            from_=10,
            to=15,
            increment=0.5,
        )
        self._field(
            grid,
            3,
            1,
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
            0,
            "Ollama 上下文 / Context",
            ttk.Combobox,
            textvariable=self.ctx_var,
            values=(2048, 4096, 8192, 16384, 32768),
        )
        self._field(
            grid,
            4,
            1,
            "时间线 / Timeline",
            ttk.Entry,
            textvariable=self.timeline_var,
        )
        self._field(
            grid,
            5,
            0,
            "Resolve 工程 / Project",
            ttk.Entry,
            textvariable=self.project_var,
        )

        checks = ttk.Frame(parent, style="Card.TFrame")
        checks.grid(row=5, column=0, sticky="ew", pady=(13, 0))
        self.skip_resolve_check = ttk.Checkbutton(
            checks,
            text="只生成 JSON（跳过 Resolve）/ JSON only",
            variable=self.skip_resolve_var,
        )
        self.skip_resolve_check.pack(side=tk.LEFT)
        ttk.Checkbutton(
            checks,
            text="严格 FPS / Strict FPS",
            variable=self.strict_fps_var,
        ).pack(side=tk.LEFT, padx=(18, 0))

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
            fg="#C9D7E8",
            insertbackground=TEXT,
            selectbackground="#23466F",
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
        self.log_text.tag_configure("info", foreground="#C9D7E8")
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
        return WorkflowOptions(
            video=self.video_var.get().strip(),
            proxy=self.proxy_var.get().strip(),
            data_dir=self.data_var.get().strip() or "data/ui-run",
            flow=FLOW_LABELS.get(self.flow_var.get(), "full"),
            whisper_model=self.whisper_var.get().strip(),
            whisper_device=self.device_var.get().strip(),
            language=self.language_var.get().strip(),
            ollama_model=self.ollama_model_var.get().strip(),
            ollama_url=self.ollama_url_var.get().strip(),
            chunk_minutes=float(self.chunk_var.get()),
            project_fps=float(self.fps_var.get()),
            num_ctx=int(self.ctx_var.get()),
            timeline_name=self.timeline_var.get().strip() or "CyberEditor Timeline",
            project_name=self.project_var.get().strip() or "CyberEditor Project",
            skip_resolve=bool(self.skip_resolve_var.get()),
            strict_fps=bool(self.strict_fps_var.get()),
        )

    def _apply_flow_rules(self, _event: object = None) -> None:
        """Keep mode-dependent controls internally consistent. / 保持模式相关控件的一致性。"""
        flow = FLOW_LABELS.get(self.flow_var.get(), "full")
        if flow == "resolve":
            self.skip_resolve_var.set(False)
            self.skip_resolve_check.configure(state=tk.DISABLED)
        else:
            self.skip_resolve_check.configure(state=tk.NORMAL)

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

        models: List[str] = []
        try:
            request = urllib_request.Request(
                ollama_url.rstrip("/") + "/api/tags",
                headers={"Accept": "application/json"},
            )
            with urllib_request.urlopen(request, timeout=3) as response:
                payload = json.loads(response.read().decode("utf-8"))
            models = [
                str(item.get("name", "")).strip()
                for item in payload.get("models", [])
                if str(item.get("name", "")).strip()
            ]
            results["Ollama"] = (
                True,
                f"{len(models)} 个模型" if models else "服务在线",
            )
        except (OSError, ValueError, urllib_error.URLError):
            results["Ollama"] = (False, "未连接")

        resolve_candidates = [
            Path(r"C:\Program Files\Blackmagic Design\DaVinci Resolve\Resolve.exe"),
            Path(
                r"C:\Program Files\Blackmagic Design\DaVinci Resolve Studio\Resolve.exe"
            ),
        ]
        resolve_ready = any(path.is_file() for path in resolve_candidates)
        results["Resolve"] = (resolve_ready, "已安装" if resolve_ready else "未安装")
        self.messages.put(("environment", (results, models)))

    def _apply_environment_result(self, payload: object) -> None:
        """Render dependency checks and installed Ollama models. / 显示依赖检测与本地模型。"""
        results, models = payload  # type: ignore[misc]
        for key, (ready, text) in results.items():
            label = self.status_labels.get(key)
            if label is not None:
                label.configure(text=f"● {text}", fg=ACCENT if ready else ERROR)
        if models:
            self.ollama_combo.configure(values=tuple(models))
            current = self.ollama_model_var.get()
            if current not in models:
                self.ollama_model_var.set(models[0])
        summary = "，".join(
            f"{name}: {'OK' if ready else text}"
            for name, (ready, text) in results.items()
        )
        self._append_log("环境检测 / Environment: " + summary + "\n", "info")

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

    def _save_settings(self, options: WorkflowOptions) -> None:
        """Persist non-secret UI settings under ignored runtime data. / 在已忽略的运行目录保存非敏感设置。"""
        self.settings_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.settings_path.with_suffix(".tmp")
        temporary.write_text(
            json.dumps(asdict(options), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        temporary.replace(self.settings_path)

    def _load_settings(self) -> None:
        """Restore the previous local UI session when available. / 在可用时恢复上次本地界面设置。"""
        if not self.settings_path.is_file():
            return
        try:
            data = json.loads(self.settings_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
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
    project_root = Path(__file__).resolve().parents[1]
    root = tk.Tk()
    CyberEditorApp(root, project_root)
    root.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
