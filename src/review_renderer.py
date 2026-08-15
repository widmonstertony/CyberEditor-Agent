#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Render an immediately viewable MP4 from the AI edit plan with FFmpeg.
使用 FFmpeg 按 AI 剪辑计划渲染可立即观看的 MP4。

Resolve remains the editable timeline target.  This renderer exists because
Resolve's public scripting API does not expose a reliable general-purpose
transition insertion method.  Both outputs consume the same validated JSON;
the review render implements the requested transitions, audio cleanup, looks,
and gentle motion without loading another AI model.

Resolve 仍负责生成可编辑时间线。本渲染器用于弥补 Resolve 公开脚本 API 缺少可靠
通用转场写入方法的问题。两种输出读取同一份已校验 JSON；预览成片会真正执行转场、
音频清理、基础风格和轻微镜头运动，且不会加载另一个 AI 模型。
"""

from __future__ import annotations

import argparse
import json
import logging
import math
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
from typing import Any, Dict, List, NamedTuple, Optional, Sequence, Tuple

from .color_pipeline import ensure_sony_pp8_display_lut
from .frame_edl import (
    FrameEDLError,
    map_original_time_to_record_frame,
    validate_frame_edl,
)


LOGGER_NAME = "cybereditor.review"


class ReviewRenderError(RuntimeError):
    """Expected review-render failure. / 可预期的预览渲染错误。"""


class RenderClip(NamedTuple):
    """Validated review clip and constrained effects. / 已校验的预览片段及受限效果。"""

    file_name: str
    cut_in_sec: float
    cut_out_sec: float
    transition_to_next: str = "cut"
    transition_duration_sec: float = 0.0
    audio_cleanup: str = "light"
    color_look: str = "neutral"
    motion: str = "static"
    volume_db: float = 0.0
    source_color: Optional[Dict[str, Any]] = None
    color_match: Optional[Dict[str, Any]] = None
    creative_grade: Optional[Dict[str, Any]] = None
    source_duration_sec: float = 0.0
    record_duration_sec: float = 0.0


class ReviewRenderer:
    """
    Build and run one deterministic FFmpeg filter graph.
    构建并运行一个确定性的 FFmpeg 滤镜图。

    Parameters / 参数:
        plan_path:
            AI-produced ``timeline_cuts.json``. / AI 生成的剪辑计划。
        output_path:
            Final MP4 destination. / 最终 MP4 输出位置。
        width, height:
            Review resolution; defaults to 1080p. / 预览分辨率，默认 1080p。
        logger:
            Optional application logger. / 可选应用日志器。
    """

    def __init__(
        self,
        plan_path: os.PathLike,
        output_path: os.PathLike,
        width: int = 1920,
        height: int = 1080,
        logger: Optional[logging.Logger] = None,
    ) -> None:
        """Validate configuration without invoking FFmpeg. / 校验配置但暂不调用 FFmpeg。"""
        self.plan_path = Path(plan_path).expanduser().resolve()
        self.output_path = Path(output_path).expanduser().resolve()
        self.width = int(width)
        self.height = int(height)
        self.logger = logger or logging.getLogger(LOGGER_NAME)
        self.color_pipeline: Dict[str, Any] = {}
        self.music_plan: Dict[str, Any] = {}
        self.audio_program: Dict[str, Any] = {}
        self.graphics_plan: Dict[str, Any] = {}
        self.technical_lut_path: Optional[Path] = None
        if self.width < 320 or self.height < 180:
            raise ReviewRenderError(
                "预览分辨率过小 / Review resolution is too small."
            )

    def run(self) -> Path:
        """
        Validate the plan, render all selected clips, and return the MP4 path.
        校验计划、渲染全部入选片段并返回 MP4 路径。
        """
        ffmpeg = shutil.which("ffmpeg")
        ffprobe = shutil.which("ffprobe")
        if not ffmpeg or not ffprobe:
            raise ReviewRenderError(
                "生成预览需要 FFmpeg/ffprobe / FFmpeg and ffprobe are required."
            )
        fps, clips = self.load_plan()
        self.output_path.parent.mkdir(parents=True, exist_ok=True)
        sources = self.color_pipeline.get("sources", {})
        has_slog3 = any(
            str(item.get("resolve_input_gamma") or "").casefold() == "s-log3"
            for item in (sources.values() if isinstance(sources, dict) else [])
            if isinstance(item, dict)
        )
        legacy_slog3 = str(self.color_pipeline.get("camera_profile", "")).casefold() == "sony_pp8_slog3_sgamut3cine"
        if bool(self.color_pipeline.get("enabled")) and (has_slog3 or legacy_slog3):
            self.technical_lut_path = ensure_sony_pp8_display_lut(
                self.output_path.parent / "technical_luts" / "sony_pp8_to_rec709.cube"
            )
            self.logger.info(
                "预览已启用 Sony PP8 技术还原 / Sony PP8 technical transform enabled for preview"
            )
        command, filter_text, duration = self.build_command(
            ffmpeg, ffprobe, fps, clips
        )
        script_path: Optional[Path] = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                suffix=".fffilter",
                prefix="cybereditor_",
                dir=str(self.output_path.parent),
                delete=False,
            ) as handle:
                handle.write(filter_text)
                script_path = Path(handle.name)
            script_index = command.index("__FILTER_SCRIPT__")
            command[script_index] = str(script_path)
            self._run_ffmpeg(command, duration)
        finally:
            if script_path is not None:
                try:
                    script_path.unlink()
                except FileNotFoundError:
                    pass
        if not self.output_path.is_file() or self.output_path.stat().st_size == 0:
            raise ReviewRenderError(
                f"FFmpeg 未生成预览 / FFmpeg produced no output: {self.output_path}"
            )
        self.logger.info(
            "预览成片完成：%s / Review render complete: %s",
            self.output_path,
            self.output_path,
        )
        return self.output_path

    def load_plan(self) -> Tuple[float, List[RenderClip]]:
        """Read and strictly validate the edit plan. / 读取并严格校验剪辑计划。"""
        if not self.plan_path.is_file():
            raise ReviewRenderError(
                f"找不到剪辑计划 / Edit plan not found: {self.plan_path}"
            )
        try:
            payload = json.loads(
                self.plan_path.read_text(encoding="utf-8-sig")
            )
        except (OSError, ValueError) as exc:
            raise ReviewRenderError(
                f"无法解析剪辑计划 / Cannot parse edit plan: {exc}"
            ) from exc
        try:
            fps = float(payload["project_fps"])
            raw_clips = payload["clips"]
        except (KeyError, TypeError, ValueError) as exc:
            raise ReviewRenderError(
                "剪辑计划缺少 project_fps/clips / Invalid edit plan."
            ) from exc
        if not math.isfinite(fps) or fps <= 0 or not isinstance(raw_clips, list):
            raise ReviewRenderError("剪辑计划 FPS/clips 无效 / Invalid FPS/clips.")
        pipeline = payload.get("color_pipeline")
        self.color_pipeline = pipeline if isinstance(pipeline, dict) else {}
        music = payload.get("music_plan")
        self.music_plan = music if isinstance(music, dict) else {}
        audio_program = payload.get("audio_program")
        self.audio_program = audio_program if isinstance(audio_program, dict) else {}
        try:
            frame_edl = validate_frame_edl(payload)
        except FrameEDLError as exc:
            raise ReviewRenderError(
                f"审片需要统一帧时间表 / Review render requires frame_edl: {exc}"
            ) from exc
        schedule_entries = frame_edl["clips"]
        graphics = payload.get("graphics_plan")
        self.graphics_plan = dict(graphics) if isinstance(graphics, dict) else {}
        raw_graphics = self.graphics_plan.get("items")
        if isinstance(raw_graphics, list):
            mapped_graphics: List[Dict[str, Any]] = []
            for graphic in raw_graphics:
                if not isinstance(graphic, dict):
                    continue
                mapped = dict(graphic)
                mapped["timeline_in_sec"] = (
                    map_original_time_to_record_frame(
                        frame_edl, graphic.get("timeline_in_sec", 0), rounding="floor"
                    )
                    / fps
                )
                mapped["timeline_out_sec"] = (
                    map_original_time_to_record_frame(
                        frame_edl,
                        graphic.get("timeline_out_sec", 0),
                        rounding="ceil",
                    )
                    / fps
                )
                mapped_graphics.append(mapped)
            self.graphics_plan["items"] = mapped_graphics

        result: List[RenderClip] = []
        for index, (item, frame_entry) in enumerate(zip(raw_clips, schedule_entries)):
            if not isinstance(item, dict):
                raise ReviewRenderError(f"clips[{index}] 必须是对象 / must be an object.")
            path = Path(str(item.get("file_name", ""))).expanduser()
            if not path.is_absolute():
                path = self.plan_path.parent / path
            path = path.resolve()
            if not path.is_file():
                raise ReviewRenderError(
                    f"找不到入选素材 / Selected media not found: {path}"
                )
            source_fps = self._finite(
                frame_entry.get("source_fps"), f"frame_edl.clips[{index}].source_fps"
            )
            source_in_frame = int(frame_entry["source_frame_in"])
            source_out_frame = int(frame_entry["source_frame_out_exclusive"])
            record_frames = int(frame_entry["record_frame_count"])
            start = source_in_frame / source_fps
            end = source_out_frame / source_fps
            record_duration = record_frames / fps
            if start < 0 or end - start < 0.2:
                raise ReviewRenderError(
                    f"clips[{index}] 时间范围无效 / invalid time range."
                )
            result.append(
                RenderClip(
                    file_name=str(path),
                    cut_in_sec=start,
                    cut_out_sec=end,
                    transition_to_next=self._choice(
                        item.get("transition_to_next"),
                        {"cut", "cross_dissolve", "fade_black"},
                        "cut",
                    ),
                    transition_duration_sec=max(
                        0.0,
                        min(
                            2.0,
                            self._finite(
                                item.get("transition_duration_sec", 0.0),
                                f"clips[{index}].transition_duration_sec",
                            ),
                        ),
                    ),
                    audio_cleanup=self._choice(
                        item.get("audio_cleanup"),
                        {"none", "light", "strong"},
                        "light",
                    ),
                    color_look=self._choice(
                        item.get("color_look"),
                        {"source", "neutral", "warm", "cool", "contrast"},
                        "neutral",
                    ),
                    motion=self._choice(
                        item.get("motion"),
                        {"static", "gentle_push_in"},
                        "static",
                    ),
                    volume_db=max(
                        -60.0,
                        min(
                            12.0,
                            self._finite(
                                item.get("volume_db", 0.0),
                                f"clips[{index}].volume_db",
                            ),
                        ),
                    ),
                    source_color=(
                        dict(item["source_color"])
                        if isinstance(item.get("source_color"), dict) else None
                    ),
                    color_match=(
                        dict(item["color_match"])
                        if isinstance(item.get("color_match"), dict) else None
                    ),
                    creative_grade=(
                        dict(item["creative_grade"])
                        if isinstance(item.get("creative_grade"), dict) else None
                    ),
                    source_duration_sec=end - start,
                    record_duration_sec=record_duration,
                )
            )
        if not result:
            raise ReviewRenderError("剪辑计划没有片段 / Edit plan has no clips.")
        return fps, result

    def build_command(
        self,
        ffmpeg: str,
        ffprobe: str,
        fps: float,
        clips: Sequence[RenderClip],
    ) -> Tuple[List[str], str, float]:
        """
        Build FFmpeg arguments and a filter-complex script.
        构建 FFmpeg 参数与 filter-complex 脚本。
        """
        command: List[str] = [ffmpeg, "-hide_banner", "-y"]
        filters: List[str] = []
        video_labels: List[str] = []
        audio_labels: List[str] = []
        durations = [
            clip.record_duration_sec or (clip.cut_out_sec - clip.cut_in_sec)
            for clip in clips
        ]
        program_audio_text = str(self.audio_program.get("bed_file") or "").strip()
        program_audio_path = (
            Path(program_audio_text).expanduser().resolve()
            if program_audio_text else None
        )
        if program_audio_path is not None and not program_audio_path.is_file():
            raise ReviewRenderError(
                f"找不到预混现场声 / Conformed program audio not found: {program_audio_path}"
            )
        use_program_audio = program_audio_path is not None

        input_index = 0
        for index, (clip, duration) in enumerate(zip(clips, durations)):
            source_duration = clip.source_duration_sec or (
                clip.cut_out_sec - clip.cut_in_sec
            )
            command.extend(
                [
                    "-ss",
                    self._number(clip.cut_in_sec),
                    "-t",
                    self._number(source_duration),
                    "-i",
                    clip.file_name,
                ]
            )
            video_input = input_index
            input_index += 1
            audio_input: Optional[int] = None
            if not use_program_audio:
                if self._has_audio(ffprobe, Path(clip.file_name)):
                    audio_input = video_input
                else:
                    command.extend(
                        [
                            "-f",
                            "lavfi",
                            "-t",
                            self._number(duration),
                            "-i",
                            "anullsrc=r=48000:cl=stereo",
                        ]
                    )
                    audio_input = input_index
                    input_index += 1

            timing_factor = duration / source_duration
            video_chain = (
                f"[{video_input}:v:0]setpts=(PTS-STARTPTS)*{self._number(timing_factor)},"
                f"scale={self.width}:{self.height}:force_original_aspect_ratio=decrease,"
                f"pad={self.width}:{self.height}:(ow-iw)/2:(oh-ih)/2,"
                # ``concat`` emits AVTB (microsecond) timestamps. Normalize every
                # source to the same time base before joining mixed-FPS footage.
                # ``concat`` 输出 AVTB（微秒）时间基；混合帧率素材拼接前统一时间基。
                f"fps={self._number(fps)},settb=AVTB,format=yuv420p"
                f"{self._technical_color_filter(clip)}"
                f"{self._color_match_filter(clip)}"
                f"{self._color_filter(clip.color_look)}"
                f"{self._creative_grade_filter(clip)}"
                f"{self._motion_filter(clip.motion, fps)}[v{index}]"
            )
            filters.append(video_chain)
            video_labels.append(f"v{index}")
            if audio_input is not None:
                audio_chain = (
                    f"[{audio_input}:a:0]atrim=0:{self._number(duration)},"
                    "asetpts=PTS-STARTPTS,aformat=sample_rates=48000:channel_layouts=stereo"
                    f"{self._audio_filter(clip.audio_cleanup)}"
                    f",volume={self._number(clip.volume_db)}dB[a{index}]"
                )
                filters.append(audio_chain)
                audio_labels.append(f"a{index}")

        current_video = video_labels[0]
        current_audio = audio_labels[0] if audio_labels else ""
        current_duration = durations[0]
        deferred_transitions = any(
            clip.transition_to_next != "cut" for clip in clips[:-1]
        )
        if deferred_transitions:
            self.logger.warning(
                "Resolve 交付尚无经验证的逐帧转场 API；预览按统一硬切渲染，避免转场后音画错位。"
                " / Planned transitions are deferred until a frame-accurate Resolve "
                "transition path is available; review uses canonical hard cuts."
            )
        for index in range(1, len(clips)):
            next_video = f"vx{index}"
            next_audio = f"ax{index}"
            filters.append(
                f"[{current_video}][{video_labels[index]}]"
                f"concat=n=2:v=1:a=0[{next_video}]"
            )
            if not use_program_audio:
                filters.append(
                    f"[{current_audio}][{audio_labels[index]}]"
                    f"concat=n=2:v=0:a=1[{next_audio}]"
                )
            current_video = next_video
            if not use_program_audio:
                current_audio = next_audio
            current_duration += durations[index]

        graphics_items = (
            self.graphics_plan.get("items", [])
            if isinstance(self.graphics_plan.get("items"), list)
            else []
        )
        for graphic_index, raw_graphic in enumerate(graphics_items):
            if not isinstance(raw_graphic, dict):
                continue
            graphic_filter = self._graphics_filter(
                raw_graphic, current_video, f"vg{graphic_index}"
            )
            if graphic_filter:
                filters.append(graphic_filter)
                current_video = f"vg{graphic_index}"

        if use_program_audio:
            command.extend(["-i", str(program_audio_path)])
            program_input = input_index
            input_index += 1
            filters.append(
                f"[{program_input}:a:0]atrim=0:{self._number(current_duration)},"
                "asetpts=PTS-STARTPTS,aformat=sample_rates=48000:channel_layouts=stereo[programbed]"
            )
            final_audio = "programbed"
        else:
            final_audio = current_audio
        bed_path_text = str(self.music_plan.get("bed_file") or "").strip()
        music_path_text = bed_path_text or str(self.music_plan.get("file_name") or "").strip()
        if music_path_text:
            music_path = Path(music_path_text).expanduser().resolve()
            if not music_path.is_file():
                raise ReviewRenderError(
                    f"找不到导演选择的配乐 / Selected music not found: {music_path}"
                )
            if bed_path_text:
                command.extend(["-i", str(music_path)])
            else:
                command.extend(["-stream_loop", "-1", "-i", str(music_path)])
            music_input = input_index
            if bed_path_text:
                filters.append(
                    f"[{music_input}:a:0]atrim=0:{self._number(current_duration)},"
                    "asetpts=PTS-STARTPTS,aformat=sample_rates=48000:channel_layouts=stereo[musicbed]"
                )
                filters.append(
                    f"[{final_audio}][musicbed]amix=inputs=2:duration=first:normalize=0,"
                    "alimiter=limit=0.95[programaudio]"
                )
                final_audio = "programaudio"
            else:
                level = max(-36.0, min(-6.0, self._finite(
                    self.music_plan.get("target_level_db", -20.0),
                    "music_plan.target_level_db",
                )))
                fade_in = max(0.0, min(10.0, self._finite(
                    self.music_plan.get("fade_in_sec", 2.0),
                    "music_plan.fade_in_sec",
                )))
                fade_out = max(0.0, min(10.0, self._finite(
                    self.music_plan.get("fade_out_sec", 3.0),
                    "music_plan.fade_out_sec",
                )))
                fade_out_start = max(0.0, current_duration - fade_out)
                filters.append(
                    f"[{music_input}:a:0]atrim=0:{self._number(current_duration)},"
                    "asetpts=PTS-STARTPTS,aformat=sample_rates=48000:channel_layouts=stereo,"
                    f"volume={self._number(level)}dB,afade=t=in:st=0:d={self._number(fade_in)},"
                    f"afade=t=out:st={self._number(fade_out_start)}:d={self._number(fade_out)}[musicbed]"
                )
                filters.append(
                    f"[{final_audio}][musicbed]amix=inputs=2:duration=first:normalize=0,"
                    "alimiter=limit=0.95[programaudio]"
                )
                final_audio = "programaudio"

        command.extend(
            [
                "-filter_complex_script",
                "__FILTER_SCRIPT__",
                "-map",
                f"[{current_video}]",
                "-map",
                f"[{final_audio}]",
                "-c:v",
                "libx264",
                "-preset",
                "medium",
                "-crf",
                "18",
                "-c:a",
                "aac",
                "-b:a",
                "192k",
                "-movflags",
                "+faststart",
                "-progress",
                "pipe:1",
                "-nostats",
                str(self.output_path),
            ]
        )
        return command, ";\n".join(filters), current_duration

    def _run_ffmpeg(self, command: Sequence[str], total_duration: float) -> None:
        """Run FFmpeg and report bounded percentage progress. / 运行 FFmpeg 并报告进度。"""
        self.logger.info(
            "开始渲染 %.1f 分钟预览 / Rendering %.1f-minute review",
            total_duration / 60.0,
            total_duration / 60.0,
        )
        try:
            process = subprocess.Popen(
                list(command),
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="replace",
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        except OSError as exc:
            raise ReviewRenderError(
                f"无法启动 FFmpeg / Could not start FFmpeg: {exc}"
            ) from exc
        assert process.stdout is not None
        last_percent = -10
        tail: List[str] = []
        for raw_line in process.stdout:
            line = raw_line.strip()
            tail.append(line)
            tail = tail[-60:]
            if line.startswith(("out_time_us=", "out_time_ms=")):
                try:
                    seconds = int(line.split("=", 1)[1]) / 1_000_000.0
                except ValueError:
                    continue
                percent = min(99, int(seconds / max(total_duration, 0.1) * 100))
                if percent >= last_percent + 5:
                    last_percent = percent
                    self.logger.info(
                        "预览渲染 %d%% / Review render %d%%", percent, percent
                    )
        process.stdout.close()
        return_code = process.wait()
        if return_code != 0:
            raise ReviewRenderError(
                "FFmpeg 预览渲染失败 / FFmpeg review render failed:\n"
                + "\n".join(tail[-25:])
            )

    @staticmethod
    def _has_audio(ffprobe: str, path: Path) -> bool:
        """Return whether a media file exposes an audio stream. / 判断媒体是否包含音轨。"""
        completed = subprocess.run(
            [
                ffprobe,
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
            timeout=30,
            check=False,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        return completed.returncode == 0 and bool(completed.stdout.strip())

    @staticmethod
    def _audio_filter(level: str) -> str:
        return {
            "none": ",aresample=48000",
            "light": ",highpass=f=70,lowpass=f=17000,afftdn=nr=10:nf=-35",
            "strong": ",highpass=f=90,lowpass=f=15000,afftdn=nr=18:nf=-30",
        }[level]

    @staticmethod
    def _color_filter(look: str) -> str:
        return {
            "source": "",
            "neutral": ",eq=contrast=1.02:saturation=1.03",
            "warm": ",colorbalance=rs=.035:bs=-.025,eq=saturation=1.05",
            "cool": ",colorbalance=rs=-.02:bs=.035,eq=saturation=1.03",
            "contrast": ",eq=contrast=1.10:brightness=-.01:saturation=1.06",
        }[look]

    def _technical_color_filter(self, clip: Optional[RenderClip] = None) -> str:
        """Return a source-specific S-Log3 preview transform. / 返回逐素材 S-Log3 预览变换。"""
        if self.technical_lut_path is None:
            return ""
        if clip is not None and isinstance(clip.source_color, dict):
            gamma = str(clip.source_color.get("resolve_input_gamma") or "").casefold()
            if gamma and gamma != "s-log3":
                # S-Log2 is intentionally left to Resolve's verified native RCM;
                # applying an S-Log3 LUT would be visibly wrong.
                return ""
        escaped = self.technical_lut_path.as_posix().replace("\\", "/")
        escaped = escaped.replace(":", "\\:").replace("'", "\\'")
        return f",lut3d=file='{escaped}':interp=tetrahedral"

    def _color_match_filter(self, clip: RenderClip) -> str:
        """Return bounded per-source exposure/WB matching for FFmpeg preview. / 返回预览用受限曝光/白平衡匹配。"""
        value = clip.color_match or {}
        if not isinstance(value, dict) or not value:
            return ""
        if str(value.get("analysis_domain") or "") != "display_referred":
            return ""
        try:
            exposure = max(-1.5, min(1.5, float(value.get("exposure_ev", 0))))
            raw = value.get("rgb_gain", [1.0, 1.0, 1.0])
            gains = [max(0.667, min(1.5, float(raw[index]))) for index in range(3)]
        except (IndexError, TypeError, ValueError):
            return ""
        return (
            ",colorchannelmixer="
            f"rr={self._number(gains[0])}:gg={self._number(gains[1])}:bb={self._number(gains[2])}"
            f",exposure=exposure={self._number(exposure)}"
        )

    def _creative_grade_filter(self, clip: RenderClip) -> str:
        """
        Translate the director's color bible into bounded FFmpeg filters.
        将导演调色圣经转换为受限的 FFmpeg 创意调色滤镜。

        Technical log conversion and source matching are intentionally applied
        before this creative layer. / 技术还原与素材匹配始终先于本创意层。
        """
        value = clip.creative_grade or {}
        if not isinstance(value, dict) or not value:
            return ""
        try:
            exposure = max(-0.5, min(0.5, float(value.get("exposure_ev", 0))))
            contrast = max(0.8, min(1.35, float(value.get("contrast", 1))))
            saturation = max(0.65, min(1.35, float(value.get("saturation", 1))))
            warmth = max(-1.0, min(1.0, float(value.get("warmth", 0))))
        except (TypeError, ValueError):
            return ""
        palette = str(value.get("palette") or "natural").casefold()
        palette_balance = {
            "natural": (0.0, 0.0, 0.0, 0.0),
            "teal_amber": (-0.012, 0.022, 0.028, -0.018),
            "cool_moonlight": (-0.025, 0.040, -0.010, 0.018),
            "warm_memory": (0.035, -0.030, 0.018, -0.012),
            "desaturated_grit": (-0.008, 0.010, 0.008, -0.006),
            "neon_night": (0.010, 0.035, 0.020, 0.018),
        }.get(palette, (0.0, 0.0, 0.0, 0.0))
        shadow_red, shadow_blue, highlight_red, highlight_blue = palette_balance
        shadow_red += 0.035 * warmth
        shadow_blue -= 0.035 * warmth
        highlight_red += 0.025 * warmth
        highlight_blue -= 0.025 * warmth
        return (
            ",colorbalance="
            f"rs={self._number(shadow_red)}:bs={self._number(shadow_blue)}:"
            f"rh={self._number(highlight_red)}:bh={self._number(highlight_blue)}"
            f",eq=contrast={self._number(contrast)}:saturation={self._number(saturation)}"
            f",exposure=exposure={self._number(exposure)}"
        )

    @staticmethod
    def _escape_drawtext(value: object) -> str:
        """Escape one UTF-8 string for FFmpeg's drawtext option parser. / 转义 drawtext UTF-8 文本。"""
        return (
            str(value or "")
            .replace("\\", "\\\\")
            .replace("'", "\\'")
            .replace(":", "\\:")
            .replace("%", "\\%")
        )

    def _graphics_filter(
        self,
        graphic: Dict[str, Any],
        input_label: str,
        output_label: str,
        *,
        transparent_canvas: bool = False,
    ) -> str:
        """
        Build one story-motivated title/chapter overlay for the review render.
        为审片成片构建一个由故事驱动的标题或章节字卡。

        Parameters / 参数:
            graphic: Validated graphics-plan item. / 已校验的字卡条目。
            input_label/output_label: FFmpeg graph labels. / FFmpeg 图标签。
        """
        try:
            start = max(0.0, float(graphic.get("timeline_in_sec", 0)))
            end = max(start + 0.2, float(graphic.get("timeline_out_sec", start + 2.5)))
        except (TypeError, ValueError):
            return ""
        text = " ".join(str(graphic.get("text") or "").split())
        if not text:
            return ""
        subtitle = " ".join(str(graphic.get("subtitle") or "").split())
        kind = str(graphic.get("kind") or "chapter").casefold()
        style = str(graphic.get("style") or "minimal").casefold()
        font_candidates = (
            Path(os.environ.get("WINDIR", "C:/Windows")) / "Fonts" / "msyh.ttc",
            Path(os.environ.get("WINDIR", "C:/Windows")) / "Fonts" / "segoeuib.ttf",
        )
        font_path = next((path for path in font_candidates if path.is_file()), None)
        if font_path is not None:
            escaped_font = font_path.as_posix().replace(":", "\\:").replace("'", "\\'")
            font_option = f"fontfile='{escaped_font}'"
        else:
            font_option = "font='Segoe UI'"
        enable = f"between(t,{self._number(start)},{self._number(end)})"
        fade = min(0.35, max(0.12, (end - start) / 4.0))
        fade_out_start = max(start + fade, end - fade)
        alpha = (
            f"if(lt(t,{self._number(start + fade)}),(t-{self._number(start)})/{self._number(fade)},"
            f"if(lt(t,{self._number(fade_out_start)}),1,"
            f"( {self._number(end)}-t)/{self._number(fade)}))"
        ).replace(" ", "")
        centered = kind in {"title_card", "end_card"}
        layout_scale = max(0.5, float(self.height) / 1080.0)
        main_size = int(round((72 if centered else 48) * layout_scale))
        sub_size = int(round((32 if centered else 26) * layout_scale))
        left_main = int(round(100 * layout_scale))
        left_sub = int(round(104 * layout_scale))
        lower_main = int(round(220 * layout_scale))
        lower_sub = int(round(152 * layout_scale))
        lower_box = int(round(270 * layout_scale))
        lower_box_height = int(round(190 * layout_scale))
        main_x = "(w-text_w)/2" if centered else str(left_main)
        main_y = "h*0.40" if centered else "h-220"
        if not centered:
            main_y = f"h-{lower_main}"
        sub_x = "(w-text_w)/2" if centered else str(left_sub)
        sub_y = "h*0.56" if centered else f"h-{lower_sub}"
        box_y = "ih*0.30" if centered else f"ih-{lower_box}"
        box_h = "ih*0.38" if centered else str(lower_box_height)
        box_alpha = "0.42" if style in {"bold_cinematic", "kinetic"} else "0.28"
        accent = "0x35d0ba" if style == "kinetic" else "white"
        replace = ":replace=1" if transparent_canvas else ""
        chain = (
            f"[{input_label}]drawbox=x=0:y={box_y}:w=iw:h={box_h}:"
            f"color=black@{box_alpha}:t=fill{replace}:enable='{enable}',"
            f"drawtext={font_option}:text='{self._escape_drawtext(text)}':"
            f"fontcolor={accent}:fontsize={main_size}:x={main_x}:y={main_y}:"
            f"borderw=1:bordercolor=black@0.7:alpha='{alpha}':enable='{enable}'"
        )
        if subtitle:
            chain += (
                f",drawtext={font_option}:text='{self._escape_drawtext(subtitle)}':"
                f"fontcolor=white@0.92:fontsize={sub_size}:x={sub_x}:y={sub_y}:"
                f"borderw=1:bordercolor=black@0.7:alpha='{alpha}':enable='{enable}'"
            )
        return chain + f"[{output_label}]"

    def _motion_filter(self, motion: str, fps: float) -> str:
        if motion != "gentle_push_in":
            return ""
        return (
            ",zoompan=z='min(zoom+0.00035,1.06)':"
            "x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)':"
            f"d=1:s={self.width}x{self.height}:fps={self._number(fps)}"
        )

    @staticmethod
    def _finite(value: object, label: str) -> float:
        try:
            number = float(value)
        except (TypeError, ValueError) as exc:
            raise ReviewRenderError(f"{label} 不是数字 / is not numeric.") from exc
        if not math.isfinite(number):
            raise ReviewRenderError(f"{label} 必须是有限数 / must be finite.")
        return number

    @staticmethod
    def _choice(value: object, allowed: set, default: str) -> str:
        text = str(value or default).strip().casefold()
        return text if text in allowed else default

    @staticmethod
    def _number(value: float) -> str:
        return f"{float(value):.6f}".rstrip("0").rstrip(".")


