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
import struct
import subprocess
import sys
from typing import Any, Dict, Iterable, List, NamedTuple, Optional, Sequence, Tuple


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
    """

    def __init__(
        self,
        json_path: os.PathLike,
        media_root: Optional[os.PathLike] = None,
        timeline_name: str = "CyberEditor Timeline",
        project_name: str = "CyberEditor Project",
        strict_fps: bool = False,
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
        self.logger = logger or logging.getLogger(LOGGER_NAME)
        if not self.timeline_name or not self.project_name:
            raise ResolveExecutorError(
                "工程/时间线名称不能为空 / Project/timeline name cannot be empty."
            )

        self.resolve: Any = None
        self.project_manager: Any = None
        self.project: Any = None
        self.media_pool: Any = None
        self.timeline: Any = None
        self.created_project = False

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
        self.save_project()
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
            decisions.append(
                ClipDecision(
                    clip_id=clip_id,
                    file_name=file_name.strip(),
                    cut_in_sec=cut_in,
                    cut_out_sec=cut_out,
                    reason_for_cut=reason.strip(),
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
        process_state = self._is_resolve_running()
        if process_state is False:
            raise ResolveExecutorError(
                "未检测到 Resolve.exe。请启动 DaVinci Resolve 后重试。"
                " / Resolve.exe is not running. Start Resolve and retry."
            )

        self._configure_resolve_library()
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
        try:
            resolve = module.scriptapp("Resolve")
        except Exception as exc:
            raise ResolveExecutorError(
                "连接 Resolve 失败。请在 Preferences > System > General 中将 "
                "External scripting 设置为 Local，并重启 Resolve。\n"
                f"Resolve connection failed; enable Local external scripting: {exc}"
            ) from exc
        if resolve is None:
            raise ResolveExecutorError(
                "GetResolve() 返回空实例。确认 Resolve 正在运行、外部脚本设为 Local。"
                " / GetResolve() returned no instance; check Resolve and Local scripting."
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
        if project is not None:
            self.logger.info(
                "使用当前工程“%s” / Using current project '%s'",
                self._safe_name(project, "unnamed"),
                self._safe_name(project, "unnamed"),
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
        fps_text = self._decimal_text(fps)
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
        if timeline is None or not self.project.SetCurrentTimeline(timeline):
            raise ResolveExecutorError(
                f"无法创建/激活时间线 / Cannot create/activate timeline: {self.timeline_name}"
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
        if json_fps == resolve_fps:
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
            start_frame, end_frame = self.seconds_to_frames(
                decision.cut_in_sec, decision.cut_out_sec, fps
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
    def _configure_resolve_library() -> None:
        """Populate RESOLVE_SCRIPT_LIB from standard installs when absent. / 未配置时从标准安装位置填充 RESOLVE_SCRIPT_LIB。"""
        if os.environ.get("RESOLVE_SCRIPT_LIB"):
            return
        root = Path(os.environ.get("PROGRAMFILES", r"C:\Program Files"))
        candidates = (
            root
            / "Blackmagic Design"
            / "DaVinci Resolve"
            / "fusionscript.dll",
            root
            / "Blackmagic Design"
            / "DaVinci Resolve"
            / "Fusion"
            / "fusionscript.dll",
        )
        for candidate in candidates:
            if candidate.is_file():
                os.environ["RESOLVE_SCRIPT_LIB"] = str(candidate)
                return

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
                text=True,
                timeout=10,
                check=False,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        except (OSError, subprocess.SubprocessError):
            return None
        if completed.returncode != 0:
            return None
        return "resolve.exe" in completed.stdout.casefold()

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
