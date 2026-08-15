"""Repeatable editorial-quality evaluation against a human reference edit.

This module intentionally uses only the Python standard library so that an
evaluation can run in CI without loading any AI, video, or DaVinci component.
It measures *decision similarity*, not whether a video is artistically good.
"""

from __future__ import annotations

import argparse
from collections import defaultdict
from dataclasses import dataclass
import json
import logging
import math
from pathlib import Path
import sys
from typing import Any, Iterable, Mapping, Sequence


LOGGER = logging.getLogger("cybereditor.editorial_eval")
REPORT_SCHEMA_VERSION = "1.0"


class EditorialEvalError(RuntimeError):
    """Raised for actionable input errors. / 表示可由用户修复的评测输入错误。"""


@dataclass(frozen=True)
class TimelineClip:
    """Normalized clip decision. / 归一化后的单条剪辑决策。"""

    index: int
    source_key: str
    source_label: str
    cut_in_sec: float
    cut_out_sec: float
    clip_id: str

    @property
    def duration_sec(self) -> float:
        """Return selected source duration. / 返回所选源片段时长。"""

        return self.cut_out_sec - self.cut_in_sec


@dataclass(frozen=True)
class ParsedTimeline:
    """Validated timeline plus optional reference target. / 已验证时间线及可选目标时长。"""

    clips: tuple[TimelineClip, ...]
    target_duration_sec: float | None = None


