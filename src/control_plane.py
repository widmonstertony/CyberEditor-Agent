#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Deployable control plane for outbound-connected CyberEditor workers.

CyberEditor 本机 Worker 主动出站连接的可部署控制平面。

The control plane never sees source media paths as files and never connects to
Resolve, Ollama, or CUDA directly.  It stores job metadata, bounded logs, and
optional web-ready previews uploaded by an authenticated Windows worker.
"""

from __future__ import annotations

import argparse
import json
import logging
import mimetypes
import os
from pathlib import Path
import re
import secrets
import sqlite3
import threading
import time
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple
from urllib import parse as urllib_parse
import uuid


LOGGER = logging.getLogger("cybereditor.control_plane")
MAX_JSON_BYTES = 2 * 1024 * 1024
ONLINE_WINDOW_SEC = 30.0
TERMINAL_STATES = {"succeeded", "failed", "stopped"}
WORKER_ID_PATTERN = re.compile(r"[A-Za-z0-9_.-]{1,80}")

DEFAULT_REMOTE_CONFIG: Dict[str, object] = {
    "video": "", "videos": [], "input_folder": "", "proxy": "",
    "proxy_folder": "", "data_dir": "data/ui-run", "flow": "full",
    "hardware_profile": "auto", "theme": "system", "ui_language": "system",
    "fps_mode": "auto", "project_fps": 25.0, "whisper_model": "small",
    "whisper_device": "auto", "language": "", "ollama_model": "",
    "director_model": "", "ollama_url": "http://localhost:11434",
    "chunk_minutes": 12.0, "num_ctx": 8192, "creative_brief": "",
    "target_duration_sec": 0.0, "camera_profile": "auto",
    "music_folder": "", "music_provider": "off", "music_candidate_limit": 8,
    "jamendo_client_id": "", "music_rights_confirmed": False,
    "music_rights_claim": "", "timeline_name": "CyberEditor Timeline",
    "project_name": "CyberEditor Project", "skip_resolve": False,
    "strict_fps": True, "render_preview": True, "drx_root": "config/drx",
    "fairlight_preset": "", "macro_profile": "", "render_final": True,
    "render_dir": "data/ui-run/final", "render_name": "CyberEditor_final",
    "render_preset": "",
}


def _json_bytes(payload: object) -> bytes:
    """Serialize compact UTF-8 JSON. / 序列化紧凑 UTF-8 JSON。"""
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def _safe_name(value: str, fallback: str = "preview.mp4") -> str:
    """Return a filesystem-safe artifact name. / 返回安全的预览文件名。"""
    name = Path(urllib_parse.unquote(value)).name
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "_", name).strip("._")
    return cleaned[:160] or fallback


def _worker_id(value: str) -> str:
    """Validate one worker identifier at the trust boundary. / 在信任边界校验 Worker ID。"""
    if not WORKER_ID_PATTERN.fullmatch(value):
        raise ValueError(
            "Worker ID may contain only letters, digits, dot, underscore, and dash."
        )
    return value


class ControlPlaneStore:
    """Persist workers, jobs, events, and optional previews in SQLite.

    使用 SQLite 持久化 Worker、任务、事件和可选预览，支持单进程多线程部署。
    """

    def __init__(
        self,
        database: Path,
        storage_root: Path,
        *,
        max_storage_bytes: Optional[int] = None,
        artifact_retention_seconds: Optional[float] = None,
    ) -> None:
        """Open the database and create the schema. / 打开数据库并创建表结构。"""
        self.database = database.resolve()
        self.storage_root = storage_root.resolve()
        self.max_storage_bytes = max_storage_bytes or int(os.environ.get("CYBEREDITOR_MAX_STORAGE_MB", "512")) * 1024 * 1024
        self.artifact_retention_seconds = artifact_retention_seconds or float(os.environ.get("CYBEREDITOR_ARTIFACT_RETENTION_DAYS", "7")) * 86400
        if self.max_storage_bytes < 1 or self.artifact_retention_seconds < 1:
            raise ValueError("Artifact storage and retention limits must be positive.")
        self.database.parent.mkdir(parents=True, exist_ok=True)
        self.storage_root.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._condition = threading.Condition(self._lock)
        self._connection = sqlite3.connect(
            str(self.database), check_same_thread=False, isolation_level=None
        )
        self._connection.row_factory = sqlite3.Row
        self._connection.execute("PRAGMA journal_mode=WAL")
        self._connection.execute("PRAGMA foreign_keys=ON")
        self._create_schema()
        self.prune_artifacts()

    def __enter__(self) -> "ControlPlaneStore":
        """Return this open store for context-manager use. / 返回已打开的上下文数据库。"""
        return self

    def __exit__(self, *_: object) -> None:
        """Close SQLite when leaving a context. / 离开上下文时关闭 SQLite。"""
        self.close()

    def close(self) -> None:
        """Close the SQLite handle so Windows can release the database file.

        关闭 SQLite 句柄，使 Windows 可以立即释放数据库文件。
        """
        with self._lock:
            self._connection.close()

    def _create_schema(self) -> None:
        """Create idempotent control-plane tables. / 幂等创建控制平面数据表。"""
        with self._lock:
            self._connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS workers (
                    worker_id TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    environment_json TEXT NOT NULL DEFAULT '{}',
                    status TEXT NOT NULL DEFAULT 'offline',
                    active_job_id TEXT,
                    last_seen REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS jobs (
                    job_id TEXT PRIMARY KEY,
                    worker_id TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    state TEXT NOT NULL,
                    stage TEXT NOT NULL DEFAULT 'queued',
                    progress REAL NOT NULL DEFAULT 0,
                    payload_json TEXT NOT NULL,
                    result_json TEXT NOT NULL DEFAULT '{}',
                    cancel_requested INTEGER NOT NULL DEFAULT 0,
                    created_at REAL NOT NULL,
                    claimed_at REAL,
                    updated_at REAL NOT NULL,
                    return_code INTEGER,
                    FOREIGN KEY(worker_id) REFERENCES workers(worker_id)
                );
                CREATE INDEX IF NOT EXISTS jobs_worker_created
                    ON jobs(worker_id, created_at DESC);
                CREATE TABLE IF NOT EXISTS events (
                    event_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    job_id TEXT NOT NULL,
                    source_id INTEGER NOT NULL,
                    timestamp REAL NOT NULL,
                    level TEXT NOT NULL,
                    message TEXT NOT NULL,
                    UNIQUE(job_id, source_id),
                    FOREIGN KEY(job_id) REFERENCES jobs(job_id) ON DELETE CASCADE
                );
                CREATE TABLE IF NOT EXISTS artifacts (
                    artifact_id TEXT PRIMARY KEY,
                    job_id TEXT NOT NULL,
                    name TEXT NOT NULL,
                    path TEXT NOT NULL,
                    mime_type TEXT NOT NULL,
                    size INTEGER NOT NULL,
                    created_at REAL NOT NULL,
                    FOREIGN KEY(job_id) REFERENCES jobs(job_id) ON DELETE CASCADE
                );
                """
            )

    @staticmethod
    def _decode(value: str) -> Dict[str, object]:
        """Decode a stored JSON object. / 解码数据库中的 JSON 对象。"""
        try:
            payload = json.loads(value or "{}")
        except ValueError:
            return {}
        return payload if isinstance(payload, dict) else {}

    def register_worker(
        self, worker_id: str, name: str, environment: Mapping[str, object]
    ) -> Dict[str, object]:
        """Register or refresh an outbound worker. / 注册或刷新主动出站 Worker。"""
        worker_id = _worker_id(worker_id)
        name = str(name).strip()[:120]
        if not name:
            raise ValueError("Worker name cannot be empty.")
        now = time.time()
        with self._lock:
            self._connection.execute(
                """INSERT INTO workers(worker_id,name,environment_json,status,last_seen)
                   VALUES(?,?,?,?,?)
                   ON CONFLICT(worker_id) DO UPDATE SET
                     name=excluded.name,
                     environment_json=excluded.environment_json,
                     last_seen=excluded.last_seen,
                     status=CASE WHEN workers.active_job_id IS NULL THEN 'online' ELSE workers.status END""",
                (worker_id, name, json.dumps(environment, ensure_ascii=False), "online", now),
            )
        return self.worker(worker_id)

    def heartbeat(self, worker_id: str) -> None:
        """Refresh worker liveness. / 刷新 Worker 在线时间。"""
        worker_id = _worker_id(worker_id)
        with self._lock:
            cursor = self._connection.execute(
                "UPDATE workers SET last_seen=? WHERE worker_id=?",
                (time.time(), worker_id),
            )
            if cursor.rowcount == 0:
                raise KeyError("Unknown worker; register first.")

    def worker(self, worker_id: str) -> Dict[str, object]:
        """Return one normalized worker. / 返回一个规范化 Worker。"""
        worker_id = _worker_id(worker_id)
        with self._lock:
            row = self._connection.execute(
                "SELECT * FROM workers WHERE worker_id=?", (worker_id,)
            ).fetchone()
        if row is None:
            raise KeyError("Worker not found.")
        online = time.time() - float(row["last_seen"]) <= ONLINE_WINDOW_SEC
        return {
            "worker_id": row["worker_id"], "name": row["name"],
            "environment": self._decode(row["environment_json"]),
            "status": row["status"] if online else "offline",
            "online": online, "active_job_id": row["active_job_id"],
            "last_seen": row["last_seen"],
        }

    def list_workers(self) -> List[Dict[str, object]]:
        """List workers by most recent heartbeat. / 按最近心跳列出 Worker。"""
        with self._lock:
            ids = [row[0] for row in self._connection.execute(
                "SELECT worker_id FROM workers ORDER BY last_seen DESC"
            ).fetchall()]
        return [self.worker(worker_id) for worker_id in ids]

    def create_job(
        self, worker_id: str, kind: str, payload: Mapping[str, object]
    ) -> Dict[str, object]:
        """Enqueue a workflow or native-picker command. / 排队工作流或原生选择命令。"""
        if kind not in {"workflow", "picker"}:
            raise ValueError("Unsupported remote job kind.")
        worker = self.worker(worker_id)
        if not worker["online"]:
            raise RuntimeError("Selected worker is offline.")
        job_id = uuid.uuid4().hex
        now = time.time()
        with self._lock:
            if kind == "workflow":
                active = self._connection.execute(
                    "SELECT 1 FROM jobs WHERE worker_id=? AND kind='workflow' AND state IN ('queued','running','stopping') LIMIT 1",
                    (worker_id,),
                ).fetchone()
                if active:
                    raise RuntimeError("This worker already has an active workflow.")
            self._connection.execute(
                "INSERT INTO jobs(job_id,worker_id,kind,state,stage,progress,payload_json,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?)",
                (job_id, worker_id, kind, "queued", "queued", 0.0,
                 json.dumps(payload, ensure_ascii=False), now, now),
            )
            self._condition.notify_all()
        return self.job_status(job_id)

    def claim_next(self, worker_id: str) -> Optional[Dict[str, object]]:
        """Atomically claim the next command for one worker. / 原子领取一个 Worker 的下一条命令。"""
        now = time.time()
        with self._lock:
            self.heartbeat(worker_id)
            self._connection.execute("BEGIN IMMEDIATE")
            try:
                row = self._connection.execute(
                    "SELECT * FROM jobs WHERE worker_id=? AND state='queued' ORDER BY created_at LIMIT 1",
                    (worker_id,),
                ).fetchone()
                if row is None:
                    self._connection.execute("COMMIT")
                    return None
                self._connection.execute(
                    "UPDATE jobs SET state='running',stage='starting',claimed_at=?,updated_at=? WHERE job_id=? AND state='queued'",
                    (now, now, row["job_id"]),
                )
                self._connection.execute(
                    "UPDATE workers SET status='busy',active_job_id=?,last_seen=? WHERE worker_id=?",
                    (row["job_id"], now, worker_id),
                )
                self._connection.execute("COMMIT")
            except Exception:
                self._connection.execute("ROLLBACK")
                raise
        return {
            "job_id": row["job_id"], "kind": row["kind"],
            "payload": self._decode(row["payload_json"]),
        }

    def report(
        self,
        worker_id: str,
        job_id: str,
        payload: Mapping[str, object],
    ) -> Dict[str, object]:
        """Store worker progress and return cancellation intent.

        保存 Worker 进度并返回用户是否要求取消，日志用源 ID 去重以支持网络重试。
        """
        state = str(payload.get("state") or "running")
        if state not in {"running", "stopping", *TERMINAL_STATES}:
            raise ValueError("Invalid worker job state.")
        now = time.time()
        logs = payload.get("logs") if isinstance(payload.get("logs"), list) else []
        with self._lock:
            row = self._connection.execute(
                "SELECT worker_id FROM jobs WHERE job_id=?", (job_id,)
            ).fetchone()
            if row is None or row["worker_id"] != worker_id:
                raise KeyError("Job does not belong to this worker.")
            for item in logs:
                if not isinstance(item, dict):
                    continue
                self._connection.execute(
                    "INSERT OR IGNORE INTO events(job_id,source_id,timestamp,level,message) VALUES(?,?,?,?,?)",
                    (
                        job_id, int(item.get("id") or 0),
                        float(item.get("timestamp") or now),
                        str(item.get("level") or "info")[:16],
                        str(item.get("message") or "")[:12000],
                    ),
                )
            result = payload.get("result") if isinstance(payload.get("result"), dict) else {}
            return_code = payload.get("return_code")
            self._connection.execute(
                "UPDATE jobs SET state=?,stage=?,progress=?,result_json=?,return_code=?,updated_at=? WHERE job_id=?",
                (
                    state, str(payload.get("stage") or state)[:120],
                    max(0.0, min(100.0, float(payload.get("progress") or 0))),
                    json.dumps(result, ensure_ascii=False),
                    int(return_code) if return_code is not None else None,
                    now, job_id,
                ),
            )
            if state in TERMINAL_STATES:
                self._connection.execute(
                    "UPDATE workers SET status='online',active_job_id=NULL,last_seen=? WHERE worker_id=?",
                    (now, worker_id),
                )
            else:
                self._connection.execute(
                    "UPDATE workers SET status='busy',active_job_id=?,last_seen=? WHERE worker_id=?",
                    (job_id, now, worker_id),
                )
            cancel = bool(self._connection.execute(
                "SELECT cancel_requested FROM jobs WHERE job_id=?", (job_id,)
            ).fetchone()[0])
            self._condition.notify_all()
        return {"cancel_requested": cancel}

    def request_stop(self, job_id: str) -> Dict[str, object]:
        """Mark a remote workflow for cooperative cancellation. / 标记远程工作流等待协作取消。"""
        with self._lock:
            row = self._connection.execute(
                "SELECT state FROM jobs WHERE job_id=?", (job_id,)
            ).fetchone()
            if row is None or row["state"] not in {"queued", "running", "stopping"}:
                raise KeyError("Active job not found.")
            now = time.time()
            if row["state"] == "queued":
                self._connection.execute(
                    "UPDATE jobs SET cancel_requested=1,state='stopped',stage='stopped',return_code=-15,updated_at=? WHERE job_id=?",
                    (now, job_id),
                )
            else:
                self._connection.execute(
                    "UPDATE jobs SET cancel_requested=1,state='stopping',stage='stopping',updated_at=? WHERE job_id=?",
                    (now, job_id),
                )
            self._condition.notify_all()
        return self.job_status(job_id)

    def job_status(self, job_id: str, since: int = 0) -> Dict[str, object]:
        """Return one browser-compatible remote status. / 返回浏览器兼容的远程任务状态。"""
        with self._lock:
            row = self._connection.execute(
                "SELECT * FROM jobs WHERE job_id=?", (job_id,)
            ).fetchone()
            events = self._connection.execute(
                "SELECT event_id,timestamp,level,message FROM events WHERE job_id=? AND event_id>? ORDER BY event_id",
                (job_id, since),
            ).fetchall() if row is not None else []
        if row is None:
            raise KeyError("Job not found.")
        state = str(row["state"])
        event_payload = [dict(event) | {"id": int(event["event_id"])} for event in events]
        return {
            "job_id": row["job_id"], "worker_id": row["worker_id"],
            "kind": row["kind"], "state": state, "stage": row["stage"],
            "progress": row["progress"], "started_at": row["claimed_at"] or 0,
            "finished_at": row["updated_at"] if state in TERMINAL_STATES else 0,
            "elapsed_sec": round(max(0.0, (row["updated_at"] if state in TERMINAL_STATES else time.time()) - (row["claimed_at"] or row["created_at"])), 1),
            "return_code": row["return_code"], "last_log_id": event_payload[-1]["id"] if event_payload else since,
            "logs": event_payload, "running": state in {"queued", "running", "stopping"},
            "result": self._decode(row["result_json"]),
        }

    def latest_job(self, worker_id: str) -> Optional[str]:
        """Return the newest workflow job ID. / 返回最新工作流任务 ID。"""
        with self._lock:
            row = self._connection.execute(
                "SELECT job_id FROM jobs WHERE worker_id=? AND kind='workflow' ORDER BY created_at DESC LIMIT 1",
                (worker_id,),
            ).fetchone()
        return str(row[0]) if row else None

    def wait_terminal(self, job_id: str, timeout: float) -> Dict[str, object]:
        """Wait for a short interactive command such as a native picker.

        等待原生选择器等短交互任务完成；每个 HTTP 请求使用独立线程，不阻塞其他客户端。
        """
        deadline = time.time() + timeout
        with self._condition:
            while True:
                status = self.job_status(job_id)
                if status["state"] in TERMINAL_STATES:
                    return status
                remaining = deadline - time.time()
                if remaining <= 0:
                    raise TimeoutError("Local worker did not finish the picker in time.")
                self._condition.wait(timeout=min(1.0, remaining))

    def save_artifact(
        self,
        worker_id: str,
        job_id: str,
        name: str,
        mime_type: str,
        source: Any,
        length: int,
    ) -> Dict[str, object]:
        """Stream one authenticated preview into managed storage.

        将经过认证的低码率预览流式写入受管存储，不在内存中缓存整个视频。
        """
        with self._lock:
            row = self._connection.execute(
                "SELECT worker_id FROM jobs WHERE job_id=?", (job_id,)
            ).fetchone()
            if row is None or row["worker_id"] != worker_id:
                raise KeyError("Artifact job does not belong to this worker.")
        if length > self.max_storage_bytes:
            raise ValueError("Preview exceeds the total managed storage limit.")
        self.prune_artifacts(reserve_bytes=length)
        artifact_id = uuid.uuid4().hex
        safe = _safe_name(name)
        folder = self.storage_root / "artifacts" / job_id
        folder.mkdir(parents=True, exist_ok=True)
        destination = folder / f"{artifact_id}_{safe}"
        temporary = destination.with_suffix(destination.suffix + ".part")
        remaining = length
        try:
            with temporary.open("wb") as handle:
                while remaining:
                    chunk = source.read(min(1024 * 1024, remaining))
                    if not chunk:
                        raise ConnectionError("Preview upload ended early.")
                    handle.write(chunk)
                    remaining -= len(chunk)
            temporary.replace(destination)
        finally:
            if temporary.exists():
                temporary.unlink()
        with self._lock:
            self._connection.execute(
                "INSERT INTO artifacts(artifact_id,job_id,name,path,mime_type,size,created_at) VALUES(?,?,?,?,?,?,?)",
                (artifact_id, job_id, safe, str(destination), mime_type, length, time.time()),
            )
        return {"artifact_id": artifact_id, "name": safe, "size": length}

    def prune_artifacts(self, reserve_bytes: int = 0) -> int:
        """Delete expired/oldest previews until the configured disk bound is met.

        删除过期或最旧预览，确保控制面磁盘使用保持在配置上限内。
        """
        cutoff = time.time() - self.artifact_retention_seconds
        with self._lock:
            rows = self._connection.execute(
                "SELECT artifact_id,path,size,created_at FROM artifacts ORDER BY created_at DESC"
            ).fetchall()
            retained = max(0, reserve_bytes)
            delete_rows = []
            for row in rows:
                size = max(0, int(row["size"]))
                if float(row["created_at"]) < cutoff or retained + size > self.max_storage_bytes:
                    delete_rows.append(row)
                else:
                    retained += size
            for row in delete_rows:
                self._connection.execute(
                    "DELETE FROM artifacts WHERE artifact_id=?", (row["artifact_id"],)
                )
        for row in delete_rows:
            path = Path(row["path"]).resolve()
            try:
                path.relative_to(self.storage_root)
            except ValueError:
                LOGGER.error("Refusing to prune artifact outside managed storage: %s", path)
                continue
            path.unlink(missing_ok=True)
            try:
                path.parent.rmdir()
            except OSError:
                pass
        return len(delete_rows)

    def list_artifacts(self) -> List[Dict[str, object]]:
        """List recent remote previews. / 列出最近的远程预览。"""
        self.prune_artifacts()
        with self._lock:
            rows = self._connection.execute(
                "SELECT a.*,j.worker_id FROM artifacts a JOIN jobs j ON j.job_id=a.job_id ORDER BY a.created_at DESC LIMIT 30"
            ).fetchall()
        return [
            {
                "artifact_id": row["artifact_id"], "job_id": row["job_id"],
                "worker_id": row["worker_id"], "name": row["name"],
                "size": row["size"], "modified": row["created_at"],
                "url": "/api/output/file?id=" + urllib_parse.quote(row["artifact_id"]),
            }
            for row in rows
        ]

    def artifact(self, artifact_id: str) -> Tuple[Path, str]:
        """Resolve a managed artifact. / 解析受管预览文件。"""
        with self._lock:
            row = self._connection.execute(
                "SELECT path,mime_type FROM artifacts WHERE artifact_id=?", (artifact_id,)
            ).fetchone()
        if row is None:
            raise KeyError("Artifact not found.")
        path = Path(row["path"]).resolve()
        try:
            path.relative_to(self.storage_root)
        except ValueError as exc:
            raise PermissionError("Artifact escaped managed storage.") from exc
        if not path.is_file():
            raise FileNotFoundError(path)
        return path, str(row["mime_type"])


