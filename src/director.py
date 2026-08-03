#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Chunked local-Ollama editing decisions.
基于本地 Ollama 的分块剪辑决策。

The director never imports Whisper, OpenCV, or DaVinci Resolve. It processes
10–15 minute time windows sequentially, validates every model response, merges
the decisions deterministically, and unloads the Ollama model in ``finally``.

导演层绝不导入 Whisper、OpenCV 或 DaVinci Resolve。它串行处理 10–15 分钟
时间窗，验证每次模型响应，确定性合并决策，并在 ``finally`` 中卸载 Ollama 模型。
"""

import argparse
import base64
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
import hashlib
import json
import logging
import os
from pathlib import Path
import re
import sys
from typing import Any, Dict, List, Optional, Sequence


LOGGER_NAME = "cybereditor.director"
DIRECTOR_CHECKPOINT_VERSION = 1
DIRECTOR_PROMPT_VERSION = "2026-08-03.1-72b-per-source-color"

TREATMENT_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "properties": {
        "title": {"type": "string", "minLength": 1},
        "logline": {"type": "string", "minLength": 1},
        "central_theme": {"type": "string", "minLength": 1},
        "chronology_policy": {
            "type": "string",
            "enum": ["strict_chronological", "teaser_then_chronological"],
        },
        "target_duration_sec": {"type": "number", "minimum": 30, "maximum": 600},
        "opening_beat": {"type": "string", "minLength": 1},
        "development_beat": {"type": "string", "minLength": 1},
        "payoff_beat": {"type": "string", "minLength": 1},
        "ending_beat": {"type": "string", "minLength": 1},
        "color_intent": {"type": "string", "minLength": 1},
        "creative_look": {
            "type": "string",
            "enum": ["clean_neutral", "cinematic_warm", "cool_steel", "high_contrast"],
        },
        "music_mood": {"type": "string", "minLength": 1},
        "music_energy_arc": {"type": "string", "minLength": 1},
        "editorial_rules": {
            "type": "array",
            "items": {"type": "string", "minLength": 1},
            "minItems": 2,
            "maxItems": 8,
        },
        "story_anchors": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "asset_id": {"type": "string", "minLength": 1},
                    "cut_in_sec": {"type": "number", "minimum": 0},
                    "cut_out_sec": {"type": "number", "minimum": 0},
                    "beat": {
                        "type": "string",
                        "enum": ["opening", "development", "payoff", "ending"],
                    },
                    "reason": {"type": "string", "minLength": 1},
                },
                "required": [
                    "asset_id", "cut_in_sec", "cut_out_sec", "beat", "reason"
                ],
                "additionalProperties": False,
            },
            "minItems": 3,
            "maxItems": 12,
        },
    },
    "required": [
        "title", "logline", "central_theme", "chronology_policy",
        "target_duration_sec", "opening_beat", "development_beat",
        "payoff_beat", "ending_beat", "color_intent", "creative_look",
        "music_mood", "music_energy_arc", "editorial_rules", "story_anchors",
    ],
    "additionalProperties": False,
}

DECISION_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "properties": {
        "decisions": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "cut_in_sec": {"type": "number", "minimum": 0},
                    "cut_out_sec": {"type": "number", "minimum": 0},
                    "reason_for_cut": {"type": "string", "minLength": 1},
                    "confidence": {
                        "type": "number",
                        "minimum": 0,
                        "maximum": 1,
                    },
                    "volume_db": {"type": "number", "minimum": -24, "maximum": 12},
                    "drx_preset": {
                        "type": "string",
                        "enum": [
                            "none",
                            "interview_clean",
                            "cinematic",
                            "low_light_cleanup",
                        ],
                    },
                    "stabilization": {
                        "type": "string",
                        "enum": ["none", "auto"],
                    },
                    "tracking": {
                        "type": "string",
                        "enum": [
                            "none",
                            "magic_mask_forward",
                            "magic_mask_backward",
                            "magic_mask_bidirectional",
                        ],
                    },
                    "smart_reframe": {"type": "boolean"},
                },
                "required": [
                    "cut_in_sec",
                    "cut_out_sec",
                    "reason_for_cut",
                ],
                "additionalProperties": False,
            },
        }
    },
    "required": ["decisions"],
    "additionalProperties": False,
}

CANDIDATE_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "properties": {
        "decisions": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "cut_in_sec": {"type": "number", "minimum": 0},
                    "cut_out_sec": {"type": "number", "minimum": 0},
                    "reason_for_cut": {"type": "string", "minLength": 1},
                    "visual_summary": {"type": "string", "minLength": 1},
                    "story_role": {
                        "type": "string",
                        "enum": [
                            "opening",
                            "context",
                            "interview",
                            "broll",
                            "bridge",
                            "climax",
                            "closing",
                        ],
                    },
                    "confidence": {"type": "number", "minimum": 0, "maximum": 1},
                    "quality_score": {"type": "number", "minimum": 0, "maximum": 1},
                    "transition_to_next": {
                        "type": "string",
                        "enum": ["cut", "cross_dissolve", "fade_black"],
                    },
                    "transition_duration_sec": {
                        "type": "number",
                        "minimum": 0,
                        "maximum": 2,
                    },
                    "audio_cleanup": {
                        "type": "string",
                        "enum": ["none", "light", "strong"],
                    },
                    "color_look": {
                        "type": "string",
                        "enum": ["source", "neutral", "warm", "cool", "contrast"],
                    },
                    "motion": {
                        "type": "string",
                        "enum": ["static", "gentle_push_in"],
                    },
                    "volume_db": {"type": "number", "minimum": -24, "maximum": 12},
                    "drx_preset": {
                        "type": "string",
                        "enum": [
                            "none",
                            "interview_clean",
                            "cinematic",
                            "low_light_cleanup",
                        ],
                    },
                    "stabilization": {
                        "type": "string",
                        "enum": ["none", "auto"],
                    },
                    "tracking": {
                        "type": "string",
                        "enum": [
                            "none",
                            "magic_mask_forward",
                            "magic_mask_backward",
                            "magic_mask_bidirectional",
                        ],
                    },
                    "smart_reframe": {"type": "boolean"},
                },
                "required": [
                    "cut_in_sec",
                    "cut_out_sec",
                    "reason_for_cut",
                    "visual_summary",
                    "story_role",
                ],
                "additionalProperties": False,
            },
        }
    },
    "required": ["decisions"],
    "additionalProperties": False,
}

SEQUENCE_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "properties": {
        "project_summary": {"type": "string"},
        "music_plan": {
            "type": "object",
            "properties": {
                "track_file": {"type": "string"},
                "reason": {"type": "string"},
                "target_level_db": {"type": "number", "minimum": -36, "maximum": -6},
                "fade_in_sec": {"type": "number", "minimum": 0, "maximum": 10},
                "fade_out_sec": {"type": "number", "minimum": 0, "maximum": 10},
                "duck_dialogue": {"type": "boolean"},
            },
            "required": [
                "track_file", "reason", "target_level_db", "fade_in_sec",
                "fade_out_sec", "duck_dialogue",
            ],
            "additionalProperties": False,
        },
        "sequence": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "candidate_id": {"type": "string", "minLength": 1},
                    "reason_for_position": {"type": "string"},
                    "transition_to_next": {
                        "type": "string",
                        "enum": ["cut", "cross_dissolve", "fade_black"],
                    },
                    "transition_duration_sec": {
                        "type": "number",
                        "minimum": 0,
                        "maximum": 2,
                    },
                    "audio_cleanup": {
                        "type": "string",
                        "enum": ["none", "light", "strong"],
                    },
                    "color_look": {
                        "type": "string",
                        "enum": ["source", "neutral", "warm", "cool", "contrast"],
                    },
                    "motion": {
                        "type": "string",
                        "enum": ["static", "gentle_push_in"],
                    },
                    "volume_db": {"type": "number", "minimum": -24, "maximum": 12},
                    "drx_preset": {
                        "type": "string",
                        "enum": [
                            "none",
                            "interview_clean",
                            "cinematic",
                            "low_light_cleanup",
                        ],
                    },
                    "stabilization": {
                        "type": "string",
                        "enum": ["none", "auto"],
                    },
                    "tracking": {
                        "type": "string",
                        "enum": [
                            "none",
                            "magic_mask_forward",
                            "magic_mask_backward",
                            "magic_mask_bidirectional",
                        ],
                    },
                    "smart_reframe": {"type": "boolean"},
                },
                "required": ["candidate_id"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["project_summary", "music_plan", "sequence"],
    "additionalProperties": False,
}


class DirectorError(RuntimeError):
    """Expected AI director failure. / 可预期的 AI 导演错误。"""


class AIDirector:
    """
    Generate validated timeline cuts with a local Ollama model.
    使用本地 Ollama 模型生成已验证的时间线剪辑决策。

    Parameters / 参数:
        model:
            Local Ollama model tag, including a quantization tag if desired.
            本地 Ollama 模型标签，可包含量化标签。
        base_url:
            Ollama server root URL.
            Ollama 服务根 URL。
        chunk_minutes:
            Time window length; constrained to 10–15 minutes.
            时间窗长度；强制限制为 10–15 分钟。
        project_fps:
            FPS metadata written to ``timeline_cuts.json``.
            写入 ``timeline_cuts.json`` 的 FPS 元数据。
        num_ctx:
            Ollama context window requested per chunk.
            每个分块请求的 Ollama 上下文窗口。
        timeout_sec:
            Per-generation read timeout.
            每次生成请求的读取超时。
        merge_gap_sec:
            Adjacent decisions separated by no more than this gap are merged.
            间隔不超过此值的相邻决策会被合并。
        session:
            Optional requests-compatible session for tests/integration.
            用于测试/集成的可选 requests 兼容会话。
    """

    def __init__(
        self,
        model: str,
        text_model: Optional[str] = None,
        base_url: str = "http://localhost:11434",
        chunk_minutes: float = 12.0,
        project_fps: float = 25.0,
        num_ctx: int = 8192,
        timeout_sec: int = 1800,
        merge_gap_sec: float = 0.4,
        creative_brief: str = "",
        target_duration_sec: float = 0.0,
        camera_profile: str = "sony_pp8_slog3_sgamut3cine",
        music_folder: Optional[os.PathLike] = None,
        music_analysis: Optional[os.PathLike] = None,
        session: Optional[Any] = None,
        logger: Optional[logging.Logger] = None,
    ) -> None:
        """Validate configuration without contacting or loading Ollama. / 校验配置，但不连接或加载 Ollama。"""
        if not model.strip():
            raise DirectorError("Ollama 模型名不能为空 / Ollama model is required.")
        if not 10.0 <= chunk_minutes <= 15.0:
            raise DirectorError(
                "chunk_minutes 必须在 10–15 分钟之间 / must be between 10 and 15."
            )
        if project_fps <= 0 or num_ctx < 2048 or timeout_sec <= 0:
            raise DirectorError(
                "FPS、num_ctx 或 timeout 配置无效 / Invalid FPS, num_ctx, or timeout."
            )
        if merge_gap_sec < 0:
            raise DirectorError(
                "merge_gap_sec 不能为负数 / cannot be negative."
            )
        if target_duration_sec < 0:
            raise DirectorError(
                "目标时长不能为负数 / target_duration_sec cannot be negative."
            )

        self.model = model.strip()
        self.text_model = str(text_model or model).strip()
        self.base_url = base_url.rstrip("/")
        self.chunk_duration_sec = chunk_minutes * 60.0
        self.project_fps = float(project_fps)
        self.num_ctx = int(num_ctx)
        self.timeout_sec = int(timeout_sec)
        self.merge_gap_sec = float(merge_gap_sec)
        self.creative_brief = " ".join(str(creative_brief or "").split())
        self.target_duration_sec = float(target_duration_sec)
        self.camera_profile = str(camera_profile or "").strip().casefold()
        if self.camera_profile not in {
            "sony_pp8_slog3_sgamut3cine", "rec709", "auto"
        }:
            raise DirectorError(
                "不支持的相机色彩配置 / Unsupported camera profile: "
                f"{camera_profile}"
            )
        self.music_folder = (
            Path(music_folder).expanduser().resolve() if music_folder else None
        )
        if self.music_folder is not None and not self.music_folder.is_dir():
            raise DirectorError(
                f"配乐目录不存在 / Music folder not found: {self.music_folder}"
            )
        self.music_analysis_path = (
            Path(music_analysis).expanduser().resolve() if music_analysis else None
        )
        if self.music_analysis_path is not None and not self.music_analysis_path.is_file():
            raise DirectorError(
                f"配乐分析文件不存在 / Music analysis not found: {self.music_analysis_path}"
            )
        self._music_analysis: Dict[str, Any] = {}
        self._active_treatment: Dict[str, Any] = {}
        self._active_target_duration_sec = 0.0
        self._music_files: List[Path] = []
        self.logger = logger or logging.getLogger(LOGGER_NAME)
        self._session = session

    @property
    def session(self) -> Any:
        """Create a requests session lazily. / 延迟创建 requests 会话。"""
        if self._session is None:
            try:
                import requests
            except ImportError as exc:
                raise DirectorError(
                    "缺少 requests。请执行 pip install -r requirements.txt。"
                    " / requests is missing. Run pip install -r requirements.txt."
                ) from exc
            self._session = requests.Session()
        return self._session

    def run(
        self,
        raw_data_path: os.PathLike,
        output_path: os.PathLike,
        proxy_file_name: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        Chunk raw data, query Ollama serially, merge, and write final JSON.
        分块原始数据、串行请求 Ollama、合并并写入最终 JSON。

        The model is unloaded in ``finally`` even if one chunk fails. No partial
        ``timeline_cuts.json`` is published.

        即使某个分块失败，也会在 ``finally`` 中卸载模型；不会发布不完整的
        ``timeline_cuts.json``。
        """
        raw_path = Path(raw_data_path).expanduser().resolve()
        destination = Path(output_path).expanduser().resolve()
        raw_data = self.load_raw_data(raw_path)
        if isinstance(raw_data.get("assets"), list):
            return self._run_multi_asset(raw_data, raw_path, destination)
        source_name = (
            proxy_file_name
            or str(raw_data.get("proxy_file_name") or "").strip()
            or Path(str(raw_data.get("source_video", ""))).name
        )
        if not source_name:
            raise DirectorError(
                "无法确定代理素材 file_name / Could not determine proxy file_name."
            )

        chunks = self.chunk_raw_data(raw_data)
        self.logger.info(
            "已将 %.1f 分钟素材划分为 %d 个 %.1f 分钟窗口 / Split %.1f minutes into %d windows of %.1f minutes",
            float(raw_data["duration_sec"]) / 60.0,
            len(chunks),
            self.chunk_duration_sec / 60.0,
            float(raw_data["duration_sec"]) / 60.0,
            len(chunks),
            self.chunk_duration_sec / 60.0,
        )

        all_decisions: List[Dict[str, Any]] = []
        try:
            self.check_ollama()
            for index, chunk in enumerate(chunks, start=1):
                self.logger.info(
                    "分析分块 %d/%d（%.1fs–%.1fs）/ Analyzing chunk %d/%d (%.1fs–%.1fs)",
                    index,
                    len(chunks),
                    chunk["start_sec"],
                    chunk["end_sec"],
                    index,
                    len(chunks),
                    chunk["start_sec"],
                    chunk["end_sec"],
                )
                response_payload = self.request_chunk(chunk, source_name)
                decisions = self.validate_chunk_decisions(
                    response_payload, chunk, source_name
                )
                all_decisions.extend(decisions)
                self.logger.info(
                    "分块 %d 产生 %d 条有效决策 / Chunk %d produced %d valid decisions",
                    index,
                    len(decisions),
                    index,
                    len(decisions),
                )
        finally:
            self.unload_model()

        merged = self.merge_decisions(all_decisions)
        if not merged:
            raise DirectorError(
                "AI 未生成任何有效剪辑决策；未写入 timeline_cuts.json。"
                " / AI produced no valid cut decisions; no output was written."
            )
        for index, decision in enumerate(merged, start=1):
            decision["clip_id"] = index

        output: Dict[str, Any] = {
            "schema_version": "1.0",
            "generated_at_utc": datetime.now(timezone.utc).isoformat(),
            "project_fps": self.project_fps,
            "source_raw_data": str(raw_path),
            "director_model": self.model,
            "chunk_duration_sec": self.chunk_duration_sec,
            "clips": merged,
        }
        self._atomic_write_json(output, destination)
        self.logger.info(
            "导演决策完成：%d 条片段 -> %s / Director complete: %d clips -> %s",
            len(merged),
            destination,
            len(merged),
            destination,
        )
        return output

    def revalidate_existing_plan(
        self,
        raw_data_path: os.PathLike,
        plan_path: os.PathLike,
    ) -> Dict[str, Any]:
        """
        Reapply deterministic story, overlap, runtime, and color gates.
        对已有计划重新应用确定性的叙事、重叠、时长与色彩守门。

        This recovery path uses ``candidate_audit`` and never contacts Ollama,
        making validator improvements cheap to apply after a long model pass.

        此恢复路径读取 ``candidate_audit``，不会连接 Ollama；长时间模型审片完成后，
        可低成本应用更新后的本地校验规则。
        """
        raw_path = Path(raw_data_path).expanduser().resolve()
        destination = Path(plan_path).expanduser().resolve()
        raw_data = self.load_raw_data(raw_path)
        assets = raw_data.get("assets")
        if not isinstance(assets, list):
            raise DirectorError(
                "重新校验只支持多素材 schema 3.0 / Revalidation requires a multi-asset plan."
            )
        try:
            payload = json.loads(destination.read_text(encoding="utf-8-sig"))
        except (OSError, ValueError) as exc:
            raise DirectorError(
                f"无法读取已有剪辑计划 / Cannot read existing plan: {exc}"
            ) from exc
        if not isinstance(payload, dict):
            raise DirectorError("已有剪辑计划格式无效 / Existing plan is invalid.")
        audit = payload.get("candidate_audit")
        if not isinstance(audit, list) or not audit:
            raise DirectorError(
                "已有计划没有 candidate_audit，必须重新运行 AI 导演。"
                " / Existing plan has no candidate audit; rerun the director."
            )
        self._active_target_duration_sec = self._finite_float(
            payload.get("target_duration_sec", self.target_duration_sec or 90),
            "target_duration_sec",
        )
        treatment_payload = payload.get("director_treatment")
        treatment = self.validate_treatment(
            treatment_payload if isinstance(treatment_payload, dict) else {},
            assets,
        )
        candidates = self.candidates_from_treatment(treatment, assets)
        candidates.extend(
            dict(item) for item in audit
            if isinstance(item, dict) and not item.get("protected_story_anchor")
        )
        candidates = self.merge_decisions(candidates)
        for index, candidate in enumerate(candidates, start=1):
            candidate["candidate_id"] = f"C{index:04d}"
        final_clips = self._remove_overlaps(candidates)
        global_look = {
            "clean_neutral": "neutral", "cinematic_warm": "warm",
            "cool_steel": "cool", "high_contrast": "contrast",
        }.get(str(treatment.get("creative_look") or ""), "neutral")
        for clip in final_clips:
            if str(clip.get("color_look") or "neutral") in {"source", "neutral"}:
                clip["color_look"] = global_look
        final_clips = self._fit_target_duration(final_clips, treatment)
        for index, clip in enumerate(final_clips, start=1):
            clip["clip_id"] = index
        payload.update(
            {
                "director_treatment": treatment,
                "target_duration_sec": self._active_target_duration_sec,
                "color_pipeline": self.build_color_pipeline(
                    treatment, assets, raw_data.get("color_match_plan")
                ),
                "candidate_count": len(candidates),
                "candidate_audit": candidates,
                "clips": final_clips,
                "revalidated_at_utc": datetime.now(timezone.utc).isoformat(),
            }
        )
        self._atomic_write_json(payload, destination)
        self.logger.info(
            "已有计划重新校验完成：%d 个镜头 / Existing plan revalidated: %d clips",
            len(final_clips), len(final_clips),
        )
        return payload

    def _run_multi_asset(
        self,
        raw_data: Dict[str, Any],
        raw_path: Path,
        destination: Path,
    ) -> Dict[str, Any]:
        """
        Run visual candidate selection followed by global story assembly.
        先执行视觉候选片段筛选，再进行全局故事编排。

        Every source/chunk is analyzed once with its transcript and actual JPEG
        frames.  A second constrained model call sees candidates from every
        asset and decides which clips to use and in what order.

        每个素材分块都会结合台词和真实 JPEG 画面分析一次；第二次受约束模型调用会
        看到全部素材的候选片段，并决定最终选用内容与跨素材顺序。
        """
        assets = raw_data["assets"]
        chunks: List[Dict[str, Any]] = []
        for asset_order, asset in enumerate(assets):
            asset_chunks = self.chunk_raw_data(asset)
            source_name = str(
                asset.get("proxy_file_name")
                or asset.get("source_video")
                or ""
            ).strip()
            if not source_name:
                raise DirectorError(
                    f"素材 {asset.get('asset_id')} 缺少媒体路径 / asset has no media path."
                )
            for chunk in asset_chunks:
                chunk["asset_id"] = str(asset["asset_id"])
                chunk["source_name"] = source_name
                chunk["source_video"] = str(asset.get("source_video") or "")
                chunk["asset_label"] = Path(
                    str(asset.get("source_video") or source_name)
                ).name
                chunk["source_order"] = asset_order
                chunks.append(chunk)

        checkpoint_path = destination.with_name(
            destination.stem + ".director-checkpoint.json"
        )
        checkpoint_fingerprint = self._checkpoint_fingerprint(raw_path)
        completed_chunks = self._load_director_checkpoint(
            checkpoint_path, checkpoint_fingerprint
        )
        if completed_chunks:
            self.logger.info(
                "已恢复 %d 个导演分块检查点；不会重复分析 / Resuming %d completed director chunks",
                len(completed_chunks),
                len(completed_chunks),
            )

        self.logger.info(
            "多素材视觉导演：%d 个视频，%d 个分块 / Multi-asset visual director: %d videos, %d chunks",
            len(assets),
            len(chunks),
            len(assets),
            len(chunks),
        )
        candidates: List[Dict[str, Any]] = []
        try:
            self.check_ollama(require_vision=True)
            self._music_analysis = self.load_music_analysis()
            analyzed_tracks = self._music_analysis.get("tracks", [])
            self._music_files = [
                Path(str(item.get("file_name"))).expanduser().resolve()
                for item in analyzed_tracks
                if isinstance(item, dict) and str(item.get("file_name") or "").strip()
            ] or self.discover_music_files()
            treatment_payload = self.request_treatment(assets)
            treatment = self.validate_treatment(treatment_payload, assets)
            self._active_treatment = treatment
            candidates.extend(self.candidates_from_treatment(treatment, assets))
            for index, chunk in enumerate(chunks, start=1):
                chunk_key = self._director_chunk_key(chunk)
                cached_decisions = completed_chunks.get(chunk_key)
                if isinstance(cached_decisions, list):
                    self.logger.info(
                        "复用视觉分析 %d/%d：%s / Reusing visual analysis %d/%d: %s",
                        index,
                        len(chunks),
                        chunk["asset_label"],
                        index,
                        len(chunks),
                        chunk["asset_label"],
                    )
                    candidates.extend(dict(item) for item in cached_decisions)
                    continue
                self.logger.info(
                    "视觉分析 %d/%d：%s %.1fs–%.1fs / Visual analysis %d/%d: %s %.1fs–%.1fs",
                    index,
                    len(chunks),
                    chunk["asset_label"],
                    chunk["start_sec"],
                    chunk["end_sec"],
                    index,
                    len(chunks),
                    chunk["asset_label"],
                    chunk["start_sec"],
                    chunk["end_sec"],
                )
                response_payload = self.request_chunk(
                    chunk,
                    str(chunk["source_name"]),
                    schema=CANDIDATE_SCHEMA,
                    include_images=True,
                    treatment=treatment,
                )
                decisions = self.validate_chunk_decisions(
                    response_payload,
                    chunk,
                    str(chunk["source_name"]),
                )
                for decision in decisions:
                    decision["asset_id"] = str(chunk["asset_id"])
                    decision["source_order"] = int(chunk["source_order"])
                candidates.extend(decisions)
                completed_chunks[chunk_key] = [dict(item) for item in decisions]
                self._write_director_checkpoint(
                    checkpoint_path,
                    checkpoint_fingerprint,
                    completed_chunks,
                )

            candidates = self.merge_decisions(candidates)
            candidate_limit = max(
                24,
                min(160, self._effective_num_ctx(self.text_model) // 128),
            )
            candidates = self._limit_candidates(
                candidates, limit=candidate_limit
            )
            for index, candidate in enumerate(candidates, start=1):
                candidate["candidate_id"] = f"C{index:04d}"
            if not candidates:
                raise DirectorError(
                    "视觉导演未找到任何可用片段 / Visual director found no usable clips."
                )
            if self.text_model.casefold() != self.model.casefold():
                self.logger.info(
                    "视觉候选完成，卸载 %s 后加载 72B 文字导演 %s / Switching from vision to text director",
                    self.model,
                    self.text_model,
                )
                self.unload_model(self.model)
                self.check_ollama(model=self.text_model)
            sequence_payload = self.request_sequence(candidates, assets, treatment)
            final_clips = self.validate_sequence(
                sequence_payload, candidates, treatment
            )
        finally:
            self.unload_model(self.model)
            if self.text_model.casefold() != self.model.casefold():
                self.unload_model(self.text_model)

        music_plan = self.validate_music_plan(sequence_payload.get("music_plan"))
        final_clips = self.snap_visual_cuts_to_beats(final_clips, music_plan, assets)
        color_pipeline = self.build_color_pipeline(
            treatment, assets, raw_data.get("color_match_plan")
        )
        color_sources = color_pipeline.get("sources", {})
        for index, clip in enumerate(final_clips, start=1):
            clip["clip_id"] = index
            source_color = color_sources.get(str(clip.get("asset_id") or ""), {})
            if isinstance(source_color, dict):
                clip["source_color"] = {
                    key: value for key, value in source_color.items()
                    if key != "color_match"
                }
                clip["color_match"] = dict(source_color.get("color_match") or {})
        output: Dict[str, Any] = {
            "schema_version": "3.0",
            "generated_at_utc": datetime.now(timezone.utc).isoformat(),
            "project_fps": self.project_fps,
            "source_raw_data": str(raw_path),
            "vision_model": self.model,
            "director_model": self.text_model,
            "chunk_duration_sec": self.chunk_duration_sec,
            "asset_count": len(assets),
            "candidate_count": len(candidates),
            "candidate_audit": candidates,
            "project_summary": str(sequence_payload.get("project_summary") or "").strip(),
            "director_treatment": treatment,
            "target_duration_sec": self._active_target_duration_sec,
            "color_pipeline": color_pipeline,
            "music_plan": music_plan,
            "effects_engine": {
                "review": "ffmpeg",
                "editable_timeline": "davinci_resolve",
            },
            "clips": final_clips,
        }
        self._atomic_write_json(output, destination)
        try:
            checkpoint_path.unlink(missing_ok=True)
        except OSError as exc:
            self.logger.warning(
                "无法删除已完成的导演检查点：%s / Could not remove completed director checkpoint: %s",
                exc,
                exc,
            )
        self.logger.info(
            "全局编排完成：从 %d 个候选中选出 %d 个片段 / Global assembly selected %d of %d candidates",
            len(candidates),
            len(final_clips),
            len(final_clips),
            len(candidates),
        )
        return output

    def load_raw_data(self, path: Path) -> Dict[str, Any]:
        """
        Read and validate the extractor's ``raw_data.json``.
        读取并校验提取层的 ``raw_data.json``。
        """
        if not path.is_file():
            raise DirectorError(f"找不到 raw_data.json / File not found: {path}")
        try:
            payload = json.loads(path.read_text(encoding="utf-8-sig"))
        except (OSError, json.JSONDecodeError) as exc:
            raise DirectorError(
                f"无法解析 raw_data.json / Cannot parse raw_data.json: {exc}"
            ) from exc
        if not isinstance(payload, dict):
            raise DirectorError(
                "raw_data.json 根节点必须是对象 / root must be an object."
            )
        assets = payload.get("assets")
        if isinstance(assets, list):
            if not assets:
                raise DirectorError(
                    "raw_data.json 的 assets 不能为空 / assets cannot be empty."
                )
            normalized_assets: List[Dict[str, Any]] = []
            seen_ids = set()
            for index, raw_asset in enumerate(assets):
                if not isinstance(raw_asset, dict):
                    raise DirectorError(
                        f"assets[{index}] 必须是对象 / must be an object."
                    )
                asset = dict(raw_asset)
                asset_id = str(asset.get("asset_id") or f"asset_{index + 1}").strip()
                if not asset_id or asset_id in seen_ids:
                    raise DirectorError(
                        f"assets[{index}].asset_id 缺失或重复 / missing or duplicate."
                    )
                seen_ids.add(asset_id)
                asset["asset_id"] = asset_id
                self._validate_asset_data(asset, f"assets[{index}]")
                for frame in asset.get("keyframes", []):
                    if not isinstance(frame, dict):
                        continue
                    image_path = str(frame.get("image_path") or "").strip()
                    if image_path:
                        frame["image_path"] = str(
                            Path(image_path).expanduser().resolve()
                        )
                        continue
                    raw_data_path = str(asset.get("raw_data_path") or "").strip()
                    frame_name = str(frame.get("file_name") or "").strip()
                    if raw_data_path and frame_name:
                        frame["image_path"] = str(
                            Path(raw_data_path).expanduser().resolve().parent
                            / "keyframes"
                            / frame_name
                        )
                normalized_assets.append(asset)
            payload["assets"] = normalized_assets
            payload["duration_sec"] = sum(
                float(asset["duration_sec"]) for asset in normalized_assets
            )
            return payload
        transcript = payload.get("transcript")
        if not isinstance(transcript, list) or not transcript:
            raise DirectorError(
                "raw_data.json 缺少非空 transcript / requires a non-empty transcript."
            )
        duration = self._finite_float(payload.get("duration_sec"), "duration_sec")
        if duration <= 0:
            raise DirectorError("duration_sec 必须大于 0 / must be positive.")

        previous_start = -1.0
        for index, segment in enumerate(transcript):
            if not isinstance(segment, dict):
                raise DirectorError(f"transcript[{index}] 必须是对象 / must be an object.")
            start = self._finite_float(
                segment.get("start_sec"), f"transcript[{index}].start_sec"
            )
            end = self._finite_float(
                segment.get("end_sec"), f"transcript[{index}].end_sec"
            )
            if start < 0 or end <= start or start < previous_start:
                raise DirectorError(
                    f"transcript[{index}] 时间范围无效或未排序 / invalid or unsorted."
                )
            if not str(segment.get("text", "")).strip():
                raise DirectorError(
                    f"transcript[{index}].text 不能为空 / cannot be empty."
                )
            previous_start = start
        payload["duration_sec"] = duration
        return payload

    def _validate_asset_data(self, asset: Dict[str, Any], prefix: str) -> None:
        """
        Validate one schema-v2 asset transcript and time range.
        校验 schema-v2 中一个素材的台词与时间范围。
        """
        transcript = asset.get("transcript")
        keyframes = asset.get("keyframes")
        if not isinstance(transcript, list):
            raise DirectorError(f"{prefix}.transcript 必须是数组 / must be an array.")
        if not isinstance(keyframes, list):
            asset["keyframes"] = []
            keyframes = []
        if not transcript and not keyframes:
            raise DirectorError(
                f"{prefix} 没有台词或关键帧 / has no transcript or keyframes."
            )
        duration = self._finite_float(asset.get("duration_sec"), f"{prefix}.duration_sec")
        if duration <= 0:
            raise DirectorError(f"{prefix}.duration_sec 必须大于 0 / must be positive.")
        previous_start = -1.0
        for index, segment in enumerate(transcript):
            if not isinstance(segment, dict):
                raise DirectorError(
                    f"{prefix}.transcript[{index}] 必须是对象 / must be an object."
                )
            start = self._finite_float(
                segment.get("start_sec"), f"{prefix}.transcript[{index}].start_sec"
            )
            end = self._finite_float(
                segment.get("end_sec"), f"{prefix}.transcript[{index}].end_sec"
            )
            if start < 0 or end <= start or start < previous_start:
                raise DirectorError(
                    f"{prefix}.transcript[{index}] 时间范围无效或未排序 / invalid or unsorted."
                )
            if not str(segment.get("text", "")).strip():
                raise DirectorError(
                    f"{prefix}.transcript[{index}].text 不能为空 / cannot be empty."
                )
            previous_start = start
        asset["duration_sec"] = duration

    def chunk_raw_data(self, raw_data: Dict[str, Any]) -> List[Dict[str, Any]]:
        """
        Partition transcript/keyframes into non-overlapping time windows.
        将台词和关键帧划分为互不重叠的时间窗口。

        Transcript segments are assigned by midpoint, ensuring every segment is
        sent exactly once and avoiding duplicated cuts at chunk boundaries.

        台词片段按中点归属，保证每条台词只发送一次，避免分块边界的重复剪辑。
        """
        duration = float(raw_data["duration_sec"])
        transcript = raw_data["transcript"]
        keyframes = raw_data.get("keyframes", [])
        if not isinstance(keyframes, list):
            keyframes = []

        chunks: List[Dict[str, Any]] = []
        start = 0.0
        index = 0
        while start < duration:
            end = min(duration, start + self.chunk_duration_sec)
            is_last = end >= duration
            chunk_segments = []
            for segment in transcript:
                midpoint = (
                    float(segment["start_sec"]) + float(segment["end_sec"])
                ) / 2.0
                if start <= midpoint < end or (is_last and midpoint == end):
                    chunk_segments.append(segment)
            chunk_keyframes = []
            for keyframe in keyframes:
                if not isinstance(keyframe, dict):
                    continue
                try:
                    timestamp = float(keyframe["timestamp_sec"])
                except (KeyError, TypeError, ValueError):
                    continue
                if start <= timestamp < end or (is_last and timestamp == end):
                    chunk_keyframes.append(keyframe)

            # Empty windows provide no editorial evidence and need no LLM call.
            if chunk_segments or chunk_keyframes:
                chunks.append(
                    {
                        "index": index,
                        "start_sec": round(start, 3),
                        "end_sec": round(end, 3),
                        "transcript": chunk_segments,
                        "keyframes": chunk_keyframes,
                    }
                )
                index += 1
            start = end
        if not chunks:
            raise DirectorError(
                "没有可分析的分块 / No analyzable chunks were produced."
            )
        return chunks

    def check_ollama(
        self, require_vision: bool = False, model: Optional[str] = None
    ) -> None:
        """
        Verify Ollama is reachable and the configured model is installed.
        验证 Ollama 可访问且已安装配置的模型。
        """
        url = self.base_url + "/api/tags"
        try:
            response = self.session.get(url, timeout=(5, 15))
            response.raise_for_status()
            payload = response.json()
        except Exception as exc:
            raise DirectorError(
                "无法连接本地 Ollama。请启动 Ollama，并确认地址为 "
                f"{self.base_url}。\nCannot reach local Ollama at {self.base_url}: {exc}"
            ) from exc

        models = payload.get("models", []) if isinstance(payload, dict) else []
        names = {
            str(item.get("name") or item.get("model") or "")
            for item in models
            if isinstance(item, dict)
        }
        selected_model = str(model or self.model).strip()
        requested_base = selected_model.split(":", 1)[0]
        normalized_names = {name.casefold() for name in names}
        installed = (
            selected_model.casefold() in normalized_names
            if ":" in selected_model
            else any(name.split(":", 1)[0].casefold() == requested_base.casefold() for name in names)
        )
        if not installed:
            available = ", ".join(sorted(name for name in names if name)) or "(none)"
            raise DirectorError(
                f"Ollama 中未找到模型 {selected_model!r}。请先执行：ollama pull {selected_model}\n"
                f"Model not installed. Available: {available}"
            )

        if require_vision:
            self._require_vision_model()

        # The project promises a single-model VRAM policy. Refuse to load the
        # director while an unrelated Ollama model is already resident.
        try:
            process_response = self.session.get(
                self.base_url + "/api/ps", timeout=(5, 15)
            )
            process_response.raise_for_status()
            process_payload = process_response.json()
        except Exception as exc:
            raise DirectorError(
                f"无法读取 Ollama 已加载模型 / Cannot inspect loaded Ollama models: {exc}"
            ) from exc
        loaded = {
            str(item.get("name") or item.get("model") or "")
            for item in (
                process_payload.get("models", [])
                if isinstance(process_payload, dict)
                else []
            )
            if isinstance(item, dict)
        }
        unrelated = {
            name
            for name in loaded
            if name
            and name != selected_model
            and name.split(":", 1)[0] != requested_base
        }
        if unrelated:
            commands = "；".join(f"ollama stop {name}" for name in sorted(unrelated))
            raise DirectorError(
                "检测到其他 Ollama 模型仍驻留内存，拒绝违反串行显存策略："
                f"{', '.join(sorted(unrelated))}。请先执行：{commands}\n"
                "Other Ollama models are loaded; stop them before continuing."
            )

    def _require_vision_model(self) -> None:
        """
        Verify that the selected Ollama model accepts image inputs.
        验证所选 Ollama 模型能够接收图像输入。
        """
        try:
            response = self.session.post(
                self.base_url + "/api/show",
                json={"model": self.model},
                timeout=(5, 30),
            )
            response.raise_for_status()
            payload = response.json()
        except Exception as exc:
            self.logger.warning(
                "无法读取 Ollama 模型能力，将按模型名称判断 / "
                "Could not inspect model capabilities: %s",
                exc,
            )
            payload = {}
        capabilities = payload.get("capabilities") if isinstance(payload, dict) else None
        if isinstance(capabilities, list):
            if "vision" in {str(value).casefold() for value in capabilities}:
                return
            raise DirectorError(
                f"模型 {self.model!r} 不支持看图，不能执行多视频视觉剪辑。"
                "请安装视觉模型，例如：ollama pull qwen3.5:35b-a3b\n"
                "The selected model has no vision capability. Install a vision model."
            )
        normalized = self.model.casefold().replace("_", "-")
        known_vision_markers = (
            "qwen3.5", "qwen2.5-vl", "gemma3", "llava", "minicpm-v",
            "llama3.2-vision", "moondream",
        )
        if not any(marker in normalized for marker in known_vision_markers):
            raise DirectorError(
                f"无法确认模型 {self.model!r} 支持视觉输入。请改用 qwen3.5 等视觉模型。\n"
                "Cannot confirm vision support for the selected model."
            )

    def request_chunk(
        self,
        chunk: Dict[str, Any],
        source_name: str,
        schema: Optional[Dict[str, Any]] = None,
        include_images: bool = False,
        treatment: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """
        Request one non-streaming, schema-constrained Ollama completion.
        请求一次非流式、受 JSON Schema 约束的 Ollama 生成。
        """
        selected_frames = (
            self._select_keyframes(chunk.get("keyframes", []), limit=12)
            if include_images
            else list(chunk.get("keyframes", []))
        )
        images: List[str] = []
        if include_images:
            selected_frames, images = self._encode_images(selected_frames)
        prompt_chunk = dict(chunk)
        prompt_chunk["keyframes"] = selected_frames
        active_schema = schema or DECISION_SCHEMA
        prompt = self.build_prompt(
            prompt_chunk, source_name, active_schema, treatment=treatment
        )
        return self._request_json(prompt, active_schema, images)

    def _request_json(
        self,
        prompt: str,
        schema: Dict[str, Any],
        images: Sequence[str] = (),
        model: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        Send one schema-constrained Ollama request with optional real images.
        发送一次受 Schema 约束、可包含真实图片的 Ollama 请求。
        """
        selected_model = str(model or self.model).strip()
        normalized_model = selected_model.casefold()
        request_num_ctx = self._effective_num_ctx(selected_model)
        # Ollama accepts low/medium/high only for GPT-OSS. Qwen and other
        # thinking models use a boolean. Supplying "high" to Qwen can consume
        # the whole generation budget in the separate `thinking` field and
        # leave the structured `response` empty.
        # Ollama 仅允许 GPT-OSS 使用 low/medium/high；Qwen 等模型使用布尔值。
        # 向 Qwen 发送 "high" 可能让 thinking 耗尽预算，导致 response 为空。
        quality_think: Any = "high" if "gpt-oss" in normalized_model else True
        direct_think: Any = "low" if "gpt-oss" in normalized_model else False
        num_predict = max(1024, min(4096, request_num_ctx // 4))
        attempts = (
            ("quality", schema, quality_think),
            ("direct-json", schema, direct_think),
            ("compatibility-json", "json", direct_think),
        )
        url = self.base_url + "/api/generate"
        last_issue = "unknown response"
        for attempt_index, (label, output_format, think_value) in enumerate(
            attempts, start=1
        ):
            payload: Dict[str, Any] = {
                "model": selected_model,
                "system": (
                    "You are a senior documentary editor and visual storyteller. "
                    "Use only supplied transcript, timestamps, and images. Return "
                    "only the requested JSON and never invent content."
                ),
                "prompt": prompt,
                "stream": False,
                "format": output_format,
                "think": think_value,
                "keep_alive": "10m",
                "options": {
                    "temperature": 0,
                    "seed": 42,
                    "num_ctx": request_num_ctx,
                    "num_predict": num_predict,
                },
            }
            if images:
                payload["images"] = list(images)
            try:
                response = self.session.post(
                    url, json=payload, timeout=(10, self.timeout_sec)
                )
                status_code = int(getattr(response, "status_code", 0) or 0)
                if status_code in {400, 422} and attempt_index < len(attempts):
                    last_issue = f"HTTP {status_code} ({label})"
                    self.logger.warning(
                        "Ollama 拒绝 %s 请求（HTTP %d），切换兼容模式 / "
                        "Ollama rejected %s request (HTTP %d); retrying compatibly",
                        label,
                        status_code,
                        label,
                        status_code,
                    )
                    continue
                response.raise_for_status()
                envelope = response.json()
            except Exception as exc:
                raise DirectorError(
                    f"Ollama 分块请求失败 / Ollama chunk request failed: {exc}"
                ) from exc

            if not isinstance(envelope, dict) or not envelope.get("done", False):
                last_issue = "incomplete response"
            else:
                generated = envelope.get("response")
                if isinstance(generated, str) and generated.strip():
                    try:
                        return self.parse_generated_json(generated)
                    except DirectorError as exc:
                        last_issue = str(exc)
                else:
                    last_issue = "empty response"

            if attempt_index < len(attempts):
                self.logger.warning(
                    "Ollama 未返回可用 JSON（模式=%s, done_reason=%s, prompt_tokens=%s, output_tokens=%s, "
                    "thinking_chars=%d）；关闭显式思考并重试 / Ollama returned no usable JSON; retrying without explicit thinking",
                    label,
                    envelope.get("done_reason") if isinstance(envelope, dict) else None,
                    envelope.get("prompt_eval_count") if isinstance(envelope, dict) else None,
                    envelope.get("eval_count") if isinstance(envelope, dict) else None,
                    len(str(envelope.get("thinking") or "")) if isinstance(envelope, dict) else 0,
                )

        raise DirectorError(
            "Ollama 连续三次未返回可解析的结构化 JSON"
            f"（最后错误：{last_issue}）。请减少候选数量或重试。 / "
            "Ollama failed to return parseable structured JSON after three attempts "
            f"(last issue: {last_issue}). Reduce candidates or retry."
        )

    def _effective_num_ctx(self, model: str) -> int:
        """
        Cap 70B/72B KV cache on 64GB-class mixed-memory systems.
        在 64GB 级混合内存设备上限制 70B/72B 的 KV Cache。

        Quantization quality is unchanged; only request context allocation is
        bounded. Compact global candidates fit in 8K while leaving memory for
        Windows, Ollama runtime state, and GPU spill.
        量化精度不变，仅限制请求上下文；紧凑候选可容纳于 8K，并给 Windows、
        Ollama 运行状态与显存回退保留内存。
        """
        normalized = str(model or "").casefold()
        return min(self.num_ctx, 8192) if any(
            marker in normalized for marker in ("70b", "72b")
        ) else self.num_ctx

    def build_prompt(
        self,
        chunk: Dict[str, Any],
        source_name: str,
        schema: Optional[Dict[str, Any]] = None,
        treatment: Optional[Dict[str, Any]] = None,
    ) -> str:
        """
        Build a compact prompt grounded by absolute timestamps and schema.
        构建由绝对时间戳和 Schema 约束的紧凑提示词。
        """
        transcript_lines = [
            "[{:.3f}-{:.3f}] {}".format(
                float(item["start_sec"]),
                float(item["end_sec"]),
                " ".join(str(item["text"]).split()),
            )
            for item in chunk["transcript"]
        ]
        keyframe_lines = [
            "IMAGE_{} [{:.3f}s] scene_score={} file={}".format(
                index,
                float(item.get("timestamp_sec", 0)),
                item.get("scene_score", "unknown"),
                item.get("file_name", ""),
            )
            for index, item in enumerate(chunk["keyframes"], start=1)
        ]
        schema_text = json.dumps(schema or DECISION_SCHEMA, ensure_ascii=False)
        treatment_text = json.dumps(
            treatment or {}, ensure_ascii=False, separators=(",", ":")
        )
        return (
            "Analyze only this source-video window: "
            "{start:.3f}s to {end:.3f}s.\n"
            "Follow the DIRECTOR TREATMENT below. Select only concise, coherent "
            "ranges that actively serve its theme and beats. Use "
            "absolute seconds in this source, not time relative to the chunk. "
            "Inspect every attached image in the listed IMAGE order. Prefer complete "
            "sentences, expressive visuals, stable/focused shots, meaningful B-roll, "
            "and authentic moments; reject dead air, repetition, camera setup, severe "
            "shake, accidental frames, and unusable audio. Suggest restrained effects "
            "only from the schema enums. Stabilize only genuinely shaky shots; request "
            "Magic Mask tracking only when a clear subject benefits from it. DRX names "
            "refer to optional user-exported Resolve presets and should be 'none' unless "
            "the look is clearly justified. Strong audio cleanup is for visibly/noisily "
            "problematic speech; transitions should serve the story, not decorate it. "
            "Every range must remain inside this window, cut_out_sec must be "
            "greater than cut_in_sec, and each candidate must be no longer than "
            "dynamic duration limits: B-roll 10s, bridges 12s, context/opening 20s, "
            "climax 25s, closing 30s, and complete interview thoughts up to 45s. "
            "Do not keep an entire setup conversation. An empty "
            "decisions array is preferred when this source adds no new story value.\n"
            "Source order in the shoot: {source_order}. Preserve production chronology.\n"
            "Source/proxy media: {source}\n"
            "DIRECTOR TREATMENT: {treatment}\n"
            "Required JSON schema: {schema}\n\n"
            "TRANSCRIPT:\n{transcript}\n\nKEYFRAMES:\n{keyframes}"
        ).format(
            start=float(chunk["start_sec"]),
            end=float(chunk["end_sec"]),
            source=source_name,
            source_order=int(chunk.get("source_order", 0)),
            treatment=treatment_text,
            schema=schema_text,
            transcript="\n".join(transcript_lines) or "(none)",
            keyframes="\n".join(keyframe_lines) or "(none)",
        )

    def request_treatment(
        self, assets: Sequence[Dict[str, Any]]
    ) -> Dict[str, Any]:
        """
        Create one project-wide director treatment before selecting shots.
        在选择镜头前生成一份覆盖全项目的导演阐述。

        Parameters / 参数:
            assets: Validated source assets in their real shooting order.
                按真实拍摄顺序排列的已校验素材。
        """
        total_duration = sum(float(item.get("duration_sec", 0)) for item in assets)
        automatic_target = min(180.0, max(45.0, total_duration * 0.12))
        requested_target = self.target_duration_sec or automatic_target
        self._active_target_duration_sec = round(requested_target, 1)
        compact_assets: List[Dict[str, Any]] = []
        for source_order, asset in enumerate(assets):
            transcript = " ".join(
                "[{:.1f}-{:.1f}] {}".format(
                    float(segment.get("start_sec", 0)),
                    float(segment.get("end_sec", 0)),
                    " ".join(str(segment.get("text", "")).split()),
                )
                for segment in asset.get("transcript", [])
                if isinstance(segment, dict)
            )
            compact_assets.append(
                {
                    "source_order": source_order,
                    "asset_id": asset.get("asset_id", ""),
                    "file": Path(str(asset.get("source_video") or "")).name,
                    "duration_sec": round(float(asset.get("duration_sec", 0)), 2),
                    "transcript": transcript[:5000],
                    "keyframe_count": len(asset.get("keyframes", [])),
                }
            )
        brief = self.creative_brief or (
            "Discover the strongest truthful theme in the footage. Make a concise "
            "director-led short with a clear setup, development, payoff, and ending."
        )
        prompt = (
            "Create the director treatment before any shot selection. The sources "
            "are listed in real shooting order. Default to strict chronology; use a "
            "very short teaser only if it materially improves comprehension. Do not "
            "make a chronological dump: identify one central theme, reject setup "
            "chatter that does not serve it, and design a complete emotional arc. "
            "Choose 4-8 story_anchors from the supplied exact asset_id and absolute "
            "timestamps. Anchors must cover at least three beats and at least three "
            "different sources when the footage supports it. Use dynamic duration: "
            "1.5-10 seconds for B-roll, up to 20 seconds for context, and up to 45 "
            "seconds for an uninterrupted complete spoken thought; never cross an "
            "asset duration. Include a complete "
            "ending action rather than a one-word tail. "
            "Use the exact requested target duration (within one second). The camera "
            "profile is technical input metadata, not a creative look. Return JSON only.\n"
            f"USER CREATIVE BRIEF: {brief}\n"
            f"REQUESTED TARGET DURATION: {self._active_target_duration_sec:.1f} seconds\n"
            f"CAMERA PROFILE: {self.camera_profile}\n"
            f"AVAILABLE LOCAL MUSIC: {json.dumps([p.name for p in self._music_files], ensure_ascii=False)}\n"
            f"SCHEMA: {json.dumps(TREATMENT_SCHEMA, ensure_ascii=False)}\n"
            f"SOURCES: {json.dumps(compact_assets, ensure_ascii=False)}"
        )
        self.logger.info(
            "正在生成导演阐述与叙事弧线 / Creating director treatment and story arc"
        )
        return self._request_json(prompt, TREATMENT_SCHEMA)

    def validate_treatment(
        self, payload: Dict[str, Any], assets: Sequence[Dict[str, Any]]
    ) -> Dict[str, Any]:
        """
        Validate treatment text and enforce the locally requested runtime.
        校验导演阐述，并强制采用本地请求的成片时长。
        """
        if not isinstance(payload, dict):
            raise DirectorError("导演阐述必须是对象 / Treatment must be an object.")
        required_text = (
            "title", "logline", "central_theme", "opening_beat",
            "development_beat", "payoff_beat", "ending_beat",
            "color_intent", "music_mood", "music_energy_arc",
        )
        treatment: Dict[str, Any] = {}
        for key in required_text:
            value = " ".join(str(payload.get(key) or "").split())
            if not value:
                raise DirectorError(
                    f"导演阐述缺少 {key} / Treatment is missing {key}."
                )
            treatment[key] = value
        policy = str(payload.get("chronology_policy") or "").casefold()
        if policy not in {"strict_chronological", "teaser_then_chronological"}:
            policy = "strict_chronological"
        # The default product promise is a time-flow edit. A non-linear teaser is
        # accepted only when the user explicitly asks for one in the creative brief.
        if "teaser" not in self.creative_brief.casefold() and "预告" not in self.creative_brief:
            policy = "strict_chronological"
        treatment["chronology_policy"] = policy
        treatment["target_duration_sec"] = self._active_target_duration_sec
        look = str(payload.get("creative_look") or "clean_neutral").casefold()
        look = look if look in {
            "clean_neutral", "cinematic_warm", "cool_steel", "high_contrast"
        } else "clean_neutral"
        color_language = treatment["color_intent"].casefold()
        if any(token in color_language for token in ("gold", "golden", "warm", "金色", "暖")):
            look = "cinematic_warm"
        elif any(token in color_language for token in ("steel", "cool", "冷", "钢")):
            look = "cool_steel"
        elif any(token in color_language for token in ("high contrast", "高对比")):
            look = "high_contrast"
        treatment["creative_look"] = look
        rules = payload.get("editorial_rules")
        treatment["editorial_rules"] = [
            " ".join(str(value).split()) for value in (rules if isinstance(rules, list) else [])
            if str(value).strip()
        ][:8] or [
            "Every selected shot must serve the central theme.",
            "Preserve real production chronology and finish on the payoff.",
        ]
        treatment["source_count"] = len(assets)
        by_id = {str(asset.get("asset_id")): asset for asset in assets}
        anchors: List[Dict[str, Any]] = []
        raw_anchors = payload.get("story_anchors")
        for raw_anchor in raw_anchors if isinstance(raw_anchors, list) else []:
            if not isinstance(raw_anchor, dict):
                continue
            asset_id = str(raw_anchor.get("asset_id") or "").strip()
            asset = by_id.get(asset_id)
            if asset is None:
                continue
            try:
                cut_in = self._finite_float(raw_anchor.get("cut_in_sec"), "story_anchor.in")
                cut_out = self._finite_float(raw_anchor.get("cut_out_sec"), "story_anchor.out")
            except DirectorError:
                continue
            duration = float(asset.get("duration_sec", 0))
            cut_in = max(0.0, min(duration, cut_in))
            beat = str(raw_anchor.get("beat") or "").casefold()
            anchor_role = "closing" if beat == "ending" else "interview"
            cut_out = max(
                cut_in,
                min(duration, cut_out, cut_in + self._max_candidate_duration(anchor_role, True)),
            )
            reason = " ".join(str(raw_anchor.get("reason") or "").split())
            if cut_out - cut_in < 1.5 or beat not in {
                "opening", "development", "payoff", "ending"
            } or not reason:
                continue
            anchors.append({
                "asset_id": asset_id,
                "cut_in_sec": round(cut_in, 3),
                "cut_out_sec": round(cut_out, 3),
                "beat": beat,
                "reason": reason,
            })
        if len(anchors) < 3:
            anchors = self._fallback_story_anchors(assets)
            self.logger.warning(
                "AI 导演锚点不足，已按台词时间流补足 / Treatment anchors were incomplete; added chronological transcript anchors"
            )
        if anchors:
            source_order_by_id = {
                str(asset.get("asset_id")): source_order
                for source_order, asset in enumerate(assets)
            }
            latest_index = max(
                range(len(anchors)),
                key=lambda index: (
                    source_order_by_id.get(str(anchors[index]["asset_id"]), -1),
                    float(anchors[index]["cut_in_sec"]),
                ),
            )
            for index, anchor in enumerate(anchors):
                if anchor["beat"] == "ending" and index != latest_index:
                    anchor["beat"] = "development"
            anchors[latest_index]["beat"] = "ending"
            latest_asset = by_id[str(anchors[latest_index]["asset_id"])]
            latest_duration = float(latest_asset.get("duration_sec", 0))
            latest_anchor = anchors[latest_index]
            if float(latest_anchor["cut_out_sec"]) - float(latest_anchor["cut_in_sec"]) < 5.0:
                expanded_out = min(
                    latest_duration, float(latest_anchor["cut_in_sec"]) + 5.0
                )
                expanded_in = max(0.0, expanded_out - 5.0)
                latest_anchor["cut_in_sec"] = round(expanded_in, 3)
                latest_anchor["cut_out_sec"] = round(expanded_out, 3)
        treatment["story_anchors"] = anchors[:12]
        return treatment

    def _fallback_story_anchors(
        self, assets: Sequence[Dict[str, Any]]
    ) -> List[Dict[str, Any]]:
        """Build safe chronological anchors when model anchors are invalid. / 模型锚点无效时构建安全时间流锚点。"""
        usable = [
            asset for asset in assets
            if isinstance(asset.get("transcript"), list) and asset.get("transcript")
        ]
        if not usable:
            return []
        positions = sorted({0, len(usable) // 3, (2 * len(usable)) // 3, len(usable) - 1})
        beats = ("opening", "development", "payoff", "ending")
        anchors: List[Dict[str, Any]] = []
        for beat, position in zip(beats, positions):
            asset = usable[position]
            segments = [item for item in asset.get("transcript", []) if isinstance(item, dict)]
            start = max(0.0, float(segments[0].get("start_sec", 0)))
            end = min(float(asset.get("duration_sec", 0)), start + 8.0)
            if end - start >= 1.5:
                anchors.append({
                    "asset_id": str(asset.get("asset_id") or ""),
                    "cut_in_sec": round(start, 3),
                    "cut_out_sec": round(end, 3),
                    "beat": beat,
                    "reason": f"Chronological {beat} fallback grounded in source transcript.",
                })
        return anchors

    def candidates_from_treatment(
        self,
        treatment: Dict[str, Any],
        assets: Sequence[Dict[str, Any]],
    ) -> List[Dict[str, Any]]:
        """Convert global treatment anchors into protected edit candidates. / 将全局导演锚点转换为受保护候选。"""
        by_id = {
            str(asset.get("asset_id")): (source_order, asset)
            for source_order, asset in enumerate(assets)
        }
        role_map = {
            "opening": "opening", "development": "context",
            "payoff": "climax", "ending": "closing",
        }
        candidates: List[Dict[str, Any]] = []
        for anchor in treatment.get("story_anchors", []):
            match = by_id.get(str(anchor.get("asset_id") or ""))
            if match is None:
                continue
            source_order, asset = match
            beat = str(anchor.get("beat") or "development")
            source = str(asset.get("proxy_file_name") or asset.get("source_video") or "")
            candidates.append({
                "file_name": source,
                "asset_id": str(asset.get("asset_id") or ""),
                "source_order": source_order,
                "cut_in_sec": float(anchor["cut_in_sec"]),
                "cut_out_sec": float(anchor["cut_out_sec"]),
                "reason_for_cut": str(anchor.get("reason") or "Treatment anchor"),
                "visual_summary": str(anchor.get("reason") or "Treatment anchor"),
                "story_role": role_map.get(beat, "context"),
                "confidence": 0.78,
                "quality_score": 0.78,
                "transition_to_next": "cut",
                "transition_duration_sec": 0.0,
                "audio_cleanup": "light",
                "color_look": "neutral",
                "motion": "static",
                "volume_db": 0.0,
                "drx_preset": "none",
                "stabilization": "none",
                "tracking": "none",
                "smart_reframe": False,
                "protected_story_anchor": True,
                "treatment_beat": beat,
            })
        return candidates

    def discover_music_files(self) -> List[Path]:
        """List supported local, user-owned music files. / 列出支持的本地用户配乐文件。"""
        if self.music_folder is None:
            return []
        allowed = {".wav", ".mp3", ".flac", ".m4a", ".aac", ".ogg"}
        return sorted(
            (path.resolve() for path in self.music_folder.rglob("*")
             if path.is_file() and path.suffix.casefold() in allowed),
            key=lambda path: str(path).casefold(),
        )[:200]

    def build_color_pipeline(
        self,
        treatment: Dict[str, Any],
        assets: Sequence[Dict[str, Any]] = (),
        color_match_plan: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """
        Build per-source technical transforms plus bounded matching corrections.
        构建逐素材技术变换与受限的曝光/白平衡匹配。

        Sony XML always wins. The legacy camera-profile option is used only as
        an explicit fallback when sidecar metadata is missing.
        Sony XML 始终优先；仅在伴随元数据缺失时才采用显式旧版相机配置作为回退。
        """
        matches = color_match_plan if isinstance(color_match_plan, dict) else {}
        corrections = matches.get("assets") if isinstance(matches.get("assets"), dict) else {}
        source_map: Dict[str, Any] = {}
        for asset in assets:
            asset_id = str(asset.get("asset_id") or "").strip()
            if not asset_id:
                continue
            detected = asset.get("source_color")
            source = dict(detected) if isinstance(detected, dict) else {}
            if not bool(source.get("transform_supported")):
                if self.camera_profile == "sony_pp8_slog3_sgamut3cine":
                    source.update(
                        {
                            "camera_profile": "sony_slog3_sgamut3cine",
                            "resolve_input_color_space": "Sony S-Gamut3.Cine",
                            "resolve_input_gamma": "S-Log3",
                            "transform_supported": True,
                            "is_log": True,
                            "source": "explicit_fallback",
                        }
                    )
                elif self.camera_profile == "rec709":
                    source.update(
                        {
                            "camera_profile": "rec709",
                            "resolve_input_color_space": "Rec.709",
                            "resolve_input_gamma": "Gamma 2.4",
                            "transform_supported": True,
                            "is_log": False,
                            "source": "explicit_fallback",
                        }
                    )
            source_map[asset_id] = {
                "asset_id": asset_id,
                "file_name": str(asset.get("proxy_file_name") or asset.get("source_video") or ""),
                "camera_profile": str(source.get("camera_profile") or "unknown"),
                "capture_gamma": str(source.get("capture_gamma") or ""),
                "capture_primaries": str(source.get("capture_primaries") or ""),
                "resolve_input_color_space": str(source.get("resolve_input_color_space") or ""),
                "resolve_input_gamma": str(source.get("resolve_input_gamma") or ""),
                "transform_supported": bool(source.get("transform_supported")),
                "is_log": bool(source.get("is_log")),
                "metadata_source": str(source.get("source") or "missing"),
                "sidecar_path": str(source.get("sidecar_path") or ""),
                "color_match": dict(corrections.get(asset_id, {}))
                if isinstance(corrections.get(asset_id), dict) else {},
            }
        return {
            "mode": "per_source",
            "camera_profile": "mixed_or_detected",
            "enabled": any(bool(item.get("is_log")) for item in source_map.values()),
            "default_input_color_space": "Rec.709",
            "default_input_gamma": "Gamma 2.4",
            "timeline_color_space": "DaVinci WG",
            "timeline_gamma": "DaVinci Intermediate",
            "output_color_space": "Rec.709",
            "output_gamma": "Gamma 2.4",
            "creative_look": treatment.get("creative_look", "clean_neutral"),
            "color_intent": treatment.get("color_intent", ""),
            "matching": matches,
            "sources": source_map,
        }

    def load_music_analysis(self) -> Dict[str, Any]:
        """Load CPU beat-analysis output without importing librosa here. / 读取 CPU 鼓点分析结果。"""
        if self.music_analysis_path is None:
            return {}
        try:
            payload = json.loads(self.music_analysis_path.read_text(encoding="utf-8-sig"))
        except (OSError, json.JSONDecodeError) as exc:
            raise DirectorError(f"无法读取配乐分析 / Cannot read music analysis: {exc}") from exc
        if not isinstance(payload, dict) or not isinstance(payload.get("tracks"), list):
            raise DirectorError("配乐分析格式无效 / Invalid music-analysis schema.")
        return payload

    def validate_music_plan(self, payload: Any) -> Dict[str, Any]:
        """Resolve a model-selected track only within the supplied library. / 仅在用户配乐库中解析模型选择。"""
        value = payload if isinstance(payload, dict) else {}
        requested = str(value.get("track_file") or "").strip()
        selected = next(
            (path for path in self._music_files
             if requested.casefold() in {path.name.casefold(), str(path).casefold()}),
            None,
        )
        if requested and selected is None:
            self.logger.warning(
                "AI 选择了配乐库之外的文件 %r，已安全忽略 / Ignoring music outside the supplied library",
                requested,
            )
        analyzed = next(
            (
                item for item in self._music_analysis.get("tracks", [])
                if isinstance(item, dict) and selected is not None
                and str(item.get("file_name") or "").casefold() == str(selected).casefold()
            ),
            {},
        )
        return {
            "file_name": str(selected) if selected else "",
            "reason": " ".join(str(value.get("reason") or "").split()),
            "target_level_db": round(min(-6.0, max(-36.0, float(value.get("target_level_db", -20)))), 1),
            "fade_in_sec": round(min(10.0, max(0.0, float(value.get("fade_in_sec", 2)))), 2),
            "fade_out_sec": round(min(10.0, max(0.0, float(value.get("fade_out_sec", 3)))), 2),
            "duck_dialogue": value.get("duck_dialogue", True) is True,
            "tempo_bpm": float(analyzed.get("tempo_bpm", 0) or 0),
            "duration_sec": float(analyzed.get("duration_sec", 0) or 0),
            "beats_sec": [
                round(float(beat), 4) for beat in analyzed.get("beats_sec", [])
                if isinstance(beat, (int, float)) and float(beat) >= 0
            ],
            "license": str(analyzed.get("license") or ("user-supplied" if selected else "")),
            "license_url": str(analyzed.get("license_url") or ""),
            "license_provenance": str(analyzed.get("license_provenance") or ""),
        }

    def snap_visual_cuts_to_beats(
        self,
        clips: Sequence[Dict[str, Any]],
        music_plan: Dict[str, Any],
        assets: Sequence[Dict[str, Any]],
        max_shift_sec: float = 0.25,
    ) -> List[Dict[str, Any]]:
        """
        Nudge visual-only out-points to nearby beats while preserving source bounds.
        在不越过素材边界的前提下，把纯画面出点轻微吸附到邻近鼓点。

        Dialogue and closing thoughts are never time-warped or truncated for a beat.
        对话与结尾语义绝不会为了卡点被截断或变速。
        """
        beats = [float(value) for value in music_plan.get("beats_sec", []) if isinstance(value, (int, float))]
        track_duration = float(music_plan.get("duration_sec", 0) or 0)
        if not beats or track_duration <= 0:
            return [dict(item) for item in clips]
        total_duration = sum(
            max(0.0, float(item.get("cut_out_sec", 0)) - float(item.get("cut_in_sec", 0)))
            for item in clips
        )
        absolute_beats: List[float] = []
        loop = 0
        while loop * track_duration <= total_duration + max_shift_sec:
            absolute_beats.extend(loop * track_duration + beat for beat in beats)
            loop += 1
        asset_duration = {
            str(asset.get("asset_id") or ""): float(asset.get("duration_sec", 0) or 0)
            for asset in assets
        }
        result: List[Dict[str, Any]] = []
        timeline_cursor = 0.0
        snap_roles = {"opening", "broll", "bridge", "climax"}
        for original in clips:
            item = dict(original)
            duration = float(item.get("cut_out_sec", 0)) - float(item.get("cut_in_sec", 0))
            proposed_end = timeline_cursor + duration
            if str(item.get("story_role") or "").casefold() in snap_roles and absolute_beats:
                nearest = min(absolute_beats, key=lambda beat: abs(beat - proposed_end))
                shift = nearest - proposed_end
                source_end = float(item.get("cut_out_sec", 0)) + shift
                maximum = asset_duration.get(str(item.get("asset_id") or ""), source_end)
                new_duration = duration + shift
                if abs(shift) <= max_shift_sec and new_duration >= 0.4 and source_end <= maximum + 1e-6:
                    item["cut_out_sec"] = round(source_end, 3)
                    item["beat_snap"] = {
                        "timeline_beat_sec": round(nearest, 4),
                        "shift_sec": round(shift, 4),
                    }
                    duration = new_duration
            result.append(item)
            timeline_cursor += max(0.0, duration)
        return result

    def request_sequence(
        self,
        candidates: Sequence[Dict[str, Any]],
        assets: Sequence[Dict[str, Any]],
        treatment: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """
        Ask the model to choose and order candidates across every source.
        让模型跨全部素材选择并排序候选片段。
        """
        compact_candidates = []
        for item in candidates:
            compact_candidates.append(
                {
                    "candidate_id": item["candidate_id"],
                    "asset_id": item.get("asset_id", ""),
                    "source_order": item.get("source_order", 0),
                    "source": Path(str(item["file_name"])).name,
                    "in": item["cut_in_sec"],
                    "out": item["cut_out_sec"],
                    "story_role": item.get("story_role", "context"),
                    "visual_summary": item.get("visual_summary", ""),
                    "reason": item.get("reason_for_cut", ""),
                    "confidence": item.get("confidence", 0.5),
                    "quality_score": item.get("quality_score", 0.5),
                    "suggested_transition": item.get("transition_to_next", "cut"),
                    "suggested_audio": item.get("audio_cleanup", "light"),
                    "suggested_look": item.get("color_look", "neutral"),
                    "suggested_motion": item.get("motion", "static"),
                    "suggested_volume_db": item.get("volume_db", 0.0),
                    "suggested_drx": item.get("drx_preset", "none"),
                    "suggested_stabilization": item.get("stabilization", "none"),
                    "suggested_tracking": item.get("tracking", "none"),
                    "suggested_smart_reframe": item.get("smart_reframe", False),
                }
            )
        asset_names = [
            {
                "source_order": source_order,
                "asset_id": asset.get("asset_id", ""),
                "file": Path(str(asset.get("source_video") or "")).name,
                "duration_sec": asset.get("duration_sec", 0),
            }
            for source_order, asset in enumerate(assets)
        ]
        treatment = treatment or self._active_treatment
        music_choices = [
            {
                "track_file": Path(str(item.get("file_name") or "")).name,
                "title": item.get("title", ""),
                "mood": item.get("mood", ""),
                "tags": item.get("tags", []),
                "tempo_bpm": item.get("tempo_bpm", 0),
                "license": item.get("license", ""),
            }
            for item in self._music_analysis.get("tracks", [])
            if isinstance(item, dict)
        ] or [{"track_file": path.name} for path in self._music_files]
        prompt = (
            "You have already inspected representative frames and transcripts "
            "from every source video. Build one coherent documentary edit from "
            "the candidate list below. Select only useful candidate_id values, "
            "never invent or duplicate an id. Follow the treatment. Preserve the "
            "real source_order and in-file timestamp chronology; narrative quality "
            "must come from selection and juxtaposition, not scrambling the shoot. "
            "Establish context, develop the story, "
            "use B-roll to cover or bridge speech, avoid repetitive points, and "
            "finish deliberately. Preserve complete thoughts. Choose restrained "
            "transitions and effects from the schema; default to hard cuts, use "
            "cross dissolves for genuine time/mood changes, and fade_black only "
            "for major chapter endings. Keep the sum of selected clip durations "
            f"between {self._active_target_duration_sec * 0.85:.1f} and "
            f"{self._active_target_duration_sec * 1.10:.1f} seconds. Select only "
            "the strongest minority of candidates; never include everything. For "
            "music_plan.track_file choose exactly one AVAILABLE MUSIC filename or "
            "an empty string when no music is supplied. Return JSON only.\n"
            f"Required JSON schema: {json.dumps(SEQUENCE_SCHEMA, ensure_ascii=False)}\n"
            f"DIRECTOR TREATMENT:\n{json.dumps(treatment, ensure_ascii=False)}\n"
            f"AVAILABLE MUSIC:\n{json.dumps(music_choices, ensure_ascii=False)}\n"
            f"ASSETS:\n{json.dumps(asset_names, ensure_ascii=False)}\n"
            f"CANDIDATES:\n{json.dumps(compact_candidates, ensure_ascii=False)}"
        )
        self.logger.info(
            "正在进行跨素材全局编排（%d 个候选）/ Global story assembly (%d candidates)",
            len(candidates),
            len(candidates),
        )
        return self._request_json(prompt, SEQUENCE_SCHEMA, model=self.text_model)

    def validate_sequence(
        self,
        payload: Dict[str, Any],
        candidates: Sequence[Dict[str, Any]],
        treatment: Optional[Dict[str, Any]] = None,
    ) -> List[Dict[str, Any]]:
        """
        Validate global ordering and apply only constrained effect overrides.
        校验全局顺序，并仅应用受约束的效果覆盖。
        """
        sequence = payload.get("sequence")
        if not isinstance(sequence, list) or not sequence:
            raise DirectorError(
                "全局导演未返回非空 sequence / Global director returned no sequence."
            )
        by_id = {str(item["candidate_id"]): item for item in candidates}
        seen = set()
        final: List[Dict[str, Any]] = []
        for index, sequence_item in enumerate(sequence):
            if not isinstance(sequence_item, dict):
                raise DirectorError(f"sequence[{index}] 必须是对象 / must be an object.")
            candidate_id = str(sequence_item.get("candidate_id") or "").strip()
            if candidate_id not in by_id:
                raise DirectorError(
                    f"sequence[{index}] 引用了未知 candidate_id={candidate_id!r}"
                    " / references an unknown candidate."
                )
            if candidate_id in seen:
                raise DirectorError(
                    f"sequence 重复 candidate_id={candidate_id!r} / duplicate candidate."
                )
            seen.add(candidate_id)
            clip = dict(by_id[candidate_id])
            clip["reason_for_position"] = " ".join(
                str(sequence_item.get("reason_for_position") or "").split()
            )
            for key, allowed, default in (
                (
                    "transition_to_next",
                    {"cut", "cross_dissolve", "fade_black"},
                    str(clip.get("transition_to_next") or "cut"),
                ),
                (
                    "audio_cleanup",
                    {"none", "light", "strong"},
                    str(clip.get("audio_cleanup") or "light"),
                ),
                (
                    "color_look",
                    {"source", "neutral", "warm", "cool", "contrast"},
                    str(clip.get("color_look") or "neutral"),
                ),
                (
                    "motion",
                    {"static", "gentle_push_in"},
                    str(clip.get("motion") or "static"),
                ),
                (
                    "drx_preset",
                    {"none", "interview_clean", "cinematic", "low_light_cleanup"},
                    str(clip.get("drx_preset") or "none"),
                ),
                (
                    "stabilization",
                    {"none", "auto"},
                    str(clip.get("stabilization") or "none"),
                ),
                (
                    "tracking",
                    {
                        "none",
                        "magic_mask_forward",
                        "magic_mask_backward",
                        "magic_mask_bidirectional",
                    },
                    str(clip.get("tracking") or "none"),
                ),
            ):
                value = str(sequence_item.get(key) or default).strip().casefold()
                clip[key] = value if value in allowed else default
            transition_duration = self._finite_float(
                sequence_item.get(
                    "transition_duration_sec",
                    clip.get("transition_duration_sec", 0.0),
                ),
                f"sequence[{index}].transition_duration_sec",
            )
            clip["transition_duration_sec"] = (
                0.0
                if clip["transition_to_next"] == "cut"
                else round(min(2.0, max(0.1, transition_duration)), 3)
            )
            volume_db = self._finite_float(
                sequence_item.get("volume_db", clip.get("volume_db", 0.0)),
                f"sequence[{index}].volume_db",
            )
            clip["volume_db"] = round(min(12.0, max(-24.0, volume_db)), 2)
            smart_reframe = sequence_item.get(
                "smart_reframe", clip.get("smart_reframe", False)
            )
            clip["smart_reframe"] = (
                smart_reframe if isinstance(smart_reframe, bool) else False
            )
            final.append(clip)
        # The executor receives a real time-flow edit. Model ordering is advisory;
        # local validation prevents a visually plausible response from jumping from
        # the last take back to early setup footage.
        active_treatment = treatment or self._active_treatment
        global_look = {
            "clean_neutral": "neutral",
            "cinematic_warm": "warm",
            "cool_steel": "cool",
            "high_contrast": "contrast",
        }.get(str(active_treatment.get("creative_look") or ""), "neutral")
        final.sort(
            key=lambda item: (
                int(item.get("source_order", 0)),
                float(item.get("cut_in_sec", 0)),
                float(item.get("cut_out_sec", 0)),
            )
        )
        final = self._complete_story_coverage(final, candidates, active_treatment)
        final = self._remove_overlaps(final)
        for clip in final:
            if str(clip.get("color_look") or "neutral") in {"source", "neutral"}:
                clip["color_look"] = global_look
        return self._fit_target_duration(final, active_treatment)

    def _remove_overlaps(
        self, clips: Sequence[Dict[str, Any]]
    ) -> List[Dict[str, Any]]:
        """Remove repeated source frames while retaining chronological tails. / 删除重复源画面并保留后续时间段。"""
        cleaned: List[Dict[str, Any]] = []
        last_out_by_file: Dict[str, float] = {}
        for raw_clip in clips:
            clip = dict(raw_clip)
            file_key = str(clip.get("file_name") or "").casefold()
            cut_in = float(clip["cut_in_sec"])
            cut_out = float(clip["cut_out_sec"])
            previous_out = last_out_by_file.get(file_key, -1.0)
            if cut_in < previous_out:
                cut_in = previous_out
            if cut_out - cut_in < 1.5:
                continue
            clip["cut_in_sec"] = round(cut_in, 3)
            cleaned.append(clip)
            last_out_by_file[file_key] = cut_out
        return cleaned

    def _complete_story_coverage(
        self,
        selected: Sequence[Dict[str, Any]],
        candidates: Sequence[Dict[str, Any]],
        treatment: Dict[str, Any],
    ) -> List[Dict[str, Any]]:
        """
        Fill missing treatment beats and minimum runtime from inspected candidates.
        从已审片候选中补齐缺失叙事节拍与最低时长。
        """
        result = [dict(item) for item in selected]
        used = {str(item.get("candidate_id") or "") for item in result}
        target = float(treatment.get("target_duration_sec") or 90.0)
        minimum = target * 0.85

        def duration(item: Dict[str, Any]) -> float:
            return float(item["cut_out_sec"]) - float(item["cut_in_sec"])

        available_sources = {
            int(item.get("source_order", 0)) for item in candidates
        }
        selected_sources = {
            int(item.get("source_order", 0)) for item in result
        }
        missing_sources = sorted(available_sources - selected_sources)
        for source_order in missing_sources:
            options = [
                item for item in candidates
                if int(item.get("source_order", 0)) == source_order
                and str(item.get("candidate_id") or "") not in used
            ]
            if not options:
                continue
            choice = max(
                options,
                key=lambda item: (
                    bool(item.get("protected_story_anchor")),
                    float(item.get("quality_score", 0.5))
                    + float(item.get("confidence", 0.5)),
                ),
            )
            result.append(dict(choice))
            used.add(str(choice.get("candidate_id") or ""))

        ranked = sorted(
            (
                item for item in candidates
                if str(item.get("candidate_id") or "") not in used
            ),
            key=lambda item: (
                bool(item.get("protected_story_anchor")),
                float(item.get("quality_score", 0.5))
                + float(item.get("confidence", 0.5)),
            ),
            reverse=True,
        )
        while sum(duration(item) for item in result) < minimum and ranked:
            choice = ranked.pop(0)
            result.append(dict(choice))
            used.add(str(choice.get("candidate_id") or ""))
        result.sort(
            key=lambda item: (
                int(item.get("source_order", 0)),
                float(item.get("cut_in_sec", 0)),
            )
        )
        return result

    def _fit_target_duration(
        self,
        clips: Sequence[Dict[str, Any]],
        treatment: Dict[str, Any],
    ) -> List[Dict[str, Any]]:
        """
        Enforce a concise runtime while preserving opening and final payoff.
        强制控制成片时长，同时保留开场与最终高潮。
        """
        copied = [dict(item) for item in clips]
        if not copied:
            return []
        target = float(
            treatment.get("target_duration_sec")
            or self._active_target_duration_sec
            or self.target_duration_sec
            or 90.0
        )
        budget = max(30.0, target * 1.10)

        def duration(item: Dict[str, Any]) -> float:
            return float(item["cut_out_sec"]) - float(item["cut_in_sec"])

        if sum(duration(item) for item in copied) <= budget:
            return copied

        role_bonus = {
            "opening": 0.25, "climax": 0.35, "closing": 0.40,
            "bridge": 0.05, "context": 0.0, "interview": 0.05, "broll": 0.05,
        }
        payoff_index = max(
            range(len(copied)),
            key=lambda index: (
                role_bonus.get(str(copied[index].get("story_role")), 0.0),
                int(copied[index].get("source_order", 0)),
                float(copied[index].get("cut_in_sec", 0)),
            ),
        )
        protected_indices = []
        protected_beats = set()
        for index, item in enumerate(copied):
            beat = str(item.get("treatment_beat") or "")
            if item.get("protected_story_anchor") and beat not in protected_beats:
                protected_indices.append(index)
                protected_beats.add(beat)
        keep_indices = {0, len(copied) - 1, payoff_index, *protected_indices}
        used = sum(duration(copied[index]) for index in keep_indices)
        ranked = sorted(
            (index for index in range(len(copied)) if index not in keep_indices),
            key=lambda index: (
                float(copied[index].get("quality_score", 0.5))
                + float(copied[index].get("confidence", 0.5))
                + role_bonus.get(str(copied[index].get("story_role")), 0.0)
                + (0.5 if copied[index].get("protected_story_anchor") else 0.0)
            ),
            reverse=True,
        )
        for index in ranked:
            clip_duration = duration(copied[index])
            if used + clip_duration <= budget:
                keep_indices.add(index)
                used += clip_duration
        selected = [item for index, item in enumerate(copied) if index in keep_indices]
        if len(selected) < 3 and len(copied) >= 3:
            middle = len(copied) // 2
            selected.append(copied[middle])
        selected.sort(
            key=lambda item: (
                int(item.get("source_order", 0)),
                float(item.get("cut_in_sec", 0)),
            )
        )
        self.logger.info(
            "时长守门：%d 个模型片段压缩为 %d 个，目标 %.1fs / Runtime guard: %d clips -> %d, target %.1fs",
            len(copied), len(selected), target, len(copied), len(selected), target,
        )
        return selected

    def _encode_images(
        self, frames: Sequence[Dict[str, Any]]
    ) -> tuple[List[Dict[str, Any]], List[str]]:
        """
        Read selected JPEGs as Ollama REST base64 image inputs.
        将选中的 JPEG 读取为 Ollama REST 所需的 Base64 图片输入。
        """
        available: List[Dict[str, Any]] = []
        encoded: List[str] = []
        for frame in frames:
            path_text = str(frame.get("image_path") or "").strip()
            if not path_text:
                continue
            path = Path(path_text).expanduser()
            if not path.is_file():
                self.logger.warning(
                    "关键帧不存在，跳过：%s / Missing keyframe: %s", path, path
                )
                continue
            try:
                data = path.read_bytes()
            except OSError as exc:
                self.logger.warning(
                    "无法读取关键帧 %s：%s / Cannot read keyframe", path, exc
                )
                continue
            available.append(dict(frame))
            encoded.append(base64.b64encode(data).decode("ascii"))
        return available, encoded

    @staticmethod
    def _select_keyframes(
        raw_frames: object, limit: int
    ) -> List[Dict[str, Any]]:
        """
        Select time-distributed high-change frames for one vision request.
        为一次视觉请求选择时间分布均匀且变化明显的画面。
        """
        if not isinstance(raw_frames, list):
            return []
        frames = [dict(item) for item in raw_frames if isinstance(item, dict)]
        frames.sort(key=lambda item: float(item.get("timestamp_sec", 0.0)))
        if len(frames) <= limit:
            return frames
        selected: List[Dict[str, Any]] = []
        for bucket in range(limit):
            start = int(bucket * len(frames) / limit)
            end = max(start + 1, int((bucket + 1) * len(frames) / limit))
            group = frames[start:end]
            selected.append(
                max(group, key=lambda item: float(item.get("scene_score", 0.0)))
            )
        selected.sort(key=lambda item: float(item.get("timestamp_sec", 0.0)))
        return selected

    @staticmethod
    def _limit_candidates(
        candidates: Sequence[Dict[str, Any]], limit: int
    ) -> List[Dict[str, Any]]:
        """Bound global context while retaining the strongest candidates. / 限制全局上下文并保留最强候选。"""
        copied = [dict(item) for item in candidates]
        if len(copied) <= limit:
            return copied

        def rank(item: Dict[str, Any]) -> tuple[float, float]:
            return (
                float(item.get("quality_score", 0.5))
                + float(item.get("confidence", 0.5)),
                float(item.get("cut_out_sec", 0))
                - float(item.get("cut_in_sec", 0)),
            )

        # Keep every source represented before filling the global context
        # budget. A long interview must not crowd all B-roll sources out.
        by_asset: Dict[str, List[Dict[str, Any]]] = {}
        for item in copied:
            by_asset.setdefault(str(item.get("asset_id") or ""), []).append(item)
        per_asset = max(1, min(4, limit // max(1, len(by_asset))))
        selected: List[Dict[str, Any]] = []
        selected_ids = set()
        for group in by_asset.values():
            for item in sorted(group, key=rank, reverse=True)[:per_asset]:
                selected.append(item)
                selected_ids.add(id(item))
                if len(selected) == limit:
                    return selected
        remaining = sorted(
            (item for item in copied if id(item) not in selected_ids),
            key=rank,
            reverse=True,
        )
        selected.extend(remaining[: max(0, limit - len(selected))])
        return selected

    def validate_chunk_decisions(
        self,
        payload: Dict[str, Any],
        chunk: Dict[str, Any],
        source_name: str,
    ) -> List[Dict[str, Any]]:
        """
        Validate model decisions and clamp only one-second boundary drift.
        校验模型决策，仅允许并修正一秒以内的边界漂移。
        """
        decisions = payload.get("decisions")
        if not isinstance(decisions, list):
            raise DirectorError(
                "模型 JSON 缺少 decisions 数组 / Model JSON requires decisions array."
            )
        validated: List[Dict[str, Any]] = []
        chunk_start = float(chunk["start_sec"])
        chunk_end = float(chunk["end_sec"])
        for index, decision in enumerate(decisions):
            if not isinstance(decision, dict):
                raise DirectorError(
                    f"decisions[{index}] 必须是对象 / must be an object."
                )
            story_role = self._enum_value(
                decision.get("story_role"),
                {"opening", "context", "interview", "broll", "bridge", "climax", "closing"},
                "context",
            )
            cut_in = self._finite_float(
                decision.get("cut_in_sec"), f"decisions[{index}].cut_in_sec"
            )
            cut_out = self._finite_float(
                decision.get("cut_out_sec"), f"decisions[{index}].cut_out_sec"
            )
            if cut_in < chunk_start - 1.0 or cut_out > chunk_end + 1.0:
                raise DirectorError(
                    f"模型产生越界时间戳 {cut_in}-{cut_out}，分块为 "
                    f"{chunk_start}-{chunk_end} / Model timestamp is outside chunk."
                )
            cut_in = max(chunk_start, cut_in)
            cut_out = min(chunk_end, cut_out)
            maximum_duration = self._max_candidate_duration(story_role)
            if cut_out - cut_in > maximum_duration:
                self.logger.warning(
                    "候选片段超过 %s 角色上限 %.1f 秒，已截短：%.3f-%.3f / Candidate exceeded role limit and was shortened",
                    story_role, maximum_duration, cut_in, cut_out,
                )
                cut_out = cut_in + maximum_duration
            if cut_in < 0 or cut_out - cut_in < 0.2:
                raise DirectorError(
                    f"decisions[{index}] 时间范围过短或无效 / range is invalid."
                )
            reason = " ".join(
                str(decision.get("reason_for_cut", "")).split()
            )
            if not reason:
                raise DirectorError(
                    f"decisions[{index}].reason_for_cut 不能为空 / cannot be empty."
                )
            confidence = decision.get("confidence", 0.5)
            confidence_value = self._finite_float(
                confidence, f"decisions[{index}].confidence"
            )
            confidence_value = min(1.0, max(0.0, confidence_value))
            quality_value = self._finite_float(
                decision.get("quality_score", confidence_value),
                f"decisions[{index}].quality_score",
            )
            quality_value = min(1.0, max(0.0, quality_value))
            transition = self._enum_value(
                decision.get("transition_to_next"),
                {"cut", "cross_dissolve", "fade_black"},
                "cut",
            )
            transition_duration = self._finite_float(
                decision.get("transition_duration_sec", 0.0),
                f"decisions[{index}].transition_duration_sec",
            )
            validated.append(
                {
                    "file_name": source_name,
                    "cut_in_sec": round(cut_in, 3),
                    "cut_out_sec": round(cut_out, 3),
                    "reason_for_cut": reason,
                    "confidence": round(confidence_value, 3),
                    "quality_score": round(quality_value, 3),
                    "visual_summary": " ".join(
                        str(decision.get("visual_summary") or reason).split()
                    ),
                    "story_role": story_role,
                    "transition_to_next": transition,
                    "transition_duration_sec": (
                        0.0
                        if transition == "cut"
                        else round(min(2.0, max(0.1, transition_duration)), 3)
                    ),
                    "audio_cleanup": self._enum_value(
                        decision.get("audio_cleanup"),
                        {"none", "light", "strong"},
                        "light",
                    ),
                    "color_look": self._enum_value(
                        decision.get("color_look"),
                        {"source", "neutral", "warm", "cool", "contrast"},
                        "neutral",
                    ),
                    "motion": self._enum_value(
                        decision.get("motion"),
                        {"static", "gentle_push_in"},
                        "static",
                    ),
                    "volume_db": round(
                        min(
                            12.0,
                            max(
                                -24.0,
                                self._finite_float(
                                    decision.get("volume_db", 0.0),
                                    f"decisions[{index}].volume_db",
                                ),
                            ),
                        ),
                        2,
                    ),
                    "drx_preset": self._enum_value(
                        decision.get("drx_preset"),
                        {"none", "interview_clean", "cinematic", "low_light_cleanup"},
                        "none",
                    ),
                    "stabilization": self._enum_value(
                        decision.get("stabilization"),
                        {"none", "auto"},
                        "none",
                    ),
                    "tracking": self._enum_value(
                        decision.get("tracking"),
                        {
                            "none",
                            "magic_mask_forward",
                            "magic_mask_backward",
                            "magic_mask_bidirectional",
                        },
                        "none",
                    ),
                    "smart_reframe": (
                        decision.get("smart_reframe", False)
                        if isinstance(decision.get("smart_reframe", False), bool)
                        else False
                    ),
                }
            )
        return validated

    @staticmethod
    def _max_candidate_duration(story_role: object, protected: bool = False) -> float:
        """
        Return a narrative-role-specific shot ceiling in seconds.
        按叙事角色返回动态镜头时长上限（秒）。

        Dialogue and conclusions need complete thoughts; visual bridges stay concise.
        对话与结尾需要完整语义，纯画面桥段则保持简洁。
        """
        role = str(story_role or "context").casefold()
        limits = {
            "interview": 45.0,
            "closing": 30.0,
            "climax": 25.0,
            "opening": 20.0,
            "context": 20.0,
            "bridge": 12.0,
            "broll": 10.0,
        }
        return max(limits.get(role, 20.0), 45.0 if protected else 0.0)

    def merge_decisions(
        self, decisions: Sequence[Dict[str, Any]]
    ) -> List[Dict[str, Any]]:
        """
        Sort, deduplicate, and merge overlapping/near-adjacent decisions.
        排序、去重并合并重叠或近乎相邻的决策。
        """
        ordered = sorted(
            (dict(item) for item in decisions),
            key=lambda item: (
                int(item.get("source_order", 0)),
                str(item["file_name"]).casefold(),
                float(item["cut_in_sec"]),
                float(item["cut_out_sec"]),
            ),
        )
        merged: List[Dict[str, Any]] = []
        for current in ordered:
            if not merged:
                merged.append(current)
                continue
            previous = merged[-1]
            same_media = (
                str(previous["file_name"]).casefold()
                == str(current["file_name"]).casefold()
            )
            merged_role = str(
                previous.get("story_role") or current.get("story_role") or "context"
            )
            protected = bool(
                previous.get("protected_story_anchor")
                or current.get("protected_story_anchor")
            )
            touches = (
                float(current["cut_in_sec"])
                <= float(previous["cut_out_sec"]) + self.merge_gap_sec
                and max(
                    float(previous["cut_out_sec"]),
                    float(current["cut_out_sec"]),
                ) - min(
                    float(previous["cut_in_sec"]),
                    float(current["cut_in_sec"]),
                ) <= self._max_candidate_duration(merged_role, protected)
            )
            if same_media and touches:
                previous["cut_out_sec"] = round(
                    max(
                        float(previous["cut_out_sec"]),
                        float(current["cut_out_sec"]),
                    ),
                    3,
                )
                existing_reasons = [
                    part.strip()
                    for part in str(previous["reason_for_cut"]).split("；")
                ]
                new_reason = str(current["reason_for_cut"]).strip()
                if new_reason not in existing_reasons:
                    previous["reason_for_cut"] += "；" + new_reason
                previous["confidence"] = round(
                    min(
                        float(previous.get("confidence", 0.5)),
                        float(current.get("confidence", 0.5)),
                    ),
                    3,
                )
                previous["protected_story_anchor"] = bool(
                    previous.get("protected_story_anchor")
                    or current.get("protected_story_anchor")
                )
                if current.get("protected_story_anchor"):
                    previous["treatment_beat"] = current.get("treatment_beat", "")
            else:
                merged.append(current)
        return merged

    def unload_model(self, model: Optional[str] = None) -> None:
        """
        Ask Ollama to immediately unload the configured model.
        请求 Ollama 立即卸载配置的模型。

        Ollama documents ``keep_alive: 0`` as the API mechanism that releases a
        loaded model. Failure is logged because it must not mask the original
        generation error; the parent orchestrator performs a second unload.

        Ollama 将 ``keep_alive: 0`` 定义为释放已加载模型的 API 机制。失败仅记录
        日志，以免掩盖原始生成错误；父调度器还会执行第二次卸载。
        """
        if self._session is None:
            return
        selected_model = str(model or self.model).strip()
        try:
            response = self.session.post(
                self.base_url + "/api/generate",
                json={
                    "model": selected_model,
                    "prompt": "",
                    "stream": False,
                    "keep_alive": 0,
                },
                timeout=(5, 60),
            )
            response.raise_for_status()
            self.logger.info(
                "已请求 Ollama 卸载模型 %s / Requested Ollama unload for %s",
                selected_model,
                selected_model,
            )
        except Exception as exc:
            self.logger.warning(
                "Ollama 模型卸载请求失败：%s / Ollama unload request failed: %s",
                exc,
                exc,
            )

    @staticmethod
    def parse_generated_json(text: str) -> Dict[str, Any]:
        """
        Parse plain JSON or one fenced JSON object from a model response.
        解析纯 JSON 或代码围栏中的单个 JSON 对象。
        """
        cleaned = text.strip()
        if cleaned.startswith("```"):
            cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned, flags=re.I)
            cleaned = re.sub(r"\s*```$", "", cleaned)
        try:
            value = json.loads(cleaned)
        except json.JSONDecodeError as exc:
            raise DirectorError(
                f"模型输出不是有效 JSON / Model output is not valid JSON: {exc}"
            ) from exc
        if not isinstance(value, dict):
            raise DirectorError(
                "模型输出根节点必须是对象 / Model JSON root must be an object."
            )
        return value

    @staticmethod
    def _finite_float(value: Any, field_name: str) -> float:
        """Convert a finite JSON number without accepting booleans. / 转换有限 JSON 数值且拒绝布尔值。"""
        if isinstance(value, bool) or value is None:
            raise DirectorError(f"{field_name} 必须是数值 / must be numeric.")
        try:
            decimal_value = Decimal(str(value))
        except (InvalidOperation, ValueError):
            raise DirectorError(
                f"{field_name} 不是有效数值 / is not numeric: {value!r}"
            )
        if not decimal_value.is_finite():
            raise DirectorError(f"{field_name} 必须是有限数值 / must be finite.")
        return float(decimal_value)

    @staticmethod
    def _enum_value(value: object, allowed: set, default: str) -> str:
        """Return a constrained lower-case enum or its safe default. / 返回受约束的小写枚举或安全默认值。"""
        text = str(value or default).strip().casefold()
        return text if text in allowed else default

    @staticmethod
    def _atomic_write_json(payload: Dict[str, Any], destination: Path) -> None:
        """Atomically replace the UTF-8 output JSON. / 原子替换 UTF-8 输出 JSON。"""
        destination.parent.mkdir(parents=True, exist_ok=True)
        temporary = destination.with_suffix(destination.suffix + ".tmp")
        try:
            temporary.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            os.replace(str(temporary), str(destination))
        except OSError as exc:
            raise DirectorError(
                f"无法写入输出 JSON / Cannot write output JSON: {exc}"
            ) from exc

    def _checkpoint_fingerprint(self, raw_path: Path) -> str:
        """Hash inputs that affect director decisions. / 哈希会影响导演决策的输入。"""
        digest = hashlib.sha256()
        try:
            with raw_path.open("rb") as handle:
                for block in iter(lambda: handle.read(1024 * 1024), b""):
                    digest.update(block)
        except OSError as exc:
            raise DirectorError(
                f"无法读取导演输入以创建检查点 / Cannot fingerprint director input: {exc}"
            ) from exc
        settings = {
            "model": self.model,
            "text_model": self.text_model,
            "chunk_duration_sec": self.chunk_duration_sec,
            "num_ctx": self.num_ctx,
            "prompt_version": DIRECTOR_PROMPT_VERSION,
            "creative_brief": self.creative_brief,
            "target_duration_sec": self.target_duration_sec,
            "camera_profile": self.camera_profile,
            "music_folder": str(self.music_folder or ""),
            "music_analysis": str(self.music_analysis_path or ""),
        }
        digest.update(
            json.dumps(settings, sort_keys=True, ensure_ascii=False).encode("utf-8")
        )
        return digest.hexdigest()

    @staticmethod
    def _director_chunk_key(chunk: Dict[str, Any]) -> str:
        """Build a deterministic checkpoint key for one chunk. / 为单个分块生成确定性检查点键。"""
        return "{}|{:.6f}|{:.6f}|{}".format(
            str(chunk.get("asset_id") or ""),
            float(chunk.get("start_sec") or 0.0),
            float(chunk.get("end_sec") or 0.0),
            str(chunk.get("source_name") or ""),
        )

    def _load_director_checkpoint(
        self, path: Path, fingerprint: str
    ) -> Dict[str, List[Dict[str, Any]]]:
        """Load a matching partial director result. / 读取匹配的导演阶段部分结果。"""
        if not path.is_file():
            return {}
        try:
            payload = json.loads(path.read_text(encoding="utf-8-sig"))
        except (OSError, json.JSONDecodeError) as exc:
            self.logger.warning(
                "忽略损坏的导演检查点：%s / Ignoring corrupt director checkpoint: %s",
                exc,
                exc,
            )
            return {}
        if (
            not isinstance(payload, dict)
            or payload.get("checkpoint_version") != DIRECTOR_CHECKPOINT_VERSION
            or payload.get("fingerprint") != fingerprint
            or not isinstance(payload.get("completed_chunks"), dict)
        ):
            self.logger.info(
                "现有导演检查点与当前素材或设置不匹配，重新分析 / "
                "Director checkpoint does not match current inputs; starting fresh"
            )
            return {}
        completed: Dict[str, List[Dict[str, Any]]] = {}
        for key, decisions in payload["completed_chunks"].items():
            if isinstance(key, str) and isinstance(decisions, list) and all(
                isinstance(item, dict) for item in decisions
            ):
                completed[key] = [dict(item) for item in decisions]
        return completed

    def _write_director_checkpoint(
        self,
        path: Path,
        fingerprint: str,
        completed_chunks: Dict[str, List[Dict[str, Any]]],
    ) -> None:
        """Atomically save completed visual chunks. / 原子保存已完成的视觉分块。"""
        self._atomic_write_json(
            {
                "checkpoint_version": DIRECTOR_CHECKPOINT_VERSION,
                "fingerprint": fingerprint,
                "updated_at_utc": datetime.now(timezone.utc).isoformat(),
                "completed_chunks": completed_chunks,
            },
            path,
        )


def configure_logging(level: str = "INFO") -> logging.Logger:
    """Configure standalone director logging. / 配置独立导演日志。"""
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
    """Create AI director CLI arguments. / 创建 AI 导演命令行参数。"""
    parser = argparse.ArgumentParser(
        description="Generate chunked local-AI edit decisions. / 生成分块本地 AI 剪辑决策。"
    )
    parser.add_argument("--raw-data", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--model", required=True)
    parser.add_argument(
        "--text-model",
        default="",
        help="卸载视觉模型后使用的全局文字导演 / global text director loaded after vision unload",
    )
    parser.add_argument("--proxy-file-name")
    parser.add_argument("--ollama-url", default="http://localhost:11434")
    parser.add_argument("--chunk-minutes", type=float, default=12.0)
    parser.add_argument("--project-fps", type=float, default=25.0)
    parser.add_argument("--num-ctx", type=int, default=8192)
    parser.add_argument("--timeout", type=int, default=1800)
    parser.add_argument("--merge-gap", type=float, default=0.4)
    parser.add_argument(
        "--creative-brief",
        default="",
        help="创作意图/主题要求 / creative theme or directing brief",
    )
    parser.add_argument(
        "--target-duration-sec",
        type=float,
        default=0.0,
        help="目标成片秒数，0=自动 / target runtime in seconds; 0=automatic",
    )
    parser.add_argument(
        "--camera-profile",
        default="sony_pp8_slog3_sgamut3cine",
        choices=("sony_pp8_slog3_sgamut3cine", "rec709", "auto"),
    )
    parser.add_argument("--music-folder")
    parser.add_argument("--music-analysis")
    parser.add_argument(
        "--revalidate-existing",
        action="store_true",
        help="不调用 Ollama，仅重新应用本地守门 / reapply local gates without Ollama",
    )
    parser.add_argument(
        "--log-level",
        default="INFO",
        choices=("DEBUG", "INFO", "WARNING", "ERROR"),
    )
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    """Run director CLI with deterministic exit codes. / 运行导演 CLI 并返回确定性退出码。"""
    args = build_parser().parse_args(argv)
    logger = configure_logging(args.log_level)
    try:
        director = AIDirector(
            model=args.model,
            text_model=args.text_model or args.model,
            base_url=args.ollama_url,
            chunk_minutes=args.chunk_minutes,
            project_fps=args.project_fps,
            num_ctx=args.num_ctx,
            timeout_sec=args.timeout,
            merge_gap_sec=args.merge_gap,
            creative_brief=args.creative_brief,
            target_duration_sec=args.target_duration_sec,
            camera_profile=args.camera_profile,
            music_folder=args.music_folder,
            music_analysis=args.music_analysis,
            logger=logger,
        )
        if args.revalidate_existing:
            director.revalidate_existing_plan(args.raw_data, args.output)
        else:
            director.run(args.raw_data, args.output, args.proxy_file_name)
        return 0
    except DirectorError as exc:
        logger.error("%s", exc)
        return 2
    except KeyboardInterrupt:
        logger.warning("用户中断导演阶段 / Director interrupted.")
        return 130
    except Exception:
        logger.exception("未预期导演错误 / Unexpected director error.")
        return 1


if __name__ == "__main__":
    sys.exit(main())
