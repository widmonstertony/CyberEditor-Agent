#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Local transcript and visual-keyframe extraction.
本地台词与视觉关键帧提取。

Heavy dependencies are imported lazily so importing this module never allocates
GPU memory. The workflow orchestrator runs this module in its own process; when
the process exits, Windows reclaims its CUDA/DirectML allocations before the
director stage starts.

重型依赖采用延迟导入，因此仅导入本模块不会占用显存。工作流调度器会在独立
进程中运行本模块；进程退出后，Windows 会先回收 CUDA/DirectML 资源，再启动
AI 导演阶段。
"""

import argparse
from datetime import datetime, timezone
import gc
import json
import logging
import os
from pathlib import Path
import shutil
import subprocess
import sys
from typing import Any, Dict, List, Optional, Sequence, Tuple

try:
    from .color_analysis import analyze_bgr_frame, summarize_color_samples
    from .sony_metadata import SonyMetadataError, detect_sony_color_metadata
except ImportError:  # pragma: no cover - direct ``python src/...`` fallback.
    from color_analysis import analyze_bgr_frame, summarize_color_samples
    from sony_metadata import SonyMetadataError, detect_sony_color_metadata


LOGGER_NAME = "cybereditor.extractor"


class ExtractionError(RuntimeError):
    """Expected extraction failure. / 可预期的数据提取错误。"""


class MediaExtractor:
    """
    Extract time-coded speech and representative frames from a video.
    从视频中提取带时间戳的台词和代表性画面。

    Parameters / 参数:
        whisper_model:
            OpenAI Whisper model name or local checkpoint path.
            OpenAI Whisper 模型名称或本地检查点路径。
        device:
            ``auto``, ``cpu``, ``cuda``, or another PyTorch device string.
            ``auto``、``cpu``、``cuda`` 或其他 PyTorch 设备字符串。
        language:
            Optional Whisper language code; ``None`` enables auto detection.
            可选 Whisper 语言代码；``None`` 表示自动检测。
        scene_threshold:
            Normalized grayscale difference required to save a scene keyframe.
            保存场景关键帧所需的归一化灰度差异阈值。
        sample_interval_sec:
            Interval between visual samples.
            视觉采样间隔（秒）。
        min_keyframe_gap_sec:
            Minimum distance between saved frames.
            保存的关键帧之间的最小时间间隔（秒）。
        max_keyframes:
            Hard cap protecting disk space on long videos.
            长视频关键帧数量上限，用于保护磁盘空间。
    """

    def __init__(
        self,
        whisper_model: str = "small",
        device: str = "auto",
        language: Optional[str] = None,
        scene_threshold: float = 0.28,
        sample_interval_sec: float = 2.0,
        min_keyframe_gap_sec: float = 5.0,
        max_keyframes: int = 240,
        logger: Optional[logging.Logger] = None,
    ) -> None:
        """Initialize extraction policy without loading a model. / 初始化提取策略，但不加载模型。"""
        if not whisper_model.strip():
            raise ExtractionError("Whisper 模型名不能为空 / Whisper model is required.")
        if device not in {"auto", "cpu", "cuda"} and not device.startswith("cuda:"):
            raise ExtractionError(
                f"不支持的设备字符串 / Unsupported device string: {device}"
            )
        if not 0.0 < scene_threshold <= 1.0:
            raise ExtractionError(
                "scene_threshold 必须在 (0, 1] / must be in (0, 1]."
            )
        if sample_interval_sec <= 0 or min_keyframe_gap_sec < 0:
            raise ExtractionError(
                "抽帧时间参数无效 / Invalid keyframe timing parameters."
            )
        if max_keyframes <= 0:
            raise ExtractionError(
                "max_keyframes 必须大于 0 / must be greater than zero."
            )

        self.whisper_model = whisper_model.strip()
        self.device = device
        self.language = language
        self.scene_threshold = scene_threshold
        self.sample_interval_sec = sample_interval_sec
        self.min_keyframe_gap_sec = min_keyframe_gap_sec
        self.max_keyframes = max_keyframes
        self.logger = logger or logging.getLogger(LOGGER_NAME)

    def run(
        self,
        video_path: os.PathLike,
        raw_data_path: os.PathLike,
        srt_path: os.PathLike,
        keyframes_dir: os.PathLike,
        proxy_file_name: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        Execute transcription, release Whisper, then extract visual keyframes.
        执行语音转写、释放 Whisper，然后提取视觉关键帧。

        Parameters / 参数:
            video_path:
                Source video or audio/video container.
                源视频或音视频容器路径。
            raw_data_path:
                Destination for the normalized ``raw_data.json``.
                规范化 ``raw_data.json`` 的输出路径。
            srt_path:
                Destination for the human-readable subtitle file.
                便于人工检查的字幕文件输出路径。
            keyframes_dir:
                Directory receiving JPEG keyframes.
                JPEG 关键帧输出目录。
            proxy_file_name:
                Resolve proxy path/name carried into downstream metadata.
                传递到下游的 Resolve 代理素材路径或名称。

        Returns / 返回:
            The same JSON object written to ``raw_data_path``.
            写入 ``raw_data_path`` 的同一 JSON 对象。
        """
        source = Path(video_path).expanduser().resolve()
        if not source.is_file():
            raise ExtractionError(
                f"找不到输入媒体 / Input media not found: {source}"
            )
        if shutil.which("ffmpeg") is None:
            raise ExtractionError(
                "未找到 ffmpeg。Whisper 需要 FFmpeg 读取音视频。\n"
                "ffmpeg was not found on PATH. Whisper requires FFmpeg to read media."
            )

        raw_destination = Path(raw_data_path).expanduser().resolve()
        srt_destination = Path(srt_path).expanduser().resolve()
        frames_destination = Path(keyframes_dir).expanduser().resolve()
        raw_destination.parent.mkdir(parents=True, exist_ok=True)
        srt_destination.parent.mkdir(parents=True, exist_ok=True)
        frames_destination.mkdir(parents=True, exist_ok=True)

        if self.has_audio_stream(source):
            self.logger.info(
                "阶段 1/2：Whisper 转写开始，模型=%s / Stage 1/2: Whisper transcription started, model=%s",
                self.whisper_model,
                self.whisper_model,
            )
            transcription = self.transcribe(source)
        else:
            self.logger.info(
                "素材没有音轨，跳过 Whisper；将作为纯画面素材分析"
                " / No audio stream; skipping Whisper and analyzing visuals only"
            )
            transcription = {"language": None, "segments": []}
        segments = transcription["segments"]
        self.write_srt(segments, srt_destination)
        self.logger.info(
            "Whisper 已卸载并清理缓存；开始 CPU 抽帧 / Whisper unloaded and cache cleared; starting CPU keyframes"
        )

        try:
            source_color = detect_sony_color_metadata(source)
        except SonyMetadataError as exc:
            self.logger.warning(
                "Sony XML 解析失败，将标记色彩配置为未知：%s / "
                "Sony XML parsing failed; source color remains unknown: %s",
                exc,
                exc,
            )
            source_color = {
                "source": "invalid",
                "camera_profile": "unknown",
                "transform_supported": False,
                "confidence": 0.0,
            }
        keyframes, video_metadata = self.extract_keyframes(
            source, frames_destination, source_color=source_color
        )
        color_analysis = summarize_color_samples(
            item.get("color_metrics", {})
            for item in keyframes
            if isinstance(item, dict)
        )
        duration = max(
            float(video_metadata.get("duration_sec", 0.0)),
            max((float(item["end_sec"]) for item in segments), default=0.0),
        )

        proxy_value = proxy_file_name or source.name
        payload: Dict[str, Any] = {
            "schema_version": "1.0",
            "generated_at_utc": datetime.now(timezone.utc).isoformat(),
            "source_video": str(source),
            "proxy_file_name": proxy_value,
            "duration_sec": round(duration, 3),
            "language": transcription.get("language"),
            "whisper_model": self.whisper_model,
            "video": video_metadata,
            "source_color": source_color,
            "color_analysis": color_analysis,
            "transcript": segments,
            "keyframes": keyframes,
        }
        self._atomic_write_json(payload, raw_destination)
        self.logger.info(
            "提取完成：%d 条台词、%d 个关键帧 -> %s / Extraction complete: %d segments, %d keyframes -> %s",
            len(segments),
            len(keyframes),
            raw_destination,
            len(segments),
            len(keyframes),
            raw_destination,
        )
        return payload

    def transcribe(self, media_path: Path) -> Dict[str, Any]:
        """
        Load Whisper lazily, transcribe a media path, and release GPU memory.
        延迟加载 Whisper、转写媒体路径，并释放 GPU 显存。

        Model destruction occurs in ``finally`` so failed transcription does
        not keep the model resident until normal process shutdown.

        模型销毁位于 ``finally`` 中，因此即使转写失败，也不会让模型一直驻留
        到进程正常结束。
        """
        try:
            import torch
            import whisper
        except ImportError as exc:
            raise ExtractionError(
                "缺少 openai-whisper/PyTorch。请执行 pip install -r requirements.txt。\n"
                "openai-whisper/PyTorch is missing. Run pip install -r requirements.txt."
            ) from exc

        selected_device = self._select_device(torch)
        self.logger.info(
            "正在加载 Whisper %s 到 %s / Loading Whisper %s on %s",
            self.whisper_model,
            selected_device,
            self.whisper_model,
            selected_device,
        )
        model = None
        try:
            model = whisper.load_model(self.whisper_model, device=selected_device)
            options: Dict[str, Any] = {
                "verbose": False,
                "fp16": selected_device.startswith("cuda"),
                "word_timestamps": False,
                "condition_on_previous_text": True,
            }
            if self.language:
                options["language"] = self.language
            result = model.transcribe(str(media_path), **options)
            return {
                "language": result.get("language"),
                "segments": self._normalize_segments(result.get("segments", [])),
            }
        except Exception as exc:
            raise ExtractionError(
                f"Whisper 转写失败 / Whisper transcription failed: {exc}"
            ) from exc
        finally:
            if model is not None:
                del model
            gc.collect()
            try:
                if torch.cuda.is_available():
                    torch.cuda.empty_cache()
                    # ipc_collect may be unavailable on some torch builds.
                    if hasattr(torch.cuda, "ipc_collect"):
                        torch.cuda.ipc_collect()
            except Exception as exc:
                self.logger.warning(
                    "PyTorch CUDA 缓存清理返回警告：%s / CUDA cache cleanup warning: %s",
                    exc,
                    exc,
                )

    def extract_keyframes(
        self,
        video_path: Path,
        output_dir: Path,
        source_color: Optional[Dict[str, Any]] = None,
    ) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
        """
        Sample a video with OpenCV and save scene-change keyframes.
        使用 OpenCV 采样视频并保存场景变化关键帧。

        This stage also records bounded CPU-only exposure/white-balance
        statistics. It intentionally avoids a second neural model so the
        extraction process remains usable on integrated graphics.

        本阶段同时记录受限的纯 CPU 曝光/白平衡统计；有意不加载第二个神经网络，
        使核显设备也能使用。

        Parameters / 参数:
            video_path: Source video. / 源视频。
            output_dir: JPEG keyframe directory. / JPEG 关键帧目录。
            source_color: Parsed Sony input profile, when available.
                已解析的 Sony 输入色彩配置（如存在）。
        """
        try:
            import cv2
        except ImportError as exc:
            raise ExtractionError(
                "缺少 opencv-python。请执行 pip install -r requirements.txt。\n"
                "opencv-python is missing. Run pip install -r requirements.txt."
            ) from exc

        capture = cv2.VideoCapture(str(video_path))
        if not capture.isOpened():
            raise ExtractionError(
                f"OpenCV 无法打开视频 / OpenCV cannot open video: {video_path}"
            )

        try:
            fps = float(capture.get(cv2.CAP_PROP_FPS) or 0.0)
            frame_count = int(capture.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
            width = int(capture.get(cv2.CAP_PROP_FRAME_WIDTH) or 0)
            height = int(capture.get(cv2.CAP_PROP_FRAME_HEIGHT) or 0)
            duration = frame_count / fps if fps > 0 and frame_count > 0 else 0.0
            if duration <= 0:
                raise ExtractionError(
                    "无法读取视频时长/FPS / Could not determine video duration/FPS."
                )

            keyframes: List[Dict[str, Any]] = []
            previous_gray = None
            last_saved_at = -self.min_keyframe_gap_sec
            timestamp = 0.0
            coverage_gap = duration / max(1, self.max_keyframes - 1)
            effective_min_gap = max(
                self.min_keyframe_gap_sec, coverage_gap
            )

            while timestamp <= duration and len(keyframes) < self.max_keyframes:
                capture.set(cv2.CAP_PROP_POS_MSEC, timestamp * 1000.0)
                ok, frame = capture.read()
                if not ok or frame is None:
                    timestamp += self.sample_interval_sec
                    continue

                gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
                # Fixed small comparison surface keeps CPU work predictable.
                gray = cv2.resize(gray, (320, 180), interpolation=cv2.INTER_AREA)
                difference = (
                    1.0
                    if previous_gray is None
                    else float(cv2.mean(cv2.absdiff(gray, previous_gray))[0])
                    / 255.0
                )
                # Scene changes capture visual events; a periodic 30-second
                # floor also guarantees coverage of static interviews and
                # long takes whose pixels do not change enough to cross the
                # threshold. The director later selects a bounded, evenly
                # distributed subset for each 10–15 minute request.
                since_last = timestamp - last_saved_at
                periodic_due = since_last >= max(
                    30.0, effective_min_gap
                )
                should_save = (
                    previous_gray is None
                    or periodic_due
                    or (
                        difference >= self.scene_threshold
                        and since_last >= effective_min_gap
                    )
                )
                if should_save:
                    file_name = "keyframe_{:04d}_{:010.3f}.jpg".format(
                        len(keyframes) + 1, timestamp
                    )
                    destination = output_dir / file_name
                    preview = frame
                    if frame.shape[1] > 1280:
                        scale = 1280.0 / float(frame.shape[1])
                        preview = cv2.resize(
                            frame,
                            (1280, max(1, int(frame.shape[0] * scale))),
                            interpolation=cv2.INTER_AREA,
                        )
                    self._write_jpeg_unicode_safe(cv2, preview, destination)
                    try:
                        color_metrics = analyze_bgr_frame(preview, cv2)
                    except (RuntimeError, ValueError) as exc:
                        self.logger.warning(
                            "关键帧色彩统计失败 %.3fs：%s / "
                            "Keyframe color analysis failed at %.3fs: %s",
                            timestamp,
                            exc,
                            timestamp,
                            exc,
                        )
                        color_metrics = {}
                    keyframes.append(
                        {
                            "timestamp_sec": round(timestamp, 3),
                            "file_name": file_name,
                            "scene_score": round(difference, 4),
                            "color_metrics": color_metrics,
                        }
                    )
                    last_saved_at = timestamp
                previous_gray = gray
                timestamp += self.sample_interval_sec

            metadata = {
                "fps": round(fps, 6),
                "frame_count": frame_count,
                "width": width,
                "height": height,
                "duration_sec": round(duration, 3),
                "source_color_profile": str(
                    (source_color or {}).get("camera_profile") or "unknown"
                ),
            }
            return keyframes, metadata
        finally:
            capture.release()

    @staticmethod
    def write_srt(segments: Sequence[Dict[str, Any]], destination: Path) -> None:
        """
        Write normalized transcript segments as UTF-8 SRT.
        将规范化台词片段写为 UTF-8 SRT。
        """
        lines: List[str] = []
        for index, segment in enumerate(segments, start=1):
            lines.extend(
                [
                    str(index),
                    "{} --> {}".format(
                        MediaExtractor.format_srt_timestamp(
                            float(segment["start_sec"])
                        ),
                        MediaExtractor.format_srt_timestamp(
                            float(segment["end_sec"])
                        ),
                    ),
                    str(segment["text"]).strip(),
                    "",
                ]
            )
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text("\n".join(lines), encoding="utf-8")

    @staticmethod
    def has_audio_stream(media_path: Path) -> bool:
        """
        Detect an audio stream with ffprobe before loading Whisper.
        在加载 Whisper 前使用 ffprobe 检测音轨。

        Silent B-roll is valid project media and must not fail the complete
        batch merely because Whisper has nothing to decode.
        无声 B-roll 是合法素材，不能因为 Whisper 无音频可解码而导致整批失败。
        """
        ffprobe = shutil.which("ffprobe")
        if not ffprobe:
            # FFmpeg distributions normally ship both tools. If only ffmpeg is
            # present, preserve the historical behavior and let Whisper report
            # a detailed decode error.
            return True
        try:
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
                    str(media_path),
                ],
                capture_output=True,
                text=True,
                timeout=30,
                check=False,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        except (OSError, subprocess.SubprocessError):
            return True
        return completed.returncode == 0 and bool(completed.stdout.strip())

    @staticmethod
    def format_srt_timestamp(seconds: float) -> str:
        """
        Convert seconds to ``HH:MM:SS,mmm`` without float carry errors.
        将秒转换为 ``HH:MM:SS,mmm``，并正确处理浮点进位。
        """
        total_ms = max(0, int(round(seconds * 1000.0)))
        hours, remainder = divmod(total_ms, 3_600_000)
        minutes, remainder = divmod(remainder, 60_000)
        secs, milliseconds = divmod(remainder, 1000)
        return "{:02d}:{:02d}:{:02d},{:03d}".format(
            hours, minutes, secs, milliseconds
        )

    def _select_device(self, torch_module: Any) -> str:
        """Resolve ``auto`` to CUDA or CPU. / 将 ``auto`` 解析为 CUDA 或 CPU。"""
        if self.device != "auto":
            if self.device.startswith("cuda") and not torch_module.cuda.is_available():
                raise ExtractionError(
                    "指定了 CUDA，但 PyTorch 未检测到可用 CUDA 设备。"
                    " / CUDA was requested but PyTorch found no CUDA device."
                )
            return self.device
        return "cuda" if torch_module.cuda.is_available() else "cpu"

    @staticmethod
    def _normalize_segments(raw_segments: Any) -> List[Dict[str, Any]]:
        """Validate and normalize Whisper segment dictionaries. / 校验并规范化 Whisper 片段字典。"""
        if not isinstance(raw_segments, list):
            raise ExtractionError(
                "Whisper 未返回 segments 数组 / Whisper returned no segment list."
            )
        normalized: List[Dict[str, Any]] = []
        for index, segment in enumerate(raw_segments):
            if not isinstance(segment, dict):
                continue
            try:
                start = max(0.0, float(segment.get("start", 0.0)))
                end = float(segment.get("end", start))
            except (TypeError, ValueError):
                continue
            text = " ".join(str(segment.get("text", "")).split())
            if end <= start or not text:
                continue
            normalized.append(
                {
                    "id": int(segment.get("id", index)),
                    "start_sec": round(start, 3),
                    "end_sec": round(end, 3),
                    "text": text,
                }
            )
        if not normalized:
            raise ExtractionError(
                "Whisper 没有生成有效台词。请检查音轨、语言或模型。"
                " / Whisper produced no valid speech segments."
            )
        return normalized

    @staticmethod
    def _write_jpeg_unicode_safe(
        cv2_module: Any, frame: Any, destination: Path
    ) -> None:
        """Encode then write JPEG to support non-ASCII Windows paths. / 编码后写入 JPEG，以支持 Windows 非 ASCII 路径。"""
        ok, encoded = cv2_module.imencode(
            ".jpg", frame, [int(cv2_module.IMWRITE_JPEG_QUALITY), 88]
        )
        if not ok:
            raise ExtractionError(
                f"关键帧 JPEG 编码失败 / JPEG encoding failed: {destination}"
            )
        try:
            encoded.tofile(str(destination))
        except OSError as exc:
            raise ExtractionError(
                f"无法写入关键帧 / Cannot write keyframe: {destination} ({exc})"
            ) from exc

    @staticmethod
    def _atomic_write_json(payload: Dict[str, Any], destination: Path) -> None:
        """Atomically replace a UTF-8 JSON file. / 原子替换 UTF-8 JSON 文件。"""
        temporary = destination.with_suffix(destination.suffix + ".tmp")
        try:
            temporary.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            os.replace(str(temporary), str(destination))
        except OSError as exc:
            raise ExtractionError(
                f"无法写入 JSON / Cannot write JSON: {destination} ({exc})"
            ) from exc


