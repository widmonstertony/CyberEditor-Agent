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
from array import array
from datetime import datetime, timezone
import json
import logging
import math
from pathlib import Path
import re
import shutil
import subprocess
from typing import Any, Dict, List, Optional, Sequence


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
        Compare one grayscale structural frame per second after a common scale.
        统一缩放后每秒抽一帧，以灰度结构计算 SSIM。

        Parameters / 参数:
            approved/final: Approved master and Resolve export. / 审核母版与 Resolve 导出。
        """
        graph = (
            "[0:v]fps=1,scale=480:270:force_original_aspect_ratio=decrease,"
            "pad=480:270:(ow-iw)/2:(oh-ih)/2,format=gray[v0];"
            "[1:v]fps=1,scale=480:270:force_original_aspect_ratio=decrease,"
            "pad=480:270:(ow-iw)/2:(oh-ih)/2,format=gray[v1];[v0][v1]ssim"
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

    def _scene_change_signature(self, path: Path) -> List[float]:
        """
        Extract hard visual-change timestamps without trusting color appearance.
        提取硬画面变化时间，不把近似调色外观当作像素真值。

        Parameters / 参数:
            path: Media file to inspect. / 要检查的媒体文件。
        """
        completed = subprocess.run(
            [
                str(self.ffmpeg), "-hide_banner", "-nostats", "-i", str(path),
                "-vf", "select='gt(scene,0.16)',showinfo", "-an", "-f", "null", "-",
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
        )
        if completed.returncode != 0:
            raise FinalOutputQAError(
                "Could not extract scene-change signature from "
                f"{path}: " + "\n".join(completed.stderr.splitlines()[-8:])
            )
        return [
            float(value)
            for value in re.findall(r"pts_time:([0-9]+(?:\.[0-9]+)?)", completed.stderr)
        ]

    @staticmethod
    def _compare_scene_signatures(
        approved: Sequence[float],
        final: Sequence[float],
        *,
        tolerance_sec: float = 0.35,
    ) -> Dict[str, float]:
        """
        Score one-to-one scene-boundary agreement with temporal tolerance.
        在时间容差内按一对一方式计算场景边界一致率。
        """
        left = sorted(float(value) for value in approved)
        right = sorted(float(value) for value in final)
        if not left and not right:
            return {"precision": 1.0, "recall": 1.0, "f1": 1.0}
        used = set()
        matches = 0
        for boundary in left:
            choices = [
                (abs(boundary - candidate), index)
                for index, candidate in enumerate(right)
                if index not in used and abs(boundary - candidate) <= tolerance_sec
            ]
            if not choices:
                continue
            _distance, best_index = min(choices)
            used.add(best_index)
            matches += 1
        precision = matches / len(right) if right else 0.0
        recall = matches / len(left) if left else 0.0
        f1 = (
            2.0 * precision * recall / (precision + recall)
            if precision + recall > 0
            else 0.0
        )
        return {"precision": precision, "recall": recall, "f1": f1}

    def _audio_energy_fingerprint(
        self,
        path: Path,
        *,
        sample_rate: int = 2000,
        window_sec: float = 0.25,
    ) -> Dict[str, Any]:
        """
        Decode mono PCM and build a time-local loudness fingerprint.
        解码单声道 PCM，并建立按时间定位的响度指纹。

        The fingerprint deliberately measures the complete program rather than
        trusting stream duration alone.  A silent track, a displaced music bed,
        or unrelated audio can have the correct duration while still being the
        wrong film.

        Parameters / 参数:
            path: Media file to inspect. / 要检查的媒体文件。
            sample_rate: Low analysis sample rate. / 分析用低采样率。
            window_sec: RMS window duration. / RMS 窗口时长。
        """
        completed = subprocess.run(
            [
                str(self.ffmpeg),
                "-hide_banner",
                "-loglevel",
                "error",
                "-i",
                str(path),
                "-map",
                "0:a:0",
                "-vn",
                "-ac",
                "1",
                "-ar",
                str(sample_rate),
                "-f",
                "s16le",
                "pipe:1",
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
        if completed.returncode != 0 or not completed.stdout:
            detail = completed.stderr.decode("utf-8", errors="replace").strip()
            raise FinalOutputQAError(
                f"Could not decode the first audio stream from {path}: {detail}"
            )
        pcm = array("h")
        usable_bytes = len(completed.stdout) - (len(completed.stdout) % 2)
        pcm.frombytes(completed.stdout[:usable_bytes])
        if not pcm:
            raise FinalOutputQAError(f"Decoded audio is empty: {path}")

        window_samples = max(1, int(round(sample_rate * window_sec)))
        envelope_db: List[float] = []
        active_windows = 0
        square_sum = 0.0
        sample_count = 0
        for offset in range(0, len(pcm), window_samples):
            block = pcm[offset : offset + window_samples]
            if not block:
                continue
            block_square = sum(float(value) * float(value) for value in block)
            square_sum += block_square
            sample_count += len(block)
            rms = math.sqrt(block_square / len(block)) / 32768.0
            dbfs = 20.0 * math.log10(max(rms, 1e-8))
            dbfs = max(-100.0, min(0.0, dbfs))
            envelope_db.append(dbfs)
            if dbfs > -55.0:
                active_windows += 1
        if not envelope_db or sample_count <= 0:
            raise FinalOutputQAError(f"No analyzable audio samples in {path}")
        overall_rms = math.sqrt(square_sum / sample_count) / 32768.0
        return {
            "window_sec": window_sec,
            "sample_rate": sample_rate,
            "envelope_db": envelope_db,
            "overall_dbfs": 20.0 * math.log10(max(overall_rms, 1e-8)),
            "active_fraction": active_windows / len(envelope_db),
        }

    @staticmethod
    def _compare_audio_fingerprints(
        approved: Dict[str, Any], final: Dict[str, Any]
    ) -> Dict[str, float]:
        """
        Compare aligned loudness envelopes with bounded length tolerance.
        在有限长度容差内比较对齐的响度包络。

        Parameters / 参数:
            approved/final: Results from ``_audio_energy_fingerprint``.
                ``_audio_energy_fingerprint`` 生成的结果。
        """
        left = [float(value) for value in approved.get("envelope_db", [])]
        right = [float(value) for value in final.get("envelope_db", [])]
        count = min(len(left), len(right))
        if count < 4:
            return {"envelope_similarity": 0.0, "mean_abs_db_error": 100.0}
        left = left[:count]
        right = right[:count]
        # Convert dB into a bounded 0..1 loudness feature.  This keeps silent
        # regions meaningful without allowing one loud transient to dominate.
        left_feature = [(max(-80.0, value) + 80.0) / 80.0 for value in left]
        right_feature = [(max(-80.0, value) + 80.0) / 80.0 for value in right]
        dot = sum(a * b for a, b in zip(left_feature, right_feature))
        left_norm = math.sqrt(sum(value * value for value in left_feature))
        right_norm = math.sqrt(sum(value * value for value in right_feature))
        cosine = dot / max(1e-12, left_norm * right_norm)
        mean_abs_error = sum(abs(a - b) for a, b in zip(left, right)) / count
        return {
            "envelope_similarity": max(0.0, min(1.0, cosine)),
            "mean_abs_db_error": mean_abs_error,
        }

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
        # The Resolve source timeline legitimately applies RCM, the director's
        # creative grade, DRX, stabilization, and tracking that the low-resolution
        # FFmpeg review can only approximate.  A grayscale structural threshold is
        # therefore intentionally tolerant; edit-boundary agreement below remains
        # the hard sequencing gate.
        if not math.isfinite(ssim) or ssim < 0.55:
            failures.append(
                f"Resolve picture structure diverged from the approved film "
                f"(grayscale sampled SSIM={ssim:.4f})."
            )
        approved_scenes = self._scene_change_signature(approved)
        final_scenes = self._scene_change_signature(final)
        scene_comparison = self._compare_scene_signatures(
            approved_scenes, final_scenes
        )
        if (
            max(len(approved_scenes), len(final_scenes)) >= 2
            and float(scene_comparison["f1"]) < 0.65
        ):
            failures.append(
                "Resolve edit boundaries diverged from the approved film "
                f"(scene-boundary F1={scene_comparison['f1']:.4f})."
            )
        approved_audio = self._audio_energy_fingerprint(approved)
        final_audio = self._audio_energy_fingerprint(final)
        audio_comparison = self._compare_audio_fingerprints(
            approved_audio, final_audio
        )
        if float(approved_audio["active_fraction"]) >= 0.02:
            if float(final_audio["active_fraction"]) < 0.02 or float(
                final_audio["overall_dbfs"]
            ) < -60.0:
                failures.append(
                    "Final export audio is effectively silent although the approved film is audible."
                )
        if (
            float(audio_comparison["envelope_similarity"]) < 0.94
            or float(audio_comparison["mean_abs_db_error"]) > 6.0
        ):
            failures.append(
                "Resolve audio diverged from the approved program "
                f"(envelope similarity={audio_comparison['envelope_similarity']:.4f}, "
                f"mean error={audio_comparison['mean_abs_db_error']:.2f} dB)."
            )
        report = {
            "schema_version": "1.1",
            "generated_at_utc": datetime.now(timezone.utc).isoformat(),
            "approved_master": approved_probe,
            "final_export": final_probe,
            "duration_tolerance_sec": round(tolerance, 4),
            "sampled_ssim": round(ssim, 6),
            "picture_structure_qa": {
                "approved_scene_boundaries_sec": [
                    round(value, 4) for value in approved_scenes
                ],
                "final_scene_boundaries_sec": [
                    round(value, 4) for value in final_scenes
                ],
                "scene_boundary_precision": round(
                    float(scene_comparison["precision"]), 6
                ),
                "scene_boundary_recall": round(
                    float(scene_comparison["recall"]), 6
                ),
                "scene_boundary_f1": round(
                    float(scene_comparison["f1"]), 6
                ),
            },
            "audio_qa": {
                "approved_overall_dbfs": round(
                    float(approved_audio["overall_dbfs"]), 3
                ),
                "final_overall_dbfs": round(
                    float(final_audio["overall_dbfs"]), 3
                ),
                "approved_active_fraction": round(
                    float(approved_audio["active_fraction"]), 6
                ),
                "final_active_fraction": round(
                    float(final_audio["active_fraction"]), 6
                ),
                "envelope_similarity": round(
                    float(audio_comparison["envelope_similarity"]), 6
                ),
                "mean_abs_db_error": round(
                    float(audio_comparison["mean_abs_db_error"]), 3
                ),
            },
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
            "Final Resolve export matches approved master: duration %.3fs, SSIM %.4f, audio %.4f",
            actual,
            ssim,
            float(audio_comparison["envelope_similarity"]),
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
