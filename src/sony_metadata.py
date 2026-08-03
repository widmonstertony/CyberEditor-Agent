#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Sony XAVC sidecar metadata and Resolve input-transform mapping.
Sony XAVC 伴随 XML 元数据与 Resolve 输入变换映射。

Sony cameras normally place ``C0001M01.XML`` beside ``C0001.MP4``.  Reading
that file is safer than assuming every clip used the same picture profile.

Sony 相机通常会在 ``C0001.MP4`` 旁写入 ``C0001M01.XML``。读取该文件比假设
全部片段使用同一图片配置更安全。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, Iterable, Optional
import xml.etree.ElementTree as ET


class SonyMetadataError(RuntimeError):
    """Expected Sony sidecar parsing failure. / 可预期的 Sony 元数据解析错误。"""


def _local_name(tag: str) -> str:
    """Return an XML tag without its namespace. / 返回不含命名空间的 XML 标签名。"""
    return str(tag).rsplit("}", 1)[-1]


def _candidate_sidecars(media_path: Path) -> Iterable[Path]:
    """Yield conventional Sony sidecar paths. / 生成常见 Sony 伴随 XML 路径。"""
    source = Path(media_path).expanduser().resolve()
    yield source.with_name(source.stem + "M01.XML")
    yield source.with_name(source.stem + "M01.xml")
    yield source.with_suffix(".XML")
    yield source.with_suffix(".xml")


def find_sony_sidecar(media_path: Path) -> Optional[Path]:
    """
    Find the XML sidecar belonging to one media file.
    查找一条媒体对应的 XML 伴随文件。

    Parameters / 参数:
        media_path: Absolute or relative video path. / 视频绝对或相对路径。

    Returns / 返回:
        Resolved XML path, or ``None`` when no sidecar exists.
        解析后的 XML 路径；不存在时返回 ``None``。
    """
    for candidate in _candidate_sidecars(Path(media_path)):
        if candidate.is_file():
            return candidate.resolve()
    return None


def map_resolve_input_transform(
    capture_gamma: str, capture_primaries: str
) -> Dict[str, Any]:
    """
    Map Sony XML values to one explicit Resolve input transform.
    将 Sony XML 字段映射为明确的 Resolve 输入变换。

    Unknown or display-referred profiles deliberately remain untransformed;
    silently applying an incorrect Log transform is worse than flagging the
    clip for review.

    未知或显示参照配置会刻意保持不变；错误套用 Log 变换比标记待检查更危险。
    """
    gamma = str(capture_gamma or "").strip().casefold()
    primaries = str(capture_primaries or "").strip().casefold()
    mappings = {
        ("s-log3-cine", "s-gamut3-cine"): (
            "sony_slog3_sgamut3cine", "Sony S-Gamut3.Cine", "S-Log3"
        ),
        ("s-log3", "s-gamut3-cine"): (
            "sony_slog3_sgamut3cine", "Sony S-Gamut3.Cine", "S-Log3"
        ),
        ("s-log3", "s-gamut3"): (
            "sony_slog3_sgamut3", "Sony S-Gamut3", "S-Log3"
        ),
        ("s-log2", "s-gamut"): (
            "sony_slog2_sgamut", "Sony S-Gamut", "S-Log2"
        ),
    }
    mapped = mappings.get((gamma, primaries))
    if mapped:
        profile, color_space, resolve_gamma = mapped
        return {
            "camera_profile": profile,
            "is_log": True,
            "resolve_input_color_space": color_space,
            "resolve_input_gamma": resolve_gamma,
            "transform_supported": True,
        }
    display_referred = gamma in {
        "rec709", "ex-cine1", "ex-cine2", "ex-cine3", "ex-cine4"
    }
    return {
        "camera_profile": "rec709" if display_referred else "unknown",
        "is_log": False,
        "resolve_input_color_space": "Rec.709",
        "resolve_input_gamma": "Gamma 2.4",
        "transform_supported": display_referred,
    }


def read_sony_sidecar(sidecar_path: Path) -> Dict[str, Any]:
    """
    Parse one Sony ``NonRealTimeMeta`` XML file.
    解析一份 Sony ``NonRealTimeMeta`` XML 文件。

    Parameters / 参数:
        sidecar_path: Existing Sony XML path. / 已存在的 Sony XML 路径。

    Returns / 返回:
        Normalized camera, recording, and color metadata.
        规范化后的相机、录制与色彩元数据。
    """
    path = Path(sidecar_path).expanduser().resolve()
    try:
        root = ET.parse(path).getroot()
    except (OSError, ET.ParseError) as exc:
        raise SonyMetadataError(
            f"无法解析 Sony XML / Cannot parse Sony XML: {path} ({exc})"
        ) from exc

    result: Dict[str, Any] = {
        "source": "sony_non_realtime_metadata",
        "sidecar_path": str(path),
        "camera_manufacturer": "Sony",
        "camera_model": "",
        "capture_gamma": "",
        "capture_primaries": "",
        "coding_equations": "",
        "capture_fps": "",
        "format_fps": "",
        "video_codec": "",
    }
    for element in root.iter():
        name = _local_name(element.tag)
        if name == "Device":
            result["camera_manufacturer"] = element.attrib.get(
                "manufacturer", result["camera_manufacturer"]
            )
            result["camera_model"] = element.attrib.get("modelName", "")
        elif name == "VideoFrame":
            result["capture_fps"] = element.attrib.get("captureFps", "")
            result["format_fps"] = element.attrib.get("formatFps", "")
            result["video_codec"] = element.attrib.get("videoCodec", "")
        elif name == "Item":
            item_name = element.attrib.get("name", "")
            value = element.attrib.get("value", "")
            if item_name == "CaptureGammaEquation":
                result["capture_gamma"] = value
            elif item_name == "CaptureColorPrimaries":
                result["capture_primaries"] = value
            elif item_name == "CodingEquations":
                result["coding_equations"] = value

    result.update(
        map_resolve_input_transform(
            result["capture_gamma"], result["capture_primaries"]
        )
    )
    result["confidence"] = 1.0 if result["capture_gamma"] else 0.0
    return result


def detect_sony_color_metadata(media_path: Path) -> Dict[str, Any]:
    """
    Detect source color metadata without guessing a global picture profile.
    检测单条素材的色彩元数据，不猜测全局图片配置。

    Parameters / 参数:
        media_path: Source video path. / 源视频路径。

    Returns / 返回:
        A normalized dictionary. Missing XML produces an explicit ``unknown``
        result so downstream code can apply a user-selected fallback.
        返回规范化字典；XML 缺失时明确标记为 ``unknown``，由下游采用用户回退值。
    """
    source = Path(media_path).expanduser().resolve()
    sidecar = find_sony_sidecar(source)
    if sidecar is None:
        return {
            "source": "missing",
            "sidecar_path": "",
            "camera_profile": "unknown",
            "capture_gamma": "",
            "capture_primaries": "",
            "resolve_input_color_space": "",
            "resolve_input_gamma": "",
            "transform_supported": False,
            "is_log": False,
            "confidence": 0.0,
        }
    return read_sony_sidecar(sidecar)