class ControlPlaneServer(ThreadingHTTPServer):
    """Threaded HTTP server carrying control-plane state. / 携带控制平面状态的多线程 HTTP 服务。"""

    daemon_threads = True
    allow_reuse_address = True

    def __init__(
        self,
        address: Tuple[str, int],
        handler: type[BaseHTTPRequestHandler],
        store: ControlPlaneStore,
        static_root: Path,
        admin_token: str,
        worker_token: str,
        max_artifact_bytes: int,
    ) -> None:
        super().__init__(address, handler)
        self.store = store
        self.static_root = static_root.resolve()
        self.admin_token = admin_token
        self.worker_token = worker_token
        self.max_artifact_bytes = max_artifact_bytes


class ControlPlaneHandler(BaseHTTPRequestHandler):
    """Serve the deployed browser API and worker protocol. / 提供部署网页 API 与 Worker 协议。"""

    server: ControlPlaneServer
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt: str, *args: object) -> None:
        LOGGER.info("%s - %s", self.address_string(), fmt % args)

    def _send(self, body: bytes, content_type: str, status: HTTPStatus = HTTPStatus.OK) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Content-Security-Policy", "default-src 'self'; style-src 'self'; script-src 'self'; media-src 'self' blob:; img-src 'self' data:; connect-src 'self'")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, payload: object, status: HTTPStatus = HTTPStatus.OK) -> None:
        self._send(_json_bytes(payload), "application/json; charset=utf-8", status)

    def _error(self, status: HTTPStatus, message: str) -> None:
        self._json({"ok": False, "error": message}, status)

    def _admin_ok(self) -> bool:
        supplied = self.headers.get("X-CyberEditor-Token", "")
        return secrets.compare_digest(supplied, self.server.admin_token)

    def _worker_ok(self) -> bool:
        supplied = self.headers.get("X-CyberEditor-Worker-Token", "")
        return secrets.compare_digest(supplied, self.server.worker_token)

    def _read_json(self) -> Dict[str, object]:
        content_type = self.headers.get("Content-Type", "").split(";", 1)[0].casefold()
        if content_type != "application/json":
            raise ValueError("Content-Type must be application/json.")
        length = int(self.headers.get("Content-Length", "0"))
        if length <= 0 or length > MAX_JSON_BYTES:
            raise ValueError("JSON body is empty or too large.")
        payload = json.loads(self.rfile.read(length).decode("utf-8"))
        if not isinstance(payload, dict):
            raise ValueError("JSON body must be an object.")
        return payload

    def do_GET(self) -> None:  # noqa: N802
        parsed = urllib_parse.urlparse(self.path)
        try:
            if parsed.path == "/api/worker/next":
                if not self._worker_ok():
                    return self._error(HTTPStatus.UNAUTHORIZED, "Invalid worker token.")
                worker_id = str((urllib_parse.parse_qs(parsed.query).get("worker_id") or [""])[0])
                job = self.server.store.claim_next(worker_id)
                return self._json({"ok": True, "job": job})
            if parsed.path.startswith("/api/") and not self._admin_ok():
                return self._error(HTTPStatus.UNAUTHORIZED, "Invalid or missing admin token.")
            query = urllib_parse.parse_qs(parsed.query)
            if parsed.path == "/api/capabilities":
                self._json({"ok": True, "mode": "remote", "picker": "worker", "preview_relay": True})
            elif parsed.path == "/api/config":
                self._json({"ok": True, "config": DEFAULT_REMOTE_CONFIG})
            elif parsed.path == "/api/workers":
                self._json({"ok": True, "workers": self.server.store.list_workers()})
            elif parsed.path == "/api/environment":
                worker = self.server.store.worker(str((query.get("worker_id") or [""])[0]))
                self._json({"ok": True, "environment": worker["environment"]})
            elif parsed.path == "/api/status":
                worker_id = str((query.get("worker_id") or [""])[0])
                job_id = str((query.get("job_id") or [""])[0]) or self.server.store.latest_job(worker_id)
                since = int((query.get("since") or ["0"])[0])
                if not job_id:
                    self._json({"ok": True, "state": "idle", "stage": "ready", "progress": 0, "running": False, "logs": [], "last_log_id": since})
                else:
                    self._json({"ok": True, **self.server.store.job_status(job_id, since)})
            elif parsed.path == "/api/outputs":
                self._json({"ok": True, "outputs": self.server.store.list_artifacts()})
            elif parsed.path == "/api/output/file":
                path, mime = self.server.store.artifact(str((query.get("id") or [""])[0]))
                self._send_file(path, mime)
            else:
                self._serve_static(parsed.path)
        except (KeyError, FileNotFoundError):
            self._error(HTTPStatus.NOT_FOUND, "Not found.")
        except (ValueError, PermissionError) as exc:
            self._error(HTTPStatus.BAD_REQUEST, str(exc))
        except Exception as exc:
            LOGGER.exception("GET %s failed", parsed.path)
            self._error(HTTPStatus.INTERNAL_SERVER_ERROR, str(exc))

    def do_POST(self) -> None:  # noqa: N802
        parsed = urllib_parse.urlparse(self.path)
        try:
            if parsed.path.startswith("/api/worker/"):
                if not self._worker_ok():
                    return self._error(HTTPStatus.UNAUTHORIZED, "Invalid worker token.")
                return self._worker_post(parsed.path)
            if not self._admin_ok():
                return self._error(HTTPStatus.UNAUTHORIZED, "Invalid or missing admin token.")
            payload = self._read_json()
            if parsed.path == "/api/workflow/start":
                worker_id = str(payload.pop("worker_id", ""))
                status = self.server.store.create_job(worker_id, "workflow", payload)
                self._json({"ok": True, **status}, HTTPStatus.ACCEPTED)
            elif parsed.path == "/api/workflow/stop":
                status = self.server.store.request_stop(str(payload.get("job_id") or ""))
                self._json({"ok": True, **status})
            elif parsed.path == "/api/picker":
                worker_id = str(payload.get("worker_id") or "")
                job = self.server.store.create_job(worker_id, "picker", {"kind": str(payload.get("kind") or "")})
                result = self.server.store.wait_terminal(str(job["job_id"]), timeout=600)
                if result["state"] != "succeeded":
                    raise RuntimeError(str(result.get("result", {}).get("error") or "Local picker failed."))
                self._json({"ok": True, "paths": result.get("result", {}).get("paths", [])})
            else:
                self._error(HTTPStatus.NOT_FOUND, "Unknown API endpoint.")
        except KeyError as exc:
            self._error(HTTPStatus.NOT_FOUND, str(exc))
        except TimeoutError as exc:
            self._error(HTTPStatus.GATEWAY_TIMEOUT, str(exc))
        except (ValueError, RuntimeError, OSError, json.JSONDecodeError) as exc:
            self._error(HTTPStatus.CONFLICT if isinstance(exc, RuntimeError) else HTTPStatus.BAD_REQUEST, str(exc))
        except Exception as exc:
            LOGGER.exception("POST %s failed", parsed.path)
            self._error(HTTPStatus.INTERNAL_SERVER_ERROR, str(exc))

    def _worker_post(self, path: str) -> None:
        if path == "/api/worker/artifact":
            length = int(self.headers.get("Content-Length", "0"))
            if length <= 0 or length > self.server.max_artifact_bytes:
                raise ValueError("Preview is empty or exceeds the deployment limit.")
            artifact = self.server.store.save_artifact(
                self.headers.get("X-CyberEditor-Worker-Id", ""),
                self.headers.get("X-CyberEditor-Job-Id", ""),
                self.headers.get("X-CyberEditor-File-Name", "preview.mp4"),
                self.headers.get("Content-Type", "application/octet-stream"),
                self.rfile,
                length,
            )
            return self._json({"ok": True, "artifact": artifact}, HTTPStatus.CREATED)
        payload = self._read_json()
        if path == "/api/worker/register":
            worker = self.server.store.register_worker(
                str(payload.get("worker_id") or ""),
                str(payload.get("name") or payload.get("worker_id") or "Windows worker"),
                payload.get("environment") if isinstance(payload.get("environment"), dict) else {},
            )
            self._json({"ok": True, "worker": worker})
        elif path == "/api/worker/report":
            result = self.server.store.report(
                str(payload.get("worker_id") or ""),
                str(payload.get("job_id") or ""),
                payload,
            )
            self._json({"ok": True, **result})
        else:
            self._error(HTTPStatus.NOT_FOUND, "Unknown worker endpoint.")

    def _serve_static(self, request_path: str) -> None:
        mapping = {"/": "index.html", "/index.html": "index.html", "/app.js": "app.js", "/styles.css": "styles.css"}
        name = mapping.get(request_path)
        if not name:
            raise FileNotFoundError(request_path)
        path = self.server.static_root / name
        self._send(path.read_bytes(), mimetypes.guess_type(name)[0] or "application/octet-stream")

    def _send_file(self, path: Path, mime_type: str) -> None:
        size = path.stat().st_size
        start, end, status = 0, size - 1, HTTPStatus.OK
        header = self.headers.get("Range", "")
        if header:
            match = re.fullmatch(r"bytes=(\d*)-(\d*)", header.strip())
            if not match:
                return self._error(HTTPStatus.REQUESTED_RANGE_NOT_SATISFIABLE, "Invalid byte range.")
            if match.group(1):
                start = int(match.group(1))
                if match.group(2):
                    end = min(end, int(match.group(2)))
            elif match.group(2):
                start = max(0, size - int(match.group(2)))
            if start > end or start >= size:
                return self._error(HTTPStatus.REQUESTED_RANGE_NOT_SATISFIABLE, "Range outside file.")
            status = HTTPStatus.PARTIAL_CONTENT
        length = end - start + 1
        self.send_response(status)
        self.send_header("Content-Type", mime_type)
        self.send_header("Content-Length", str(length))
        self.send_header("Accept-Ranges", "bytes")
        if status == HTTPStatus.PARTIAL_CONTENT:
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        self.end_headers()
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
    """Build deployment CLI options. / 构建部署命令参数。"""
    parser = argparse.ArgumentParser(description="CyberEditor deployable control plane")
    parser.add_argument("--host", default=os.environ.get("CYBEREDITOR_HOST", "0.0.0.0"))
    parser.add_argument("--port", type=int, default=int(os.environ.get("PORT", "8765")))
    parser.add_argument("--token", default=os.environ.get("CYBEREDITOR_ADMIN_TOKEN", ""))
    parser.add_argument("--worker-token", default=os.environ.get("CYBEREDITOR_WORKER_TOKEN", ""))
    parser.add_argument("--storage-dir", default=os.environ.get("CYBEREDITOR_STORAGE", "data/control-plane"))
    parser.add_argument("--max-preview-mb", type=int, default=int(os.environ.get("CYBEREDITOR_MAX_PREVIEW_MB", "256")))
    parser.add_argument("--log-level", default="INFO", choices=("DEBUG", "INFO", "WARNING", "ERROR"))
    return parser