class EditorialEvaluator:
    """Compare an AI edit with a manually approved reference edit.

    将 AI 时间线与人工认可的参考剪辑进行可重复比较。源素材选择按唯一
    ``source_id``（缺省时按文件名）统计；同源片段再进行一对一边界匹配。

    Args:
        duplicate_iou_threshold: Temporal IoU above which a later selection is
            considered a repeated shot. / 判定后续片段为重复镜头的时间交并比阈值。
    """

    SOURCE_FIELDS = ("source_id", "asset_id", "file_name", "source_file", "proxy_file_name")

    def __init__(self, duplicate_iou_threshold: float = 0.8) -> None:
        if not 0.0 <= duplicate_iou_threshold <= 1.0:
            raise ValueError("duplicate_iou_threshold must be between 0 and 1")
        self.duplicate_iou_threshold = float(duplicate_iou_threshold)

    def evaluate_files(self, ai_path: Path | str, reference_path: Path | str) -> dict[str, Any]:
        """Load two JSON files and return a serializable report.

        读取 AI 与人工参考 JSON，并返回可直接序列化的报告。

        Args:
            ai_path: Path to the generated ``timeline_cuts.json``. / AI 时间线路径。
            reference_path: Path to ``human_reference.json``. / 人工参考时间线路径。

        Raises:
            EditorialEvalError: If a file is missing, malformed, or has invalid
                clip fields. / 文件缺失、JSON 损坏或片段字段无效时抛出。
        """

        ai_path = Path(ai_path)
        reference_path = Path(reference_path)
        ai_payload = self._read_json(ai_path)
        reference_payload = self._read_json(reference_path)
        ai = self.parse_timeline(ai_payload, label=f"AI timeline ({ai_path})", allow_empty=True)
        reference = self.parse_timeline(
            reference_payload,
            label=f"human reference ({reference_path})",
            allow_empty=False,
        )
        report = self.evaluate(ai, reference)
        report["inputs"] = {
            "ai_timeline": str(ai_path.resolve()),
            "human_reference": str(reference_path.resolve()),
        }
        return report

    def parse_timeline(
        self,
        payload: Any,
        *,
        label: str,
        allow_empty: bool,
    ) -> ParsedTimeline:
        """Validate and normalize one timeline document.

        校验并归一化一个时间线文档。支持 ``source_id``、``asset_id`` 或
        常见文件路径字段作为素材身份，但入点和出点必须是秒数。

        Args:
            payload: Decoded JSON value. / 已解析的 JSON 值。
            label: Human-readable input name for errors. / 用于错误信息的输入名称。
            allow_empty: Whether an empty ``clips`` list is valid. / 是否允许空片段列表。
        """

        if not isinstance(payload, Mapping):
            raise EditorialEvalError(f"{label}: JSON root must be an object / JSON 根节点必须是对象")
        raw_clips = payload.get("clips")
        if not isinstance(raw_clips, list):
            raise EditorialEvalError(f"{label}: missing array field 'clips' / 缺少数组字段 clips")
        if not raw_clips and not allow_empty:
            raise EditorialEvalError(f"{label}: 'clips' cannot be empty / 人工参考 clips 不能为空")

        clips: list[TimelineClip] = []
        for index, raw_clip in enumerate(raw_clips):
            where = f"{label}: clips[{index}]"
            if not isinstance(raw_clip, Mapping):
                raise EditorialEvalError(f"{where} must be an object / 必须是对象")

            source_field, source_value = self._find_source(raw_clip)
            if source_field is None:
                expected = ", ".join(self.SOURCE_FIELDS)
                raise EditorialEvalError(
                    f"{where}: missing source identity; provide one of {expected} "
                    f"/ 缺少素材身份字段"
                )
            source_label = str(source_value).strip()
            if not source_label:
                raise EditorialEvalError(f"{where}.{source_field} cannot be blank / 不能为空")

            if "cut_in_sec" not in raw_clip:
                raise EditorialEvalError(
                    f"{where}: missing 'cut_in_sec' (in point) / 缺少入点字段 cut_in_sec"
                )
            if "cut_out_sec" not in raw_clip:
                raise EditorialEvalError(
                    f"{where}: missing 'cut_out_sec' (out point) / 缺少出点字段 cut_out_sec"
                )
            cut_in = self._number(raw_clip, "cut_in_sec", where)
            cut_out = self._number(raw_clip, "cut_out_sec", where)
            if cut_in < 0:
                raise EditorialEvalError(f"{where}.cut_in_sec must be >= 0 / 入点不能为负数")
            if cut_out <= cut_in:
                raise EditorialEvalError(
                    f"{where}.cut_out_sec must be greater than cut_in_sec "
                    f"/ 出点必须晚于入点"
                )

            raw_id = raw_clip.get("clip_id", index + 1)
            clips.append(
                TimelineClip(
                    index=index,
                    source_key=self._source_key(source_field, source_label),
                    source_label=source_label,
                    cut_in_sec=cut_in,
                    cut_out_sec=cut_out,
                    clip_id=str(raw_id),
                )
            )

        target: float | None = None
        if payload.get("target_duration_sec") is not None:
            target = self._number(payload, "target_duration_sec", label)
            if target <= 0:
                raise EditorialEvalError(f"{label}.target_duration_sec must be > 0 / 目标时长必须大于零")
        return ParsedTimeline(tuple(clips), target)

    def evaluate(self, ai: ParsedTimeline, reference: ParsedTimeline) -> dict[str, Any]:
        """Compute stable, model-independent editorial metrics.

        计算稳定且与模型无关的剪辑指标。此函数不读取媒体，仅评估时间线决策。

        Args:
            ai: Parsed AI timeline. / 已解析的 AI 时间线。
            reference: Parsed human reference timeline. / 已解析的人工参考时间线。
        """

        ai_sources = {clip.source_key for clip in ai.clips}
        reference_sources = {clip.source_key for clip in reference.clips}
        true_sources = ai_sources & reference_sources
        precision = self._safe_ratio(len(true_sources), len(ai_sources))
        recall = self._safe_ratio(len(true_sources), len(reference_sources))
        f1 = self._f1(precision, recall)

        matches = self._match_clips(ai.clips, reference.clips)
        boundary = self._boundary_metrics(matches)
        order = self._order_metrics(matches)
        repetition = self._repetition_metrics(ai.clips)

        ai_duration = sum(clip.duration_sec for clip in ai.clips)
        reference_duration = sum(clip.duration_sec for clip in reference.clips)
        target_duration = reference.target_duration_sec or reference_duration
        duration_error = ai_duration - target_duration

        matched_ai = {ai_clip.index for ai_clip, _ in matches}
        matched_reference = {ref_clip.index for _, ref_clip in matches}
        report_matches = [
            {
                "ai_clip_id": ai_clip.clip_id,
                "reference_clip_id": ref_clip.clip_id,
                "source": ai_clip.source_label,
                "in_error_sec": round(ai_clip.cut_in_sec - ref_clip.cut_in_sec, 6),
                "out_error_sec": round(ai_clip.cut_out_sec - ref_clip.cut_out_sec, 6),
                "temporal_iou": round(self._temporal_iou(ai_clip, ref_clip), 6),
            }
            for ai_clip, ref_clip in sorted(matches, key=lambda pair: pair[0].index)
        ]

        return {
            "schema_version": REPORT_SCHEMA_VERSION,
            "metric_scope": (
                "Timeline-decision similarity only; artistic quality still requires human review. "
                "/ 仅衡量时间线决策相似度，艺术质量仍需人工评审。"
            ),
            "metrics": {
                "source_selection": {
                    "unit": "unique_source",
                    "ai_selected_count": len(ai_sources),
                    "reference_selected_count": len(reference_sources),
                    "correct_selected_count": len(true_sources),
                    "precision": round(precision, 6),
                    "recall": round(recall, 6),
                    "f1": round(f1, 6),
                },
                "boundary_accuracy": boundary,
                "order_consistency": order,
                "repetition": repetition,
                "duration": {
                    "ai_duration_sec": round(ai_duration, 6),
                    "reference_edit_duration_sec": round(reference_duration, 6),
                    "target_duration_sec": round(target_duration, 6),
                    "target_source": (
                        "human_reference.target_duration_sec"
                        if reference.target_duration_sec is not None
                        else "sum_of_reference_clips"
                    ),
                    "signed_error_sec": round(duration_error, 6),
                    "absolute_error_sec": round(abs(duration_error), 6),
                    "absolute_error_percent": round(
                        self._safe_ratio(abs(duration_error), target_duration) * 100.0,
                        6,
                    ),
                },
            },
            "matches": report_matches,
            "unmatched_ai_clip_ids": [clip.clip_id for clip in ai.clips if clip.index not in matched_ai],
            "unmatched_reference_clip_ids": [
                clip.clip_id for clip in reference.clips if clip.index not in matched_reference
            ],
        }

    @staticmethod
    def write_report(report: Mapping[str, Any], output_path: Path | str) -> Path:
        """Write a UTF-8 JSON report. / 将报告写为 UTF-8 JSON 文件。

        Args:
            report: Evaluation report returned by :meth:`evaluate`. / 评测报告。
            output_path: Destination JSON path. / 输出 JSON 路径。
        """

        path = Path(output_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        return path

    @staticmethod
    def _read_json(path: Path) -> Any:
        if not path.is_file():
            raise EditorialEvalError(f"File not found / 找不到文件: {path}")
        try:
            return json.loads(path.read_text(encoding="utf-8-sig"))
        except OSError as exc:
            raise EditorialEvalError(f"Cannot read file / 无法读取文件 {path}: {exc}") from exc
        except json.JSONDecodeError as exc:
            raise EditorialEvalError(
                f"Invalid JSON / JSON 格式无效 {path}:{exc.lineno}:{exc.colno}: {exc.msg}"
            ) from exc

    @classmethod
    def _find_source(cls, clip: Mapping[str, Any]) -> tuple[str | None, Any]:
        for field in cls.SOURCE_FIELDS:
            if clip.get(field) is not None:
                return field, clip[field]
        return None, None

    @staticmethod
    def _source_key(field: str, value: str) -> str:
        normalized = value.strip().replace("\\", "/").rstrip("/")
        if field not in {"source_id", "asset_id"}:
            normalized = normalized.rsplit("/", 1)[-1]
        return normalized.casefold()

    @staticmethod
    def _number(mapping: Mapping[str, Any], field: str, where: str) -> float:
        if field not in mapping:
            raise EditorialEvalError(f"{where}: missing '{field}' / 缺少字段 {field}")
        value = mapping[field]
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise EditorialEvalError(f"{where}.{field} must be a number / 必须是数字")
        result = float(value)
        if not math.isfinite(result):
            raise EditorialEvalError(f"{where}.{field} must be finite / 必须是有限数字")
        return result

    @staticmethod
    def _safe_ratio(numerator: float, denominator: float) -> float:
        return numerator / denominator if denominator else 0.0

    @staticmethod
    def _f1(precision: float, recall: float) -> float:
        return 2.0 * precision * recall / (precision + recall) if precision + recall else 0.0

    @staticmethod
    def _temporal_iou(left: TimelineClip, right: TimelineClip) -> float:
        overlap = max(0.0, min(left.cut_out_sec, right.cut_out_sec) - max(left.cut_in_sec, right.cut_in_sec))
        union = max(left.cut_out_sec, right.cut_out_sec) - min(left.cut_in_sec, right.cut_in_sec)
        return overlap / union if union > 0 else 0.0

    def _match_clips(
        self,
        ai_clips: Sequence[TimelineClip],
        reference_clips: Sequence[TimelineClip],
    ) -> list[tuple[TimelineClip, TimelineClip]]:
        """Greedily make deterministic one-to-one same-source matches.

        以时间交并比优先、边界误差次优的顺序，为同源片段建立确定性一对一匹配。
        Non-overlapping same-source selections are still paired so that a large boundary
        error remains visible instead of silently disappearing from the report.
        """

        candidates: list[tuple[float, float, int, int, TimelineClip, TimelineClip]] = []
        for ai_clip in ai_clips:
            for reference_clip in reference_clips:
                if ai_clip.source_key != reference_clip.source_key:
                    continue
                iou = self._temporal_iou(ai_clip, reference_clip)
                boundary_error = abs(ai_clip.cut_in_sec - reference_clip.cut_in_sec) + abs(
                    ai_clip.cut_out_sec - reference_clip.cut_out_sec
                )
                # Round ranking values so binary floating-point noise cannot
                # change a tie across Python/platform builds.
                candidates.append(
                    (
                        -round(iou, 12),
                        round(boundary_error, 12),
                        ai_clip.index,
                        reference_clip.index,
                        ai_clip,
                        reference_clip,
                    )
                )
        candidates.sort(key=lambda item: item[:4])
        used_ai: set[int] = set()
        used_reference: set[int] = set()
        matches: list[tuple[TimelineClip, TimelineClip]] = []
        for _, _, _, _, ai_clip, reference_clip in candidates:
            if ai_clip.index in used_ai or reference_clip.index in used_reference:
                continue
            used_ai.add(ai_clip.index)
            used_reference.add(reference_clip.index)
            matches.append((ai_clip, reference_clip))
        return matches

    @staticmethod
    def _boundary_metrics(matches: Sequence[tuple[TimelineClip, TimelineClip]]) -> dict[str, Any]:
        if not matches:
            return {
                "matched_clip_count": 0,
                "in_mae_sec": None,
                "out_mae_sec": None,
                "boundary_mae_sec": None,
                "note": "No same-source clips could be matched / 没有可匹配的同源片段",
            }
        in_errors = [abs(ai.cut_in_sec - ref.cut_in_sec) for ai, ref in matches]
        out_errors = [abs(ai.cut_out_sec - ref.cut_out_sec) for ai, ref in matches]
        in_mae = sum(in_errors) / len(in_errors)
        out_mae = sum(out_errors) / len(out_errors)
        return {
            "matched_clip_count": len(matches),
            "in_mae_sec": round(in_mae, 6),
            "out_mae_sec": round(out_mae, 6),
            "boundary_mae_sec": round((in_mae + out_mae) / 2.0, 6),
        }

    @staticmethod
    def _order_metrics(matches: Sequence[tuple[TimelineClip, TimelineClip]]) -> dict[str, Any]:
        ordered = sorted(matches, key=lambda pair: pair[0].index)
        concordant = 0
        discordant = 0
        for left_index in range(len(ordered)):
            for right_index in range(left_index + 1, len(ordered)):
                if ordered[left_index][1].index < ordered[right_index][1].index:
                    concordant += 1
                else:
                    discordant += 1
        comparable = concordant + discordant
        return {
            "score": round(concordant / comparable, 6) if comparable else None,
            "matched_clip_count": len(matches),
            "comparable_pair_count": comparable,
            "concordant_pair_count": concordant,
            "inversion_count": discordant,
            "note": (
                None
                if comparable
                else "At least two matched clips are required / 至少需要两个匹配片段"
            ),
        }

    def _repetition_metrics(self, clips: Sequence[TimelineClip]) -> dict[str, Any]:
        duplicate_count = 0
        previous_by_source: dict[str, list[TimelineClip]] = defaultdict(list)
        for clip in clips:
            if any(
                self._temporal_iou(clip, previous) >= self.duplicate_iou_threshold
                for previous in previous_by_source[clip.source_key]
            ):
                duplicate_count += 1
            previous_by_source[clip.source_key].append(clip)

        adjacent_same_source_count = sum(
            1 for left, right in zip(clips, clips[1:]) if left.source_key == right.source_key
        )
        adjacent_pair_count = max(0, len(clips) - 1)
        return {
            "duplicate_iou_threshold": self.duplicate_iou_threshold,
            "duplicate_clip_count": duplicate_count,
            "duplicate_clip_ratio": round(self._safe_ratio(duplicate_count, len(clips)), 6),
            "adjacent_pair_count": adjacent_pair_count,
            "adjacent_same_source_count": adjacent_same_source_count,
            "adjacent_same_source_ratio": round(
                self._safe_ratio(adjacent_same_source_count, adjacent_pair_count),
                6,
            ),
        }


def build_parser() -> argparse.ArgumentParser:
    """Build the CLI parser. / 构建命令行参数解析器。"""

    parser = argparse.ArgumentParser(
        description="Compare timeline_cuts.json against a human reference / 对比 AI 与人工参考剪辑",
    )
    parser.add_argument("--ai", required=True, type=Path, help="AI timeline_cuts.json path / AI 时间线路径")
    parser.add_argument(
        "--reference",
        required=True,
        type=Path,
        help="human_reference.json path / 人工参考时间线路径",
    )
    parser.add_argument("--output", type=Path, help="optional report JSON path / 可选报告输出路径")
    parser.add_argument(
        "--duplicate-iou-threshold",
        type=float,
        default=0.8,
        help="repeated-shot IoU threshold (default: 0.8) / 重复镜头交并比阈值",
    )
    parser.add_argument(
        "--log-level",
        choices=("DEBUG", "INFO", "WARNING", "ERROR"),
        default="INFO",
        help="logging level / 日志级别",
    )
    return parser


def main(argv: Iterable[str] | None = None) -> int:
    """Run the editorial-evaluation CLI. / 运行剪辑评测命令行工具。"""

    args = build_parser().parse_args(list(argv) if argv is not None else None)
    logging.basicConfig(
        level=getattr(logging, args.log_level),
        format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    )
    try:
        evaluator = EditorialEvaluator(args.duplicate_iou_threshold)
        report = evaluator.evaluate_files(args.ai, args.reference)
        if args.output:
            written = evaluator.write_report(report, args.output)
            LOGGER.info("Evaluation report written / 评测报告已写入: %s", written)
        else:
            sys.stdout.write(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
        metrics = report["metrics"]
        LOGGER.info(
            "Editorial evaluation complete / 剪辑评测完成: source F1=%.3f, boundary MAE=%s s, order=%s",
            metrics["source_selection"]["f1"],
            metrics["boundary_accuracy"]["boundary_mae_sec"],
            metrics["order_consistency"]["score"],
        )
        return 0
    except (EditorialEvalError, ValueError) as exc:
        LOGGER.error("Editorial evaluation failed / 剪辑评测失败: %s", exc)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
