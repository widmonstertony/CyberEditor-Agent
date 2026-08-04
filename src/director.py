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
from difflib import SequenceMatcher
import hashlib
import json
import logging
import math
import os
from pathlib import Path
import re
import sys
from typing import Any, Dict, List, Optional, Sequence


LOGGER_NAME = "cybereditor.director"
DIRECTOR_CHECKPOINT_VERSION = 1
DIRECTOR_PROMPT_VERSION = "2026-08-03.5-continuous-full-review"

COLOR_BIBLE_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "properties": {
        "global_palette": {
            "type": "string",
            "enum": [
                "natural", "teal_amber", "cool_moonlight", "warm_memory",
                "desaturated_grit", "neon_night",
            ],
        },
        "contrast": {"type": "number", "minimum": 0.85, "maximum": 1.25},
        "saturation": {"type": "number", "minimum": 0.75, "maximum": 1.25},
        "warmth": {"type": "number", "minimum": -1, "maximum": 1},
        "highlight_rolloff": {"type": "number", "minimum": 0, "maximum": 1},
        "chapter_grades": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "beat": {
                        "type": "string",
                        "enum": ["opening", "development", "payoff", "ending"],
                    },
                    "exposure_ev": {"type": "number", "minimum": -0.5, "maximum": 0.5},
                    "contrast": {"type": "number", "minimum": 0.9, "maximum": 1.2},
                    "saturation": {"type": "number", "minimum": 0.8, "maximum": 1.2},
                    "warmth": {"type": "number", "minimum": -0.5, "maximum": 0.5},
                    "reason": {"type": "string", "minLength": 1},
                },
                "required": [
                    "beat", "exposure_ev", "contrast", "saturation", "warmth", "reason"
                ],
                "additionalProperties": False,
            },
            "minItems": 4,
            "maxItems": 4,
        },
    },
    "required": [
        "global_palette", "contrast", "saturation", "warmth",
        "highlight_rolloff", "chapter_grades",
    ],
    "additionalProperties": False,
}

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
        "color_bible": COLOR_BIBLE_SCHEMA,
        "music_mood": {"type": "string", "minLength": 1},
        "music_energy_arc": {"type": "string", "minLength": 1},
        "music_search_queries": {
            "type": "array",
            "items": {"type": "string", "minLength": 2},
            "minItems": 2,
            "maxItems": 6,
        },
        "music_instrumentation": {
            "type": "array",
            "items": {"type": "string", "minLength": 1},
            "minItems": 1,
            "maxItems": 8,
        },
        "music_tempo_min_bpm": {"type": "number", "minimum": 40, "maximum": 220},
        "music_tempo_max_bpm": {"type": "number", "minimum": 40, "maximum": 220},
        "music_vocal_policy": {
            "type": "string",
            "enum": ["instrumental_only", "vocals_allowed", "vocals_preferred"],
        },
        "music_cue_count": {"type": "integer", "minimum": 1, "maximum": 3},
        "music_silence_strategy": {"type": "string", "minLength": 1},
        "music_license_intent": {
            "type": "string",
            "enum": ["commercial_safe", "noncommercial", "user_authorized_any"],
        },
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
        "payoff_beat", "ending_beat", "color_intent", "creative_look", "color_bible",
        "music_mood", "music_energy_arc", "music_search_queries",
        "music_instrumentation", "music_tempo_min_bpm",
        "music_tempo_max_bpm", "music_vocal_policy", "music_cue_count",
        "music_silence_strategy", "music_license_intent",
        "editorial_rules", "story_anchors",
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
        "continuity_summary": {
            "type": "string",
            "minLength": 1,
            "maxLength": 1200,
        },
        "decisions": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "cut_in_sec": {"type": "number", "minimum": 0},
                    "cut_out_sec": {"type": "number", "minimum": 0},
                    "reason_for_cut": {"type": "string", "minLength": 1},
                    "visual_summary": {"type": "string", "minLength": 1},
                    "subject_action": {"type": "string", "minLength": 1},
                    "emotion": {"type": "string", "minLength": 1},
                    "action_phase": {
                        "type": "string",
                        "enum": ["setup", "build", "action", "reaction", "payoff", "aftermath"],
                    },
                    "shot_scale": {
                        "type": "string",
                        "enum": ["extreme_wide", "wide", "medium", "closeup", "detail"],
                    },
                    "camera_motion": {
                        "type": "string",
                        "enum": ["static", "pan", "tilt", "handheld", "tracking"],
                    },
                    "continuity_tags": {
                        "type": "array",
                        "items": {"type": "string", "minLength": 1},
                        "maxItems": 6,
                    },
                    "rhythmic_potential": {"type": "number", "minimum": 0, "maximum": 1},
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
                    "subject_action",
                    "emotion",
                    "action_phase",
                    "shot_scale",
                    "camera_motion",
                    "continuity_tags",
                    "rhythmic_potential",
                    "story_role",
                ],
                "additionalProperties": False,
            },
        }
    },
    "required": ["continuity_summary", "decisions"],
    "additionalProperties": False,
}

SEQUENCE_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "properties": {
        "project_summary": {"type": "string"},
        "music_plan": {
            "type": "object",
            "properties": {
                "strategy": {"type": "string", "minLength": 1},
                "silence_regions": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "timeline_in_sec": {"type": "number", "minimum": 0},
                            "timeline_out_sec": {"type": "number", "minimum": 0},
                            "reason": {"type": "string", "minLength": 1},
                        },
                        "required": ["timeline_in_sec", "timeline_out_sec", "reason"],
                        "additionalProperties": False,
                    },
                    "maxItems": 8,
                },
                "cues": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "cue_id": {"type": "string", "minLength": 1},
                            "track_file": {"type": "string", "minLength": 1},
                            "story_beat": {
                                "type": "string",
                                "enum": ["opening", "development", "payoff", "ending"],
                            },
                            "timeline_in_sec": {"type": "number", "minimum": 0},
                            "timeline_out_sec": {"type": "number", "minimum": 0},
                            "track_in_sec": {"type": "number", "minimum": 0},
                            "track_out_sec": {"type": "number", "minimum": 0},
                            "reason": {"type": "string", "minLength": 1},
                            "target_lufs": {"type": "number", "minimum": -36, "maximum": -14},
                            "fade_in_sec": {"type": "number", "minimum": 0, "maximum": 12},
                            "fade_out_sec": {"type": "number", "minimum": 0, "maximum": 12},
                            "crossfade_sec": {"type": "number", "minimum": 0, "maximum": 8},
                            "duck_under_dialogue_db": {"type": "number", "minimum": -24, "maximum": 0},
                            "sync_points": {
                                "type": "array",
                                "items": {
                                    "type": "object",
                                    "properties": {
                                        "timeline_sec": {"type": "number", "minimum": 0},
                                        "track_sec": {"type": "number", "minimum": 0},
                                        "type": {
                                            "type": "string",
                                            "enum": ["beat", "strong_beat", "downbeat", "section", "energy_peak"],
                                        },
                                        "purpose": {"type": "string", "minLength": 1},
                                    },
                                    "required": ["timeline_sec", "track_sec", "type", "purpose"],
                                    "additionalProperties": False,
                                },
                                "maxItems": 12,
                            },
                        },
                        "required": [
                            "cue_id", "track_file", "story_beat",
                            "timeline_in_sec", "timeline_out_sec", "track_in_sec",
                            "track_out_sec", "reason", "target_lufs",
                            "fade_in_sec", "fade_out_sec", "crossfade_sec",
                            "duck_under_dialogue_db", "sync_points"
                        ],
                        "additionalProperties": False,
                    },
                    "maxItems": 3,
                },
            },
            "required": ["strategy", "silence_regions", "cues"],
            "additionalProperties": False,
        },
        "sequence": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "candidate_id": {"type": "string", "minLength": 1},
                    "reason_for_position": {"type": "string"},
                    "music_edit_role": {
                        "type": "string",
                        "enum": [
                            "natural_sound", "on_beat", "phrase_start",
                            "build", "payoff_hit", "release",
                        ],
                    },
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
                "required": ["candidate_id", "music_edit_role"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["project_summary", "music_plan", "sequence"],
    "additionalProperties": False,
}

# The final directing pass is deliberately split into two schema-constrained
# requests while the same Ollama process remains loaded.  Resolve sequencing
# and music cue design have different evidence requirements; separating their
# schemas prevents the combined prompt/schema from consuming the generation
# window on long, multi-asset projects.
# 最终导演在同一次 Ollama 模型驻留期间拆成两次 Schema 约束请求。镜头编排与音乐
# cue 设计依赖不同证据，拆分后可避免长项目的组合 Prompt/Schema 吃光 JSON 输出空间。
SEQUENCE_SELECTION_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "properties": {
        "project_summary": SEQUENCE_SCHEMA["properties"]["project_summary"],
        "sequence": SEQUENCE_SCHEMA["properties"]["sequence"],
    },
    "required": ["project_summary", "sequence"],
    "additionalProperties": False,
}

MUSIC_PLAN_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "properties": {
        "music_plan": SEQUENCE_SCHEMA["properties"]["music_plan"],
    },
    "required": ["music_plan"],
    "additionalProperties": False,
}

