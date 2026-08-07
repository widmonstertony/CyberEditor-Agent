#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Local-first browser controller for CyberEditor-Agent.

CyberEditor-Agent 的本地优先浏览器控制台。

The module intentionally uses only the Python standard library.  It never
imports Torch, Whisper, OpenCV, or Resolve and therefore preserves the strict
serial VRAM lifecycle implemented by :mod:`main`.
"""

from __future__ import annotations

import argparse
from collections import deque
from dataclasses import asdict, fields
import json
import logging
import mimetypes
import os
from pathlib import Path
import re
import secrets
import shutil
import subprocess
import sys
import threading
import time
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Deque, Dict, List, Mapping, Optional, Sequence, Tuple
from urllib import parse as urllib_parse

from .gui import (
    WorkflowOptions,
    build_runtime_environment,
    console_python_executable,
    detect_hardware,
    detect_media_fps,
    detect_torch_runtime,
    recommend_automatic_settings,
)
from .media_manifest import MediaManifestError, discover_video_files
from .runtime_services import (
    RuntimeServiceError,
    ensure_ollama_service,
    fetch_ollama_models,
    get_resolve_registration,
)


LOGGER = logging.getLogger("cybereditor.web")
VIDEO_SUFFIXES = {".mp4", ".mov", ".mkv", ".avi", ".mxf", ".mts", ".m2ts"}
OUTPUT_SUFFIXES = {".mp4", ".mov", ".mkv", ".webm", ".m4v"}
MAX_JSON_BYTES = 2 * 1024 * 1024
MAX_LOG_LINES = 5000


def _json_bytes(payload: object) -> bytes:
    """Serialize a JSON response. / 将 HTTP 响应序列化为 JSON。"""
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def _atomic_write_json(path: Path, payload: Mapping[str, object]) -> None:
    """Atomically save browser settings. / 原子保存浏览器界面设置。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def _is_loopback(host: str) -> bool:
    """Return whether a bind address is local-only. / 判断监听地址是否仅限本机。"""
    return host.casefold() in {"127.0.0.1", "localhost", "::1"}


def _hidden_creation_flags() -> int:
    """Return the Windows no-console flag. / 返回 Windows 隐藏控制台标志。"""
    return int(getattr(subprocess, "CREATE_NO_WINDOW", 0)) if os.name == "nt" else 0


