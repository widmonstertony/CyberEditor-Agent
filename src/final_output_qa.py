#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""File-level QA for the actual Resolve export against its approved master.
对真实 Resolve 导出文件与已审核母版执行文件级验收。

The rough-cut reviewer used to approve one MP4 while Resolve rendered a different
timeline. This module verifies the file the user will actually watch: picture/audio
duration must remain aligned and sampled visual similarity must stay high.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import logging
import math
from pathlib import Path
import re
import shutil
import subprocess
from typing import Any, Dict, Optional, Sequence


LOGGER_NAME = "cybereditor.final_output_qa"


class FinalOutputQAError(RuntimeError):
    """Raised when the real delivery file does not match the approved film. / 最终文件不匹配时抛出。"""


class FinalOutputQA:
    """Validate one rendered delivery against the blind-review-approved master. / 验收最终导出。"""

    def __init__(self, logger: Optional[logging.Logger] = None) -> None:
        """Locate FFmpeg tools without modifying either media file. / 定位 FFmpeg 工具且不修改媒体。"""
        self.logger = logger or logging.getLogger(LOGGER_NAME)
        self.ffprobe = shutil.which("ffprobe")
        self.ffmpeg = shutil.which("ffmpeg")
        if not self.ffprobe or not self.ffmpeg:
            raise FinalOutputQAError("FFmpeg and ffprobe are required for final-output QA.")

    def _probe(self, path: Path) -> Dict[str, Any]:
        """
        Read stream durations and frame rate using ffprobe.
        使用 ffprobe 读取流时长与帧率。

        Parameters / 参数:
            path: Existing media path. / 已存在的媒体路径。
        """
        completed = subprocess.run(
            [
                str(self.ffprobe), "-v", "error", "-show_entries",
                "format=duration:stream=index,codec_type,duration,avg_frame_rate",
                "-of", "json", str(path),
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
        )
        if completed.returncode != 0:
            raise FinalOutputQAError(
                f"ffprobe failed for {path}: {completed.stderr.strip()}"
            )
        try:
            payload = json.loads(completed.stdout)
        except json.JSONDecodeError as exc:
            raise FinalOutputQAError(f"Invalid ffprobe JSON for {path}: {exc}") from exc
        streams = payload.get("streams") if isinstance(payload, dict) else None
        streams = streams if isinstance(streams, list) else []
        format_data = payload.get("format") if isinstance(payload, dict) else {}
        try:
            container_duration = float((format_data or {}).get("duration", 0) or 0)
        except (TypeError, ValueError):
            container_duration = 0.0
        result: Dict[str, Any] = {
            "path": str(path),
            "container_duration_sec": container_duration,
            "video_duration_sec": 0.0,
            "audio_duration_sec": 0.0,
            "fps": 0.0,
            "has_video": False,
            "has_audio": False,
        }
        for stream in streams:
            if not isinstance(stream, dict):
                continue
            kind = str(stream.get("codec_type") or "")
            try:
                stream_duration = float(stream.get("duration", 0) or 0)
            except (TypeError, ValueError):
                stream_duration = 0.0
            if stream_duration <= 0:
                stream_duration = container_duration
            if kind == "video":
                result["has_video"] = True
                result["video_duration_sec"] = max(result["video_duration_sec"], stream_duration)
                rate = str(stream.get("avg_frame_rate") or "0/1")
                try:
                    numerator, denominator = rate.split("/", 1)
                    result["fps"] = float(numerator) / float(denominator)
                except (ValueError, ZeroDivisionError):
                    pass
            elif kind == "audio":
                result["has_audio"] = True
                result["audio_duration_sec"] = max(result["audio_duration_sec"], stream_duration)
        return result

    def _sampled_ssim(self, approved: Path, final: Path) -> float:
        """
        Compare one frame per second after a small common scale.
        统一缩放后每秒抽一帧计算 SSIM。

        Parameters / 参数:
            approved/final: Approved master and Resolve export. / 审核母版与 Resolve 导出。
        """
        graph = (
            "[0:v]fps=1,scale=480:270:force_original_aspect_ratio=decrease,"
            "pad=480:270:(ow-iw)/2:(oh-ih)/2[v0];"
            "[1:v]fps=1,scale=480:270:force_original_aspect_ratio=decrease,"
            "pad=480:270:(ow-iw)/2:(oh-ih)/2[v1];[v0][v1]ssim"
        )
        completed = subprocess.run(
            [
                str(self.ffmpeg), "-hide_banner", "-nostats", "-i", str(approved),
                "-i", str(final), "-filter_complex", graph, "-an", "-f", "null", "-",
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
        )
        matches = re.findall(r"All:(\d+(?:\.\d+)?)", completed.stderr)
        if completed.returncode != 0 or not matches:
            raise FinalOutputQAError(
                "Could not compare approved and final pictures: "
                + "\n".join(completed.stderr.splitlines()[-8:])
            )
        return float(matches[-1])

    def run(self, final_path: Path, approved_path: Path, output_path: Path) -> Dict[str, Any]:
        """
        Validate and atomically write a QA report; raise on any failed gate.
        验收并原子写入报告；任一硬门失败即抛出错误。

        Parameters / 参数:
            final_path: Actual Resolve export. / 实际 Resolve 导出文件。
            approved_path: Blind-review-approved master. / 已通过盲审的母版。
            output_path: Destination JSON report. / QA JSON 输出路径。
        """
        final = Path(final_path).expanduser().resolve()
        approved = Path(approved_path).expanduser().resolve()
        destination = Path(output_path).expanduser().resolve()
        for label, path in (("final export", final), ("approved master", approved)):
            if not path.is_file():
                raise FinalOutputQAError(f"{label} not found: {path}")
        final_probe = self._probe(final)
        approved_probe = self._probe(approved)
        failures = []
        if not final_probe["has_video"] or not final_probe["has_audio"]:
            failures.append("Final export must contain both video and audio streams.")
        expected = float(approved_probe["container_duration_sec"] or 0)
        actual = float(final_probe["container_duration_sec"] or 0)
        fps = max(1.0, float(final_probe.get("fps") or approved_probe.get("fps") or 25.0))
        tolerance = max(0.12, 2.0 / fps)
        if expected <= 0 or actual <= 0 or abs(actual - expected) > tolerance:
            failures.append(
                f"Duration changed after approval: approved={expected:.3f}s, final={actual:.3f}s."
            )
        video_duration = float(final_probe.get("video_duration_sec") or actual)
        audio_duration = float(final_probe.get("audio_duration_sec") or 0)
        if abs(video_duration - audio_duration) > tolerance:
            failures.append(
                f"Final picture/audio lengths differ: video={video_duration:.3f}s, "
                f"audio={audio_duration:.3f}s."
            )
        ssim = self._sampled_ssim(approved, final)
        if not math.isfinite(ssim) or ssim < 0.88:
            failures.append(
                f"Resolve picture diverged from approved master (sampled SSIM={ssim:.4f})."
            )
        report = {
            "schema_version": "1.0",
            "generated_at_utc": datetime.now(timezone.utc).isoformat(),
            "approved_master": approved_probe,
            "final_export": final_probe,
            "duration_tolerance_sec": round(tolerance, 4),
            "sampled_ssim": round(ssim, 6),
            "failures": failures,
            "passes": not failures,
        }
        destination.parent.mkdir(parents=True, exist_ok=True)
        temporary = destination.with_suffix(destination.suffix + ".tmp")
        temporary.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        temporary.replace(destination)
        if failures:
            raise FinalOutputQAError(
                "Final Resolve export failed QA: " + "; ".join(failures)
            )
        self.logger.info(
            "Final Resolve export matches approved master: duration %.3fs, SSIM %.4f",
            actual,
            ssim,
        )
        return report


def build_parser() -> argparse.ArgumentParser:
    """Create CLI arguments. / 创建命令行参数。"""
    parser = argparse.ArgumentParser(description="Validate actual Resolve export.")
    parser.add_argument("--final", required=True)
    parser.add_argument("--approved", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--log-level", default="INFO")
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    """Run final-output QA with deterministic exit codes. / 运行最终文件验收。"""
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=getattr(logging, str(args.log_level).upper(), logging.INFO),
        format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    )
    try:
        FinalOutputQA().run(Path(args.final), Path(args.approved), Path(args.output))
        return 0
    except FinalOutputQAError as exc:
        logging.getLogger(LOGGER_NAME).error("%s", exc)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())

