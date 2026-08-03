#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Dependency-free interface translations for CyberEditor-Agent.
CyberEditor-Agent 无第三方依赖的界面翻译模块。

This module intentionally imports no GUI toolkit, so translation tests can run
in the repository's standard-library-only CI job.
本模块刻意不导入 GUI 工具包，因此翻译测试可在仓库的纯标准库 CI 中运行。
"""

from __future__ import annotations

import ctypes
import locale
import os
from typing import Dict


TRANSLATIONS: Dict[str, Dict[str, str]] = {
    "zh": {
        "tagline": "本地 · 隐私 · 严格串行 AI 工作流",
        "open_output": "打开输出",
        "theme_system": "跟随系统", "theme_dark": "深色", "theme_light": "浅色",
        "language_system": "跟随系统", "language_zh": "中文", "language_en": "English",
        "python": "PYTHON", "ffmpeg": "FFMPEG", "cuda": "PYTORCH / CUDA",
        "ollama": "OLLAMA", "resolve": "DAVINCI", "detecting": "检测中…",
        "ready": "可用", "online": "在线", "not_found": "未找到",
        "not_connected": "未连接", "installed": "已安装",
        "installed_auto_start": "已安装 · 执行时自动启动",
        "models_count": "{count} 个模型",
        "models_count_started": "{count} 个模型 · 已自动启动",
        "online_started": "在线 · 已自动启动", "not_installed": "未安装",
        "gpu_cuda_ready": "{version} · CUDA 可用",
        "gpu_cpu_only": "{version} · 仅 CPU",
        "project_media": "项目与素材", "workflow_mode": "工作流模式",
        "flow_full": "完整流程", "flow_director": "从 AI 导演继续",
        "flow_resolve": "仅执行 Resolve", "source_video": "源视频",
        "proxy_media": "代理素材（可选）", "runtime_data": "运行数据目录",
        "browse": "浏览", "select_folder": "选择", "project_fps": "项目 FPS",
        "fps_auto": "自动读取源素材",
        "fps_auto_detected": "自动 · {fps} fps",
        "fps_detected_log": "已读取源素材帧率：{fps} fps",
        "fps_detection_failed": "无法自动读取素材 FPS。请确认 FFmpeg/ffprobe 已安装，或手动选择帧率。",
        "ai_hardware": "AI 与硬件", "hardware_profile": "硬件配置",
        "profile_auto": "自动检测", "profile_conservative": "节能",
        "profile_balanced": "均衡", "profile_performance": "高质量（较慢）",
        "profile_custom": "自定义",
        "detecting_hardware": "正在检测硬件并计算安全参数…",
        "whisper_model": "Whisper 模型", "whisper_device": "Whisper 设备",
        "source_language": "语音语言（可选，如 zh/en）",
        "ollama_model": "Ollama 模型", "ollama_context": "上下文长度",
        "ollama_url": "Ollama 地址", "chunk_minutes": "分块分钟数（10–15）",
        "resolve_settings": "DaVinci Resolve", "timeline_name": "时间线名称",
        "project_name": "工程名称", "skip_resolve": "暂不执行 Resolve",
        "strict_fps": "严格校验 JSON 与工程 FPS",
        "run_center": "任务中心", "serial_badge": "SERIAL / VRAM SAFE",
        "window_effect": "Windows 11 Mica · 原生贴靠布局",
        "ready_stage": "准备就绪",
        "ready_hint": "可多选视频或选择文件夹；AI 会看画面、听台词并跨素材统一剪辑。",
        "starting": "正在启动", "extracting": "1/4  逐个提取素材",
        "directing": "2/4  AI 全局导演", "previewing": "3/4  生成预览成片",
        "assembling": "4/4  Resolve 组装",
        "whisper_released": "Whisper 显存已释放",
        "ollama_released": "Ollama 显存已释放", "completed": "全部完成",
        "stopping": "正在停止", "stopped": "已停止",
        "failed": "运行失败（退出码 {code}）", "start": "开始串行工作流",
        "stop": "停止", "live_log": "实时日志",
        "view_timeline": "查看 timeline_cuts.json", "recheck": "重新检测环境",
        "custom_settings": "自定义参数", "auto_settings": "自动配置",
        "settings_applied": "已应用自动配置",
        "ui_ready_log": "现代界面已启动。请选择素材并开始运行。\n",
        "environment_log": "环境检测", "hardware_log": "硬件检测",
        "launch_log": "启动严格串行工作流", "command_log": "命令",
        "workflow_success": "✓ 工作流成功完成\n",
        "workflow_stopped": "工作流已停止\n",
        "workflow_failed": "✕ 工作流失败，退出码 {code}\n",
        "stop_title": "停止任务",
        "stop_question": "确定停止当前任务吗？已完成的中间文件会保留。",
        "close_title": "任务正在运行",
        "close_question": "关闭界面将停止当前任务。是否继续？",
        "cannot_start": "无法启动", "launch_failed": "启动失败",
        "no_output": "尚无输出", "no_output_detail": "尚未生成文件：\n{path}",
        "select_video": "选择源视频", "select_proxy": "选择代理素材",
        "select_data": "选择运行数据目录", "stop_requested": "用户请求停止任务\n",
        "source_videos": "视频素材（可多选）",
        "input_folder": "或选择素材文件夹",
        "select_input_folder": "选择素材文件夹",
        "videos_selected": "已选择 {count} 个视频",
        "folder_videos_found": "文件夹中找到 {count} 个视频",
        "render_preview": "生成可观看预览",
        "text_only_model": "当前模型不能看图 · 请安装视觉模型",
        "open_preview": "播放预览成片",
        "no_preview_detail": "尚未生成预览成片：\n{path}",
    },
    "en": {
        "tagline": "LOCAL · PRIVATE · STRICT SERIAL AI WORKFLOW",
        "open_output": "Open output",
        "theme_system": "System", "theme_dark": "Dark", "theme_light": "Light",
        "language_system": "System", "language_zh": "中文", "language_en": "English",
        "python": "PYTHON", "ffmpeg": "FFMPEG", "cuda": "PYTORCH / CUDA",
        "ollama": "OLLAMA", "resolve": "DAVINCI", "detecting": "Detecting…",
        "ready": "Ready", "online": "Online", "not_found": "Not found",
        "not_connected": "Not connected", "installed": "Installed",
        "installed_auto_start": "Installed · auto-starts when needed",
        "models_count": "{count} models",
        "models_count_started": "{count} models · auto-started",
        "online_started": "Online · auto-started", "not_installed": "Not installed",
        "gpu_cuda_ready": "{version} · CUDA ready",
        "gpu_cpu_only": "{version} · CPU only",
        "project_media": "Project & media", "workflow_mode": "Workflow mode",
        "flow_full": "Full pipeline", "flow_director": "Resume at AI director",
        "flow_resolve": "Resolve only", "source_video": "Source video",
        "proxy_media": "Proxy media (optional)",
        "runtime_data": "Runtime data directory", "browse": "Browse",
        "select_folder": "Choose", "project_fps": "Project FPS",
        "fps_auto": "Auto from source media",
        "fps_auto_detected": "Auto · {fps} fps",
        "fps_detected_log": "Detected source frame rate: {fps} fps",
        "fps_detection_failed": "Could not detect media FPS. Verify FFmpeg/ffprobe or choose a frame rate manually.",
        "ai_hardware": "AI & hardware", "hardware_profile": "Hardware profile",
        "profile_auto": "Auto detect", "profile_conservative": "Conservative",
        "profile_balanced": "Balanced", "profile_performance": "Quality (slower)",
        "profile_custom": "Custom",
        "detecting_hardware": "Detecting hardware and calculating safe settings…",
        "whisper_model": "Whisper model", "whisper_device": "Whisper device",
        "source_language": "Speech language (optional, e.g. zh/en)",
        "ollama_model": "Ollama model", "ollama_context": "Context length",
        "ollama_url": "Ollama URL", "chunk_minutes": "Chunk minutes (10–15)",
        "resolve_settings": "DaVinci Resolve", "timeline_name": "Timeline name",
        "project_name": "Project name", "skip_resolve": "Skip Resolve for now",
        "strict_fps": "Strict JSON/project FPS validation",
        "run_center": "Run center", "serial_badge": "SERIAL / VRAM SAFE",
        "window_effect": "Windows 11 Mica · native Snap Layouts",
        "ready_stage": "Ready",
        "ready_hint": "Select videos or a folder. AI reviews frames and speech, then edits across all sources.",
        "starting": "Starting", "extracting": "1/4  Extracting every source",
        "directing": "2/4  AI global edit", "previewing": "3/4  Rendering preview",
        "assembling": "4/4  Resolve assembly",
        "whisper_released": "Whisper VRAM released",
        "ollama_released": "Ollama VRAM released", "completed": "Completed",
        "stopping": "Stopping", "stopped": "Stopped",
        "failed": "Failed (exit code {code})", "start": "Start serial workflow",
        "stop": "Stop", "live_log": "Live log",
        "view_timeline": "View timeline_cuts.json", "recheck": "Recheck environment",
        "custom_settings": "Custom settings", "auto_settings": "Automatic settings",
        "settings_applied": "Automatic settings applied",
        "ui_ready_log": "Modern UI ready. Select media and start the workflow.\n",
        "environment_log": "Environment", "hardware_log": "Hardware",
        "launch_log": "Starting strict serial workflow", "command_log": "Command",
        "workflow_success": "✓ Workflow completed successfully\n",
        "workflow_stopped": "Workflow stopped\n",
        "workflow_failed": "✕ Workflow failed with exit code {code}\n",
        "stop_title": "Stop workflow",
        "stop_question": "Stop now? Completed intermediate files will be kept.",
        "close_title": "Workflow running",
        "close_question": "Closing the UI will stop the active workflow. Continue?",
        "cannot_start": "Cannot start", "launch_failed": "Launch failed",
        "no_output": "No output",
        "no_output_detail": "This file has not been generated yet:\n{path}",
        "select_video": "Select source video", "select_proxy": "Select proxy media",
        "select_data": "Select runtime data directory",
        "stop_requested": "Stop requested by user\n",
        "source_videos": "Video sources (multi-select)",
        "input_folder": "Or choose a media folder",
        "select_input_folder": "Select media folder",
        "videos_selected": "{count} videos selected",
        "folder_videos_found": "Found {count} videos in the folder",
        "render_preview": "Render watchable preview",
        "text_only_model": "Text-only model · install a vision model",
        "open_preview": "Play preview",
        "no_preview_detail": "The preview has not been rendered yet:\n{path}",
    },
}


def detect_system_language() -> str:
    """
    Detect whether Windows should use Chinese or English.
    检测 Windows 应使用中文还是英文。
    """
    language = ""
    if os.name == "nt":
        try:
            buffer = ctypes.create_unicode_buffer(85)
            if ctypes.windll.kernel32.GetUserDefaultLocaleName(buffer, len(buffer)):
                language = buffer.value
        except (AttributeError, OSError):
            pass
    if not language:
        try:
            language = locale.getlocale()[0] or ""
        except (ValueError, TypeError):
            pass
    return "zh" if language.casefold().startswith("zh") else "en"


def resolve_language(mode: str) -> str:
    """Resolve System/Chinese/English to an active language. / 解析系统/中文/英文为实际语言。"""
    return detect_system_language() if mode == "system" else (
        mode if mode in TRANSLATIONS else "en"
    )


def translate(language: str, key: str, **values: object) -> str:
    """Translate one UI key with optional values. / 翻译一个 UI 键并替换可选变量。"""
    text = TRANSLATIONS.get(language, TRANSLATIONS["en"]).get(
        key, TRANSLATIONS["en"].get(key, key)
    )
    return text.format(**values) if values else text
