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
        reviews every one-second temporal sample in sequence.
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
                saved = int(sampling.get("saved_frame_count"))
            except (TypeError, ValueError):
                return False
            if (
                sampling.get("mode") != "continuous_temporal_coverage"
                or sampling.get("complete_source_span") is not True
                or interval <= 0
                or interval > 1.05
                or saved <= 0
                or saved != len(keyframes)
            ):
                return False
        return True

    def run(self, args: argparse.Namespace) -> None:
        """
        Execute selected stages and verify every handoff artifact.
        执行所选阶段并校验每个交接产物。
        """
        self.data_dir.mkdir(parents=True, exist_ok=True)
        raw_data = self.data_dir / "raw_data.json"
        timeline_cuts = self.data_dir / "timeline_cuts.json"
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
                        "现有 raw_data.json 来自旧版稀疏审片，且没有提供源素材，无法自动升级。"
                        "请重新选择素材并运行完整流程。 / Existing extraction uses legacy sparse "
                        "review data and no source media was supplied; select the media and run again."
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

        with WorkflowLock(lock_path):
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
            provider = str(getattr(args, "music_provider", "off") or "off")
            # Preserve older CLI/UI integrations: supplying a local folder means
            # local-provider mode even when the new flag is absent.
            if provider == "off" and getattr(args, "music_folder", None):
                provider = "local"

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
                    self._run_stage(
                        "音乐床合成与对白 Ducking / Music-bed conform and dialogue ducking",
                        bed_command,
                    )
                    # A valid no-music creative decision intentionally produces no WAV.
                    self._release_barrier("FFmpeg CPU music-bed conform")

            # Programmatic callers created before preview support have no
            # ``skip_preview`` attribute; keep those integrations backward
            # compatible. The CLI parser always supplies its explicit default.
            if not bool(getattr(args, "skip_preview", True)):
                preview_path = self.data_dir / "review" / str(
                    getattr(args, "preview_name", "CyberEditor_preview.mp4")
                )
                command = [
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
                self._run_stage("预览成片 / Preview render", command)
                self._require_file(preview_path, "预览渲染未生成输出文件")

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
                if bool(getattr(args, "render_final", False)):
                    command.append("--render")
                    render_dir = str(
                        getattr(args, "render_dir", "") or ""
                    ).strip()
                    if render_dir:
                        command.extend(["--render-dir", render_dir])
                    command.extend(
                        [
                            "--render-name",
                            str(
                                getattr(
                                    args, "render_name", "CyberEditor_final"
                                )
                            ),
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
        self.logger.info("=" * 72)
        self.logger.info("开始阶段：%s / Starting stage: %s", display_name, display_name)
        self.logger.info("命令 / Command: %s", subprocess.list2cmdline(list(command)))
        started = time.monotonic()
        try:
            self.active_process = subprocess.Popen(
                list(command),
                cwd=str(self.project_root),
                env=environment,
            )
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
                f" / Stage failed with exit code {return_code}."
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
        else:
            self.logger.warning(
                "无法确认 %s 已卸载；启动 Resolve 前请运行 ollama stop %s。"
                " / Could not confirm unload; stop the model before Resolve.",
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
                "  ollama pull qwen3.6:27b-mtp-q8_0\n"
                "  ollama pull qwen3.6:27b-mtp-q4_K_M\n"
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
        "--sample-interval", type=float, default=1.0,
        help="连续视觉审片采样间隔；默认每秒一帧 / full-review sampling interval",
    )
    parser.add_argument(
        "--max-keyframes", type=int, default=7200,
        help="每个素材的视觉证据硬上限 / per-source visual evidence cap",
    )
    parser.add_argument("--ollama-model", default="qwen3.6:27b-mtp-q8_0")
    parser.add_argument(
        "--director-model",
        default="",
        help="视觉阶段卸载后加载的全局文字导演；空值沿用视觉模型 / global text director",
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
    parser.add_argument("--ollama-timeout", type=int, default=1800)
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
