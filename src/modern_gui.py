#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Modern Windows 11 desktop interface for CyberEditor-Agent.
CyberEditor-Agent 的现代 Windows 11 桌面界面。

The long-lived UI never imports PyTorch. It probes CUDA in a disposable child
process and starts ``main.py`` as another child, preserving strict serial
execution and VRAM release.

常驻 UI 不会导入 PyTorch。CUDA 检测在一次性子进程中完成，工作流通过另一个
子进程启动 ``main.py``，从而保持严格串行执行与显存释放。
"""

from __future__ import annotations

import ctypes
from dataclasses import asdict
import json
import os
from pathlib import Path
import queue
import re
import shutil
import subprocess
import sys
import threading
from typing import Dict, List, Optional, Sequence, Tuple
from urllib import error as urllib_error
from urllib import request as urllib_request

import customtkinter as ctk
import tkinter as tk
from tkinter import filedialog, messagebox

from .gui import (
    WorkflowOptions,
    build_runtime_environment,
    detect_hardware,
    detect_media_fps,
    detect_system_theme,
    detect_torch_runtime,
    enable_windows_high_dpi,
    get_primary_work_area,
    parse_frame_rate,
    recommend_automatic_settings,
)
from .ui_i18n import (
    detect_system_language,
    resolve_language,
    translate,
)


APP_TITLE = "CyberEditor Agent"
COLORS = {
    "window": ("#EEF3F9", "#080D16"),
    "card": ("#FFFFFF", "#101827"),
    "card_alt": ("#F5F8FC", "#151F31"),
    "field": ("#F5F7FB", "#0B1220"),
    "border": ("#DDE5EF", "#263248"),
    "text": ("#132033", "#F2F6FC"),
    "muted": ("#61738B", "#8FA2BA"),
    "accent": "#3BCDB5",
    "accent_hover": "#29B8A2",
    "accent_text": "#041411",
    "success": "#36D399",
    "error": "#FF7182",
    "danger": ("#FDECEF", "#3B1D2A"),
    "danger_hover": ("#F9DCE2", "#532538"),
    "console": ("#F6F8FC", "#090F1B"),
}

def apply_windows_11_effects(window: tk.Misc, dark: bool) -> List[str]:
    """
    Apply native dark title bar, rounded corners, and Mica backdrop.
    应用原生深色标题栏、圆角与 Mica 云母背景。

    The native frame remains enabled, preserving Snap Layouts, Alt+Tab,
    taskbar previews, and accessibility.
    保留原生窗口边框，从而继续支持贴靠布局、Alt+Tab、任务栏预览和无障碍能力。
    """
    if os.name != "nt":
        return []
    try:
        window.update_idletasks()
        child = int(window.winfo_id())
        parent = int(ctypes.windll.user32.GetParent(child))
        handle = parent or child
        dwmapi = ctypes.windll.dwmapi
    except (AttributeError, OSError, tk.TclError, ValueError):
        return []
    applied: List[str] = []

    def set_attribute(attribute: int, value: int, name: str) -> None:
        data = ctypes.c_int(value)
        try:
            result = dwmapi.DwmSetWindowAttribute(
                ctypes.c_void_p(handle), ctypes.c_uint(attribute),
                ctypes.byref(data), ctypes.sizeof(data)
            )
            if result == 0:
                applied.append(name)
        except (AttributeError, OSError):
            pass

    set_attribute(20, int(dark), "immersive-dark-titlebar")
    set_attribute(33, 2, "rounded-corners")
    set_attribute(38, 2, "mica-backdrop")
    return applied


class ModernDropdown(ctk.CTkFrame):
    """
    Rounded single-surface selector with a modern popup menu.
    带现代弹出菜单的圆角单一表面选择器。

    CustomTkinter's stock option menu uses a visibly split arrow button. This
    control uses one calm surface, a subtle chevron, and a rounded popover that
    matches the rest of the Windows 11 interface.
    CustomTkinter 原生选项菜单带有明显的分割箭头按钮。本控件改用统一表面、
    轻量下箭头和与 Windows 11 界面一致的圆角浮层。
    """

    def __init__(
        self,
        master: object,
        values: Sequence[str],
        selected: str,
        command: object,
        width: int = 180,
        height: int = 36,
    ) -> None:
        """Create the selector and retain canonical display values. / 创建选择器并保存规范显示值。"""
        super().__init__(
            master,
            width=width,
            height=height,
            fg_color="transparent",
            corner_radius=0,
        )
        self._values = [str(item) for item in values]
        self._value = str(selected)
        self._command = command
        self._popup: Optional[ctk.CTkToplevel] = None
        self._height = int(height)
        self.grid_columnconfigure(0, weight=1)
        self.button = ctk.CTkButton(
            self,
            text=self._value,
            command=self._toggle_popup,
            height=height,
            corner_radius=11,
            anchor="w",
            fg_color=COLORS["field"],
            hover=False,
            border_width=1,
            border_color=COLORS["border"],
            text_color=COLORS["text"],
            font=ctk.CTkFont(size=11),
            cursor="hand2",
        )
        self.button.grid(row=0, column=0, sticky="ew")
        self.chevron = ctk.CTkLabel(
            self,
            text="⌄",
            width=28,
            height=height - 4,
            fg_color=COLORS["field"],
            corner_radius=9,
            text_color=COLORS["muted"],
            font=ctk.CTkFont(size=17, weight="bold"),
            cursor="hand2",
        )
        self.chevron.place(relx=1.0, rely=0.5, x=-7, anchor="e")
        self.chevron.bind("<Button-1>", lambda _event: self._toggle_popup())
        for widget in (self.button, self.chevron):
            widget.bind("<Enter>", self._schedule_hover_refresh, add="+")
            widget.bind("<Leave>", self._schedule_hover_refresh, add="+")

    def get(self) -> str:
        """Return the selected display value. / 返回所选显示值。"""
        return self._value

    def set(self, value: str) -> None:
        """Update the selected value without firing the command. / 更新所选值但不触发回调。"""
        self._value = str(value)
        self.button.configure(text=self._value)

    def set_values(self, values: Sequence[str]) -> None:
        """Replace popup choices while retaining a valid selection. / 替换浮层选项并保留有效选择。"""
        self._values = [str(item) for item in values]
        if self._values and self._value not in self._values:
            self.set(self._values[0])

    def set_state(self, state: str) -> None:
        """Enable or disable the complete selector. / 启用或禁用整个选择器。"""
        self.button.configure(state=state)
        self.chevron.configure(
            text_color=COLORS["muted"] if state != "disabled" else COLORS["border"]
        )

    def _schedule_hover_refresh(self, _event: object = None) -> None:
        """Keep the field and chevron on one hover surface. / 保持输入区和箭头使用同一悬停表面。"""
        self.after_idle(self._refresh_hover_surface)

    def _refresh_hover_surface(self) -> None:
        """Apply hover only when the pointer remains inside the selector. / 仅当指针仍在选择器内时应用悬停色。"""
        pointer_x = self.winfo_pointerx()
        pointer_y = self.winfo_pointery()
        inside = (
            self.winfo_rootx() <= pointer_x < self.winfo_rootx() + self.winfo_width()
            and self.winfo_rooty() <= pointer_y < self.winfo_rooty() + self.winfo_height()
        )
        color = COLORS["card_alt"] if inside else COLORS["field"]
        self.button.configure(fg_color=color)
        self.chevron.configure(fg_color=color)

    def _toggle_popup(self) -> None:
        """Open the popover or close the existing one. / 打开浮层或关闭现有浮层。"""
        if self._popup is not None and self._popup.winfo_exists():
            self._close_popup()
            return
        if not self._values:
            return
        self.update_idletasks()
        popup = ctk.CTkToplevel(self)
        self._popup = popup
        popup.withdraw()
        popup.overrideredirect(True)
        popup.attributes("-topmost", True)
        popup.transient(self.winfo_toplevel())
        popup.configure(fg_color=COLORS["card"])
        popup.grid_columnconfigure(0, weight=1)
        popup.grid_rowconfigure(0, weight=1)

        # ``winfo_width`` and root coordinates are physical pixels after
        # per-monitor DPI awareness is enabled. CTkToplevel.geometry() applies
        # CustomTkinter's window scale to width/height once more, so convert
        # only the size back to logical pixels and keep x/y as screen pixels.
        # 启用逐显示器 DPI 后，winfo_width/屏幕坐标使用物理像素；CTk 会再次缩放
        # geometry 的宽高。因此这里只反算尺寸，x/y 继续使用物理屏幕坐标。
        try:
            window_scale = max(
                1.0,
                float(self.winfo_toplevel()._get_window_scaling()),  # noqa: SLF001
            )
        except (AttributeError, TypeError, ValueError):
            window_scale = 1.0

        screen_width = int(self.winfo_screenwidth())
        screen_height = int(self.winfo_screenheight())
        width = max(170, int(round(self.winfo_width() / window_scale)))
        width = min(width, max(170, int((screen_width - 36) / window_scale)))
        row_height = 38
        visible_rows = min(7, len(self._values))
        height = visible_rows * row_height + 14
        physical_width = int(round(width * window_scale))
        physical_height = int(round(height * window_scale))
        x = int(self.winfo_rootx())
        y = int(self.winfo_rooty() + self.winfo_height() + 6)
        x = min(max(18, x), max(18, screen_width - physical_width - 18))
        if y + physical_height > screen_height - 18:
            y = max(18, int(self.winfo_rooty() - physical_height - 6))
        popup.geometry(f"{width}x{height}+{x}+{y}")

        if len(self._values) > visible_rows:
            surface: ctk.CTkBaseClass = ctk.CTkScrollableFrame(
                popup,
                fg_color=COLORS["card"],
                corner_radius=14,
                border_width=1,
                border_color=COLORS["border"],
                scrollbar_button_color=COLORS["border"],
                scrollbar_button_hover_color=COLORS["accent"],
            )
        else:
            surface = ctk.CTkFrame(
                popup,
                fg_color=COLORS["card"],
                corner_radius=14,
                border_width=1,
                border_color=COLORS["border"],
            )
        surface.grid(row=0, column=0, sticky="nsew")
        surface.grid_columnconfigure(0, weight=1)
        for row, value in enumerate(self._values):
            selected = value == self._value
            item = ctk.CTkButton(
                surface,
                text=("✓  " if selected else "   ") + value,
                command=lambda choice=value: self._select(choice),
                height=34,
                corner_radius=9,
                anchor="w",
                fg_color=("#DDF8F1", "#173B35") if selected else "transparent",
                hover_color=COLORS["card_alt"],
                text_color=COLORS["text"],
                font=ctk.CTkFont(size=11, weight="bold" if selected else "normal"),
            )
            item.grid(row=row, column=0, sticky="ew", padx=7, pady=(7 if row == 0 else 1, 1))
        popup.bind("<Escape>", lambda _event: self._close_popup())
        popup.bind("<FocusOut>", self._on_popup_focus_out)
        popup.deiconify()
        popup.lift()
        popup.after(
            20,
            lambda: (
                apply_windows_11_effects(
                    popup, ctk.get_appearance_mode().casefold() == "dark"
                ),
                popup.focus_force(),
            ),
        )

    def _on_popup_focus_out(self, _event: object) -> None:
        """Close after focus moves outside the popup hierarchy. / 焦点移出浮层层级后关闭。"""
        if self._popup is not None:
            self._popup.after(100, self._close_if_focus_lost)

    def _close_if_focus_lost(self) -> None:
        """Close only when no popup descendant owns focus. / 仅在浮层子控件均无焦点时关闭。"""
        popup = self._popup
        if popup is None or not popup.winfo_exists():
            return
        focused = popup.focus_get()
        if focused is None or not str(focused).startswith(str(popup)):
            self._close_popup()

    def _select(self, value: str) -> None:
        """Commit one choice, close the popover, and notify the owner. / 提交选项、关闭浮层并通知所有者。"""
        self.set(value)
        self._close_popup()
        if callable(self._command):
            self._command(value)

    def _close_popup(self) -> None:
        """Destroy the active popover safely. / 安全销毁当前浮层。"""
        popup, self._popup = self._popup, None
        if popup is not None and popup.winfo_exists():
            popup.destroy()

    def destroy(self) -> None:
        """Close the popover before destroying the selector. / 销毁选择器前先关闭浮层。"""
        self._close_popup()
        super().destroy()


class ModernCyberEditorApp:
    """
    Modern bilingual controller for the strict serial editing workflow.
    面向严格串行剪辑工作流的现代双语控制器。
    """

    def __init__(self, root: ctk.CTk, project_root: Path) -> None:
        """Initialize state, restore settings, and render the UI. / 初始化状态、恢复设置并渲染界面。"""
        self.root = root
        self.project_root = project_root.resolve()
        self.settings_path = self.project_root / "data" / "ui-settings.json"
        self.saved_settings = self._read_settings_data()
        self.theme_mode = str(self.saved_settings.get("theme", "system"))
        if self.theme_mode not in {"system", "dark", "light"}:
            self.theme_mode = "system"
        self.active_theme = (
            detect_system_theme() if self.theme_mode == "system"
            else self.theme_mode
        )
        self.language_mode = str(
            self.saved_settings.get("ui_language", "system")
        )
        if self.language_mode not in {"system", "zh", "en"}:
            self.language_mode = "system"
        self.active_language = resolve_language(self.language_mode)
        ctk.set_appearance_mode(self.active_theme)
        ctk.set_default_color_theme("blue")

        self.process: Optional[subprocess.Popen[str]] = None
        self.active_options: Optional[WorkflowOptions] = None
        self.messages: "queue.Queue[Tuple[str, object]]" = queue.Queue()
        self.stop_requested = False
        self.detected_hardware: Dict[str, object] = {}
        self.available_ollama_models: List[Dict[str, object]] = []
        self.automatic_recommendation: Dict[str, object] = {}
        self.environment_state: Dict[str, Tuple[bool, str]] = {}
        self.status_value_labels: Dict[str, ctk.CTkLabel] = {}
        self.log_history = ""
        self.stage_key = "ready_stage"
        self.failure_code = 0
        self.progress_value = 0.0

        self._create_variables()
        self._configure_window()
        self._build_layout()
        self._apply_flow_rules()
        self._append_log(self.t("ui_ready_log"))
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)
        self.root.after(100, self._drain_messages)
        self.root.after(150, self._refresh_native_effects)
        self.root.after(650, self._start_environment_check)
        self.root.after(900, self._start_initial_fps_detection)
        self.root.after(2500, self._poll_system_preferences)

    def t(self, key: str, **values: object) -> str:
        """Return localized interface text. / 返回本地化界面文本。"""
        return translate(self.active_language, key, **values)

    def _create_variables(self) -> None:
        """Create canonical Tk variables from saved settings. / 根据已保存设置创建规范 Tk 变量。"""
        data = self.saved_settings
        self.video_var = tk.StringVar(value=str(data.get("video", "")))
        self.proxy_var = tk.StringVar(value=str(data.get("proxy", "")))
        self.data_var = tk.StringVar(value=str(data.get("data_dir", "data/ui-run")))
        self.flow_key = str(data.get("flow", "full"))
        if self.flow_key not in {"full", "director", "resolve"}:
            self.flow_key = "full"
        self.profile_key = str(data.get("hardware_profile", "auto"))
        if self.profile_key not in {
            "auto", "conservative", "balanced", "performance", "custom"
        }:
            self.profile_key = "auto"
        self.whisper_var = tk.StringVar(
            value=str(data.get("whisper_model", "small"))
        )
        self.device_var = tk.StringVar(
            value=str(data.get("whisper_device", "auto"))
        )
        self.speech_language_var = tk.StringVar(
            value=str(data.get("language", ""))
        )
        self.ollama_model_var = tk.StringVar(
            value=str(data.get("ollama_model", "qwen2.5:3b"))
        )
        self.ollama_url_var = tk.StringVar(
            value=str(data.get("ollama_url", "http://localhost:11434"))
        )
        self.chunk_var = tk.StringVar(
            value=str(data.get("chunk_minutes", 12.0))
        )
        self.fps_mode = str(data.get("fps_mode", "auto"))
        if self.fps_mode != "auto":
            try:
                restored_fps = parse_frame_rate(self.fps_mode)
            except ValueError:
                self.fps_mode = "auto"
            else:
                self.fps_mode = (
                    f"{restored_fps:.6f}".rstrip("0").rstrip(".")
                )
        # Never present a saved numeric fallback as a fresh media probe. The
        # restored source is probed after first paint; handoff JSON is checked
        # when a run starts. This avoids a misleading “Auto · 25 fps” when no
        # source is selected. / 不把设置中的数值兜底伪装成新鲜素材检测结果：
        # 首帧后重新检测恢复的素材，启动时再检查交接 JSON。
        self.detected_project_fps = 0.0
        self.ctx_var = tk.StringVar(value=str(data.get("num_ctx", 8192)))
        self.timeline_var = tk.StringVar(
            value=str(data.get("timeline_name", "CyberEditor Timeline"))
        )
        self.project_var = tk.StringVar(
            value=str(data.get("project_name", "CyberEditor Project"))
        )
        self.skip_resolve_var = tk.BooleanVar(
            value=bool(data.get("skip_resolve", True))
        )
        self.strict_fps_var = tk.BooleanVar(
            value=bool(data.get("strict_fps", False))
        )

    def _configure_window(self) -> None:
        """Configure a crisp and taskbar-safe main window. / 配置清晰且避开任务栏的主窗口。"""
        self.root.title(APP_TITLE)
        self.root.configure(fg_color=COLORS["window"])
        left, top, area_width, area_height = get_primary_work_area(
            self.root.winfo_screenwidth(), self.root.winfo_screenheight()
        )
        # CustomTkinter already multiplies geometry by its window scaling.
        # Convert the physical Windows work area back to logical pixels before
        # selecting a size; multiplying by DPI here would scale the window twice.
        try:
            window_scale = max(1.0, float(self.root._get_window_scaling()))
        except (AttributeError, TypeError, ValueError):
            window_scale = 1.0
        logical_area_width = area_width / window_scale
        logical_area_height = area_height / window_scale
        width = min(1420, int(logical_area_width * 0.94))
        height = min(900, int(logical_area_height * 0.90))
        physical_width = int(width * window_scale)
        physical_height = int(height * window_scale)
        x = left + max(0, (area_width - physical_width) // 2)
        y = top + max(0, (area_height - physical_height) // 2)
        self.root.geometry(f"{width}x{height}+{x}+{y}")
        self.root.minsize(
            min(width, 1120),
            min(height, 700),
        )
        self.root.grid_rowconfigure(0, weight=1)
        self.root.grid_columnconfigure(0, weight=1)

    def _build_layout(self) -> None:
        """Build the responsive Fluent-inspired layout. / 构建响应式 Fluent 风格界面。"""
        old_shell = getattr(self, "shell", None)
        if old_shell is not None:
            old_shell.destroy()
        self.status_value_labels = {}
        self.shell = ctk.CTkFrame(
            self.root, fg_color="transparent", corner_radius=0
        )
        self.shell.grid(row=0, column=0, sticky="nsew", padx=26, pady=(18, 24))
        self.shell.grid_columnconfigure(0, weight=1)
        self.shell.grid_rowconfigure(2, weight=1)
        self._build_header(self.shell)
        self._build_status_strip(self.shell)

        content = ctk.CTkFrame(self.shell, fg_color="transparent")
        content.grid(row=2, column=0, sticky="nsew", pady=(16, 0))
        content.grid_columnconfigure(0, weight=7, uniform="content")
        content.grid_columnconfigure(1, weight=5, uniform="content")
        content.grid_rowconfigure(0, weight=1)
        form = ctk.CTkScrollableFrame(
            content,
            fg_color=COLORS["card"],
            border_width=1,
            border_color=COLORS["border"],
            corner_radius=22,
            scrollbar_button_color=COLORS["border"],
            scrollbar_button_hover_color=COLORS["accent"],
        )
        form.grid(row=0, column=0, sticky="nsew", padx=(0, 8))
        form.grid_columnconfigure(0, weight=1)
        self._build_form(form)
        run_card = ctk.CTkFrame(
            content,
            fg_color=COLORS["card"],
            border_width=1,
            border_color=COLORS["border"],
            corner_radius=22,
        )
        run_card.grid(row=0, column=1, sticky="nsew", padx=(8, 0))
        run_card.grid_columnconfigure(0, weight=1)
        run_card.grid_rowconfigure(4, weight=1)
        self._build_run_center(run_card)

    def _build_header(self, parent: ctk.CTkFrame) -> None:
        """Build branding, language, theme, and output actions. / 构建品牌、语言、主题与输出操作区。"""
        header = ctk.CTkFrame(parent, fg_color="transparent")
        header.grid(row=0, column=0, sticky="ew")
        header.grid_columnconfigure(1, weight=1)
        mark = ctk.CTkFrame(
            header, width=52, height=52, corner_radius=17,
            fg_color=COLORS["accent"]
        )
        mark.grid(row=0, column=0, rowspan=2, padx=(0, 14))
        mark.grid_propagate(False)
        ctk.CTkLabel(
            mark, text="C//", text_color=COLORS["accent_text"],
            font=ctk.CTkFont("Segoe UI Variable Display", 17, "bold")
        ).place(relx=0.5, rely=0.5, anchor="center")
        ctk.CTkLabel(
            header, text=APP_TITLE, text_color=COLORS["text"],
            font=ctk.CTkFont("Segoe UI Variable Display", 24, "bold")
        ).grid(row=0, column=1, sticky="sw")
        ctk.CTkLabel(
            header, text=self.t("tagline"), text_color=COLORS["muted"],
            font=ctk.CTkFont("Segoe UI Variable Text", 11, "bold")
        ).grid(row=1, column=1, sticky="nw")
        ctk.CTkLabel(
            header, text=self.t("window_effect"), text_color=COLORS["muted"],
            fg_color=COLORS["card_alt"], corner_radius=13, height=28,
            font=ctk.CTkFont(size=10)
        ).grid(row=0, column=2, rowspan=2, padx=8)

        language_values = [
            self.t("language_system"), self.t("language_zh"),
            self.t("language_en")
        ]
        language_map = dict(
            zip(("system", "zh", "en"), language_values)
        )
        self.language_menu = self._top_menu(
            header, language_values, language_map[self.language_mode],
            self._on_language_change, 3, 118
        )
        theme_values = [
            self.t("theme_system"), self.t("theme_dark"),
            self.t("theme_light")
        ]
        theme_map = dict(zip(("system", "dark", "light"), theme_values))
        self.theme_menu = self._top_menu(
            header, theme_values, theme_map[self.theme_mode],
            self._on_theme_change, 4, 124
        )
        ctk.CTkButton(
            header, text=self.t("open_output"), command=self._open_output,
            width=112, height=34, corner_radius=10,
            fg_color=COLORS["card_alt"], hover_color=COLORS["border"],
            border_width=1, border_color=COLORS["border"],
            text_color=COLORS["text"]
        ).grid(row=0, column=5, rowspan=2, padx=(4, 0))

    def _top_menu(
        self, parent: ctk.CTkFrame, values: List[str], selected: str,
        command: object, column: int, width: int
    ) -> ModernDropdown:
        """Create one compact header option menu. / 创建一个紧凑的页眉选项菜单。"""
        menu = ModernDropdown(
            parent,
            values=values,
            selected=selected,
            command=command,
            width=width,
            height=34,
        )
        menu.grid(row=0, column=column, rowspan=2, padx=4)
        return menu

    def _build_status_strip(self, parent: ctk.CTkFrame) -> None:
        """Build dependency and real CUDA status cards. / 构建依赖与真实 CUDA 状态卡片。"""
        strip = ctk.CTkFrame(parent, fg_color="transparent")
        strip.grid(row=1, column=0, sticky="ew", pady=(17, 0))
        keys = ("Python", "FFmpeg", "CUDA", "Ollama", "Resolve")
        for column, key in enumerate(keys):
            strip.grid_columnconfigure(column, weight=1, uniform="health")
            card = ctk.CTkFrame(
                strip, height=58, corner_radius=16,
                fg_color=COLORS["card_alt"], border_width=1,
                border_color=COLORS["border"]
            )
            card.grid(
                row=0, column=column, sticky="ew",
                padx=(0 if column == 0 else 5, 0 if column == 4 else 5)
            )
            card.grid_propagate(False)
            ctk.CTkLabel(
                card, text=self.t(key.casefold()), text_color=COLORS["muted"],
                font=ctk.CTkFont(size=9, weight="bold")
            ).pack(anchor="w", padx=13, pady=(7, 0))
            value = ctk.CTkLabel(
                card, text="●  " + self.t("detecting"),
                text_color=COLORS["muted"],
                font=ctk.CTkFont(size=11, weight="bold")
            )
            value.pack(anchor="w", padx=13, pady=(0, 7))
            self.status_value_labels[key] = value
        self._render_environment_state()

    def _build_form(self, parent: ctk.CTkScrollableFrame) -> None:
        """Build all workflow configuration controls. / 构建全部工作流配置控件。"""
        self._section_title(parent, 0, self.t("project_media"))
        flow_values = [
            self.t("flow_full"), self.t("flow_director"),
            self.t("flow_resolve")
        ]
        self._flow_display_to_key = dict(
            zip(flow_values, ("full", "director", "resolve"))
        )
        self.flow_menu = self._option_field(
            parent, 1, self.t("workflow_mode"), flow_values,
            next(k for k, v in self._flow_display_to_key.items()
                 if v == self.flow_key),
            self._on_flow_change
        )
        self._path_field(
            parent, 2, self.t("source_video"), self.video_var,
            self._choose_video, self.t("browse")
        )
        self._path_field(
            parent, 3, self.t("proxy_media"), self.proxy_var,
            self._choose_proxy, self.t("browse")
        )
        self._path_field(
            parent, 4, self.t("runtime_data"), self.data_var,
            self._choose_data_dir, self.t("select_folder")
        )
        fps_row = ctk.CTkFrame(parent, fg_color="transparent")
        fps_row.grid(row=5, column=0, sticky="ew", pady=(4, 12))
        fps_row.grid_columnconfigure(0, weight=1)
        auto_fps_display = self._fps_auto_display()
        fps_values = [
            auto_fps_display, "23.976", "24", "25", "29.97",
            "30", "50", "59.94", "60",
        ]
        if self.fps_mode != "auto" and self.fps_mode not in fps_values:
            fps_values.append(self.fps_mode)
        self._fps_display_to_mode = {
            value: ("auto" if index == 0 else value)
            for index, value in enumerate(fps_values)
        }
        selected_fps = (
            auto_fps_display if self.fps_mode == "auto" else self.fps_mode
        )
        self.fps_menu = self._option_field(
            fps_row,
            0,
            self.t("project_fps"),
            fps_values,
            selected_fps,
            self._on_fps_change,
        )

        self._section_title(parent, 6, self.t("ai_hardware"))
        profile_values = [
            self.t("profile_auto"), self.t("profile_conservative"),
            self.t("profile_balanced"), self.t("profile_performance"),
            self.t("profile_custom")
        ]
        self._profile_display_to_key = dict(zip(
            profile_values,
            ("auto", "conservative", "balanced", "performance", "custom")
        ))
        self.profile_menu = self._option_field(
            parent, 7, self.t("hardware_profile"), profile_values,
            next(k for k, v in self._profile_display_to_key.items()
                 if v == self.profile_key),
            self._on_profile_change
        )
        self.hardware_label = ctk.CTkLabel(
            parent, text=self._hardware_description(), anchor="w",
            justify="left", wraplength=760, height=52, corner_radius=13,
            fg_color=COLORS["card_alt"], text_color=COLORS["muted"],
            font=ctk.CTkFont(size=11)
        )
        self.hardware_label.grid(row=8, column=0, sticky="ew", pady=(3, 9))
        ai = ctk.CTkFrame(parent, fg_color="transparent")
        ai.grid(row=9, column=0, sticky="ew")
        ai.grid_columnconfigure((0, 1), weight=1, uniform="ai")
        self.whisper_menu = self._option_field(
            ai, 0, self.t("whisper_model"),
            ["tiny", "base", "small", "medium", "turbo"],
            self.whisper_var.get(), self.whisper_var.set, column=0
        )
        self.device_menu = self._option_field(
            ai, 0, self.t("whisper_device"), ["auto", "cuda", "cpu"],
            self.device_var.get(), self.device_var.set, column=1
        )
        self._entry_field(
            ai, 1, self.t("source_language"),
            self.speech_language_var, column=0
        )
        self.ollama_menu = self._option_field(
            ai, 1, self.t("ollama_model"), [self.ollama_model_var.get()],
            self.ollama_model_var.get(), self.ollama_model_var.set, column=1
        )
        self._entry_field(
            ai, 2, self.t("ollama_context"), self.ctx_var, column=0
        )
        self._entry_field(
            ai, 2, self.t("chunk_minutes"), self.chunk_var, column=1
        )
        self._entry_field(
            ai, 3, self.t("ollama_url"), self.ollama_url_var,
            column=0, columnspan=2
        )

        self._section_title(parent, 10, self.t("resolve_settings"))
        resolve = ctk.CTkFrame(parent, fg_color="transparent")
        resolve.grid(row=11, column=0, sticky="ew")
        resolve.grid_columnconfigure((0, 1), weight=1, uniform="resolve")
        self._entry_field(
            resolve, 0, self.t("timeline_name"), self.timeline_var, column=0
        )
        self._entry_field(
            resolve, 0, self.t("project_name"), self.project_var, column=1
        )
        switches = ctk.CTkFrame(parent, fg_color="transparent")
        switches.grid(row=12, column=0, sticky="ew", pady=(7, 14))
        switches.grid_columnconfigure((0, 1), weight=1)
        self.skip_resolve_switch = ctk.CTkSwitch(
            switches, text=self.t("skip_resolve"),
            variable=self.skip_resolve_var, progress_color=COLORS["accent"],
            button_hover_color=COLORS["accent_hover"],
            text_color=COLORS["text"]
        )
        self.skip_resolve_switch.grid(row=0, column=0, sticky="w")
        ctk.CTkSwitch(
            switches, text=self.t("strict_fps"),
            variable=self.strict_fps_var, progress_color=COLORS["accent"],
            button_hover_color=COLORS["accent_hover"],
            text_color=COLORS["text"]
        ).grid(row=0, column=1, sticky="w")
        if self.available_ollama_models:
            self.ollama_menu.set_values([
                str(item["name"]) for item in self.available_ollama_models
            ])

    def _build_run_center(self, parent: ctk.CTkFrame) -> None:
        """Build progress, live log, and task actions. / 构建进度、实时日志与任务操作。"""
        top = ctk.CTkFrame(parent, fg_color="transparent")
        top.grid(row=0, column=0, sticky="ew", padx=20, pady=(20, 0))
        top.grid_columnconfigure(0, weight=1)
        ctk.CTkLabel(
            top, text=self.t("run_center"), text_color=COLORS["text"],
            font=ctk.CTkFont(size=19, weight="bold")
        ).grid(row=0, column=0, sticky="w")
        ctk.CTkLabel(
            top, text=self.t("serial_badge"),
            fg_color=("#DDF8F1", "#153A34"),
            text_color=("#08705E", "#58DDC5"),
            corner_radius=12, height=25,
            font=ctk.CTkFont(size=9, weight="bold")
        ).grid(row=0, column=1, sticky="e")
        self.stage_label = ctk.CTkLabel(
            parent, text=self._stage_text(), anchor="w",
            text_color=COLORS["text"],
            font=ctk.CTkFont(size=17, weight="bold")
        )
        self.stage_label.grid(row=1, column=0, sticky="ew", padx=20, pady=(18, 0))
        ctk.CTkLabel(
            parent, text=self.t("ready_hint"), anchor="w", justify="left",
            wraplength=560, text_color=COLORS["muted"],
            font=ctk.CTkFont(size=11)
        ).grid(row=2, column=0, sticky="ew", padx=20, pady=(3, 9))
        self.progress = ctk.CTkProgressBar(
            parent, height=7, corner_radius=4,
            fg_color=COLORS["card_alt"], progress_color=COLORS["accent"]
        )
        self.progress.grid(row=3, column=0, sticky="ew", padx=20)
        self.progress.set(self.progress_value)

        console = ctk.CTkFrame(
            parent, fg_color=COLORS["console"], corner_radius=16,
            border_width=1, border_color=COLORS["border"]
        )
        console.grid(row=4, column=0, sticky="nsew", padx=20, pady=(16, 12))
        console.grid_columnconfigure(0, weight=1)
        console.grid_rowconfigure(1, weight=1)
        ctk.CTkLabel(
            console, text=self.t("live_log").upper(),
            text_color=COLORS["muted"],
            font=ctk.CTkFont(size=9, weight="bold")
        ).grid(row=0, column=0, sticky="w", padx=14, pady=(10, 3))
        self.log_text = ctk.CTkTextbox(
            console, fg_color="transparent", text_color=COLORS["text"],
            border_width=0, wrap="word",
            font=ctk.CTkFont("Cascadia Mono", 10)
        )
        self.log_text.grid(row=1, column=0, sticky="nsew", padx=8, pady=(0, 8))
        if self.log_history:
            self.log_text.insert("1.0", self.log_history)
            self.log_text.see("end")

        actions = ctk.CTkFrame(parent, fg_color="transparent")
        actions.grid(row=5, column=0, sticky="ew", padx=20, pady=(0, 12))
        actions.grid_columnconfigure(0, weight=1)
        self.start_button = ctk.CTkButton(
            actions, text=self.t("start"), command=self._start_workflow,
            height=46, corner_radius=14, fg_color=COLORS["accent"],
            hover_color=COLORS["accent_hover"],
            text_color=COLORS["accent_text"],
            font=ctk.CTkFont(size=13, weight="bold")
        )
        self.start_button.grid(row=0, column=0, sticky="ew", padx=(0, 7))
        self.stop_button = ctk.CTkButton(
            actions, text=self.t("stop"), command=self._stop_workflow,
            width=90, height=46, corner_radius=14,
            fg_color=COLORS["danger"], hover_color=COLORS["danger_hover"],
            text_color=COLORS["error"],
            font=ctk.CTkFont(size=12, weight="bold")
        )
        self.stop_button.grid(row=0, column=1)
        if self.process is None:
            self.stop_button.configure(state="disabled")
        else:
            self.start_button.configure(state="disabled")

        utilities = ctk.CTkFrame(parent, fg_color="transparent")
        utilities.grid(row=6, column=0, sticky="ew", padx=20, pady=(0, 18))
        utilities.grid_columnconfigure((0, 1), weight=1, uniform="utility")
        for column, (text, command) in enumerate((
            (self.t("view_timeline"), self._open_timeline),
            (self.t("recheck"), self._start_environment_check),
        )):
            ctk.CTkButton(
                utilities, text=text, command=command, height=35,
                corner_radius=11, fg_color=COLORS["card_alt"],
                hover_color=COLORS["border"], border_width=1,
                border_color=COLORS["border"], text_color=COLORS["text"]
            ).grid(
                row=0, column=column, sticky="ew",
                padx=(0, 5) if column == 0 else (5, 0)
            )

    def _section_title(
        self, parent: ctk.CTkBaseClass, row: int, text: str
    ) -> None:
        """Create a section heading. / 创建分区标题。"""
        ctk.CTkLabel(
            parent, text=text, anchor="w", text_color=COLORS["text"],
            font=ctk.CTkFont(size=16, weight="bold")
        ).grid(row=row, column=0, sticky="ew", pady=(10 if row else 4, 8))

    def _path_field(
        self, parent: ctk.CTkBaseClass, row: int, label: str,
        variable: tk.StringVar, command: object, button_text: str
    ) -> None:
        """Create a path entry with browse action. / 创建带浏览操作的路径输入框。"""
        frame = ctk.CTkFrame(parent, fg_color="transparent")
        frame.grid(row=row, column=0, sticky="ew", pady=4)
        frame.grid_columnconfigure(0, weight=1)
        ctk.CTkLabel(
            frame, text=label, anchor="w", text_color=COLORS["muted"],
            font=ctk.CTkFont(size=10)
        ).grid(row=0, column=0, columnspan=2, sticky="ew", pady=(0, 3))
        ctk.CTkEntry(
            frame, textvariable=variable, height=36, corner_radius=10,
            fg_color=COLORS["field"], border_color=COLORS["border"],
            text_color=COLORS["text"]
        ).grid(row=1, column=0, sticky="ew", padx=(0, 7))
        ctk.CTkButton(
            frame, text=button_text, command=command, width=72, height=36,
            corner_radius=10, fg_color=COLORS["card_alt"],
            hover_color=COLORS["border"], border_width=1,
            border_color=COLORS["border"], text_color=COLORS["text"]
        ).grid(row=1, column=1)

    def _entry_field(
        self, parent: ctk.CTkBaseClass, row: int, label: str,
        variable: tk.Variable, column: int = 0, columnspan: int = 1
    ) -> ctk.CTkEntry:
        """Create one labeled text entry. / 创建一个带标签的文本输入框。"""
        frame = ctk.CTkFrame(parent, fg_color="transparent")
        frame.grid(
            row=row, column=column, columnspan=columnspan, sticky="ew",
            padx=(0, 6) if column == 0 and columnspan == 1
            else ((6, 0) if column else (0, 0)),
            pady=4
        )
        frame.grid_columnconfigure(0, weight=1)
        ctk.CTkLabel(
            frame, text=label, anchor="w", text_color=COLORS["muted"],
            font=ctk.CTkFont(size=10)
        ).grid(row=0, column=0, sticky="ew", pady=(0, 3))
        entry = ctk.CTkEntry(
            frame, textvariable=variable, height=36, corner_radius=10,
            fg_color=COLORS["field"], border_color=COLORS["border"],
            text_color=COLORS["text"]
        )
        entry.grid(row=1, column=0, sticky="ew")
        return entry

    def _option_field(
        self, parent: ctk.CTkBaseClass, row: int, label: str,
        values: List[str], selected: str, command: object,
        column: int = 0, columnspan: int = 1
    ) -> ModernDropdown:
        """Create one labeled option menu. / 创建一个带标签的选项菜单。"""
        frame = ctk.CTkFrame(parent, fg_color="transparent")
        frame.grid(
            row=row, column=column, columnspan=columnspan, sticky="ew",
            padx=(0, 6) if column == 0 and columnspan == 1
            else ((6, 0) if column else (0, 0)),
            pady=4
        )
        frame.grid_columnconfigure(0, weight=1)
        ctk.CTkLabel(
            frame, text=label, anchor="w", text_color=COLORS["muted"],
            font=ctk.CTkFont(size=10)
        ).grid(row=0, column=0, sticky="ew", pady=(0, 3))
        menu = ModernDropdown(
            frame,
            values=values,
            selected=selected,
            command=command,
            height=36,
        )
        menu.grid(row=1, column=0, sticky="ew")
        return menu

    def _on_flow_change(self, display_value: str) -> None:
        """Apply a localized workflow selection. / 应用本地化显示的工作流选择。"""
        self.flow_key = self._flow_display_to_key.get(display_value, "full")
        self._apply_flow_rules()
        self._save_current_preferences()

    def _apply_flow_rules(self) -> None:
        """Keep Resolve-only mode internally consistent. / 保持仅 Resolve 模式的内部状态一致。"""
        if self.flow_key == "resolve":
            self.skip_resolve_var.set(False)
            if hasattr(self, "skip_resolve_switch"):
                self.skip_resolve_switch.configure(state="disabled")
        elif hasattr(self, "skip_resolve_switch"):
            self.skip_resolve_switch.configure(state="normal")

    def _fps_auto_display(self) -> str:
        """Format the automatic FPS choice with its detected value. / 格式化带检测值的自动 FPS 选项。"""
        if self.detected_project_fps > 0:
            return self.t(
                "fps_auto_detected",
                fps=self._format_fps(self.detected_project_fps),
            )
        return self.t("fps_auto")

    @staticmethod
    def _format_fps(fps: float) -> str:
        """Display common integer/NTSC frame rates without noise. / 无多余小数地显示常见整数/NTSC 帧率。"""
        standards = (
            (23.976, "23.976"), (24.0, "24"), (25.0, "25"),
            (29.97, "29.97"), (30.0, "30"), (50.0, "50"),
            (59.94, "59.94"), (60.0, "60"),
        )
        for expected, label in standards:
            if abs(float(fps) - expected) < 0.01:
                return label
        return f"{float(fps):.3f}".rstrip("0").rstrip(".")

    def _on_fps_change(self, display_value: str) -> None:
        """Select automatic source FPS or an explicit timeline rate. / 选择自动源素材 FPS 或明确时间线帧率。"""
        self.fps_mode = self._fps_display_to_mode.get(
            display_value, display_value
        )
        if self.fps_mode == "auto":
            candidate = (
                self.video_var.get().strip()
                or self.proxy_var.get().strip()
            )
            if candidate:
                self._start_fps_detection(candidate)
        self._save_current_preferences()

    def _start_initial_fps_detection(self) -> None:
        """Detect FPS for a restored media path after first paint. / 首帧显示后检测已恢复素材路径的 FPS。"""
        if self.fps_mode != "auto":
            return
        candidate = (
            self.video_var.get().strip() or self.proxy_var.get().strip()
        )
        if candidate and self._absolute_path(candidate).is_file():
            self._start_fps_detection(candidate)

    def _start_fps_detection(self, source: str) -> None:
        """Probe media FPS in a disposable background process. / 在一次性后台进程中检测素材 FPS。"""
        if self.fps_mode == "auto":
            self.detected_project_fps = 0.0
            if hasattr(self, "fps_menu"):
                self.fps_menu.set(self.t("fps_auto"))

        def worker() -> None:
            try:
                fps = detect_media_fps(self._absolute_path(source))
                self.messages.put(("fps", (fps, source)))
            except ValueError:
                self.messages.put(("fps_error", self.t("fps_detection_failed")))

        threading.Thread(target=worker, daemon=True).start()

    def _apply_detected_fps(self, fps: float, source: str) -> None:
        """Accept a non-stale source FPS result and refresh the selector. / 接受未过期的源 FPS 结果并刷新选择器。"""
        current_candidates = {
            str(self._absolute_path(value))
            for value in (
                self.video_var.get().strip(),
                self.proxy_var.get().strip(),
            )
            if value
        }
        if str(self._absolute_path(source)) not in current_candidates:
            return
        self.detected_project_fps = parse_frame_rate(fps)
        if self.fps_mode == "auto" and hasattr(self, "fps_menu"):
            display = self._fps_auto_display()
            manual_values = [
                "23.976", "24", "25", "29.97", "30", "50", "59.94", "60"
            ]
            values = [display] + manual_values
            self._fps_display_to_mode = {
                value: ("auto" if index == 0 else value)
                for index, value in enumerate(values)
            }
            self.fps_menu.set_values(values)
            self.fps_menu.set(display)
        self._append_log(
            self.t(
                "fps_detected_log",
                fps=self._format_fps(self.detected_project_fps),
            ) + "\n"
        )
        self._save_current_preferences()

    def _resolve_project_fps(self, required: bool) -> float:
        """
        Resolve manual, media, or handoff-artifact FPS.
        解析手动、素材或交接产物中的 FPS。

        Parameters / 参数:
            required:
                Raise a friendly error instead of using an internal save-only
                fallback. / 检测失败时抛出友好错误，而非使用仅保存设置的内部兜底。
        """
        if self.fps_mode != "auto":
            return parse_frame_rate(self.fps_mode)
        if not required:
            return self.detected_project_fps or 25.0

        for value in (
            self.video_var.get().strip(),
            self.proxy_var.get().strip(),
        ):
            if not value:
                continue
            path = self._absolute_path(value)
            if not path.is_file():
                continue
            try:
                fps = detect_media_fps(path)
            except ValueError:
                continue
            self._apply_detected_fps(fps, str(path))
            return fps

        data_dir = self._absolute_path(
            self.data_var.get().strip() or "data/ui-run"
        )
        artifact_candidates = (
            (data_dir / "timeline_cuts.json", ("project_fps",)),
            (data_dir / "raw_data.json", ("video", "fps")),
        )
        for path, keys in artifact_candidates:
            if not path.is_file():
                continue
            try:
                value: object = json.loads(path.read_text(encoding="utf-8"))
                for key in keys:
                    value = value[key]  # type: ignore[index]
                fps = parse_frame_rate(value)
            except (OSError, ValueError, KeyError, IndexError, TypeError):
                continue
            self.detected_project_fps = fps
            if hasattr(self, "fps_menu"):
                self.fps_menu.set(self._fps_auto_display())
            return fps
        if self.detected_project_fps > 0:
            return self.detected_project_fps
        raise ValueError(self.t("fps_detection_failed"))

    def _on_profile_change(self, display_value: str) -> None:
        """Apply and save the selected hardware profile. / 应用并保存选定的硬件配置。"""
        self.profile_key = self._profile_display_to_key.get(
            display_value, "auto"
        )
        self._apply_hardware_profile(self.profile_key)
        self._save_current_preferences()

    def _apply_hardware_profile(self, profile: str) -> None:
        """
        Apply safe automatic or named serial-workflow settings.
        应用安全的自动或命名串行工作流参数。

        FPS is intentionally untouched because it is a media/project property,
        not a hardware performance setting.
        FPS 不会被修改，因为它属于素材/工程属性，而不是硬件性能设置。
        """
        if profile == "custom":
            self._set_hardware_text(
                self._hardware_description(self.t("custom_settings"))
            )
            return
        if profile == "auto":
            if not self.automatic_recommendation:
                self._set_hardware_text(self.t("detecting_hardware"))
                return
            settings = self.automatic_recommendation
            label = self.t("auto_settings")
        else:
            presets: Dict[str, Dict[str, object]] = {
                "conservative": {
                    "whisper_model": "base", "whisper_device": "auto",
                    "chunk_minutes": 10.0, "num_ctx": 4096,
                },
                "balanced": {
                    "whisper_model": "small", "whisper_device": "auto",
                    "chunk_minutes": 12.0, "num_ctx": 8192,
                },
                "performance": {
                    "whisper_model": "turbo", "whisper_device": "auto",
                    "chunk_minutes": 15.0, "num_ctx": 16384,
                },
            }
            settings = presets.get(profile, presets["balanced"])
            label = self.t("profile_" + profile)
        self.whisper_var.set(str(settings["whisper_model"]))
        self.device_var.set(str(settings["whisper_device"]))
        self.chunk_var.set(str(settings["chunk_minutes"]))
        self.ctx_var.set(str(settings["num_ctx"]))
        model = str(settings.get("ollama_model", "")).strip()
        if model:
            self.ollama_model_var.set(model)
        if hasattr(self, "whisper_menu"):
            self.whisper_menu.set(self.whisper_var.get())
        if hasattr(self, "device_menu"):
            self.device_menu.set(self.device_var.get())
        if model and hasattr(self, "ollama_menu"):
            self.ollama_menu.set(model)
        details = (
            f"{label}: Whisper {self.whisper_var.get()}  ·  "
            f"Context {self.ctx_var.get()}  ·  Chunk {self.chunk_var.get()}m"
        )
        if model:
            details += f"  ·  {model}"
        self._set_hardware_text(self._hardware_description(details))

    def _hardware_description(self, suffix: str = "") -> str:
        """Format hardware and genuine PyTorch CUDA status. / 格式化硬件与真实 PyTorch CUDA 状态。"""
        if not self.detected_hardware:
            return suffix or self.t("detecting_hardware")
        ram = float(self.detected_hardware.get("ram_gb") or 0)
        threads = int(self.detected_hardware.get("cpu_threads") or 1)
        gpu = str(self.detected_hardware.get("gpu") or "Unknown GPU")
        gpu = gpu.replace(" with Max-Q Design", " Max-Q").replace("NVIDIA ", "")
        vram = float(self.detected_hardware.get("vram_gb") or 0)
        gpu_text = f"{gpu} {vram:g} GB" if vram else gpu
        torch_mode = (
            "PyTorch CUDA"
            if bool(self.detected_hardware.get("torch_cuda"))
            else "PyTorch CPU"
        )
        prefix = (
            f"{gpu_text}  ·  RAM {ram:g} GB  ·  "
            f"CPU {threads}T  ·  {torch_mode}"
        )
        return f"{prefix}\n↳ {suffix}" if suffix else prefix

    def _set_hardware_text(self, text: str) -> None:
        """Update the summary widget when available. / 在摘要控件存在时更新文字。"""
        if hasattr(self, "hardware_label"):
            self.hardware_label.configure(text=text)

    def _on_theme_change(self, display_value: str) -> None:
        """Apply and persist a localized theme selection. / 应用并保存本地化主题选择。"""
        mapping = {
            self.t("theme_system"): "system",
            self.t("theme_dark"): "dark",
            self.t("theme_light"): "light",
        }
        self.theme_mode = mapping.get(display_value, "system")
        self.active_theme = (
            detect_system_theme() if self.theme_mode == "system"
            else self.theme_mode
        )
        ctk.set_appearance_mode(self.active_theme)
        self.root.configure(fg_color=COLORS["window"])
        self._refresh_native_effects()
        self._save_current_preferences()

    def _on_language_change(self, display_value: str) -> None:
        """Switch Chinese/English immediately and save the mode. / 立即切换中英文并保存模式。"""
        mapping = {
            self.t("language_system"): "system",
            self.t("language_zh"): "zh",
            self.t("language_en"): "en",
        }
        self.language_mode = mapping.get(display_value, "system")
        self.active_language = resolve_language(self.language_mode)
        self._build_layout()
        self._apply_flow_rules()
        self._save_current_preferences()
        self.root.after(50, self._refresh_native_effects)

    def _refresh_native_effects(self) -> None:
        """Refresh title-bar, rounded-corner, and Mica attributes. / 刷新标题栏、圆角与 Mica 属性。"""
        self.native_effects = apply_windows_11_effects(
            self.root, self.active_theme == "dark"
        )

    def _poll_system_preferences(self) -> None:
        """Track Windows theme/language while following System. / 在跟随系统时监测 Windows 主题与语言。"""
        rebuild = False
        if self.theme_mode == "system":
            actual_theme = detect_system_theme()
            if actual_theme != self.active_theme:
                self.active_theme = actual_theme
                ctk.set_appearance_mode(actual_theme)
                self._refresh_native_effects()
        if self.language_mode == "system":
            actual_language = detect_system_language()
            if actual_language != self.active_language:
                self.active_language = actual_language
                rebuild = True
        if rebuild:
            self._build_layout()
            self._apply_flow_rules()
        self.root.after(2500, self._poll_system_preferences)

    def _collect_options(self, require_fps: bool = True) -> WorkflowOptions:
        """Normalize current UI values into workflow options. / 将当前界面值规范化为工作流选项。"""
        project_fps = self._resolve_project_fps(required=require_fps)
        return WorkflowOptions(
            video=self.video_var.get().strip(),
            proxy=self.proxy_var.get().strip(),
            data_dir=self.data_var.get().strip() or "data/ui-run",
            flow=self.flow_key,
            hardware_profile=self.profile_key,
            theme=self.theme_mode,
            ui_language=self.language_mode,
            fps_mode=self.fps_mode,
            whisper_model=self.whisper_var.get().strip(),
            whisper_device=self.device_var.get().strip(),
            language=self.speech_language_var.get().strip(),
            ollama_model=self.ollama_model_var.get().strip(),
            ollama_url=self.ollama_url_var.get().strip(),
            chunk_minutes=float(self.chunk_var.get()),
            project_fps=project_fps,
            num_ctx=int(self.ctx_var.get()),
            timeline_name=(
                self.timeline_var.get().strip() or "CyberEditor Timeline"
            ),
            project_name=(
                self.project_var.get().strip() or "CyberEditor Project"
            ),
            skip_resolve=bool(self.skip_resolve_var.get()),
            strict_fps=bool(self.strict_fps_var.get()),
        )

    def _start_environment_check(self) -> None:
        """Start non-blocking dependency and CUDA detection. / 启动非阻塞依赖与 CUDA 检测。"""
        for label in self.status_value_labels.values():
            label.configure(
                text="●  " + self.t("detecting"),
                text_color=COLORS["muted"],
            )
        url = self.ollama_url_var.get().strip()
        threading.Thread(
            target=self._check_environment, args=(url,), daemon=True
        ).start()

    def _check_environment(self, ollama_url: str) -> None:
        """
        Detect services, software, hardware, and real PyTorch CUDA.
        检测服务、软件、硬件与真实 PyTorch CUDA。

        The PyTorch probe exits before any workflow begins, so no CUDA context
        remains resident in the UI process.
        PyTorch 检测会在工作流开始前退出，因此 UI 进程不会保留 CUDA 上下文。
        """
        environment = build_runtime_environment()
        results: Dict[str, Tuple[bool, str]] = {
            "Python": (
                sys.version_info >= (3, 10),
                f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}",
            )
        }
        ffmpeg = shutil.which("ffmpeg", path=environment.get("PATH"))
        results["FFmpeg"] = (
            bool(ffmpeg), self.t("ready") if ffmpeg else self.t("not_found")
        )
        models: List[Dict[str, object]] = []
        try:
            request = urllib_request.Request(
                ollama_url.rstrip("/") + "/api/tags",
                headers={"Accept": "application/json"},
            )
            with urllib_request.urlopen(request, timeout=3) as response:
                payload = json.loads(response.read().decode("utf-8"))
            for item in payload.get("models", []):
                if isinstance(item, dict) and str(item.get("name", "")).strip():
                    models.append({
                        "name": str(item["name"]).strip(),
                        "size": int(item.get("size") or 0),
                    })
            results["Ollama"] = (
                True,
                self.t("models_count", count=len(models))
                if models else self.t("online"),
            )
        except (OSError, ValueError, urllib_error.URLError):
            results["Ollama"] = (False, self.t("not_connected"))

        resolve_paths = [
            Path(r"C:\Program Files\Blackmagic Design\DaVinci Resolve\Resolve.exe"),
            Path(r"C:\Program Files\Blackmagic Design\DaVinci Resolve Studio\Resolve.exe"),
        ]
        resolve_ready = any(path.is_file() for path in resolve_paths)
        results["Resolve"] = (
            resolve_ready,
            self.t("installed") if resolve_ready else self.t("not_found"),
        )
        hardware = detect_hardware()
        torch_runtime = detect_torch_runtime()
        hardware.update(torch_runtime)
        version = str(torch_runtime.get("torch_version") or "PyTorch")
        if bool(torch_runtime.get("torch_cuda")):
            results["CUDA"] = (
                True, self.t("gpu_cuda_ready", version=version)
            )
        elif bool(torch_runtime.get("torch_available")):
            results["CUDA"] = (
                False, self.t("gpu_cpu_only", version=version)
            )
        else:
            results["CUDA"] = (False, self.t("not_installed"))
        recommendation = recommend_automatic_settings(hardware, models)
        self.messages.put(
            ("environment", (results, models, hardware, recommendation))
        )

    def _apply_environment_result(self, payload: object) -> None:
        """Render environment checks and apply automatic settings. / 显示环境检测并应用自动设置。"""
        results, models, hardware, recommendation = payload  # type: ignore[misc]
        self.environment_state = dict(results)
        self.available_ollama_models = list(models)
        self.detected_hardware = dict(hardware)
        self.automatic_recommendation = dict(recommendation)
        self._render_environment_state()
        model_names = [str(item["name"]) for item in models]
        if model_names and hasattr(self, "ollama_menu"):
            self.ollama_menu.set_values(model_names)
            if self.ollama_model_var.get() not in model_names:
                self.ollama_model_var.set(model_names[0])
                self.ollama_menu.set(model_names[0])
        self._apply_hardware_profile(self.profile_key)
        summary = " · ".join(
            f"{name}: {'OK' if ready else detail}"
            for name, (ready, detail) in results.items()
        )
        self._append_log(f"{self.t('environment_log')}: {summary}\n")
        self._append_log(
            f"{self.t('hardware_log')}: {self._hardware_description()}\n"
        )
        if self.profile_key == "auto":
            self._append_log(
                f"{self.t('settings_applied')}: "
                f"Whisper={self.whisper_var.get()}, "
                f"Ollama={self.ollama_model_var.get()}, "
                f"Context={self.ctx_var.get()}, "
                f"Chunk={self.chunk_var.get()}m\n"
            )
        # Persist migrations such as the new automatic FPS mode after the
        # environment and hardware profile have been applied.
        # 环境与硬件方案应用完成后，保存“自动 FPS”等新版设置迁移结果。
        self._save_current_preferences()

    def _render_environment_state(self) -> None:
        """Update status cards from canonical environment state. / 根据规范环境状态更新状态卡。"""
        for key, label in self.status_value_labels.items():
            if key not in self.environment_state:
                continue
            ready, detail = self.environment_state[key]
            label.configure(
                text=f"●  {detail}",
                text_color=COLORS["success"] if ready else COLORS["error"],
            )

    def _start_workflow(self) -> None:
        """Validate settings and launch the serial orchestrator. / 校验设置并启动串行调度器。"""
        if self.process is not None:
            return
        try:
            options = self._collect_options()
            command = options.build_command(sys.executable, self.project_root)
            self._save_settings(options)
        except (ValueError, tk.TclError, OSError) as exc:
            messagebox.showerror(
                self.t("cannot_start"), str(exc), parent=self.root
            )
            return
        self.stop_requested = False
        self.active_options = options
        self._set_progress(0.05)
        self._set_stage("starting")
        self.start_button.configure(state="disabled")
        self.stop_button.configure(state="normal")
        self._append_log("\n" + "─" * 68 + "\n")
        self._append_log(self.t("launch_log") + "\n")
        self._append_log(
            self.t("command_log") + ": "
            + subprocess.list2cmdline(command) + "\n"
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
            self.start_button.configure(state="normal")
            self.stop_button.configure(state="disabled")
            messagebox.showerror(
                self.t("launch_failed"), str(exc), parent=self.root
            )
            return
        threading.Thread(
            target=self._read_process_output,
            args=(self.process,),
            daemon=True,
        ).start()

    def _read_process_output(self, process: subprocess.Popen[str]) -> None:
        """Stream child output into the thread-safe UI queue. / 将子进程输出流式送入线程安全队列。"""
        assert process.stdout is not None
        try:
            for line in process.stdout:
                self.messages.put(("log", line))
        finally:
            self.messages.put(("done", process.wait()))

    def _stop_workflow(self) -> None:
        """Confirm and terminate the active workflow tree. / 确认并终止当前工作流进程树。"""
        process = self.process
        if process is None:
            return
        if not messagebox.askyesno(
            self.t("stop_title"), self.t("stop_question"), parent=self.root
        ):
            return
        self.stop_requested = True
        self._set_stage("stopping")
        self._append_log(self.t("stop_requested"))
        threading.Thread(
            target=self._terminate_process_tree,
            args=(process, self.active_options),
            daemon=True,
        ).start()

    def _terminate_process_tree(
        self,
        process: subprocess.Popen[str],
        options: Optional[WorkflowOptions],
    ) -> None:
        """Stop the orchestrator and all heavy child processes. / 停止调度器与全部重型子进程。"""
        if process.poll() is not None:
            return
        try:
            if os.name == "nt":
                subprocess.run(
                    ["taskkill", "/PID", str(process.pid), "/T", "/F"],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    check=False,
                    creationflags=(
                        subprocess.CREATE_NO_WINDOW
                        if hasattr(subprocess, "CREATE_NO_WINDOW")
                        else 0
                    ),
                )
            else:
                process.terminate()
        except OSError as exc:
            self.messages.put(("log", f"Stop failed: {exc}\n"))
        finally:
            if options is not None:
                self._unload_active_ollama(
                    options.ollama_model, options.ollama_url
                )

    def _unload_active_ollama(self, model: str, base_url: str) -> None:
        """Unload Ollama only if the selected model is resident. / 仅在所选模型驻留时卸载 Ollama。"""
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
            body = json.dumps({
                "model": model, "prompt": "", "stream": False, "keep_alive": 0,
            }).encode("utf-8")
            request = urllib_request.Request(
                base_url.rstrip("/") + "/api/generate",
                data=body,
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urllib_request.urlopen(request, timeout=15):
                pass
        except (OSError, ValueError, urllib_error.URLError) as exc:
            self.messages.put(
                ("log", f"Post-stop Ollama unload check failed: {exc}\n")
            )

    def _drain_messages(self) -> None:
        """Apply worker messages on the Tk main thread. / 在 Tk 主线程处理后台消息。"""
        try:
            while True:
                kind, payload = self.messages.get_nowait()
                if kind == "log":
                    line = self._strip_ansi(str(payload))
                    self._append_log(line)
                    self._update_stage_from_log(line)
                elif kind == "done":
                    self._finish_workflow(int(payload))
                elif kind == "environment":
                    self._apply_environment_result(payload)
                elif kind == "fps":
                    fps, source = payload  # type: ignore[misc]
                    self._apply_detected_fps(float(fps), str(source))
                elif kind == "fps_error":
                    self._append_log(str(payload) + "\n")
        except queue.Empty:
            pass
        self.root.after(100, self._drain_messages)

    def _finish_workflow(self, return_code: int) -> None:
        """Restore controls and report the final state. / 恢复控件并报告最终状态。"""
        self.process = None
        self.active_options = None
        self.start_button.configure(state="normal")
        self.stop_button.configure(state="disabled")
        if return_code == 0:
            self._set_progress(1.0)
            self._set_stage("completed")
            self._append_log(self.t("workflow_success"))
        elif self.stop_requested:
            self._set_progress(0.0)
            self._set_stage("stopped")
            self._append_log(self.t("workflow_stopped"))
        else:
            self.failure_code = return_code
            self._set_stage("failed")
            self._append_log(self.t("workflow_failed", code=return_code))

    def _update_stage_from_log(self, line: str) -> None:
        """Map orchestrator markers to coarse progress. / 将调度器标记映射为粗粒度进度。"""
        if "Starting stage:" in line:
            if "Extract" in line:
                self._set_progress(0.12)
                self._set_stage("extracting")
            elif "Direct" in line:
                self._set_progress(0.48)
                self._set_stage("directing")
            elif "Resolve" in line:
                self._set_progress(0.82)
                self._set_stage("assembling")
        elif "VRAM barrier passed: Whisper/OpenCV" in line:
            self._set_progress(0.42)
            self._set_stage("whisper_released")
        elif "VRAM barrier passed: Ollama" in line:
            self._set_progress(0.78)
            self._set_stage("ollama_released")
        elif "All selected stages completed" in line:
            self._set_progress(1.0)

    def _set_progress(self, value: float) -> None:
        """Set and retain normalized progress. / 设置并保存归一化进度。"""
        self.progress_value = max(0.0, min(1.0, float(value)))
        if hasattr(self, "progress"):
            self.progress.set(self.progress_value)

    def _set_stage(self, key: str) -> None:
        """Set a canonical stage key and refresh text. / 设置规范阶段键并刷新文字。"""
        self.stage_key = key
        if hasattr(self, "stage_label"):
            self.stage_label.configure(text=self._stage_text())

    def _stage_text(self) -> str:
        """Return localized text for the current stage. / 返回当前阶段的本地化文本。"""
        if self.stage_key == "failed":
            return self.t("failed", code=self.failure_code)
        return self.t(self.stage_key)

    def _append_log(self, text: str) -> None:
        """Append a bounded log that survives language rebuilds. / 追加可跨语言重建保留的有界日志。"""
        self.log_history = (self.log_history + text)[-250_000:]
        if hasattr(self, "log_text") and self.log_text.winfo_exists():
            self.log_text.insert("end", text)
            self.log_text.see("end")

    @staticmethod
    def _strip_ansi(text: str) -> str:
        """Remove terminal ANSI escape sequences. / 移除终端 ANSI 转义序列。"""
        return re.sub(r"\x1b\[[0-?]*[ -/]*[@-~]", "", text)

    def _choose_video(self) -> None:
        """Select source media and use it as the default proxy. / 选择源素材并默认将其作为代理素材。"""
        path = filedialog.askopenfilename(
            title=self.t("select_video"),
            filetypes=[
                ("Video", "*.mp4 *.mov *.mkv *.avi *.mxf *.mts *.m2ts"),
                ("All files", "*.*"),
            ],
        )
        if path:
            self.video_var.set(path)
            if not self.proxy_var.get().strip():
                self.proxy_var.set(path)
            self._start_fps_detection(path)

    def _choose_proxy(self) -> None:
        """Select an optional proxy media file. / 选择可选代理素材。"""
        path = filedialog.askopenfilename(
            title=self.t("select_proxy"),
            filetypes=[
                ("Video", "*.mp4 *.mov *.mkv *.avi *.mxf"),
                ("All files", "*.*"),
            ],
        )
        if path:
            self.proxy_var.set(path)
            if not self.video_var.get().strip():
                self._start_fps_detection(path)

    def _choose_data_dir(self) -> None:
        """Select the runtime artifact directory. / 选择运行产物目录。"""
        path = filedialog.askdirectory(
            title=self.t("select_data"),
            initialdir=str(self.project_root / "data"),
        )
        if path:
            self.data_var.set(path)

    def _open_output(self) -> None:
        """Open or create the selected output directory. / 打开或创建所选输出目录。"""
        path = self._absolute_path(self.data_var.get() or "data/ui-run")
        path.mkdir(parents=True, exist_ok=True)
        self._open_path(path)

    def _open_timeline(self) -> None:
        """Open ``timeline_cuts.json`` with the default app. / 使用默认应用打开 ``timeline_cuts.json``。"""
        path = self._absolute_path(
            self.data_var.get() or "data/ui-run"
        ) / "timeline_cuts.json"
        if not path.is_file():
            messagebox.showinfo(
                self.t("no_output"),
                self.t("no_output_detail", path=path),
                parent=self.root,
            )
            return
        self._open_path(path)

    def _absolute_path(self, value: str) -> Path:
        """Resolve a user path against the repository. / 相对于仓库解析用户路径。"""
        path = Path(value).expanduser()
        return (
            path if path.is_absolute() else self.project_root / path
        ).resolve()

    @staticmethod
    def _open_path(path: Path) -> None:
        """Open a local path with the operating-system shell. / 使用操作系统外壳打开本地路径。"""
        if os.name == "nt":
            os.startfile(str(path))  # type: ignore[attr-defined]
        elif sys.platform == "darwin":
            subprocess.Popen(["open", str(path)])
        else:
            subprocess.Popen(["xdg-open", str(path)])

    def _read_settings_data(self) -> Dict[str, object]:
        """Read local UI settings or return an empty object. / 读取本地 UI 设置，失败时返回空对象。"""
        if not self.settings_path.is_file():
            return {}
        try:
            payload = json.loads(
                self.settings_path.read_text(encoding="utf-8")
            )
        except (OSError, ValueError):
            return {}
        return payload if isinstance(payload, dict) else {}

    def _save_settings(self, options: WorkflowOptions) -> None:
        """Atomically save non-secret local settings. / 原子保存非敏感本地设置。"""
        self.settings_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.settings_path.with_suffix(".tmp")
        payload = asdict(options)
        temporary.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        temporary.replace(self.settings_path)
        self.saved_settings = payload

    def _save_current_preferences(self) -> None:
        """Save current choices without requiring valid media. / 无需有效素材即可保存当前选择。"""
        try:
            self._save_settings(self._collect_options(require_fps=False))
        except (OSError, ValueError, tk.TclError):
            pass

    def _on_close(self) -> None:
        """Protect active work before closing the UI. / 关闭 UI 前保护正在运行的任务。"""
        if self.process is not None:
            if not messagebox.askyesno(
                self.t("close_title"),
                self.t("close_question"),
                parent=self.root,
            ):
                return
            self.stop_requested = True
            self._terminate_process_tree(self.process, self.active_options)
        self.root.destroy()


def main(argv: Optional[Sequence[str]] = None) -> int:
    """
    Launch the modern desktop interface.
    启动现代桌面界面。
    """
    del argv
    enable_windows_high_dpi()
    project_root = Path(__file__).resolve().parents[1]
    root = ctk.CTk()
    ModernCyberEditorApp(root, project_root)
    root.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
