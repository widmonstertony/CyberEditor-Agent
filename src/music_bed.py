#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Deterministic CPU conformer for a multi-cue editorial music bed.
用于多段配乐的确定性 CPU 合成器。

The final director chooses music and cue timing; this module performs no
creative inference. It trims, loudness-matches, crossfades, ducks dialogue,
preserves intentional silence, and writes one stereo 48 kHz WAV for Resolve.
最终导演决定曲目与 cue 时序；本模块不做创意推理，只负责裁切、响度匹配、淡化、
对白压低和留白，并为 Resolve 写出一条 48 kHz 立体声 WAV。
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
from typing import Any, Dict, List, Optional, Sequence, Tuple


LOGGER_NAME = "cybereditor.music_bed"


class MusicBedError(RuntimeError):
    """Expected music-bed conform failure. / 可预期的音乐床合成错误。"""


class MusicBedRenderer:
    """
    Render the validated cue sheet in ``timeline_cuts.json`` with FFmpeg.
    使用 FFmpeg 渲染 ``timeline_cuts.json`` 中已校验的 cue 表。
    """

    def __init__(
        self,
        timeline_path: os.PathLike,
        output_path: os.PathLike,
        ffmpeg_path: str = "ffmpeg",
        logger: Optional[logging.Logger] = None,
    ) -> None:
        """
        Store absolute paths and locate FFmpeg. / 保存绝对路径并定位 FFmpeg。

        Parameters / 参数:
            timeline_path: Final director JSON. / 最终导演 JSON。
            output_path: Destination WAV. / 目标 WAV。
            ffmpeg_path: Executable name or absolute path. / FFmpeg 名称或绝对路径。
            logger: Optional application logger. / 可选应用日志器。
        """
        self.timeline_path = Path(timeline_path).expanduser().resolve()
        self.output_path = Path(output_path).expanduser().resolve()
        executable = shutil.which(ffmpeg_path) if not Path(ffmpeg_path).is_file() else ffmpeg_path
        if not executable:
            raise MusicBedError("未找到 FFmpeg / FFmpeg was not found on PATH.")
        self.ffmpeg = str(executable)
        self.logger = logger or logging.getLogger(LOGGER_NAME)

    def load_timeline(self) -> Dict[str, Any]:
        """Read and minimally validate the timeline handoff. / 读取并最小校验时间线交接文件。"""
        try:
            payload = json.loads(self.timeline_path.read_text(encoding="utf-8-sig"))
        except (OSError, json.JSONDecodeError) as exc:
            raise MusicBedError(f"无法读取 timeline_cuts.json / Cannot read timeline: {exc}") from exc
        if not isinstance(payload, dict) or not isinstance(payload.get("clips"), list):
            raise MusicBedError("timeline_cuts.json 格式无效 / Invalid timeline schema.")
        return payload

    @staticmethod
    def _program_duration(payload: Dict[str, Any]) -> float:
        """Calculate picture duration from validated source ranges. / 从已校验的源范围计算画面时长。"""
        return sum(
            max(0.0, float(item.get("cut_out_sec", 0)) - float(item.get("cut_in_sec", 0)))
            for item in payload.get("clips", [])
            if isinstance(item, dict)
        )

    @staticmethod
    def _dialogue_intervals(payload: Dict[str, Any]) -> List[Tuple[float, float]]:
        """
        Infer conservative dialogue spans from final story roles.
        根据最终叙事角色保守推断对白区间。

        Explicit ``has_dialogue`` wins. Otherwise interview, context, opening,
        and closing roles are protected; visual B-roll and bridges remain full.
        显式 ``has_dialogue`` 优先；否则保护采访、背景、开场和收尾，纯 B-roll/桥段不压低。
        """
        intervals: List[Tuple[float, float]] = []
        cursor = 0.0
        protected_roles = {"interview", "context", "opening", "closing"}
        for clip in payload.get("clips", []):
            if not isinstance(clip, dict):
                continue
            duration = max(
                0.0,
                float(clip.get("cut_out_sec", 0)) - float(clip.get("cut_in_sec", 0)),
            )
            explicit = clip.get("has_dialogue")
            has_dialogue = explicit is True or (
                explicit is not False
                and str(clip.get("story_role") or "").casefold() in protected_roles
            )
            if has_dialogue and duration > 0:
                intervals.append((cursor, cursor + duration))
            cursor += duration
        return intervals

    @staticmethod
    def _overlap_local(
        intervals: Sequence[Tuple[float, float]],
        cue_start: float,
        cue_end: float,
    ) -> List[Tuple[float, float]]:
        """Clip timeline intervals to one cue and convert to cue-local time. / 将时间线区间裁到 cue 并转换为 cue 内时间。"""
        result: List[Tuple[float, float]] = []
        for start, end in intervals:
            overlap_start = max(start, cue_start)
            overlap_end = min(end, cue_end)
            if overlap_end > overlap_start:
                result.append((overlap_start - cue_start, overlap_end - cue_start))
        return result

    @staticmethod
    def _smooth_duck_filter(
        start: float,
        end: float,
        duck_db: float,
        cue_duration: float,
        attack_sec: float = 0.25,
        release_sec: float = 0.60,
    ) -> str:
        """
        Build click-free frame-evaluated dialogue ducking automation.
        构建无突变点击声、逐帧计算的对白 ducking 自动化。

        Parameters / 参数:
            start/end: Dialogue interval in cue-local seconds. / cue 内对白区间。
            duck_db: Negative gain below dialogue. / 对白下方的负增益。
            cue_duration: Cue duration used to bound release. / 用于限制释放段的 cue 时长。
            attack_sec/release_sec: Automation ramp lengths. / 自动化进入与释放时长。
        """
        ratio = math.pow(10.0, float(duck_db) / 20.0)
        attack_start = max(0.0, start - max(0.01, attack_sec))
        release_end = min(cue_duration, end + max(0.01, release_sec))
        attack_length = max(0.001, start - attack_start)
        release_length = max(0.001, release_end - end)
        expression = (
            f"if(lt(t,{attack_start:.6f}),1,"
            f"if(lt(t,{start:.6f}),1-(1-{ratio:.8f})*(t-{attack_start:.6f})/{attack_length:.6f},"
            f"if(lt(t,{end:.6f}),{ratio:.8f},"
            f"if(lt(t,{release_end:.6f}),{ratio:.8f}+(1-{ratio:.8f})*(t-{end:.6f})/{release_length:.6f},1))))"
        )
        return f"volume='{expression}':eval=frame"

    @staticmethod
    def _atomic_write_json(payload: Dict[str, Any], destination: Path) -> None:
        """Atomically replace a UTF-8 JSON file. / 原子替换 UTF-8 JSON 文件。"""
        temporary = destination.with_suffix(destination.suffix + ".tmp")
        temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(str(temporary), str(destination))

    def render(self) -> Optional[Path]:
        """
        Render ``music_bed.wav`` and update the timeline's ``bed_file``.
        渲染 ``music_bed.wav``，并更新交接 JSON 的 ``bed_file``。

        Returns / 返回:
            Output path, or ``None`` when the director intentionally chose no music.
            输出路径；若导演有意不配乐则返回 ``None``。
        """
        payload = self.load_timeline()
        music_plan = payload.get("music_plan")
        if not isinstance(music_plan, dict):
            return None
        cues = music_plan.get("cues")
        if not isinstance(cues, list) or not cues:
            self.logger.info("导演选择不使用配乐 / Director chose no music bed")
            return None
        program_duration = self._program_duration(payload)
        if program_duration <= 0:
            raise MusicBedError("最终时间线时长为零 / Final timeline duration is zero.")
        dialogue = self._dialogue_intervals(payload)
        silence = [
            (float(item.get("timeline_in_sec", 0)), float(item.get("timeline_out_sec", 0)))
            for item in music_plan.get("silence_regions", [])
            if isinstance(item, dict)
        ]

        inputs: List[str] = []
        filters: List[str] = []
        cue_labels: List[str] = []
        audit_cues: List[Dict[str, Any]] = []
        for cue in cues:
            if not isinstance(cue, dict):
                continue
            path = Path(str(cue.get("file_name") or "")).expanduser().resolve()
            if not path.is_file():
                raise MusicBedError(f"配乐文件不存在 / Music file not found: {path}")
            timeline_in = max(0.0, float(cue.get("timeline_in_sec", 0)))
            timeline_out = min(program_duration, float(cue.get("timeline_out_sec", program_duration)))
            track_in = max(0.0, float(cue.get("track_in_sec", 0)))
            duration = min(
                timeline_out - timeline_in,
                float(cue.get("track_out_sec", track_in)) - track_in,
            )
            if duration <= 0.05:
                continue
            input_index = len(cue_labels)
            inputs.extend(["-i", str(path)])
            target_lufs = float(cue.get("target_lufs", -24) or -24)
            measured = cue.get("integrated_lufs")
            try:
                gain_db = target_lufs - float(measured)
            except (TypeError, ValueError):
                gain_db = 0.0
            gain_db = min(12.0, max(-12.0, gain_db))
            fade_in = min(duration / 2, max(0.0, float(cue.get("fade_in_sec", 1.5))))
            fade_out = min(duration / 2, max(0.0, float(cue.get("fade_out_sec", 2.0))))
            chain = [
                f"[{input_index}:a:0]atrim=start={track_in:.6f}:duration={duration:.6f}",
                "asetpts=PTS-STARTPTS",
                "aresample=48000",
                "aformat=sample_fmts=fltp:channel_layouts=stereo",
                f"volume={gain_db:.3f}dB",
            ]
            if fade_in > 0:
                chain.append(f"afade=t=in:st=0:d={fade_in:.6f}")
            if fade_out > 0:
                chain.append(
                    f"afade=t=out:st={max(0.0, duration - fade_out):.6f}:d={fade_out:.6f}"
                )
            duck_db = min(0.0, max(-24.0, float(cue.get("duck_under_dialogue_db", -9))))
            dialogue_overlaps = self._overlap_local(
                dialogue, timeline_in, timeline_in + duration
            )
            if dialogue_overlaps and duck_db > -6.0:
                # Never trust a model-selected 0 dB value over real speech. This
                # fail-safe also repairs older plans without rerunning the model.
                # 真实对白优先于模型给出的 0 dB；此守门也能安全修复旧计划。
                duck_db = -10.0
            for start, end in dialogue_overlaps:
                chain.append(self._smooth_duck_filter(start, end, duck_db, duration))
            for start, end in self._overlap_local(silence, timeline_in, timeline_in + duration):
                chain.append(
                    f"volume=0:enable='between(t,{start:.6f},{end:.6f})'"
                )
            delay_ms = max(0, int(round(timeline_in * 1000)))
            label = f"cue{len(cue_labels)}"
            chain.append(f"adelay={delay_ms}|{delay_ms}[{label}]")
            filters.append(",".join(chain))
            cue_labels.append(f"[{label}]")
            audit_cues.append({
                "cue_id": cue.get("cue_id", label),
                "file_name": str(path),
                "source_url": cue.get("source_url", ""),
                "sha256": cue.get("sha256", ""),
                "license": cue.get("license", ""),
                "license_url": cue.get("license_url", ""),
                "timeline_in_sec": timeline_in,
                "timeline_out_sec": timeline_in + duration,
                "track_in_sec": track_in,
                "track_out_sec": track_in + duration,
                "applied_gain_db": round(gain_db, 3),
                "duck_under_dialogue_db": duck_db,
            })
        if not cue_labels:
            return None
        filters.append(
            "".join(cue_labels)
            + f"amix=inputs={len(cue_labels)}:duration=longest:normalize=0,"
            + f"apad=pad_dur={program_duration:.6f},atrim=duration={program_duration:.6f},"
            + "alimiter=limit=0.95[bed]"
        )
        self.output_path.parent.mkdir(parents=True, exist_ok=True)
        command = [self.ffmpeg, "-hide_banner", "-y", *inputs, "-filter_complex", ";".join(filters),
                   "-map", "[bed]", "-ar", "48000", "-ac", "2", "-c:a", "pcm_s24le", str(self.output_path)]
        self.logger.info(
            "正在合成 %d 段音乐床：%s / Rendering %d-cue music bed",
            len(cue_labels), self.output_path, len(cue_labels),
        )
        completed = subprocess.run(
            command,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
        )
        if completed.returncode != 0 or not self.output_path.is_file():
            tail = "\n".join(completed.stderr.splitlines()[-20:])
            raise MusicBedError(f"音乐床 FFmpeg 合成失败 / Music-bed render failed:\n{tail}")
        music_plan["bed_file"] = str(self.output_path)
        music_plan["bed_render"] = {
            "rendered_at_utc": datetime.now(timezone.utc).isoformat(),
            "sample_rate": 48000,
            "channels": 2,
            "codec": "pcm_s24le",
            "program_duration_sec": round(program_duration, 4),
            "cue_count": len(cue_labels),
        }
        payload["music_plan"] = music_plan
        self._atomic_write_json(payload, self.timeline_path)
        audit_path = self.output_path.with_suffix(".audit.json")
        self._atomic_write_json(
            {
                "schema_version": "1.0",
                "generated_at_utc": datetime.now(timezone.utc).isoformat(),
                "warning": "Source and license records are audit metadata, not a grant of rights.",
                "timeline": str(self.timeline_path),
                "music_bed": str(self.output_path),
                "cues": audit_cues,
            },
            audit_path,
        )
        return self.output_path


