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

import json
import os
from pathlib import Path
import shutil
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

    def safe_file(path: Path) -> bool:
        """Ignore inaccessible per-user installs during discovery. / 检测时忽略无权访问的用户安装。"""
        try:
            return path.is_file()
        except OSError:
            return False

    found_cli = next(
        (path.resolve() for path in _unique_paths(cli_candidates) if safe_file(path)),
        None,
    )
    found_app = next(
        (path.resolve() for path in _unique_paths(app_candidates) if safe_file(path)),
        None,
    )
    return found_cli, found_app


def _resolve_registry_value(hive: object, subkey: str, value_name: str) -> object:
    """Read one Resolve registry value without raising. / 安全读取一个 Resolve 注册表值。"""
    if winreg is None:
        return None
    access = winreg.KEY_READ | getattr(winreg, "KEY_WOW64_64KEY", 0)
    try:
        key = winreg.OpenKey(hive, subkey, 0, access)
    except OSError:
        return None
    try:
        return winreg.QueryValueEx(key, value_name)[0]
    except OSError:
        return None
    finally:
        winreg.CloseKey(key)


def get_resolve_registration() -> Dict[str, object]:
    """
    Read Blackmagic's official Resolve installation registration.
    读取 Blackmagic 官方 Resolve 安装注册信息。

    Resolve Studio activation is intentionally not inferred here: Free and
    Studio use the same executable and MSI product name. The authoritative
    edition is ``resolve.GetProductName()`` after a successful API connection.
    此处不会猜测 Studio 授权：免费版与 Studio 共用可执行文件和 MSI 产品名；成功
    连接 API 后的 ``resolve.GetProductName()`` 才是权威版本信息。
    """
    if winreg is None or os.name != "nt":
        return {"installed": False, "version": "", "user_registered": False}
    key = r"SOFTWARE\Blackmagic Design\DaVinci Resolve"
    version = str(
        _resolve_registry_value(winreg.HKEY_LOCAL_MACHINE, key, "Version") or ""
    ).strip()
    installed_value = _resolve_registry_value(
        winreg.HKEY_CURRENT_USER, key, "installed"
    )
    try:
        user_registered = int(installed_value or 0) == 1
    except (TypeError, ValueError):
        user_registered = False
    return {
        "installed": bool(version or user_registered),
        "version": version,
        "user_registered": user_registered,
    }


def _resolve_registry_installations() -> Sequence[Tuple[Path, str]]:
    """
    Read Resolve paths together with registered product names.
    读取 Resolve 路径及其注册产品名。

    Both Free and Studio can install scripting modules, so module presence is
    insufficient to determine whether external automation is available.
    免费版与 Studio 都可能安装脚本模块，因此不能仅凭模块存在判断外部自动化能力。
    """
    if winreg is None or os.name != "nt":
        return ()
    installations: List[Tuple[Path, str]] = []
    seen = set()
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
                            ).strip()
                        except OSError:
                            continue
                        try:
                            if display.casefold() not in {
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
                                executable = (
                                    path
                                    if path.suffix.casefold() == ".exe"
                                    else path / "Resolve.exe"
                                )
                                key = (
                                    os.path.normcase(os.path.abspath(str(executable))),
                                    display.casefold(),
                                )
                                if key not in seen:
                                    seen.add(key)
                                    installations.append((executable, display))
                        finally:
                            winreg.CloseKey(entry)
                finally:
                    winreg.CloseKey(root)
    return installations


def _resolve_registry_candidates() -> Sequence[Path]:
    """Read Resolve executable hints from uninstall entries. / 读取 Resolve 路径线索。"""
    return _unique_paths(
        [path for path, _display in _resolve_registry_installations()]
    )


def _resolve_start_app_candidates() -> Sequence[Path]:
    """
    Ask Windows for registered Start-app targets; never scan drives.
    从 Windows 注册应用表读取启动目标，不扫描磁盘。
    """
    if os.name != "nt":
        return ()
    powershell = shutil.which("powershell.exe") or shutil.which("powershell")
    if not powershell:
        return ()
    script = (
        "$items = Get-StartApps | Where-Object { $_.Name -eq 'DaVinci Resolve' }; "
        "@($items | ForEach-Object { $_.AppID }) | ConvertTo-Json -Compress"
    )
    try:
        completed = subprocess.run(
            [powershell, "-NoProfile", "-NonInteractive", "-Command", script],
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=10,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        if completed.returncode != 0 or not completed.stdout.strip():
            return ()
        payload = json.loads(completed.stdout)
    except (OSError, subprocess.SubprocessError, ValueError):
        return ()
    values = payload if isinstance(payload, list) else [payload]
    return _unique_paths(
        [
            Path(str(value).strip())
            for value in values
            if str(value).strip().casefold().endswith("resolve.exe")
        ]
    )


def find_resolve_executable() -> Optional[Path]:
    """
    Locate ``Resolve.exe`` from Windows/Blackmagic registration, never drive scans.
    通过 Windows/Blackmagic 注册信息定位 ``Resolve.exe``，绝不扫描磁盘猜测。

    Blackmagic's MSI may omit ``InstallLocation``. Windows Start Apps still
    records the exact custom-drive target and is the preferred path source after
    confirming Blackmagic's installation keys.
    Blackmagic MSI 可能不写 ``InstallLocation``；Windows 注册应用表仍保存自定义盘
    的精确启动目标，因此在确认 Blackmagic 注册表后优先使用该目标。
    """
    registration = get_resolve_registration()
    direct = shutil.which("Resolve.exe")
    candidates: List[Path] = [Path(direct)] if direct else []
    library = str(os.environ.get("RESOLVE_SCRIPT_LIB") or "").strip()
    if library:
        candidates.append(Path(library).expanduser().parent / "Resolve.exe")
    candidates.extend(_resolve_registry_candidates())
    if bool(registration.get("installed")):
        candidates.extend(_resolve_start_app_candidates())
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
