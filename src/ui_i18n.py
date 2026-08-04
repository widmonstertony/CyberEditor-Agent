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
        "resolve_registered": "已注册 {version} · API 连接时确认 Studio",
        "resolve_registered_path_missing": "已注册 {version} · 缺少启动目标",
        "resolve_registry_missing": "找到程序 · 注册信息缺失",
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
        "ollama_model": "视觉理解模型", "director_model": "全局文字导演（串行）", "ollama_context": "上下文长度",
        "ollama_url": "Ollama 地址", "chunk_minutes": "每个分析窗口（10–15 分钟；总素材不限）",
        "director_settings": "导演意图与影像风格",
        "creative_brief": "成片主题 / AI 导演要求（可选）",
        "creative_brief_hint": "例如：剪成一支‘夜骑伙伴从集合到出发’的热血短片；留空则由 AI 自由发现主题并完成叙事。",
        "target_duration": "目标成片秒数（0=自动）",
        "camera_profile": "原素材色彩配置",
        "camera_sony_pp8": "Sony PP8 · S-Log3 / S-Gamut3.Cine",
        "camera_rec709": "标准 Rec.709（已还原素材）",
        "camera_auto": "自动（默认按 Sony PP8 检查）",
        "music_folder": "本地授权配乐库（可选）",
        "select_music_folder": "选择本地授权配乐文件夹",
        "music_provider": "自动配乐来源",
        "music_provider_online": "任意在线音频 · 效果优先",
        "music_provider_local": "本地授权曲库",
        "music_provider_jamendo": "Jamendo · 可验证许可证",
        "music_provider_off": "不使用配乐",
        "music_candidate_limit": "听诊候选数（1–12）",
        "jamendo_client_id": "Jamendo client_id（仅 Jamendo 模式）",
        "music_online_warning": "⚠ 任意在线模式会使用 yt-dlp。来源可用不等于获得版权；商用前必须自行取得完整授权并遵守平台条款。",
        "music_verified_hint": "本地/Jamendo 模式仍会保存曲目来源、许可证与文件哈希，发布前请复核授权范围。",
        "music_rights_title": "版权与平台条款确认",
        "music_rights_confirmation": "任意在线音频模式将搜索并下载候选音乐。\n\n继续即表示：\n• 你确认拥有下载、改编、同步到视频和使用候选音频所需的权利或许可；\n• 你会遵守每个来源平台的服务条款；\n• 商业发布前会另行取得覆盖商用、改编与同步的授权。\n\n此确认只是审计记录，不会自动授予版权。是否继续？",
        "music_rights_audit_claim": "用户在本次运行中确认其拥有下载、改编、同步与使用候选音频所需的权利，并会遵守来源平台条款；商用前将取得相应商业授权。",
        "resolve_settings": "DaVinci Resolve", "timeline_name": "时间线名称",
        "project_name": "工程名称", "skip_resolve": "暂不执行 Resolve",
        "run_resolve": "完成后发送到 Resolve",
        "strict_fps": "严格校验 JSON 与工程 FPS",
        "drx_root": "DRX 预设目录",
        "fairlight_preset": "Fairlight 预设名（可选）",
        "macro_profile": "UI 宏配置（高级，可选）",
        "render_final": "Resolve 导出最终成片",
        "render_dir": "最终成片目录",
        "render_name": "最终成片文件名",
        "render_preset": "Resolve 渲染预设名（可选）",
        "run_center": "任务中心", "serial_badge": "SERIAL / VRAM SAFE",
        "window_effect": "Windows 11 Mica · 原生贴靠布局",
        "ready_stage": "准备就绪",
        "ready_hint": "可多选视频或选择文件夹；AI 会看画面、听台词并跨素材统一剪辑。",
        "starting": "正在启动", "extracting": "1/6  逐个提取素材",
        "directing": "2–5/6  双导演、找歌、听诊与音乐床",
        "previewing": "5/6  生成预览成片",
        "assembling": "6/6  Resolve 效果与最终导出",
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
        "resolve_registered": "Registered {version} · Studio verified by API",
        "resolve_registered_path_missing": "Registered {version} · launch target missing",
        "resolve_registry_missing": "Executable found · registration missing",
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
        "ollama_model": "Vision model", "director_model": "Global text director (serial)", "ollama_context": "Context length",
        "ollama_url": "Ollama URL", "chunk_minutes": "Per analysis window (10–15 min; total unlimited)",
        "director_settings": "Director intent & image style",
        "creative_brief": "Film theme / AI director brief (optional)",
        "creative_brief_hint": "Example: a high-energy night-ride short, from meeting up to departure. Leave blank for AI free direction.",
        "target_duration": "Target runtime seconds (0 = auto)",
        "camera_profile": "Source camera color profile",
        "camera_sony_pp8": "Sony PP8 · S-Log3 / S-Gamut3.Cine",
        "camera_rec709": "Standard Rec.709 (already normalized)",
        "camera_auto": "Auto (verify as Sony PP8 by default)",
        "music_folder": "Local licensed music library (optional)",
        "select_music_folder": "Select local licensed music folder",
        "music_provider": "Automatic music source",
        "music_provider_online": "Any online audio · quality first",
        "music_provider_local": "Local licensed library",
        "music_provider_jamendo": "Jamendo · verifiable license",
        "music_provider_off": "No music",
        "music_candidate_limit": "Candidates to analyze (1–12)",
        "jamendo_client_id": "Jamendo client_id (Jamendo only)",
        "music_online_warning": "⚠ Any-online mode uses yt-dlp. Availability is not a license; obtain every required commercial right and follow source-platform terms.",
        "music_verified_hint": "Local/Jamendo modes still save source, license, and file hashes. Verify the grant before publishing.",
        "music_rights_title": "Rights and platform terms",
        "music_rights_confirmation": "Any-online mode will search for and download music candidates.\n\nBy continuing, you confirm that:\n• you hold the rights or permission required to download, adapt, synchronize, and use candidate audio;\n• you will comply with every source platform's terms; and\n• before commercial release, you will obtain a license covering commercial use, adaptation, and synchronization.\n\nThis confirmation is only an audit record and grants no rights. Continue?",
        "music_rights_audit_claim": "For this run, the user confirmed the rights required to download, adapt, synchronize, and use candidate audio, agreed to source-platform terms, and will obtain commercial rights before commercial release.",
        "resolve_settings": "DaVinci Resolve", "timeline_name": "Timeline name",
        "project_name": "Project name", "skip_resolve": "Skip Resolve for now",
        "run_resolve": "Send finished edit to Resolve",
        "strict_fps": "Strict JSON/project FPS validation",
        "drx_root": "DRX preset folder",
        "fairlight_preset": "Fairlight preset name (optional)",
        "macro_profile": "UI macro profile (advanced, optional)",
        "render_final": "Export final movie in Resolve",
        "render_dir": "Final render folder",
        "render_name": "Final render name",
        "render_preset": "Resolve render preset (optional)",
        "run_center": "Run center", "serial_badge": "SERIAL / VRAM SAFE",
        "window_effect": "Windows 11 Mica · native Snap Layouts",
        "ready_stage": "Ready",
        "ready_hint": "Select videos or a folder. AI reviews frames and speech, then edits across all sources.",
        "starting": "Starting", "extracting": "1/6  Extracting every source",
        "directing": "2–5/6  Two directors, retrieval, analysis & music bed",
        "previewing": "5/6  Rendering preview",
        "assembling": "6/6  Resolve effects & final export",
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
