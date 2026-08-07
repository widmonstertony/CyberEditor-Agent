#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Blind multimodal review for a rendered CyberEditor rough cut.
对 CyberEditor 已渲染低清粗剪执行隔离上下文的多模态盲审。

The reviewer sees sampled frames in chronological order plus the literal audible
dialogue/music map. It never receives the treatment or editor rationale. This
keeps a fluent model from rewarding its own story explanation instead of the film.

审片模型只看到按时间排列的实际成片帧，以及字面可听对白/音乐表；它不会收到导演
阐述或镜头选择理由，避免模型用自己的解释替代观众真实理解。
"""

from __future__ import annotations

import argparse
import base64
from datetime import datetime, timezone
import json
import logging
from pathlib import Path
import shutil
import subprocess
import tempfile
from typing import Any, Dict, List, Optional, Sequence

from .director import AIDirector, DirectorError


LOGGER_NAME = "cybereditor.rough_cut_reviewer"

VISUAL_BATCH_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "properties": {
        "literal_visual_summary": {"type": "string", "minLength": 1, "maxLength": 1000},
        "observed_actions": {
            "type": "array",
            "items": {"type": "string", "minLength": 1, "maxLength": 350},
            "minItems": 1,
            "maxItems": 12,
        },
        "visible_state_changes": {
            "type": "array",
            "items": {"type": "string", "minLength": 1, "maxLength": 350},
            "maxItems": 8,
        },
        "continuity_observations": {
            "type": "array",
            "items": {"type": "string", "minLength": 1, "maxLength": 350},
            "maxItems": 8,
        },
        "visible_text": {
            "type": "array",
            "items": {"type": "string", "minLength": 1, "maxLength": 200},
            "maxItems": 8,
        },
    },
    "required": [
        "literal_visual_summary", "observed_actions", "visible_state_changes",
        "continuity_observations", "visible_text",
    ],
    "additionalProperties": False,
}

PREVIEW_REVIEW_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "properties": {
        "inferred_form": {
            "type": "string",
            "enum": [
                "causal_story", "dialogue_scene", "character_vignette",
                "mood_montage", "bts_process", "incoherent",
            ],
        },
        "literal_synopsis": {"type": "string", "minLength": 1, "maxLength": 1000},
        "subject": {"type": "string", "minLength": 1, "maxLength": 250},
        "apparent_goal": {"type": "string", "minLength": 1, "maxLength": 350},
        "progression": {
            "type": "array",
            "items": {"type": "string", "minLength": 1, "maxLength": 350},
            "minItems": 1,
            "maxItems": 10,
        },
        "ending": {"type": "string", "minLength": 1, "maxLength": 400},
        "takeaway_guess": {"type": "string", "minLength": 1, "maxLength": 500},
        "coherence_score": {"type": "integer", "minimum": 1, "maximum": 10},
        "causal_clarity_score": {"type": "integer", "minimum": 1, "maximum": 10},
        "visual_payoff_score": {"type": "integer", "minimum": 1, "maximum": 10},
        "pacing_score": {"type": "integer", "minimum": 1, "maximum": 10},
        "audio_story_score": {"type": "integer", "minimum": 1, "maximum": 10},
        "confusing_transitions": {
            "type": "array",
            "items": {"type": "string", "minLength": 1, "maxLength": 350},
            "maxItems": 10,
        },
        "unsupported_or_unresolved_points": {
            "type": "array",
            "items": {"type": "string", "minLength": 1, "maxLength": 350},
            "maxItems": 10,
        },
        "required_changes": {
            "type": "array",
            "items": {"type": "string", "minLength": 1, "maxLength": 500},
            "maxItems": 10,
        },
        "passes": {"type": "boolean"},
        "reason": {"type": "string", "minLength": 1, "maxLength": 800},
    },
    "required": [
        "inferred_form", "literal_synopsis", "subject", "apparent_goal",
        "progression", "ending", "takeaway_guess", "coherence_score",
        "causal_clarity_score", "visual_payoff_score", "pacing_score",
        "audio_story_score", "confusing_transitions",
        "unsupported_or_unresolved_points", "required_changes", "passes", "reason",
    ],
    "additionalProperties": False,
}


class RoughCutReviewer:
    """Review an actual preview before Resolve is allowed to render. / 在 Resolve 前审看真实预览。"""

    def __init__(
        self,
        model: str,
        text_model: Optional[str] = None,
        base_url: str = "http://localhost:11434",
        num_ctx: int = 32768,
        timeout_sec: int = 7200,
        logger: Optional[logging.Logger] = None,
        session: Optional[Any] = None,
    ) -> None:
        """
        Configure the serial visual/text reviewer without loading a model.
        配置串行视觉/文字审片器，但暂不加载模型。

        Parameters / 参数:
            model: Ollama vision model tag. / Ollama 视觉模型标签。
            text_model: Optional final critic model. / 可选的最终文字审片模型。
            base_url: Ollama service root. / Ollama 服务地址。
            num_ctx: Context window for each request. / 每次请求上下文长度。
            timeout_sec: Per-request timeout. / 单次请求超时。
        """
        self.model = model
        self.text_model = text_model or model
        self.logger = logger or logging.getLogger(LOGGER_NAME)
        self.director = AIDirector(
            model=model,
            text_model=self.text_model,
            base_url=base_url,
            chunk_minutes=10.0,
            project_fps=25.0,
            num_ctx=num_ctx,
            timeout_sec=timeout_sec,
            session=session,
            logger=self.logger,
        )

    @staticmethod
    def _load_json(path: Path) -> Dict[str, Any]:
        """Read one UTF-8 JSON object. / 读取一个 UTF-8 JSON 对象。"""
        try:
            payload = json.loads(path.read_text(encoding="utf-8-sig"))
        except (OSError, json.JSONDecodeError) as exc:
            raise DirectorError(f"无法读取 JSON / Cannot read JSON {path}: {exc}") from exc
        if not isinstance(payload, dict):
            raise DirectorError(f"JSON 根节点必须是对象 / JSON root must be an object: {path}")
        return payload

    @staticmethod
    def _program_duration(timeline: Dict[str, Any]) -> float:
        """Return authored picture duration in seconds. / 返回已编排画面总秒数。"""
        total = 0.0
        for clip in timeline.get("clips", []):
            if not isinstance(clip, dict):
                continue
            try:
                total += max(
                    0.0,
                    float(clip.get("cut_out_sec", 0) or 0)
                    - float(clip.get("cut_in_sec", 0) or 0),
                )
            except (TypeError, ValueError):
                continue
        return total

    def _extract_frames(self, preview: Path, destination: Path, duration: float) -> List[Path]:
        """
        Sample the complete preview chronologically with an adaptive frame rate.
        以自适应帧率按时间顺序采样完整预览。

        Parameters / 参数:
            preview: Rendered rough-cut MP4. / 已渲染低清粗剪 MP4。
            destination: Temporary JPEG directory. / 临时 JPEG 目录。
            duration: Authored runtime in seconds. / 成片时长秒数。
        """
        ffmpeg = shutil.which("ffmpeg")
        if not ffmpeg:
            raise DirectorError("未找到 FFmpeg，无法审看低清成片 / FFmpeg is required for preview review.")
        sample_fps = min(2.0, max(0.5, 120.0 / max(1.0, duration)))
        output_pattern = destination / "frame_%05d.jpg"
        command = [
            ffmpeg, "-hide_banner", "-loglevel", "error", "-y", "-i", str(preview),
            "-vf", f"fps={sample_fps:.5f},scale=768:-2:flags=lanczos",
            "-q:v", "4", str(output_pattern),
        ]
        completed = subprocess.run(
            command, capture_output=True, text=True, encoding="utf-8", errors="replace", check=False
        )
        if completed.returncode != 0:
            raise DirectorError(
                "低清成片抽帧失败 / Preview frame extraction failed: "
                + (completed.stderr.strip() or f"exit {completed.returncode}")
            )
        frames = sorted(destination.glob("frame_*.jpg"))
        if not frames:
            raise DirectorError("低清成片没有可审帧 / No reviewable preview frames were extracted.")
        self._sample_fps = sample_fps
        return frames

    @staticmethod
    def _audible_program(timeline: Dict[str, Any]) -> List[Dict[str, Any]]:
        """Map literal selected dialogue onto program time. / 将实际保留对白映射到成片时间。"""
        result: List[Dict[str, Any]] = []
        cursor = 0.0
        for clip in timeline.get("clips", []):
            if not isinstance(clip, dict):
                continue
            start = float(clip.get("cut_in_sec", 0) or 0)
            end = float(clip.get("cut_out_sec", start) or start)
            intent = str(clip.get("audio_intent") or "natural_texture").casefold()
            if intent != "mute_for_music":
                ranges = clip.get("dialogue_ranges_sec")
                for segment in ranges if isinstance(ranges, list) else []:
                    if not isinstance(segment, dict):
                        continue
                    seg_start = max(start, float(segment.get("start_sec", start) or start))
                    seg_end = min(end, float(segment.get("end_sec", seg_start) or seg_start))
                    text = " ".join(str(segment.get("text") or "").split())
                    if seg_end > seg_start and text:
                        result.append(
                            {
                                "timeline_in_sec": round(cursor + seg_start - start, 3),
                                "timeline_out_sec": round(cursor + seg_end - start, 3),
                                "text": text,
                            }
                        )
            cursor += max(0.0, end - start)
        return result

    @staticmethod
    def _compact_music_map(timeline: Dict[str, Any]) -> Dict[str, Any]:
        """Expose only literal cue placement and measured landmarks. / 仅暴露实际音乐位置与测量节拍。"""
        plan = timeline.get("music_plan")
        plan = plan if isinstance(plan, dict) else {}
        cues = []
        for raw in plan.get("cues", []):
            if not isinstance(raw, dict):
                continue
            cues.append(
                {
                    key: raw.get(key)
                    for key in (
                        "cue_id", "track_file", "timeline_in_sec", "timeline_out_sec",
                        "track_in_sec", "track_out_sec", "target_lufs",
                        "duck_under_dialogue_db", "sync_points",
                    )
                }
            )
        return {
            "cues": cues,
            "silence_regions": plan.get("silence_regions", []),
            "rhythm_audit": plan.get("rhythm_audit", {}),
        }

    @staticmethod
    def _deterministic_failures(
        timeline: Dict[str, Any],
        duration: float,
        audible_program: Sequence[Dict[str, Any]],
        visual_batches: Sequence[Dict[str, Any]],
        inferred_form: str,
    ) -> List[str]:
        """
        Return objective failures that a fluent critic is not allowed to overrule.
        返回不能被语言模型主观高分推翻的客观失败项。

        Parameters / 参数:
            timeline: Authored timeline JSON. / 已编排时间线 JSON。
            duration: Picture duration in seconds. / 画面总时长（秒）。
            audible_program: Literal audible dialogue mapped to program time. /
                映射到成片时间的实际可听对白。
            visual_batches: Chronological literal visual observations. /
                按时间排列的字面画面观察。
            inferred_form: Form inferred by the blind reviewer. / 盲审推断的影片形式。
        """
        failures: List[str] = []
        directing = timeline.get("candidate_directing")
        if (
            isinstance(directing, dict)
            and directing.get("quality_gate_passed") is False
            and directing.get("quality_gate_degraded_acceptance") is not True
        ):
            failures.append(
                "The upstream measured director quality gate is false; an unapproved picture "
                "lock cannot pass rendered review."
            )
        intervals: List[tuple[float, float]] = []
        for item in audible_program:
            try:
                start = max(0.0, float(item.get("timeline_in_sec", 0) or 0))
                end = min(duration, float(item.get("timeline_out_sec", start) or start))
            except (TypeError, ValueError):
                continue
            if end > start:
                intervals.append((start, end))
        intervals.sort()
        merged: List[List[float]] = []
        for start, end in intervals:
            if not merged or start > merged[-1][1]:
                merged.append([start, end])
            else:
                merged[-1][1] = max(merged[-1][1], end)
        speech_ratio = (
            sum(end - start for start, end in merged) / duration
            if duration > 0 else 0.0
        )
        if inferred_form not in {"dialogue_scene", "bts_process"} and speech_ratio > 0.72:
            failures.append(
                f"Audible dialogue occupies {speech_ratio:.1%} of a non-dialogue film."
            )
        stagnant_tail = 0.0
        for batch in reversed(list(visual_batches)):
            changes = batch.get("visible_state_changes")
            if isinstance(changes, list) and changes:
                break
            try:
                batch_in = float(batch.get("timeline_in_sec", 0) or 0)
                batch_out = float(batch.get("timeline_out_sec", batch_in) or batch_in)
            except (TypeError, ValueError):
                break
            stagnant_tail += max(0.0, batch_out - batch_in)
        stagnant_limit = max(8.0, duration * 0.25)
        if len(visual_batches) > 1 and stagnant_tail >= stagnant_limit:
            failures.append(
                f"The final {stagnant_tail:.1f}s contains no observed visual state change."
            )
        return failures

    def review(self, preview_path: Path, timeline_path: Path, output_path: Path) -> Dict[str, Any]:
        """
        Inspect every sampled preview interval and publish a blind verdict.
        审查低清成片的全部采样区间并发布隔离盲审结论。

        Parameters / 参数:
            preview_path: Rendered MP4 to inspect. / 待审 MP4。
            timeline_path: Matching timeline JSON for literal audio timing. / 对应时间线 JSON。
            output_path: Destination review JSON. / 审片 JSON 输出路径。
        """
        preview = preview_path.expanduser().resolve()
        timeline_file = timeline_path.expanduser().resolve()
        destination = output_path.expanduser().resolve()
        if not preview.is_file():
            raise DirectorError(f"低清成片不存在 / Preview not found: {preview}")
        timeline = self._load_json(timeline_file)
        duration = self._program_duration(timeline)
        if duration <= 0:
            raise DirectorError("时间线没有正时长 / Timeline has no positive duration.")

        batch_reviews: List[Dict[str, Any]] = []
        try:
            self.director.check_ollama(require_vision=True)
            with tempfile.TemporaryDirectory(prefix="cybereditor-preview-review-") as temp:
                frame_dir = Path(temp)
                frames = self._extract_frames(preview, frame_dir, duration)
                batch_size = 12
                prior_memory = ""
                for offset in range(0, len(frames), batch_size):
                    batch = frames[offset:offset + batch_size]
                    images = [
                        base64.b64encode(path.read_bytes()).decode("ascii") for path in batch
                    ]
                    legend = [
                        {
                            "image_index": index + 1,
                            "timeline_sec": round((offset + index) / self._sample_fps, 3),
                        }
                        for index in range(len(batch))
                    ]
                    prompt = (
                        "RENDERED ROUGH-CUT VISUAL AUDIT. The images are consecutive sampled "
                        "frames from the actual edited preview in chronological order. Describe "
                        "only visible subjects, actions, reactions, shot changes, on-screen text, "
                        "and state changes. Do not infer events between frames. A pose, headlight, "
                        "countdown, or forward lean is not a departure. Note abrupt or confusing "
                        "visual continuity. Return JSON only.\n"
                        f"PRIOR LITERAL VISUAL MEMORY: {prior_memory or '(start of film)'}\n"
                        f"FRAME TIMES: {json.dumps(legend, ensure_ascii=False)}"
                    )
                    review = self.director._request_json(
                        prompt,
                        VISUAL_BATCH_SCHEMA,
                        images=images,
                        model=self.model,
                        progress_activity="rough_cut_visual_review",
                    )
                    review["timeline_in_sec"] = legend[0]["timeline_sec"]
                    review["timeline_out_sec"] = min(
                        duration,
                        legend[-1]["timeline_sec"] + 1.0 / self._sample_fps,
                    )
                    batch_reviews.append(review)
                    prior_memory = str(review.get("literal_visual_summary") or "")[-1200:]

            if self.text_model != self.model:
                self.director.unload_model(self.model)
            audible_program = self._audible_program(timeline)
            music_map = self._compact_music_map(timeline)
            final_prompt = (
                "CONTEXT-ISOLATED ROUGH-CUT VIEWER TEST. You did not receive the creative brief, "
                "treatment, intended theme, shot labels, or editor explanations. Judge only the "
                "actual chronological visual observations and literal audible program below. "
                "Explain what a stranger would think happened, who the film is about, what changed, "
                "and why it ends where it does. Production chatter is useful only if the resulting "
                "problem-attempt-result story is understandable without hidden context. A mood film "
                "may pass without plot only when subject, progression, pacing, and visual payoff are "
                "clearly deliberate. Music metadata proves placement, not emotional success; do not "
                "claim you heard qualities that are not supplied. Set passes=true only when coherence "
                "is >=7 and either causal clarity or visual payoff is >=7, with no unresolved claim "
                "that the ending depends on. Return JSON only.\n"
                f"PROGRAM DURATION: {duration:.3f}s\n"
                f"CHRONOLOGICAL VISUAL OBSERVATIONS: {json.dumps(batch_reviews, ensure_ascii=False, separators=(',', ':'))}\n"
                f"LITERAL AUDIBLE DIALOGUE: {json.dumps(audible_program, ensure_ascii=False, separators=(',', ':'))}\n"
                f"LITERAL MUSIC PLACEMENT: {json.dumps(music_map, ensure_ascii=False, separators=(',', ':'))}"
            )
            blind = self.director._request_json(
                final_prompt,
                PREVIEW_REVIEW_SCHEMA,
                model=self.text_model,
                progress_activity="rough_cut_blind_review",
            )
            coherence = int(blind.get("coherence_score", 0) or 0)
            causal = int(blind.get("causal_clarity_score", 0) or 0)
            payoff = int(blind.get("visual_payoff_score", 0) or 0)
            inferred_form = str(blind.get("inferred_form") or "incoherent")
            form_score = (
                payoff
                if inferred_form in {"mood_montage", "character_vignette"}
                else causal
            )
            deterministic_failures = self._deterministic_failures(
                timeline,
                duration,
                audible_program,
                batch_reviews,
                inferred_form,
            )
            deterministic_pass = (
                bool(blind.get("passes"))
                and inferred_form != "incoherent"
                and coherence >= 7
                and form_score >= 7
                and not bool(blind.get("unsupported_or_unresolved_points"))
                and not deterministic_failures
            )
            blind["model_passes"] = bool(blind.get("passes"))
            blind["passes"] = deterministic_pass
            blind["deterministic_failures"] = deterministic_failures
            result = {
                "schema_version": "1.0",
                "generated_at_utc": datetime.now(timezone.utc).isoformat(),
                "review_kind": "rendered_preview_context_isolated",
                "preview_file": str(preview),
                "timeline_file": str(timeline_file),
                "duration_sec": round(duration, 3),
                "sample_fps": round(self._sample_fps, 5),
                "frame_count": len(frames),
                "visual_batch_count": len(batch_reviews),
                "visual_batches": batch_reviews,
                "blind_review": blind,
                "deterministic_failures": deterministic_failures,
                "passes": deterministic_pass,
                "quality_score": round((coherence + form_score + int(blind.get("pacing_score", 0) or 0)) / 3, 2),
            }
            destination.parent.mkdir(parents=True, exist_ok=True)
            temporary = destination.with_suffix(destination.suffix + ".tmp")
            temporary.write_text(
                json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            temporary.replace(destination)
            self.logger.info(
                "低清成片盲审：%s，连贯性 %d/10 / Rendered rough-cut blind review: %s, coherence %d/10",
                "通过" if deterministic_pass else "退回重剪",
                coherence,
                "passed" if deterministic_pass else "recut required",
                coherence,
            )
            return result
        finally:
            self.director.unload_model(self.model)
            if self.text_model != self.model:
                self.director.unload_model(self.text_model)


def configure_logging(level: str = "INFO") -> logging.Logger:
    """Configure standalone bilingual logging. / 配置独立双语日志。"""
    logger = logging.getLogger(LOGGER_NAME)
    logger.handlers.clear()
    logger.propagate = False
    logger.setLevel(getattr(logging, level.upper()))
    handler = logging.StreamHandler()
    handler.setFormatter(logging.Formatter("%(asctime)s | %(levelname)s | %(name)s | %(message)s"))
    logger.addHandler(handler)
    return logger


def build_parser() -> argparse.ArgumentParser:
    """Create the rough-cut review CLI. / 创建低清粗剪审片命令行。"""
    parser = argparse.ArgumentParser(description="Blind-review a rendered CyberEditor preview.")
    parser.add_argument("--preview", required=True)
    parser.add_argument("--timeline", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--model", required=True)
    parser.add_argument("--text-model", default="")
    parser.add_argument("--ollama-url", default="http://localhost:11434")
    parser.add_argument("--num-ctx", type=int, default=32768)
    parser.add_argument("--timeout", type=int, default=7200)
    parser.add_argument(
        "--log-level", default="INFO", choices=("DEBUG", "INFO", "WARNING", "ERROR")
    )
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    """Run preview review with deterministic exit codes. / 运行预览审片并返回确定性退出码。"""
    args = build_parser().parse_args(argv)
    logger = configure_logging(args.log_level)
    try:
        reviewer = RoughCutReviewer(
            model=args.model,
            text_model=args.text_model or args.model,
            base_url=args.ollama_url,
            num_ctx=args.num_ctx,
            timeout_sec=args.timeout,
            logger=logger,
        )
        reviewer.review(Path(args.preview), Path(args.timeline), Path(args.output))
        return 0
    except DirectorError as exc:
        logger.error("%s", exc)
        return 2
    except KeyboardInterrupt:
        logger.warning("用户中断低清成片审片 / Rough-cut review interrupted.")
        return 130
    except Exception:
        logger.exception("未预期低清成片审片错误 / Unexpected rough-cut review error.")
        return 1


if __name__ == "__main__":
    sys_exit = main()
    raise SystemExit(sys_exit)
