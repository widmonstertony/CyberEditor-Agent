#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Canonical source/record frame schedule for every CyberEditor consumer.

为 CyberEditor 的画面、现场声、音乐、审片和 Resolve 生成唯一的源帧/记录帧时间表。
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation, ROUND_CEILING, ROUND_FLOOR, ROUND_HALF_UP
import hashlib
import json
import logging
import math
import os
from pathlib import Path
import shutil
import subprocess
from typing import Any, Dict, List, Mapping, Optional, Sequence


LOGGER_NAME = "cybereditor.frame_edl"
FRAME_EDL_VERSION = "1.0"


class FrameEDLError(RuntimeError):
    """Expected canonical-frame scheduling failure. / 可预期的统一帧时间表错误。"""


def _decimal(value: object, field: str) -> Decimal:
    """Parse one positive finite Decimal. / 解析一个正有限 Decimal。"""
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError) as exc:
        raise FrameEDLError(f"{field} 不是有效数字 / is not numeric.") from exc
    if not result.is_finite() or result <= 0:
        raise FrameEDLError(f"{field} 必须是正有限数 / must be positive and finite.")
    return result


def _finite_decimal(value: object, field: str) -> Decimal:
    """Parse one finite Decimal, including zero. / 解析一个可含零的有限 Decimal。"""
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError) as exc:
        raise FrameEDLError(f"{field} 不是有效数字 / is not numeric.") from exc
    if not result.is_finite():
        raise FrameEDLError(f"{field} 必须是有限数 / must be finite.")
    return result


def _rate_from_text(value: object) -> Decimal:
    """Parse an ffprobe fraction or decimal frame rate. / 解析 ffprobe 分数或小数帧率。"""
    text = str(value or "").strip()
    if "/" in text:
        numerator, denominator = text.split("/", 1)
        top = _decimal(numerator, "frame-rate numerator")
        bottom = _decimal(denominator, "frame-rate denominator")
        return top / bottom
    return _decimal(text, "frame rate")


