#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Frame-faithful production-audio conform for the final picture edit.
为最终画面剪辑生成逐剪点一致的现场声预混音轨。

Resolve's public scripting API cannot reliably change linked source-audio gain
or cleanup on every version. This CPU stage therefore trims the same source
ranges as picture, concatenates them in JSON order, and hands Resolve one exact
48 kHz stereo WAV. Resolve then imports video-only picture plus this audio bed,
which removes source/timeline drift and preserves deterministic gain decisions.

Resolve 公共脚本 API 在不同版本中无法稳定修改链接原声的音量和降噪。本 CPU
阶段按与画面完全相同的源时间范围裁切并串接现场声，输出一条 48 kHz 立体声 WAV；
Resolve 只导入画面和这条预混原声，从而消除音画错位。
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import logging
import math
import os
from pathlib import Path
import shutil
import subprocess
import sys
from typing import Any, Dict, List, Optional, Sequence


LOGGER_NAME = "cybereditor.program_audio"


class ProgramAudioError(RuntimeError):
    """Expected production-audio conform failure. / 可预期的现场声合成错误。"""


class ProgramAudioRenderer:
    """Render source audio for every final picture cut with FFmpeg. / 用 FFmpeg 合成所有最终剪点的原声。"""

    def __init__(
        self,
        timeline_path: os.PathLike,
        output_path: os.PathLike,
        ffmpeg_path: str = "ffmpeg",
        ffprobe_path: str = "ffprobe",
        logger: Optional[logging.Logger] = None,
    ) -> None:
        """
        Resolve paths and required FFmpeg tools. / 解析路径与所需 FFmpeg 工具。

        Parameters / 参数:
            timeline_path: Final ``timeline_cuts.json``. / 最终剪辑 JSON。
            output_path: Destination production-audio WAV. / 现场声 WAV 输出路径。
            ffmpeg_path/ffprobe_path: Tool name or absolute path. / 工具名称或绝对路径。
            logger: Optional application logger. / 可选应用日志器。
        """
        self.timeline_path = Path(timeline_path).expanduser().resolve()
        self.output_path = Path(output_path).expanduser().resolve()
        self.ffmpeg = self._resolve_tool(ffmpeg_path, "FFmpeg")
        self.ffprobe = self._resolve_tool(ffprobe_path, "ffprobe")
        self.logger = logger or logging.getLogger(LOGGER_NAME)

    @staticmethod
    def _resolve_tool(value: str, label: str) -> str:
        """Resolve one executable or raise a friendly error. / 定位一个工具，否则给出友好错误。"""
        candidate = str(value or "").strip()
        executable = candidate if Path(candidate).is_file() else shutil.which(candidate)
        if not executable:
            raise ProgramAudioError(f"未找到 {label} / {label} was not found on PATH.")
        return str(executable)

    def load_timeline(self) -> Dict[str, Any]:
        """Read and minimally validate the final handoff. / 读取并最小校验最终交接文件。"""
        try:
            payload = json.loads(self.timeline_path.read_text(encoding="utf-8-sig"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ProgramAudioError(
                f"无法读取 timeline_cuts.json / Cannot read timeline: {exc}"
            ) from exc
        clips = payload.get("clips") if isinstance(payload, dict) else None
        if not isinstance(clips, list) or not clips:
            raise ProgramAudioError("timeline_cuts.json 缺少 clips / Timeline has no clips.")
        return payload

    @staticmethod
    def _atomic_write_json(payload: Dict[str, Any], destination: Path) -> None:
        """Atomically replace one UTF-8 JSON file. / 原子替换 UTF-8 JSON 文件。"""
        temporary = destination.with_suffix(destination.suffix + ".tmp")
        temporary.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        os.replace(str(temporary), str(destination))

    def _has_audio_stream(self, path: Path) -> bool:
        """Return whether ffprobe finds at least one audio stream. / 判断素材是否包含音频流。"""
        completed = subprocess.run(
            [
                self.ffprobe,
                "-v",
                "error",
                "-select_streams",
                "a:0",
                "-show_entries",
                "stream=index",
                "-of",
                "csv=p=0",
                str(path),
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=30,
            check=False,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        return completed.returncode == 0 and bool((completed.stdout or "").strip())

    @staticmethod
    def _finite_number(value: Any, field: str) -> float:
        """Parse one finite number. / 解析一个有限数值。"""
        try:
            result = float(value)
        except (TypeError, ValueError) as exc:
            raise ProgramAudioError(f"{field} 不是有效数字 / is not numeric.") from exc
        if not math.isfinite(result):
            raise ProgramAudioError(f"{field} 必须是有限数值 / must be finite.")
        return result

    def build_command(self, payload: Dict[str, Any]) -> tuple[List[str], float]:
        """
        Build one exact FFmpeg concat graph for all picture cuts.
        为全部画面剪点构建一条精确的 FFmpeg 串接图。

        Parameters / 参数:
            payload: Validated final timeline payload. / 已校验的最终时间线数据。
        Returns / 返回:
            Command argument list and program duration. / 命令参数列表与成片时长。
        """
        inputs: List[str] = []
        filters: List[str] = []
        labels: List[str] = []
        program_duration = 0.0
        for index, raw in enumerate(payload.get("clips", [])):
            if not isinstance(raw, dict):
                raise ProgramAudioError(f"clips[{index}] 必须是对象 / must be an object.")
            source = Path(str(raw.get("file_name") or "")).expanduser().resolve()
            if not source.is_file():
                raise ProgramAudioError(f"找不到原声素材 / Source file not found: {source}")
            cut_in = self._finite_number(raw.get("cut_in_sec"), f"clips[{index}].cut_in_sec")
            cut_out = self._finite_number(raw.get("cut_out_sec"), f"clips[{index}].cut_out_sec")
            duration = cut_out - cut_in
            if cut_in < 0 or duration <= 0:
                raise ProgramAudioError(f"clips[{index}] 时间范围无效 / invalid source range.")
            input_index = index
            has_audio = self._has_audio_stream(source)
            if has_audio:
                inputs.extend(["-i", str(source)])
                chain = [
                    f"[{input_index}:a:0]atrim=start={cut_in:.6f}:end={cut_out:.6f}",
                    "asetpts=PTS-STARTPTS",
                ]
            else:
                inputs.extend(
                    [
                        "-f",
                        "lavfi",
                        "-t",
                        f"{duration:.6f}",
                        "-i",
                        "anullsrc=r=48000:cl=stereo",
                    ]
                )
                chain = [f"[{input_index}:a:0]atrim=duration={duration:.6f}", "asetpts=PTS-STARTPTS"]
            chain.extend(
                [
                    "aresample=48000:async=0:first_pts=0",
                    "aformat=sample_fmts=fltp:channel_layouts=stereo",
                ]
            )
            cleanup = str(raw.get("audio_cleanup") or "light").casefold()
            if cleanup == "strong":
                chain.extend(["highpass=f=90", "lowpass=f=15000", "afftdn=nr=16:nf=-30"])
            elif cleanup == "light":
                chain.extend(["highpass=f=70", "lowpass=f=17000", "afftdn=nr=9:nf=-36"])
            has_dialogue = bool(raw.get("has_dialogue"))
            gain = self._finite_number(raw.get("volume_db", 0), f"clips[{index}].volume_db")
            if has_dialogue:
                # The model may describe relative emphasis, but it must never bury
                # spoken content. Normalize every spoken cut and permit only a
                # restrained post-normalization trim.
                # 模型可表达相对轻重，但不能把对白压没；先统一响度，再限制微调范围。
                chain.append("loudnorm=I=-18:TP=-2:LRA=11")
                gain = min(3.0, max(-3.0, gain))
            else:
                gain = min(6.0, max(-24.0, gain))
            if abs(gain) >= 0.01:
                chain.append(f"volume={gain:.3f}dB")
            chain.extend(
                [
                    # Denoise and loudnorm can introduce a small filter delay or
                    # switch to a higher internal sample rate. Resample again,
                    # guarantee the exact whole duration, then regenerate sample-
                    # counted timestamps before concat. Without this reset the
                    # Resolve bed can lose roughly one filter frame per cut.
                    # 降噪/响度滤镜会引入少量延迟或改变内部采样率；再次重采样、补齐
                    # 精确总时长并按样本重建时间戳，避免每个剪点累计少一小段声音。
                    "aresample=48000:async=0:first_pts=0",
                    f"apad=whole_dur={duration:.6f}",
                    f"atrim=duration={duration:.6f}",
                    "asetpts=N/SR/TB" + f"[pa{index}]",
                ]
            )
            filters.append(",".join(chain))
            labels.append(f"[pa{index}]")
            program_duration += duration
        filters.append(
            "".join(labels)
            + f"concat=n={len(labels)}:v=0:a=1,"
            + f"atrim=duration={program_duration:.6f},alimiter=limit=0.95[program]"
        )
        command = [
            self.ffmpeg,
            "-hide_banner",
            "-y",
            *inputs,
            "-filter_complex",
            ";".join(filters),
            "-map",
            "[program]",
            "-ar",
            "48000",
            "-ac",
            "2",
            "-c:a",
            "pcm_s24le",
            str(self.output_path),
        ]
        return command, program_duration

    def render(self) -> Path:
        """Render the program WAV and publish its timeline metadata. / 渲染现场声 WAV 并写入时间线元数据。"""
        payload = self.load_timeline()
        command, program_duration = self.build_command(payload)
        self.output_path.parent.mkdir(parents=True, exist_ok=True)
        self.logger.info(
            "正在按 %d 个画面剪点预混现场声：%s / Conforming production audio for %d cuts",
            len(payload["clips"]),
            self.output_path,
            len(payload["clips"]),
        )
        completed = subprocess.run(
            command,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        if completed.returncode != 0 or not self.output_path.is_file():
            tail = "\n".join((completed.stderr or "").splitlines()[-24:])
            raise ProgramAudioError(
                f"现场声 FFmpeg 合成失败 / Production-audio conform failed:\n{tail}"
            )
        payload["audio_program"] = {
            "mode": "preconformed_source_audio",
            "bed_file": str(self.output_path),
            "duration_sec": round(program_duration, 6),
            "sample_rate": 48000,
            "channels": 2,
            "codec": "pcm_s24le",
            "rendered_at_utc": datetime.now(timezone.utc).isoformat(),
            "picture_cut_count": len(payload["clips"]),
        }
        self._atomic_write_json(payload, self.timeline_path)
        return self.output_path


def configure_logging(level: str) -> logging.Logger:
    """Configure standalone bilingual logs. / 配置独立双语日志。"""
    logging.basicConfig(
        level=getattr(logging, level.upper()),
        format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    )
    return logging.getLogger(LOGGER_NAME)


def build_parser() -> argparse.ArgumentParser:
    """Create production-audio CLI arguments. / 创建现场声合成命令行参数。"""
    parser = argparse.ArgumentParser(description="Conform source audio to final picture cuts.")
    parser.add_argument("--timeline", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--ffmpeg", default="ffmpeg")
    parser.add_argument("--ffprobe", default="ffprobe")
    parser.add_argument(
        "--log-level", default="INFO", choices=("DEBUG", "INFO", "WARNING", "ERROR")
    )
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    """Run the production-audio conformer. / 运行现场声合成器。"""
    args = build_parser().parse_args(argv)
    try:
        ProgramAudioRenderer(
            args.timeline,
            args.output,
            args.ffmpeg,
            args.ffprobe,
            configure_logging(args.log_level),
        ).render()
        return 0
    except ProgramAudioError as exc:
        logging.getLogger(LOGGER_NAME).error("%s", exc)
        return 2
    except KeyboardInterrupt:
        logging.getLogger(LOGGER_NAME).warning("用户中断现场声合成 / Production-audio conform interrupted.")
        return 130
    except Exception:
        logging.getLogger(LOGGER_NAME).exception("未预期现场声错误 / Unexpected production-audio error.")
        return 1


if __name__ == "__main__":
    sys.exit(main())
