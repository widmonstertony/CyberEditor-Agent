#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Guarded PyAutoGUI fallback for Resolve features absent from the public API.
用于 Resolve 公共 API 未覆盖功能的受保护 PyAutoGUI 后备通道。

Native scripting is always preferred. This module runs only an explicitly
named action from a local profile, verifies screen geometry and the foreground
Resolve window, enables PyAutoGUI's corner fail-safe, and never executes shell
commands or model-produced Python.

始终优先使用原生脚本接口。本模块只运行本地配置中显式命名的动作；执行前校验
屏幕尺寸与前台 Resolve 窗口，开启 PyAutoGUI 角落急停，并且绝不执行 shell 命令
或模型生成的 Python。
"""

from __future__ import annotations

import argparse
import json
import logging
from pathlib import Path
import sys
import time
from typing import Any, Dict, Optional, Sequence


LOGGER_NAME = "cybereditor.resolve_macro"


class ResolveMacroError(RuntimeError):
    """Expected safe-macro failure. / 可预期的安全宏错误。"""


class SafeResolveMacroRunner:
    """
    Validate and execute one allow-listed Resolve UI macro action.
    校验并执行一个白名单 Resolve UI 宏动作。

    Parameters / 参数:
        profile_path:
            JSON profile created for one display/layout configuration.
            针对某一显示器与界面布局制作的 JSON 配置。
        logger:
            Optional application logger. / 可选应用日志器。
    """

    def __init__(
        self,
        profile_path: Path,
        logger: Optional[logging.Logger] = None,
    ) -> None:
        """Load profile metadata without importing PyAutoGUI. / 加载配置元数据但暂不导入 PyAutoGUI。"""
        self.profile_path = Path(profile_path).expanduser().resolve()
        self.logger = logger or logging.getLogger(LOGGER_NAME)
        self.profile = self._load_profile()

    def _load_profile(self) -> Dict[str, Any]:
        """Read and validate the bounded macro profile. / 读取并校验有界宏配置。"""
        try:
            payload = json.loads(
                self.profile_path.read_text(encoding="utf-8-sig")
            )
        except (OSError, ValueError) as exc:
            raise ResolveMacroError(
                f"无法读取宏配置 / Cannot read macro profile: {exc}"
            ) from exc
        if not isinstance(payload, dict) or payload.get("schema_version") != "1.0":
            raise ResolveMacroError(
                "宏配置 schema_version 必须为 1.0 / Macro schema must be 1.0."
            )
        resolution = payload.get("expected_resolution")
        if (
            not isinstance(resolution, list)
            or len(resolution) != 2
            or any(not isinstance(value, int) or value <= 0 for value in resolution)
        ):
            raise ResolveMacroError(
                "expected_resolution 必须是 [宽, 高] / must be [width, height]."
            )
        actions = payload.get("actions")
        if not isinstance(actions, dict):
            raise ResolveMacroError("actions 必须是对象 / actions must be an object.")
        return payload

    def run(self, action_name: str) -> None:
        """
        Focus Resolve and execute one validated profile action.
        聚焦 Resolve 并执行一个已校验配置动作。

        Parameters / 参数:
            action_name:
                Exact key below profile ``actions``. / 配置 ``actions`` 下的精确键名。
        """
        actions = self.profile["actions"].get(action_name)
        if not isinstance(actions, list) or not actions:
            raise ResolveMacroError(
                f"宏动作不存在或为空 / Macro action is missing or empty: {action_name!r}"
            )
        if len(actions) > 200:
            raise ResolveMacroError("单个宏最多 200 步 / A macro is limited to 200 steps.")
        try:
            import pyautogui
        except ImportError as exc:
            raise ResolveMacroError(
                "缺少 PyAutoGUI；请重新安装 requirements.txt / PyAutoGUI is missing."
            ) from exc
        pyautogui.FAILSAFE = True
        pyautogui.PAUSE = 0.12
        expected = tuple(self.profile["expected_resolution"])
        actual = tuple(int(value) for value in pyautogui.size())
        if actual != expected:
            raise ResolveMacroError(
                f"屏幕分辨率不匹配 / Screen mismatch: expected {expected}, got {actual}."
            )
        windows = [
            window
            for window in pyautogui.getAllWindows()
            if "davinci resolve" in str(getattr(window, "title", "")).casefold()
        ]
        if not windows:
            raise ResolveMacroError(
                "找不到 DaVinci Resolve 窗口 / Resolve window was not found."
            )
        try:
            windows[0].activate()
        except Exception as exc:
            raise ResolveMacroError(
                f"无法激活 Resolve 窗口 / Cannot activate Resolve: {exc}"
            ) from exc
        time.sleep(0.8)
        active = pyautogui.getActiveWindow()
        title = str(getattr(active, "title", ""))
        if "davinci resolve" not in title.casefold():
            raise ResolveMacroError(
                f"前台窗口不是 Resolve，已拒绝输入 / Foreground is not Resolve: {title!r}"
            )
        self.logger.warning(
            "Executing guarded UI fallback %s; move the pointer to a screen corner to abort.",
            action_name,
        )
        for index, step in enumerate(actions):
            self._run_step(pyautogui, step, index)

    @staticmethod
    def _run_step(pyautogui: Any, step: object, index: int) -> None:
        """Execute one constrained macro step. / 执行一个受约束宏步骤。"""
        if not isinstance(step, dict):
            raise ResolveMacroError(f"actions[{index}] 必须是对象 / must be an object.")
        kind = str(step.get("type") or "").casefold()
        if kind == "wait":
            seconds = float(step.get("seconds", 0.5))
            if not 0 <= seconds <= 10:
                raise ResolveMacroError("wait 必须在 0–10 秒 / wait must be 0–10 seconds.")
            time.sleep(seconds)
            return
        if kind == "hotkey":
            keys = step.get("keys")
            if not isinstance(keys, list) or not 1 <= len(keys) <= 5:
                raise ResolveMacroError("hotkey.keys 必须包含 1–5 个键 / must contain 1–5 keys.")
            pyautogui.hotkey(*(str(key) for key in keys))
            return
        if kind == "press":
            key = str(step.get("key") or "")
            presses = int(step.get("presses", 1))
            if not key or not 1 <= presses <= 20:
                raise ResolveMacroError("press 参数无效 / Invalid press step.")
            pyautogui.press(key, presses=presses, interval=0.05)
            return
        if kind == "click_ratio":
            x = float(step.get("x", -1))
            y = float(step.get("y", -1))
            if not 0 <= x <= 1 or not 0 <= y <= 1:
                raise ResolveMacroError("click_ratio x/y 必须在 0–1 / must be in 0–1.")
            width, height = pyautogui.size()
            pyautogui.click(int(width * x), int(height * y))
            return
        raise ResolveMacroError(
            f"不支持的宏步骤 / Unsupported macro step at {index}: {kind!r}"
        )


def configure_logging(level: str = "INFO") -> logging.Logger:
    """Configure standalone macro logging. / 配置独立宏日志。"""
    logging.basicConfig(
        level=getattr(logging, level.upper()),
        format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    )
    return logging.getLogger(LOGGER_NAME)


def main(argv: Optional[Sequence[str]] = None) -> int:
    """Run one explicit guarded macro action. / 运行一个显式指定的受保护宏动作。"""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profile", required=True)
    parser.add_argument("--action", required=True)
    parser.add_argument("--log-level", default="INFO")
    args = parser.parse_args(argv)
    logger = configure_logging(args.log_level)
    try:
        SafeResolveMacroRunner(Path(args.profile), logger).run(args.action)
        return 0
    except ResolveMacroError as exc:
        logger.error("%s", exc)
        return 2
    except KeyboardInterrupt:
        logger.warning("Macro aborted by user / 宏已由用户中止")
        return 130


if __name__ == "__main__":
    sys.exit(main())
