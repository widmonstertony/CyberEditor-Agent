#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""CPU-only exposure and white-balance analysis for editorial keyframes.
用于剪辑关键帧的纯 CPU 曝光与白平衡分析。

The measurements are conservative scene statistics, not a replacement for a
colorist.  Every result carries a confidence score and is clamped before it can
reach Resolve.

这些测量是保守的场景统计，并不能替代调色师。每个结果都带置信度，且在进入
Resolve 前会受到安全限幅。
"""

from __future__ import annotations

import math
from statistics import median
from typing import Any, Dict, Iterable, List, Mapping, Sequence


def analyze_bgr_frame(frame: Any, cv2_module: Any) -> Dict[str, Any]:
    """
    Measure exposure and neutral-pixel channel balance from one BGR frame.
    从一帧 BGR 画面测量曝光和中性色通道平衡。

    Parameters / 参数:
        frame: OpenCV/Numpy BGR image. / OpenCV/Numpy BGR 图像。
        cv2_module: Imported ``cv2`` module. / 已导入的 ``cv2`` 模块。

    Returns / 返回:
        JSON-safe luma percentiles, RGB neutral estimate, correction gains,
        and confidence. / 可写入 JSON 的亮度分位数、中性色 RGB、修正增益与置信度。
    """
    try:
        import numpy as np
    except ImportError as exc:  # OpenCV wheels normally include Numpy.
        raise RuntimeError("Numpy is required for color analysis.") from exc
    if frame is None or getattr(frame, "size", 0) == 0:
        raise ValueError("Color analysis requires a non-empty frame.")

    small = cv2_module.resize(frame, (192, 108), interpolation=cv2_module.INTER_AREA)
    rgb = small[:, :, ::-1].astype(np.float32) / 255.0
    luma = (
        rgb[:, :, 0] * 0.2126
        + rgb[:, :, 1] * 0.7152
        + rgb[:, :, 2] * 0.0722
    )
    maximum = np.max(rgb, axis=2)
    minimum = np.min(rgb, axis=2)
    saturation = (maximum - minimum) / np.maximum(maximum, 1e-6)
    midtone_mask = (luma >= 0.12) & (luma <= 0.88)
    neutral_mask = midtone_mask & (saturation <= 0.12)
    neutral_fraction = float(np.count_nonzero(neutral_mask)) / float(neutral_mask.size)
    if neutral_fraction >= 0.004:
        neutral_rgb = np.median(rgb[neutral_mask], axis=0)
        confidence = min(1.0, 0.25 + neutral_fraction * 12.0)
        method = "neutral_pixels"
    else:
        usable = rgb[midtone_mask]
        if usable.size == 0:
            usable = rgb.reshape((-1, 3))
        neutral_rgb = np.median(usable, axis=0)
        confidence = 0.12
        method = "gray_world_fallback"

    red, green, blue = [max(1e-4, float(value)) for value in neutral_rgb]
    gains = (
        min(1.8, max(0.55, green / red)),
        1.0,
        min(1.8, max(0.55, green / blue)),
    )
    return {
        "median_luma": round(float(np.percentile(luma, 50)), 6),
        "shadow_luma_p05": round(float(np.percentile(luma, 5)), 6),
        "highlight_luma_p95": round(float(np.percentile(luma, 95)), 6),
        "neutral_rgb": [round(red, 6), round(green, 6), round(blue, 6)],
        "rgb_gain": [round(value, 6) for value in gains],
        "neutral_fraction": round(neutral_fraction, 6),
        "confidence": round(confidence, 4),
        "method": method,
    }


def summarize_color_samples(samples: Iterable[Mapping[str, Any]]) -> Dict[str, Any]:
    """
    Aggregate keyframe measurements into one source-level estimate.
    将关键帧测量聚合为一条素材级估计。

    Parameters / 参数:
        samples: Keyframe color metric dictionaries. / 关键帧色彩指标字典。

    Returns / 返回:
        Robust medians and a conservative confidence value.
        稳健中位数与保守置信度。
    """
    valid: List[Mapping[str, Any]] = []
    for sample in samples:
        try:
            luma = float(sample.get("median_luma", 0))
            gains = sample.get("rgb_gain")
            confidence = float(sample.get("confidence", 0))
        except (TypeError, ValueError):
            continue
        if (
            0 < luma <= 1
            and isinstance(gains, Sequence)
            and len(gains) == 3
            and confidence >= 0
        ):
            valid.append(sample)
    if not valid:
        return {
            "sample_count": 0,
            "median_luma": 0.0,
            "rgb_gain": [1.0, 1.0, 1.0],
            "confidence": 0.0,
            "method": "unavailable",
        }
    high_confidence = [item for item in valid if float(item.get("confidence", 0)) >= 0.25]
    selected = high_confidence or valid
    channel_gains = []
    for channel in range(3):
        channel_gains.append(
            median(float(item["rgb_gain"][channel]) for item in selected)
        )
    return {
        "sample_count": len(valid),
        "high_confidence_sample_count": len(high_confidence),
        "median_luma": round(median(float(item["median_luma"]) for item in selected), 6),
        "rgb_gain": [round(min(1.6, max(0.625, value)), 6) for value in channel_gains],
        "confidence": round(
            min(1.0, median(float(item.get("confidence", 0)) for item in selected)),
            4,
        ),
        "method": "neutral_median" if high_confidence else "gray_world_median",
    }


def build_project_color_match(assets: Sequence[Mapping[str, Any]]) -> Dict[str, Any]:
    """
    Build bounded per-source exposure/WB corrections around a shared reference.
    围绕统一参考构建受限的逐素材曝光/白平衡修正。

    Parameters / 参数:
        assets: Combined raw-data assets. / 合并 raw-data 中的素材列表。

    Returns / 返回:
        Project reference and an ``asset_id`` keyed correction map.
        项目参考值与按 ``asset_id`` 索引的修正映射。
    """
    usable = []
    for asset in assets:
        analysis = asset.get("color_analysis")
        if not isinstance(analysis, Mapping):
            continue
        try:
            luma = float(analysis.get("median_luma", 0))
            confidence = float(analysis.get("confidence", 0))
        except (TypeError, ValueError):
            continue
        if 0.03 <= luma <= 0.95 and confidence > 0:
            usable.append((asset, analysis, luma, confidence))
    if not usable:
        return {
            "enabled": False,
            "reference_luma": 0.0,
            "reference_asset_id": "",
            "assets": {},
        }

    reference_luma = median(item[2] for item in usable)
    reference = min(
        usable,
        key=lambda item: (
            abs(item[2] - reference_luma),
            -item[3],
            str(item[0].get("asset_id", "")),
        ),
    )
    corrections: Dict[str, Any] = {}
    for asset, analysis, luma, confidence in usable:
        asset_id = str(asset.get("asset_id") or "").strip()
        if not asset_id:
            continue
        exposure_ev = math.log(max(reference_luma, 1e-6) / max(luma, 1e-6), 2)
        exposure_ev = min(1.5, max(-1.5, exposure_ev))
        raw_gains = analysis.get("rgb_gain", [1.0, 1.0, 1.0])
        gains = [
            min(1.5, max(0.667, float(raw_gains[index])))
            for index in range(3)
        ]
        # Low-confidence gray-world estimates are intentionally blended toward
        # identity to avoid neutralizing intentional colored lighting.
        blend = min(1.0, max(0.0, confidence))
        if confidence < 0.25:
            blend *= 0.35
        gains = [1.0 + (value - 1.0) * blend for value in gains]
        corrections[asset_id] = {
            "exposure_ev": round(exposure_ev, 4),
            "rgb_gain": [round(value, 6) for value in gains],
            "confidence": round(confidence, 4),
            "reference_asset_id": str(reference[0].get("asset_id") or ""),
            "method": analysis.get("method", "unknown"),
        }
    return {
        "enabled": True,
        "reference_luma": round(reference_luma, 6),
        "reference_asset_id": str(reference[0].get("asset_id") or ""),
        "assets": corrections,
    }