class WorkflowManager:
    """Own exactly one serial workflow process and its observable state.

    严格管理一个串行工作流进程及其可观察状态，避免两个浏览器标签页同时争用 GPU。
    """

    def __init__(self, project_root: Path, python_executable: str) -> None:
        """Create an idle manager. / 创建空闲的工作流管理器。"""
        self.project_root = project_root.resolve()
        self.python_executable = console_python_executable(python_executable)
        self.settings_path = self.project_root / "data" / "ui-settings.json"
        self._lock = threading.RLock()
        self._process: Optional[subprocess.Popen[str]] = None
        self._reader: Optional[threading.Thread] = None
        self._logs: Deque[Dict[str, object]] = deque(maxlen=MAX_LOG_LINES)
        self._sequence = 0
        self._state = "idle"
        self._stage = "ready"
        self._progress = 0.0
        self._started_at = 0.0
        self._finished_at = 0.0
        self._return_code: Optional[int] = None
        self._command: List[str] = []
        self._active_data_dir = self.project_root / "data" / "ui-run"

    def _append_log(self, message: str, level: str = "info") -> None:
        """Append one bounded log event. / 追加一条有界日志事件。"""
        clean = message.rstrip("\r\n")
        if not clean:
            return
        with self._lock:
            self._sequence += 1
            self._logs.append(
                {
                    "id": self._sequence,
                    "timestamp": time.time(),
                    "level": level,
                    "message": clean,
                }
            )
            self._update_progress(clean)

    def _update_progress(self, line: str) -> None:
        """Estimate coarse overall progress from stable workflow log markers.

        根据工作流稳定日志标记估算总体进度；模型内部无法给出准确百分比时保持阶段进度。
        """
        folded = line.casefold()
        match = re.search(r"(?:提取|extract)[^0-9]*(\d+)\s*/\s*(\d+)", line, re.I)
        if match:
            current, total = int(match.group(1)), max(1, int(match.group(2)))
            self._stage = "extract"
            self._progress = max(self._progress, min(35.0, 5.0 + 30.0 * current / total))
            return
        markers: Sequence[Tuple[Sequence[str], str, float]] = (
            (("treatment", "导演阐述", "music brief"), "treatment", 40.0),
            (("music retrieval", "音乐搜索", "music analysis", "音乐听诊"), "music", 50.0),
            (("picture assembly", "supervising editor", "quality-gate", "final director"), "director", 62.0),
            (("rough-cut", "blind review", "review master"), "review", 74.0),
            (("resolve", "时间线", "assembly"), "resolve", 86.0),
            (("render", "渲染", "deliver"), "render", 94.0),
        )
        for terms, stage, progress in markers:
            if any(term in folded for term in terms):
                self._stage = stage
                self._progress = max(self._progress, progress)
                break

    def _load_saved_settings(self) -> Dict[str, object]:
        """Load known GUI settings without trusting arbitrary keys.

        读取桌面版已保存设置，但不信任或传播未知字段。
        """
        try:
            payload = json.loads(self.settings_path.read_text(encoding="utf-8-sig"))
        except (OSError, ValueError):
            return {}
        return payload if isinstance(payload, dict) else {}

    def settings(self) -> Dict[str, object]:
        """Return complete safe browser defaults. / 返回完整且安全的浏览器默认设置。"""
        defaults = asdict(WorkflowOptions())
        allowed = {item.name for item in fields(WorkflowOptions)}
        saved = self._load_saved_settings()
        defaults.update({key: value for key, value in saved.items() if key in allowed})
        defaults["videos"] = [str(value) for value in defaults.get("videos", []) if str(value).strip()]
        return defaults

    def _resolve_auto_fps(self, options: WorkflowOptions) -> None:
        """Resolve automatic FPS from source media or existing workflow data.

        从源素材或已有工作流数据读取自动 FPS，绝不按硬件猜测帧率。
        """
        if options.fps_mode != "auto":
            return
        explicit = [Path(value).expanduser() for value in options.videos if str(value).strip()]
        if not explicit and options.video:
            explicit = [Path(options.video).expanduser()]
        if not explicit and options.input_folder:
            try:
                explicit = discover_video_files([], Path(options.input_folder).expanduser())
            except MediaManifestError as exc:
                raise ValueError(str(exc)) from exc
        if explicit:
            options.project_fps = detect_media_fps(explicit[0])
            return
        raw_path = Path(options.data_dir).expanduser()
        if not raw_path.is_absolute():
            raw_path = self.project_root / raw_path
        raw_path = raw_path / "raw_data.json"
        try:
            raw = json.loads(raw_path.read_text(encoding="utf-8-sig"))
            fps = float(raw.get("project_fps") or raw.get("source_fps") or 0)
        except (OSError, ValueError, TypeError):
            fps = 0.0
        if fps > 0:
            options.project_fps = fps

    def build_options(self, payload: Mapping[str, object]) -> WorkflowOptions:
        """Merge, normalize, and validate one browser submission.

        合并、规范化并校验一次浏览器提交，拒绝未知字段和命令注入。
        """
        allowed = {item.name for item in fields(WorkflowOptions)}
        unknown = sorted(set(payload) - allowed)
        if unknown:
            raise ValueError("Unknown option fields: " + ", ".join(unknown))
        merged = self.settings()
        merged.update(payload)
        merged["videos"] = [
            str(value).strip()
            for value in (merged.get("videos") or [])
            if str(value).strip()
        ]
        options = WorkflowOptions(**{key: merged[key] for key in allowed if key in merged})
        self._resolve_auto_fps(options)
        options.validate(self.project_root)
        return options

    def start(self, payload: Mapping[str, object]) -> Dict[str, object]:
        """Start one validated workflow child process. / 启动一个已校验的工作流子进程。"""
        with self._lock:
            if self._process is not None and self._process.poll() is None:
                raise RuntimeError("A workflow is already running.")
            options = self.build_options(payload)
            command = options.build_command(self.python_executable, self.project_root)
            data_dir = Path(options.data_dir).expanduser()
            if not data_dir.is_absolute():
                data_dir = self.project_root / data_dir
            self._active_data_dir = data_dir.resolve()
            self._state = "starting"
            self._stage = "starting"
            self._progress = 1.0
            self._started_at = time.time()
            self._finished_at = 0.0
            self._return_code = None
            self._command = command
            _atomic_write_json(self.settings_path, asdict(options))
            self._append_log("Starting strict serial workflow / 启动严格串行工作流")
            try:
                self._process = subprocess.Popen(
                    command,
                    cwd=str(self.project_root),
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    stdin=subprocess.DEVNULL,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    bufsize=1,
                    env=build_runtime_environment(),
                    creationflags=_hidden_creation_flags(),
                )
            except OSError:
                self._state = "failed"
                self._finished_at = time.time()
                raise
            self._state = "running"
            self._reader = threading.Thread(target=self._read_output, daemon=True)
            self._reader.start()
            return self.snapshot()

    def _read_output(self) -> None:
        """Drain child output and finalize state. / 持续读取子进程输出并完成状态收尾。"""
        with self._lock:
            process = self._process
        if process is None:
            return
        stream = process.stdout
        if stream is not None:
            for line in stream:
                level = "error" if "| ERROR |" in line or "失败" in line else (
                    "warning" if "| WARNING |" in line else "info"
                )
                self._append_log(line, level)
        return_code = process.wait()
        with self._lock:
            self._return_code = return_code
            self._finished_at = time.time()
            if self._state == "stopping":
                self._state = "stopped"
                self._stage = "stopped"
            elif return_code == 0:
                self._state = "succeeded"
                self._stage = "complete"
                self._progress = 100.0
            else:
                self._state = "failed"
                self._stage = "failed"
            self._process = None
        self._append_log(
            f"Workflow finished with exit code {return_code} / 工作流结束，退出码 {return_code}",
            "info" if return_code == 0 else "error",
        )

    def stop(self) -> Dict[str, object]:
        """Stop only the owned workflow process tree. / 仅停止本服务拥有的工作流进程树。"""
        with self._lock:
            process = self._process
            if process is None or process.poll() is not None:
                return self.snapshot()
            self._state = "stopping"
            self._stage = "stopping"
            pid = process.pid
        if os.name == "nt":
            subprocess.run(
                ["taskkill", "/PID", str(pid), "/T", "/F"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
                creationflags=_hidden_creation_flags(),
            )
        else:
            process.terminate()
        self._append_log("Stop requested / 已请求停止", "warning")
        return self.snapshot()

    def snapshot(self, since: int = 0) -> Dict[str, object]:
        """Return state and incremental log events. / 返回状态及增量日志事件。"""
        with self._lock:
            now = time.time()
            end = self._finished_at or now
            elapsed = max(0.0, end - self._started_at) if self._started_at else 0.0
            logs = [dict(item) for item in self._logs if int(item["id"]) > since]
            return {
                "state": self._state,
                "stage": self._stage,
                "progress": round(self._progress, 2),
                "started_at": self._started_at,
                "finished_at": self._finished_at,
                "elapsed_sec": round(elapsed, 1),
                "return_code": self._return_code,
                "last_log_id": self._sequence,
                "logs": logs,
                "running": self._process is not None and self._process.poll() is None,
                "data_dir": str(self._active_data_dir),
            }

    def output_files(self) -> List[Dict[str, object]]:
        """List preview/final videos inside the active data directory.

        列出活动数据目录中的预览与最终视频，排除素材缓存目录。
        """
        root = self._active_data_dir
        candidates: List[Path] = []
        for folder in (root, root / "final", root / "review"):
            if not folder.is_dir():
                continue
            candidates.extend(
                path for path in folder.iterdir()
                if path.is_file() and path.suffix.casefold() in OUTPUT_SUFFIXES
            )
        unique = sorted(set(candidates), key=lambda item: item.stat().st_mtime, reverse=True)
        return [
            {
                "name": path.name,
                "path": str(path),
                "size": path.stat().st_size,
                "modified": path.stat().st_mtime,
                "url": "/api/output/file?path=" + urllib_parse.quote(str(path)),
            }
            for path in unique[:20]
        ]

    def authorize_output_path(self, value: str) -> Path:
        """Resolve an output path and confine it to the active data directory.

        解析输出路径并限制在活动数据目录内，防止通过浏览器读取任意文件。
        """
        path = Path(value).expanduser().resolve()
        root = self._active_data_dir.resolve()
        try:
            path.relative_to(root)
        except ValueError as exc:
            raise PermissionError("Output path is outside the active data directory.") from exc
        if not path.is_file() or path.suffix.casefold() not in OUTPUT_SUFFIXES:
            raise FileNotFoundError(path)
        return path


def detect_environment(settings: Mapping[str, object]) -> Dict[str, object]:
    """Probe local runtimes without retaining GPU resources.

    在一次性子进程中检查本地运行时，不在 Web 服务中保留 GPU 资源。
    """
    ollama_url = str(settings.get("ollama_url") or "http://localhost:11434")
    ollama_error = ""
    try:
        ensure_ollama_service(ollama_url)
        models = fetch_ollama_models(ollama_url, timeout=5)
    except (RuntimeServiceError, OSError, ValueError) as exc:
        models = []
        ollama_error = str(exc)
    hardware = detect_hardware()
    hardware.update(detect_torch_runtime())
    recommendation = recommend_automatic_settings(hardware, models)
    registration = get_resolve_registration()
    environment = build_runtime_environment()
    return {
        "python": {"ok": True, "version": sys.version.split()[0]},
        "ffmpeg": {"ok": bool(shutil.which("ffmpeg", path=environment.get("PATH")))},
        "ollama": {
            "ok": bool(models),
            "models": models,
            "error": ollama_error,
        },
        "resolve": registration,
        "hardware": hardware,
        "recommendation": recommendation,
    }


def native_picker(kind: str) -> List[str]:
    """Open a Windows-native server-side media picker.

    打开 Windows 原生的服务器端素材选择器；远程或无桌面部署时会明确失败。
    """
    if os.name != "nt":
        raise RuntimeError("Native picker is available only on Windows hosts.")
    powershell = shutil.which("powershell.exe") or shutil.which("powershell")
    if not powershell:
        raise RuntimeError("PowerShell was not found.")
    if kind == "videos":
        script = (
            "Add-Type -AssemblyName System.Windows.Forms;"
            "$d=New-Object System.Windows.Forms.OpenFileDialog;"
            "$d.Multiselect=$true;"
            "$d.Filter='Video files|*.mp4;*.mov;*.mkv;*.avi;*.mxf;*.mts;*.m2ts|All files|*.*';"
            "if($d.ShowDialog() -eq [System.Windows.Forms.DialogResult]::OK){"
            "$d.FileNames | ConvertTo-Json -Compress}"
        )
    elif kind == "folder":
        script = (
            "Add-Type -AssemblyName System.Windows.Forms;"
            "$d=New-Object System.Windows.Forms.FolderBrowserDialog;"
            "$d.Description='Select media folder / 选择素材文件夹';"
            "if($d.ShowDialog() -eq [System.Windows.Forms.DialogResult]::OK){"
            "@($d.SelectedPath) | ConvertTo-Json -Compress}"
        )
    else:
        raise ValueError("Unknown picker kind.")
    result = subprocess.run(
        [powershell, "-NoProfile", "-STA", "-Command", script],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=600,
        check=False,
        creationflags=_hidden_creation_flags(),
    )
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or "Native picker failed.")
    if not result.stdout.strip():
        return []
    payload = json.loads(result.stdout)
    values = payload if isinstance(payload, list) else [payload]
    return [str(value) for value in values if str(value).strip()]