COVERAGE_SYNOPSIS_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "properties": {
        "whole_footage_summary": {"type": "string", "minLength": 1, "maxLength": 2400},
        "discovered_central_theme": {"type": "string", "minLength": 1, "maxLength": 500},
        "character_threads": {
            "type": "array",
            "items": {"type": "string", "minLength": 1, "maxLength": 400},
            "maxItems": 10,
        },
        "event_timeline": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "asset_id": {"type": "string", "minLength": 1},
                    "source_order": {"type": "integer", "minimum": 0},
                    "event": {"type": "string", "minLength": 1, "maxLength": 500},
                    "story_meaning": {"type": "string", "minLength": 1, "maxLength": 500},
                },
                "required": ["asset_id", "source_order", "event", "story_meaning"],
                "additionalProperties": False,
            },
            "maxItems": 24,
        },
        "visual_motifs": {
            "type": "array",
            "items": {"type": "string", "minLength": 1, "maxLength": 300},
            "maxItems": 10,
        },
        "continuity_risks": {
            "type": "array",
            "items": {"type": "string", "minLength": 1, "maxLength": 300},
            "maxItems": 10,
        },
    },
    "required": [
        "whole_footage_summary", "discovered_central_theme", "character_threads",
        "event_timeline", "visual_motifs", "continuity_risks",
    ],
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
        treatment_file: Optional[os.PathLike] = None,
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
        self.treatment_path = (
            Path(treatment_file).expanduser().resolve() if treatment_file else None
        )
        if self.treatment_path is not None and not self.treatment_path.is_file():
            raise DirectorError(
                f"导演初审文件不存在 / Director treatment not found: {self.treatment_path}"
            )
        self._music_analysis: Dict[str, Any] = {}
        self._active_treatment: Dict[str, Any] = {}
        self._active_target_duration_sec = 0.0
        self._music_files: List[Path] = []
        self._asset_continuity_summaries: Dict[str, str] = {}
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

    def run_treatment_only(
        self,
        raw_data_path: os.PathLike,
        treatment_output_path: os.PathLike,
        music_brief_output_path: os.PathLike,
    ) -> Dict[str, Any]:
        """
        Run the first multimodal director pass and publish its two handoff files.
        运行第一次多模态导演初审，并写出两份阶段交接文件。

        Parameters / 参数:
            raw_data_path: Combined extractor output. / 合并后的提取结果。
            treatment_output_path: Validated story treatment JSON. / 已校验的导演阐述 JSON。
            music_brief_output_path: Search and emotion brief for CPU retrieval. / 供 CPU 找歌的情绪与检索简报。

        The model is always unloaded before this method returns, so network and
        CPU music analysis can start with no Ollama VRAM residency.
        本方法返回前始终卸载模型，使联网找歌与 CPU 听诊在 Ollama 不占显存时运行。
        """
        raw_path = Path(raw_data_path).expanduser().resolve()
        raw_data = self.load_raw_data(raw_path)
        assets = raw_data.get("assets")
        if not isinstance(assets, list) or not assets:
            raise DirectorError(
                "双导演音乐流程需要批量素材 schema / Two-pass music directing requires multi-asset raw data."
            )
        try:
            self.check_ollama(require_vision=True)
            payload = self.request_treatment(assets)
            treatment = self.validate_treatment(payload, assets)
            music_brief = self.build_music_brief(treatment, assets)
            self._atomic_write_json(
                treatment,
                Path(treatment_output_path).expanduser().resolve(),
            )
            self._atomic_write_json(
                music_brief,
                Path(music_brief_output_path).expanduser().resolve(),
            )
            self.logger.info(
                "音乐导演初审完成：%s / Music-director review complete: %s",
                music_brief_output_path,
                music_brief_output_path,
            )
            return {"director_treatment": treatment, "music_brief": music_brief}
        finally:
            self.unload_model(self.model)

    def load_treatment(self, assets: Sequence[Dict[str, Any]]) -> Dict[str, Any]:
        """Load and validate the first-pass treatment. / 读取并校验第一次导演初审。"""
        if self.treatment_path is None:
            payload = self.request_treatment(assets)
        else:
            try:
                payload = json.loads(
                    self.treatment_path.read_text(encoding="utf-8-sig")
                )
            except (OSError, json.JSONDecodeError) as exc:
                raise DirectorError(
                    f"无法读取导演初审 / Cannot read director treatment: {exc}"
                ) from exc
            if not isinstance(payload, dict):
                raise DirectorError(
                    "导演初审根节点必须是对象 / Director treatment must be an object."
                )
            self._active_target_duration_sec = self._finite_float(
                payload.get("target_duration_sec", self.target_duration_sec or 90.0),
                "target_duration_sec",
            )
        return self.validate_treatment(payload, assets)

    def build_music_brief(
        self,
        treatment: Dict[str, Any],
        assets: Sequence[Dict[str, Any]],
    ) -> Dict[str, Any]:
        """
        Convert the first director decision into a stable CPU-search contract.
        将第一次导演决策转换为稳定的 CPU 音乐检索契约。

        Parameters / 参数:
            treatment: Validated project treatment. / 已校验的项目导演阐述。
            assets: Source assets used for provenance. / 用于记录来源的素材列表。
        """
        return {
            "schema_version": "1.0",
            "generated_at_utc": datetime.now(timezone.utc).isoformat(),
            "director_model": self.model,
            "project_title": treatment.get("title", ""),
            "central_theme": treatment.get("central_theme", ""),
            "story_summary": treatment.get("logline", ""),
            "emotion_arc": treatment.get("music_energy_arc", ""),
            "mood": treatment.get("music_mood", ""),
            "search_queries": list(treatment.get("music_search_queries") or []),
            "instrumentation": list(treatment.get("music_instrumentation") or []),
            "tempo_bpm": {
                "min": float(treatment.get("music_tempo_min_bpm", 70.0)),
                "max": float(treatment.get("music_tempo_max_bpm", 130.0)),
            },
            "vocal_policy": treatment.get("music_vocal_policy", "instrumental_only"),
            "preferred_cue_count": int(treatment.get("music_cue_count", 2)),
            "silence_strategy": treatment.get("music_silence_strategy", ""),
            "license_intent": treatment.get("music_license_intent", "commercial_safe"),
            "target_duration_sec": float(treatment.get("target_duration_sec", 0.0)),
            "source_asset_ids": [str(item.get("asset_id") or "") for item in assets],
        }

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
        final_clips = self._complete_story_coverage([], candidates, treatment)
        final_clips = self._remove_overlaps(final_clips)
        global_look = {
            "clean_neutral": "neutral", "cinematic_warm": "warm",
            "cool_steel": "cool", "high_contrast": "contrast",
        }.get(str(treatment.get("creative_look") or ""), "neutral")
        for clip in final_clips:
            # One film gets one creative baseline.  Per-shot look changes made
            # matching Sony Log sources appear inconsistent and are never a
            # safe substitute for a deliberate scene-level grade.
            # 一部成片只使用一个创意基线，禁止逐镜头随机冷暖漂移。
            clip["color_look"] = global_look
        final_clips = self._fit_target_duration(final_clips, treatment)
        final_clips = self._apply_creative_grade_plan(final_clips, treatment)
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

        Every saved one-fps visual sample is inspected exactly once in a small
        overlapping transport batch. A rolling continuity summary carries the
        whole source's state between calls. A second constrained model call sees
        the completed source summaries and candidates from every asset.

        每个已保存的一帧/秒视觉证据都会在小型重叠传输批次中被审看；滚动连续性摘要
        在调用间携带整条素材状态。第二次受约束模型调用会看到完整素材摘要与全部候选。
        """
        assets = raw_data["assets"]
        chunks: List[Dict[str, Any]] = []
        for asset_order, asset in enumerate(assets):
            # Ollama cannot accept thousands of images in one request. These
            # overlapping 16-second batches are transport envelopes only: no
            # saved frame is sampled away, and continuity is carried forward.
            # Ollama 无法一次接收数千张图片。16 秒重叠批次仅是传输容器：不会再次
            # 抽样丢帧，并会把连续性状态传给下一批。
            asset_chunks = self.chunk_raw_data(
                asset, window_sec=16.0, overlap_sec=2.0
            )
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
        self._asset_continuity_summaries = {}
        try:
            self.check_ollama(require_vision=True)
            self._music_analysis = self.load_music_analysis()
            analyzed_tracks = self._music_analysis.get("tracks", [])
            self._music_files = [
                Path(str(item.get("file_name"))).expanduser().resolve()
                for item in analyzed_tracks
                if isinstance(item, dict) and str(item.get("file_name") or "").strip()
            ] or self.discover_music_files()
            treatment = self.load_treatment(assets)
            self._active_treatment = treatment
            candidates.extend(self.candidates_from_treatment(treatment, assets))
            for index, chunk in enumerate(chunks, start=1):
                asset_id = str(chunk["asset_id"])
                chunk["continuity_context"] = self._asset_continuity_summaries.get(
                    asset_id,
                    "This is the beginning of the source; no earlier visual state exists.",
                )
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
                    cached_summary = next(
                        (
                            str(item.get("continuity_summary") or "").strip()
                            for item in reversed(cached_decisions)
                            if isinstance(item, dict)
                            and str(item.get("continuity_summary") or "").strip()
                        ),
                        "",
                    )
                    if cached_summary:
                        self._asset_continuity_summaries[asset_id] = cached_summary
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
                continuity_summary = self._compact_prompt_text(
                    response_payload.get("continuity_summary")
                    or self._local_continuity_summary(chunk, decisions),
                    1200,
                )
                self._asset_continuity_summaries[asset_id] = continuity_summary
                for decision in decisions:
                    decision["asset_id"] = asset_id
                    decision["source_order"] = int(chunk["source_order"])
                    decision["continuity_summary"] = continuity_summary
                candidates.extend(decisions)
                completed_chunks[chunk_key] = [dict(item) for item in decisions]
                self._write_director_checkpoint(
                    checkpoint_path,
                    checkpoint_fingerprint,
                    completed_chunks,
                )

            candidates = self.merge_decisions(candidates)
            candidate_limit = max(
                20,
                min(96, self._effective_num_ctx(self.text_model) // 256),
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
                    "视觉候选完成，卸载 %s 后加载文字导演 %s / Switching from vision to text director",
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

        program_duration = sum(
            max(0.0, float(item.get("cut_out_sec", 0)) - float(item.get("cut_in_sec", 0)))
            for item in final_clips
        )
        music_plan = self.validate_music_plan(
            sequence_payload.get("music_plan"), program_duration
        )
        music_plan = self.enrich_music_sync_points(final_clips, music_plan)
        final_clips = self.snap_visual_cuts_to_beats(final_clips, music_plan, assets)
        music_plan["program_duration_sec"] = round(
            sum(
                max(
                    0.0,
                    float(item.get("cut_out_sec", 0))
                    - float(item.get("cut_in_sec", 0)),
                )
                for item in final_clips
            ),
            4,
        )
        color_pipeline = self.build_color_pipeline(
            treatment, assets, raw_data.get("color_match_plan")
        )
        color_sources = color_pipeline.get("sources", {})
        transcript_by_asset = {
            str(asset.get("asset_id") or ""): [
                segment for segment in asset.get("transcript", [])
                if isinstance(segment, dict) and str(segment.get("text") or "").strip()
            ]
            for asset in assets
        }
        for index, clip in enumerate(final_clips, start=1):
            clip["clip_id"] = index
            clip_start = float(clip.get("cut_in_sec", 0))
            clip_end = float(clip.get("cut_out_sec", 0))
            clip["has_dialogue"] = any(
                min(clip_end, float(segment.get("end_sec", 0)))
                - max(clip_start, float(segment.get("start_sec", 0)))
                >= 0.15
                for segment in transcript_by_asset.get(
                    str(clip.get("asset_id") or ""), []
                )
            )
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
            "visual_review": {
                "mode": "continuous_all_saved_samples",
                "transport_batch_sec": 16.0,
                "transport_overlap_sec": 2.0,
                "transport_batch_count": len(chunks),
                "saved_visual_sample_count": sum(
                    len(asset.get("keyframes", [])) for asset in assets
                ),
                "second_stage_frame_subsampling": False,
                "continuity_summaries": dict(self._asset_continuity_summaries),
            },
            "candidate_count": len(candidates),
            "candidate_audit": candidates,
            "project_summary": str(sequence_payload.get("project_summary") or "").strip(),
            "full_review_synopsis": sequence_payload.get("coverage_synopsis", {}),
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

    def chunk_raw_data(
        self,
        raw_data: Dict[str, Any],
        window_sec: Optional[float] = None,
        overlap_sec: float = 0.0,
    ) -> List[Dict[str, Any]]:
        """
        Partition transcript/keyframes into non-overlapping time windows.
        将台词和关键帧划分为互不重叠的时间窗口。

        Transcript segments are assigned by midpoint, ensuring every segment is
        sent exactly once and avoiding duplicated cuts at chunk boundaries.

        台词片段按中点归属，保证每条台词只发送一次，避免分块边界的重复剪辑。

        Parameters / 参数:
            raw_data: One extracted source record. / 一条已提取的素材记录。
            window_sec: Optional local visual-review window override. /
                可选的局部视觉审片窗口秒数。
            overlap_sec: Context overlap on both sides of each transport batch. /
                每个传输批次两侧的连续性上下文重叠秒数。
        """
        duration = float(raw_data["duration_sec"])
        transcript = raw_data["transcript"]
        keyframes = raw_data.get("keyframes", [])
        if not isinstance(keyframes, list):
            keyframes = []

        chunks: List[Dict[str, Any]] = []
        effective_window = float(window_sec or self.chunk_duration_sec)
        if not math.isfinite(effective_window) or effective_window <= 0:
            raise DirectorError("window_sec 必须大于 0 / must be positive.")
        effective_overlap = float(overlap_sec)
        if (
            not math.isfinite(effective_overlap)
            or effective_overlap < 0
            or effective_overlap >= effective_window / 2.0
        ):
            raise DirectorError(
                "overlap_sec 必须在 [0, window_sec/2) / overlap is out of range."
            )
        core_start = 0.0
        index = 0
        while core_start < duration:
            core_end = min(duration, core_start + effective_window)
            start = max(0.0, core_start - effective_overlap)
            end = min(duration, core_end + effective_overlap)
            is_last = core_end >= duration
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
                        "core_start_sec": round(core_start, 3),
                        "core_end_sec": round(core_end, 3),
                        "transcript": chunk_segments,
                        "keyframes": chunk_keyframes,
                    }
                )
                index += 1
            core_start = core_end
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
                "请安装视觉模型，例如：ollama pull qwen3.6:27b-mtp-q8_0\n"
                "The selected model has no vision capability. Install a vision model."
            )
        normalized = self.model.casefold().replace("_", "-")
        known_vision_markers = (
            "qwen3.6", "qwen3.5", "qwen2.5-vl", "gemma3", "llava", "minicpm-v",
            "llama3.2-vision", "moondream",
        )
        if not any(marker in normalized for marker in known_vision_markers):
            raise DirectorError(
                f"无法确认模型 {self.model!r} 支持视觉输入。请改用 qwen3.6 等视觉模型。\n"
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
        # Full-review batches are already bounded by time. Never perform a
        # second representative-frame selection here: every extracted temporal
        # sample must reach the vision model.
        # 完整审片批次已经按时间限制；此处禁止再次挑代表帧，所有提取帧都必须送入模型。
        selected_frames = list(chunk.get("keyframes", []))
        if include_images and len(selected_frames) > 32:
            raise DirectorError(
                "单个连续审片批次超过 32 张图；请使用默认 1 fps，不能静默丢帧。"
                " / A full-review batch exceeds 32 images; use the default 1 fps."
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
        desired_num_predict = max(1024, min(4096, request_num_ctx // 4))
        system_prompt = (
            "You are a senior documentary editor and visual storyteller. "
            "Use only supplied transcript, timestamps, and images. Return "
            "only the requested JSON and never invent content."
        )
        schema_text = json.dumps(schema, ensure_ascii=False, separators=(",", ":"))
        estimated_input_tokens = self._estimate_prompt_tokens(
            system_prompt + "\n" + prompt + "\n" + schema_text
        )
        # Ollama's num_ctx is shared by the prompt and generated response. Keep
        # a safety margin and lower num_predict only when a compact request is
        # still close to the configured window. Semantic prompt compaction is
        # performed by the caller (not by blindly truncating JSON evidence).
        # Ollama 的 num_ctx 由输入和输出共享。这里保留安全余量；语义压缩由调用方
        # 完成，绝不从中间粗暴截断 JSON 证据。
        available_output_tokens = request_num_ctx - estimated_input_tokens - 256
        num_predict = min(
            desired_num_predict,
            max(1024, available_output_tokens),
        )
        utilization = estimated_input_tokens / max(1, request_num_ctx)
        if utilization >= 0.70:
            self.logger.warning(
                "Ollama 上下文预检：预计输入约 %d token，保留输出 %d / 总计 %d；"
                "若仍超限将由分阶段导演避免截断 / Context preflight: ~%d input, "
                "%d output of %d tokens",
                estimated_input_tokens,
                num_predict,
                request_num_ctx,
                estimated_input_tokens,
                num_predict,
                request_num_ctx,
            )
        else:
            self.logger.debug(
                "Ollama context preflight: ~%d input + %d output <= %d",
                estimated_input_tokens,
                num_predict,
                request_num_ctx,
            )
        if "qwen3.6" in normalized_model:
            # Qwen3.6 commonly spends the entire first structured generation in
            # its separate thinking channel, then returns no parseable JSON.
            # Starting with thinking disabled preserves schema enforcement and
            # avoids doing every expensive mixed-memory request twice.
            # Qwen3.6 常把首轮结构化生成预算消耗在独立 thinking 字段中，最终没有
            # 可解析 JSON。首轮直接关闭显式思考仍保留 Schema 约束，并避免混合内存
            # 推理的每个请求都重复执行一次。
            attempts = (
                ("direct-json", schema, False),
                ("compatibility-json", "json", False),
            )
        else:
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
                "system": system_prompt,
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
                if think_value is False:
                    retry_message = (
                        "Ollama 未返回可用 JSON（模式=%s, done_reason=%s, prompt_tokens=%s, "
                        "output_tokens=%s, thinking_chars=%d）；切换 JSON 兼容模式并重试 / "
                        "Ollama returned no usable JSON; retrying with JSON compatibility mode"
                    )
                else:
                    retry_message = (
                        "Ollama 未返回可用 JSON（模式=%s, done_reason=%s, prompt_tokens=%s, "
                        "output_tokens=%s, thinking_chars=%d）；关闭显式思考并重试 / "
                        "Ollama returned no usable JSON; retrying without explicit thinking"
                    )
                self.logger.warning(
                    retry_message,
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

    @staticmethod
    def _estimate_prompt_tokens(value: str) -> int:
        """
        Conservatively estimate mixed Chinese/English JSON prompt tokens.
        保守估算中英混合 JSON Prompt 的 token 数。

        This is a dependency-free guard, not a tokenizer replacement. ASCII
        JSON is budgeted at roughly 2.5 characters/token while UTF-8 byte size
        protects CJK text, where one visible character is often one token.
        该方法是零依赖预算守门而非 tokenizer；ASCII JSON 按约 2.5 字符/token，
        同时用 UTF-8 字节数保护通常接近一字一 token 的中日韩文本。
        """
        text = str(value or "")
        return max(
            1,
            int(math.ceil(len(text) / 2.5)),
            int(math.ceil(len(text.encode("utf-8")) / 3.0)),
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

    def _local_continuity_summary(
        self,
        chunk: Dict[str, Any],
        decisions: Sequence[Dict[str, Any]],
    ) -> str:
        """
        Build a deterministic rolling-summary fallback for model/test compatibility.
        为模型兼容与测试构建确定性的滚动摘要回退。

        Parameters / 参数:
            chunk: Current continuous-review transport batch. / 当前连续审片传输批次。
            decisions: Validated editorial observations in the batch. / 本批次有效观察。
        """
        previous = self._compact_prompt_text(
            chunk.get("continuity_context", ""), 700
        )
        observations = [
            "{:.1f}-{:.1f}s {} [{}; {}]".format(
                float(item.get("cut_in_sec", 0)),
                float(item.get("cut_out_sec", 0)),
                self._compact_prompt_text(
                    item.get("subject_action") or item.get("visual_summary") or "",
                    180,
                ),
                item.get("action_phase", "action"),
                self._compact_prompt_text(item.get("emotion", ""), 80),
            )
            for item in decisions[-4:]
        ]
        current = "; ".join(observations) or (
            f"No edit-worthy event in {float(chunk.get('core_start_sec', chunk['start_sec'])):.1f}-"
            f"{float(chunk.get('core_end_sec', chunk['end_sec'])):.1f}s; continuity remains unchanged."
        )
        return self._compact_prompt_text(f"{previous} Latest: {current}", 1200)

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
        core_start = float(chunk.get("core_start_sec", chunk["start_sec"]))
        core_end = float(chunk.get("core_end_sec", chunk["end_sec"]))
        keyframe_lines = [
            "IMAGE_{} [{:.3f}s] role={} scene_score={} file={}".format(
                index,
                float(item.get("timestamp_sec", 0)),
                (
                    "OVERLAP_CONTEXT"
                    if float(item.get("timestamp_sec", 0)) < core_start
                    or float(item.get("timestamp_sec", 0)) >= core_end
                    else "NEW_CONTINUOUS_EVIDENCE"
                ),
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
            "The images are time-ordered evidence, not independent thumbnails: compare "
            "adjacent timestamps and describe how subject action, camera motion, emotion, "
            "and shot scale progress. Infer continuity only from adjacent supplied frames; "
            "never invent an unseen action. Fill subject_action, action_phase, continuity_tags, "
            "and rhythmic_potential so the final director can match action to music. Prefer complete "
            "Read CONTINUITY FROM THE PREVIOUS BATCH first, then update continuity_summary "
            "into a cumulative account of the source so far. Preserve identities, locations, "
            "ongoing actions, unresolved intentions, and meaningful changes; do not reset the "
            "story at this transport boundary. OVERLAP_CONTEXT images are shown only to reconnect "
            "motion and must not create duplicate edit decisions. "
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
            "CORE RANGE FOR NEW EVIDENCE: {core_start:.3f}s to {core_end:.3f}s\n"
            "CONTINUITY FROM PREVIOUS BATCH: {continuity}\n"
            "DIRECTOR TREATMENT: {treatment}\n"
            "Required JSON schema: {schema}\n\n"
            "TRANSCRIPT:\n{transcript}\n\nKEYFRAMES:\n{keyframes}"
        ).format(
            start=float(chunk["start_sec"]),
            end=float(chunk["end_sec"]),
            source=source_name,
            source_order=int(chunk.get("source_order", 0)),
            core_start=core_start,
            core_end=core_end,
            continuity=self._compact_prompt_text(
                chunk.get("continuity_context", ""), 1200
            ),
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
        representative_frames: List[Dict[str, Any]] = []
        if len(assets) <= 12:
            representative_assets = list(assets)
        else:
            representative_assets = [
                assets[round(index * (len(assets) - 1) / 11)]
                for index in range(12)
            ]
        per_asset_limit = max(
            1, min(3, 12 // max(1, len(representative_assets)))
        )
        for asset in representative_assets:
            for frame in self._select_keyframes(
                asset.get("keyframes", []), limit=per_asset_limit
            ):
                annotated = dict(frame)
                annotated["asset_id"] = str(asset.get("asset_id") or "")
                representative_frames.append(annotated)
        representative_frames, images = self._encode_images(
            representative_frames[:12]
        )
        image_legend = [
            {
                "image_index": index,
                "asset_id": frame.get("asset_id", ""),
                "timestamp_sec": round(float(frame.get("timestamp_sec", 0)), 3),
            }
            for index, frame in enumerate(representative_frames, start=1)
        ]
        compact_assets: List[Dict[str, Any]] = []
        transcript_budget = max(
            2400,
            min(8000, int(self._effective_num_ctx(self.model) * 0.40)),
        )
        per_asset_budget = max(160, transcript_budget // max(1, len(assets)))
        for source_order, asset in enumerate(assets):
            transcript, transcript_count = self._compact_transcript_excerpt(
                asset.get("transcript", []), per_asset_budget
            )
            compact_assets.append(
                {
                    "source_order": source_order,
                    "asset_id": asset.get("asset_id", ""),
                    "file": Path(str(asset.get("source_video") or "")).name,
                    "duration_sec": round(float(asset.get("duration_sec", 0)), 2),
                    "transcript_excerpt": transcript,
                    "transcript_segment_count": transcript_count,
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
            "Design the music before searching: provide 2-6 specific multilingual "
            "search queries, instrumentation, a useful tempo range, vocal policy, "
            "one to three cues, and intentional silence. Prefer instrumental music "
            "under dialogue. Search terms must describe emotion, genre, pacing, and "
            "instrumentation rather than copyrighted song titles. Treat the requested "
            "duration as an editorial target, never as permission to pad with repeated "
            "or weak footage. Create an executable color_bible that supports the theme: "
            "choose one coherent palette and subtle opening/development/payoff/ending grade "
            "changes. Base it on the real lighting and emotional arc, preserve skin and practical "
            "light color, and avoid random shot-by-shot tint changes. The camera profile is only "
            "a technical input transform; the color_bible is the creative grade. Return JSON only.\n"
            f"USER CREATIVE BRIEF: {brief}\n"
            f"REQUESTED TARGET DURATION: {self._active_target_duration_sec:.1f} seconds\n"
            f"CAMERA PROFILE: {self.camera_profile}\n"
            f"REPRESENTATIVE IMAGE ORDER: {json.dumps(image_legend, ensure_ascii=False)}\n"
            f"SCHEMA: {json.dumps(TREATMENT_SCHEMA, ensure_ascii=False)}\n"
            f"SOURCES: {json.dumps(compact_assets, ensure_ascii=False)}"
        )
        self.logger.info(
            "正在生成导演阐述与叙事弧线 / Creating director treatment and story arc"
        )
        return self._request_json(prompt, TREATMENT_SCHEMA, images=images)

    @staticmethod
    def _compact_transcript_excerpt(
        raw_segments: object, character_budget: int
    ) -> tuple[str, int]:
        """
        Sample dialogue across the full source instead of keeping only its start.
        在整个素材范围均匀采样台词，而不是只保留开头。

        Parameters / 参数:
            raw_segments: Extractor transcript records. / 提取层台词记录。
            character_budget: Maximum prompt characters for this asset. / 本素材最大提示字符数。
        """
        if not isinstance(raw_segments, list):
            return "", 0
        lines = [
            "[{:.1f}-{:.1f}] {}".format(
                float(segment.get("start_sec", 0)),
                float(segment.get("end_sec", 0)),
                " ".join(str(segment.get("text", "")).split()),
            )
            for segment in raw_segments
            if isinstance(segment, dict) and str(segment.get("text") or "").strip()
        ]
        if not lines:
            return "", 0
        budget = max(80, int(character_budget))
        combined = " ".join(lines)
        if len(combined) <= budget:
            return combined, len(lines)
        sample_count = max(2, min(len(lines), budget // 100))
        if sample_count == 1:
            indexes = [0]
        else:
            indexes = sorted({
                round(index * (len(lines) - 1) / (sample_count - 1))
                for index in range(sample_count)
            })
        selected: List[str] = []
        used = 0
        for source_index in indexes:
            line = lines[source_index]
            remaining = budget - used
            if remaining <= 0:
                break
            if len(line) > remaining:
                line = line[: max(0, remaining - 1)].rstrip() + "…"
            selected.append(line)
            used += len(line) + 1
        return " ".join(selected), len(lines)

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
        treatment["color_bible"] = self._validate_color_bible(
            payload.get("color_bible"), look
        )
        queries = payload.get("music_search_queries")
        treatment["music_search_queries"] = [
            " ".join(str(value).split())
            for value in (queries if isinstance(queries, list) else [])
            if str(value).strip()
        ][:6]
        if len(treatment["music_search_queries"]) < 2:
            base = f"{treatment['music_mood']} {treatment['music_energy_arc']} instrumental cinematic"
            treatment["music_search_queries"] = [
                base,
                f"{treatment['central_theme']} emotional documentary score no vocals",
            ]
        instruments = payload.get("music_instrumentation")
        treatment["music_instrumentation"] = [
            " ".join(str(value).split())
            for value in (instruments if isinstance(instruments, list) else [])
            if str(value).strip()
        ][:8] or ["cinematic acoustic ensemble"]
        tempo_min = min(220.0, max(40.0, float(payload.get("music_tempo_min_bpm", 70))))
        tempo_max = min(220.0, max(40.0, float(payload.get("music_tempo_max_bpm", 130))))
        if tempo_max < tempo_min:
            tempo_min, tempo_max = tempo_max, tempo_min
        treatment["music_tempo_min_bpm"] = round(tempo_min, 1)
        treatment["music_tempo_max_bpm"] = round(tempo_max, 1)
        treatment["music_vocal_policy"] = self._enum_value(
            payload.get("music_vocal_policy"),
            {"instrumental_only", "vocals_allowed", "vocals_preferred"},
            "instrumental_only",
        )
        try:
            cue_count = int(payload.get("music_cue_count", 2))
        except (TypeError, ValueError):
            cue_count = 2
        treatment["music_cue_count"] = min(3, max(1, cue_count))
        treatment["music_silence_strategy"] = " ".join(
            str(payload.get("music_silence_strategy") or "Leave key dialogue and the ending breath unscored.").split()
        )
        treatment["music_license_intent"] = self._enum_value(
            payload.get("music_license_intent"),
            {"commercial_safe", "noncommercial", "user_authorized_any"},
            "commercial_safe",
        )
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

    @staticmethod
    def _validate_color_bible(payload: Any, creative_look: str) -> Dict[str, Any]:
        """
        Clamp an AI-authored creative grade into a coherent executable plan.
        将 AI 编写的创意调色方案限制为连贯、可执行的安全参数。

        Parameters / 参数:
            payload: Model ``color_bible`` object. / 模型返回的调色圣经对象。
            creative_look: Validated legacy look used as fallback. / 已校验的旧版风格回退值。
        """
        value = payload if isinstance(payload, dict) else {}
        palette_fallback = {
            "clean_neutral": "natural",
            "cinematic_warm": "warm_memory",
            "cool_steel": "cool_moonlight",
            "high_contrast": "desaturated_grit",
        }.get(creative_look, "natural")
        palettes = {
            "natural", "teal_amber", "cool_moonlight", "warm_memory",
            "desaturated_grit", "neon_night",
        }
        look_defaults = {
            "clean_neutral": (1.0, 1.0, 0.0),
            "cinematic_warm": (1.03, 1.04, 0.28),
            "cool_steel": (1.04, 1.01, -0.28),
            "high_contrast": (1.10, 0.94, 0.0),
        }.get(creative_look, (1.0, 1.0, 0.0))

        def number(key: str, default: float, low: float, high: float) -> float:
            try:
                result = float(value.get(key, default))
            except (TypeError, ValueError):
                result = default
            return round(min(high, max(low, result)), 3)

        result: Dict[str, Any] = {
            "global_palette": str(value.get("global_palette") or palette_fallback)
            if str(value.get("global_palette") or palette_fallback) in palettes
            else palette_fallback,
            "contrast": number("contrast", look_defaults[0], 0.85, 1.25),
            "saturation": number("saturation", look_defaults[1], 0.75, 1.25),
            "warmth": number("warmth", look_defaults[2], -1.0, 1.0),
            "highlight_rolloff": number("highlight_rolloff", 0.45, 0.0, 1.0),
        }
        defaults = {
            "opening": (0.0, 0.98, 0.95, -0.05),
            "development": (0.0, 1.0, 1.0, 0.0),
            "payoff": (0.08, 1.06, 1.04, 0.05),
            "ending": (-0.03, 0.98, 0.96, 0.02),
        }
        supplied = {
            str(item.get("beat") or "").casefold(): item
            for item in value.get("chapter_grades", [])
            if isinstance(item, dict)
        }
        chapters: List[Dict[str, Any]] = []
        for beat, fallback in defaults.items():
            raw = supplied.get(beat, {})

            def chapter_number(key: str, default: float, low: float, high: float) -> float:
                try:
                    parsed = float(raw.get(key, default))
                except (TypeError, ValueError):
                    parsed = default
                return round(min(high, max(low, parsed)), 3)

            chapters.append({
                "beat": beat,
                "exposure_ev": chapter_number("exposure_ev", fallback[0], -0.5, 0.5),
                "contrast": chapter_number("contrast", fallback[1], 0.9, 1.2),
                "saturation": chapter_number("saturation", fallback[2], 0.8, 1.2),
                "warmth": chapter_number("warmth", fallback[3], -0.5, 0.5),
                "reason": " ".join(
                    str(raw.get("reason") or f"Subtle {beat} emotional progression.").split()
                ),
            })
        result["chapter_grades"] = chapters
        return result

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
                "subject_action": str(anchor.get("reason") or "Treatment anchor"),
                "emotion": "treatment-led",
                "action_phase": beat if beat in {"setup", "build", "payoff"} else (
                    "setup" if beat == "opening" else "aftermath" if beat == "ending" else "build"
                ),
                "shot_scale": "medium",
                "camera_motion": "static",
                "continuity_tags": [beat],
                "rhythmic_potential": 0.5,
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
            "color_bible": treatment.get("color_bible", {}),
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

    def validate_music_plan(
        self, payload: Any, program_duration_sec: float = 0.0
    ) -> Dict[str, Any]:
        """
        Resolve a one-to-three-cue model plan only against analyzed files.
        仅允许模型在已分析曲目中解析一至三段配乐计划。

        Parameters / 参数:
            payload: Raw model ``music_plan``. / 模型原始配乐计划。
            program_duration_sec: Final picture duration. / 最终画面时长。
        """
        value = payload if isinstance(payload, dict) else {}
        raw_cues = value.get("cues")
        if not isinstance(raw_cues, list):
            # Backward-compatible conversion for schema 3.0 single-track plans.
            requested = str(value.get("track_file") or "").strip()
            raw_cues = [] if not requested else [{
                "cue_id": "M1", "track_file": requested,
                "story_beat": "development", "timeline_in_sec": 0,
                "timeline_out_sec": program_duration_sec,
                "track_in_sec": 0, "track_out_sec": program_duration_sec,
                "reason": value.get("reason", ""),
                "target_lufs": -24, "fade_in_sec": value.get("fade_in_sec", 2),
                "fade_out_sec": value.get("fade_out_sec", 3),
                "crossfade_sec": 0,
                "duck_under_dialogue_db": -9 if value.get("duck_dialogue", True) else 0,
                "sync_points": [],
            }]
        tracks = [
            item for item in self._music_analysis.get("tracks", [])
            if isinstance(item, dict) and str(item.get("file_name") or "").strip()
        ]
        by_name: Dict[str, Dict[str, Any]] = {}
        for item in tracks:
            path = Path(str(item["file_name"])).expanduser().resolve()
            by_name[path.name.casefold()] = item
            by_name[str(path).casefold()] = item

        def number(raw: Any, default: float, minimum: float, maximum: float) -> float:
            try:
                result = float(raw)
            except (TypeError, ValueError):
                result = default
            return min(maximum, max(minimum, result))

        program_duration = max(0.0, float(program_duration_sec or 0.0))
        cues: List[Dict[str, Any]] = []
        credits: List[Dict[str, Any]] = []
        for cue_index, raw in enumerate(raw_cues[:3], start=1):
            if not isinstance(raw, dict):
                continue
            requested = str(raw.get("track_file") or "").strip()
            analyzed = by_name.get(requested.casefold())
            if analyzed is None:
                if requested:
                    self.logger.warning(
                        "AI 选择了候选库之外的曲目 %r，已忽略 / Ignoring music outside analyzed candidates",
                        requested,
                    )
                continue
            selected = Path(str(analyzed["file_name"])).expanduser().resolve()
            track_duration = max(0.0, float(analyzed.get("duration_sec", 0) or 0))
            timeline_in = number(raw.get("timeline_in_sec"), 0, 0, program_duration)
            timeline_out = number(
                raw.get("timeline_out_sec"), program_duration, timeline_in, program_duration
            )
            if timeline_out - timeline_in < 0.25:
                continue
            track_in = number(raw.get("track_in_sec"), 0, 0, track_duration)
            desired = timeline_out - timeline_in
            track_out = number(
                raw.get("track_out_sec"), track_in + desired, track_in, track_duration
            )
            usable = min(desired, track_out - track_in)
            if usable < 0.25:
                continue
            timeline_out = timeline_in + usable
            track_out = track_in + usable
            sync_points: List[Dict[str, Any]] = []
            for point in raw.get("sync_points", []) if isinstance(raw.get("sync_points"), list) else []:
                if not isinstance(point, dict):
                    continue
                point_timeline = number(point.get("timeline_sec"), timeline_in, timeline_in, timeline_out)
                point_track = number(point.get("track_sec"), track_in, track_in, track_out)
                sync_points.append({
                    "timeline_sec": round(point_timeline, 4),
                    "track_sec": round(point_track, 4),
                    "type": self._enum_value(
                        point.get("type"),
                        {"beat", "strong_beat", "downbeat", "section", "energy_peak"},
                        "strong_beat",
                    ),
                    "purpose": " ".join(str(point.get("purpose") or "Musical sync").split()),
                })
            def cue_times(field_name: str) -> List[float]:
                """Keep only analyzed events that occur inside this cue. / 仅保留 cue 内的已分析事件。"""
                return [
                    round(float(event), 4)
                    for event in analyzed.get(field_name, [])
                    if isinstance(event, (int, float))
                    and track_in <= float(event) <= track_out
                ]
            cue_sections = [
                dict(section)
                for section in analyzed.get("sections", [])
                if isinstance(section, dict)
                and float(section.get("end_sec", 0) or 0) >= track_in
                and float(section.get("start_sec", 0) or 0) <= track_out
            ]
            cue = {
                "cue_id": str(raw.get("cue_id") or f"M{cue_index}"),
                "file_name": str(selected),
                "track_file": selected.name,
                "story_beat": self._enum_value(
                    raw.get("story_beat"),
                    {"opening", "development", "payoff", "ending"},
                    "development",
                ),
                "timeline_in_sec": round(timeline_in, 4),
                "timeline_out_sec": round(timeline_out, 4),
                "track_in_sec": round(track_in, 4),
                "track_out_sec": round(track_out, 4),
                "reason": " ".join(str(raw.get("reason") or "").split()),
                "target_lufs": round(number(raw.get("target_lufs"), -24, -36, -14), 2),
                "fade_in_sec": round(number(raw.get("fade_in_sec"), 1.5, 0, min(12, usable / 2)), 3),
                "fade_out_sec": round(number(raw.get("fade_out_sec"), 2.0, 0, min(12, usable / 2)), 3),
                "crossfade_sec": round(number(raw.get("crossfade_sec"), 1.5, 0, min(8, usable / 2)), 3),
                "duck_under_dialogue_db": round(number(raw.get("duck_under_dialogue_db"), -9, -24, 0), 2),
                "sync_points": sync_points,
                "tempo_bpm": float(analyzed.get("tempo_bpm", 0) or 0),
                "key": str(analyzed.get("key") or ""),
                "mode": str(analyzed.get("mode") or ""),
                "integrated_lufs": analyzed.get("integrated_lufs"),
                "beats_sec": cue_times("beats_sec"),
                "strong_beats_sec": cue_times("strong_beats_sec"),
                "downbeats_sec": cue_times("downbeats_sec"),
                "sections": cue_sections,
                "license": str(analyzed.get("license") or "user-supplied"),
                "license_url": str(analyzed.get("license_url") or ""),
                "license_provenance": str(analyzed.get("license_provenance") or ""),
                "source_url": str(analyzed.get("source_url") or ""),
                "sha256": str(analyzed.get("sha256") or ""),
            }
            cues.append(cue)
            credits.append({
                "title": str(analyzed.get("title") or selected.stem),
                "artist": str(analyzed.get("artist") or analyzed.get("uploader") or ""),
                "source_url": cue["source_url"],
                "license": cue["license"],
                "license_url": cue["license_url"],
            })
        cues.sort(key=lambda item: (float(item["timeline_in_sec"]), item["cue_id"]))
        silence_regions: List[Dict[str, Any]] = []
        for raw in value.get("silence_regions", []) if isinstance(value.get("silence_regions"), list) else []:
            if not isinstance(raw, dict):
                continue
            start = number(raw.get("timeline_in_sec"), 0, 0, program_duration)
            end = number(raw.get("timeline_out_sec"), start, start, program_duration)
            if end > start:
                silence_regions.append({
                    "timeline_in_sec": round(start, 3),
                    "timeline_out_sec": round(end, 3),
                    "reason": " ".join(str(raw.get("reason") or "Intentional silence").split()),
                })
        return {
            "mode": "multi_cue_pre_mix" if cues else "none",
            "strategy": " ".join(str(value.get("strategy") or "").split()),
            "program_duration_sec": round(program_duration, 4),
            "bed_file": "",
            "silence_regions": silence_regions,
            "cues": cues,
            "credits": credits,
        }

    def snap_visual_cuts_to_beats(
        self,
        clips: Sequence[Dict[str, Any]],
        music_plan: Dict[str, Any],
        assets: Sequence[Dict[str, Any]],
        max_shift_sec: float = 0.45,
    ) -> List[Dict[str, Any]]:
        """
        Nudge visual-only out-points to nearby beats while preserving source bounds.
        在不越过素材边界的前提下，把纯画面出点轻微吸附到邻近鼓点。

        Dialogue and closing thoughts are never time-warped or truncated for a beat.
        对话与结尾语义绝不会为了卡点被截断或变速。
        """
        absolute_beats: List[float] = []
        absolute_priority: List[float] = []
        cues = music_plan.get("cues")
        if isinstance(cues, list):
            for cue in cues:
                if not isinstance(cue, dict):
                    continue
                emphasized = list(cue.get("downbeats_sec") or []) + list(
                    cue.get("strong_beats_sec") or []
                )
                source_beats = sorted({
                    float(value) for value in emphasized
                    if isinstance(value, (int, float))
                }) or [
                    float(value) for index, value in enumerate(cue.get("beats_sec") or [])
                    if isinstance(value, (int, float)) and index % 2 == 0
                ]
                track_in = float(cue.get("track_in_sec", 0) or 0)
                track_out = float(cue.get("track_out_sec", 0) or 0)
                timeline_in = float(cue.get("timeline_in_sec", 0) or 0)
                absolute_beats.extend(
                    timeline_in + float(beat) - track_in
                    for beat in source_beats
                    if isinstance(beat, (int, float)) and track_in <= float(beat) <= track_out
                )
                absolute_priority.extend(
                    float(point.get("timeline_sec", 0))
                    for point in cue.get("sync_points", [])
                    if isinstance(point, dict)
                    and str(point.get("type") or "") in {"downbeat", "section", "energy_peak"}
                    and isinstance(point.get("timeline_sec"), (int, float))
                )
        else:
            # Legacy schema fallback.
            beats = music_plan.get("beats_sec", [])
            absolute_beats.extend(
                float(value) for value in beats if isinstance(value, (int, float))
            )
        if not absolute_beats:
            return [dict(item) for item in clips]
        asset_duration = {
            str(asset.get("asset_id") or ""): float(asset.get("duration_sec", 0) or 0)
            for asset in assets
        }
        transcript_by_asset = {
            str(asset.get("asset_id") or ""): [
                segment for segment in asset.get("transcript", [])
                if isinstance(segment, dict) and str(segment.get("text") or "").strip()
            ]
            for asset in assets
        }
        result: List[Dict[str, Any]] = []
        timeline_cursor = 0.0
        for original in clips:
            item = dict(original)
            duration = float(item.get("cut_out_sec", 0)) - float(item.get("cut_in_sec", 0))
            proposed_end = timeline_cursor + duration
            source_in = float(item.get("cut_in_sec", 0))
            source_out = float(item.get("cut_out_sec", 0))
            has_dialogue = str(item.get("story_role") or "").casefold() == "interview" or any(
                min(source_out, float(segment.get("end_sec", 0)))
                - max(source_in, float(segment.get("start_sec", 0))) >= 0.15
                for segment in transcript_by_asset.get(str(item.get("asset_id") or ""), [])
            )
            music_role = str(item.get("music_edit_role") or "on_beat").casefold()
            landmarks = (
                absolute_priority
                if music_role in {"phrase_start", "payoff_hit", "release"} and absolute_priority
                else absolute_beats
            )
            if not has_dialogue and music_role != "natural_sound" and landmarks:
                nearest = min(landmarks, key=lambda beat: abs(beat - proposed_end))
                shift = nearest - proposed_end
                source_end = float(item.get("cut_out_sec", 0)) + shift
                maximum = asset_duration.get(str(item.get("asset_id") or ""), source_end)
                new_duration = duration + shift
                allowed_shift = (
                    0.75 if music_role == "payoff_hit"
                    else 0.60 if music_role in {"phrase_start", "release"}
                    else max_shift_sec
                )
                if (
                    abs(shift) <= allowed_shift
                    and new_duration >= 0.4
                    and source_end <= maximum + 1e-6
                    and source_end > source_in
                ):
                    item["cut_out_sec"] = round(source_end, 3)
                    item["beat_snap"] = {
                        "timeline_beat_sec": round(nearest, 4),
                        "shift_sec": round(shift, 4),
                    }
                    duration = new_duration
            result.append(item)
            timeline_cursor += max(0.0, duration)
        return result

    def enrich_music_sync_points(
        self,
        clips: Sequence[Dict[str, Any]],
        music_plan: Dict[str, Any],
    ) -> Dict[str, Any]:
        """
        Ground sparse model sync points in analyzed musical landmarks.
        用已分析的音乐强拍补足模型过于稀疏的卡点计划。

        Parameters / 参数:
            clips: Selected picture edit with ``music_edit_role``. /
                带 ``music_edit_role`` 的入选画面序列。
            music_plan: Validated cue sheet containing analyzed beats. /
                含已分析鼓点的已校验 cue 表。
        """
        plan = dict(music_plan)
        cues = [dict(item) for item in plan.get("cues", []) if isinstance(item, dict)]
        if not cues:
            return plan
        boundaries: List[tuple[float, str]] = []
        cursor = 0.0
        for clip in clips[:-1]:
            cursor += max(
                0.0,
                float(clip.get("cut_out_sec", 0)) - float(clip.get("cut_in_sec", 0)),
            )
            role = str(clip.get("music_edit_role") or "on_beat").casefold()
            if role != "natural_sound":
                boundaries.append((cursor, role))
        aligned: List[Dict[str, Any]] = []
        for cue in cues:
            timeline_in = float(cue.get("timeline_in_sec", 0) or 0)
            timeline_out = float(cue.get("timeline_out_sec", 0) or 0)
            track_in = float(cue.get("track_in_sec", 0) or 0)
            track_out = float(cue.get("track_out_sec", 0) or 0)
            existing = [
                dict(point) for point in cue.get("sync_points", [])
                if isinstance(point, dict)
            ]
            events: List[tuple[float, str]] = []
            for field, kind in (("downbeats_sec", "downbeat"), ("strong_beats_sec", "strong_beat")):
                events.extend(
                    (float(value), kind)
                    for value in cue.get(field, [])
                    if isinstance(value, (int, float)) and track_in <= float(value) <= track_out
                )
            events.sort(key=lambda item: item[0])
            wanted = [
                (boundary, role) for boundary, role in boundaries
                if timeline_in <= boundary <= timeline_out
            ]
            wanted.sort(
                key=lambda item: (
                    0 if item[1] == "payoff_hit" else 1 if item[1] in {"phrase_start", "release"} else 2,
                    item[0],
                )
            )
            for boundary, role in wanted:
                if len(existing) >= 6 or not events:
                    break
                track_event, event_type = min(
                    events,
                    key=lambda item: abs((timeline_in + item[0] - track_in) - boundary),
                )
                timeline_event = timeline_in + track_event - track_in
                if abs(timeline_event - boundary) > 0.75:
                    continue
                if any(
                    abs(float(point.get("timeline_sec", -99)) - timeline_event) < 0.12
                    for point in existing
                ):
                    continue
                point = {
                    "timeline_sec": round(timeline_event, 4),
                    "track_sec": round(track_event, 4),
                    "type": event_type,
                    "purpose": f"Ground {role} picture transition on analyzed {event_type}.",
                }
                existing.append(point)
                aligned.append(point)
            existing.sort(key=lambda point: float(point.get("timeline_sec", 0)))
            cue["sync_points"] = existing[:6]
        plan["cues"] = cues
        plan["rhythm_audit"] = {
            "director_boundaries": len(boundaries),
            "grounded_sync_points_added": len(aligned),
            "method": "analyzed-downbeat-strong-beat-v1",
        }
        return plan

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
                    "visual_summary": self._compact_prompt_text(
                        item.get("visual_summary", ""), 220
                    ),
                    "subject_action": self._compact_prompt_text(
                        item.get("subject_action", ""), 140
                    ),
                    "emotion": self._compact_prompt_text(item.get("emotion", ""), 80),
                    "action_phase": item.get("action_phase", "action"),
                    "shot_scale": item.get("shot_scale", "medium"),
                    "camera_motion": item.get("camera_motion", "static"),
                    "continuity_tags": list(item.get("continuity_tags") or [])[:6],
                    "rhythmic_potential": item.get("rhythmic_potential", 0.5),
                    "reason": self._compact_prompt_text(
                        item.get("reason_for_cut", ""), 180
                    ),
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
        full_review_summaries = [
            {
                "asset_id": str(asset.get("asset_id") or ""),
                "source_order": source_order,
                "file": Path(str(asset.get("source_video") or "")).name,
                "continuous_review_summary": self._compact_prompt_text(
                    self._asset_continuity_summaries.get(
                        str(asset.get("asset_id") or ""), ""
                    ),
                    600,
                ),
            }
            for source_order, asset in enumerate(assets)
        ]
        treatment = treatment or self._active_treatment
        compact_treatment = self._compact_treatment_for_prompt(treatment)
        music_choices = [
            {
                "track_file": Path(str(item.get("file_name") or "")).name,
                "title": self._compact_prompt_text(item.get("title", ""), 120),
                "mood": self._compact_prompt_text(item.get("mood", ""), 120),
                "tags": list(item.get("tags") or [])[:8],
                "tempo_bpm": item.get("tempo_bpm", 0),
                "duration_sec": item.get("duration_sec", 0),
                "key": item.get("key", ""),
                "mode": item.get("mode", ""),
                "integrated_lufs": item.get("integrated_lufs"),
                "dynamic_range_db": item.get("dynamic_range_db"),
                "energy_profile": item.get("energy_profile", {}),
                "strong_beats_sec": self._sample_numeric_landmarks(
                    item.get("strong_beats_sec") or [], 48
                ),
                "downbeats_sec": self._sample_numeric_landmarks(
                    item.get("downbeats_sec") or [], 32
                ),
                "sections": (item.get("sections") or [])[:10],
                "director_match": item.get("director_match", {}),
            }
            for item in self._music_analysis.get("tracks", [])
            if isinstance(item, dict)
        ] or [{"track_file": path.name} for path in self._music_files]
        strong_music_matches = [
            item for item in music_choices
            if bool(item.get("director_match", {}).get("tempo_in_range"))
            and bool(item.get("director_match", {}).get("energy_arc_match", True))
        ]
        if strong_music_matches:
            music_choices = strong_music_matches[:8]
        else:
            music_choices = music_choices[:8]
        score_profiles = [
            {
                "track_file": item.get("track_file", ""),
                "title": item.get("title", ""),
                "mood": item.get("mood", ""),
                "tempo_bpm": item.get("tempo_bpm", 0),
                "duration_sec": item.get("duration_sec", 0),
                "key": item.get("key", ""),
                "mode": item.get("mode", ""),
                "sections": item.get("sections", []),
                "energy_profile": item.get("energy_profile", {}),
                "director_match": item.get("director_match", {}),
            }
            for item in music_choices
        ]
        coverage_prompt = (
            "CONTINUOUS FULL-FOOTAGE SYNTHESIS. The vision pass has inspected every "
            "saved one-fps visual sample in chronological order and carried state across "
            "overlapping transport batches. Synthesize the complete source summaries into "
            "one grounded project memory before selecting shots. Track people, locations, "
            "actions, reactions, cause/effect, recurring motifs, and unresolved intentions. "
            "Do not invent events. You may refine the treatment's central thesis when the "
            "complete evidence supports a stronger interpretation, but respect an explicit "
            "user creative brief. Return JSON only.\n"
            f"USER CREATIVE BRIEF: {self.creative_brief or '(free direction)'}\n"
            f"INITIAL TREATMENT: {json.dumps(compact_treatment, ensure_ascii=False, separators=(',', ':'))}\n"
            f"SOURCE SUMMARIES: {json.dumps(full_review_summaries, ensure_ascii=False, separators=(',', ':'))}"
        )
        self.logger.info(
            "正在汇总连续全片审片记忆 / Synthesizing continuous full-footage memory"
        )
        coverage_synopsis = self._request_json(
            coverage_prompt,
            COVERAGE_SYNOPSIS_SCHEMA,
            model=self.text_model,
        )
        sequence_prompt = (
            "PICTURE ASSEMBLY STEP 1/2. You have already inspected every saved one-fps frame and transcripts "
            "from every source video in continuous order. The FULL COVERAGE SYNOPSIS was synthesized "
            "from every one-fps visual sample in chronological order; use it to understand "
            "the whole action and intention, not only the shortlisted candidates. Build one "
            "coherent documentary edit from "
            "the candidate list below. Select only useful candidate_id values, "
            "never invent or duplicate an id. Follow the treatment. Preserve the "
            "real source_order and in-file timestamp chronology; narrative quality "
            "must come from selection and juxtaposition, not scrambling the shoot. "
            "Establish context, develop the story, "
            "use B-roll to cover or bridge speech, avoid repetitive points, and "
            "finish deliberately. Preserve complete thoughts. Choose restrained "
            "transitions and effects from the schema; default to hard cuts, use "
            "cross dissolves for genuine time/mood changes, and fade_black only "
            "for major chapter endings. Keep the sum of selected clip durations at "
            f"or below {self._active_target_duration_sec * 1.10:.1f} seconds. A shorter "
            "complete film is better than padding to the target: never repeat an action, "
            "idea, lineup, countdown, or setup merely to reach runtime. Select only "
            "the strongest minority of candidates; never include everything. Design "
            "picture rhythm against AVAILABLE SCORE PROFILES now, before selecting: "
            "assign every shot a music_edit_role, preserve natural sound for speech, "
            "use action/reaction and high rhythmic_potential shots for musical builds, "
            "and reserve payoff_hit for the true narrative payoff. Do not pretend a flat "
            "track has a crescendo. "
            "Return project_summary and sequence only; the next constrained call "
            "will score music against this exact edit. Return JSON only.\n"
            f"DIRECTOR TREATMENT:\n{json.dumps(compact_treatment, ensure_ascii=False, separators=(',', ':'))}\n"
            f"AVAILABLE SCORE PROFILES:\n{json.dumps(score_profiles, ensure_ascii=False, separators=(',', ':'))}\n"
            f"ASSETS:\n{json.dumps(asset_names, ensure_ascii=False, separators=(',', ':'))}\n"
            f"FULL COVERAGE SYNOPSIS:\n{json.dumps(coverage_synopsis, ensure_ascii=False, separators=(',', ':'))}\n"
            f"CANDIDATES:\n{json.dumps(compact_candidates, ensure_ascii=False, separators=(',', ':'))}"
        )
        self.logger.info(
            "正在进行跨素材全局编排（%d 个候选，第 1/2 步：镜头）/ "
            "Global story assembly (%d candidates, step 1/2: picture)",
            len(candidates),
            len(candidates),
        )
        sequence_payload = self._request_json(
            sequence_prompt,
            SEQUENCE_SELECTION_SCHEMA,
            model=self.text_model,
        )

        selected_storyboard = self._build_music_storyboard(
            sequence_payload.get("sequence"), candidates
        )
        if music_choices:
            music_prompt = (
                "Design the final documentary music cue sheet for the already selected "
                "picture edit below. Use zero to three cues from AVAILABLE MUSIC and "
                "choose exact track_file values only. A cue may begin inside a track at "
                "a musically useful section and must fit both the track and program. "
                "Use sections, downbeats, and strong beats for openings, transitions, "
                "and a few meaningful climax hits; do not cut every beat. Select a "
                "track section whose measured energy_profile actually follows the "
                "treatment arc; never describe a swell when the analyzed track is flat. "
                "Create 2-6 meaningful sync_points across story transitions and the "
                "payoff when musical landmarks permit, and align a section boundary or "
                "downbeat with the true payoff. Protect "
                "intelligible dialogue with 6-14 dB ducking. Use silence_regions for "
                "emotional breathing room and important speech. Different tracks must "
                "serve distinct story beats, not add random variety. The CPU conformer "
                "will render this cue sheet into one music_bed.wav. Return music_plan "
                "JSON only.\n"
                f"TARGET PROGRAM: {self._active_target_duration_sec:.1f}s\n"
                f"DIRECTOR TREATMENT:\n{json.dumps(compact_treatment, ensure_ascii=False, separators=(',', ':'))}\n"
                f"FULL COVERAGE SYNOPSIS:\n{json.dumps(coverage_synopsis, ensure_ascii=False, separators=(',', ':'))}\n"
                f"SELECTED PICTURE STORYBOARD:\n{json.dumps(selected_storyboard, ensure_ascii=False, separators=(',', ':'))}\n"
                f"AVAILABLE MUSIC:\n{json.dumps(music_choices, ensure_ascii=False, separators=(',', ':'))}"
            )
            self.logger.info(
                "正在设计最终配乐（第 2/2 步：音乐）/ "
                "Designing final music (step 2/2: cue sheet)"
            )
            music_payload = self._request_json(
                music_prompt,
                MUSIC_PLAN_SCHEMA,
                model=self.text_model,
            )
            music_plan = music_payload.get("music_plan")
        else:
            music_plan = {
                "strategy": "No analyzed music candidates were supplied.",
                "silence_regions": [],
                "cues": [],
            }
        return {
            "project_summary": sequence_payload.get("project_summary", ""),
            "coverage_synopsis": coverage_synopsis,
            "sequence": sequence_payload.get("sequence", []),
            "music_plan": music_plan,
        }

    @staticmethod
    def _compact_prompt_text(value: object, limit: int) -> str:
        """Normalize and bound one model-facing string. / 规范并限制模型输入字符串。"""
        text = " ".join(str(value or "").split())
        return text if len(text) <= limit else text[: max(1, limit - 1)].rstrip() + "…"

    @classmethod
    def _compact_treatment_for_prompt(cls, treatment: Dict[str, Any]) -> Dict[str, Any]:
        """Keep treatment semantics while removing prompt-only verbosity. / 保留导演意图并压缩提示。"""
        compact: Dict[str, Any] = {}
        for key, value in treatment.items():
            if key in {"generated_at_utc", "director_model", "story_anchors"}:
                continue
            if isinstance(value, str):
                compact[key] = cls._compact_prompt_text(value, 320)
            elif isinstance(value, list):
                compact[key] = [
                    cls._compact_prompt_text(item, 180) if isinstance(item, str) else item
                    for item in value[:12]
                ]
            else:
                compact[key] = value
        anchors = treatment.get("story_anchors") or []
        compact["story_anchors"] = [
            {
                "asset_id": item.get("asset_id", ""),
                "in": item.get("cut_in_sec", 0),
                "out": item.get("cut_out_sec", 0),
                "beat": item.get("beat", ""),
                "reason": cls._compact_prompt_text(item.get("reason", ""), 160),
            }
            for item in anchors[:16]
            if isinstance(item, dict)
        ]
        return compact

    @staticmethod
    def _sample_numeric_landmarks(values: Sequence[Any], limit: int) -> List[float]:
        """Evenly sample time landmarks across the full track. / 在整首音乐中均匀采样时间标记。"""
        numeric: List[float] = []
        for value in values:
            try:
                number = float(value)
            except (TypeError, ValueError):
                continue
            if math.isfinite(number) and number >= 0:
                numeric.append(round(number, 3))
        if len(numeric) <= limit:
            return numeric
        if limit <= 1:
            return numeric[:1]
        indexes = {
            int(round(index * (len(numeric) - 1) / (limit - 1)))
            for index in range(limit)
        }
        return [numeric[index] for index in sorted(indexes)]

    @classmethod
    def _build_music_storyboard(
        cls,
        sequence: object,
        candidates: Sequence[Dict[str, Any]],
    ) -> List[Dict[str, Any]]:
        """
        Convert the selected source ranges into a compact timeline storyboard.
        将选中的源片段转换成紧凑的时间线故事板。
        """
        by_id = {str(item.get("candidate_id") or ""): item for item in candidates}
        result: List[Dict[str, Any]] = []
        seen = set()
        cursor = 0.0
        for selected in sequence if isinstance(sequence, list) else []:
            if not isinstance(selected, dict):
                continue
            candidate_id = str(selected.get("candidate_id") or "")
            item = by_id.get(candidate_id)
            if item is None or candidate_id in seen:
                continue
            seen.add(candidate_id)
            duration = max(
                0.0,
                float(item.get("cut_out_sec", 0)) - float(item.get("cut_in_sec", 0)),
            )
            result.append(
                {
                    "candidate_id": candidate_id,
                    "timeline_in": round(cursor, 3),
                    "timeline_out": round(cursor + duration, 3),
                    "story_role": item.get("story_role", "context"),
                    "music_edit_role": selected.get("music_edit_role", "natural_sound"),
                    "action_phase": item.get("action_phase", "action"),
                    "emotion": cls._compact_prompt_text(item.get("emotion", ""), 80),
                    "rhythmic_potential": item.get("rhythmic_potential", 0.5),
                    "visual": cls._compact_prompt_text(item.get("visual_summary", ""), 180),
                    "position_reason": cls._compact_prompt_text(
                        selected.get("reason_for_position", ""), 140
                    ),
                }
            )
            cursor += duration
        return result

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
            clip["music_edit_role"] = self._enum_value(
                sequence_item.get("music_edit_role"),
                {
                    "natural_sound", "on_beat", "phrase_start",
                    "build", "payoff_hit", "release",
                },
                "natural_sound" if clip.get("story_role") == "interview" else "on_beat",
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
        # Preserve the director's sequence for explicitly non-linear treatments;
        # otherwise enforce real shooting chronology as promised by the treatment.
        # 非线性方案保留导演排序；严格时间流方案才按拍摄顺序排序。
        active_treatment = treatment or self._active_treatment
        global_look = {
            "clean_neutral": "neutral",
            "cinematic_warm": "warm",
            "cool_steel": "cool",
            "high_contrast": "contrast",
        }.get(str(active_treatment.get("creative_look") or ""), "neutral")
        final = self._order_story_clips(final, active_treatment)
        final = self._remove_semantic_redundancy(final)
        final = self._complete_story_coverage(final, candidates, active_treatment)
        final = self._remove_overlaps(final)
        for clip in final:
            clip["color_look"] = global_look
        final = self._fit_target_duration(final, active_treatment)
        return self._apply_creative_grade_plan(final, active_treatment)

    def _apply_creative_grade_plan(
        self,
        clips: Sequence[Dict[str, Any]],
        treatment: Dict[str, Any],
    ) -> List[Dict[str, Any]]:
        """
        Attach one coherent palette plus subtle story-beat grade modulation.
        为每个镜头挂载统一色彩基线与细微的叙事节拍调色变化。

        Parameters / 参数:
            clips: Validated edit decisions. / 已校验的剪辑决策。
            treatment: Validated director treatment. / 已校验的导演阐述。
        """
        bible = self._validate_color_bible(
            treatment.get("color_bible"),
            str(treatment.get("creative_look") or "clean_neutral"),
        )
        chapters = {
            str(item.get("beat") or "development"): item
            for item in bible.get("chapter_grades", [])
            if isinstance(item, dict)
        }
        result: List[Dict[str, Any]] = []
        for raw_clip in clips:
            clip = dict(raw_clip)
            beat = self._canonical_story_beat(clip)
            chapter = chapters.get(beat, chapters.get("development", {}))
            clip["creative_grade"] = {
                "palette": bible.get("global_palette", "natural"),
                "story_beat": beat,
                "exposure_ev": round(float(chapter.get("exposure_ev", 0)), 3),
                "contrast": round(
                    min(1.35, max(0.8, float(bible.get("contrast", 1)) * float(chapter.get("contrast", 1)))),
                    3,
                ),
                "saturation": round(
                    min(1.35, max(0.65, float(bible.get("saturation", 1)) * float(chapter.get("saturation", 1)))),
                    3,
                ),
                "warmth": round(
                    min(1.0, max(-1.0, float(bible.get("warmth", 0)) + float(chapter.get("warmth", 0)))),
                    3,
                ),
                "highlight_rolloff": round(float(bible.get("highlight_rolloff", 0.45)), 3),
                "reason": str(chapter.get("reason") or treatment.get("color_intent") or ""),
            }
            result.append(clip)
        return result

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
        Fill only missing treatment beats from inspected candidates.
        仅从已审片候选中补齐缺失叙事节拍，不为凑时长填充镜头。
        """
        result = self._remove_semantic_redundancy(selected)
        used = {str(item.get("candidate_id") or "") for item in result}
        # Coverage means a complete story, not one obligatory shot from every
        # source file.  The previous all-source rule was the direct cause of
        # repeated countdowns, setup takes, and edits without a central idea.
        # “覆盖”指覆盖叙事节拍，而不是强制每个源文件都出镜。
        required_beats = ("opening", "development", "payoff", "ending")
        selected_beats = {self._canonical_story_beat(item) for item in result}
        for beat in required_beats:
            if beat in selected_beats:
                continue
            options = [
                item for item in candidates
                if self._canonical_story_beat(item) == beat
                and str(item.get("candidate_id") or "") not in used
                and not self._is_semantically_redundant(item, result)
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
            selected_beats.add(beat)

        return self._order_story_clips(result, treatment)

    @staticmethod
    def _canonical_story_beat(item: Dict[str, Any]) -> str:
        """Map model labels to four film-level beats. / 将模型标签归并为四个成片叙事节拍。"""
        text = " ".join(
            str(item.get(key) or "")
            for key in ("treatment_beat", "story_role")
        ).casefold()
        if any(token in text for token in ("opening", "intro", "setup", "开场", "引入")):
            return "opening"
        if any(token in text for token in ("closing", "ending", "resolution", "结尾", "收束")):
            return "ending"
        if any(token in text for token in ("payoff", "climax", "高潮", "兑现")):
            return "payoff"
        return "development"

    @staticmethod
    def _story_text(item: Dict[str, Any]) -> str:
        """Return normalized semantic text for repetition checks. / 返回用于查重的规范化语义文本。"""
        return " ".join(
            " ".join(str(item.get(key) or "").casefold().split())
            for key in ("visual_summary", "reason_for_cut", "transcript_excerpt")
        ).strip()

    @classmethod
    def _is_semantically_redundant(
        cls, candidate: Dict[str, Any], selected: Sequence[Dict[str, Any]]
    ) -> bool:
        """Reject near-identical visual/story statements. / 拒绝语义近乎相同的重复镜头。"""
        candidate_text = cls._story_text(candidate)
        if len(candidate_text) < 5:
            return False
        candidate_tokens = set(re.findall(r"[a-z0-9]+|[\u4e00-\u9fff]", candidate_text))
        candidate_is_countdown = (
            all(value in candidate_tokens for value in ("1", "2", "3"))
            and any(token in candidate_text for token in ("count", "start", "倒计时", "开始"))
        )
        for existing in selected:
            existing_text = cls._story_text(existing)
            if len(existing_text) < 5:
                continue
            existing_tokens = set(re.findall(r"[a-z0-9]+|[\u4e00-\u9fff]", existing_text))
            existing_is_countdown = (
                all(value in existing_tokens for value in ("1", "2", "3"))
                and any(token in existing_text for token in ("count", "start", "倒计时", "开始"))
            )
            if candidate_is_countdown and existing_is_countdown:
                return True
            union = candidate_tokens | existing_tokens
            jaccard = len(candidate_tokens & existing_tokens) / len(union) if union else 0.0
            similarity = SequenceMatcher(None, candidate_text, existing_text).ratio()
            if jaccard >= 0.72 or similarity >= 0.82:
                return True
        return False

    @classmethod
    def _remove_semantic_redundancy(
        cls, clips: Sequence[Dict[str, Any]]
    ) -> List[Dict[str, Any]]:
        """Keep the first editorially distinct instance of each idea. / 每个叙事信息只保留首个有效镜头。"""
        result: List[Dict[str, Any]] = []
        for item in clips:
            candidate = dict(item)
            if not cls._is_semantically_redundant(candidate, result):
                result.append(candidate)
        return result

    @staticmethod
    def _order_story_clips(
        clips: Sequence[Dict[str, Any]], treatment: Dict[str, Any]
    ) -> List[Dict[str, Any]]:
        """Apply the treatment's chronology promise. / 应用导演阐述中的时间结构承诺。"""
        result = [dict(item) for item in clips]
        if str(treatment.get("chronology_policy") or "") == "strict_chronological":
            result.sort(
                key=lambda item: (
                    int(item.get("source_order", 0)),
                    float(item.get("cut_in_sec", 0)),
                    float(item.get("cut_out_sec", 0)),
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
        selected = self._order_story_clips(selected, treatment)
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
        core_start = float(chunk.get("core_start_sec", chunk_start))
        core_end = float(chunk.get("core_end_sec", chunk_end))
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
            if cut_out <= core_start or cut_in >= core_end:
                # Overlap images reconnect motion only; candidates wholly outside
                # the new core belong to the neighboring transport batch.
                continue
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
                    "subject_action": " ".join(
                        str(decision.get("subject_action") or reason).split()
                    ),
                    "emotion": " ".join(
                        str(decision.get("emotion") or "observational").split()
                    ),
                    "action_phase": self._enum_value(
                        decision.get("action_phase"),
                        {"setup", "build", "action", "reaction", "payoff", "aftermath"},
                        "action",
                    ),
                    "shot_scale": self._enum_value(
                        decision.get("shot_scale"),
                        {"extreme_wide", "wide", "medium", "closeup", "detail"},
                        "medium",
                    ),
                    "camera_motion": self._enum_value(
                        decision.get("camera_motion"),
                        {"static", "pan", "tilt", "handheld", "tracking"},
                        "static",
                    ),
                    "continuity_tags": [
                        " ".join(str(value).split())
                        for value in (
                            decision.get("continuity_tags")
                            if isinstance(decision.get("continuity_tags"), list) else []
                        )[:6]
                        if str(value).strip()
                    ],
                    "rhythmic_potential": round(
                        min(
                            1.0,
                            max(
                                0.0,
                                self._finite_float(
                                    decision.get("rhythmic_potential", 0.5),
                                    f"decisions[{index}].rhythmic_potential",
                                ),
                            ),
                        ),
                        3,
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
            "treatment_file": str(self.treatment_path or ""),
        }
        digest.update(
            json.dumps(settings, sort_keys=True, ensure_ascii=False).encode("utf-8")
        )
        if self.treatment_path is not None:
            try:
                digest.update(self.treatment_path.read_bytes())
            except OSError as exc:
                raise DirectorError(
                    f"无法读取导演初审以创建检查点 / Cannot fingerprint treatment: {exc}"
                ) from exc
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
    parser.add_argument("--output", default="")
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
        "--treatment-file",
        help="第一次导演初审 JSON；提供后最终导演不会重复初审 / first-pass treatment JSON",
    )
    parser.add_argument(
        "--treatment-only",
        action="store_true",
        help="仅执行第一次多模态导演初审 / run only the first multimodal director pass",
    )
    parser.add_argument("--treatment-output")
    parser.add_argument("--music-brief-output")
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
            treatment_file=args.treatment_file,
            logger=logger,
        )
        if args.treatment_only:
            if not args.treatment_output or not args.music_brief_output:
                raise DirectorError(
                    "--treatment-only 需要 --treatment-output 和 --music-brief-output"
                    " / treatment-only requires both output paths."
                )
            director.run_treatment_only(
                args.raw_data, args.treatment_output, args.music_brief_output
            )
        elif not args.output:
            raise DirectorError("缺少 --output / --output is required for the final director pass.")
        elif args.revalidate_existing:
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
