#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Deterministic multi-video discovery and extraction-manifest helpers.
确定性的多视频发现与提取清单辅助模块。

Only Python's standard library is used so the UI and the serial orchestrator
can share exactly the same input rules without importing media/ML packages.

本模块只使用 Python 标准库，使 UI 与串行调度器共享完全一致的输入规则，且不会
导入音视频或机器学习包。
"""

from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
from typing import Any, Dict, Iterable, List, Optional, Sequence

try:
    from .color_analysis import build_project_color_match
except ImportError:  # pragma: no cover - direct ``python src/...`` fallback.
    from color_analysis import build_project_color_match


VIDEO_EXTENSIONS = frozenset(
    {
        ".3gp",
        ".avi",
        ".braw",
        ".m2ts",
        ".m4v",
        ".mkv",
        ".mov",
        ".mp4",
        ".mpeg",
        ".mpg",
        ".mts",
        ".mxf",
        ".r3d",
        ".webm",
    }
)


class MediaManifestError(ValueError):
    """Expected media-discovery failure. / 可预期的素材发现错误。"""


def discover_video_files(
    explicit_paths: Sequence[os.PathLike] = (),
    input_folder: Optional[os.PathLike] = None,
    recursive: bool = True,
) -> List[Path]:
    """
    Discover supported videos from explicit selections and one folder.
    从显式选择和一个文件夹中发现支持的视频。

    Parameters / 参数:
        explicit_paths:
            Files selected by the user; order is preserved.
            用户选择的文件；保持选择顺序。
        input_folder:
            Optional directory scanned in natural, case-insensitive order.
            可选目录；按自然、不区分大小写的顺序扫描。
        recursive:
            Include nested folders. / 是否包含子文件夹。

    Returns / 返回:
        Absolute, de-duplicated media paths. / 绝对且去重后的素材路径。
    """
    discovered: List[Path] = []
    for value in explicit_paths:
        path = Path(value).expanduser().resolve()
        if not path.is_file():
            raise MediaManifestError(
                f"找不到输入视频 / Input video not found: {path}"
            )
        if path.suffix.casefold() not in VIDEO_EXTENSIONS:
            raise MediaManifestError(
                f"不支持的视频扩展名 / Unsupported video extension: {path}"
            )
        discovered.append(path)

    if input_folder:
        folder = Path(input_folder).expanduser().resolve()
        if not folder.is_dir():
            raise MediaManifestError(
                f"找不到素材文件夹 / Input folder not found: {folder}"
            )
        iterator: Iterable[Path] = folder.rglob("*") if recursive else folder.glob("*")
        folder_files = [
            path.resolve()
            for path in iterator
            if path.is_file() and path.suffix.casefold() in VIDEO_EXTENSIONS
        ]
        discovered.extend(sorted(folder_files, key=_natural_path_key))

    unique: List[Path] = []
    seen = set()
    for path in discovered:
        key = os.path.normcase(str(path))
        if key in seen:
            continue
        seen.add(key)
        unique.append(path)
    if not unique:
        raise MediaManifestError(
            "没有找到可用视频。请选择一个或多个视频，或选择包含视频的文件夹。"
            " / No supported videos were found."
        )
    return unique


def match_proxy_files(
    sources: Sequence[Path],
    explicit_proxies: Sequence[os.PathLike] = (),
    proxy_folder: Optional[os.PathLike] = None,
) -> Dict[Path, Path]:
    """
    Match optional proxy media to source paths without silent ambiguity.
    将可选代理素材与源素材匹配，且不静默处理歧义。

    Exact ordered mapping is accepted when explicit proxy and source counts
    match. Otherwise proxies are matched by case-insensitive stem. Unmatched
    sources safely fall back to their originals.

    当显式代理数量与源素材数量相同时按顺序匹配，否则按不区分大小写的文件名主干
    匹配。未匹配的源素材安全回退到原片。
    """
    candidates: List[Path] = []
    for value in explicit_proxies:
        path = Path(value).expanduser().resolve()
        if not path.is_file():
            raise MediaManifestError(
                f"找不到代理素材 / Proxy not found: {path}"
            )
        candidates.append(path)
    if proxy_folder:
        folder = Path(proxy_folder).expanduser().resolve()
        if not folder.is_dir():
            raise MediaManifestError(
                f"找不到代理文件夹 / Proxy folder not found: {folder}"
            )
        candidates.extend(
            sorted(
                (
                    path.resolve()
                    for path in folder.rglob("*")
                    if path.is_file()
                    and path.suffix.casefold() in VIDEO_EXTENSIONS
                ),
                key=_natural_path_key,
            )
        )

    if explicit_proxies and len(explicit_proxies) == len(sources):
        return dict(zip(sources, candidates[: len(sources)]))

    by_stem: Dict[str, List[Path]] = {}
    for path in candidates:
        by_stem.setdefault(path.stem.casefold(), []).append(path)
    result: Dict[Path, Path] = {}
    for source in sources:
        matches = by_stem.get(source.stem.casefold(), [])
        if len(matches) > 1:
            raise MediaManifestError(
                f"代理文件名不唯一 / Ambiguous proxies for {source.name}: "
                + ", ".join(str(path) for path in matches)
            )
        result[source] = matches[0] if matches else source
    return result


def make_asset_id(index: int, source: Path) -> str:
    """
    Build a stable, filesystem-safe asset identifier.
    构建稳定且适合文件系统的素材标识符。
    """
    slug = re.sub(r"[^A-Za-z0-9_-]+", "_", source.stem).strip("_")
    slug = slug[:40] or "video"
    digest = hashlib.sha1(
        os.path.normcase(str(source)).encode("utf-8", errors="surrogatepass")
    ).hexdigest()[:8]
    return f"{index:04d}_{slug}_{digest}"


def build_combined_raw_data(
    assets: Sequence[Dict[str, Any]],
) -> Dict[str, Any]:
    """
    Build the schema-v2 handoff consumed by the multimodal director.
    构建多模态导演读取的 schema-v2 交接数据。
    """
    if not assets:
        raise MediaManifestError(
            "无法构建空素材清单 / Cannot build an empty asset manifest."
        )
    total_duration = sum(float(item.get("duration_sec") or 0.0) for item in assets)
    normalized_assets = list(assets)
    return {
        "schema_version": "2.0",
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "duration_sec": round(total_duration, 3),
        "asset_count": len(assets),
        "assets": normalized_assets,
        "color_match_plan": build_project_color_match(normalized_assets),
    }


def atomic_write_json(payload: Dict[str, Any], destination: Path) -> None:
    """Atomically write UTF-8 JSON. / 原子写入 UTF-8 JSON。"""
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_suffix(destination.suffix + ".tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    os.replace(temporary, destination)


def _natural_path_key(path: Path) -> List[object]:
    """Return a human-friendly path sort key. / 返回符合人类习惯的路径排序键。"""
    return [
        (0, int(part)) if part.isdigit() else (1, part.casefold())
        for part in re.split(r"(\d+)", str(path))
    ]
