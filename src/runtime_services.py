#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Windows runtime discovery and just-in-time service startup.
Windows 运行时发现与按需服务启动。

This module deliberately uses only Python's standard library.  It is shared by
the lightweight desktop UI and the Resolve executor, so locating software never
imports a machine-learning or media-processing package.

本模块刻意只使用 Python 标准库。轻量桌面界面和 Resolve 执行器共同使用它，因此
软件定位过程不会导入机器学习或音视频处理依赖。
"""

from __future__ import annotations

import ctypes
import json
import os
from pathlib import Path
import shutil
import string
import subprocess
import time
from typing import Dict, List, Optional, Sequence, Tuple
from urllib import error as urllib_error
from urllib import parse as urllib_parse
from urllib import request as urllib_request

try:
    import winreg
except ImportError:  # pragma: no cover - Windows-only registry support.
    winreg = None  # type: ignore[assignment]


class RuntimeServiceError(RuntimeError):
    """Expected local-runtime failure. / 可预期的本地运行时错误。"""


def _unique_paths(paths: Sequence[Path]) -> List[Path]:
    """Return paths once while preserving order. / 按原顺序返回去重后的路径。"""
    result: List[Path] = []
    seen = set()
    for path in paths:
        text = str(path).strip()
        if not text:
            continue
        key = os.path.normcase(os.path.abspath(text))
        if key in seen:
            continue
        seen.add(key)
        result.append(Path(text))
    return result


def _windows_user_homes() -> Sequence[Path]:
    """
    Return plausible homes for the current interactive Windows user.
    返回当前 Windows 交互用户可能使用的主目录。

    Environment variables are preferred.  ``Path.home()`` is retained as a
    fallback for embedded Python launchers that omit ``USERPROFILE``.
    优先使用环境变量；对于未传递 ``USERPROFILE`` 的嵌入式 Python 启动器，
    保留 ``Path.home()`` 作为后备。
    """
    homes: List[Path] = []
    for value in (
        os.environ.get("USERPROFILE"),
        (
            os.environ.get("HOMEDRIVE", "")
            + os.environ.get("HOMEPATH", "")
        ),
        str(Path.home()),
    ):
        if value:
            homes.append(Path(value).expanduser())
    return _unique_paths(homes)


def find_ollama_executables() -> Tuple[Optional[Path], Optional[Path]]:
    """
    Locate the Ollama CLI and Windows tray application.
    定位 Ollama 命令行程序与 Windows 托盘应用。

    Returns / 返回:
        ``(cli_path, app_path)``; either value can be ``None``.
        ``(命令行路径, 应用路径)``；任意一项都可能为 ``None``。
    """
    cli_candidates: List[Path] = []
    app_candidates: List[Path] = []
    cli = shutil.which("ollama")
    if cli:
        cli_candidates.append(Path(cli))
    local_app_data = os.environ.get("LOCALAPPDATA")
    if local_app_data:
        root = Path(local_app_data) / "Programs" / "Ollama"
        cli_candidates.append(root / "ollama.exe")
        app_candidates.append(root / "ollama app.exe")
    for home in _windows_user_homes():
        root = home / "AppData" / "Local" / "Programs" / "Ollama"
        cli_candidates.append(root / "ollama.exe")
        app_candidates.append(root / "ollama app.exe")

    found_cli = next(
        (path.resolve() for path in _unique_paths(cli_candidates) if path.is_file()),
        None,
    )
    found_app = next(
        (path.resolve() for path in _unique_paths(app_candidates) if path.is_file()),
        None,
    )
    return found_cli, found_app


def _fixed_drive_roots() -> Sequence[Path]:
    """
    List mounted Windows drive roots without invoking PowerShell.
    不调用 PowerShell，列出已挂载的 Windows 盘符根目录。
    """
    if os.name != "nt":
        return ()
    roots: List[Path] = []
    try:
        mask = int(ctypes.windll.kernel32.GetLogicalDrives())
    except (AttributeError, OSError, ValueError):
        mask = 0
    for index, letter in enumerate(string.ascii_uppercase):
        if mask and not (mask & (1 << index)):
            continue
        root = Path(f"{letter}:\\")
        try:
            if root.is_dir():
                roots.append(root)
        except OSError:
            continue
    return roots


def _resolve_registry_candidates() -> Sequence[Path]:
    """
    Read Resolve install hints from Windows uninstall entries.
    从 Windows 卸载注册表项读取 Resolve 安装线索。
    """
    if winreg is None or os.name != "nt":
        return ()
    candidates: List[Path] = []
    hives = (winreg.HKEY_LOCAL_MACHINE, winreg.HKEY_CURRENT_USER)
    subkeys = (
        r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall",
        r"SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall",
    )
    access_flags = [winreg.KEY_READ]
    for flag_name in ("KEY_WOW64_64KEY", "KEY_WOW64_32KEY"):
        flag = getattr(winreg, flag_name, 0)
        if flag:
            access_flags.append(winreg.KEY_READ | flag)

    for hive in hives:
        for subkey in subkeys:
            for access in access_flags:
                try:
                    root = winreg.OpenKey(hive, subkey, 0, access)
                except OSError:
                    continue
                try:
                    count = winreg.QueryInfoKey(root)[0]
                    for index in range(count):
                        try:
                            name = winreg.EnumKey(root, index)
                            entry = winreg.OpenKey(root, name)
                            display = str(
                                winreg.QueryValueEx(entry, "DisplayName")[0]
                            )
                        except OSError:
                            continue
                        try:
                            if display.strip().casefold() not in {
                                "davinci resolve",
                                "davinci resolve studio",
                            }:
                                continue
                            for value_name in (
                                "InstallLocation",
                                "DisplayIcon",
                            ):
                                try:
                                    value = str(
                                        winreg.QueryValueEx(
                                            entry, value_name
                                        )[0]
                                    ).strip().strip('"')
                                except OSError:
                                    continue
                                if not value:
                                    continue
                                value = value.split(",", 1)[0]
                                path = Path(value)
                                candidates.append(
                                    path
                                    if path.suffix.casefold() == ".exe"
                                    else path / "Resolve.exe"
                                )
                        finally:
                            winreg.CloseKey(entry)
                finally:
                    winreg.CloseKey(root)
    return _unique_paths(candidates)


def find_resolve_executable() -> Optional[Path]:
    """
    Locate ``Resolve.exe`` across custom Windows installation drives.
    在 Windows 自定义安装盘中定位 ``Resolve.exe``。

    Registry hints are checked first, followed by every mounted drive's common
    Program Files locations.  The latter is important because the MSI entry can
    omit ``InstallLocation`` when Resolve is installed on another drive.

    先检查注册表线索，再检查所有盘符的常见 Program Files 目录。Resolve 安装在
    其他盘时 MSI 记录可能缺少 ``InstallLocation``，因此跨盘检查不可省略。
    """
    direct = shutil.which("Resolve.exe")
    candidates: List[Path] = [Path(direct)] if direct else []
    candidates.extend(_resolve_registry_candidates())

    program_roots = [
        Path(value)
        for value in (
            os.environ.get("PROGRAMFILES"),
            os.environ.get("PROGRAMFILES(X86)"),
        )
        if value
    ]
    for drive in _fixed_drive_roots():
        program_roots.extend((drive / "Program Files", drive / "Program Files (x86)"))
    for root in _unique_paths(program_roots):
        candidates.extend(
            (
                root / "Blackmagic Design" / "DaVinci Resolve" / "Resolve.exe",
                root
                / "Blackmagic Design"
                / "DaVinci Resolve Studio"
                / "Resolve.exe",
            )
        )
    return next(
        (path.resolve() for path in _unique_paths(candidates) if path.is_file()),
        None,
    )


def fetch_ollama_models(
    base_url: str, timeout: float = 3.0
) -> List[Dict[str, object]]:
    """
    Fetch installed Ollama models from the local HTTP API.
    通过本地 HTTP API 获取已安装的 Ollama 模型。

    Parameters / 参数:
        base_url:
            Ollama server root, normally ``http://127.0.0.1:11434``.
            Ollama 服务根地址，通常为 ``http://127.0.0.1:11434``。
        timeout:
            HTTP timeout in seconds. / HTTP 超时秒数。
    """
    request = urllib_request.Request(
        base_url.rstrip("/") + "/api/tags",
        headers={"Accept": "application/json"},
    )
    with urllib_request.urlopen(request, timeout=timeout) as response:
        payload = json.loads(response.read().decode("utf-8"))
    models: List[Dict[str, object]] = []
    for item in payload.get("models", []):
        if not isinstance(item, dict):
            continue
        name = str(item.get("name", "")).strip()
        if not name:
            continue
        models.append({"name": name, "size": int(item.get("size") or 0)})
    return models


def _local_ollama_address(base_url: str) -> Tuple[str, str]:
    """
    Validate a local Ollama URL and return ``(hostname, host:port)``.
    校验本地 Ollama URL，并返回 ``(主机名, 主机:端口)``。
    """
    parsed = urllib_parse.urlparse(base_url)
    hostname = (parsed.hostname or "").casefold()
    if parsed.scheme not in {"http", "https"} or hostname not in {
        "localhost",
        "127.0.0.1",
        "::1",
    }:
        raise RuntimeServiceError(
            "仅会自动启动本机 Ollama；远程地址请自行管理服务。"
            " / Auto-start is limited to local Ollama URLs."
        )
    port = parsed.port or (443 if parsed.scheme == "https" else 11434)
    host_for_env = f"[{hostname}]" if ":" in hostname else hostname
    return hostname, f"{host_for_env}:{port}"


def _launch_detached(
    command: Sequence[str], environment: Optional[Dict[str, str]] = None
) -> subprocess.Popen:
    """
    Launch a background Windows process without a console window.
    在后台启动 Windows 进程且不显示控制台窗口。
    """
    flags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    flags |= getattr(subprocess, "DETACHED_PROCESS", 0)
    return subprocess.Popen(
        list(command),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        env=environment,
        close_fds=True,
        creationflags=flags,
    )


def ensure_ollama_service(
    base_url: str,
    timeout: float = 30.0,
) -> Tuple[List[Dict[str, object]], bool]:
    """
    Ensure the local Ollama API is ready, starting it when necessary.
    确保本地 Ollama API 可用；必要时自动启动。

    The Windows tray app is preferred because it follows Ollama's supported
    desktop lifecycle.  If it does not expose the API promptly, ``ollama
    serve`` is used as a headless fallback.  No model is loaded here, so this
    health check consumes no model VRAM.

    优先启动 Ollama 官方 Windows 托盘应用；若其未及时提供 API，则以
    ``ollama serve`` 作为无窗口后备。本步骤不会加载模型，因此不占用模型显存。

    Returns / 返回:
        ``(installed_models, started_by_this_call)``.
        ``(已安装模型, 本次是否执行了自动启动)``。
    """
    try:
        return fetch_ollama_models(base_url), False
    except (OSError, ValueError, urllib_error.URLError, json.JSONDecodeError):
        pass

    _, host = _local_ollama_address(base_url)
    cli_path, app_path = find_ollama_executables()
    if cli_path is None and app_path is None:
        raise RuntimeServiceError(
            "未找到 Ollama 可执行文件。请安装 Ollama for Windows。"
            " / Ollama is not installed or could not be located."
        )

    started = False
    launched: List[subprocess.Popen] = []
    deadline = time.monotonic() + max(5.0, float(timeout))
    # GPU discovery can take roughly 15–20 seconds on older Quadro systems.
    # Give the supported tray application enough time before falling back to a
    # second ``ollama serve`` process.
    # 较老 Quadro 的 GPU 发现可能耗时 15–20 秒；在启动第二个 serve 后备进程前，
    # 给官方托盘应用足够的初始化时间。
    app_deadline = min(deadline, time.monotonic() + 25.0)
    if app_path is not None:
        try:
            launched.append(_launch_detached([str(app_path)]))
            started = True
        except OSError:
            pass
        while time.monotonic() < app_deadline:
            try:
                return fetch_ollama_models(base_url), started
            except (OSError, ValueError, urllib_error.URLError, json.JSONDecodeError):
                time.sleep(0.5)

    if cli_path is not None:
        environment = os.environ.copy()
        environment["OLLAMA_HOST"] = host
        try:
            launched.append(
                _launch_detached([str(cli_path), "serve"], environment)
            )
            started = True
        except OSError as exc:
            raise RuntimeServiceError(
                f"启动 Ollama 失败 / Failed to launch Ollama: {exc}"
            ) from exc

    last_error = ""
    while time.monotonic() < deadline:
        try:
            return fetch_ollama_models(base_url), started
        except (OSError, ValueError, urllib_error.URLError, json.JSONDecodeError) as exc:
            last_error = str(exc)
            if launched and all(process.poll() is not None for process in launched):
                break
            time.sleep(0.5)
    raise RuntimeServiceError(
        "Ollama 已安装但 API 未能自动启动。请查看 "
        "%LOCALAPPDATA%\\Ollama\\server.log。\n"
        f"Ollama was found but its API did not become ready: {last_error}"
    )
