#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
CyberEditor-Agent strict serial workflow orchestrator.
CyberEditor-Agent 严格串行工作流调度器。

This module deliberately imports no ML, OpenCV, requests, or Resolve packages.
Each heavy stage runs in a blocking child process, and the next stage cannot
start until the previous process has exited.

本模块刻意不导入机器学习、OpenCV、requests 或 Resolve 包。每个重型阶段在
阻塞式子进程中运行；前一进程退出前，下一阶段绝不会启动。
"""

import argparse
import gc
import json
import logging
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time
from typing import Dict, List, Optional, Sequence
from urllib import error as urllib_error
from urllib import request as urllib_request

from src.runtime_services import RuntimeServiceError, ensure_ollama_service
from src.media_manifest import (
    MediaManifestError,
    atomic_write_json,
    build_combined_raw_data,
    discover_video_files,
    make_asset_id,
    match_proxy_files,
)


LOGGER_NAME = "cybereditor"


class WorkflowError(RuntimeError):
    """Expected workflow orchestration failure. / 可预期的工作流调度错误。"""


class WindowsSleepInhibitor:
    """
    Keep Windows awake while a long workflow owns the current process.
    在长时间工作流占用当前进程期间阻止 Windows 因空闲而睡眠。

    ``SetThreadExecutionState`` changes no persistent power-plan setting. The
    request is scoped to this orchestrator thread and is explicitly cleared on
    success, failure, or cancellation. Keeping the display awake also protects
    Resolve UI automation and PyAutoGUI actions later in the pipeline.
    """

    ES_SYSTEM_REQUIRED = 0x00000001
    ES_DISPLAY_REQUIRED = 0x00000002
    ES_CONTINUOUS = 0x80000000

    def __init__(self, logger: Optional[logging.Logger] = None) -> None:
        """Store the logger and inactive state. / 保存日志器与未激活状态。"""
        self.logger = logger or logging.getLogger(LOGGER_NAME)
        self.active = False

    @staticmethod
    def _is_supported() -> bool:
        """Return whether the Windows execution-state API is available. / 判断 Windows 防睡眠 API 是否可用。"""
        return os.name == "nt"

    @staticmethod
    def _set_execution_state(flags: int) -> int:
        """Call the native execution-state API. / 调用 Windows 原生执行状态 API。"""
        import ctypes

        setter = ctypes.WinDLL("kernel32", use_last_error=True).SetThreadExecutionState
        setter.argtypes = (ctypes.c_ulong,)
        setter.restype = ctypes.c_ulong
        return int(setter(flags))

    def __enter__(self) -> "WindowsSleepInhibitor":
        """Request system/display wakefulness for this thread. / 请求本线程保持系统与显示器唤醒。"""
        if not self._is_supported():
            return self
        flags = self.ES_CONTINUOUS | self.ES_SYSTEM_REQUIRED | self.ES_DISPLAY_REQUIRED
        try:
            self.active = bool(self._set_execution_state(flags))
        except (AttributeError, OSError) as exc:
            self.logger.warning(
                "Windows 防睡眠请求失败，工作流仍会继续：%s / "
                "Could not inhibit Windows sleep; workflow will continue: %s",
                exc,
                exc,
            )
            return self
        if self.active:
            self.logger.info(
                "工作流期间已阻止 Windows 自动睡眠并保持显示器唤醒 / "
                "Windows sleep and display idle timeout inhibited during workflow"
            )
        else:
            self.logger.warning(
                "Windows 拒绝了防睡眠请求；请临时检查电源设置 / "
                "Windows rejected the sleep-inhibition request"
            )
        return self

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        """Restore normal Windows idle behavior on every exit path. / 无论成功或异常退出都恢复正常空闲策略。"""
        if not self.active:
            return
        try:
            restored = bool(self._set_execution_state(self.ES_CONTINUOUS))
        except (AttributeError, OSError) as restore_error:
            self.logger.warning(
                "恢复 Windows 电源状态请求失败：%s / Failed to restore execution state: %s",
                restore_error,
                restore_error,
            )
        else:
            if restored:
                self.logger.info(
                    "工作流结束，已恢复 Windows 正常电源策略 / "
                    "Workflow ended; normal Windows idle policy restored"
                )
        finally:
            self.active = False


class WorkflowLock:
    """
    Prevent two local workflows from competing for the same GPU/data directory.
    防止两个本地工作流争用同一 GPU/数据目录。
    """

    def __init__(self, path: Path) -> None:
        """Store an absolute lock path. / 保存绝对锁文件路径。"""
        self.path = path.resolve()
        self.fd: Optional[int] = None

    def __enter__(self) -> "WorkflowLock":
        """Acquire the lock atomically and record the owner PID. / 原子获取锁并记录所有者 PID。"""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        try:
            self._acquire()
        except FileExistsError as exc:
            owner = "unknown"
            try:
                owner = self.path.read_text(encoding="ascii").strip() or owner
            except OSError:
                pass
            if owner.isdecimal() and not self._pid_is_running(int(owner)):
                try:
                    self.path.unlink()
                    self._acquire()
                    return self
                except FileNotFoundError:
                    try:
                        self._acquire()
                        return self
                    except FileExistsError:
                        pass
                except FileExistsError:
                    pass
            raise WorkflowError(
                f"检测到另一个工作流或遗留锁文件：{self.path}（PID={owner}）。"
                "确认没有任务运行后删除该文件。\n"
                f"Another workflow or stale lock exists at {self.path} (PID={owner})."
            ) from exc
        return self

    def _acquire(self) -> None:
        """Create this lock atomically. / 原子创建本锁文件。"""
        self.fd = os.open(
            str(self.path),
            os.O_CREAT | os.O_EXCL | os.O_WRONLY,
        )
        os.write(self.fd, str(os.getpid()).encode("ascii"))

    @staticmethod
    def _pid_is_running(pid: int) -> bool:
        """Return whether a lock-owner PID still exists. / 判断锁所有者进程是否仍存在。"""
        if pid <= 0:
            return False
        if os.name == "nt":
            # ``os.kill(pid, 0)`` is not a harmless existence probe on Windows:
            # CPython maps non-console signals to TerminateProcess. Query a
            # limited process handle instead so stale-lock recovery can never
            # terminate the workflow it is checking.
            # Windows 上 ``os.kill(pid, 0)`` 并非无副作用的存在性检查；改用只读
            # 进程句柄查询，确保遗留锁恢复绝不会终止被检查的工作流。
            import ctypes

            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            open_process = kernel32.OpenProcess
            open_process.argtypes = (
                ctypes.c_ulong,
                ctypes.c_int,
                ctypes.c_ulong,
            )
            open_process.restype = ctypes.c_void_p
            close_handle = kernel32.CloseHandle
            close_handle.argtypes = (ctypes.c_void_p,)
            close_handle.restype = ctypes.c_int
            handle = open_process(0x1000, False, pid)
            if handle:
                close_handle(handle)
                return True
            return ctypes.get_last_error() == 5
        try:
            os.kill(pid, 0)
        except PermissionError:
            return True
        except (OSError, OverflowError, ValueError):
            return False
        return True

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        """Close and remove only the lock acquired by this process. / 关闭并仅删除本进程获取的锁。"""
        if self.fd is not None:
            os.close(self.fd)
            self.fd = None
        try:
            self.path.unlink()
        except FileNotFoundError:
            pass


class WorkflowOrchestrator:
    """
    Run extractor, director, and Resolve in strict serial child processes.
    在严格串行的子进程中运行提取器、导演和 Resolve。

    Parameters / 参数:
        project_root:
            Repository root containing ``src``.
            包含 ``src`` 的仓库根目录。
        data_dir:
            Runtime artifact directory.
            运行时产物目录。
        python_executable:
            Python used for all isolated stages.
            所有隔离阶段使用的 Python。
        logger:
            Workflow progress logger.
            工作流进度日志器。
    """

    def __init__(
        self,
        project_root: Path,
        data_dir: Path,
        python_executable: str = sys.executable,
        logger: Optional[logging.Logger] = None,
    ) -> None:
        """Initialize paths and active-process guard. / 初始化路径及活动进程保护。"""
        self.project_root = project_root.resolve()
        self.data_dir = data_dir.resolve()
        executable = Path(python_executable).expanduser()
        if os.name == "nt" and executable.name.casefold() == "pythonw.exe":
            console = executable.with_name("python.exe")
            if console.is_file():
                executable = console
        self.python_executable = str(executable)
        self.logger = logger or logging.getLogger(LOGGER_NAME)
        self.active_process: Optional[subprocess.Popen] = None

    @staticmethod
    def _has_continuous_visual_review(raw_data: Path) -> bool:
        """
        Validate full-span visual review metadata for every source asset.
        验证每条源素材都包含覆盖完整时长的连续视觉审片元数据。

        Legacy extraction stored only occasional scene thumbnails. It remains
        a valid archive, but is insufficient for the current director, which
        reviews every half-second temporal sample in sequence.
        """
        try:
            payload = json.loads(raw_data.read_text(encoding="utf-8-sig"))
        except (OSError, json.JSONDecodeError):
            return False
        assets = payload.get("assets") if isinstance(payload, dict) else None
        if not isinstance(assets, list) or not assets:
            return False
        for asset in assets:
            if not isinstance(asset, dict):
                return False
            sampling = asset.get("visual_sampling")
            keyframes = asset.get("keyframes")
            if not isinstance(sampling, dict) or not isinstance(keyframes, list):
                return False
            try:
                interval = float(sampling.get("requested_interval_sec"))
                effective_gap = float(
                    sampling.get("effective_min_gap_sec", interval)
                )
                hard_cap = int(sampling.get("hard_cap", 0))
                saved = int(sampling.get("saved_frame_count"))
            except (TypeError, ValueError):
                return False
            if (
                sampling.get("mode") != "continuous_temporal_coverage"
                or sampling.get("complete_source_span") is not True
                or interval <= 0
                or interval > 0.55
                or effective_gap <= 0
                or effective_gap > 0.55
                or hard_cap < 14400
                or saved <= 0
                or saved != len(keyframes)
            ):
                return False
        return True

    @staticmethod
    def _has_reusable_candidate_audit(
        timeline_cuts: Path,
        raw_data: Path,
        vision_model: str,
    ) -> bool:
        """
        Return whether a prior full visual pass can be safely reused.
        判断既有计划是否包含可复用的完整视觉候选审计。

        Parameters / 参数:
            timeline_cuts: Existing final director handoff. / 既有最终导演交接文件。
            raw_data: Current full extraction evidence. / 当前完整提取证据。
            vision_model: Installed vision model selected for review. / 审片视觉模型。
        """
        try:
            payload = json.loads(timeline_cuts.read_text(encoding="utf-8-sig"))
        except (OSError, json.JSONDecodeError):
            return False
        audit = payload.get("candidate_audit") if isinstance(payload, dict) else None
        review = payload.get("visual_review") if isinstance(payload, dict) else None
        if not (
            isinstance(audit, list)
            and bool(audit)
            and isinstance(review, dict)
            and review.get("mode") == "neutral_complete_temporal_coverage"
            and review.get("candidate_audit_complete") is True
            and int(review.get("candidate_audit_version", 0) or 0) >= 3
        ):
            return False
        recorded_fingerprint = str(
            review.get("evidence_fingerprint") or ""
        ).strip()
        if not recorded_fingerprint:
            return False
        try:
            # Imported only for the lightweight cache check. director.py itself
            # uses the standard library and does not load Ollama or any ML stack.
            # 仅在缓存校验时延迟导入；不会加载 Ollama 或 ML 运行时。
            from src.director import build_evidence_fingerprint

            current_fingerprint = build_evidence_fingerprint(
                raw_data, vision_model
            )
        except (ImportError, OSError, RuntimeError, TypeError, ValueError):
            return False
        return recorded_fingerprint == current_fingerprint

    def run(self, args: argparse.Namespace) -> None:
        """
        Execute selected stages and verify every handoff artifact.
        执行所选阶段并校验每个交接产物。
        """
        self.data_dir.mkdir(parents=True, exist_ok=True)
        raw_data = self.data_dir / "raw_data.json"
        timeline_cuts = self.data_dir / "timeline_cuts.json"
        preview_path: Optional[Path] = None
        lock_path = self.data_dir / ".cybereditor.lock"

        explicit_videos = self._argument_list(getattr(args, "video", None))
        explicit_proxies = self._argument_list(getattr(args, "proxy", None))
        if args.skip_extraction and not args.skip_director:
            self._require_file(raw_data, "跳过提取时需要现有 raw_data.json")
            if not self._has_continuous_visual_review(raw_data):
                has_sources = bool(
                    explicit_videos or getattr(args, "input_folder", None)
                )
                if not has_sources:
                    raise WorkflowError(
                        "现有 raw_data.json 来自旧版或低于 2fps 的审片，且没有提供源素材，无法自动升级。"
                        "请重新选择素材并运行完整流程。 / Existing extraction is legacy or below "
                        "the required 2-fps review density, and no source media was supplied; "
                        "select the media and run again."
                    )
                args.skip_extraction = False
                self.logger.warning(
                    "检测到旧版稀疏审片数据；已自动取消 --skip-extraction，将按完整时长每秒重新审片。"
                    " / Legacy sparse visual data detected; extraction will rerun automatically "
                    "with full-span one-second coverage."
                )
        sources: List[Path] = []
        proxy_map: Dict[Path, Path] = {}
        if not args.skip_extraction:
            try:
                sources = discover_video_files(
                    explicit_videos,
                    getattr(args, "input_folder", None),
                    recursive=not bool(getattr(args, "no_recursive", False)),
                )
                proxy_map = match_proxy_files(
                    sources,
                    explicit_proxies,
                    getattr(args, "proxy_folder", None),
                )
            except MediaManifestError as exc:
                raise WorkflowError(str(exc)) from exc
        if args.skip_extraction:
            self._require_file(raw_data, "跳过提取时需要现有 raw_data.json")
        if args.skip_director:
            self._require_file(
                timeline_cuts, "跳过导演时需要现有 timeline_cuts.json"
            )
        reuse_visual_audit = bool(
            args.skip_extraction
            and not args.skip_director
            and self._has_reusable_candidate_audit(
                timeline_cuts, raw_data, args.ollama_model
            )
        )

        with WorkflowLock(lock_path), WindowsSleepInhibitor(self.logger):
            self.logger.info(
                "串行工作流启动；任意时刻最多一个重型阶段 / Serial workflow started; one heavy stage at a time"
            )

            if not args.skip_extraction and not args.skip_director:
                self._require_vision_model(args.ollama_model, args.ollama_url)

            if not args.skip_extraction:
                assets: List[Dict[str, object]] = []
                for index, video_path in enumerate(sources, start=1):
                    asset_id = make_asset_id(index, video_path)
                    asset_dir = self.data_dir / "assets" / asset_id
                    asset_raw = asset_dir / "raw_data.json"
                    asset_srt = asset_dir / "transcript.srt"
                    asset_keyframes = asset_dir / "keyframes"
                    proxy_path = proxy_map[video_path]
                    command = [
                        self.python_executable,
                        "-m",
                        "src.extractor",
                        "--video",
                        str(video_path),
                        "--raw-data",
                        str(asset_raw),
                        "--srt",
                        str(asset_srt),
                        "--keyframes-dir",
                        str(asset_keyframes),
                        "--proxy-file-name",
                        str(proxy_path),
                        "--whisper-model",
                        args.whisper_model,
                        "--device",
                        args.whisper_device,
                        "--scene-threshold",
                        str(args.scene_threshold),
                        "--sample-interval",
                        str(args.sample_interval),
                        "--max-keyframes",
                        str(args.max_keyframes),
                        "--log-level",
                        args.log_level,
                    ]
                    if args.language:
                        command.extend(["--language", args.language])
                    self._run_stage(
                        f"提取 {index}/{len(sources)}：{video_path.name} / Extract",
                        command,
                    )
                    self._require_file(
                        asset_raw,
                        f"素材 {video_path.name} 未生成 raw_data.json",
                    )
                    try:
                        asset_payload = json.loads(
                            asset_raw.read_text(encoding="utf-8-sig")
                        )
                    except (OSError, ValueError) as exc:
                        raise WorkflowError(
                            f"无法读取素材提取结果 / Cannot read {asset_raw}: {exc}"
                        ) from exc
                    if not isinstance(asset_payload, dict):
                        raise WorkflowError(
                            f"素材提取结果格式错误 / Invalid asset JSON: {asset_raw}"
                        )
                    asset_payload["asset_id"] = asset_id
                    asset_payload["source_video"] = str(video_path)
                    asset_payload["proxy_file_name"] = str(proxy_path)
                    asset_payload["raw_data_path"] = str(asset_raw)
                    for frame in asset_payload.get("keyframes", []):
                        if isinstance(frame, dict) and frame.get("file_name"):
                            frame["image_path"] = str(
                                asset_keyframes / str(frame["file_name"])
                            )
                    assets.append(asset_payload)
                    self._release_barrier(
                        f"Whisper/OpenCV asset {index}/{len(sources)}"
                    )
                atomic_write_json(build_combined_raw_data(assets), raw_data)
                self._require_file(raw_data, "提取阶段未生成 raw_data.json")
                self.logger.info(
                    "批量提取完成：%d 个视频 / Batch extraction complete: %d videos",
                    len(assets),
                    len(assets),
                )

            treatment_path = self.data_dir / "director_treatment.json"
            music_brief = self.data_dir / "music_brief.json"
            music_analysis = self.data_dir / "music_analysis.json"
            music_cache = self.data_dir / "music-candidates"
            music_bed = self.data_dir / "music" / "music_bed.wav"
            program_audio = self.data_dir / "audio" / "program_audio.wav"
            provider = str(getattr(args, "music_provider", "off") or "off")
            # Preserve older CLI/UI integrations: supplying a local folder means
            # local-provider mode even when the new flag is absent.
            if provider == "off" and getattr(args, "music_folder", None):
                provider = "local"
            final_director_command: Optional[List[str]] = None
            bed_command: Optional[List[str]] = None

            if not args.skip_director:
                try:
                    _, ollama_started = ensure_ollama_service(
                        args.ollama_url,
                        timeout=min(30.0, float(args.ollama_timeout)),
                    )
                except RuntimeServiceError as exc:
                    raise WorkflowError(str(exc)) from exc
                if ollama_started:
                    self.logger.info(
                        "已自动启动 Ollama 服务（尚未加载模型）"
                        " / Ollama service auto-started; no model is loaded yet"
                    )
                preliminary_command = [
                    self.python_executable,
                    "-m",
                    "src.director",
                    "--raw-data",
                    str(raw_data),
                    "--model",
                    args.ollama_model,
                    "--text-model",
                    args.director_model or args.ollama_model,
                    "--ollama-url",
                    args.ollama_url,
                    "--chunk-minutes",
                    str(args.chunk_minutes),
                    "--project-fps",
                    str(args.project_fps),
                    "--num-ctx",
                    str(args.num_ctx),
                    "--target-duration-sec",
                    str(args.target_duration_sec),
                    "--camera-profile",
                    args.camera_profile,
                    "--timeout",
                    str(args.ollama_timeout),
                    "--treatment-only",
                    "--treatment-output",
                    str(treatment_path),
                    "--music-brief-output",
                    str(music_brief),
                    "--log-level",
                    args.log_level,
                ]
                if args.creative_brief.strip():
                    preliminary_command.extend(
                        ["--creative-brief", args.creative_brief.strip()]
                    )
                try:
                    self._run_stage(
                        "音乐导演初审 / Music director first pass",
                        preliminary_command,
                    )
                finally:
                    self._force_ollama_unload(args.ollama_model, args.ollama_url)
                    if (
                        str(args.director_model or args.ollama_model).casefold()
                        != str(args.ollama_model).casefold()
                    ):
                        self._force_ollama_unload(
                            args.director_model, args.ollama_url
                        )
                self._require_file(treatment_path, "导演初审未生成 director_treatment.json")
                self._require_file(music_brief, "导演初审未生成 music_brief.json")
                self._release_barrier("Ollama music-director first pass")

                if provider != "off":
                    music_command = [
                        self.python_executable,
                        "-m",
                        "src.music_analyzer",
                        "--provider",
                        provider,
                        "--cache-dir",
                        str(music_cache),
                        "--brief",
                        str(music_brief),
                        "--output",
                        str(music_analysis),
                        "--query",
                        str(args.creative_brief or "documentary cinematic"),
                        "--limit",
                        str(getattr(args, "music_candidate_limit", 8)),
                        # Phase 2 is documented as CPU/network-only.  Whisper's
                        # vocal audit defaults to ``auto`` and would otherwise
                        # silently claim CUDA while the orchestration contract
                        # says VRAM is free. / 阶段二必须保持 0 VRAM；显式禁止
                        # 人声审计的 auto 设备选择偷偷加载 CUDA。
                        "--vocal-audit-device",
                        "cpu",
                        "--log-level",
                        args.log_level,
                    ]
                    if provider == "local":
                        if not getattr(args, "music_folder", None):
                            raise WorkflowError(
                                "本地配乐模式需要曲库文件夹 / Local music mode requires --music-folder."
                            )
                        music_command.extend(["--library", str(args.music_folder)])
                    elif provider == "jamendo":
                        music_command.extend([
                            "--jamendo-client-id",
                            str(getattr(args, "jamendo_client_id", "") or ""),
                        ])
                    elif provider == "yt_dlp":
                        if bool(getattr(args, "music_rights_confirmed", False)):
                            music_command.append("--rights-confirmed")
                        music_command.extend([
                            "--rights-claim",
                            str(getattr(args, "music_rights_claim", "") or ""),
                        ])
                    self._run_stage(
                        "联网候选获取与 CPU 音乐听诊 / Music retrieval and CPU analysis",
                        music_command,
                    )
                    self._require_file(
                        music_analysis, "配乐听诊未生成 music_analysis.json"
                    )
                    self._release_barrier("CPU music retrieval and analysis")

                command = [
                    self.python_executable,
                    "-m",
                    "src.director",
                    "--raw-data",
                    str(raw_data),
                    "--output",
                    str(timeline_cuts),
                    "--model",
                    args.ollama_model,
                    "--text-model",
                    args.director_model or args.ollama_model,
                    "--ollama-url",
                    args.ollama_url,
                    "--chunk-minutes",
                    str(args.chunk_minutes),
                    "--project-fps",
                    str(args.project_fps),
                    "--num-ctx",
                    str(args.num_ctx),
                    "--target-duration-sec",
                    str(args.target_duration_sec),
                    "--camera-profile",
                    args.camera_profile,
                    "--treatment-file",
                    str(treatment_path),
                    "--timeout",
                    str(args.ollama_timeout),
                    "--log-level",
                    args.log_level,
                ]
                if args.creative_brief.strip():
                    command.extend(["--creative-brief", args.creative_brief.strip()])
                if provider != "off":
                    command.extend(["--music-analysis", str(music_analysis)])
                if reuse_visual_audit:
                    command.append("--reassemble-existing")
                    self.logger.info(
                        "检测到完整视觉候选审计；最终导演将直接重组，不重复逐秒看图 / "
                        "Reusable visual audit found; final director will reassemble without re-reviewing frames"
                    )
                final_director_command = list(command)
                try:
                    self._run_stage("最终 AI 导演 / Final AI director", command)
                finally:
                    # Second safety layer: runs even if the director is killed
                    # after loading the model but before its own finally block.
                    self._force_ollama_unload(
                        args.ollama_model, args.ollama_url
                    )
                    if args.director_model and args.director_model != args.ollama_model:
                        self._force_ollama_unload(
                            args.director_model, args.ollama_url
                        )
                self._require_file(
                    timeline_cuts, "导演阶段未生成 timeline_cuts.json"
                )
                self._release_barrier("Ollama")

                if provider != "off":
                    bed_command = [
                        self.python_executable,
                        "-m",
                        "src.music_bed",
                        "--timeline",
                        str(timeline_cuts),
                        "--output",
                        str(music_bed),
                        "--log-level",
                        args.log_level,
                    ]

            needs_program_audio = not (
                bool(args.skip_director)
                and bool(args.skip_resolve)
                and bool(getattr(args, "skip_preview", True))
            )
            if not args.skip_director or needs_program_audio or bed_command is not None:
                self._require_file(
                    timeline_cuts, "统一帧时间表需要现有 timeline_cuts.json"
                )
                frame_edl_command = [
                    self.python_executable,
                    "-m",
                    "src.frame_edl",
                    "--timeline",
                    str(timeline_cuts),
                    "--log-level",
                    args.log_level,
                ]
                self._run_stage(
                    "统一源帧/记录帧时间表 / Canonical source-record frame EDL",
                    frame_edl_command,
                )
                self._release_barrier("FFprobe frame-EDL conform")
            else:
                frame_edl_command = []

            if bed_command is not None:
                self._run_stage(
                    "音乐床合成与对白 Ducking / Music-bed conform and dialogue ducking",
                    bed_command,
                )
                # A valid no-music creative decision intentionally produces no WAV.
                self._release_barrier("FFmpeg CPU music-bed conform")

            if needs_program_audio:
                self._require_file(
                    timeline_cuts, "现场声合成需要现有 timeline_cuts.json"
                )
                program_audio_command = [
                    self.python_executable,
                    "-m",
                    "src.program_audio",
                    "--timeline",
                    str(timeline_cuts),
                    "--output",
                    str(program_audio),
                    "--log-level",
                    args.log_level,
                ]
                self._run_stage(
                    "逐剪点现场声预混 / Frame-faithful production-audio conform",
                    program_audio_command,
                )
                self._require_file(
                    program_audio, "现场声阶段未生成 program_audio.wav"
                )
                self._release_barrier("FFmpeg CPU production-audio conform")

            # Programmatic callers created before preview support have no
            # ``skip_preview`` attribute; keep those integrations backward
            # compatible. The CLI parser always supplies its explicit default.
            if not bool(getattr(args, "skip_preview", True)):
                preview_path = self.data_dir / "review" / str(
                    getattr(args, "preview_name", "CyberEditor_preview.mp4")
                )
                preview_command = [
                    self.python_executable,
                    "-m",
                    "src.review_renderer",
                    "--json",
                    str(timeline_cuts),
                    "--output",
                    str(preview_path),
                    "--width",
                    str(getattr(args, "preview_width", 1920)),
                    "--height",
                    str(getattr(args, "preview_height", 1080)),
                    "--log-level",
                    args.log_level,
                ]
                self._run_stage("预览成片 / Preview render", preview_command)
                self._require_file(preview_path, "预览渲染未生成输出文件")

                preview_review_enabled = (
                    hasattr(args, "skip_preview_review")
                    and not bool(getattr(args, "skip_preview_review", False))
                    and not bool(args.skip_director)
                    and final_director_command is not None
                )
                if preview_review_enabled:
                    review_path = self.data_dir / "review" / "rough_cut_review.json"
                    max_feedback_recuts = max(
                        0, min(3, int(getattr(args, "preview_review_rounds", 2)))
                    )
                    for review_round in range(max_feedback_recuts + 1):
                        review_command = [
                            self.python_executable,
                            "-m",
                            "src.rough_cut_reviewer",
                            "--preview",
                            str(preview_path),
                            "--timeline",
                            str(timeline_cuts),
                            "--output",
                            str(review_path),
                            "--model",
                            args.ollama_model,
                            "--text-model",
                            args.director_model or args.ollama_model,
                            "--ollama-url",
                            args.ollama_url,
                            "--num-ctx",
                            str(args.num_ctx),
                            "--timeout",
                            str(args.ollama_timeout),
                            "--log-level",
                            args.log_level,
                        ]
                        try:
                            self._run_stage(
                                f"低清成片盲审 {review_round + 1}/{max_feedback_recuts + 1}"
                                " / Rendered rough-cut blind review",
                                review_command,
                            )
                        finally:
                            self._force_ollama_unload(args.ollama_model, args.ollama_url)
                            if args.director_model and args.director_model != args.ollama_model:
                                self._force_ollama_unload(args.director_model, args.ollama_url)
                        self._require_file(review_path, "低清成片盲审未生成 JSON")
                        try:
                            review_payload = json.loads(
                                review_path.read_text(encoding="utf-8-sig")
                            )
                        except (OSError, ValueError) as exc:
                            raise WorkflowError(
                                f"无法读取低清成片盲审 / Cannot read rough-cut review: {exc}"
                            ) from exc
                        if bool(review_payload.get("passes")):
                            self.logger.info(
                                "低清成片通过陌生观众盲审，可进入 Resolve / "
                                "Rendered rough cut passed blind review; Resolve is now allowed"
                            )
                            break
                        if review_round >= max_feedback_recuts:
                            blind = review_payload.get("blind_review")
                            reason = (
                                blind.get("reason")
                                if isinstance(blind, dict) else "blind review failed"
                            )
                            raise WorkflowError(
                                "低清成片已耗尽 "
                                f"{max_feedback_recuts} 次导演重剪但仍未通过陌生观众盲审；"
                                f"已阻止 Resolve。审片报告：{review_path}。原因：{reason} / "
                                "Rendered rough cut exhausted all director recuts without passing "
                                "the blind-viewer gate; Resolve was not started."
                            )

                        self.logger.warning(
                            "低清成片盲审未通过，正在把真实成片反馈交回导演重剪 %d/%d / "
                            "Rendered rough cut failed; returning actual-film feedback for recut %d/%d",
                            review_round + 1,
                            max_feedback_recuts,
                            review_round + 1,
                            max_feedback_recuts,
                        )
                        recut_command = list(final_director_command)
                        if "--reassemble-existing" not in recut_command:
                            recut_command.append("--reassemble-existing")
                        recut_command.extend(["--rough-cut-feedback", str(review_path)])
                        try:
                            self._run_stage(
                                f"成片反馈重剪 {review_round + 1}/{max_feedback_recuts}"
                                " / Rough-cut feedback recut",
                                recut_command,
                            )
                        finally:
                            self._force_ollama_unload(args.ollama_model, args.ollama_url)
                            if args.director_model and args.director_model != args.ollama_model:
                                self._force_ollama_unload(args.director_model, args.ollama_url)
                        self._require_file(
                            timeline_cuts, "成片反馈重剪未生成 timeline_cuts.json"
                        )
                        self._release_barrier("Ollama rough-cut feedback recut")
                        self._run_stage(
                            "重剪统一帧时间表 / Recut canonical frame EDL",
                            frame_edl_command,
                        )
                        self._release_barrier("FFprobe recut frame-EDL conform")
                        if bed_command is not None:
                            self._run_stage(
                                "重剪音乐床合成 / Recut music-bed conform",
                                bed_command,
                            )
                            self._release_barrier("FFmpeg recut music-bed conform")
                        self._run_stage(
                            "重剪现场声预混 / Recut production-audio conform",
                            program_audio_command,
                        )
                        self._require_file(
                            program_audio, "重剪现场声阶段未生成 program_audio.wav"
                        )
                        self._release_barrier("FFmpeg recut production-audio conform")
                        self._run_stage(
                            "重剪预览成片 / Recut preview render", preview_command
                        )
                        self._require_file(
                            preview_path, "重剪预览渲染未生成输出文件"
                        )

            if not args.skip_resolve:
                media_root = (
                    Path(args.media_root).expanduser().resolve()
                    if args.media_root
                    else self.data_dir
                )
                command = [
                    self.python_executable,
                    "-m",
                    "src.resolve_executor",
                    "--json",
                    str(timeline_cuts),
                    "--media-root",
                    str(media_root),
                    "--timeline-name",
                    args.timeline_name,
                    "--project-name",
                    args.project_name,
                    "--log-level",
                    args.log_level,
                ]
                if args.strict_fps:
                    command.append("--strict-fps")
                if preview_path is not None and preview_path.is_file():
                    command.extend(["--approved-preview", str(preview_path)])
                drx_root = str(getattr(args, "drx_root", "") or "").strip()
                if drx_root:
                    command.extend(["--drx-root", drx_root])
                fairlight_preset = str(
                    getattr(args, "fairlight_preset", "") or ""
                ).strip()
                if fairlight_preset:
                    command.extend(["--fairlight-preset", fairlight_preset])
                macro_profile = str(
                    getattr(args, "macro_profile", "") or ""
                ).strip()
                if macro_profile:
                    command.extend(
                        [
                            "--macro-profile",
                            macro_profile,
                            "--macro-action",
                            str(
                                getattr(args, "macro_action", "post_assembly")
                            ),
                        ]
                    )
                final_dir: Optional[Path] = None
                render_name = str(
                    getattr(args, "render_name", "CyberEditor_final")
                    or "CyberEditor_final"
                )
                render_before: Dict[str, tuple[int, int]] = {}
                if bool(getattr(args, "render_final", False)):
                    command.append("--render")
                    render_dir = str(
                        getattr(args, "render_dir", "") or ""
                    ).strip()
                    if render_dir:
                        command.extend(["--render-dir", render_dir])
                    final_dir = (
                        Path(render_dir).expanduser().resolve()
                        if render_dir
                        else (self.data_dir / "final").resolve()
                    )
                    for existing in final_dir.glob(f"{render_name}.*"):
                        if not existing.is_file():
                            continue
                        try:
                            stat = existing.stat()
                        except OSError:
                            continue
                        render_before[str(existing.resolve()).casefold()] = (
                            stat.st_size,
                            stat.st_mtime_ns,
                        )
                    command.extend(
                        [
                            "--render-name",
                            render_name,
                        ]
                    )
                    render_preset = str(
                        getattr(args, "render_preset", "") or ""
                    ).strip()
                    if render_preset:
                        command.extend(["--render-preset", render_preset])
                    command.extend(
                        [
                            "--render-timeout",
                            str(getattr(args, "render_timeout", 86400.0)),
                        ]
                    )
                self._run_stage("执行 / Resolve", command)
                if bool(getattr(args, "render_final", False)):
                    if final_dir is None:
                        raise WorkflowError(
                            "Resolve 渲染目录未初始化 / Resolve render directory was not initialized."
                        )
                    rendered_candidates = [
                        path
                        for path in final_dir.glob(f"{render_name}.*")
                        if path.is_file()
                        and path.suffix.casefold()
                        in {".mov", ".mp4", ".mxf", ".avi", ".mkv"}
                        and (
                            str(path.resolve()).casefold() not in render_before
                            or (
                                path.stat().st_size,
                                path.stat().st_mtime_ns,
                            )
                            != render_before[str(path.resolve()).casefold()]
                        )
                    ]
                    if not rendered_candidates:
                        raise WorkflowError(
                            "Resolve reported success but produced no new or updated final media "
                            f"file in {final_dir}; an older same-name render is not accepted."
                        )
                    final_export = max(
                        rendered_candidates,
                        key=lambda path: path.stat().st_mtime_ns,
                    )
                    if preview_path is not None and preview_path.is_file():
                        final_qa_path = (
                            self.data_dir / "review" / "final_output_qa.json"
                        )
                        self._run_stage(
                            "最终导出一致性验收 / Final export consistency QA",
                            [
                                self.python_executable,
                                "-m",
                                "src.final_output_qa",
                                "--final",
                                str(final_export),
                                "--approved",
                                str(preview_path),
                                "--output",
                                str(final_qa_path),
                                "--log-level",
                                args.log_level,
                            ],
                        )
                        self._require_file(
                            final_qa_path,
                            "Resolve 最终导出未生成一致性 QA 报告",
                        )
                        self._require_passing_qa(final_qa_path)
                    else:
                        self.logger.warning(
                            "未生成审核预览；已确认 Resolve 产物存在，但无法执行音画一致性对照。"
                            " / No approved preview exists; the Resolve artifact is present, "
                            "but picture/audio consistency comparison is unavailable."
                        )

            self.logger.info(
                "全部所选阶段完成 / All selected stages completed"
            )

    @staticmethod
    def _argument_list(value: object) -> List[str]:
        """
        Normalize one argparse scalar/list into a clean string list.
        将 argparse 的标量或列表参数规范化为字符串列表。
        """
        if value is None:
            return []
        if isinstance(value, (str, os.PathLike)):
            return [str(value)]
        if isinstance(value, Sequence):
            return [str(item) for item in value if str(item).strip()]
        return [str(value)]

    def _run_stage(self, display_name: str, command: Sequence[str]) -> None:
        """
        Run one child synchronously and forbid process overlap.
        同步运行一个子进程并禁止进程重叠。
        """
        if self.active_process is not None:
            raise WorkflowError(
                "内部错误：尝试并行启动重型阶段 / Refusing concurrent heavy stage."
            )
        environment = os.environ.copy()
        environment["PYTHONUNBUFFERED"] = "1"
        environment["PYTHONIOENCODING"] = "utf-8"
        environment["PYTHONUTF8"] = "1"
        self.logger.info("=" * 72)
        self.logger.info("开始阶段：%s / Starting stage: %s", display_name, display_name)
        self.logger.info("命令 / Command: %s", subprocess.list2cmdline(list(command)))
        started = time.monotonic()
        stage_output_path = self.data_dir / "stage_output.log"
        try:
            stage_output_path.parent.mkdir(parents=True, exist_ok=True)
            with stage_output_path.open("a", encoding="utf-8", newline="") as transcript:
                transcript.write(
                    f"\n{'=' * 72}\n{display_name}\n"
                    f"{subprocess.list2cmdline(list(command))}\n"
                )
                transcript.flush()
                self.active_process = subprocess.Popen(
                    list(command),
                    cwd=str(self.project_root),
                    env=environment,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    bufsize=1,
                )
                assert self.active_process.stdout is not None
                for line in self.active_process.stdout:
                    transcript.write(line)
                    transcript.flush()
                    sys.stdout.write(line)
                    sys.stdout.flush()
                self.active_process.stdout.close()
                return_code = self.active_process.wait()
        except KeyboardInterrupt:
            self.logger.warning(
                "正在终止阶段：%s / Terminating stage: %s",
                display_name,
                display_name,
            )
            if self.active_process is not None:
                self.active_process.terminate()
                try:
                    self.active_process.wait(timeout=15)
                except subprocess.TimeoutExpired:
                    self.active_process.kill()
                    self.active_process.wait()
            raise
        except OSError as exc:
            raise WorkflowError(
                f"无法启动阶段 {display_name} / Cannot start stage: {exc}"
            ) from exc
        finally:
            self.active_process = None

        elapsed = time.monotonic() - started
        if return_code != 0:
            raise WorkflowError(
                f"阶段 {display_name} 失败，退出码 {return_code}。"
                f"完整子进程日志：{stage_output_path} / "
                f"Stage failed with exit code {return_code}; full child log: {stage_output_path}"
            )
        self.logger.info(
            "阶段完成：%s，耗时 %.1f 分钟 / Stage complete: %s, %.1f minutes",
            display_name,
            elapsed / 60.0,
            display_name,
            elapsed / 60.0,
        )

    def _force_ollama_unload(self, model: str, base_url: str) -> None:
        """
        Send ``keep_alive:0`` and use ``ollama stop`` as a fallback.
        发送 ``keep_alive:0``，并以 ``ollama stop`` 作为回退。
        """
        payload = json.dumps(
            {
                "model": model,
                "prompt": "",
                "stream": False,
                "keep_alive": 0,
            }
        ).encode("utf-8")
        request = urllib_request.Request(
            base_url.rstrip("/") + "/api/generate",
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        api_succeeded = False
        try:
            with urllib_request.urlopen(request, timeout=60) as response:
                api_succeeded = 200 <= response.status < 300
        except (urllib_error.URLError, TimeoutError, OSError) as exc:
            self.logger.warning(
                "父进程 Ollama 卸载 API 失败：%s / Parent unload API failed: %s",
                exc,
                exc,
            )

        ollama_command = shutil.which("ollama")
        if not api_succeeded and ollama_command:
            try:
                completed = subprocess.run(
                    [ollama_command, "stop", model],
                    capture_output=True,
                    text=True,
                    timeout=60,
                    check=False,
                )
                api_succeeded = completed.returncode == 0
            except (OSError, subprocess.SubprocessError) as exc:
                self.logger.warning(
                    "ollama stop 失败：%s / ollama stop failed: %s", exc, exc
                )
        if api_succeeded:
            self.logger.info(
                "父进程已确认请求卸载 %s / Parent confirmed unload request for %s",
                model,
                model,
            )
        canonical_model = str(model or "").strip().casefold()
        if ":" not in canonical_model:
            canonical_model += ":latest"

        def canonical(value: object) -> str:
            text = str(value or "").strip().casefold()
            return text if ":" in text else text + ":latest"

        verified_unloaded = False
        deadline = time.monotonic() + 30.0
        last_error = ""
        while time.monotonic() < deadline:
            try:
                with urllib_request.urlopen(
                    base_url.rstrip("/") + "/api/ps", timeout=5
                ) as response:
                    resident_payload = json.loads(response.read().decode("utf-8"))
                resident_rows = (
                    resident_payload.get("models", [])
                    if isinstance(resident_payload, dict)
                    else []
                )
                resident = {
                    canonical(
                        item.get("name") or item.get("model")
                    )
                    for item in resident_rows
                    if isinstance(item, dict)
                }
                if canonical_model not in resident:
                    verified_unloaded = True
                    break
                last_error = f"still resident: {sorted(resident)}"
            except (
                urllib_error.URLError,
                TimeoutError,
                OSError,
                ValueError,
            ) as exc:
                last_error = str(exc)
                if ollama_command:
                    try:
                        ps = subprocess.run(
                            [ollama_command, "ps"],
                            capture_output=True,
                            text=True,
                            timeout=10,
                            check=False,
                        )
                    except (OSError, subprocess.SubprocessError):
                        ps = None
                    if ps is not None and ps.returncode == 0:
                        resident_lines = {
                            canonical(line.split()[0])
                            for line in ps.stdout.splitlines()[1:]
                            if line.split()
                        }
                        if canonical_model not in resident_lines:
                            verified_unloaded = True
                            break
            time.sleep(0.5)
        if not verified_unloaded:
            raise WorkflowError(
                f"无法确认 Ollama 模型 {model!r} 已完全卸载（{last_error}）；"
                "为避免与下一重型阶段并发占用显存，工作流已停止。 / Could not "
                "verify exact-tag Ollama unload; the serial workflow stopped before "
                "starting the next VRAM-heavy stage."
            )
        self.logger.info(
            "已通过 /api/ps 确认 %s 不再驻留 / Verified exact model is no longer resident: %s",
            model,
            model,
        )

    def _require_vision_model(self, model: str, base_url: str) -> None:
        """
        Fail before long extraction when the selected model cannot inspect images.
        在耗时提取前确认所选模型能看图，避免数小时后才失败。

        Parameters / 参数:
            model:
                Installed Ollama model tag. / 已安装的 Ollama 模型标签。
            base_url:
                Local Ollama service URL. / 本地 Ollama 服务地址。
        """
        try:
            ensure_ollama_service(base_url, timeout=30.0)
            body = json.dumps({"model": model}).encode("utf-8")
            request = urllib_request.Request(
                base_url.rstrip("/") + "/api/show",
                data=body,
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urllib_request.urlopen(request, timeout=30) as response:
                payload = json.loads(response.read().decode("utf-8"))
        except (
            RuntimeServiceError,
            urllib_error.URLError,
            TimeoutError,
            OSError,
            ValueError,
        ) as exc:
            raise WorkflowError(
                f"无法检查 Ollama 模型 {model!r}。请确认模型已安装：ollama pull {model}\n"
                f"Could not inspect the selected Ollama model: {exc}"
            ) from exc
        capabilities = (
            payload.get("capabilities", []) if isinstance(payload, dict) else []
        )
        if "vision" not in {str(value).casefold() for value in capabilities}:
            raise WorkflowError(
                f"模型 {model!r} 不支持图像输入，不能审阅视频画面。"
                "请先安装并选择视觉模型，例如：\n"
                "  ollama pull hf.co/ggml-org/Qwen3.8-27B-GGUF:Q4_K_M\n"
                "  ollama pull hf.co/ggml-org/Qwen3.8-27B-GGUF:Q8_0\n"
                "The selected model is text-only; a vision model is required."
            )

    def _release_barrier(self, stage_name: str) -> None:
        """
        Document and enforce the process-exit memory barrier.
        记录并执行进程退出显存屏障。

        ``Popen.wait`` has already observed child termination; at that point the
        OS owns no live process holding that stage's CUDA context.

        ``Popen.wait`` 已确认子进程结束；此时操作系统中不存在仍持有该阶段 CUDA
        上下文的活动进程。
        """
        gc.collect()
        self.logger.info(
            "显存屏障通过：%s 子进程已退出 / VRAM barrier passed: %s process exited",
            stage_name,
            stage_name,
        )

    @staticmethod
    def _require_file(path: Path, context: str) -> None:
        """Require a non-empty stage handoff artifact. / 要求阶段交接产物存在且非空。"""
        if not path.is_file() or path.stat().st_size == 0:
            raise WorkflowError(f"{context}: {path}")

    @staticmethod
    def _require_passing_qa(path: Path) -> None:
        """
        Require an explicit true hard-gate result from final-output QA.
        要求最终导出 QA 明确返回 true，禁止技术失败被标记为成功。

        Parameters / 参数:
            path: Existing final-output QA JSON report. / 已存在的最终导出 QA 报告。
        """
        try:
            payload = json.loads(path.read_text(encoding="utf-8-sig"))
        except (OSError, json.JSONDecodeError) as exc:
            raise WorkflowError(
                f"最终导出 QA 报告无法读取 / Invalid final-output QA report: {path}: {exc}"
            ) from exc
        if not isinstance(payload, dict) or payload.get("passes") is not True:
            failures = payload.get("failures", []) if isinstance(payload, dict) else []
            detail = "; ".join(str(item) for item in failures) or "passes was not true"
            raise WorkflowError(
                "Resolve 最终导出未通过技术验收 / "
                f"Final Resolve export did not pass technical QA: {detail}"
            )


def configure_logging(level: str, log_file: Path) -> logging.Logger:
    """
    Configure UTF-8 file logging plus console progress.
    配置 UTF-8 文件日志及控制台进度。
    """
    logger = logging.getLogger(LOGGER_NAME)
    logger.handlers.clear()
    logger.propagate = False
    logger.setLevel(getattr(logging, level.upper()))
    formatter = logging.Formatter(
        "%(asctime)s | %(levelname)s | %(name)s | %(message)s"
    )
    console = logging.StreamHandler()
    console.setFormatter(formatter)
    logger.addHandler(console)
    log_file.parent.mkdir(parents=True, exist_ok=True)
    file_handler = logging.FileHandler(log_file, encoding="utf-8")
    file_handler.setFormatter(formatter)
    logger.addHandler(file_handler)
    return logger


def build_parser() -> argparse.ArgumentParser:
    """Create top-level workflow CLI arguments. / 创建顶层工作流命令行参数。"""
    parser = argparse.ArgumentParser(
        description=(
            "CyberEditor-Agent: strict serial local-AI video editing. "
            "/ 严格串行的本地 AI 视频剪辑。"
        )
    )
    parser.add_argument(
        "--video",
        action="append",
        help="源视频，可重复传入 / source video; repeat for multiple files",
    )
    parser.add_argument(
        "--input-folder",
        help="批量素材文件夹 / folder containing source videos",
    )
    parser.add_argument(
        "--no-recursive",
        action="store_true",
        help="不扫描素材子文件夹 / do not scan nested folders",
    )
    parser.add_argument(
        "--proxy",
        action="append",
        help="代理素材，可重复传入 / proxy media; repeat for multiple files",
    )
    parser.add_argument(
        "--proxy-folder",
        help="代理素材目录，按同名文件匹配 / proxy folder matched by stem",
    )
    parser.add_argument(
        "--data-dir",
        default="data",
        help="运行数据目录 / runtime data directory (default: data)",
    )
    parser.add_argument("--media-root", help="Resolve 相对素材根目录")
    parser.add_argument("--whisper-model", default="small")
    parser.add_argument("--whisper-device", default="auto")
    parser.add_argument("--language")
    parser.add_argument("--scene-threshold", type=float, default=0.28)
    parser.add_argument(
        "--sample-interval", type=float, default=0.5,
        help="连续视觉审片采样间隔；默认每秒两帧 / full-review sampling interval",
    )
    parser.add_argument(
        "--max-keyframes", type=int, default=14400,
        help="每个素材的视觉证据硬上限 / per-source visual evidence cap",
    )
    parser.add_argument(
        "--ollama-model",
        default="hf.co/ggml-org/Qwen3.8-27B-GGUF:Q4_K_M",
    )
    parser.add_argument(
        "--director-model",
        default="hf.co/ggml-org/Qwen3.8-27B-GGUF:Q8_0",
        help="视觉阶段卸载后加载的全局文字导演 / global text director",
    )
    parser.add_argument("--ollama-url", default="http://localhost:11434")
    parser.add_argument("--chunk-minutes", type=float, default=12.0)
    parser.add_argument("--project-fps", type=float, default=25.0)
    parser.add_argument("--num-ctx", type=int, default=8192)
    parser.add_argument("--creative-brief", default="")
    parser.add_argument("--target-duration-sec", type=float, default=0.0)
    parser.add_argument(
        "--camera-profile",
        default="sony_pp8_slog3_sgamut3cine",
        choices=("sony_pp8_slog3_sgamut3cine", "rec709", "auto"),
    )
    parser.add_argument("--music-folder")
    parser.add_argument(
        "--music-provider",
        choices=("off", "local", "jamendo", "yt_dlp"),
        default="off",
        help="配乐来源 / music source provider",
    )
    parser.add_argument("--music-candidate-limit", type=int, default=8)
    parser.add_argument("--jamendo-client-id", default="")
    parser.add_argument(
        "--music-rights-confirmed",
        action="store_true",
        help="确认任意在线音频的下载、改编和使用权及平台条款 / confirm rights and platform terms",
    )
    parser.add_argument("--music-rights-claim", default="")
    parser.add_argument(
        "--ollama-timeout",
        type=int,
        default=7200,
        help=(
            "单次 Ollama 导演请求读取超时秒数；默认 7200，适合 27B/70B 混合内存慢推理 / "
            "per-request Ollama read timeout in seconds; default 7200 for slow mixed-memory inference"
        ),
    )
    parser.add_argument("--timeline-name", default="CyberEditor Timeline")
    parser.add_argument("--project-name", default="CyberEditor Project")
    parser.add_argument("--strict-fps", action="store_true")
    parser.add_argument("--drx-root")
    parser.add_argument("--fairlight-preset", default="")
    parser.add_argument("--macro-profile")
    parser.add_argument("--macro-action", default="post_assembly")
    parser.add_argument(
        "--render-final",
        action="store_true",
        help="由 Resolve 渲染最终成片 / render the final movie in Resolve",
    )
    parser.add_argument("--render-dir")
    parser.add_argument("--render-name", default="CyberEditor_final")
    parser.add_argument("--render-preset", default="")
    parser.add_argument("--render-timeout", type=float, default=86400.0)
    parser.add_argument("--skip-extraction", action="store_true")
    parser.add_argument("--skip-director", action="store_true")
    parser.add_argument("--skip-resolve", action="store_true")
    parser.add_argument(
        "--skip-preview",
        action="store_true",
        help="不生成可直接观看的 MP4 预览 / skip rendered MP4 preview",
    )
    parser.add_argument(
        "--preview-name", default="CyberEditor_preview.mp4"
    )
    parser.add_argument("--preview-width", type=int, default=1920)
    parser.add_argument("--preview-height", type=int, default=1080)
    parser.add_argument(
        "--skip-preview-review",
        action="store_true",
        help=(
            "跳过低清成片的多模态陌生观众盲审（不推荐） / "
            "skip multimodal blind review of the rendered rough cut"
        ),
    )
    parser.add_argument(
        "--preview-review-rounds",
        type=int,
        default=2,
        help="低清成片盲审失败后的最大自动重剪次数 / maximum rendered-preview recuts",
    )
    parser.add_argument(
        "--log-level",
        default="INFO",
        choices=("DEBUG", "INFO", "WARNING", "ERROR"),
    )
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    """Run the strict serial workflow. / 运行严格串行工作流。"""
    args = build_parser().parse_args(argv)
    project_root = Path(__file__).resolve().parent
    data_dir = Path(args.data_dir).expanduser()
    if not data_dir.is_absolute():
        data_dir = project_root / data_dir
    logger = configure_logging(
        args.log_level, data_dir.resolve() / "cybereditor.log"
    )
    try:
        orchestrator = WorkflowOrchestrator(
            project_root=project_root,
            data_dir=data_dir,
            logger=logger,
        )
        orchestrator.run(args)
        return 0
    except WorkflowError as exc:
        logger.error("%s", exc)
        return 2
    except KeyboardInterrupt:
        logger.warning("用户中断工作流 / Workflow interrupted by user.")
        return 130
    except Exception:
        logger.exception("未预期调度错误 / Unexpected orchestration error.")
        return 1


if __name__ == "__main__":
    sys.exit(main())
