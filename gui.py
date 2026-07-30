#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""CyberEditor-Agent modern desktop UI entry point. / 现代桌面 UI 启动入口。"""

try:
    from src.modern_gui import main
except ModuleNotFoundError as exc:
    if exc.name != "customtkinter":
        raise
    # Friendly fallback for partially upgraded environments.
    from src.gui import main


if __name__ == "__main__":
    raise SystemExit(main())