class CyberEditorHTTPServer(ThreadingHTTPServer):
    """Threaded server carrying application state. / 携带应用状态的多线程服务器。"""

    daemon_threads = True
    allow_reuse_address = True

    def __init__(
        self,
        address: Tuple[str, int],
        handler: type[BaseHTTPRequestHandler],
        manager: WorkflowManager,
        static_root: Path,
        token: str,
    ) -> None:
        """Attach shared manager, static root, and API token. / 绑定管理器、静态目录与令牌。"""
        super().__init__(address, handler)
        self.manager = manager
        self.static_root = static_root.resolve()
        self.api_token = token


class CyberEditorHandler(BaseHTTPRequestHandler):
    """Serve the browser app and authenticated JSON API. / 提供浏览器应用及认证 JSON API。"""

    server: CyberEditorHTTPServer
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt: str, *args: object) -> None:
        """Route access messages to logging. / 将访问日志交给 logging。"""
        LOGGER.info("%s - %s", self.address_string(), fmt % args)

    def _authorized(self) -> bool:
        """Validate the optional API bearer token. / 校验可选 API 访问令牌。"""
        expected = self.server.api_token
        if not expected:
            return True
        supplied = self.headers.get("X-CyberEditor-Token", "")
        if not supplied:
            query = urllib_parse.parse_qs(urllib_parse.urlparse(self.path).query)
            supplied = str((query.get("token") or [""])[0])
        return secrets.compare_digest(supplied, expected)

    def _send_bytes(
        self,
        body: bytes,
        content_type: str,
        status: HTTPStatus = HTTPStatus.OK,
        extra_headers: Optional[Mapping[str, str]] = None,
    ) -> None:
        """Send a complete HTTP response. / 发送完整 HTTP 响应。"""
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Content-Security-Policy", "default-src 'self'; style-src 'self'; script-src 'self'; media-src 'self' blob:; img-src 'self' data:; connect-src 'self'")
        for key, value in (extra_headers or {}).items():
            self.send_header(key, value)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _send_json(self, payload: object, status: HTTPStatus = HTTPStatus.OK) -> None:
        """Send a JSON API response. / 发送 JSON API 响应。"""
        self._send_bytes(_json_bytes(payload), "application/json; charset=utf-8", status)

    def _error(self, status: HTTPStatus, message: str) -> None:
        """Send a stable error envelope. / 发送稳定的错误响应结构。"""
        self._send_json({"ok": False, "error": message}, status)

    def _read_json(self) -> Dict[str, object]:
        """Read a bounded JSON request body. / 读取有大小上限的 JSON 请求体。"""
        content_type = self.headers.get("Content-Type", "").split(";", 1)[0].strip().casefold()
        if content_type != "application/json":
            # Requiring a non-simple request content type prevents another web
            # origin from silently POSTing commands to a loopback deployment.
            raise ValueError("Content-Type must be application/json.")
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError as exc:
            raise ValueError("Invalid Content-Length.") from exc
        if length <= 0 or length > MAX_JSON_BYTES:
            raise ValueError("JSON body is empty or too large.")
        payload = json.loads(self.rfile.read(length).decode("utf-8"))
        if not isinstance(payload, dict):
            raise ValueError("JSON body must be an object.")
        return payload

    def _require_api_auth(self) -> bool:
        """Reject unauthorized API calls. / 拒绝未认证的 API 调用。"""
        if self._authorized():
            return True
        self._error(HTTPStatus.UNAUTHORIZED, "Invalid or missing API token.")
        return False

    def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        """Handle static, state, environment, and output requests.

        处理静态资源、状态、环境检测和输出文件请求。
        """
        parsed = urllib_parse.urlparse(self.path)
        if parsed.path.startswith("/api/") and not self._require_api_auth():
            return
        try:
            if parsed.path == "/api/capabilities":
                self._send_json(
                    {
                        "ok": True,
                        "mode": "local",
                        "picker": "server",
                        "preview_relay": False,
                    }
                )
            elif parsed.path == "/api/config":
                self._send_json({"ok": True, "config": self.server.manager.settings()})
            elif parsed.path == "/api/status":
                query = urllib_parse.parse_qs(parsed.query)
                since = int((query.get("since") or ["0"])[0])
                self._send_json({"ok": True, **self.server.manager.snapshot(since)})
            elif parsed.path == "/api/environment":
                payload = detect_environment(self.server.manager.settings())
                self._send_json({"ok": True, "environment": payload})
            elif parsed.path == "/api/outputs":
                self._send_json({"ok": True, "outputs": self.server.manager.output_files()})
            elif parsed.path == "/api/output/file":
                query = urllib_parse.parse_qs(parsed.query)
                value = str((query.get("path") or [""])[0])
                self._send_file(self.server.manager.authorize_output_path(value), allow_range=True)
            else:
                self._serve_static(parsed.path)
        except (ValueError, PermissionError) as exc:
            self._error(HTTPStatus.BAD_REQUEST, str(exc))
        except FileNotFoundError:
            self._error(HTTPStatus.NOT_FOUND, "Not found.")
        except Exception as exc:  # Keep the browser API alive and observable.
            LOGGER.exception("GET %s failed", parsed.path)
            self._error(HTTPStatus.INTERNAL_SERVER_ERROR, str(exc))

    def do_POST(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        """Handle workflow and native picker mutations. / 处理工作流及原生选择器操作。"""
        parsed = urllib_parse.urlparse(self.path)
        if not self._require_api_auth():
            return
        try:
            payload = self._read_json()
            if parsed.path == "/api/workflow/start":
                state = self.server.manager.start(payload)
                self._send_json({"ok": True, **state}, HTTPStatus.ACCEPTED)
            elif parsed.path == "/api/workflow/stop":
                self._send_json({"ok": True, **self.server.manager.stop()})
            elif parsed.path == "/api/picker":
                self._send_json({"ok": True, "paths": native_picker(str(payload.get("kind") or ""))})
            else:
                self._error(HTTPStatus.NOT_FOUND, "Unknown API endpoint.")
        except (ValueError, RuntimeError, OSError, json.JSONDecodeError) as exc:
            self._error(HTTPStatus.CONFLICT if isinstance(exc, RuntimeError) else HTTPStatus.BAD_REQUEST, str(exc))
        except Exception as exc:
            LOGGER.exception("POST %s failed", parsed.path)
            self._error(HTTPStatus.INTERNAL_SERVER_ERROR, str(exc))

    def _serve_static(self, request_path: str) -> None:
        """Serve an allow-listed static path. / 提供白名单内的静态资源。"""
        mapping = {
            "/": "index.html",
            "/index.html": "index.html",
            "/app.js": "app.js",
            "/styles.css": "styles.css",
        }
        name = mapping.get(request_path)
        if not name:
            raise FileNotFoundError(request_path)
        path = self.server.static_root / name
        self._send_file(path, allow_range=False)

    def _send_file(self, path: Path, allow_range: bool) -> None:
        """Stream a static or media file with optional byte ranges.

        以流式方式发送静态或媒体文件，并可选支持视频字节范围请求。
        """
        size = path.stat().st_size
        start, end = 0, size - 1
        status = HTTPStatus.OK
        if allow_range and self.headers.get("Range"):
            match = re.fullmatch(r"bytes=(\d*)-(\d*)", self.headers["Range"].strip())
            if not match:
                self._error(HTTPStatus.REQUESTED_RANGE_NOT_SATISFIABLE, "Invalid byte range.")
                return
            if match.group(1):
                start = int(match.group(1))
                if match.group(2):
                    end = min(end, int(match.group(2)))
            elif match.group(2):
                suffix = int(match.group(2))
                if suffix <= 0:
                    self._error(
                        HTTPStatus.REQUESTED_RANGE_NOT_SATISFIABLE,
                        "Invalid byte suffix range.",
                    )
                    return
                start = max(0, size - suffix)
            else:
                self._error(
                    HTTPStatus.REQUESTED_RANGE_NOT_SATISFIABLE,
                    "Empty byte range.",
                )
                return
            if start > end or start >= size:
                self._error(HTTPStatus.REQUESTED_RANGE_NOT_SATISFIABLE, "Byte range is outside the file.")
                return
            status = HTTPStatus.PARTIAL_CONTENT
        length = end - start + 1
        content_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(length))
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("X-Content-Type-Options", "nosniff")
        if status == HTTPStatus.PARTIAL_CONTENT:
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        self.end_headers()
        if self.command == "HEAD":
            return
        with path.open("rb") as handle:
            handle.seek(start)
            remaining = length
            while remaining:
                chunk = handle.read(min(1024 * 1024, remaining))
                if not chunk:
                    break
                self.wfile.write(chunk)
                remaining -= len(chunk)