def probe_video_fps(path: Path, ffprobe: Optional[str] = None) -> Decimal:
    """
    Read the actual first video-stream FPS with ffprobe.
    使用 ffprobe 读取首条视频流的真实 FPS。

    Parameters / 参数:
        path: Source or proxy file consumed by every downstream stage. / 下游实际使用的源或代理文件。
        ffprobe: Optional executable override. / 可选 ffprobe 路径。
    """
    executable = ffprobe or shutil.which("ffprobe")
    if not executable:
        raise FrameEDLError("找不到 ffprobe / ffprobe is required for frame EDL.")
    creation_flags = int(getattr(subprocess, "CREATE_NO_WINDOW", 0))
    try:
        completed = subprocess.run(
            [
                executable,
                "-v",
                "error",
                "-select_streams",
                "v:0",
                "-show_entries",
                "stream=avg_frame_rate,r_frame_rate",
                "-of",
                "json",
                str(path),
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
            creationflags=creation_flags,
        )
    except OSError as exc:
        raise FrameEDLError(f"无法运行 ffprobe / Could not run ffprobe: {exc}") from exc
    try:
        stream = json.loads(completed.stdout)["streams"][0]
    except (json.JSONDecodeError, KeyError, IndexError, TypeError) as exc:
        raise FrameEDLError(
            f"无法读取视频 FPS / Could not probe video FPS: {path}: {completed.stderr[-600:]}"
        ) from exc
    for key in ("avg_frame_rate", "r_frame_rate"):
        try:
            return _rate_from_text(stream.get(key))
        except FrameEDLError:
            continue
    raise FrameEDLError(f"视频没有有效 FPS / Video has no valid FPS: {path}")


def schedule_fingerprint(payload: Mapping[str, Any], source_rates: Sequence[Decimal]) -> str:
    """Hash every input that controls source and record frames. / 哈希所有影响源帧和记录帧的输入。"""
    clips = payload.get("clips")
    if not isinstance(clips, list):
        raise FrameEDLError("clips 必须是数组 / clips must be an array.")
    if len(source_rates) != len(clips):
        raise FrameEDLError("source FPS 数量与 clips 不匹配 / source-rate count mismatch.")
    contract = {
        "version": FRAME_EDL_VERSION,
        "project_fps": str(payload.get("project_fps")),
        "clips": [
            {
                "clip_id": item.get("clip_id", index + 1) if isinstance(item, dict) else None,
                "file_name": str(item.get("file_name") or "") if isinstance(item, dict) else "",
                "cut_in_sec": str(item.get("cut_in_sec")) if isinstance(item, dict) else "",
                "cut_out_sec": str(item.get("cut_out_sec")) if isinstance(item, dict) else "",
                "source_fps": format(rate, "f"),
            }
            for index, (item, rate) in enumerate(zip(clips, source_rates))
        ],
    }
    return hashlib.sha256(
        json.dumps(contract, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def build_frame_edl(
    payload: Mapping[str, Any],
    *,
    plan_path: Optional[Path] = None,
    ffprobe: Optional[str] = None,
    source_fps_overrides: Optional[Sequence[object]] = None,
) -> Dict[str, Any]:
    """
    Build one immutable ``[in, out)`` source/record-frame schedule.
    构建唯一且不可变的 ``[入点, 出点)`` 源帧/记录帧时间表。

    Parameters / 参数:
        payload: Validated timeline JSON object. / 已校验的时间线 JSON。
        plan_path: Used to resolve relative media paths. / 用于解析相对媒体路径。
        ffprobe: Optional ffprobe executable. / 可选 ffprobe 路径。
        source_fps_overrides: Test/integration rates in clip order. / 测试或集成提供的逐片段 FPS。

    Resolve receives inclusive source-frame out points, while every other stage
    uses this module's exclusive out points. Record duration is the ceiling of
    the selected source-frame duration on the project grid; this preserves the
    entire requested source interval and prevents per-consumer re-rounding.

    Resolve 接收包含式源出帧；其他阶段统一使用排除式出帧。记录时长按所选源帧区间
    在工程帧网格上向上取整，既不丢请求画面，也杜绝各模块各自重复舍入。
    """
    clips = payload.get("clips")
    if not isinstance(clips, list) or not clips:
        raise FrameEDLError("clips 必须是非空数组 / clips must be a non-empty array.")
    project_fps = _decimal(payload.get("project_fps"), "project_fps")
    if source_fps_overrides is not None and len(source_fps_overrides) != len(clips):
        raise FrameEDLError("source_fps_overrides 数量不匹配 / override count does not match clips.")

    base = (plan_path.parent if plan_path is not None else Path.cwd()).resolve()
    rates: List[Decimal] = []
    resolved_paths: List[Path] = []
    rate_cache: Dict[str, Decimal] = {}
    for index, raw in enumerate(clips):
        if not isinstance(raw, dict):
            raise FrameEDLError(f"clips[{index}] 必须是对象 / must be an object.")
        media = Path(str(raw.get("file_name") or "")).expanduser()
        if not media.is_absolute():
            media = base / media
        media = media.resolve()
        if not media.is_file() and source_fps_overrides is None:
            raise FrameEDLError(f"找不到时间表素材 / Frame-EDL media not found: {media}")
        resolved_paths.append(media)
        if source_fps_overrides is not None:
            rate = _decimal(source_fps_overrides[index], f"clips[{index}].source_fps")
        else:
            key = os.path.normcase(str(media))
            if key not in rate_cache:
                rate_cache[key] = probe_video_fps(media, ffprobe)
            rate = rate_cache[key]
        rates.append(rate)

    fingerprint = schedule_fingerprint(payload, rates)
    entries: List[Dict[str, Any]] = []
    record_cursor = 0
    original_cursor = Decimal("0")
    for index, (raw, media, source_fps) in enumerate(zip(clips, resolved_paths, rates)):
        cut_in = _finite_decimal(raw.get("cut_in_sec"), f"clips[{index}].cut_in_sec")
        cut_out = _finite_decimal(raw.get("cut_out_sec"), f"clips[{index}].cut_out_sec")
        if not cut_in.is_finite() or not cut_out.is_finite() or cut_in < 0 or cut_out <= cut_in:
            raise FrameEDLError(f"clips[{index}] 时间范围无效 / invalid cut range.")
        source_in = int((cut_in * source_fps).to_integral_value(rounding=ROUND_FLOOR))
        source_out = int((cut_out * source_fps).to_integral_value(rounding=ROUND_CEILING))
        source_out = max(source_in + 1, source_out)
        source_count = source_out - source_in
        record_count = max(
            1,
            int(
                (Decimal(source_count) * project_fps / source_fps).to_integral_value(
                    rounding=ROUND_CEILING
                )
            ),
        )
        original_duration = cut_out - cut_in
        entries.append(
            {
                "clip_id": raw.get("clip_id", index + 1),
                "file_name": str(media),
                "source_fps": format(source_fps, "f"),
                "source_frame_in": source_in,
                "source_frame_out_exclusive": source_out,
                "source_frame_count": source_count,
                "record_frame_in": record_cursor,
                "record_frame_out_exclusive": record_cursor + record_count,
                "record_frame_count": record_count,
                "original_timeline_in_sec": float(original_cursor),
                "original_timeline_out_sec": float(original_cursor + original_duration),
                "quantized_source_in_sec": float(Decimal(source_in) / source_fps),
                "quantized_source_out_sec": float(Decimal(source_out) / source_fps),
            }
        )
        record_cursor += record_count
        original_cursor += original_duration
    return {
        "schema_version": FRAME_EDL_VERSION,
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "fingerprint": fingerprint,
        "project_fps": format(project_fps, "f"),
        "total_record_frames": record_cursor,
        "program_duration_sec": float(Decimal(record_cursor) / project_fps),
        "original_program_duration_sec": float(original_cursor),
        "clips": entries,
    }


def validate_frame_edl(payload: Mapping[str, Any]) -> Dict[str, Any]:
    """Validate schedule continuity and clip identity. / 校验时间表连续性与片段身份。"""
    schedule = payload.get("frame_edl")
    clips = payload.get("clips")
    if not isinstance(schedule, dict) or schedule.get("schema_version") != FRAME_EDL_VERSION:
        raise FrameEDLError("缺少受支持的 frame_edl / Supported frame_edl is required.")
    entries = schedule.get("clips")
    if not isinstance(clips, list) or not isinstance(entries, list) or len(entries) != len(clips):
        raise FrameEDLError("frame_edl 与 clips 数量不匹配 / frame_edl clip count mismatch.")
    project_fps = _decimal(payload.get("project_fps"), "project_fps")
    schedule_fps = _decimal(schedule.get("project_fps"), "frame_edl.project_fps")
    if project_fps != schedule_fps:
        raise FrameEDLError("frame_edl 工程 FPS 已过期 / project FPS is stale.")
    cursor = 0
    source_rates: List[Decimal] = []
    for index, (clip, entry) in enumerate(zip(clips, entries)):
        if not isinstance(clip, dict) or not isinstance(entry, dict):
            raise FrameEDLError(f"frame_edl.clips[{index}] 格式无效 / invalid entry.")
        if str(entry.get("clip_id")) != str(clip.get("clip_id", index + 1)):
            raise FrameEDLError(f"frame_edl.clips[{index}] clip_id 不匹配 / mismatch.")
        try:
            source_in = int(entry["source_frame_in"])
            source_out = int(entry["source_frame_out_exclusive"])
            record_in = int(entry["record_frame_in"])
            record_out = int(entry["record_frame_out_exclusive"])
            source_count = int(entry["source_frame_count"])
            record_count = int(entry["record_frame_count"])
            source_rate = _decimal(
                entry["source_fps"], f"frame_edl.clips[{index}].source_fps"
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise FrameEDLError(f"frame_edl.clips[{index}] 缺少整数帧字段 / invalid frame fields.") from exc
        if (
            source_in < 0
            or source_out <= source_in
            or source_out - source_in != source_count
            or record_in != cursor
            or record_out <= record_in
            or record_out - record_in != record_count
        ):
            raise FrameEDLError(f"frame_edl.clips[{index}] 不连续或帧数错误 / discontinuous schedule.")
        source_rates.append(source_rate)
        cursor = record_out
    if int(schedule.get("total_record_frames", -1)) != cursor:
        raise FrameEDLError("frame_edl 总帧数错误 / total frame count mismatch.")
    expected_fingerprint = schedule_fingerprint(payload, source_rates)
    if str(schedule.get("fingerprint") or "") != expected_fingerprint:
        raise FrameEDLError(
            "frame_edl 与当前剪点或素材不一致 / schedule fingerprint is stale."
        )
    return dict(schedule)


def map_original_time_to_record_frame(
    schedule: Mapping[str, Any],
    seconds: object,
    *,
    rounding: str = "nearest",
) -> int:
    """
    Map a director-authored program second onto the canonical record grid.
    将导演按原始秒数写出的节目时间映射到统一记录帧网格。

    Parameters / 参数:
        schedule: Validated ``frame_edl``. / 已校验的帧时间表。
        seconds: Time on the pre-quantized program. / 量化前节目时间。
        rounding: ``floor``, ``ceil``, or ``nearest``. / 舍入方式。
    """
    try:
        value = Decimal(str(seconds))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise FrameEDLError("节目时间不是有效数字 / Program time is not numeric.") from exc
    if not value.is_finite():
        raise FrameEDLError("节目时间必须有限 / Program time must be finite.")
    entries = schedule.get("clips")
    if not isinstance(entries, list) or not entries:
        raise FrameEDLError("frame_edl 没有 clips / frame_edl has no clips.")
    value = max(Decimal("0"), value)
    chosen = entries[-1]
    for entry in entries:
        if value <= Decimal(str(entry.get("original_timeline_out_sec", 0))):
            chosen = entry
            break
    original_in = Decimal(str(chosen["original_timeline_in_sec"]))
    original_out = Decimal(str(chosen["original_timeline_out_sec"]))
    record_in = Decimal(int(chosen["record_frame_in"]))
    record_count = Decimal(int(chosen["record_frame_count"]))
    if value >= Decimal(str(entries[-1]["original_timeline_out_sec"])):
        return int(schedule["total_record_frames"])
    fraction = (value - original_in) / max(Decimal("0.000000001"), original_out - original_in)
    fraction = min(Decimal("1"), max(Decimal("0"), fraction))
    frame = record_in + fraction * record_count
    mode = {
        "floor": ROUND_FLOOR,
        "ceil": ROUND_CEILING,
        "nearest": ROUND_HALF_UP,
    }.get(rounding)
    if mode is None:
        raise FrameEDLError(f"未知舍入方式 / Unknown rounding mode: {rounding}")
    return int(frame.to_integral_value(rounding=mode))


def ensure_frame_edl(plan_path: os.PathLike, ffprobe: Optional[str] = None) -> Dict[str, Any]:
    """Build and atomically persist a canonical schedule. / 构建并原子写入统一帧时间表。"""
    path = Path(plan_path).expanduser().resolve()
    try:
        payload = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError) as exc:
        raise FrameEDLError(f"无法读取时间线 JSON / Cannot read timeline JSON: {exc}") from exc
    if not isinstance(payload, dict):
        raise FrameEDLError("时间线 JSON 根节点必须是对象 / Timeline root must be an object.")
    schedule = build_frame_edl(payload, plan_path=path, ffprobe=ffprobe)
    payload["frame_edl"] = schedule
    temporary = path.with_suffix(path.suffix + ".frame-edl.tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(str(temporary), str(path))
    return schedule


def build_parser() -> argparse.ArgumentParser:
    """Create CLI parser. / 创建命令行解析器。"""
    parser = argparse.ArgumentParser(description="Build canonical source/record frame EDL.")
    parser.add_argument("--timeline", required=True)
    parser.add_argument("--ffprobe", default="")
    parser.add_argument("--log-level", default="INFO")
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    """Run CLI with deterministic exit codes. / 以确定性退出码运行 CLI。"""
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=getattr(logging, str(args.log_level).upper(), logging.INFO),
        format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    )
    try:
        schedule = ensure_frame_edl(args.timeline, args.ffprobe or None)
    except FrameEDLError as exc:
        logging.getLogger(LOGGER_NAME).error("%s", exc)
        return 2
    logging.getLogger(LOGGER_NAME).info(
        "统一帧时间表完成：%d 段，%d 帧 / Canonical frame EDL: %d clips, %d frames",
        len(schedule["clips"]),
        int(schedule["total_record_frames"]),
        len(schedule["clips"]),
        int(schedule["total_record_frames"]),
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
