#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Outbound Windows worker for a deployed CyberEditor control plane.

CyberEditor 部署版的 Windows 出站 Worker。

The worker keeps RAW media, Ollama, CUDA, FFmpeg, and DaVinci Resolve on the
creator's own computer.  It only receives small command JSON documents through
an outbound HTTPS connection and may upload a deliberately generated low-bitrate
preview after a successful run.
"""

from __future__ import annotations

import argparse
import http.client
import json
import logging
import mimetypes
import os
from pathlib import Path
import re
import shutil
import socket
import subprocess
import sys
import time
from typing import Dict, Mapping, Optional, Sequence
from urllib import error as urllib_error
from urllib import parse as urllib_parse
from urllib import request as urllib_request

from .gui import build_runtime_environment, console_python_executable
from .web_server import WorkflowManager, detect_environment, native_picker


LOGGER = logging.getLogger("cybereditor.remote_worker")
TERMINAL_STATES = {"succeeded", "failed", "stopped"}


class WorkerProtocolError(RuntimeError):
    """Represent a rejected or malformed control-plane response.

    表示控制平面拒绝请求，或返回了格式错误的响应。
    """


def _default_worker_id() -> str:
    """Build a stable, URL-safe ID from this computer name.

    根据本机名称生成稳定且适合放入 URL 的 Worker ID。
    """
    raw = os.environ.get("COMPUTERNAME") or socket.gethostname() or "windows-worker"
    value = re.sub(r"[^A-Za-z0-9_.-]+", "-", raw).strip("-.").lower()
    return value[:80] or "windows-worker"


class ControlPlaneClient:
    """Small authenticated client for the outbound worker protocol.

    用于出站 Worker 协议的轻量认证客户端，不依赖第三方 HTTP 库。
    """

    def __init__(
        self,
        server_url: str,
        worker_token: str,
        worker_id: str,
        *,
        allow_insecure_http: bool = False,
        timeout: float = 30.0,
    ) -> None:
        """Validate and store connection details.

        校验并保存连接参数。

        Parameters / 参数:
            server_url: Public control-plane base URL. / 公网控制平面根地址。
            worker_token: Dedicated worker secret. / 独立的 Worker 密钥。
            worker_id: Stable identifier for this PC. / 本机稳定标识。
            allow_insecure_http: Permit non-loopback HTTP for development only.
                仅开发环境允许非回环明文 HTTP。
            timeout: Per-request timeout in seconds. / 单次请求超时秒数。
        """
        parsed = urllib_parse.urlparse(server_url.rstrip("/"))
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise ValueError("--server must be a complete http(s) URL.")
        loopback = parsed.hostname.casefold() in {"localhost", "127.0.0.1", "::1"}
        if parsed.scheme != "https" and not loopback and not allow_insecure_http:
            raise ValueError(
                "Remote workers require HTTPS. Use --allow-insecure-http only on a trusted test LAN."
            )
        if len(worker_token) < 24:
            raise ValueError("Worker token must contain at least 24 characters.")
        if not re.fullmatch(r"[A-Za-z0-9_.-]{1,80}", worker_id):
            raise ValueError("Worker ID may contain only letters, digits, dot, underscore, and dash.")
        self.server_url = server_url.rstrip("/")
        self.worker_token = worker_token
        self.worker_id = worker_id
        self.timeout = timeout
        self._parsed = parsed

    def _url(self, path: str, query: Optional[Mapping[str, object]] = None) -> str:
        """Create an API URL while preserving an optional deployment prefix.

        创建 API URL，并保留反向代理可能配置的路径前缀。
        """
        suffix = path if path.startswith("/") else "/" + path
        value = self.server_url + suffix
        return value + ("?" + urllib_parse.urlencode(query) if query else "")

    def request_json(
        self,
        method: str,
        path: str,
        payload: Optional[Mapping[str, object]] = None,
        query: Optional[Mapping[str, object]] = None,
    ) -> Dict[str, object]:
        """Send one authenticated JSON request and validate its envelope.

        发送一条经过认证的 JSON 请求，并校验返回结构。
        """
        body = None
        headers = {
            "Accept": "application/json",
            "X-CyberEditor-Worker-Token": self.worker_token,
            "User-Agent": "CyberEditor-Worker/1",
        }
        if payload is not None:
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            headers["Content-Type"] = "application/json"
        request = urllib_request.Request(
            self._url(path, query), data=body, method=method, headers=headers
        )
        try:
            with urllib_request.urlopen(request, timeout=self.timeout) as response:
                result = json.loads(response.read().decode("utf-8"))
        except urllib_error.HTTPError as exc:
            try:
                detail = json.loads(exc.read().decode("utf-8")).get("error")
            except (ValueError, AttributeError):
                detail = None
            raise WorkerProtocolError(str(detail or f"Control plane returned HTTP {exc.code}.")) from exc
        except (urllib_error.URLError, TimeoutError, OSError, ValueError) as exc:
            raise WorkerProtocolError(f"Cannot reach control plane: {exc}") from exc
        if not isinstance(result, dict) or result.get("ok") is not True:
            raise WorkerProtocolError(str(result.get("error") if isinstance(result, dict) else "Invalid response."))
        return result

    def register(self, name: str, environment: Mapping[str, object]) -> Dict[str, object]:
        """Register this PC and publish its latest environment report.

        注册本机并发布最新环境检测结果。
        """
        return self.request_json(
            "POST",
            "/api/worker/register",
            {"worker_id": self.worker_id, "name": name, "environment": dict(environment)},
        )

    def next_job(self) -> Optional[Dict[str, object]]:
        """Poll and atomically claim this PC's next command.

        轮询并原子领取分配给本机的下一条命令。
        """
        result = self.request_json(
            "GET", "/api/worker/next", query={"worker_id": self.worker_id}
        )
        job = result.get("job")
        if job is None:
            return None
        if not isinstance(job, dict):
            raise WorkerProtocolError("Control plane returned an invalid job.")
        return job

    def report(self, job_id: str, payload: Mapping[str, object]) -> bool:
        """Report job state and return whether cancellation was requested.

        上报任务状态，并返回用户是否请求取消。
        """
        result = self.request_json(
            "POST",
            "/api/worker/report",
            {"worker_id": self.worker_id, "job_id": job_id, **dict(payload)},
        )
        return bool(result.get("cancel_requested"))

    def upload_artifact(self, job_id: str, path: Path) -> Dict[str, object]:
        """Stream a preview file without loading it wholly into RAM.

        流式上传预览文件，避免将整段视频读入内存。
        """
        source = path.resolve()
        length = source.stat().st_size
        base_path = self._parsed.path.rstrip("/")
        request_path = base_path + "/api/worker/artifact"
        connection_class = (
            http.client.HTTPSConnection
            if self._parsed.scheme == "https"
            else http.client.HTTPConnection
        )
        connection = connection_class(
            self._parsed.hostname,
            self._parsed.port,
            timeout=max(self.timeout, 600.0),
        )
        try:
            connection.putrequest("POST", request_path)
            connection.putheader("Content-Type", mimetypes.guess_type(source.name)[0] or "application/octet-stream")
            connection.putheader("Content-Length", str(length))
            connection.putheader("X-CyberEditor-Worker-Token", self.worker_token)
            connection.putheader("X-CyberEditor-Worker-Id", self.worker_id)
            connection.putheader("X-CyberEditor-Job-Id", job_id)
            connection.putheader("X-CyberEditor-File-Name", urllib_parse.quote(source.name))
            connection.endheaders()
            with source.open("rb") as handle:
                while True:
                    chunk = handle.read(1024 * 1024)
                    if not chunk:
                        break
                    connection.send(chunk)
            response = connection.getresponse()
            raw = response.read()
            try:
                result = json.loads(raw.decode("utf-8"))
            except ValueError as exc:
                raise WorkerProtocolError(f"Artifact upload returned HTTP {response.status}.") from exc
            if response.status >= 300 or not isinstance(result, dict) or result.get("ok") is not True:
                raise WorkerProtocolError(str(result.get("error") or f"Artifact upload returned HTTP {response.status}."))
            return result
        except (OSError, http.client.HTTPException) as exc:
            raise WorkerProtocolError(f"Preview upload failed: {exc}") from exc
        finally:
            connection.close()


class RemoteWorker:
    """Execute cloud-queued commands on one local Windows workstation.

    在一台本地 Windows 工作站上执行云端排队的命令，始终保持单任务串行。
    """

    def __init__(
        self,
        client: ControlPlaneClient,
        project_root: Path,
        python_executable: str,
        *,
        name: str,
        poll_seconds: float = 2.0,
        upload_preview: bool = True,
        preview_max_mb: int = 256,
    ) -> None:
        """Create a worker around the existing local workflow manager.

        使用现有本地工作流管理器创建 Worker。

        Parameters / 参数:
            client: Authenticated control-plane client. / 已认证控制平面客户端。
            project_root: Local CyberEditor checkout. / 本地项目目录。
            python_executable: Python used for workflow children. / 子工作流 Python。
            name: Friendly device name shown in the Web UI. / 网页显示的设备名。
            poll_seconds: Idle and progress polling interval. / 轮询间隔。
            upload_preview: Upload a low-bitrate review copy. / 是否上传低码率审片副本。
            preview_max_mb: Maximum preview upload size. / 最大预览上传 MB。
        """
        if not project_root.joinpath("main.py").is_file():
            raise ValueError(f"CyberEditor project was not found: {project_root}")
        if not 1 <= preview_max_mb <= 2048:
            raise ValueError("Preview size limit must be between 1 and 2048 MB.")
        self.client = client
        self.project_root = project_root.resolve()
        self.manager = WorkflowManager(
            self.project_root, console_python_executable(python_executable)
        )
        self.name = name
        self.poll_seconds = max(0.25, poll_seconds)
        self.upload_preview = upload_preview
        self.preview_max_bytes = preview_max_mb * 1024 * 1024
        self._stop = False

    def environment(self) -> Dict[str, object]:
        """Detect runtimes installed on this computer, not the cloud host.

        检测本机环境；这里的 Ollama、CUDA 和 Resolve 结果绝不来自部署主机。
        """
        return detect_environment(self.manager.settings())

    def register(self) -> None:
        """Publish one fresh local environment report. / 发布一次最新本机环境报告。"""
        LOGGER.info("Detecting local Ollama, CUDA, FFmpeg, and Resolve / 正在检测本机环境")
        self.client.register(self.name, self.environment())
        LOGGER.info("Worker %s is online / 本机 Worker 已上线", self.client.worker_id)

    def stop(self) -> None:
        """Request a graceful loop shutdown and stop an active child.

        请求安全退出轮询，并停止正在运行的本地子进程。
        """
        self._stop = True
        if self.manager.snapshot().get("running"):
            self.manager.stop()

    def run(self, once: bool = False) -> int:
        """Maintain the outbound poll loop until interrupted.

        持续运行出站轮询，直到被中断。

        Parameters / 参数:
            once: Process at most one poll, useful for service diagnostics.
                最多执行一次轮询，便于服务诊断。
        """
        self.register()
        failures = 0
        last_registration = time.monotonic()
        while not self._stop:
            try:
                if time.monotonic() - last_registration >= 60:
                    self.client.register(self.name, self.environment())
                    last_registration = time.monotonic()
                job = self.client.next_job()
                failures = 0
                if job is not None:
                    self.process_job(job)
                if once:
                    break
                time.sleep(self.poll_seconds)
            except WorkerProtocolError as exc:
                failures += 1
                delay = min(30.0, self.poll_seconds * (2 ** min(failures, 4)))
                LOGGER.warning("%s; retrying in %.1fs / %.1f 秒后重试", exc, delay, delay)
                if once:
                    return 2
                time.sleep(delay)
                if failures % 5 == 0:
                    try:
                        self.register()
                        failures = 0
                        last_registration = time.monotonic()
                    except WorkerProtocolError:
                        pass
        return 0

    def process_job(self, job: Mapping[str, object]) -> None:
        """Execute one already-claimed picker or workflow command.

        执行一条已领取的原生选择器或工作流命令。
        """
        job_id = str(job.get("job_id") or "")
        kind = str(job.get("kind") or "")
        payload = job.get("payload") if isinstance(job.get("payload"), dict) else {}
        LOGGER.info("Claimed %s job %s / 已领取任务", kind, job_id)
        try:
            if kind == "picker":
                paths = native_picker(str(payload.get("kind") or ""))
                self._report_with_retry(
                    job_id,
                    {"state": "succeeded", "stage": "complete", "progress": 100,
                     "result": {"paths": paths}, "return_code": 0},
                )
            elif kind == "workflow":
                self._run_workflow(job_id, payload)
            else:
                raise ValueError(f"Unsupported remote job kind: {kind}")
        except Exception as exc:
            LOGGER.exception("Job %s failed", job_id)
            if self.manager.snapshot().get("running"):
                self.manager.stop()
            try:
                self._report_with_retry(
                    job_id,
                    {
                        "state": "failed", "stage": "failed", "progress": 0,
                        "return_code": 1, "result": {"error": str(exc)},
                        "logs": [{"id": 2_000_000_000, "timestamp": time.time(),
                                  "level": "error", "message": f"Local worker failed: {exc}"}],
                    },
                )
            except WorkerProtocolError:
                LOGGER.exception("Could not report job failure")

    def _report_with_retry(
        self,
        job_id: str,
        payload: Mapping[str, object],
        attempts: int = 5,
    ) -> bool:
        """Retry idempotent status reports across short Internet outages.

        在短暂断网时重试幂等状态上报；控制面按日志源 ID 去重，不会重复显示。

        Parameters / 参数:
            job_id: Server-issued job identifier. / 服务端任务 ID。
            payload: State, progress, result, and incremental logs. / 状态、进度、结果与日志。
            attempts: Maximum send attempts. / 最大尝试次数。
        """
        last_error: Optional[WorkerProtocolError] = None
        for attempt in range(1, max(1, attempts) + 1):
            try:
                return self.client.report(job_id, payload)
            except WorkerProtocolError as exc:
                last_error = exc
                if attempt >= attempts:
                    break
                delay = min(8.0, float(2 ** (attempt - 1)))
                LOGGER.warning(
                    "Status relay interrupted; retry %d/%d in %.0fs / 状态中继中断，稍后重试",
                    attempt, attempts, delay,
                )
                time.sleep(delay)
        assert last_error is not None
        raise last_error

    def _run_workflow(self, job_id: str, payload: Mapping[str, object]) -> None:
        """Run and mirror one strict-serial local workflow.

        运行一个严格串行的本地工作流，并将增量状态镜像到控制平面。
        """
        baseline = int(self.manager.snapshot().get("last_log_id") or 0)
        snapshot = self.manager.start(payload)
        last_log_id = baseline
        stop_sent = False
        while bool(snapshot.get("running")) and not self._stop:
            snapshot = self.manager.snapshot(last_log_id)
            logs = snapshot.get("logs") if isinstance(snapshot.get("logs"), list) else []
            if logs:
                last_log_id = int(snapshot.get("last_log_id") or last_log_id)
            cancel = self._report_with_retry(
                job_id,
                {
                    "state": str(snapshot.get("state") or "running"),
                    "stage": str(snapshot.get("stage") or "running"),
                    "progress": float(snapshot.get("progress") or 0),
                    "return_code": snapshot.get("return_code"),
                    "logs": logs,
                },
            )
            if cancel and not stop_sent:
                self.manager.stop()
                stop_sent = True
            time.sleep(self.poll_seconds)
            snapshot = self.manager.snapshot(last_log_id)
        if self._stop and bool(snapshot.get("running")):
            self.manager.stop()
        snapshot = self.manager.snapshot(last_log_id)
        while bool(snapshot.get("running")):
            time.sleep(0.25)
            snapshot = self.manager.snapshot(last_log_id)
        logs = snapshot.get("logs") if isinstance(snapshot.get("logs"), list) else []
        state = str(snapshot.get("state") or "failed")
        if state not in TERMINAL_STATES:
            state = "failed"
        result: Dict[str, object] = {"data_dir": str(snapshot.get("data_dir") or "")}
        if state == "succeeded" and self.upload_preview:
            try:
                preview = self._prepare_preview()
                if preview is not None:
                    uploaded = self.client.upload_artifact(job_id, preview)
                    result["preview"] = uploaded.get("artifact", {})
                    LOGGER.info("Uploaded web preview %s / 已上传网页预览", preview.name)
            except (OSError, WorkerProtocolError, subprocess.SubprocessError) as exc:
                LOGGER.warning("Preview relay skipped: %s / 预览中继已跳过", exc)
                result["preview_warning"] = str(exc)
        self._report_with_retry(
            job_id,
            {
                "state": state,
                "stage": str(snapshot.get("stage") or state),
                "progress": float(snapshot.get("progress") or 0),
                "return_code": snapshot.get("return_code"),
                "logs": logs,
                "result": result,
            },
        )

    def _prepare_preview(self) -> Optional[Path]:
        """Create a browser-compatible 720p H.264 copy of the newest output.

        将最新输出转换为浏览器兼容的 720p H.264 低码率副本；RAW 素材不会上传。
        """
        outputs = self.manager.output_files()
        if not outputs:
            return None
        source = Path(str(outputs[0]["path"])).resolve()
        environment = build_runtime_environment()
        ffmpeg = shutil.which("ffmpeg", path=environment.get("PATH"))
        if ffmpeg:
            folder = source.parent / "remote-preview"
            folder.mkdir(parents=True, exist_ok=True)
            destination = folder / f"{source.stem}_web_preview.mp4"
            command = [
                ffmpeg, "-hide_banner", "-loglevel", "error", "-y", "-i", str(source),
                "-map", "0:v:0", "-map", "0:a:0?",
                "-vf", "scale=1280:-2:force_original_aspect_ratio=decrease",
                "-c:v", "libx264", "-preset", "veryfast", "-crf", "28",
                "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "128k",
                "-movflags", "+faststart", str(destination),
            ]
            subprocess.run(
                command,
                check=True,
                cwd=str(self.project_root),
                env=environment,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                creationflags=int(getattr(subprocess, "CREATE_NO_WINDOW", 0)) if os.name == "nt" else 0,
            )
            if destination.stat().st_size > self.preview_max_bytes:
                raise OSError(
                    f"Generated preview is {destination.stat().st_size / 1024 / 1024:.1f} MB, "
                    f"above the {self.preview_max_bytes / 1024 / 1024:.0f} MB worker limit."
                )
            return destination
        if source.suffix.casefold() == ".mp4" and source.stat().st_size <= self.preview_max_bytes:
            return source
        raise OSError("FFmpeg is required to create a safe browser preview.")


def build_parser() -> argparse.ArgumentParser:
    """Build the local Windows worker command line. / 构建本机 Windows Worker 命令行。"""
    parser = argparse.ArgumentParser(description="CyberEditor outbound local worker")
    parser.add_argument("--server", required=True, help="Deployed control-plane URL")
    parser.add_argument(
        "--worker-token", default=os.environ.get("CYBEREDITOR_WORKER_TOKEN", ""),
        help="Dedicated worker token (or CYBEREDITOR_WORKER_TOKEN)",
    )
    parser.add_argument("--worker-id", default=_default_worker_id())
    parser.add_argument("--name", default=os.environ.get("COMPUTERNAME") or socket.gethostname())
    parser.add_argument("--project-root", default=str(Path(__file__).resolve().parents[1]))
    parser.add_argument("--python-executable", default=sys.executable)
    parser.add_argument("--poll-seconds", type=float, default=2.0)
    parser.add_argument("--preview-max-mb", type=int, default=256)
    parser.add_argument("--no-preview-upload", action="store_true")
    parser.add_argument("--allow-insecure-http", action="store_true")
    parser.add_argument("--once", action="store_true", help="Register and poll only once")
    parser.add_argument("--log-level", default="INFO", choices=("DEBUG", "INFO", "WARNING", "ERROR"))
    return parser


def run_worker(argv: Optional[Sequence[str]] = None) -> int:
    """Run the outbound worker service. / 运行出站 Worker 服务。"""
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=getattr(logging, args.log_level),
        format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    )
    client = ControlPlaneClient(
        args.server,
        args.worker_token,
        args.worker_id,
        allow_insecure_http=args.allow_insecure_http,
    )
    worker = RemoteWorker(
        client,
        Path(args.project_root),
        args.python_executable,
        name=args.name,
        poll_seconds=args.poll_seconds,
        upload_preview=not args.no_preview_upload,
        preview_max_mb=args.preview_max_mb,
    )
    try:
        return worker.run(once=args.once)
    except KeyboardInterrupt:
        LOGGER.info("Stopping local worker / 正在停止本机 Worker")
        worker.stop()
        return 130


def main() -> int:
    """CLI entry point. / 命令行入口。"""
    return run_worker()


if __name__ == "__main__":
    raise SystemExit(main())