def configure_logging(level: str) -> logging.Logger:
    """Configure standalone bilingual logs. / 配置独立双语日志。"""
    logging.basicConfig(
        level=getattr(logging, level.upper()),
        format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    )
    return logging.getLogger(LOGGER_NAME)


def build_parser() -> argparse.ArgumentParser:
    """Create music-bed CLI arguments. / 创建音乐床命令行参数。"""
    parser = argparse.ArgumentParser(description="Render a final multi-cue music bed.")
    parser.add_argument("--timeline", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--ffmpeg", default="ffmpeg")
    parser.add_argument("--log-level", default="INFO", choices=("DEBUG", "INFO", "WARNING", "ERROR"))
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    """Run the CPU music-bed conformer. / 运行 CPU 音乐床合成器。"""
    args = build_parser().parse_args(argv)
    try:
        MusicBedRenderer(
            args.timeline, args.output, args.ffmpeg, configure_logging(args.log_level)
        ).render()
        return 0
    except MusicBedError as exc:
        logging.getLogger(LOGGER_NAME).error("%s", exc)
        return 2
    except KeyboardInterrupt:
        logging.getLogger(LOGGER_NAME).warning("用户中断音乐床合成 / Music-bed render interrupted.")
        return 130
    except Exception:
        logging.getLogger(LOGGER_NAME).exception("未预期音乐床错误 / Unexpected music-bed error.")
        return 1


if __name__ == "__main__":
    sys.exit(main())