def configure_logging(level: str = "INFO") -> logging.Logger:
    """Configure standalone extraction logging. / 配置独立提取日志。"""
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
    """Create extractor CLI arguments. / 创建提取器命令行参数。"""
    parser = argparse.ArgumentParser(
        description="Extract Whisper transcript and OpenCV keyframes. / 提取台词与关键帧。"
    )
    parser.add_argument("--video", required=True, help="输入媒体 / input media")
    parser.add_argument("--raw-data", required=True, help="raw_data.json 输出路径")
    parser.add_argument("--srt", required=True, help="SRT 输出路径")
    parser.add_argument("--keyframes-dir", required=True, help="关键帧目录")
    parser.add_argument("--proxy-file-name", help="Resolve 代理素材路径或名称")
    parser.add_argument("--whisper-model", default="small")
    parser.add_argument("--device", default="auto")
    parser.add_argument("--language")
    parser.add_argument("--scene-threshold", type=float, default=0.28)
    parser.add_argument("--sample-interval", type=float, default=2.0)
    parser.add_argument("--min-keyframe-gap", type=float, default=5.0)
    parser.add_argument("--max-keyframes", type=int, default=240)
    parser.add_argument(
        "--log-level",
        default="INFO",
        choices=("DEBUG", "INFO", "WARNING", "ERROR"),
    )
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    """Run extractor CLI with user-friendly errors. / 运行提取器 CLI 并提供友好错误。"""
    args = build_parser().parse_args(argv)
    logger = configure_logging(args.log_level)
    try:
        extractor = MediaExtractor(
            whisper_model=args.whisper_model,
            device=args.device,
            language=args.language,
            scene_threshold=args.scene_threshold,
            sample_interval_sec=args.sample_interval,
            min_keyframe_gap_sec=args.min_keyframe_gap,
            max_keyframes=args.max_keyframes,
            logger=logger,
        )
        extractor.run(
            args.video,
            args.raw_data,
            args.srt,
            args.keyframes_dir,
            args.proxy_file_name,
        )
        return 0
    except ExtractionError as exc:
        logger.error("%s", exc)
        return 2
    except KeyboardInterrupt:
        logger.warning("用户中断提取 / Extraction interrupted.")
        return 130
    except Exception:
        logger.exception("未预期提取错误 / Unexpected extraction error.")
        return 1


if __name__ == "__main__":
    sys.exit(main())