def configure_logging(level: str = "INFO") -> logging.Logger:
    """Configure standalone preview logging. / 配置独立预览日志。"""
    logger = logging.getLogger(LOGGER_NAME)
    logger.handlers.clear()
    logger.propagate = False
    logger.setLevel(getattr(logging, level.upper()))
    handler = logging.StreamHandler()
    handler.setFormatter(
        logging.Formatter("%(asctime)s | %(levelname)s | %(name)s | %(message)s")
    )
    logger.addHandler(handler)
    return logger


def build_parser() -> argparse.ArgumentParser:
    """Create preview-render CLI arguments. / 创建预览渲染命令行参数。"""
    parser = argparse.ArgumentParser(
        description="Render an AI edit-plan preview with FFmpeg."
    )
    parser.add_argument("--json", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--width", type=int, default=1920)
    parser.add_argument("--height", type=int, default=1080)
    parser.add_argument(
        "--log-level",
        default="INFO",
        choices=("DEBUG", "INFO", "WARNING", "ERROR"),
    )
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    """Run the review renderer with friendly errors. / 运行预览渲染并友好处理错误。"""
    args = build_parser().parse_args(argv)
    logger = configure_logging(args.log_level)
    try:
        ReviewRenderer(
            args.json,
            args.output,
            width=args.width,
            height=args.height,
            logger=logger,
        ).run()
        return 0
    except ReviewRenderError as exc:
        logger.error("%s", exc)
        return 2
    except KeyboardInterrupt:
        logger.warning("用户中断预览渲染 / Review render interrupted.")
        return 130
    except Exception:
        logger.exception("未预期预览渲染错误 / Unexpected review render error.")
        return 1


if __name__ == "__main__":
    sys.exit(main())
