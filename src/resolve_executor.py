#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
DaVinci Resolve timeline assembly for Windows.
Windows 平台 DaVinci Resolve 时间线组装。

Only Python's standard library and Resolve's bundled ``DaVinciResolveScript``
module are used in this stage.

本阶段仅使用 Python 标准库和 Resolve 自带的 ``DaVinciResolveScript`` 模块。
"""

import argparse
from decimal import Decimal, InvalidOperation, ROUND_CEILING, ROUND_FLOOR
import importlib
import json
import logging
import os
from pathlib import Path
import platform
import re
import shutil
import struct
import subprocess
import sys
import time
from typing import Any, Dict, Iterable, List, NamedTuple, Optional, Sequence, Tuple

try:
    from .runtime_services import find_resolve_executable
except ImportError:  # pragma: no cover - direct ``python src/...`` fallback.
    from runtime_services import find_resolve_executable


LOGGER_NAME = "cybereditor.resolve"


class ResolveExecutorError(RuntimeError):
    """Expected Resolve executor failure. / 可预期的 Resolve 执行错误。"""


class ClipDecision(NamedTuple):
    """Validated cut decision. / 已验证的剪辑决策。"""

    clip_id: Any
    file_name: str
    cut_in_sec: Decimal
    cut_out_sec: Decimal
    reason_for_cut: str
    transition_to_next: str = "cut"
    transition_duration_sec: Decimal = Decimal("0")
    audio_cleanup: str = "light"
    color_look: str = "neutral"
    motion: str = "static"
    volume_db: Decimal = Decimal("0")
    drx_preset: str = "none"
    stabilization: str = "none"
    tracking: str = "none"
    smart_reframe: bool = False


class MediaRecord(NamedTuple):
    """Searchable Media Pool item metadata. / 可检索的媒体池条目元数据。"""

    item: Any
    name: str
    file_path: str


class DaVinciExecutor:
    """
    Validate JSON, connect to Resolve, and assemble the current timeline.
    校验 JSON、连接 Resolve，并组装当前时间线。

    Parameters / 参数:
        json_path:
            Path to ``timeline_cuts.json``.
            ``timeline_cuts.json`` 路径。
        media_root:
            Root used to resolve relative ``file_name`` values.
            解析相对 ``file_name`` 使用的根目录。
        timeline_name:
            Name used when no current timeline exists.
            没有当前时间线时使用的名称。
        project_name:
            Base name used when no project is open.
            没有打开工程时使用的基础名称。
        strict_fps:
            Fail instead of warning when JSON and Resolve FPS differ.
            JSON 与 Resolve FPS 不一致时失败，而非警告。
        auto_start_resolve:
            Start Resolve automatically when it is installed but not running.
            Resolve 已安装但未运行时自动启动。
        startup_timeout:
            Maximum seconds to wait for the Resolve scripting API.
            等待 Resolve 脚本 API 就绪的最长秒数。
    """

    def __init__(
        self,
        json_path: os.PathLike,
        media_root: Optional[os.PathLike] = None,
        timeline_name: str = "CyberEditor Timeline",
        project_name: str = "CyberEditor Project",
        strict_fps: bool = False,
        auto_start_resolve: bool = True,
        startup_timeout: float = 120.0,
        drx_root: Optional[os.PathLike] = None,
        fairlight_preset: str = "",
        render_enabled: bool = False,
        render_dir: Optional[os.PathLike] = None,
        render_name: str = "CyberEditor_final",
        render_preset: str = "",
        render_timeout: float = 86400.0,
        macro_profile: Optional[os.PathLike] = None,
        macro_action: str = "post_assembly",
        logger: Optional[logging.Logger] = None,
    ) -> None:
        """Initialize paths and policy without connecting to Resolve. / 初始化路径和策略，但不连接 Resolve。"""
        self.json_path = Path(json_path).expanduser().resolve()
        self.media_root = (
            Path(media_root).expanduser().resolve()
            if media_root
            else self.json_path.parent
        )
        self.timeline_name = timeline_name.strip()
        self.project_name = project_name.strip()
        self.strict_fps = strict_fps
        self.auto_start_resolve = bool(auto_start_resolve)
        self.startup_timeout = float(startup_timeout)
        self.drx_root = (
            Path(drx_root).expanduser().resolve()
            if drx_root
            else Path(__file__).resolve().parents[1] / "config" / "drx"
        )
        self.fairlight_preset = fairlight_preset.strip()
        self.render_enabled = bool(render_enabled)
        self.render_dir = (
            Path(render_dir).expanduser().resolve()
            if render_dir
            else self.json_path.parent / "final"
        )
        self.render_name = render_name.strip()
        self.render_preset = render_preset.strip()
        self.render_timeout = float(render_timeout)
        self.macro_profile = (
            Path(macro_profile).expanduser().resolve() if macro_profile else None
        )
        self.macro_action = macro_action.strip()
        self.logger = logger or logging.getLogger(LOGGER_NAME)
        if not self.timeline_name or not self.project_name:
            raise ResolveExecutorError(
                "工程/时间线名称不能为空 / Project/timeline name cannot be empty."
            )
        if self.startup_timeout <= 0:
            raise ResolveExecutorError(
                "Resolve 启动超时必须大于 0 秒"
                " / Resolve startup timeout must be greater than zero."
            )
        if not self.render_name or self.render_timeout <= 0:
            raise ResolveExecutorError(
                "导出文件名不能为空，渲染超时必须大于 0 / "
                "Render name cannot be empty and render timeout must be positive."
            )

        self.resolve: Any = None
        self.project_manager: Any = None
        self.project: Any = None
        self.media_pool: Any = None
        self.timeline: Any = None
        self.created_project = False
        self.color_pipeline: Dict[str, Any] = {}
        self.music_plan: Dict[str, Any] = {}

    def run(self) -> Sequence[Any]:
        """
        Execute the complete Resolve assembly workflow.
        执行完整的 Resolve 组装工作流。

        Returns / 返回:
            Timeline items returned by ``AppendToTimeline``.
            ``AppendToTimeline`` 返回的时间线条目。
        """
        json_fps, clips = self.load_cut_plan()
        self.resolve = self.connect()
        self.project_manager, self.project = self.ensure_project()
        if self.created_project:
            self.initialize_new_project_fps(json_fps)
        self.configure_color_pipeline()
        self.media_pool = self.project.GetMediaPool()
        if self.media_pool is None:
            raise ResolveExecutorError(
                "无法获取 Media Pool / Could not obtain Media Pool."
            )
        self.timeline = self.ensure_timeline()
        active_fps = self.get_active_fps()
        self.compare_fps(json_fps, active_fps)
        prepared = self.prepare_clips(clips, active_fps)
        appended = self.append_clips(prepared)
        self.append_music_bed(active_fps, prepared)
        self.apply_timeline_audio_preset()
        self.run_macro_fallback()
        self.save_project()
        if self.render_enabled:
            self.render_final()
        self.logger.info(
            "执行完成：时间线“%s”已追加 %d 个片段 / Complete: appended %d clips to '%s'",
            self._safe_name(self.timeline, self.timeline_name),
            len(clips),
            len(clips),
            self._safe_name(self.timeline, self.timeline_name),
        )
        return appended

    def load_cut_plan(self) -> Tuple[Decimal, Sequence[ClipDecision]]:
        """
        Read and strictly validate ``timeline_cuts.json``.
        读取并严格校验 ``timeline_cuts.json``。
        """
        if not self.json_path.is_file():
            raise ResolveExecutorError(
                f"找不到剪辑 JSON / Cut JSON not found: {self.json_path}"
            )
        try:
            payload = json.loads(self.json_path.read_text(encoding="utf-8-sig"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ResolveExecutorError(
                f"无法解析剪辑 JSON / Cannot parse cut JSON: {exc}"
            ) from exc
        if not isinstance(payload, dict):
            raise ResolveExecutorError(
                "JSON 根节点必须是对象 / JSON root must be an object."
            )
        pipeline = payload.get("color_pipeline")
        self.color_pipeline = pipeline if isinstance(pipeline, dict) else {}
        music = payload.get("music_plan")
        self.music_plan = music if isinstance(music, dict) else {}
        fps = self._positive_decimal(payload.get("project_fps"), "project_fps")
        raw_clips = payload.get("clips")
        if not isinstance(raw_clips, list) or not raw_clips:
            raise ResolveExecutorError(
                "clips 必须是非空数组 / clips must be a non-empty array."
            )

        decisions: List[ClipDecision] = []
        seen_ids = set()
        for index, item in enumerate(raw_clips):
            prefix = f"clips[{index}]"
            if not isinstance(item, dict):
                raise ResolveExecutorError(f"{prefix} 必须是对象 / must be an object.")
            clip_id = item.get("clip_id", index + 1)
            identity = (type(clip_id).__name__, str(clip_id))
            if identity in seen_ids:
                raise ResolveExecutorError(
                    f"clip_id 重复 / Duplicate clip_id: {clip_id!r}"
                )
            seen_ids.add(identity)
            file_name = item.get("file_name")
            if not isinstance(file_name, str) or not file_name.strip():
                raise ResolveExecutorError(
                    f"{prefix}.file_name 必须是非空字符串 / must be a non-empty string."
                )
            cut_in = self._non_negative_decimal(
                item.get("cut_in_sec"), f"{prefix}.cut_in_sec"
            )
            cut_out = self._non_negative_decimal(
                item.get("cut_out_sec"), f"{prefix}.cut_out_sec"
            )
            if cut_out <= cut_in:
                raise ResolveExecutorError(
                    f"{prefix}: cut_out_sec 必须大于 cut_in_sec / out must exceed in."
                )
            reason = item.get("reason_for_cut", "")
            if not isinstance(reason, str):
                raise ResolveExecutorError(
                    f"{prefix}.reason_for_cut 必须是字符串 / must be a string."
                )
            transition = self._enum_text(
                item.get("transition_to_next"),
                {"cut", "cross_dissolve", "fade_black"},
                "cut",
            )
            transition_duration = self._non_negative_decimal(
                item.get("transition_duration_sec", 0),
                f"{prefix}.transition_duration_sec",
            )
            transition_duration = min(transition_duration, Decimal("2"))
            decisions.append(
                ClipDecision(
                    clip_id=clip_id,
                    file_name=file_name.strip(),
                    cut_in_sec=cut_in,
                    cut_out_sec=cut_out,
                    reason_for_cut=reason.strip(),
                    transition_to_next=transition,
                    transition_duration_sec=transition_duration,
                    audio_cleanup=self._enum_text(
                        item.get("audio_cleanup"),
                        {"none", "light", "strong"},
                        "light",
                    ),
                    color_look=self._enum_text(
                        item.get("color_look"),
                        {"source", "neutral", "warm", "cool", "contrast"},
                        "neutral",
                    ),
                    motion=self._enum_text(
                        item.get("motion"),
                        {"static", "gentle_push_in"},
                        "static",
                    ),
                    volume_db=max(
                        Decimal("-24"),
                        min(
                            Decimal("12"),
                            self._decimal(item.get("volume_db", 0), f"{prefix}.volume_db"),
                        ),
                    ),
                    drx_preset=self._enum_text(
                        item.get("drx_preset"),
                        {"none", "interview_clean", "cinematic", "low_light_cleanup"},
                        "none",
                    ),
                    stabilization=self._enum_text(
                        item.get("stabilization"), {"none", "auto"}, "none"
                    ),
                    tracking=self._enum_text(
                        item.get("tracking"),
                        {
                            "none",
                            "magic_mask_forward",
                            "magic_mask_backward",
                            "magic_mask_bidirectional",
                        },
                        "none",
                    ),
                    smart_reframe=(
                        item.get("smart_reframe", False)
                        if isinstance(item.get("smart_reframe", False), bool)
                        else False
                    ),
                )
            )
        self.logger.info(
            "已读取 %d 条剪辑决策 / Loaded %d cut decisions",
            len(decisions),
            len(decisions),
        )
        return fps, decisions

    def connect(self) -> Any:
        """
        Detect Windows/Resolve, load the bundled module, and get Resolve.
        检测 Windows/Resolve、加载自带模块并获取 Resolve 实例。
        """
        if platform.system().lower() != "windows":
            raise ResolveExecutorError(
                f"执行层仅支持 Windows / Resolve stage requires Windows; got {platform.system()}."
            )
        if struct.calcsize("P") * 8 != 64:
            raise ResolveExecutorError(
                "DaVinci Resolve 需要 64 位 Python / Resolve requires 64-bit Python."
            )
        executable = find_resolve_executable()
        self._configure_resolve_library(executable)
        module, checked, errors = self._load_resolve_module()
        if module is None:
            checked_text = "\n  - ".join(str(path) for path in checked)
            raise ResolveExecutorError(
                "无法导入 DaVinciResolveScript。请检查 Resolve 安装及 "
                "RESOLVE_SCRIPT_API、RESOLVE_SCRIPT_LIB、PYTHONPATH。\n"
                "Could not import DaVinciResolveScript. Check the Resolve "
                "installation and scripting environment variables.\n"
                f"Checked:\n  - {checked_text or '(none)'}\n"
                f"Errors: {' | '.join(errors[-3:])}"
            )

        process_state = self._is_resolve_running()
        resolve = self._try_scriptapp(module)
        launched_process: Optional[subprocess.Popen] = None
        if resolve is None and process_state is not True:
            if not self.auto_start_resolve:
                raise ResolveExecutorError(
                    "未检测到 Resolve.exe，且已关闭自动启动。"
                    " / Resolve is not running and auto-start is disabled."
                )
            if executable is None:
                raise ResolveExecutorError(
                    "未找到 Resolve.exe。已检查注册表及所有磁盘的 Program Files。"
                    " / Resolve.exe was not found in the registry or Program Files "
                    "on mounted drives."
                )
            launched_process = self._launch_resolve(executable)
            self.logger.info(
                "已自动启动 Resolve，正在等待脚本 API（最长 %.0f 秒）"
                " / Resolve auto-started; waiting up to %.0f seconds for its API",
                self.startup_timeout,
                self.startup_timeout,
            )

        if resolve is None:
            deadline = time.monotonic() + self.startup_timeout
            while resolve is None and time.monotonic() < deadline:
                if (
                    launched_process is not None
                    and launched_process.poll() is not None
                ):
                    raise ResolveExecutorError(
                        "Resolve 启动后提前退出。请手动打开 Resolve 检查启动报错。"
                        " / Resolve exited during startup; open it manually to "
                        "inspect the startup error."
                    )
                time.sleep(min(2.0, max(0.1, deadline - time.monotonic())))
                resolve = self._try_scriptapp(module)
        if resolve is None:
            raise ResolveExecutorError(
                "Resolve 已运行，但脚本 API 在等待后仍不可用。请在 Preferences > "
                "System > General 中将 External scripting 设为 Local 后重启。"
                "当前安装的 API 文档要求 DaVinci Resolve Studio；免费版可能无法"
                "接受外部脚本连接。\n"
                "Resolve is running but its API did not become ready. Enable Local "
                "external scripting and restart Resolve. The bundled API documentation "
                "targets DaVinci Resolve Studio."
            )
        try:
            name = resolve.GetProductName()
            version = resolve.GetVersionString()
        except Exception:
            name, version = "DaVinci Resolve", "unknown"
        self.logger.info(
            "已连接 %s %s / Connected to %s %s",
            name,
            version,
            name,
            version,
        )
        return resolve

    def ensure_project(self) -> Tuple[Any, Any]:
        """
        Return the current project or create a uniquely named one.
        返回当前工程，或创建名称唯一的新工程。
        """
        manager = self.resolve.GetProjectManager()
        if manager is None:
            raise ResolveExecutorError(
                "无法获取 Project Manager / Could not obtain Project Manager."
            )
        project = manager.GetCurrentProject()
        current_project_name = self._safe_name(project, "") if project is not None else ""
        expected_color_names = {
            self.project_name.casefold(),
            f"{self.project_name} director cut".casefold(),
        }
        needs_isolated_color_project = (
            project is not None
            and bool(self.color_pipeline.get("enabled"))
            and (
                self._safe_int_call(project, "GetTimelineCount") > 0
                or not any(
                    current_project_name.casefold().startswith(name)
                    for name in expected_color_names
                )
            )
        )
        if needs_isolated_color_project:
            project_names = self._project_names(manager)
            name = self._unique_name(
                f"{self.project_name} Director Cut", project_names
            )
            try:
                project = manager.CreateProject(name)
            except Exception as exc:
                raise ResolveExecutorError(
                    "当前工程已有时间线，无法安全切换 Sony Log 色彩管理，且新建导演工程失败。"
                    f" / Could not create an isolated color-managed project: {exc}"
                ) from exc
            if project is None:
                raise ResolveExecutorError(
                    "无法为 PP8 创建独立色彩管理工程 / Could not create an isolated PP8 project."
                )
            self.created_project = True
            self.logger.info(
                "为避免修改已有工程，已创建独立导演工程“%s” / Created isolated director project '%s'",
                name, name,
            )
            return manager, project
        if project is not None:
            current_name = self._safe_name(project, "unnamed")
            normalized_name = current_name.strip().casefold()
            try:
                current_page = self.resolve.GetCurrentPage()
            except Exception:
                current_page = None
            is_transient_untitled = (
                normalized_name
                in {
                    "untitled",
                    "untitled project",
                    "未命名项目",
                    "未命名工程",
                }
                and current_page is None
                and self._safe_int_call(project, "GetTimelineCount") == 0
            )
            if is_transient_untitled:
                self.logger.info(
                    "检测到未进入编辑页面的临时空工程“%s”；将创建/加载“%s” / "
                    "Detected transient untitled project; creating/loading '%s'",
                    current_name,
                    self.project_name,
                    self.project_name,
                )
                project_names = self._project_names(manager)
                matching_name = next(
                    (
                        name
                        for name in project_names
                        if name.casefold() == self.project_name.casefold()
                    ),
                    None,
                )
                try:
                    if matching_name is not None:
                        project = manager.LoadProject(matching_name)
                    else:
                        project = manager.CreateProject(
                            self._unique_name(self.project_name, project_names)
                        )
                        self.created_project = project is not None
                except Exception as exc:
                    raise ResolveExecutorError(
                        "临时 Untitled Project 无法进入编辑页面，且创建指定工程失败。"
                        f" / Could not replace transient project: {exc}"
                    ) from exc
                if project is None:
                    raise ResolveExecutorError(
                        "无法创建/加载可编辑工程。请在 Resolve Project Manager 中手动"
                        f"创建“{self.project_name}”后重试。 / Could not create or load "
                        "an editable project."
                    )
                self.logger.info(
                    "已%s工程“%s” / %s project '%s'",
                    "创建" if self.created_project else "加载",
                    self._safe_name(project, self.project_name),
                    "Created" if self.created_project else "Loaded",
                    self._safe_name(project, self.project_name),
                )
                return manager, project
            self.logger.info(
                "使用当前工程“%s” / Using current project '%s'",
                current_name,
                current_name,
            )
            return manager, project

        name = self._unique_name(self.project_name, self._project_names(manager))
        try:
            project = manager.CreateProject(name)
        except Exception as exc:
            raise ResolveExecutorError(
                f"没有打开工程且自动创建失败 / No project open and creation failed: {exc}"
            ) from exc
        if project is None:
            raise ResolveExecutorError(
                "CreateProject() 未返回工程；请手动创建工程 / returned no project."
            )
        self.created_project = True
        self.logger.info("已创建工程“%s” / Created project '%s'", name, name)
        return manager, project

    def initialize_new_project_fps(self, fps: Decimal) -> None:
        """
        Seed a newly created project FPS before media/timeline creation.
        在导入媒体/创建时间线前初始化新工程 FPS。
        """
        setter = getattr(self.project, "SetSetting", None)
        if not callable(setter):
            self.logger.warning(
                "新工程不支持 SetSetting；保留默认 FPS / New project exposes no SetSetting; keeping default FPS"
            )
            return
        # FFprobe commonly reports exact NTSC rationals (59.94006), while
        # Resolve accepts their conventional decimal setting (59.94).
        # FFprobe 常返回精确 NTSC 小数，Resolve 工程设置使用常规三位小数。
        fps_text = self._decimal_text(fps.quantize(Decimal("0.001")))
        try:
            changed = setter("timelineFrameRate", fps_text)
        except Exception as exc:
            self.logger.warning(
                "设置新工程 FPS 失败：%s / Failed to set new-project FPS: %s",
                exc,
                exc,
            )
            return
        if changed:
            self.logger.info(
                "新工程 FPS 已设为 %s / New project FPS set to %s",
                fps_text,
                fps_text,
            )
        else:
            self.logger.warning(
                "Resolve 拒绝 FPS=%s，将读取实际值 / Resolve rejected FPS=%s; actual value will be used",
                fps_text,
                fps_text,
            )

    def configure_color_pipeline(self) -> None:
        """
        Configure Resolve Color Management before importing any source media.
        在导入任何素材前配置 Resolve 色彩管理。

        The exact setting values below are accepted by Resolve 21 on Windows.
        Every required write is checked so PP8 footage cannot silently render flat.

        下列设置值已在 Windows Resolve 21 实机验证。每一步都会检查返回值，避免
        PP8 素材在技术还原失败时仍悄悄输出灰片。
        """
        if not bool(self.color_pipeline.get("enabled")):
            return
        if str(self.color_pipeline.get("camera_profile") or "").casefold() != (
            "sony_pp8_slog3_sgamut3cine"
        ):
            raise ResolveExecutorError(
                "启用了未知色彩配置 / Unknown enabled color pipeline."
            )
        setter = getattr(self.project, "SetSetting", None)
        if not callable(setter):
            raise ResolveExecutorError(
                "当前 Resolve 工程不提供 SetSetting，无法安全还原 Sony PP8。"
                " / Resolve project does not expose SetSetting for PP8 color management."
            )
        settings = (
            ("colorScienceMode", "davinciYRGBColorManaged"),
            ("isAutoColorManage", "0"),
            ("separateColorSpaceAndGamma", "1"),
            ("colorSpaceInput", "Sony S-Gamut3.Cine"),
            ("colorSpaceInputGamma", "S-Log3"),
            ("colorSpaceTimeline", "DaVinci WG"),
            ("colorSpaceTimelineGamma", "DaVinci Intermediate"),
            ("colorSpaceOutput", "Rec.709"),
            ("colorSpaceOutputGamma", "Gamma 2.4"),
        )
        for key, value in settings:
            try:
                accepted = setter(key, value)
            except Exception as exc:
                raise ResolveExecutorError(
                    f"Resolve 色彩设置失败 {key}={value}: {exc}"
                ) from exc
            if accepted is False:
                raise ResolveExecutorError(
                    f"Resolve 拒绝关键色彩设置 {key}={value}；已停止，避免输出未还原 PP8。"
                    " / Resolve rejected a required color setting; stopped to prevent flat output."
                )
        self.logger.info(
            "Sony PP8 已还原：S-Gamut3.Cine/S-Log3 → DaVinci WG/Intermediate → Rec.709 Gamma 2.4"
        )

    def ensure_timeline(self) -> Any:
        """
        Reuse the current timeline or create/reuse the configured one.
        复用当前时间线，或创建/复用配置的时间线。
        """
        timeline = self.project.GetCurrentTimeline()
        if timeline is not None:
            self.logger.info(
                "使用当前时间线“%s” / Using current timeline '%s'",
                self._safe_name(timeline, "unnamed"),
                self._safe_name(timeline, "unnamed"),
            )
            return timeline

        count = self._safe_int_call(self.project, "GetTimelineCount")
        for index in range(1, count + 1):
            candidate = self.project.GetTimelineByIndex(index)
            if (
                candidate is not None
                and self._safe_name(candidate, "") == self.timeline_name
            ):
                if not self.project.SetCurrentTimeline(candidate):
                    raise ResolveExecutorError(
                        f"无法激活时间线 / Cannot activate timeline: {self.timeline_name}"
                    )
                return candidate
        try:
            timeline = self.media_pool.CreateEmptyTimeline(self.timeline_name)
        except Exception as exc:
            raise ResolveExecutorError(
                f"创建时间线失败 / Timeline creation failed: {exc}"
            ) from exc
        if timeline is None:
            # Some Resolve states reject CreateEmptyTimeline but accept the
            # documented CreateTimelineFromClips overload with an empty list.
            fallback = getattr(self.media_pool, "CreateTimelineFromClips", None)
            if callable(fallback):
                try:
                    timeline = fallback(self.timeline_name, [])
                except Exception:
                    timeline = None
        if timeline is None:
            raise ResolveExecutorError(
                f"无法创建/激活时间线 / Cannot create/activate timeline: {self.timeline_name}"
            )
        try:
            activated = self.project.SetCurrentTimeline(timeline)
        except Exception:
            activated = False
        if not activated:
            try:
                current = self.project.GetCurrentTimeline()
            except Exception:
                current = None
            if (
                current is None
                or self._safe_name(current, "")
                != self._safe_name(timeline, self.timeline_name)
            ):
                raise ResolveExecutorError(
                    f"时间线已创建但无法激活 / Timeline created but cannot be activated: {self.timeline_name}"
                )
        self.logger.info(
            "已创建时间线“%s” / Created timeline '%s'",
            self.timeline_name,
            self.timeline_name,
        )
        return timeline

    def get_active_fps(self) -> Decimal:
        """
        Read timeline FPS, falling back to project FPS.
        读取时间线 FPS；不可用时回退到工程 FPS。
        """
        for api_object, label in (
            (self.timeline, "timeline"),
            (self.project, "project"),
        ):
            getter = getattr(api_object, "GetSetting", None)
            if not callable(getter):
                continue
            try:
                fps = self._parse_fps(getter("timelineFrameRate"))
            except Exception:
                fps = None
            if fps is not None:
                self.logger.info(
                    "实际 FPS=%s（%s）/ Active FPS=%s (%s)",
                    self._decimal_text(fps),
                    label,
                    self._decimal_text(fps),
                    label,
                )
                return fps
        raise ResolveExecutorError(
            "无法读取 timelineFrameRate；请检查 Project Settings > Master Settings。"
            " / Could not read timelineFrameRate."
        )

    def compare_fps(self, json_fps: Decimal, resolve_fps: Decimal) -> None:
        """
        Warn or fail when JSON and Resolve FPS differ.
        JSON 与 Resolve FPS 不一致时警告或失败。
        """
        # Treat exact NTSC rational values from ffprobe as equal to Resolve's
        # rounded UI values (e.g. 59.94006 and 59.94).
        # 将 ffprobe 的精确 NTSC 值与 Resolve UI 的舍入值视为同一帧率。
        if abs(json_fps - resolve_fps) <= Decimal("0.001"):
            return
        message = (
            f"FPS 不一致：JSON={self._decimal_text(json_fps)}，"
            f"Resolve={self._decimal_text(resolve_fps)}；将以 Resolve 为准。"
            f" / FPS mismatch; Resolve FPS is authoritative."
        )
        if self.strict_fps:
            raise ResolveExecutorError(message)
        self.logger.warning(message)

    def prepare_clips(
        self, clips: Sequence[ClipDecision], fps: Decimal
    ) -> Sequence[Tuple[ClipDecision, Dict[str, Any]]]:
        """
        Resolve/import every media item and calculate frame ranges first.
        先定位/导入全部媒体并计算帧区间。

        This preflight avoids modifying the timeline when a later source file is
        missing. JSON out-points are treated as exclusive; Resolve ``endFrame``
        is inclusive, so one frame is subtracted after ceiling.

        预检可避免后续素材缺失时提前修改时间线。JSON 出点按不包含处理；
        Resolve ``endFrame`` 为包含式，因此向上取整后减一帧。
        """
        index = self._index_media_pool()
        prepared: List[Tuple[ClipDecision, Dict[str, Any]]] = []
        for decision in clips:
            item, index = self._resolve_media(decision, index)
            source_fps = self._media_fps(item) or fps
            start_frame, end_frame = self.seconds_to_frames(
                decision.cut_in_sec, decision.cut_out_sec, source_fps
            )
            prepared.append(
                (
                    decision,
                    {
                        "mediaPoolItem": item,
                        "startFrame": start_frame,
                        "endFrame": end_frame,
                    },
                )
            )
            self.logger.info(
                "预检 clip_id=%r：%s -> 帧 %d-%d / Prepared clip_id=%r: %s -> frames %d-%d",
                decision.clip_id,
                decision.file_name,
                start_frame,
                end_frame,
                decision.clip_id,
                decision.file_name,
                start_frame,
                end_frame,
            )
        return prepared

    def _media_fps(self, media_item: Any) -> Optional[Decimal]:
        """
        Read a source clip's native FPS, falling back to project FPS if absent.
        读取源片段原生 FPS；API 未提供时由调用方回退到工程 FPS。

        Parameters / 参数:
            media_item:
                Resolve MediaPoolItem selected for the decision.
                剪辑决策对应的 Resolve MediaPoolItem。
        """
        getter = getattr(media_item, "GetClipProperty", None)
        if not callable(getter):
            return None
        candidates: List[Any] = []
        for key in ("FPS", "Frame Rate"):
            try:
                candidates.append(getter(key))
            except Exception:
                pass
        try:
            properties = getter()
        except Exception:
            properties = None
        if isinstance(properties, dict):
            candidates.extend(
                properties.get(key) for key in ("FPS", "Frame Rate")
            )
        for value in candidates:
            parsed = self._parse_fps(value)
            if parsed is not None:
                return parsed
        return None

    @staticmethod
    def seconds_to_frames(
        cut_in_sec: Decimal, cut_out_sec: Decimal, fps: Decimal
    ) -> Tuple[int, int]:
        """
        Convert ``[in, out)`` seconds to inclusive Resolve source frames.
        将 ``[入点, 出点)`` 秒转换为 Resolve 包含式源帧。
        """
        if cut_in_sec < 0 or cut_out_sec <= cut_in_sec or fps <= 0:
            raise ResolveExecutorError(
                "无效的秒/FPS 参数 / Invalid seconds/FPS."
            )
        start = int((cut_in_sec * fps).to_integral_value(rounding=ROUND_FLOOR))
        end_exclusive = int(
            (cut_out_sec * fps).to_integral_value(rounding=ROUND_CEILING)
        )
        return start, max(start, end_exclusive - 1)

    def append_clips(
        self, prepared: Sequence[Tuple[ClipDecision, Dict[str, Any]]]
    ) -> Sequence[Any]:
        """
        Append clips one-by-one in JSON order with precise error context.
        按 JSON 顺序逐条追加，并提供精确错误上下文。
        """
        result_items: List[Any] = []
        completed = 0
        for decision, clip_info in prepared:
            try:
                result = self.media_pool.AppendToTimeline([clip_info])
            except Exception as exc:
                raise ResolveExecutorError(
                    self._append_error(decision, completed, str(exc))
                ) from exc
            items = self._coerce_items(result)
            if result is None or result is False or (
                not items and result is not True
            ):
                raise ResolveExecutorError(
                    self._append_error(
                        decision,
                        completed,
                        "AppendToTimeline returned no timeline item",
                    )
                )
            result_items.extend(items)
            for timeline_item in items:
                self.apply_clip_effects(timeline_item, decision)
            completed += 1
            self.logger.info(
                "已追加 %d/%d：clip_id=%r / Appended %d/%d: clip_id=%r",
                completed,
                len(prepared),
                decision.clip_id,
                completed,
                len(prepared),
                decision.clip_id,
            )
        return result_items

    def append_music_bed(
        self,
        fps: Decimal,
        prepared: Sequence[Tuple[ClipDecision, Dict[str, Any]]],
    ) -> Sequence[Any]:
        """
        Loop the AI-selected local music file on audio track 2.
        将 AI 从本地曲库选中的配乐循环追加到音轨 2。

        Parameters / 参数:
            fps: Active Resolve timeline frame rate. / 当前 Resolve 时间线帧率。
            prepared: Validated picture edits used to calculate program length.
                用于计算成片长度的已校验画面剪辑。
        """
        music_text = str(self.music_plan.get("file_name") or "").strip()
        if not music_text:
            self.logger.info(
                "未提供本地授权配乐库；本次不添加音乐 / No local licensed music selected"
            )
            return []
        path = Path(music_text).expanduser().resolve()
        if not path.is_file():
            raise ResolveExecutorError(
                f"找不到导演选择的配乐 / Selected music not found: {path}"
            )
        imported = self._import_media(path)
        if len(imported) != 1:
            matches = self._items_for_path(path)
            if len(matches) != 1:
                raise ResolveExecutorError(
                    f"无法唯一导入配乐 / Could not uniquely import music: {path}"
                )
            music_item = matches[0]
        else:
            music_item = imported[0]
        music_seconds = self._probe_media_duration(path)
        if music_seconds <= 0:
            raise ResolveExecutorError(
                f"无法读取配乐时长 / Could not read music duration: {path}"
            )
        program_frames = sum(
            max(
                1,
                int(
                    ((decision.cut_out_sec - decision.cut_in_sec) * fps).to_integral_value(
                        rounding=ROUND_CEILING
                    )
                ),
            )
            for decision, _ in prepared
        )
        music_frames = max(
            1,
            int((Decimal(str(music_seconds)) * fps).to_integral_value(rounding=ROUND_CEILING)),
        )
        try:
            audio_tracks = int(self.timeline.GetTrackCount("audio") or 0)
        except Exception:
            audio_tracks = 1
        add_track = getattr(self.timeline, "AddTrack", None)
        while audio_tracks < 2 and callable(add_track):
            if add_track("audio", "stereo") is False:
                break
            audio_tracks += 1
        if audio_tracks < 2:
            raise ResolveExecutorError(
                "无法创建配乐音轨 2 / Could not create music audio track 2."
            )
        try:
            record_frame = int(self.timeline.GetStartFrame() or 0)
        except Exception:
            record_frame = 0
        appended: List[Any] = []
        cursor = 0
        while cursor < program_frames:
            segment_frames = min(music_frames, program_frames - cursor)
            clip_info = {
                "mediaPoolItem": music_item,
                "startFrame": 0,
                "endFrame": segment_frames - 1,
                "mediaType": 2,
                "trackIndex": 2,
                "recordFrame": record_frame + cursor,
            }
            result = self.media_pool.AppendToTimeline([clip_info])
            items = self._coerce_items(result)
            if result is None or result is False or (not items and result is not True):
                raise ResolveExecutorError(
                    "Resolve 无法把配乐追加到音轨 2 / Could not append music to track 2."
                )
            appended.extend(items)
            cursor += segment_frames
        level = max(
            Decimal("-36"),
            min(
                Decimal("-6"),
                self._decimal(
                    self.music_plan.get("target_level_db", -20),
                    "music_plan.target_level_db",
                ),
            ),
        )
        for timeline_item in appended:
            setter = getattr(timeline_item, "SetProperty", None)
            if callable(setter):
                try:
                    setter("Volume", float(level))
                except Exception:
                    pass
        self.logger.info(
            "已添加配乐：%s（音轨 2，%.1f dB）/ Music added on track 2",
            path.name, float(level),
        )
        return appended

    @staticmethod
    def _probe_media_duration(path: Path) -> float:
        """Read media duration with ffprobe. / 使用 ffprobe 读取媒体时长。"""
        ffprobe = shutil.which("ffprobe")
        if not ffprobe:
            return 0.0
        try:
            completed = subprocess.run(
                [
                    ffprobe, "-v", "error", "-show_entries", "format=duration",
                    "-of", "default=noprint_wrappers=1:nokey=1", str(path),
                ],
                capture_output=True,
                text=True,
                timeout=30,
                check=False,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            return float((completed.stdout or "0").strip()) if completed.returncode == 0 else 0.0
        except (OSError, ValueError, subprocess.SubprocessError):
            return 0.0

    def apply_clip_effects(self, timeline_item: Any, decision: ClipDecision) -> None:
        """
        Apply Resolve-supported parts of the AI effect plan without breaking assembly.
        应用 Resolve 公共 API 支持的 AI 效果；单个效果失败时仍保留已组装时间线。

        Parameters / 参数:
            timeline_item:
                Timeline item returned by ``AppendToTimeline``.
                ``AppendToTimeline`` 返回的时间线片段。
            decision:
                Validated editorial and effect decision for this clip.
                已校验的片段剪辑与效果决策。

        Resolve's public scripting API does not expose a stable generic
        transition-insertion method. Transition intent is stored in a marker
        and rendered exactly by ``review_renderer``.
        Resolve 公共脚本 API 没有稳定的通用转场插入方法；转场意图会写入标记，
        并由 ``review_renderer`` 精确生成到可观看预览中。
        """
        voice_isolation = getattr(timeline_item, "SetVoiceIsolationState", None)
        if decision.audio_cleanup != "none" and callable(voice_isolation):
            amount = 75.0 if decision.audio_cleanup == "strong" else 45.0
            try:
                applied = voice_isolation({"isEnabled": True, "amount": amount})
                if applied is False:
                    self.logger.warning(
                        "Voice Isolation unavailable for clip_id=%r; "
                        "the FFmpeg preview still applies denoise.",
                        decision.clip_id,
                    )
            except Exception as exc:
                self.logger.warning(
                    "Voice Isolation failed for clip_id=%r: %s",
                    decision.clip_id,
                    exc,
                )

        set_property = getattr(timeline_item, "SetProperty", None)
        if decision.motion == "gentle_push_in" and callable(set_property):
            try:
                applied = set_property(
                    {"ZoomX": 1.04, "ZoomY": 1.04, "ZoomGang": True}
                )
                if applied is False:
                    self.logger.warning(
                        "Resolve could not apply push-in to clip_id=%r",
                        decision.clip_id,
                    )
            except Exception as exc:
                self.logger.warning(
                    "Resolve motion effect failed for clip_id=%r: %s",
                    decision.clip_id,
                    exc,
                )

        if decision.volume_db != 0:
            self._apply_native_clip_volume(timeline_item, decision)

        color_values = {
            "neutral": {
                "Slope": "1.02 1.02 1.02", "Offset": "0 0 0",
                "Power": "1 1 1", "Saturation": "1.02",
            },
            "warm": {
                "Slope": "1.05 1.02 0.98", "Offset": "0 0 0",
                "Power": "1 1 1.01", "Saturation": "1.04",
            },
            "cool": {
                "Slope": "0.98 1.01 1.05", "Offset": "0 0 0",
                "Power": "1.01 1 1", "Saturation": "1.02",
            },
            "contrast": {
                "Slope": "1.06 1.06 1.06", "Offset": "-0.01 -0.01 -0.01",
                "Power": "0.98 0.98 0.98", "Saturation": "1.05",
            },
        }
        set_cdl = getattr(timeline_item, "SetCDL", None)
        if decision.color_look in color_values and callable(set_cdl):
            cdl = {"NodeIndex": "1", **color_values[decision.color_look]}
            try:
                if set_cdl(cdl) is False:
                    self.logger.warning(
                        "Resolve could not apply CDL look to clip_id=%r",
                        decision.clip_id,
                    )
            except Exception as exc:
                self.logger.warning(
                    "Resolve color look failed for clip_id=%r: %s",
                    decision.clip_id,
                    exc,
                )

        if decision.drx_preset != "none":
            self._apply_drx_preset(timeline_item, decision)

        if decision.stabilization == "auto":
            self._call_ai_clip_method(
                timeline_item,
                "Stabilize",
                decision,
                "stabilization / 防抖",
            )

        magic_mask_modes = {
            "magic_mask_forward": "F",
            "magic_mask_backward": "B",
            "magic_mask_bidirectional": "BI",
        }
        if decision.tracking in magic_mask_modes:
            self._call_ai_clip_method(
                timeline_item,
                "CreateMagicMask",
                decision,
                "Magic Mask tracking / 魔法遮罩跟踪",
                magic_mask_modes[decision.tracking],
            )

        if decision.smart_reframe:
            self._call_ai_clip_method(
                timeline_item,
                "SmartReframe",
                decision,
                "Smart Reframe / 智能重构图",
            )

        add_marker = getattr(timeline_item, "AddMarker", None)
        if callable(add_marker):
            effect_plan = {
                "transition_to_next": decision.transition_to_next,
                "transition_duration_sec": self._decimal_text(
                    decision.transition_duration_sec
                ),
                "audio_cleanup": decision.audio_cleanup,
                "color_look": decision.color_look,
                "motion": decision.motion,
                "volume_db": self._decimal_text(decision.volume_db),
                "drx_preset": decision.drx_preset,
                "stabilization": decision.stabilization,
                "tracking": decision.tracking,
                "smart_reframe": decision.smart_reframe,
            }
            try:
                add_marker(
                    0,
                    "Cyan",
                    "CyberEditor AI",
                    decision.reason_for_cut or "AI-selected clip",
                    1,
                    json.dumps(effect_plan, ensure_ascii=False),
                )
            except Exception as exc:
                self.logger.debug(
                    "Could not add AI marker for clip_id=%r: %s",
                    decision.clip_id,
                    exc,
                )

    def _apply_native_clip_volume(
        self, timeline_item: Any, decision: ClipDecision
    ) -> None:
        """
        Apply clip gain only when this Resolve build exposes an audio property.
        仅当当前 Resolve 版本公开音频属性时应用片段增益。

        Resolve 21 documents video transform keys but not a stable audio-level
        key. Some builds/plugins expose one dynamically through ``GetProperty``;
        probing the returned dictionary avoids writing an unsupported key.

        Resolve 21 文档只保证视频变换键，并未承诺稳定的音量键；部分版本或插件会
        通过 ``GetProperty`` 动态公开该属性，因此先探测再写入，避免误调用。
        """
        getter = getattr(timeline_item, "GetProperty", None)
        setter = getattr(timeline_item, "SetProperty", None)
        if not callable(getter) or not callable(setter):
            self.logger.warning(
                "Resolve has no scriptable clip-volume property for clip_id=%r; "
                "the requested %.2f dB remains recorded in the AI marker.",
                decision.clip_id,
                float(decision.volume_db),
            )
            return
        try:
            properties = getter()
        except Exception as exc:
            self.logger.warning(
                "Could not inspect clip-volume properties for clip_id=%r: %s",
                decision.clip_id,
                exc,
            )
            return
        if not isinstance(properties, dict):
            properties = {}
        by_normalized = {
            re.sub(r"[^a-z]", "", str(key).casefold()): str(key)
            for key in properties
        }
        property_key = next(
            (
                by_normalized[key]
                for key in ("audiolevel", "clipvolume", "volume")
                if key in by_normalized
            ),
            None,
        )
        if property_key is None:
            self.logger.warning(
                "Resolve %s does not expose per-clip volume through its public API; "
                "clip_id=%r keeps %.2f dB as metadata for macro/manual fallback.",
                self._resolve_version(),
                decision.clip_id,
                float(decision.volume_db),
            )
            return
        try:
            if setter(property_key, float(decision.volume_db)) is False:
                raise RuntimeError("SetProperty returned False")
        except Exception as exc:
            self.logger.warning(
                "Clip volume failed for clip_id=%r: %s", decision.clip_id, exc
            )

    def _apply_drx_preset(
        self, timeline_item: Any, decision: ClipDecision
    ) -> None:
        """
        Apply a user-exported DRX preset through the clip node graph.
        通过片段节点图应用用户从 Resolve 导出的 DRX 预设。

        The logical preset name is constrained by JSON validation and resolved
        below ``drx_root``; arbitrary model-produced paths are never opened.
        逻辑预设名已由 JSON 校验限制，只会在 ``drx_root`` 下解析，绝不打开模型
        随意生成的路径。
        """
        preset = (self.drx_root / f"{decision.drx_preset}.drx").resolve()
        try:
            preset.relative_to(self.drx_root.resolve())
        except ValueError:
            self.logger.warning("Rejected unsafe DRX path: %s", preset)
            return
        if not preset.is_file():
            self.logger.warning(
                "DRX preset %r was requested for clip_id=%r but is not installed: %s",
                decision.drx_preset,
                decision.clip_id,
                preset,
            )
            return
        get_graph = getattr(timeline_item, "GetNodeGraph", None)
        try:
            graph = get_graph() if callable(get_graph) else None
            apply_drx = getattr(graph, "ApplyGradeFromDRX", None)
            if not callable(apply_drx):
                raise RuntimeError("GetNodeGraph().ApplyGradeFromDRX is unavailable")
            if apply_drx(str(preset), 0) is False:
                raise RuntimeError("ApplyGradeFromDRX returned False")
            self.logger.info(
                "Applied DRX %s to clip_id=%r", decision.drx_preset, decision.clip_id
            )
        except Exception as exc:
            self.logger.warning(
                "DRX apply failed for clip_id=%r: %s", decision.clip_id, exc
            )

    def _call_ai_clip_method(
        self,
        timeline_item: Any,
        method_name: str,
        decision: ClipDecision,
        label: str,
        *args: Any,
    ) -> None:
        """
        Run one potentially long Resolve AI operation with explicit diagnostics.
        运行一个可能耗时较长的 Resolve AI 操作，并给出明确诊断。

        Parameters / 参数:
            timeline_item: Target Resolve timeline item. / 目标时间线片段。
            method_name: Public Resolve API method. / Resolve 公共 API 方法名。
            decision: Validated clip decision. / 已校验的片段决策。
            label: Bilingual log label. / 双语日志标签。
            args: Positional method arguments. / 方法位置参数。
        """
        method = getattr(timeline_item, method_name, None)
        if not callable(method):
            self.logger.warning(
                "%s API is unavailable for clip_id=%r", label, decision.clip_id
            )
            return
        self.logger.info("Starting %s for clip_id=%r", label, decision.clip_id)
        try:
            result = method(*args)
        except Exception as exc:
            self.logger.warning(
                "%s failed for clip_id=%r: %s", label, decision.clip_id, exc
            )
            return
        if result is False:
            self.logger.warning(
                "%s returned False for clip_id=%r. The feature may require "
                "Resolve Studio, supported hardware, or an installed AI Extra.",
                label,
                decision.clip_id,
            )
        else:
            self.logger.info("Completed %s for clip_id=%r", label, decision.clip_id)

    def apply_timeline_audio_preset(self) -> None:
        """
        Apply an optional user-created Fairlight preset to the current timeline.
        将可选的用户自建 Fairlight 预设应用到当前时间线。
        """
        if not self.fairlight_preset:
            return
        method = getattr(self.project, "ApplyFairlightPresetToCurrentTimeline", None)
        if not callable(method):
            raise ResolveExecutorError(
                "当前 Resolve 不支持 Fairlight 预设脚本接口 / "
                "This Resolve build does not expose the Fairlight preset API."
            )
        try:
            applied = method(self.fairlight_preset)
        except Exception as exc:
            raise ResolveExecutorError(
                f"Fairlight 预设应用失败 / Fairlight preset failed: {exc}"
            ) from exc
        if applied is False:
            raise ResolveExecutorError(
                f"找不到或无法应用 Fairlight 预设 / Cannot apply Fairlight preset: "
                f"{self.fairlight_preset!r}"
            )
        self.logger.info("Applied Fairlight preset: %s", self.fairlight_preset)

    def render_final(self) -> Dict[str, Any]:
        """
        Queue, start, monitor, and validate one final Resolve render job.
        创建、启动、监控并校验一个 Resolve 最终渲染任务。

        Returns / 返回:
            Final render-job status dictionary. / 最终渲染任务状态字典。
        """
        self.render_dir.mkdir(parents=True, exist_ok=True)
        if self.render_preset:
            try:
                loaded = self.project.LoadRenderPreset(self.render_preset)
            except Exception as exc:
                raise ResolveExecutorError(
                    f"无法加载渲染预设 / Cannot load render preset: {exc}"
                ) from exc
            if loaded is False:
                available = self.project.GetRenderPresetList() or []
                raise ResolveExecutorError(
                    f"渲染预设不存在 / Render preset not found: {self.render_preset!r}. "
                    f"Available: {available}"
                )
        settings = {
            "SelectAllFrames": True,
            "TargetDir": str(self.render_dir),
            "CustomName": self.render_name,
            "ExportVideo": True,
            "ExportAudio": True,
        }
        try:
            if self.project.SetRenderSettings(settings) is False:
                raise RuntimeError("SetRenderSettings returned False")
            job_id = self.project.AddRenderJob()
        except Exception as exc:
            raise ResolveExecutorError(
                f"无法创建渲染任务 / Cannot create render job: {exc}"
            ) from exc
        if not job_id:
            raise ResolveExecutorError(
                "Resolve 未返回渲染任务 ID / Resolve returned no render job id."
            )
        try:
            started = self.project.StartRendering([job_id], False)
        except Exception as exc:
            raise ResolveExecutorError(
                f"无法启动最终渲染 / Cannot start final render: {exc}"
            ) from exc
        if started is False:
            raise ResolveExecutorError(
                "Resolve 拒绝启动最终渲染 / Resolve refused to start rendering."
            )
        deadline = time.monotonic() + self.render_timeout
        last_percent = -1
        while True:
            try:
                status = self.project.GetRenderJobStatus(job_id) or {}
            except Exception as exc:
                raise ResolveExecutorError(
                    f"无法读取渲染状态 / Cannot read render status: {exc}"
                ) from exc
            percent = int(float(status.get("CompletionPercentage", 0) or 0))
            if percent >= last_percent + 5 or percent == 100:
                self.logger.info(
                    "最终渲染 %d%% / Final render %d%%", percent, percent
                )
                last_percent = percent
            state = str(status.get("JobStatus") or "").casefold()
            complete_states = {
                "complete",
                "completed",
                "完成",
                "已完成",
                "成功",
                "渲染完成",
            }
            failed_states = {
                "failed",
                "cancelled",
                "canceled",
                "失败",
                "渲染失败",
                "取消",
                "已取消",
                "错误",
            }
            if state in complete_states:
                self.logger.info("Final render complete: %s", self.render_dir)
                return dict(status)
            if state in failed_states:
                raise ResolveExecutorError(
                    f"最终渲染失败 / Final render failed: {status}"
                )
            if time.monotonic() >= deadline:
                try:
                    self.project.StopRendering()
                finally:
                    raise ResolveExecutorError(
                        f"最终渲染超过 {self.render_timeout:.0f} 秒，已停止 / "
                        "Final render timed out and was stopped."
                    )
            try:
                in_progress = bool(self.project.IsRenderingInProgress())
            except Exception:
                in_progress = True
            # Resolve localizes JobStatus (for example Chinese returns “完成”).
            # A stopped job at 100% is also unambiguously complete even when a
            # future locale uses a status string unknown to this client.
            # Resolve 会本地化 JobStatus；任务停止且进度为 100% 时可可靠视为完成。
            if not in_progress and percent >= 100:
                self.logger.info("Final render complete: %s", self.render_dir)
                return dict(status)
            if not in_progress:
                raise ResolveExecutorError(
                    f"渲染提前停止 / Rendering stopped unexpectedly: {status}"
                )
            time.sleep(1.0)

    def run_macro_fallback(self) -> None:
        """
        Run an explicit guarded UI action only when a profile was supplied.
        仅当用户提供配置时运行显式指定的受保护 UI 动作。
        """
        if self.macro_profile is None:
            return
        if not self.macro_action:
            raise ResolveExecutorError(
                "宏动作名不能为空 / Macro action name cannot be empty."
            )
        try:
            from .resolve_macro import ResolveMacroError, SafeResolveMacroRunner
        except ImportError:  # pragma: no cover - direct script fallback.
            from resolve_macro import ResolveMacroError, SafeResolveMacroRunner
        try:
            SafeResolveMacroRunner(self.macro_profile, self.logger).run(
                self.macro_action
            )
        except ResolveMacroError as exc:
            raise ResolveExecutorError(
                f"Resolve UI 后备宏失败 / Resolve UI fallback failed: {exc}"
            ) from exc

    def _resolve_version(self) -> str:
        """Return a safe Resolve version string. / 安全返回 Resolve 版本字符串。"""
        method = getattr(self.resolve, "GetVersionString", None)
        try:
            return str(method()) if callable(method) else "unknown"
        except Exception:
            return "unknown"

    def save_project(self) -> None:
        """Persist the completed Resolve project if possible. / 尽可能保存已完成的 Resolve 工程。"""
        try:
            saved = self.project_manager.SaveProject()
        except Exception as exc:
            self.logger.warning(
                "剪辑已完成，但保存异常：%s / Editing complete, save failed: %s",
                exc,
                exc,
            )
            return
        if not saved:
            self.logger.warning(
                "剪辑已完成，但请手动保存工程 / Editing complete; save manually"
            )

    def _resolve_media(
        self, decision: ClipDecision, index: Sequence[MediaRecord]
    ) -> Tuple[Any, Sequence[MediaRecord]]:
        """Find/import one media item without silently choosing duplicates. / 查找/导入一个媒体条目，且不静默选择重名项。"""
        requested = Path(decision.file_name).expanduser()
        if not requested.is_absolute():
            requested = self.media_root / requested
        requested = requested.resolve()
        normalized = self._normalize_path(requested)
        exact = [
            record
            for record in index
            if record.file_path
            and self._normalize_path(record.file_path) == normalized
        ]
        if len(exact) == 1:
            return exact[0].item, index
        if len(exact) > 1:
            raise ResolveExecutorError(
                f"同一路径在媒体池中出现多次 / Duplicate exact path: {requested}"
            )

        base_name = Path(decision.file_name).name.casefold()
        same_name = [
            record for record in index if record.name.casefold() == base_name
        ]
        if requested.is_file():
            imported = self._import_media(requested)
            refreshed = self._index_media_pool()
            exact = [
                record
                for record in refreshed
                if record.file_path
                and self._normalize_path(record.file_path) == normalized
            ]
            if len(exact) == 1:
                return exact[0].item, refreshed
            if len(imported) == 1:
                return imported[0], refreshed
            raise ResolveExecutorError(
                f"导入后无法唯一识别媒体 / Cannot identify imported media: {requested}"
            )
        if len(same_name) == 1:
            self.logger.warning(
                "磁盘路径不存在，使用媒体池唯一同名条目：%s / Disk path missing; using unique Media Pool name: %s",
                decision.file_name,
                decision.file_name,
            )
            return same_name[0].item, index
        if len(same_name) > 1:
            candidates = ", ".join(
                record.file_path or record.name for record in same_name
            )
            raise ResolveExecutorError(
                f"素材名不唯一：{decision.file_name}。请提供路径或 --media-root。"
                f" / Ambiguous media name. Candidates: {candidates}"
            )
        raise ResolveExecutorError(
            f"找不到 clip_id={decision.clip_id!r} 的素材：{decision.file_name}\n"
            f"Media not found. Checked: {requested}"
        )

    def _import_media(self, path: Path) -> Sequence[Any]:
        """Import through current API, then legacy MediaStorage fallbacks. / 通过当前 API 导入，并兼容旧版 MediaStorage。"""
        errors: List[str] = []
        importer = getattr(self.media_pool, "ImportMedia", None)
        if callable(importer):
            try:
                items = self._coerce_items(importer([str(path)]))
                if items:
                    return items
                rescanned = self._items_for_path(path)
                if rescanned:
                    return rescanned
                errors.append("MediaPool.ImportMedia returned no item")
            except Exception as exc:
                errors.append(f"MediaPool.ImportMedia: {exc}")

        try:
            storage = self.resolve.GetMediaStorage()
        except Exception as exc:
            storage = None
            errors.append(f"GetMediaStorage: {exc}")
        if storage is not None:
            for name in ("AddItemListToMediaPool", "AddItemsToMediaPool"):
                method = getattr(storage, name, None)
                if not callable(method):
                    continue
                try:
                    items = self._coerce_items(method([str(path)]))
                    if items:
                        return items
                    rescanned = self._items_for_path(path)
                    if rescanned:
                        return rescanned
                    errors.append(f"{name} returned no item")
                except Exception as exc:
                    errors.append(f"{name}: {exc}")
        raise ResolveExecutorError(
            f"Resolve 无法导入媒体 / Cannot import media: {path}\n"
            f"API details: {' | '.join(errors)}"
        )

    def _index_media_pool(self) -> Sequence[MediaRecord]:
        """Recursively index current and legacy Media Pool folder APIs. / 递归索引新旧版媒体池文件夹 API。"""
        root = self.media_pool.GetRootFolder()
        if root is None:
            raise ResolveExecutorError(
                "无法获取媒体池根目录 / Cannot obtain Media Pool root."
            )
        stack = [root]
        visited = set()
        records: List[MediaRecord] = []
        while stack:
            folder = stack.pop()
            identity = id(folder)
            if identity in visited:
                continue
            visited.add(identity)
            for item in self._folder_items(folder, "GetClipList", "GetClips"):
                name, file_path = self._media_identity(item)
                if name:
                    records.append(MediaRecord(item, name, file_path))
            stack.extend(
                self._folder_items(
                    folder, "GetSubFolderList", "GetSubFolders"
                )
            )
        return records

    def _items_for_path(self, path: Path) -> Sequence[Any]:
        """Return Media Pool objects matching one exact path. / 返回与一个精确路径匹配的媒体池对象。"""
        normalized = self._normalize_path(path)
        return [
            record.item
            for record in self._index_media_pool()
            if record.file_path
            and self._normalize_path(record.file_path) == normalized
        ]

    def _load_resolve_module(
        self,
    ) -> Tuple[Optional[Any], Sequence[Path], Sequence[str]]:
        """Locate and import Resolve's bundled Python module. / 定位并导入 Resolve 自带 Python 模块。"""
        checked: List[Path] = []
        errors: List[str] = []
        try:
            return importlib.import_module("DaVinciResolveScript"), checked, errors
        except Exception as exc:
            errors.append(f"normal import: {type(exc).__name__}: {exc}")

        for candidate in self._module_paths():
            if candidate in checked:
                continue
            checked.append(candidate)
            if not (candidate / "DaVinciResolveScript.py").is_file():
                continue
            candidate_text = str(candidate)
            if candidate_text not in sys.path:
                sys.path.insert(0, candidate_text)
            importlib.invalidate_caches()
            try:
                return (
                    importlib.import_module("DaVinciResolveScript"),
                    checked,
                    errors,
                )
            except Exception as exc:
                errors.append(f"{candidate}: {type(exc).__name__}: {exc}")
        return None, checked, errors

    @staticmethod
    def _module_paths() -> Sequence[Path]:
        """Return environment and standard Resolve module paths. / 返回环境变量及标准 Resolve 模块路径。"""
        paths: List[Path] = []
        configured = os.environ.get("RESOLVE_SCRIPT_API")
        if configured:
            root = Path(configured).expanduser()
            paths.extend((root / "Modules", root))
        program_data = Path(os.environ.get("PROGRAMDATA", r"C:\ProgramData"))
        paths.append(
            program_data
            / "Blackmagic Design"
            / "DaVinci Resolve"
            / "Support"
            / "Developer"
            / "Scripting"
            / "Modules"
        )
        return paths

    @staticmethod
    def _configure_resolve_library(
        executable: Optional[Path] = None,
    ) -> None:
        """
        Populate ``RESOLVE_SCRIPT_LIB`` from default or custom installs.
        从默认或自定义安装位置填充 ``RESOLVE_SCRIPT_LIB``。
        """
        if os.environ.get("RESOLVE_SCRIPT_LIB"):
            return
        root = Path(os.environ.get("PROGRAMFILES", r"C:\Program Files"))
        candidates: List[Path] = []
        if executable is not None:
            candidates.extend(
                (
                    executable.parent / "fusionscript.dll",
                    executable.parent / "Fusion" / "fusionscript.dll",
                )
            )
        candidates.extend((
            root
            / "Blackmagic Design"
            / "DaVinci Resolve"
            / "fusionscript.dll",
            root
            / "Blackmagic Design"
            / "DaVinci Resolve"
            / "Fusion"
            / "fusionscript.dll",
        ))
        for candidate in candidates:
            if candidate.is_file():
                os.environ["RESOLVE_SCRIPT_LIB"] = str(candidate)
                return

    @staticmethod
    def _try_scriptapp(module: Any) -> Any:
        """
        Try one non-throwing ``scriptapp('Resolve')`` connection.
        尝试一次不向外抛异常的 ``scriptapp('Resolve')`` 连接。
        """
        try:
            return module.scriptapp("Resolve")
        except Exception:
            return None

    @staticmethod
    def _launch_resolve(executable: Path) -> subprocess.Popen:
        """
        Start Resolve visibly so the user can see splash/license errors.
        以可见方式启动 Resolve，便于用户查看启动或许可证错误。
        """
        try:
            return subprocess.Popen(
                [str(executable)],
                cwd=str(executable.parent),
                close_fds=True,
                creationflags=getattr(
                    subprocess, "CREATE_NEW_PROCESS_GROUP", 0
                ),
            )
        except OSError as exc:
            raise ResolveExecutorError(
                f"自动启动 Resolve 失败 / Failed to auto-start Resolve: {exc}"
            ) from exc

    @staticmethod
    def _is_resolve_running() -> Optional[bool]:
        """Check Resolve.exe using Windows tasklist; return None on inspection failure. / 使用 tasklist 检查 Resolve.exe；检查失败返回 None。"""
        try:
            completed = subprocess.run(
                [
                    "tasklist",
                    "/FI",
                    "IMAGENAME eq Resolve.exe",
                    "/FO",
                    "CSV",
                    "/NH",
                ],
                capture_output=True,
                # `tasklist` writes in the active Windows console/OEM code
                # page, which is not necessarily UTF-8 on Chinese systems.
                # The process image name is ASCII, so inspect raw bytes and
                # avoid locale-dependent decoding entirely.
                # tasklist 使用 Windows 当前控制台/OEM 代码页输出，中文系统
                # 不一定是 UTF-8。进程名为 ASCII，直接检查原始字节最稳妥。
                text=False,
                timeout=10,
                check=False,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        except (OSError, subprocess.SubprocessError):
            return None
        if completed.returncode != 0:
            return None
        stdout = completed.stdout or b""
        if isinstance(stdout, str):
            # Supports subprocess-compatible test doubles and unusual wrappers.
            return "resolve.exe" in stdout.casefold()
        return b"resolve.exe" in bytes(stdout).lower()

    @staticmethod
    def _folder_items(
        folder: Any, current_name: str, legacy_name: str
    ) -> Sequence[Any]:
        """Read one Resolve collection across API versions. / 跨 API 版本读取一个 Resolve 集合。"""
        for method_name in (current_name, legacy_name):
            method = getattr(folder, method_name, None)
            if not callable(method):
                continue
            try:
                result = method()
            except Exception:
                continue
            if result is not None:
                return DaVinciExecutor._coerce_items(result)
        return []

    @staticmethod
    def _media_identity(item: Any) -> Tuple[str, str]:
        """Read display name and file path from a Media Pool item. / 读取媒体池条目的显示名与文件路径。"""
        properties: Dict[str, Any] = {}
        try:
            value = item.GetClipProperty()
            if isinstance(value, dict):
                properties = value
        except Exception:
            pass
        try:
            name = str(item.GetName() or "")
        except Exception:
            name = ""
        if not name:
            name = str(
                properties.get("Clip Name")
                or properties.get("File Name")
                or ""
            )
        return name, str(properties.get("File Path") or "")

    @staticmethod
    def _project_names(manager: Any) -> Sequence[str]:
        """Read project names across current/deprecated APIs. / 跨新旧 API 读取工程名称。"""
        for name in ("GetProjectListInCurrentFolder", "GetProjectsInCurrentFolder"):
            method = getattr(manager, name, None)
            if not callable(method):
                continue
            try:
                result = method()
            except Exception:
                continue
            if isinstance(result, dict):
                return [str(item) for item in result.values()]
            if isinstance(result, (list, tuple)):
                return [str(item) for item in result]
        return []

    @staticmethod
    def _unique_name(base: str, existing_names: Iterable[str]) -> str:
        """Add a numeric suffix until a name is unique. / 添加数字后缀直至名称唯一。"""
        names = {name.casefold() for name in existing_names}
        if base.casefold() not in names:
            return base
        suffix = 2
        while f"{base} {suffix}".casefold() in names:
            suffix += 1
        return f"{base} {suffix}"

    @staticmethod
    def _safe_name(value: Any, fallback: str) -> str:
        """Read GetName without failing diagnostics. / 安全读取 GetName。"""
        try:
            return str(value.GetName() or fallback)
        except Exception:
            return fallback

    @staticmethod
    def _safe_int_call(value: Any, method_name: str) -> int:
        """Call a no-argument API method and coerce to a non-negative int. / 调用无参 API 并转换为非负整数。"""
        try:
            return max(0, int(getattr(value, method_name)()))
        except Exception:
            return 0

    @staticmethod
    def _coerce_items(value: Any) -> List[Any]:
        """Normalize Resolve list/dict/single-object variants. / 规范化 Resolve 列表/字典/单对象变体。"""
        if value is None or isinstance(value, bool):
            return []
        if isinstance(value, dict):
            return list(value.values())
        if isinstance(value, (list, tuple)):
            return list(value)
        return [value]

    @staticmethod
    def _normalize_path(path: os.PathLike) -> str:
        """Normalize a path for case-insensitive Windows comparison. / 规范化路径以进行 Windows 不区分大小写比较。"""
        return os.path.normcase(
            os.path.abspath(os.path.normpath(str(path)))
        ).casefold()

    @staticmethod
    def _parse_fps(value: Any) -> Optional[Decimal]:
        """Parse Resolve FPS strings such as ``29.97 DF``. / 解析 ``29.97 DF`` 等 Resolve FPS 字符串。"""
        if value is None or isinstance(value, bool):
            return None
        match = re.match(r"^\s*(\d+(?:\.\d+)?)", str(value))
        if not match:
            return None
        try:
            fps = Decimal(match.group(1))
        except InvalidOperation:
            return None
        return fps if fps.is_finite() and fps > 0 else None

    @staticmethod
    def _decimal(value: Any, field_name: str) -> Decimal:
        """Convert a finite JSON numeric value. / 转换有限 JSON 数值。"""
        if value is None or isinstance(value, bool):
            raise ResolveExecutorError(f"{field_name} 必须是数值 / must be numeric.")
        try:
            converted = Decimal(str(value))
        except (InvalidOperation, ValueError):
            raise ResolveExecutorError(
                f"{field_name} 不是有效数值 / is not numeric: {value!r}"
            )
        if not converted.is_finite():
            raise ResolveExecutorError(
                f"{field_name} 必须是有限数值 / must be finite."
            )
        return converted

    @classmethod
    def _positive_decimal(cls, value: Any, field_name: str) -> Decimal:
        """Validate a positive Decimal. / 校验正 Decimal。"""
        converted = cls._decimal(value, field_name)
        if converted <= 0:
            raise ResolveExecutorError(f"{field_name} 必须大于 0 / must be positive.")
        return converted

    @classmethod
    def _non_negative_decimal(cls, value: Any, field_name: str) -> Decimal:
        """Validate a non-negative Decimal. / 校验非负 Decimal。"""
        converted = cls._decimal(value, field_name)
        if converted < 0:
            raise ResolveExecutorError(
                f"{field_name} 不能为负数 / cannot be negative."
            )
        return converted

    @staticmethod
    def _enum_text(value: Any, allowed: set, default: str) -> str:
        """Normalize an optional JSON enum. / 规范化可选 JSON 枚举值。"""
        normalized = str(value or default).strip().lower()
        return normalized if normalized in allowed else default

    @staticmethod
    def _decimal_text(value: Decimal) -> str:
        """Format Decimal without redundant trailing zeros. / 格式化 Decimal 并去除多余尾零。"""
        text = format(value, "f")
        return text.rstrip("0").rstrip(".") if "." in text else text

    @staticmethod
    def _append_error(
        decision: ClipDecision, completed: int, detail: str
    ) -> str:
        """Build a partial-state-aware append error. / 构建包含部分完成状态的追加错误。"""
        state = (
            f"已有 {completed} 个片段成功追加；Resolve API 无事务回滚，请检查并手动撤销。"
            f" / {completed} earlier clips were appended; inspect and undo manually."
            if completed
            else "尚未追加片段 / No clip was appended."
        )
        return (
            f"追加 clip_id={decision.clip_id!r} 失败：{decision.file_name}\n"
            f"Append failed. {state}\nAPI details: {detail}"
        )


def configure_logging(level: str = "INFO") -> logging.Logger:
    """Configure standalone Resolve logging. / 配置独立 Resolve 日志。"""
    logger = logging.getLogger(LOGGER_NAME)
    logger.handlers.clear()
    logger.propagate = False
    logger.setLevel(getattr(logging, level.upper()))
    handler = logging.StreamHandler()
    handler.setFormatter(
        logging.Formatter("%(asctime)s | %(levelname)s | %(name)s | %(message)s")
    )
    logger.addHandler(handler)
    return logger


def build_parser() -> argparse.ArgumentParser:
    """Create Resolve executor CLI arguments. / 创建 Resolve 执行器命令行参数。"""
    parser = argparse.ArgumentParser(
        description="Assemble timeline_cuts.json in DaVinci Resolve. / 在 Resolve 中组装时间线。"
    )
    parser.add_argument("--json", required=True)
    parser.add_argument("--media-root")
    parser.add_argument("--timeline-name", default="CyberEditor Timeline")
    parser.add_argument("--project-name", default="CyberEditor Project")
    parser.add_argument("--strict-fps", action="store_true")
    parser.add_argument(
        "--drx-root",
        help="Folder containing interview_clean.drx, cinematic.drx, etc.",
    )
    parser.add_argument(
        "--fairlight-preset",
        default="",
        help="Existing Resolve Fairlight preset name to apply to the timeline.",
    )
    parser.add_argument(
        "--render",
        action="store_true",
        help="Render the completed timeline through Resolve.",
    )
    parser.add_argument("--render-dir")
    parser.add_argument("--render-name", default="CyberEditor_final")
    parser.add_argument(
        "--render-preset",
        default="",
        help="Existing Resolve Deliver-page render preset; current settings are used when empty.",
    )
    parser.add_argument("--render-timeout", type=float, default=86400.0)
    parser.add_argument(
        "--macro-profile",
        help="Guarded PyAutoGUI profile for an API-unavailable post action.",
    )
    parser.add_argument("--macro-action", default="post_assembly")
    parser.add_argument(
        "--no-auto-start-resolve",
        action="store_true",
        help="Do not launch Resolve automatically when it is not running.",
    )
    parser.add_argument(
        "--resolve-startup-timeout",
        type=float,
        default=120.0,
        help="Seconds to wait for Resolve's scripting API after launch.",
    )
    parser.add_argument(
        "--log-level",
        default="INFO",
        choices=("DEBUG", "INFO", "WARNING", "ERROR"),
    )
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    """Run Resolve executor CLI with friendly errors. / 运行 Resolve 执行器 CLI 并提供友好错误。"""
    args = build_parser().parse_args(argv)
    logger = configure_logging(args.log_level)
    try:
        executor = DaVinciExecutor(
            json_path=args.json,
            media_root=args.media_root,
            timeline_name=args.timeline_name,
            project_name=args.project_name,
            strict_fps=args.strict_fps,
            auto_start_resolve=not args.no_auto_start_resolve,
            startup_timeout=args.resolve_startup_timeout,
            drx_root=args.drx_root,
            fairlight_preset=args.fairlight_preset,
            render_enabled=args.render,
            render_dir=args.render_dir,
            render_name=args.render_name,
            render_preset=args.render_preset,
            render_timeout=args.render_timeout,
            macro_profile=args.macro_profile,
            macro_action=args.macro_action,
            logger=logger,
        )
        executor.run()
        return 0
    except ResolveExecutorError as exc:
        logger.error("%s", exc)
        return 2
    except KeyboardInterrupt:
        logger.warning("用户中断 Resolve 执行 / Resolve execution interrupted.")
        return 130
    except Exception:
        logger.exception("未预期 Resolve 错误 / Unexpected Resolve error.")
        return 1


if __name__ == "__main__":
    sys.exit(main())
