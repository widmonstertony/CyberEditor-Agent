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
            self.fd = os.open(
                str(self.path),
                os.O_CREAT | os.O_EXCL | os.O_WRONLY,
            )
            os.write(self.fd, str(os.getpid()).encode("ascii"))
        except FileExistsError as exc:
            owner = "unknown"
            try:
                owner = self.path.read_text(encoding="ascii").strip() or owner
            except OSError:
                pass
            raise WorkflowError(
                f"检测到另一个工作流或遗留锁文件：{self.path}（PID={owner}）。"
                "确认没有任务运行后删除该文件。\n"
                f"Another workflow or stale lock exists at {self.path} (PID={owner})."
            ) from exc
        return self

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
        self.python_executable = python_executable
        self.logger = logger or logging.getLogger(LOGGER_NAME)
        self.active_process: Optional[subprocess.Popen] = None

    def run(self, args: argparse.Namespace) -> None:
        """
        Execute selected stages and verify every handoff artifact.
        执行所选阶段并校验每个交接产物。
        """
        self.data_dir.mkdir(parents=True, exist_ok=True)
        raw_data = self.data_dir / "raw_data.json"
        srt_path = self.data_dir / "transcript.srt"
        keyframes_dir = self.data_dir / "keyframes"
        timeline_cuts = self.data_dir / "timeline_cuts.json"
        lock_path = self.data_dir / ".cybereditor.lock"

        video_path = (
            Path(args.video).expanduser().resolve() if args.video else None
        )
        proxy_path = (
            Path(args.proxy).expanduser().resolve()
            if args.proxy
            else video_path
        )
        if not args.skip_extraction:
            if video_path is None or not video_path.is_file():
                raise WorkflowError(
                    "提取阶段需要有效的 --video / Extraction requires a valid --video."
                )
        if proxy_path is not None and not proxy_path.is_file():
            raise WorkflowError(
                f"找不到代理素材 / Proxy media not found: {proxy_path}"
            )
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

            if not args.skip_extraction:
                command = [
                    self.python_executable,
                    "-m",
                    "src.extractor",
                    "--video",
                    str(video_path),
                    "--raw-data",
                    str(raw_data),
                    "--srt",
                    str(srt_path),
                    "--keyframes-dir",
                    str(keyframes_dir),
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
                if proxy_path:
                    command.extend(["--proxy-file-name", str(proxy_path)])
                self._run_stage("提取 / Extract", command)
                self._require_file(raw_data, "提取阶段未生成 raw_data.json")
                self._release_barrier("Whisper/OpenCV")

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
                    "--ollama-url",
                    args.ollama_url,
                    "--chunk-minutes",
                    str(args.chunk_minutes),
                    "--project-fps",
                    str(args.project_fps),
                    "--num-ctx",
                    str(args.num_ctx),
                    "--timeout",
                    str(args.ollama_timeout),
                    "--log-level",
                    args.log_level,
                ]
                if proxy_path:
                    command.extend(["--proxy-file-name", str(proxy_path)])
                try:
                    self._run_stage("导演 / Direct", command)
                finally:
                    # Second safety layer: runs even if the director is killed
                    # after loading the model but before its own finally block.
                    self._force_ollama_unload(
                        args.ollama_model, args.ollama_url
                    )
                self._require_file(
                    timeline_cuts, "导演阶段未生成 timeline_cuts.json"
                )
                self._release_barrier("Ollama")

            if not args.skip_resolve:
                media_root = (
                    Path(args.media_root).expanduser().resolve()
                    if args.media_root
                    else (proxy_path.parent if proxy_path else self.data_dir)
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
                self._run_stage("执行 / Resolve", command)

            self.logger.info(
                "全部所选阶段完成 / All selected stages completed"
            )

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
    parser.add_argument("--video", help="源视频 / source video")
    parser.add_argument(
        "--proxy",
        help="Resolve 1080p 代理；省略则使用源视频 / Resolve proxy; defaults to source",
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
    parser.add_argument("--sample-interval", type=float, default=2.0)
    parser.add_argument("--max-keyframes", type=int, default=240)
    parser.add_argument("--ollama-model", default="qwen2.5:32b")
    parser.add_argument("--ollama-url", default="http://localhost:11434")
    parser.add_argument("--chunk-minutes", type=float, default=12.0)
    parser.add_argument("--project-fps", type=float, default=25.0)
    parser.add_argument("--num-ctx", type=int, default=8192)
    parser.add_argument("--ollama-timeout", type=int, default=1800)
    parser.add_argument("--timeline-name", default="CyberEditor Timeline")
    parser.add_argument("--project-name", default="CyberEditor Project")
    parser.add_argument("--strict-fps", action="store_true")
    parser.add_argument("--skip-extraction", action="store_true")
    parser.add_argument("--skip-director", action="store_true")
    parser.add_argument("--skip-resolve", action="store_true")
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