def run_control_plane(argv: Optional[Sequence[str]] = None) -> int:
    """Run the deployable HTTPS-proxy-ready control plane.

    运行可置于 HTTPS 反向代理后的控制平面；管理令牌和 Worker 令牌必须彼此独立。
    """
    args = build_parser().parse_args(argv)
    if len(args.token) < 16:
        raise SystemExit("CYBEREDITOR_ADMIN_TOKEN/--token must contain at least 16 characters.")
    if len(args.worker_token) < 24 or secrets.compare_digest(args.token, args.worker_token):
        raise SystemExit("Worker token must contain at least 24 characters and differ from the admin token.")
    if not 1 <= args.max_preview_mb <= 2048:
        raise SystemExit("--max-preview-mb must be between 1 and 2048.")
    logging.basicConfig(level=getattr(logging, args.log_level), format="%(asctime)s | %(levelname)s | %(name)s | %(message)s")
    project_root = Path(__file__).resolve().parents[1]
    storage = Path(args.storage_dir).expanduser()
    if not storage.is_absolute():
        storage = project_root / storage
    store = ControlPlaneStore(storage / "control-plane.sqlite3", storage)
    server = ControlPlaneServer(
        (args.host, args.port), ControlPlaneHandler, store, project_root / "web",
        args.token, args.worker_token, args.max_preview_mb * 1024 * 1024,
    )
    LOGGER.info("CyberEditor control plane listening on %s:%d", args.host, args.port)
    try:
        server.serve_forever(poll_interval=0.4)
    except KeyboardInterrupt:
        LOGGER.info("Control plane stopped")
    finally:
        server.server_close()
        store.close()
    return 0


def main() -> int:
    """CLI entry point. / 命令行入口。"""
    return run_control_plane()


if __name__ == "__main__":
    raise SystemExit(main())