def build_parser() -> argparse.ArgumentParser:
    """Build the web-server CLI parser. / 构建 Web 服务命令行解析器。"""
    parser = argparse.ArgumentParser(description="CyberEditor-Agent local web UI")
    parser.add_argument("--host", default="127.0.0.1", help="Bind address")
    parser.add_argument("--port", type=int, default=8765, help="TCP port")
    parser.add_argument("--token", default="", help="Required API token for non-loopback binds")
    parser.add_argument("--no-browser", action="store_true", help="Do not open the default browser")
    parser.add_argument("--log-level", default="INFO", choices=("DEBUG", "INFO", "WARNING", "ERROR"))
    return parser


def run_server(argv: Optional[Sequence[str]] = None) -> int:
    """Run the local/deployable browser control server.

    运行本地或可部署的浏览器控制服务；非回环地址必须显式设置访问令牌。
    """
    args = build_parser().parse_args(argv)
    if not 1 <= args.port <= 65535:
        raise SystemExit("--port must be between 1 and 65535")
    if not _is_loopback(args.host) and len(args.token) < 16:
        raise SystemExit("Non-loopback deployment requires --token with at least 16 characters.")
    logging.basicConfig(
        level=getattr(logging, args.log_level),
        format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    )
    project_root = Path(__file__).resolve().parents[1]
    static_root = project_root / "web"
    if not (static_root / "index.html").is_file():
        raise SystemExit(f"Web assets not found: {static_root}")
    manager = WorkflowManager(project_root, sys.executable)
    server = CyberEditorHTTPServer(
        (args.host, args.port), CyberEditorHandler, manager, static_root, args.token
    )
    display_host = "127.0.0.1" if args.host in {"0.0.0.0", "::"} else args.host
    url = f"http://{display_host}:{args.port}/"
    LOGGER.info("CyberEditor web UI listening on %s", url)
    if not args.no_browser:
        import webbrowser

        threading.Timer(0.6, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever(poll_interval=0.4)
    except KeyboardInterrupt:
        LOGGER.info("Web UI stopped by user")
    finally:
        if manager.snapshot().get("running"):
            manager.stop()
        server.server_close()
    return 0


def main() -> int:
    """CLI entry point. / 命令行入口。"""
    return run_server()


if __name__ == "__main__":
    raise SystemExit(main())
