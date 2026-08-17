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
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from typing import Any, Dict, List, Optional, Sequence


LOGGER_NAME = "cybereditor.director"
DIRECTOR_CHECKPOINT_VERSION = 1
DIRECTOR_PROMPT_VERSION = "2026-08-15.1-neutral-ledger-concept-tournament"
VISUAL_REVIEW_VERSION = 3
VISUAL_EVIDENCE_PROMPT_VERSION = "2026-08-15.1-neutral-action-atoms"
TEMPORAL_REFINEMENT_VERSION = "2026-08-15.1-dense-4fps"
# A 1280x720 Qwen-style visual sample is roughly 1.2K merged visual tokens.
# Keep a safety margin for model/version differences and for images whose
# encoded dimensions are not present in legacy extractor JSON.
# 1280x720 的 Qwen 类视觉样本通常约占 1.2K 合并视觉 token；这里额外保留
# 模型版本与旧版提取 JSON 缺少尺寸信息的安全余量。
VISION_IMAGE_TOKEN_ESTIMATE = 1536


def build_evidence_fingerprint(
    raw_data_path: os.PathLike,
    vision_model: str,
) -> str:
    """
    Fingerprint the complete visual evidence contract for safe cache reuse.
    为完整视觉证据契约生成指纹，确保缓存只在完全匹配时复用。

    Parameters / 参数:
        raw_data_path: Combined extractor JSON, including sampling metadata.
            合并后的提取 JSON，其中已包含视觉采样元数据。
        vision_model: Exact local vision-model tag. / 本地视觉模型完整标签。

    The raw JSON bytes deliberately participate in the digest. Every referenced
    source, proxy, and JPEG also contributes its resolved path, byte size, and
    nanosecond mtime. Replacing media in place therefore invalidates a cached
    ledger, while a missing evidence file fails closed instead of being silently
    treated as the footage previously reviewed.

    原始 JSON 字节直接参与哈希。每个源素材、代理和 JPEG 的解析路径、
    字节大小与纳秒 mtime 也会纳入指纹；同路径替换文件会使缓存失效，
    缺失证据文件则安全失败，绝不冒充成曾经完整审看的素材。
    """
    path = Path(raw_data_path).expanduser().resolve()
    try:
        raw_bytes = path.read_bytes()
    except OSError as exc:
        raise DirectorError(
            f"无法读取视觉证据源 / Cannot read visual evidence source: {exc}"
        ) from exc
    try:
        raw_payload = json.loads(raw_bytes.decode("utf-8-sig"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise DirectorError(
            f"无法解析视觉证据源 / Cannot parse visual evidence source: {exc}"
        ) from exc

    manifest: List[Dict[str, Any]] = []

    def append_file(role: str, raw_value: object) -> None:
        """Append one fail-closed file identity record. / 追加一条安全失败的文件身份记录。"""
        value = str(raw_value or "").strip()
        if not value:
            raise DirectorError(
                f"视觉证据清单缺少 {role} 路径 / Evidence manifest lacks {role}."
            )
        candidate = Path(value).expanduser()
        if not candidate.is_absolute():
            candidate = path.parent / candidate
        resolved = candidate.resolve()
        try:
            stat = resolved.stat()
        except OSError as exc:
            raise DirectorError(
                f"视觉证据文件缺失或不可读 / Missing or unreadable evidence file "
                f"({role}): {resolved}: {exc}"
            ) from exc
        if not resolved.is_file():
            raise DirectorError(
                f"视觉证据路径不是文件 / Evidence path is not a file "
                f"({role}): {resolved}"
            )
        manifest.append(
            {
                "role": role,
                "path": os.path.normcase(str(resolved)),
                "size": int(stat.st_size),
                "mtime_ns": int(stat.st_mtime_ns),
            }
        )

    assets = raw_payload.get("assets") if isinstance(raw_payload, dict) else None
    if isinstance(assets, list):
        if not assets:
            raise DirectorError(
                "视觉证据清单不能是空 assets / Evidence manifest has no assets."
            )
        for asset_index, asset in enumerate(assets):
            if not isinstance(asset, dict):
                raise DirectorError(
                    f"assets[{asset_index}] 不是对象 / is not an object."
                )
            append_file(
                f"asset[{asset_index}].source",
                asset.get("source_video"),
            )
            proxy_value = str(asset.get("proxy_file_name") or "").strip()
            if proxy_value:
                append_file(f"asset[{asset_index}].proxy", proxy_value)
            raw_keyframes = asset.get("keyframes")
            if not isinstance(raw_keyframes, list):
                raise DirectorError(
                    f"assets[{asset_index}].keyframes 必须是数组 / must be an array."
                )
            for frame_index, frame in enumerate(raw_keyframes):
                if not isinstance(frame, dict):
                    raise DirectorError(
                        f"assets[{asset_index}].keyframes[{frame_index}] 不是对象 / "
                        "is not an object."
                    )
                image_path = str(frame.get("image_path") or "").strip()
                if not image_path:
                    raw_asset_path = str(asset.get("raw_data_path") or "").strip()
                    frame_name = str(frame.get("file_name") or "").strip()
                    if raw_asset_path and frame_name:
                        raw_asset_candidate = Path(raw_asset_path).expanduser()
                        if not raw_asset_candidate.is_absolute():
                            raw_asset_candidate = path.parent / raw_asset_candidate
                        image_path = str(
                            raw_asset_candidate.resolve().parent
                            / "keyframes"
                            / frame_name
                        )
                append_file(
                    f"asset[{asset_index}].keyframe[{frame_index}]",
                    image_path,
                )

    digest = hashlib.sha256()
    digest.update(raw_bytes)
    digest.update(b"\0")
    digest.update(
        json.dumps(
            manifest,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    )
    digest.update(b"\0")
    digest.update(str(vision_model or "").strip().casefold().encode("utf-8"))
    digest.update(b"\0")
    digest.update(str(VISUAL_REVIEW_VERSION).encode("ascii"))
    digest.update(b"\0")
    digest.update(VISUAL_EVIDENCE_PROMPT_VERSION.encode("utf-8"))
    digest.update(b"\0")
    digest.update(TEMPORAL_REFINEMENT_VERSION.encode("utf-8"))
    return digest.hexdigest()

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
        "viewer_takeaway": {"type": "string", "minLength": 1},
        "edit_style": {
            "type": "string",
            "enum": [
                "narrative_documentary", "kinetic_montage", "atmospheric_poem",
                "dialogue_led", "hybrid_cinematic",
            ],
        },
        "typography_intent": {"type": "string", "minLength": 1},
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
        "title", "logline", "central_theme", "viewer_takeaway", "edit_style",
        "typography_intent", "chronology_policy",
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

STORY_CONCEPTS_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "properties": {
        "concepts": {
            "type": "array",
            "minItems": 3,
            "maxItems": 3,
            "items": {
                "type": "object",
                "properties": {
                    "concept_id": {"type": "string", "minLength": 1},
                    "title": {"type": "string", "minLength": 1, "maxLength": 80},
                    "form": {
                        "type": "string",
                        "enum": [
                            "causal_story", "character_vignette", "kinetic_style_film",
                            "atmospheric_poem", "dialogue_scene", "bts_process",
                        ],
                    },
                    "premise": {"type": "string", "minLength": 1, "maxLength": 600},
                    "viewer_takeaway": {"type": "string", "minLength": 1, "maxLength": 400},
                    "opening": {"type": "string", "minLength": 1, "maxLength": 400},
                    "development": {"type": "string", "minLength": 1, "maxLength": 600},
                    "payoff": {"type": "string", "minLength": 1, "maxLength": 400},
                    "ending": {"type": "string", "minLength": 1, "maxLength": 400},
                    "proof_candidate_ids": {
                        "type": "array",
                        "items": {"type": "string", "minLength": 1},
                        "minItems": 1,
                        "maxItems": 16,
                    },
                    "ending_candidate_id": {"type": "string", "minLength": 1},
                    "music_direction": {"type": "string", "minLength": 1, "maxLength": 400},
                    "color_direction": {"type": "string", "minLength": 1, "maxLength": 400},
                    "feasibility_score": {"type": "integer", "minimum": 1, "maximum": 10},
                    "biggest_risk": {"type": "string", "minLength": 1, "maxLength": 400},
                    "recommended_duration_sec": {"type": "number", "minimum": 10, "maximum": 600},
                },
                "required": [
                    "concept_id", "title", "form", "premise", "viewer_takeaway",
                    "opening", "development", "payoff", "ending",
                    "proof_candidate_ids", "ending_candidate_id", "music_direction",
                    "color_direction", "feasibility_score", "biggest_risk",
                    "recommended_duration_sec",
                ],
                "additionalProperties": False,
            },
        },
        "selected_concept_id": {"type": "string", "minLength": 1},
        "selection_reason": {"type": "string", "minLength": 1, "maxLength": 800},
    },
    "required": ["concepts", "selected_concept_id", "selection_reason"],
    "additionalProperties": False,
}

STORY_SEED_PAGE_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "properties": {
        "page_summary": {"type": "string", "minLength": 1, "maxLength": 1200},
        "story_seeds": {
            "type": "array",
            "minItems": 1,
            "maxItems": 3,
            "items": {
                "type": "object",
                "properties": {
                    "form": {
                        "type": "string",
                        "enum": [
                            "causal_story", "character_vignette", "kinetic_style_film",
                            "atmospheric_poem", "dialogue_scene", "bts_process",
                        ],
                    },
                    "premise": {"type": "string", "minLength": 1, "maxLength": 400},
                    "proof_candidate_ids": {
                        "type": "array",
                        "minItems": 1,
                        "maxItems": 10,
                        "items": {"type": "string", "minLength": 1},
                    },
                    "possible_ending_candidate_id": {
                        "type": "string",
                        "minLength": 1,
                    },
                    "observable_progression": {
                        "type": "string",
                        "minLength": 1,
                        "maxLength": 500,
                    },
                    "limitations": {"type": "string", "minLength": 1, "maxLength": 400},
                },
                "required": [
                    "form", "premise", "proof_candidate_ids",
                    "possible_ending_candidate_id", "observable_progression", "limitations",
                ],
                "additionalProperties": False,
            },
        },
    },
    "required": ["page_summary", "story_seeds"],
    "additionalProperties": False,
}

GRAPHICS_PLAN_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "properties": {
        "strategy": {"type": "string", "minLength": 1},
        "items": {
            "type": "array",
            "maxItems": 6,
            "items": {
                "type": "object",
                "properties": {
                    "graphic_id": {"type": "string", "minLength": 1},
                    "kind": {
                        "type": "string",
                        "enum": ["title_card", "chapter", "lower_third", "end_card"],
                    },
                    "anchor_candidate_id": {"type": "string", "minLength": 1},
                    "placement": {
                        "type": "string",
                        "enum": ["clip_start", "clip_middle", "clip_end"],
                    },
                    "duration_sec": {"type": "number", "minimum": 0.8, "maximum": 6},
                    "text": {"type": "string", "minLength": 1, "maxLength": 80},
                    "subtitle": {"type": "string", "maxLength": 140},
                    "style": {
                        "type": "string",
                        "enum": ["minimal", "bold_cinematic", "kinetic", "editorial"],
                    },
                    "purpose": {"type": "string", "minLength": 1, "maxLength": 240},
                },
                "required": [
                    "graphic_id", "kind", "anchor_candidate_id", "placement",
                    "duration_sec", "text", "subtitle", "style", "purpose",
                ],
                "additionalProperties": False,
            },
        },
    },
    "required": ["strategy", "items"],
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
                    "volume_db": {"type": "number", "minimum": -60, "maximum": 12},
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
                    "entry_state": {"type": "string", "minLength": 1},
                    "action_apex": {"type": "string", "minLength": 1},
                    "exit_state": {"type": "string", "minLength": 1},
                    "screen_direction": {
                        "type": "string",
                        "enum": ["left", "right", "toward_camera", "away_from_camera", "mixed", "none"],
                    },
                    "identity_tags": {
                        "type": "array",
                        "items": {"type": "string", "minLength": 1},
                        "maxItems": 8,
                    },
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
                    "volume_db": {"type": "number", "minimum": -60, "maximum": 12},
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

# The visual pass is an evidence collector, not a miniature editor.  Keeping a
# separate schema is intentional: requiring story roles, effects, transitions,
# or a ``reason_for_cut`` before the treatment existed caused the model to throw
# away ordinary setup/reaction evidence and then confirm its own premature idea.
# 视觉阶段只负责取证，不是缩小版导演。独立 Schema 可避免在阐述生成前就强迫模型
# 决定叙事角色、特效、转场或“为何保留”，从结构上消除先入为主。
EVIDENCE_ATOM_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "properties": {
        "continuity_summary": {
            "type": "string", "minLength": 1, "maxLength": 1600,
        },
        "decisions": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "cut_in_sec": {"type": "number", "minimum": 0},
                    "cut_out_sec": {"type": "number", "minimum": 0},
                    "visual_summary": {"type": "string", "minLength": 1},
                    "subject_action": {"type": "string", "minLength": 1},
                    "observable_emotion": {"type": "string", "minLength": 1},
                    "entry_state": {"type": "string", "minLength": 1},
                    "action_apex": {"type": "string", "minLength": 1},
                    "exit_state": {"type": "string", "minLength": 1},
                    "screen_direction": {
                        "type": "string",
                        "enum": [
                            "left", "right", "toward_camera",
                            "away_from_camera", "mixed", "none",
                        ],
                    },
                    "identity_tags": {
                        "type": "array",
                        "items": {"type": "string", "minLength": 1},
                        "maxItems": 8,
                    },
                    "temporal_phase": {
                        "type": "string",
                        "enum": [
                            "state", "onset", "development", "apex",
                            "reaction", "aftermath",
                        ],
                    },
                    "shot_scale": {
                        "type": "string",
                        "enum": [
                            "extreme_wide", "wide", "medium", "closeup", "detail",
                        ],
                    },
                    "camera_motion": {
                        "type": "string",
                        "enum": ["static", "pan", "tilt", "handheld", "tracking"],
                    },
                    "continuity_tags": {
                        "type": "array",
                        "items": {"type": "string", "minLength": 1},
                        "maxItems": 8,
                    },
                    "technical_readability": {
                        "type": "string",
                        "enum": ["clear", "limited", "unreadable"],
                    },
                    "confidence": {"type": "number", "minimum": 0, "maximum": 1},
                },
                "required": [
                    "cut_in_sec", "cut_out_sec", "visual_summary",
                    "subject_action", "observable_emotion", "entry_state",
                    "action_apex", "exit_state", "screen_direction",
                    "identity_tags", "temporal_phase", "shot_scale",
                    "camera_motion", "continuity_tags",
                    "technical_readability", "confidence",
                ],
                "additionalProperties": False,
            },
        },
    },
    "required": ["continuity_summary", "decisions"],
    "additionalProperties": False,
}


ATOM_REFINEMENT_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "properties": {
        "keep": {"type": "boolean"},
        "trim_in_sec": {"type": "number", "minimum": 0},
        "action_apex_sec": {"type": "number", "minimum": 0},
        "trim_out_sec": {"type": "number", "minimum": 0},
        "entry_state": {"type": "string", "minLength": 1, "maxLength": 300},
        "action_apex": {"type": "string", "minLength": 1, "maxLength": 300},
        "exit_state": {"type": "string", "minLength": 1, "maxLength": 300},
        "screen_direction": {
            "type": "string",
            "enum": ["left", "right", "toward_camera", "away_from_camera", "mixed", "none"],
        },
        "continuity_risk": {"type": "string", "maxLength": 300},
        "decision_reason": {"type": "string", "minLength": 1, "maxLength": 400},
    },
    "required": [
        "keep", "trim_in_sec", "action_apex_sec", "trim_out_sec",
        "entry_state", "action_apex", "exit_state", "screen_direction",
        "continuity_risk", "decision_reason",
    ],
    "additionalProperties": False,
}

SEQUENCE_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "properties": {
        "project_summary": {"type": "string"},
        "viewer_takeaway": {"type": "string", "minLength": 1},
        "editorial_style": {
            "type": "string",
            "enum": [
                "narrative_documentary", "kinetic_montage", "atmospheric_poem",
                "dialogue_led", "hybrid_cinematic",
            ],
        },
        "graphics_plan": GRAPHICS_PLAN_SCHEMA,
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
                    "trim_in_sec": {"type": "number", "minimum": 0},
                    "trim_out_sec": {"type": "number", "minimum": 0},
                    "narrative_function": {
                        "type": "string",
                        "enum": [
                            "hook", "context", "escalation", "contrast",
                            "payoff", "closure",
                        ],
                    },
                    "viewer_information": {"type": "string", "minLength": 1},
                    "reason_for_position": {"type": "string", "minLength": 1},
                    "evidence_claim": {"type": "string", "minLength": 1},
                    "connection_to_previous": {"type": "string", "minLength": 1},
                    "audio_intent": {
                        "type": "string",
                        "enum": [
                            "preserve_dialogue", "natural_texture",
                            "mute_for_music", "mix_with_music",
                        ],
                    },
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
                    "volume_db": {"type": "number", "minimum": -60, "maximum": 12},
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
                    "candidate_id", "trim_in_sec", "trim_out_sec",
                    "narrative_function", "viewer_information",
                    "reason_for_position", "evidence_claim",
                    "connection_to_previous", "audio_intent", "music_edit_role",
                ],
                "additionalProperties": False,
            },
        },
    },
    "required": [
        "project_summary", "viewer_takeaway", "editorial_style",
        "graphics_plan", "music_plan", "sequence",
    ],
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
        "viewer_takeaway": SEQUENCE_SCHEMA["properties"]["viewer_takeaway"],
        "editorial_style": SEQUENCE_SCHEMA["properties"]["editorial_style"],
        "graphics_plan": SEQUENCE_SCHEMA["properties"]["graphics_plan"],
        "sequence": SEQUENCE_SCHEMA["properties"]["sequence"],
    },
    "required": [
        "project_summary", "viewer_takeaway", "editorial_style",
        "graphics_plan", "sequence",
    ],
    "additionalProperties": False,
}

SUPERVISING_EDITOR_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "properties": {
        **SEQUENCE_SELECTION_SCHEMA["properties"],
        "review": {
            "type": "object",
            "properties": {
                "clarity_score": {"type": "integer", "minimum": 1, "maximum": 10},
                "pacing_score": {"type": "integer", "minimum": 1, "maximum": 10},
                "visual_storytelling_score": {"type": "integer", "minimum": 1, "maximum": 10},
                "rhythm_score": {"type": "integer", "minimum": 1, "maximum": 10},
                "problems_found": {
                    "type": "array",
                    "items": {"type": "string", "minLength": 1, "maxLength": 500},
                    "minItems": 1,
                    "maxItems": 10,
                },
                "changes_made": {
                    "type": "array",
                    "items": {"type": "string", "minLength": 1, "maxLength": 500},
                    "minItems": 1,
                    "maxItems": 10,
                },
                "dialogue_strategy": {"type": "string", "minLength": 1, "maxLength": 800},
                "rhythm_strategy": {"type": "string", "minLength": 1, "maxLength": 800},
            },
            "required": [
                "clarity_score", "pacing_score", "visual_storytelling_score",
                "rhythm_score", "problems_found", "changes_made",
                "dialogue_strategy", "rhythm_strategy",
            ],
            "additionalProperties": False,
        },
    },
    "required": [
        *SEQUENCE_SELECTION_SCHEMA["required"],
        "review",
    ],
    "additionalProperties": False,
}

# Long-form picture locks cannot safely return dozens of verbose shot objects
# in one Ollama generation. The staged protocol first selects a compact global
# order, then authors core cuts and annotations in bounded pages.
# 长片锁画不能让 Ollama 一次生成数十个富文本镜头对象；分阶段协议先返回紧凑
# 全局顺序，再分页生成核心剪点与详细标注。
PICTURE_MAX_SHOTS = 96
PICTURE_SKELETON_PAGE_SIZE = 12
PICTURE_ENRICHMENT_PAGE_SIZE = 6

PICTURE_ORDER_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "properties": {
        "project_summary": {"type": "string", "minLength": 1, "maxLength": 800},
        "viewer_takeaway": {"type": "string", "minLength": 1, "maxLength": 500},
        "editorial_style": SEQUENCE_SCHEMA["properties"]["editorial_style"],
        "ordered_candidate_ids": {
            "type": "array",
            "minItems": 1,
            "maxItems": PICTURE_MAX_SHOTS,
            "items": {"type": "string", "minLength": 1, "maxLength": 96},
        },
    },
    "required": [
        "project_summary", "viewer_takeaway", "editorial_style",
        "ordered_candidate_ids",
    ],
    "additionalProperties": False,
}

PICTURE_ORDER_REVIEW_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "properties": {
        **PICTURE_ORDER_SCHEMA["properties"],
        "review": SUPERVISING_EDITOR_SCHEMA["properties"]["review"],
    },
    "required": [*PICTURE_ORDER_SCHEMA["required"], "review"],
    "additionalProperties": False,
}

PICTURE_SKELETON_PAGE_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "properties": {
        "page_index": {"type": "integer", "minimum": 1},
        "shots": {
            "type": "array",
            "minItems": 1,
            "maxItems": PICTURE_SKELETON_PAGE_SIZE,
            "items": {
                "type": "object",
                "properties": {
                    "candidate_id": {"type": "string", "minLength": 1, "maxLength": 96},
                    "trim_in_sec": {"type": "number", "minimum": 0},
                    "trim_out_sec": {"type": "number", "minimum": 0},
                    "narrative_function": SEQUENCE_SCHEMA["properties"]["sequence"]["items"]["properties"]["narrative_function"],
                    "audio_intent": SEQUENCE_SCHEMA["properties"]["sequence"]["items"]["properties"]["audio_intent"],
                    "music_edit_role": SEQUENCE_SCHEMA["properties"]["sequence"]["items"]["properties"]["music_edit_role"],
                },
                "required": [
                    "candidate_id", "trim_in_sec", "trim_out_sec",
                    "narrative_function", "audio_intent", "music_edit_role",
                ],
                "additionalProperties": False,
            },
        },
    },
    "required": ["page_index", "shots"],
    "additionalProperties": False,
}

PICTURE_ENRICHMENT_PAGE_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "properties": {
        "page_index": {"type": "integer", "minimum": 1},
        "shots": {
            "type": "array",
            "minItems": 1,
            "maxItems": PICTURE_ENRICHMENT_PAGE_SIZE,
            "items": {
                "type": "object",
                "properties": {
                    "candidate_id": {"type": "string", "minLength": 1, "maxLength": 96},
                    "viewer_information": {"type": "string", "minLength": 1, "maxLength": 160},
                    "reason_for_position": {"type": "string", "minLength": 1, "maxLength": 180},
                    "evidence_claim": {"type": "string", "minLength": 1, "maxLength": 160},
                    "connection_to_previous": {"type": "string", "minLength": 1, "maxLength": 180},
                    "transition_to_next": SEQUENCE_SCHEMA["properties"]["sequence"]["items"]["properties"]["transition_to_next"],
                    "transition_duration_sec": SEQUENCE_SCHEMA["properties"]["sequence"]["items"]["properties"]["transition_duration_sec"],
                    "audio_cleanup": SEQUENCE_SCHEMA["properties"]["sequence"]["items"]["properties"]["audio_cleanup"],
                    "color_look": SEQUENCE_SCHEMA["properties"]["sequence"]["items"]["properties"]["color_look"],
                    "motion": SEQUENCE_SCHEMA["properties"]["sequence"]["items"]["properties"]["motion"],
                    "volume_db": SEQUENCE_SCHEMA["properties"]["sequence"]["items"]["properties"]["volume_db"],
                    "drx_preset": SEQUENCE_SCHEMA["properties"]["sequence"]["items"]["properties"]["drx_preset"],
                    "stabilization": SEQUENCE_SCHEMA["properties"]["sequence"]["items"]["properties"]["stabilization"],
                    "tracking": SEQUENCE_SCHEMA["properties"]["sequence"]["items"]["properties"]["tracking"],
                    "smart_reframe": SEQUENCE_SCHEMA["properties"]["sequence"]["items"]["properties"]["smart_reframe"],
                },
                "required": [
                    "candidate_id", "viewer_information", "reason_for_position",
                    "evidence_claim", "connection_to_previous",
                    "transition_to_next", "transition_duration_sec",
                    "audio_cleanup", "color_look", "motion", "volume_db",
                    "drx_preset", "stabilization", "tracking", "smart_reframe",
                ],
                "additionalProperties": False,
            },
        },
    },
    "required": ["page_index", "shots"],
    "additionalProperties": False,
}

PICTURE_GRAPHICS_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "properties": {"graphics_plan": GRAPHICS_PLAN_SCHEMA},
    "required": ["graphics_plan"],
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
            "minItems": 1,
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
            "minItems": 1,
            "maxItems": 10,
        },
        "continuity_risks": {
            "type": "array",
            "items": {"type": "string", "minLength": 1, "maxLength": 300},
            "maxItems": 10,
        },
        "observed_ending": {"type": "string", "minLength": 1, "maxLength": 600},
        "absent_or_unproven_events": {
            "type": "array",
            "items": {"type": "string", "minLength": 1, "maxLength": 300},
            "maxItems": 12,
        },
        "honest_adaptation": {"type": "string", "minLength": 1, "maxLength": 800},
    },
    "required": [
        "whole_footage_summary", "discovered_central_theme", "character_threads",
        "event_timeline", "visual_motifs", "continuity_risks", "observed_ending",
        "absent_or_unproven_events", "honest_adaptation",
    ],
    "additionalProperties": False,
}

NARRATIVE_CONTRACT_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "properties": {
        "narrative_mode": {
            "type": "string",
            "enum": [
                "causal_story", "dialogue_scene", "character_vignette",
                "mood_montage", "bts_process",
            ],
        },
        "premise": {"type": "string", "minLength": 1, "maxLength": 500},
        "subject": {"type": "string", "minLength": 1, "maxLength": 300},
        "observed_goal": {"type": "string", "minLength": 1, "maxLength": 500},
        "has_causal_arc": {"type": "boolean"},
        "causal_chain": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "candidate_id": {"type": "string", "minLength": 1},
                    "observed_fact": {"type": "string", "minLength": 1, "maxLength": 500},
                    "state_before": {"type": "string", "minLength": 1, "maxLength": 300},
                    "state_after": {"type": "string", "minLength": 1, "maxLength": 300},
                    "story_consequence": {"type": "string", "minLength": 1, "maxLength": 400},
                    "evidence_type": {
                        "type": "string",
                        "enum": ["visual", "audible", "both"],
                    },
                },
                "required": [
                    "candidate_id", "observed_fact", "state_before", "state_after",
                    "story_consequence", "evidence_type",
                ],
                "additionalProperties": False,
            },
            "minItems": 1,
            "maxItems": 8,
        },
        "final_observed_state": {"type": "string", "minLength": 1, "maxLength": 500},
        "unsupported_promises": {
            "type": "array",
            "items": {"type": "string", "minLength": 1, "maxLength": 300},
            "maxItems": 12,
        },
        "dialogue_policy": {
            "type": "string",
            "enum": [
                "story_dialogue_only", "sparse_character_lines",
                "natural_texture_only", "mute_production_chatter", "dialogue_led",
            ],
        },
        "success_criteria": {
            "type": "array",
            "items": {"type": "string", "minLength": 1, "maxLength": 300},
            "minItems": 3,
            "maxItems": 6,
        },
        "recommended_duration_sec": {"type": "number", "minimum": 10, "maximum": 600},
    },
    "required": [
        "narrative_mode", "premise", "subject", "observed_goal", "has_causal_arc",
        "causal_chain", "final_observed_state", "unsupported_promises",
        "dialogue_policy", "success_criteria", "recommended_duration_sec",
    ],
    "additionalProperties": False,
}

BLIND_VIEWER_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "properties": {
        "literal_synopsis": {"type": "string", "minLength": 1, "maxLength": 900},
        "subject": {"type": "string", "minLength": 1, "maxLength": 250},
        "apparent_goal": {"type": "string", "minLength": 1, "maxLength": 350},
        "progression": {
            "type": "array",
            "items": {"type": "string", "minLength": 1, "maxLength": 350},
            "minItems": 1,
            "maxItems": 8,
        },
        "ending": {"type": "string", "minLength": 1, "maxLength": 350},
        "takeaway_guess": {"type": "string", "minLength": 1, "maxLength": 500},
        "coherence_score": {"type": "integer", "minimum": 1, "maximum": 10},
        "causal_clarity_score": {"type": "integer", "minimum": 1, "maximum": 10},
        "visual_payoff_score": {"type": "integer", "minimum": 1, "maximum": 10},
        "confusing_transitions": {
            "type": "array",
            "items": {"type": "string", "minLength": 1, "maxLength": 350},
            "maxItems": 8,
        },
        "unsupported_or_unresolved_points": {
            "type": "array",
            "items": {"type": "string", "minLength": 1, "maxLength": 350},
            "maxItems": 8,
        },
        "passes": {"type": "boolean"},
        "reason": {"type": "string", "minLength": 1, "maxLength": 700},
    },
    "required": [
        "literal_synopsis", "subject", "apparent_goal", "progression", "ending",
        "takeaway_guess", "coherence_score", "causal_clarity_score",
        "visual_payoff_score", "confusing_transitions",
        "unsupported_or_unresolved_points", "passes", "reason",
    ],
    "additionalProperties": False,
}


class DirectorError(RuntimeError):
    """Expected AI director failure. / 可预期的 AI 导演错误。"""


class EditorialQualityError(DirectorError):
    """Raised when every bounded recut still fails the editorial gate. / 重剪仍未过质量门。"""

    def __init__(
        self,
        violations: Sequence[str],
        metrics: Optional[Dict[str, Any]] = None,
        blind_review: Optional[Dict[str, Any]] = None,
    ) -> None:
        """Preserve machine-readable failure evidence for concept fallback. / 保存可供换构想使用的失败证据。"""
        self.violations = [str(value) for value in violations]
        self.metrics = dict(metrics or {})
        self.blind_review = dict(blind_review or {})
        super().__init__(
            "导演的三轮重剪仍未达到可理解的成片标准，拒绝输出已知不合格版本："
            + "；".join(self.violations)
            + " / Bounded recuts still failed the editorial gate; refusing to render a known-bad cut: "
            + "; ".join(self.violations)
        )


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
        timeout_sec: int = 7200,
        merge_gap_sec: float = 0.4,
        creative_brief: str = "",
        target_duration_sec: float = 0.0,
        camera_profile: str = "sony_pp8_slog3_sgamut3cine",
        music_folder: Optional[os.PathLike] = None,
        music_analysis: Optional[os.PathLike] = None,
        treatment_file: Optional[os.PathLike] = None,
        rough_cut_feedback: Optional[os.PathLike] = None,
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
        self.rough_cut_feedback_path = (
            Path(rough_cut_feedback).expanduser().resolve()
            if rough_cut_feedback else None
        )
        if (
            self.rough_cut_feedback_path is not None
            and not self.rough_cut_feedback_path.is_file()
        ):
            raise DirectorError(
                "粗剪盲审文件不存在 / Rough-cut review not found: "
                f"{self.rough_cut_feedback_path}"
            )
        self._rough_cut_feedback: Dict[str, Any] = {}
        if self.rough_cut_feedback_path is not None:
            try:
                feedback = json.loads(
                    self.rough_cut_feedback_path.read_text(encoding="utf-8-sig")
                )
            except (OSError, json.JSONDecodeError) as exc:
                raise DirectorError(
                    f"无法读取粗剪盲审 / Cannot read rough-cut review: {exc}"
                ) from exc
            if not isinstance(feedback, dict):
                raise DirectorError(
                    "粗剪盲审根节点必须是对象 / Rough-cut review must be an object."
                )
            self._rough_cut_feedback = feedback
        self._music_analysis: Dict[str, Any] = {}
        self._active_treatment: Dict[str, Any] = {}
        self._active_target_duration_sec = 0.0
        self._music_files: List[Path] = []
        self._asset_continuity_summaries: Dict[str, str] = {}
        self._active_evidence_candidates: List[Dict[str, Any]] = []
        self._active_concept_tournament: Dict[str, Any] = {}
        self.logger = logger or logging.getLogger(LOGGER_NAME)
        self._session = session
        effective_text_ctx = self._effective_num_ctx(self.text_model)
        if effective_text_ctx < self.num_ctx:
            self.logger.warning(
                "导演模型 Context：界面配置=%d，70B/72B 混合内存安全有效值=%d；"
                "提高界面 Context 不会越过此硬上限，长片将使用分页输出 / "
                "Director context: configured=%d, effective mixed-memory 70B/72B cap=%d; "
                "raising the UI value does not bypass this hard cap, so long-form output is paged",
                self.num_ctx,
                effective_text_ctx,
                self.num_ctx,
                effective_text_ctx,
            )

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
        treatment_destination = Path(treatment_output_path).expanduser().resolve()
        ledger_path = self._footage_ledger_path(treatment_destination)
        try:
            ledger = self._review_footage_neutrally(
                assets, raw_path, ledger_path
            )
            candidates = [
                dict(item) for item in ledger.get("candidate_audit", [])
                if isinstance(item, dict)
            ]
            self._active_evidence_candidates = candidates
            if self.text_model.casefold() != self.model.casefold():
                self.logger.info(
                    "中立视觉审片完成，卸载 %s 后加载文字导演 %s / "
                    "Neutral review complete; switching vision to text director",
                    self.model, self.text_model,
                )
                self.unload_model(self.model)
                self.check_ollama(model=self.text_model)
            else:
                self.check_ollama(model=self.text_model)
            concepts = self.request_story_concepts(
                assets,
                candidates,
                ledger.get("asset_coverage", []),
            )
            self._active_concept_tournament = concepts
            payload = self.request_treatment(
                assets,
                evidence_candidates=candidates,
                concept_tournament=concepts,
                asset_coverage=ledger.get("asset_coverage", []),
            )
            treatment = self.validate_treatment(payload, assets)
            treatment["concept_tournament"] = concepts
            treatment["selected_concept_id"] = concepts["selected_concept_id"]
            treatment["footage_ledger"] = str(ledger_path)
            treatment["evidence_fingerprint"] = ledger["evidence_fingerprint"]
            music_brief = self.build_music_brief(treatment, assets)
            self._atomic_write_json(
                treatment,
                treatment_destination,
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
            if self.text_model.casefold() != self.model.casefold():
                self.unload_model(self.text_model)

    def load_treatment(self, assets: Sequence[Dict[str, Any]]) -> Dict[str, Any]:
        """Load and validate the first-pass treatment. / 读取并校验第一次导演初审。"""
        if self.treatment_path is None:
            if not self._active_evidence_candidates:
                raise DirectorError(
                    "必须先完成中立视觉证据账本，才能生成导演阐述。"
                    " / A neutral visual evidence ledger is required before treatment."
                )
            if not self._active_concept_tournament:
                raise DirectorError(
                    "必须先完成三方案构想评审 / Story concept tournament is required before treatment."
                )
            payload = self.request_treatment(
                assets,
                evidence_candidates=self._active_evidence_candidates,
                concept_tournament=self._active_concept_tournament,
            )
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
        tournament = payload.get("concept_tournament") if isinstance(payload, dict) else None
        if isinstance(tournament, dict):
            self._active_concept_tournament = dict(tournament)
        return self.validate_treatment(payload, assets)

    @staticmethod
    def _require_treatment_evidence_fingerprint(
        treatment: Dict[str, Any], expected_fingerprint: str
    ) -> None:
        """
        Require a treatment to belong to the exact current evidence ledger.
        要求导演阐述严格属于当前完整证据账本。

        Parameters / 参数:
            treatment: Validated treatment loaded from disk or a plan. /
                从磁盘或计划读取的已校验导演阐述。
            expected_fingerprint: Fingerprint rebuilt from current media evidence. /
                由当前媒体证据重建的指纹。

        Legacy treatments without a fingerprint fail closed: accepting an empty
        value would let a new source/proxy/JPEG set reuse an unrelated story.
        旧阐述若没有指纹将安全失效，避免新素材误用旧故事。
        """
        actual = str(treatment.get("evidence_fingerprint") or "").strip()
        if not actual or actual != str(expected_fingerprint or "").strip():
            raise DirectorError(
                "导演阐述缺少有效证据指纹，或与当前素材不匹配；"
                "必须重跑音乐导演初审。 / Treatment lacks the exact current "
                "evidence fingerprint; rerun the first director pass."
            )

    @staticmethod
    def _assign_missing_candidate_ids(
        candidates: Sequence[Dict[str, Any]], prefix: str = "R"
    ) -> None:
        """
        Assign IDs only to derived candidates while preserving audit identities.
        仅为派生候选分配 ID，保留审片账本的原始身份。

        Parameters / 参数:
            candidates: Mutable assembly-only candidate dictionaries. /
                仅用于组装的可变候选字典。
            prefix: Namespace for newly derived IDs. / 新派生 ID 的命名空间。
        """
        used = {
            str(item.get("candidate_id") or "").strip()
            for item in candidates
            if str(item.get("candidate_id") or "").strip()
        }
        next_index = 1
        for candidate in candidates:
            if str(candidate.get("candidate_id") or "").strip():
                continue
            candidate_id = f"{prefix}{next_index:04d}"
            while candidate_id in used:
                next_index += 1
                candidate_id = f"{prefix}{next_index:04d}"
            candidate["candidate_id"] = candidate_id
            used.add(candidate_id)
            next_index += 1

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
        visual_review = payload.get("visual_review")
        expected_fingerprint = build_evidence_fingerprint(raw_path, self.model)
        try:
            review_version = int(
                visual_review.get("candidate_audit_version", 0)
                if isinstance(visual_review, dict)
                else 0
            )
        except (TypeError, ValueError):
            review_version = 0
        if not (
            isinstance(visual_review, dict)
            and visual_review.get("mode") == "neutral_complete_temporal_coverage"
            and visual_review.get("candidate_audit_complete") is True
            and review_version >= VISUAL_REVIEW_VERSION
            and str(visual_review.get("evidence_fingerprint") or "")
            == expected_fingerprint
        ):
            raise DirectorError(
                "已有计划的审片账本与当前素材证据不匹配，不能重新校验。"
                " / Existing audit does not match current evidence; revalidation is unsafe."
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
        self._require_treatment_evidence_fingerprint(
            treatment, expected_fingerprint
        )
        candidates = [
            dict(item) for item in audit
            if isinstance(item, dict) and not item.get("protected_story_anchor")
        ]
        if not candidates:
            candidates = self.candidates_from_treatment(treatment, assets)
        candidates = self.merge_decisions(candidates)
        self._assign_missing_candidate_ids(candidates, prefix="R")
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
                "candidate_count": len(audit),
                "assembly_candidate_count": len(candidates),
                "assembly_candidate_pool": candidates,
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

    def reassemble_existing_plan(
        self,
        raw_data_path: os.PathLike,
        plan_path: os.PathLike,
    ) -> Dict[str, Any]:
        """
        Reuse the complete visual audit but rerun global picture/music directing.
        复用已经完成的全片视觉审片，只重跑全局画面与配乐导演。

        Parameters / 参数:
            raw_data_path: Current combined extraction data. / 当前合并提取数据。
            plan_path: Existing schema-3 plan containing ``candidate_audit``. /
                含 ``candidate_audit`` 的现有 schema-3 剪辑计划。

        This recovery path fixes directing, prompt, music, and validator defects
        without spending many hours asking the vision model to inspect the same
        one-fps evidence again. / 此恢复路径无需让视觉模型再次逐秒审看相同素材。
        """
        raw_path = Path(raw_data_path).expanduser().resolve()
        destination = Path(plan_path).expanduser().resolve()
        raw_data = self.load_raw_data(raw_path)
        assets = raw_data.get("assets")
        if not isinstance(assets, list) or not assets:
            raise DirectorError(
                "快速重组需要多素材 raw_data / Reassembly requires multi-asset raw data."
            )
        try:
            existing = json.loads(destination.read_text(encoding="utf-8-sig"))
        except (OSError, json.JSONDecodeError) as exc:
            raise DirectorError(
                f"无法读取已有剪辑计划 / Cannot read existing plan: {exc}"
            ) from exc
        audit = existing.get("candidate_audit") if isinstance(existing, dict) else None
        if not isinstance(audit, list) or not audit:
            raise DirectorError(
                "已有计划没有 candidate_audit，无法跳过视觉审片。"
                " / Existing plan has no candidate audit; vision review cannot be skipped."
            )
        visual_review = existing.get("visual_review")
        expected_fingerprint = build_evidence_fingerprint(raw_path, self.model)
        try:
            review_version = int(
                visual_review.get("candidate_audit_version", 0)
                if isinstance(visual_review, dict)
                else 0
            )
        except (TypeError, ValueError):
            review_version = 0
        if not (
            isinstance(visual_review, dict)
            and visual_review.get("mode") == "neutral_complete_temporal_coverage"
            and visual_review.get("candidate_audit_complete") is True
            and review_version >= VISUAL_REVIEW_VERSION
            and str(visual_review.get("evidence_fingerprint") or "")
            == expected_fingerprint
        ):
            raise DirectorError(
                "已有视觉审片缓存与当前素材、视觉模型或审片协议不匹配；"
                "不能跳过完整审片。 / Existing visual audit does not match the current "
                "sources, vision model, or review protocol; full review cannot be skipped."
            )
        summaries = (
            visual_review.get("continuity_summaries")
            if isinstance(visual_review, dict)
            else None
        )
        self._asset_continuity_summaries = {
            str(key): str(value)
            for key, value in (summaries.items() if isinstance(summaries, dict) else [])
        }
        self._music_analysis = self.load_music_analysis()
        analyzed_tracks = self._music_analysis.get("tracks", [])
        self._music_files = [
            Path(str(item.get("file_name"))).expanduser().resolve()
            for item in analyzed_tracks
            if isinstance(item, dict) and str(item.get("file_name") or "").strip()
        ] or self.discover_music_files()
        treatment = self.load_treatment(assets)
        self._require_treatment_evidence_fingerprint(
            treatment, expected_fingerprint
        )
        self._active_treatment = treatment
        candidates = self._sanitize_candidate_bounds(
            [
                dict(item) for item in audit
                if isinstance(item, dict) and not item.get("protected_story_anchor")
            ],
            assets,
        )
        # A new treatment can select evidence that an older candidate audit did
        # not promote. Reassembly must not silently make those story anchors
        # unavailable to the editor. Keep the richer reviewed candidate when the
        # ranges overlap; otherwise restore the treatment anchor to the pool.
        treatment_candidates = self._sanitize_candidate_bounds(
            self.candidates_from_treatment(treatment, assets), assets
        )
        added_treatment_candidates = 0
        for anchor in treatment_candidates:
            anchor_asset = str(anchor.get("asset_id") or "")
            anchor_in = float(anchor.get("cut_in_sec", 0) or 0)
            anchor_out = float(anchor.get("cut_out_sec", 0) or 0)
            overlaps_existing = any(
                str(item.get("asset_id") or "") == anchor_asset
                and min(anchor_out, float(item.get("cut_out_sec", 0) or 0))
                - max(anchor_in, float(item.get("cut_in_sec", 0) or 0))
                >= 0.5
                for item in candidates
            )
            if not overlaps_existing:
                candidates.append(anchor)
                added_treatment_candidates += 1
        if added_treatment_candidates:
            self.logger.info(
                "Reassembly restored %d treatment anchors omitted by the reusable "
                "candidate audit",
                added_treatment_candidates,
            )
        candidates = self._attach_candidate_dialogue(candidates, assets)
        candidates = sorted(
            candidates,
            key=lambda item: (
                int(item.get("source_order", 0) or 0),
                float(item.get("cut_in_sec", 0) or 0),
                float(item.get("cut_out_sec", 0) or 0),
            ),
        )
        self._assign_missing_candidate_ids(candidates, prefix="R")
        try:
            self.check_ollama(model=self.text_model)
            sequence_payload = self.request_sequence(candidates, assets, treatment)
            final_clips = self.validate_sequence(
                sequence_payload, candidates, treatment
            )
            final_clips = self._attach_candidate_dialogue(final_clips, assets)
        finally:
            self.unload_model(self.text_model)
        program_duration = sum(
            max(
                0.0,
                float(item.get("cut_out_sec", 0))
                - float(item.get("cut_in_sec", 0)),
            )
            for item in final_clips
        )
        music_plan = self.validate_music_plan(
            sequence_payload.get("music_plan"), program_duration
        )
        # Music is selected against the draft lock, then visual-only trims may
        # move by a few frames to the nearest analyzed downbeat/strong beat.
        # This is the final picture operation; dialogue is never changed.
        # 配乐基于草案锁画选择，随后仅允许无对白镜头轻微吸附到实测强拍；这是最终
        # 一次画面调整，对白镜头绝不改动。
        final_clips = self.snap_visual_cuts_to_beats(
            final_clips, music_plan, assets
        )
        # Beat snapping may expose or remove a short transcript overlap at the
        # new out-point. Refresh literal dialogue evidence before calculating
        # ducking; stale pre-snap ranges can otherwise leave speech uncovered.
        # 卡点微调可能在新出点处增减少量对白，计算 ducking 前必须重新挂接实证区间。
        final_clips = self._attach_candidate_dialogue(final_clips, assets)
        program_duration = sum(
            max(
                0.0,
                float(item.get("cut_out_sec", 0))
                - float(item.get("cut_in_sec", 0)),
            )
            for item in final_clips
        )
        music_plan["program_duration_sec"] = round(program_duration, 4)
        music_plan = self.enforce_dialogue_ducking(final_clips, music_plan)
        music_plan = self.enrich_music_sync_points(final_clips, music_plan)
        graphics_plan = self.validate_graphics_plan(
            sequence_payload.get("graphics_plan"), final_clips, treatment
        )
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
        final_clips = self._attach_candidate_dialogue(final_clips, assets)
        for index, clip in enumerate(final_clips, start=1):
            clip["clip_id"] = index
            source_color = color_sources.get(str(clip.get("asset_id") or ""), {})
            if isinstance(source_color, dict):
                clip["source_color"] = {
                    key: value
                    for key, value in source_color.items()
                    if key != "color_match"
                }
                clip["color_match"] = dict(source_color.get("color_match") or {})
        output = dict(existing)
        output.update(
            {
                "generated_at_utc": datetime.now(timezone.utc).isoformat(),
                "project_fps": self.project_fps,
                "source_raw_data": str(raw_path),
                "director_model": self.text_model,
                "candidate_count": len(audit),
                "assembly_candidate_count": len(candidates),
                "assembly_candidate_pool": candidates,
                "project_summary": str(
                    sequence_payload.get("project_summary") or ""
                ).strip(),
                "viewer_takeaway": str(
                    sequence_payload.get("viewer_takeaway")
                    or treatment.get("viewer_takeaway")
                    or ""
                ).strip(),
                "editorial_style": str(
                    sequence_payload.get("editorial_style")
                    or treatment.get("edit_style")
                    or "hybrid_cinematic"
                ),
                "graphics_plan": graphics_plan,
                "picture_lock_audit": sequence_payload.get("picture_lock_audit", {}),
                "full_review_synopsis": sequence_payload.get(
                    "coverage_synopsis", {}
                ),
                "candidate_directing": sequence_payload.get(
                    "candidate_directing", {}
                ),
                "director_treatment": treatment,
                "target_duration_sec": self._active_target_duration_sec,
                "color_pipeline": color_pipeline,
                "music_plan": music_plan,
                "clips": final_clips,
                "reassembled_from_visual_audit_at_utc": datetime.now(
                    timezone.utc
                ).isoformat(),
            }
        )
        output.pop("audio_program", None)
        self._atomic_write_json(output, destination)
        self.logger.info(
            "快速重组完成：复用视觉审片并选择 %d 个镜头 / "
            "Reassembly reused visual review and selected %d clips",
            len(final_clips),
            len(final_clips),
        )
        return output

    @staticmethod
    def _footage_ledger_path(anchor: Path) -> Path:
        """Return the stable neutral-evidence ledger beside a stage artifact. / 返回阶段产物旁的中立证据账本路径。"""
        return anchor.with_name("footage_ledger.json")

    def _build_visual_review_chunks(
        self, assets: Sequence[Dict[str, Any]]
    ) -> List[Dict[str, Any]]:
        """
        Build overlapping transport windows without dropping saved samples.
        构建不丢弃任何已保存采样帧的重叠传输窗口。

        Ten-second cores plus one-second overlap hold at most 24 images at the
        high-quality 2 fps default, leaving headroom below Ollama's 32-image
        request guard. These are transport envelopes, not semantic chunks.
        默认 2 fps 下，10 秒核心加 1 秒重叠最多约 24 张图；这只是传输容器，
        并不把素材语义截断。
        """
        chunks: List[Dict[str, Any]] = []
        image_budget = self._vision_image_budget(EVIDENCE_ATOM_SCHEMA)
        for asset_order, asset in enumerate(assets):
            source_name = str(
                asset.get("proxy_file_name") or asset.get("source_video") or ""
            ).strip()
            if not source_name:
                raise DirectorError(
                    f"素材 {asset.get('asset_id')} 缺少媒体路径 / asset has no media path."
                )
            window_sec = 10.0
            prepared: List[Dict[str, Any]] = []
            for _attempt in range(8):
                overlap_sec = min(1.0, window_sec * 0.2)
                trial_chunks = self.chunk_raw_data(
                    asset,
                    window_sec=window_sec,
                    overlap_sec=overlap_sec,
                )
                prepared = []
                oversized = False
                for raw_chunk in trial_chunks:
                    chunk = dict(raw_chunk)
                    chunk["asset_id"] = str(asset.get("asset_id") or "")
                    chunk["source_name"] = source_name
                    chunk["source_video"] = str(asset.get("source_video") or "")
                    chunk["asset_label"] = Path(
                        str(asset.get("source_video") or source_name)
                    ).name
                    chunk["source_order"] = asset_order
                    # Use the maximum rolling continuity payload for preflight.
                    # CJK text is a conservative worst case for the zero-
                    # dependency token estimator.
                    probe = dict(chunk)
                    probe["continuity_context"] = "证" * 1200
                    prompt = self.build_prompt(
                        probe,
                        source_name,
                        EVIDENCE_ATOM_SCHEMA,
                        treatment=None,
                    )
                    frame_count = len(chunk.get("keyframes", []))
                    if (
                        frame_count > image_budget
                        or frame_count > 32
                        or not self._request_has_multimodal_capacity(
                            prompt,
                            EVIDENCE_ATOM_SCHEMA,
                            frame_count,
                            reserve_output_tokens=1024,
                        )
                    ):
                        oversized = True
                        break
                    prepared.append(chunk)
                if not oversized:
                    break
                if window_sec <= 0.5:
                    raise DirectorError(
                        "单个最小视觉窗口仍超过模型 Context；请提高 Context、降低抽帧尺寸，"
                        "或缩短异常长字幕段。 / One minimum visual window still exceeds the "
                        "model context; increase Context, reduce frame size, or split an unusually long transcript."
                    )
                window_sec = max(0.5, window_sec / 2.0)
            else:
                raise DirectorError(
                    "无法构建 Context 安全的视觉审片窗口 / "
                    "Could not build context-safe visual review windows."
                )
            if window_sec < 10.0:
                self.logger.info(
                    "视觉 Context 自适应：%s 使用 %.3fs 核心窗口，单次最多 %d 张图 / "
                    "Adaptive visual context: %s uses %.3fs cores with at most %d images",
                    Path(str(asset.get("source_video") or source_name)).name,
                    window_sec,
                    image_budget,
                    Path(str(asset.get("source_video") or source_name)).name,
                    window_sec,
                    image_budget,
                )
            chunks.extend(prepared)
        return chunks

    @classmethod
    def _deduplicate_event_atoms(
        cls, candidates: Sequence[Dict[str, Any]]
    ) -> List[Dict[str, Any]]:
        """
        Remove only duplicate observations created by overlap transport.
        仅删除由重叠传输窗口产生的重复观察，绝不合并相邻动作。

        Older code merged touching candidates into broad ranges and retained
        mostly the first event's metadata. This method keeps action boundaries
        intact and replaces a duplicate only when ranges substantially overlap
        and their literal action descriptions agree.
        旧逻辑会把相邻候选合成长段并丢失后续动作元数据；本方法保留动作边界，
        仅在时间高度重合且字面动作一致时去重。
        """
        ordered = sorted(
            (dict(item) for item in candidates if isinstance(item, dict)),
            key=lambda item: (
                int(item.get("source_order", 0) or 0),
                str(item.get("asset_id") or item.get("file_name") or ""),
                float(item.get("cut_in_sec", 0) or 0),
                float(item.get("cut_out_sec", 0) or 0),
            ),
        )
        result: List[Dict[str, Any]] = []
        for current in ordered:
            duplicate_index: Optional[int] = None
            current_start = float(current.get("cut_in_sec", 0) or 0)
            current_end = float(current.get("cut_out_sec", 0) or 0)
            current_duration = max(0.001, current_end - current_start)
            current_text = " ".join(str(
                current.get("subject_action") or current.get("visual_summary") or ""
            ).casefold().split())
            for index in range(len(result) - 1, max(-1, len(result) - 8), -1):
                previous = result[index]
                if str(previous.get("asset_id") or previous.get("file_name") or "") != str(
                    current.get("asset_id") or current.get("file_name") or ""
                ):
                    continue
                if str(previous.get("evidence_type") or "visual_atom") != str(
                    current.get("evidence_type") or "visual_atom"
                ):
                    continue
                previous_start = float(previous.get("cut_in_sec", 0) or 0)
                previous_end = float(previous.get("cut_out_sec", 0) or 0)
                overlap = min(previous_end, current_end) - max(previous_start, current_start)
                if overlap <= 0:
                    continue
                overlap_ratio = overlap / max(
                    0.001, min(previous_end - previous_start, current_duration)
                )
                previous_text = " ".join(str(
                    previous.get("subject_action") or previous.get("visual_summary") or ""
                ).casefold().split())
                similarity = SequenceMatcher(None, previous_text, current_text).ratio()
                if overlap_ratio >= 0.65 and similarity >= 0.58:
                    duplicate_index = index
                    break
            if duplicate_index is None:
                result.append(current)
                continue
            previous = result[duplicate_index]
            previous_score = float(previous.get("quality_score", 0.5) or 0.5) + float(
                previous.get("confidence", 0.5) or 0.5
            )
            current_score = float(current.get("quality_score", 0.5) or 0.5) + float(
                current.get("confidence", 0.5) or 0.5
            )
            if current_score > previous_score:
                result[duplicate_index] = current
        return sorted(
            result,
            key=lambda item: (
                int(item.get("source_order", 0) or 0),
                float(item.get("cut_in_sec", 0) or 0),
                float(item.get("cut_out_sec", 0) or 0),
            ),
        )

    def _load_footage_ledger(
        self,
        ledger_path: Path,
        raw_path: Path,
    ) -> Optional[Dict[str, Any]]:
        """Load a ledger only when source, model, sampling, and prompt match. / 仅在素材、模型、采样和提示完全一致时读取账本。"""
        try:
            payload = json.loads(ledger_path.read_text(encoding="utf-8-sig"))
        except (OSError, json.JSONDecodeError):
            return None
        if not isinstance(payload, dict):
            return None
        try:
            version = int(payload.get("visual_review_version", 0) or 0)
        except (TypeError, ValueError):
            return None
        expected = build_evidence_fingerprint(raw_path, self.model)
        candidates = payload.get("candidate_audit")
        coverage = payload.get("asset_coverage")
        if (
            version < VISUAL_REVIEW_VERSION
            or str(payload.get("evidence_fingerprint") or "") != expected
            or not isinstance(candidates, list)
            or not candidates
            or not isinstance(coverage, list)
        ):
            return None
        return payload

    def _dense_refinement_prompt(
        self,
        item: Dict[str, Any],
        review_start: float,
        review_end: float,
        frame_times: Sequence[float],
    ) -> str:
        """Build the literal dense-review prompt for capacity planning and execution. / 构建可同时用于容量规划与执行的密集复审提示。"""
        return (
            "DENSE TEMPORAL ATOM REVIEW. These are consecutive frames from one previously "
            "observed event, not thumbnails. Locate the earliest readable action entry, the "
            "visible action apex/reaction, and the clean exit before the action becomes static "
            "or repeats. Keep the candidate unless it is technically unusable or the alleged "
            "action is not visible. Use absolute source seconds only, stay within REVIEW RANGE, "
            "preserve a complete gesture, and do not trim spoken words from a dialogue atom. "
            "Infer no event between frames. Return JSON only.\n"
            f"REVIEW RANGE: {review_start:.4f}-{review_end:.4f}\n"
            f"ORIGINAL ATOM: {json.dumps(self._compact_story_evidence([item])[0], ensure_ascii=False, separators=(',', ':'))}\n"
            f"FRAME TIMES IN IMAGE ORDER: {json.dumps(list(frame_times))}"
        )

    def _dense_refinement_sampling_plan(
        self,
        item: Dict[str, Any],
        review_start: float,
        review_end: float,
    ) -> tuple[float, int]:
        """
        Choose the highest continuous sample rate that really fits vision Context.
        选择在视觉 Context 中真实可请求的最高连续采样率。

        The old fixed 28-frame request consumed about 43K visual tokens and was
        rejected even at 32K. This planner budgets the exact dense prompt plus
        image tokens, keeping at least two chronological samples or failing
        explicitly instead of silently claiming a 4-fps review.
        旧固定 28 帧约占 43K 视觉 token，32K 也必然失败；本方法用真实提示与图像
        token 共同预算，至少保留两张连续帧，否则明确失败，绝不伪称完成 4fps 复审。
        """
        duration = max(0.001, float(review_end) - float(review_start))
        desired_frames = min(28, max(2, int(math.floor(duration * 4.0)) + 1))
        for frame_count in range(desired_frames, 1, -1):
            sample_fps = min(4.0, (frame_count - 1) / duration)
            if sample_fps <= 0:
                continue
            frame_times = [
                round(min(review_end, review_start + index / sample_fps), 4)
                for index in range(frame_count)
            ]
            prompt = self._dense_refinement_prompt(
                item, review_start, review_end, frame_times
            )
            if self._request_has_multimodal_capacity(
                prompt,
                ATOM_REFINEMENT_SCHEMA,
                frame_count,
                reserve_output_tokens=1024,
            ):
                return sample_fps, frame_count
        raise DirectorError(
            "密集动作复审连两张连续帧也无法放入视觉 Context；拒绝静默降级。 / "
            "Dense temporal review cannot fit even two consecutive frames; refusing "
            "a silent degraded claim. " + self._context_capacity_guidance(self.model)
        )

    def _refine_event_atoms_temporally(
        self,
        candidates: Sequence[Dict[str, Any]],
        assets: Sequence[Dict[str, Any]],
    ) -> tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
        """
        Rewatch concise visual atoms at up to 4 fps for human-scale trim points.
        以最高 4 fps 重新审看短视觉动作，获得接近人工剪辑粒度的入点与出点。

        Parameters / 参数:
            candidates: Neutral 2-fps event atoms. / 中立 2fps 事件原子。
            assets: Source/proxy paths and authoritative durations. / 源/代理路径及权威时长。

        Returns / 返回:
            Refined atoms and explicit rejected-atom audit records.
            精修后的动作原子与明确记录的淘汰项。

        Spoken thoughts longer than eight seconds retain Whisper timing instead
        of pretending that a sparse visual pass can improve word boundaries.
        超过八秒的完整台词保留 Whisper 时间码，不伪称稀疏画面能改善字词边界。
        """
        ffmpeg = shutil.which("ffmpeg")
        if not ffmpeg:
            self.logger.warning(
                "未找到 FFmpeg，跳过动作密集复审 / FFmpeg missing; skipping dense temporal refinement"
            )
            preserved: List[Dict[str, Any]] = []
            for raw in candidates:
                item = dict(raw)
                item["temporal_refinement"] = {
                    "version": TEMPORAL_REFINEMENT_VERSION,
                    "status": "ffmpeg_unavailable",
                }
                preserved.append(item)
            return preserved, []
        asset_by_id = {
            str(asset.get("asset_id") or ""): asset for asset in assets
        }
        refined: List[Dict[str, Any]] = []
        rejected: List[Dict[str, Any]] = []
        reviewable = [
            item for item in candidates
            if float(item.get("cut_out_sec", 0) or 0)
            - float(item.get("cut_in_sec", 0) or 0) <= 8.0
        ]
        self.logger.info(
            "动作密集复审：%d/%d 个短事件原子，最高 4 fps / Dense temporal review: %d/%d short atoms at up to 4 fps",
            len(reviewable), len(candidates), len(reviewable), len(candidates),
        )
        for index, raw in enumerate(candidates, start=1):
            item = dict(raw)
            start = float(item.get("cut_in_sec", 0) or 0)
            end = float(item.get("cut_out_sec", start) or start)
            duration = max(0.0, end - start)
            if duration > 8.0:
                item["temporal_refinement"] = {
                    "version": TEMPORAL_REFINEMENT_VERSION,
                    "status": (
                        "dialogue_timing_from_whisper"
                        if bool(item.get("has_dialogue"))
                        else "broad_context_atom_not_extended"
                    ),
                }
                refined.append(item)
                continue
            asset = asset_by_id.get(str(item.get("asset_id") or ""), {})
            authoritative_duration = self._authoritative_asset_duration(asset)
            media_text = str(
                asset.get("proxy_file_name") or asset.get("source_video") or item.get("file_name") or ""
            ).strip()
            media = Path(media_text).expanduser()
            if not media.is_file():
                item["temporal_refinement"] = {
                    "version": TEMPORAL_REFINEMENT_VERSION,
                    "status": "media_unavailable",
                }
                refined.append(item)
                continue
            review_start = max(0.0, start - 0.35)
            review_end = min(
                authoritative_duration if authoritative_duration > 0 else end + 0.35,
                end + 0.35,
            )
            review_duration = max(0.25, review_end - review_start)
            sample_fps, frame_budget = self._dense_refinement_sampling_plan(
                item, review_start, review_end
            )
            self.logger.info(
                "动作精修 %d/%d：%s %.3f-%.3fs @ %.2ffps，%d 帧（Context 自适应） / "
                "Temporal refinement %d/%d at %.2f fps with %d context-safe frames",
                index, len(candidates), media.name, review_start, review_end,
                sample_fps, frame_budget, index, len(candidates), sample_fps,
                frame_budget,
            )
            try:
                with tempfile.TemporaryDirectory(prefix="cybereditor-atom-") as temporary:
                    frame_root = Path(temporary)
                    pattern = frame_root / "frame_%03d.jpg"
                    completed = subprocess.run(
                        [
                            ffmpeg, "-hide_banner", "-loglevel", "error", "-y",
                            "-ss", f"{review_start:.6f}", "-t", f"{review_duration:.6f}",
                            "-i", str(media),
                            "-vf", f"fps={sample_fps:.6f},scale=768:-2:flags=lanczos",
                            "-frames:v", str(frame_budget), "-q:v", "4", str(pattern),
                        ],
                        capture_output=True,
                        text=True,
                        encoding="utf-8",
                        errors="replace",
                        check=False,
                    )
                    frames = sorted(frame_root.glob("frame_*.jpg"))
                    if completed.returncode != 0 or len(frames) < 2:
                        item["temporal_refinement"] = {
                            "version": TEMPORAL_REFINEMENT_VERSION,
                            "status": "ffmpeg_failed",
                            "detail": self._compact_prompt_text(completed.stderr, 240),
                        }
                        refined.append(item)
                        continue
                    images = [
                        base64.b64encode(frame.read_bytes()).decode("ascii")
                        for frame in frames
                    ]
                    frame_times = [
                        round(review_start + frame_index / sample_fps, 4)
                        for frame_index in range(len(frames))
                    ]
                    prompt = self._dense_refinement_prompt(
                        item, review_start, review_end, frame_times
                    )
                    if not self._request_has_multimodal_capacity(
                        prompt,
                        ATOM_REFINEMENT_SCHEMA,
                        len(images),
                        reserve_output_tokens=1024,
                    ):
                        raise DirectorError(
                            "FFmpeg 实际帧数超过密集复审 Context 预算，已在请求前阻止。 / "
                            "Actual dense-review frames exceed the preflight budget."
                        )
                    response = self._request_json(
                        prompt,
                        ATOM_REFINEMENT_SCHEMA,
                        images=images,
                        model=self.model,
                        progress_activity="temporal_atom_refinement",
                    )
            except (DirectorError, OSError, subprocess.SubprocessError) as exc:
                # One malformed response must not discard an hours-long neutral
                # evidence pass. Preserve the original atom and make the degraded
                # refinement explicit in the ledger for later inspection.
                # 单个响应异常不得抹掉数小时的中立审片；保留原事件并在账本中显式降级。
                item["temporal_refinement"] = {
                    "version": TEMPORAL_REFINEMENT_VERSION,
                    "status": "refinement_failed_preserved",
                    "detail": self._compact_prompt_text(str(exc), 300),
                }
                refined.append(item)
                self.logger.warning(
                    "动作精修失败但已保留原事件：%s / Dense refinement failed; original atom preserved",
                    exc,
                )
                continue
            try:
                trim_in = self._finite_float(response.get("trim_in_sec"), "trim_in_sec")
                apex = self._finite_float(response.get("action_apex_sec"), "action_apex_sec")
                trim_out = self._finite_float(response.get("trim_out_sec"), "trim_out_sec")
            except DirectorError:
                trim_in, apex, trim_out = start, (start + end) / 2.0, end
            trim_in = max(review_start, min(review_end, trim_in))
            trim_out = max(review_start, min(review_end, trim_out))
            apex = max(trim_in, min(trim_out, apex))
            if not bool(response.get("keep")) or trim_out - trim_in < 0.4:
                rejected.append({
                    "asset_id": str(item.get("asset_id") or ""),
                    "original_in_sec": start,
                    "original_out_sec": end,
                    "reason": self._compact_prompt_text(
                        response.get("decision_reason") or "Dense temporal review rejected the atom.", 400
                    ),
                })
                # Dense review is a trim assistant, not the creative director.
                # A silent short-clip pass may flag quality, but it must never erase
                # neutral evidence (especially dialogue) before story selection.
                # 密集复审只负责时间边界，不得在导演选题前删除中立证据（尤其对白）。
                item["temporal_refinement"] = {
                    "version": TEMPORAL_REFINEMENT_VERSION,
                    "status": "model_rejected_but_preserved",
                    "decision_reason": self._compact_prompt_text(
                        response.get("decision_reason"), 400
                    ),
                }
                refined.append(item)
                continue
            # Dialogue boundaries come from Whisper and must not be shortened
            # by a silent visual-only model. It may still enrich motion states.
            if not bool(item.get("has_dialogue")):
                item["cut_in_sec"] = round(trim_in, 3)
                item["cut_out_sec"] = round(trim_out, 3)
            item["entry_state"] = self._compact_prompt_text(response.get("entry_state"), 300)
            item["action_apex"] = self._compact_prompt_text(response.get("action_apex"), 300)
            item["exit_state"] = self._compact_prompt_text(response.get("exit_state"), 300)
            item["screen_direction"] = self._enum_value(
                response.get("screen_direction"),
                {"left", "right", "toward_camera", "away_from_camera", "mixed", "none"},
                "none",
            )
            item["action_apex_sec"] = round(apex, 3)
            item["continuity_risk"] = self._compact_prompt_text(
                response.get("continuity_risk"), 300
            )
            item["temporal_refinement"] = {
                "version": TEMPORAL_REFINEMENT_VERSION,
                "status": "dense_reviewed",
                "sample_fps": round(sample_fps, 4),
                "frame_count": len(frames),
                "frame_budget": frame_budget,
                "original_in_sec": round(start, 3),
                "original_out_sec": round(end, 3),
                "decision_reason": self._compact_prompt_text(
                    response.get("decision_reason"), 400
                ),
            }
            refined.append(item)
        return refined, rejected

    def _review_footage_neutrally(
        self,
        assets: Sequence[Dict[str, Any]],
        raw_path: Path,
        ledger_path: Path,
    ) -> Dict[str, Any]:
        """
        Inspect every saved temporal sample before choosing any story.
        在选择任何故事之前，按时间顺序审看全部已保存采样帧。

        Parameters / 参数:
            assets: Validated project assets. / 已校验项目素材。
            raw_path: Combined extraction handoff. / 合并提取交接文件。
            ledger_path: Durable neutral evidence output. / 持久化中立证据输出。
        """
        reusable = self._load_footage_ledger(ledger_path, raw_path)
        if reusable is not None:
            self._asset_continuity_summaries = {
                str(key): str(value)
                for key, value in (
                    reusable.get("continuity_summaries", {}).items()
                    if isinstance(reusable.get("continuity_summaries"), dict) else []
                )
            }
            self._active_evidence_candidates = [
                dict(item) for item in reusable["candidate_audit"]
                if isinstance(item, dict)
            ]
            self.logger.info(
                "证据指纹匹配，复用完整中立审片账本 / Reusing fingerprint-matched neutral footage ledger"
            )
            return reusable

        chunks = self._build_visual_review_chunks(assets)
        fingerprint = build_evidence_fingerprint(raw_path, self.model)
        checkpoint_path = ledger_path.with_name("footage_ledger.checkpoint.json")
        completed_chunks = self._load_director_checkpoint(
            checkpoint_path, fingerprint
        )
        candidates: List[Dict[str, Any]] = []
        self._asset_continuity_summaries = {}
        coverage_by_asset: Dict[str, Dict[str, Any]] = {
            str(asset.get("asset_id") or ""): {
                "asset_id": str(asset.get("asset_id") or ""),
                "source_order": source_order,
                "file": Path(str(asset.get("source_video") or "")).name,
                "duration_sec": round(float(asset.get("duration_sec", 0) or 0), 3),
                "saved_visual_samples": len(asset.get("keyframes", [])),
                "windows_reviewed": 0,
                "empty_windows": 0,
                "candidate_atom_count": 0,
            }
            for source_order, asset in enumerate(assets)
        }
        self.logger.info(
            "中立完整审片：%d 个视频、%d 个连续窗口；导演阐述尚未生成 / "
            "Neutral full review: %d videos, %d temporal windows; no treatment exists yet",
            len(assets), len(chunks), len(assets), len(chunks),
        )
        self.check_ollama(require_vision=True)
        for index, chunk in enumerate(chunks, start=1):
            asset_id = str(chunk["asset_id"])
            coverage = coverage_by_asset[asset_id]
            coverage["windows_reviewed"] += 1
            chunk["continuity_context"] = self._asset_continuity_summaries.get(
                asset_id,
                "This is the beginning of the source; no earlier visual state exists.",
            )
            chunk_key = self._director_chunk_key(chunk)
            cached = completed_chunks.get(chunk_key)
            if isinstance(cached, list):
                cached_summary = next(
                    (
                        str(item.get("continuity_summary") or "").strip()
                        for item in reversed(cached)
                        if isinstance(item, dict)
                        and str(item.get("continuity_summary") or "").strip()
                    ),
                    "",
                )
                if cached_summary:
                    self._asset_continuity_summaries[asset_id] = cached_summary
                actual = [
                    dict(item) for item in cached
                    if isinstance(item, dict) and not item.get("_coverage_only")
                ]
                if not actual:
                    coverage["empty_windows"] += 1
                candidates.extend(actual)
                continue
            self.logger.info(
                "中立视觉审片 %d/%d：%s %.1fs-%.1fs / Neutral visual review %d/%d: %s %.1fs-%.1fs",
                index, len(chunks), chunk["asset_label"], chunk["start_sec"], chunk["end_sec"],
                index, len(chunks), chunk["asset_label"], chunk["start_sec"], chunk["end_sec"],
            )
            response = self.request_chunk(
                chunk,
                str(chunk["source_name"]),
                schema=EVIDENCE_ATOM_SCHEMA,
                include_images=True,
                treatment=None,
            )
            decisions = self.validate_chunk_decisions(
                response,
                chunk,
                str(chunk["source_name"]),
                neutral_evidence=True,
            )
            summary = self._compact_prompt_text(
                response.get("continuity_summary")
                or self._local_continuity_summary(chunk, decisions),
                2400,
            )
            self._asset_continuity_summaries[asset_id] = summary
            for decision in decisions:
                decision["asset_id"] = asset_id
                decision["source_order"] = int(chunk["source_order"])
                decision["continuity_summary"] = summary
                decision["evidence_prompt_version"] = VISUAL_EVIDENCE_PROMPT_VERSION
            if not decisions:
                coverage["empty_windows"] += 1
            candidates.extend(decisions)
            checkpoint_records = [dict(item) for item in decisions] or [{
                "_coverage_only": True,
                "continuity_summary": summary,
            }]
            completed_chunks[chunk_key] = checkpoint_records
            self._write_director_checkpoint(
                checkpoint_path, fingerprint, completed_chunks
            )

        candidates = self._deduplicate_event_atoms(candidates)
        candidates = self._sanitize_candidate_bounds(candidates, assets)
        candidates = self._attach_candidate_dialogue(candidates, assets)
        candidates = self._attach_complete_transcript_atoms(candidates, assets)
        candidates, temporal_rejections = self._refine_event_atoms_temporally(
            candidates, assets
        )
        candidates = self._sanitize_candidate_bounds(candidates, assets)
        candidates = self._attach_candidate_dialogue(candidates, assets)
        for index, candidate in enumerate(candidates, start=1):
            candidate["candidate_id"] = f"C{index:04d}"
            coverage_by_asset[str(candidate.get("asset_id") or "")][
                "candidate_atom_count"
            ] += 1
        if not candidates:
            raise DirectorError(
                "完整中立审片没有识别出任何可读事件原子；拒绝虚构影片。"
                " / Neutral full review found no readable event atoms; refusing to invent a film."
            )
        asset_coverage = []
        for asset in assets:
            item = coverage_by_asset[str(asset.get("asset_id") or "")]
            item["disposition"] = (
                "event_atoms_recorded"
                if int(item["candidate_atom_count"]) > 0
                else "reviewed_no_distinct_edit_atom"
            )
            item["review_conclusion"] = self._compact_prompt_text(
                self._asset_continuity_summaries.get(str(item["asset_id"]), ""),
                800,
            )
            asset_coverage.append(item)
        temporal_refined_count = sum(
            1
            for item in candidates
            if isinstance(item.get("temporal_refinement"), dict)
            and item["temporal_refinement"].get("status") == "dense_reviewed"
        )
        temporal_attempted_count = len(
            [
                item for item in candidates
                if isinstance(item.get("temporal_refinement"), dict)
                and item["temporal_refinement"].get("status") not in {
                    "dialogue_timing_from_whisper", "broad_context_atom_not_extended",
                    "media_unavailable", "ffmpeg_unavailable",
                }
            ]
        )
        temporal_sample_rates = [
            float(item["temporal_refinement"].get("sample_fps", 0) or 0)
            for item in candidates
            if isinstance(item.get("temporal_refinement"), dict)
            and item["temporal_refinement"].get("status") == "dense_reviewed"
        ]
        ledger = {
            "schema_version": "1.0",
            "generated_at_utc": datetime.now(timezone.utc).isoformat(),
            "visual_review_version": VISUAL_REVIEW_VERSION,
            "visual_prompt_version": VISUAL_EVIDENCE_PROMPT_VERSION,
            "evidence_fingerprint": fingerprint,
            "vision_model": self.model,
            "mode": "neutral_complete_temporal_coverage",
            "transport_batch_sec": 10.0,
            "transport_overlap_sec": 1.0,
            "transport_batch_count": len(chunks),
            "saved_visual_sample_count": sum(
                len(asset.get("keyframes", [])) for asset in assets
            ),
            "second_stage_frame_subsampling": True,
            "temporal_refinement_version": TEMPORAL_REFINEMENT_VERSION,
            "temporal_refinement_mode": (
                "context_adaptive_dense_up_to_4fps_for_atoms_le_8s"
                if temporal_refined_count
                else "dense_refinement_not_completed_original_atoms_preserved"
            ),
            "temporal_refined_count": temporal_refined_count,
            "temporal_refinement_attempted_count": temporal_attempted_count,
            "temporal_refinement_failed_count": max(
                0, temporal_attempted_count - temporal_refined_count
            ),
            "temporal_refinement_max_sample_fps": round(
                max(temporal_sample_rates, default=0.0), 4
            ),
            "temporal_rejections": temporal_rejections,
            "continuity_summaries": dict(self._asset_continuity_summaries),
            "asset_coverage": asset_coverage,
            "candidate_count": len(candidates),
            "candidate_audit": candidates,
        }
        self._active_evidence_candidates = [dict(item) for item in candidates]
        self._atomic_write_json(ledger, ledger_path)
        try:
            checkpoint_path.unlink(missing_ok=True)
        except OSError:
            pass
        self.logger.info(
            "中立证据账本完成：%d 个动作/事件原子，%d/%d 条素材有候选 / "
            "Neutral evidence ledger complete: %d atoms; %d/%d assets have candidates",
            len(candidates),
            sum(1 for item in asset_coverage if item["candidate_atom_count"]),
            len(asset_coverage),
            len(candidates),
            sum(1 for item in asset_coverage if item["candidate_atom_count"]),
            len(asset_coverage),
        )
        return ledger

    def _run_multi_asset(
        self,
        raw_data: Dict[str, Any],
        raw_path: Path,
        destination: Path,
    ) -> Dict[str, Any]:
        """
        Run visual candidate selection followed by global story assembly.
        先执行视觉候选片段筛选，再进行全局故事编排。

        The neutral ledger is produced before treatment and reused here only
        when its source/model/prompt fingerprint matches. Story directing never
        filters what the visual pass was allowed to observe.

        中立证据账本先于导演阐述生成，且仅在素材/模型/提示指纹一致时复用；故事导演
        不再反向限制视觉层允许观察的内容。
        """
        assets = raw_data["assets"]
        ledger_anchor = self.treatment_path or destination
        ledger_path = self._footage_ledger_path(ledger_anchor)
        try:
            ledger = self._review_footage_neutrally(
                assets, raw_path, ledger_path
            )
            candidates = [
                dict(item) for item in ledger.get("candidate_audit", [])
                if isinstance(item, dict)
            ]
            self._active_evidence_candidates = candidates
            self._music_analysis = self.load_music_analysis()
            analyzed_tracks = self._music_analysis.get("tracks", [])
            self._music_files = [
                Path(str(item.get("file_name"))).expanduser().resolve()
                for item in analyzed_tracks
                if isinstance(item, dict) and str(item.get("file_name") or "").strip()
            ] or self.discover_music_files()
            if self.text_model.casefold() != self.model.casefold():
                self.logger.info(
                    "完整视觉证据就绪，卸载 %s 后加载文字导演 %s / Switching from vision to text director",
                    self.model,
                    self.text_model,
                )
                self.unload_model(self.model)
                self.check_ollama(model=self.text_model)
            else:
                self.check_ollama(model=self.text_model)
            if self.treatment_path is None:
                concepts = self.request_story_concepts(
                    assets, candidates, ledger.get("asset_coverage", [])
                )
                concept_rows = [
                    item for item in concepts.get("concepts", [])
                    if isinstance(item, dict)
                ]
                preferred_id = str(concepts.get("selected_concept_id") or "")
                attempt_ids = [preferred_id] + [
                    str(item.get("concept_id") or "")
                    for item in sorted(
                        concept_rows,
                        key=lambda item: int(item.get("feasibility_score", 0) or 0),
                        reverse=True,
                    )
                    if str(item.get("concept_id") or "") != preferred_id
                ]
                concept_attempts: List[Dict[str, Any]] = []
                treatment = {}
                sequence_payload = {}
                for concept_attempt, concept_id in enumerate(attempt_ids, start=1):
                    attempt_tournament = dict(concepts)
                    attempt_tournament["selected_concept_id"] = concept_id
                    if concept_attempt > 1:
                        attempt_tournament["selection_reason"] = (
                            "The higher-ranked concept failed the blind-viewer editorial gate; "
                            "testing the next independently grounded concept."
                        )
                        self.logger.warning(
                            "首选构想未通过成片质量门，改试构想 %d/%d：%s / "
                            "Preferred concept failed; trying concept %d/%d: %s",
                            concept_attempt,
                            len(attempt_ids),
                            concept_id,
                            concept_attempt,
                            len(attempt_ids),
                            concept_id,
                        )
                    self._active_concept_tournament = attempt_tournament
                    treatment_payload = self.request_treatment(
                        assets,
                        evidence_candidates=candidates,
                        concept_tournament=attempt_tournament,
                        asset_coverage=ledger.get("asset_coverage", []),
                    )
                    attempted_treatment = self.validate_treatment(
                        treatment_payload, assets
                    )
                    attempted_treatment["concept_tournament"] = attempt_tournament
                    attempted_treatment["selected_concept_id"] = concept_id
                    attempted_treatment["footage_ledger"] = str(ledger_path)
                    attempted_treatment["evidence_fingerprint"] = ledger[
                        "evidence_fingerprint"
                    ]
                    self._active_treatment = attempted_treatment
                    try:
                        attempted_sequence = self.request_sequence(
                            candidates, assets, attempted_treatment
                        )
                    except EditorialQualityError as exc:
                        concept_attempts.append(
                            {
                                "concept_id": concept_id,
                                "status": "failed_editorial_gate",
                                "violations": list(exc.violations),
                                "metrics": dict(exc.metrics),
                                "blind_viewer_review": dict(exc.blind_review),
                            }
                        )
                        continue
                    concept_attempts.append(
                        {"concept_id": concept_id, "status": "passed"}
                    )
                    treatment = attempted_treatment
                    treatment["concept_attempt_audit"] = concept_attempts
                    sequence_payload = attempted_sequence
                    break
                if not treatment or not sequence_payload:
                    raise DirectorError(
                        "三种证据化构想及其重剪均未通过陌生观众质量门；已在进入 Resolve "
                        "前停止，请提供更明确主题或检查 footage_ledger.json。 / All three "
                        "evidence-backed concepts failed the blind-viewer gate; stopped before Resolve."
                    )
            else:
                treatment = self.load_treatment(assets)
                self._require_treatment_evidence_fingerprint(
                    treatment, str(ledger["evidence_fingerprint"])
                )
                self._active_treatment = treatment
                try:
                    sequence_payload = self.request_sequence(
                        candidates, assets, treatment
                    )
                except EditorialQualityError as first_failure:
                    tournament = treatment.get("concept_tournament")
                    tournament = tournament if isinstance(tournament, dict) else {}
                    original_id = str(
                        treatment.get("selected_concept_id")
                        or tournament.get("selected_concept_id")
                        or ""
                    )
                    alternatives = [
                        str(item.get("concept_id") or "")
                        for item in sorted(
                            (
                                item for item in tournament.get("concepts", [])
                                if isinstance(item, dict)
                            ),
                            key=lambda item: int(
                                item.get("feasibility_score", 0) or 0
                            ),
                            reverse=True,
                        )
                        if str(item.get("concept_id") or "")
                        and str(item.get("concept_id") or "") != original_id
                    ]
                    concept_attempts = [
                        {
                            "concept_id": original_id,
                            "status": "failed_editorial_gate",
                            "violations": list(first_failure.violations),
                            "metrics": dict(first_failure.metrics),
                            "blind_viewer_review": dict(
                                first_failure.blind_review
                            ),
                        }
                    ]
                    sequence_payload = {}
                    for offset, concept_id in enumerate(alternatives, start=2):
                        self.logger.warning(
                            "已保存的首选构想未通过成片质量门，改试构想 %d/%d：%s / "
                            "Saved preferred concept failed; trying %d/%d: %s",
                            offset,
                            len(alternatives) + 1,
                            concept_id,
                            offset,
                            len(alternatives) + 1,
                            concept_id,
                        )
                        attempt_tournament = dict(tournament)
                        attempt_tournament["selected_concept_id"] = concept_id
                        attempt_tournament["selection_reason"] = (
                            "The previously selected concept failed the blind-viewer gate; "
                            "testing the next grounded concept."
                        )
                        treatment_payload = self.request_treatment(
                            assets,
                            evidence_candidates=candidates,
                            concept_tournament=attempt_tournament,
                            asset_coverage=ledger.get("asset_coverage", []),
                        )
                        attempted_treatment = self.validate_treatment(
                            treatment_payload, assets
                        )
                        attempted_treatment["concept_tournament"] = attempt_tournament
                        attempted_treatment["selected_concept_id"] = concept_id
                        attempted_treatment["footage_ledger"] = str(ledger_path)
                        attempted_treatment["evidence_fingerprint"] = ledger[
                            "evidence_fingerprint"
                        ]
                        self._active_treatment = attempted_treatment
                        try:
                            attempted_sequence = self.request_sequence(
                                candidates, assets, attempted_treatment
                            )
                        except EditorialQualityError as exc:
                            concept_attempts.append(
                                {
                                    "concept_id": concept_id,
                                    "status": "failed_editorial_gate",
                                    "violations": list(exc.violations),
                                    "metrics": dict(exc.metrics),
                                    "blind_viewer_review": dict(exc.blind_review),
                                }
                            )
                            continue
                        concept_attempts.append(
                            {"concept_id": concept_id, "status": "passed"}
                        )
                        treatment = attempted_treatment
                        treatment["concept_attempt_audit"] = concept_attempts
                        sequence_payload = attempted_sequence
                        break
                    if not sequence_payload:
                        raise DirectorError(
                            "已保存构想及其全部证据化备选均未通过陌生观众质量门；"
                            "已在 Resolve 前停止。 / The saved concept and every grounded "
                            "alternative failed the blind-viewer gate; stopped before Resolve."
                        )
            self._active_treatment = treatment
            final_clips = self.validate_sequence(
                sequence_payload, candidates, treatment
            )
            final_clips = self._attach_candidate_dialogue(final_clips, assets)
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
        final_clips = self.snap_visual_cuts_to_beats(
            final_clips, music_plan, assets
        )
        # Keep the normal and reassembly paths identical: ducking must consume
        # dialogue ranges derived from the final, post-snap source boundaries.
        # 正常与重组路径都必须让 ducking 使用卡点后的最终对白范围。
        final_clips = self._attach_candidate_dialogue(final_clips, assets)
        program_duration = sum(
            max(
                0.0,
                float(item.get("cut_out_sec", 0))
                - float(item.get("cut_in_sec", 0)),
            )
            for item in final_clips
        )
        music_plan["program_duration_sec"] = round(program_duration, 4)
        music_plan = self.enforce_dialogue_ducking(final_clips, music_plan)
        music_plan = self.enrich_music_sync_points(final_clips, music_plan)
        graphics_plan = self.validate_graphics_plan(
            sequence_payload.get("graphics_plan"), final_clips, treatment
        )
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
                "mode": "neutral_complete_temporal_coverage",
                "candidate_audit_complete": True,
                "candidate_audit_version": VISUAL_REVIEW_VERSION,
                "visual_prompt_version": VISUAL_EVIDENCE_PROMPT_VERSION,
                "evidence_fingerprint": ledger["evidence_fingerprint"],
                "transport_batch_sec": ledger["transport_batch_sec"],
                "transport_overlap_sec": ledger["transport_overlap_sec"],
                "transport_batch_count": ledger["transport_batch_count"],
                "saved_visual_sample_count": ledger["saved_visual_sample_count"],
                "second_stage_frame_subsampling": bool(
                    ledger.get("second_stage_frame_subsampling")
                ),
                "temporal_refinement_version": str(
                    ledger.get("temporal_refinement_version") or ""
                ),
                "temporal_refinement_mode": str(
                    ledger.get("temporal_refinement_mode") or ""
                ),
                "temporal_refined_count": int(
                    ledger.get("temporal_refined_count", 0) or 0
                ),
                "temporal_refinement_attempted_count": int(
                    ledger.get("temporal_refinement_attempted_count", 0) or 0
                ),
                "temporal_refinement_failed_count": int(
                    ledger.get("temporal_refinement_failed_count", 0) or 0
                ),
                "temporal_refinement_max_sample_fps": float(
                    ledger.get("temporal_refinement_max_sample_fps", 0) or 0
                ),
                "temporal_rejections": list(
                    ledger.get("temporal_rejections", [])
                ),
                "continuity_summaries": dict(self._asset_continuity_summaries),
                "asset_coverage": list(ledger.get("asset_coverage", [])),
                "footage_ledger": str(ledger_path),
            },
            "candidate_count": len(candidates),
            "candidate_audit": candidates,
            "project_summary": str(sequence_payload.get("project_summary") or "").strip(),
            "viewer_takeaway": str(
                sequence_payload.get("viewer_takeaway")
                or treatment.get("viewer_takeaway")
                or ""
            ).strip(),
            "editorial_style": str(
                sequence_payload.get("editorial_style")
                or treatment.get("edit_style")
                or "hybrid_cinematic"
            ),
            "graphics_plan": graphics_plan,
            "picture_lock_audit": sequence_payload.get("picture_lock_audit", {}),
            "full_review_synopsis": sequence_payload.get("coverage_synopsis", {}),
            "candidate_directing": sequence_payload.get("candidate_directing", {}),
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
                authoritative_duration = self._authoritative_asset_duration(asset)
                if authoritative_duration > 0:
                    asset["duration_sec"] = authoritative_duration
                    transcript = asset.get("transcript")
                    if isinstance(transcript, list):
                        bounded_transcript: List[Dict[str, Any]] = []
                        for raw_segment in transcript:
                            if not isinstance(raw_segment, dict):
                                bounded_transcript.append(raw_segment)
                                continue
                            segment = dict(raw_segment)
                            try:
                                start_sec = float(segment.get("start_sec", 0))
                                end_sec = float(segment.get("end_sec", 0))
                            except (TypeError, ValueError):
                                bounded_transcript.append(segment)
                                continue
                            if start_sec >= authoritative_duration:
                                continue
                            segment["end_sec"] = round(
                                min(end_sec, authoritative_duration), 3
                            )
                            if float(segment["end_sec"]) > start_sec:
                                bounded_transcript.append(segment)
                        asset["transcript"] = self._sanitize_transcript_segments(
                            bounded_transcript,
                            f"assets[{index}]",
                        )
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

    def _sanitize_transcript_segments(
        self,
        segments: Sequence[Dict[str, Any]],
        source_label: str,
    ) -> List[Dict[str, Any]]:
        """
        Remove high-confidence structural signs of Whisper silence hallucination.
        删除具有明确结构特征的 Whisper 静音幻听。

        Parameters / 参数:
            segments: Timestamped Whisper segments. / 带时间戳的 Whisper 片段。
            source_label: Human-readable source identifier for logging. / 日志素材标识。

        The filter is deliberately conservative: it rejects only a tiny phrase
        stretched across a long range, or a segment whose own Whisper metadata
        identifies probable silence with poor log probability. Existing
        ``raw_data.json`` is cleaned on load, so a director-only rerun benefits
        without repeating GPU extraction.

        过滤器刻意保守：只删除横跨长时间的极短词组，或 Whisper 元数据同时显示
        高静音概率与低置信度的片段。旧 ``raw_data.json`` 在加载时也会获益。
        """
        kept: List[Dict[str, Any]] = []
        rejected = 0
        for raw in segments:
            if not isinstance(raw, dict):
                continue
            item = dict(raw)
            text = " ".join(str(item.get("text") or "").split())
            try:
                start = float(item.get("start_sec", 0))
                end = float(item.get("end_sec", start))
            except (TypeError, ValueError):
                kept.append(item)
                continue
            duration = max(0.0, end - start)
            speech_units = len(re.findall(r"[A-Za-z0-9\u4e00-\u9fff]", text))
            try:
                avg_logprob = float(item.get("avg_logprob"))
            except (TypeError, ValueError):
                avg_logprob = None
            try:
                no_speech_prob = float(item.get("no_speech_prob"))
            except (TypeError, ValueError):
                no_speech_prob = None
            implausibly_sparse = (
                (duration >= 8.0 and speech_units <= 4)
                or (duration >= 10.0 and speech_units / max(duration, 0.001) < 0.45)
            )
            low_confidence_silence = (
                no_speech_prob is not None
                and avg_logprob is not None
                and no_speech_prob >= 0.80
                and avg_logprob <= -0.50
            )
            if implausibly_sparse or low_confidence_silence:
                rejected += 1
                continue
            kept.append(item)
        if rejected:
            self.logger.warning(
                "%s 已过滤 %d 条长静音幻听字幕，防止误保留对白和错误压低配乐 / "
                "%s: filtered %d long-silence transcript hallucinations",
                source_label,
                rejected,
                source_label,
                rejected,
            )
        return kept

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
        sampling = asset.get("visual_sampling")
        if isinstance(sampling, dict) and (
            sampling.get("mode") == "continuous_temporal_coverage"
            and sampling.get("complete_source_span") is not True
        ):
            raise DirectorError(
                f"{prefix}.visual_sampling 没有完整覆盖素材时间轴；请重新运行提取。"
                " / visual sampling is incomplete; rerun extraction."
            )
        if isinstance(sampling, dict):
            try:
                recorded_count = int(sampling.get("saved_frame_count", len(keyframes)))
            except (TypeError, ValueError):
                recorded_count = -1
            if recorded_count != len(keyframes):
                raise DirectorError(
                    f"{prefix}.visual_sampling 帧数与关键帧清单不一致；缓存可能损坏。"
                    " / saved-frame count does not match the keyframe ledger."
                )
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

    @staticmethod
    def _authoritative_asset_duration(asset: Dict[str, Any]) -> float:
        """
        Return the real probed video duration, falling back to legacy metadata.
        返回探测到的真实视频时长；仅在旧数据缺失时回退到顶层时长。

        Whisper occasionally hallucinates a final subtitle beyond EOF. The video
        probe is therefore authoritative and transcript tails must never extend it.
        Whisper 偶尔会在文件结束后幻听字幕，因此视频探测值始终是时间边界真值。
        """
        video = asset.get("video")
        candidates = []
        if isinstance(video, dict):
            candidates.append(video.get("duration_sec"))
        candidates.append(asset.get("duration_sec"))
        for raw in candidates:
            try:
                value = float(raw)
            except (TypeError, ValueError):
                continue
            if math.isfinite(value) and value > 0:
                return value
        return 0.0

    def _sanitize_candidate_bounds(
        self,
        candidates: Sequence[Dict[str, Any]],
        assets: Sequence[Dict[str, Any]],
    ) -> List[Dict[str, Any]]:
        """
        Clamp candidate cuts to the authoritative media EOF and drop empty cuts.
        将候选剪点限制在真实媒体文件末尾，并删除无效空片段。

        Parameters / 参数:
            candidates: Vision/treatment candidate cuts. / 视觉与初审候选剪点。
            assets: Extracted media records containing ffprobe duration. /
                含 ffprobe 真实时长的素材记录。
        """
        by_asset = {
            str(asset.get("asset_id") or ""): self._authoritative_asset_duration(asset)
            for asset in assets
        }
        result: List[Dict[str, Any]] = []
        for raw in candidates:
            item = dict(raw)
            duration = by_asset.get(str(item.get("asset_id") or ""), 0.0)
            cut_in = max(0.0, float(item.get("cut_in_sec", 0) or 0))
            cut_out = float(item.get("cut_out_sec", 0) or 0)
            if duration > 0:
                cut_out = min(cut_out, duration)
            if cut_out - cut_in < 0.2:
                self.logger.warning(
                    "候选剪点越过真实媒体末尾，已删除：%s %.3f-%.3f / "
                    "Dropping candidate outside real media bounds",
                    Path(str(item.get("file_name") or "")).name,
                    cut_in,
                    float(item.get("cut_out_sec", 0) or 0),
                )
                continue
            item["cut_in_sec"] = round(cut_in, 3)
            item["cut_out_sec"] = round(cut_out, 3)
            result.append(item)
        return result

    def _attach_candidate_dialogue(
        self,
        candidates: Sequence[Dict[str, Any]],
        assets: Sequence[Dict[str, Any]],
    ) -> List[Dict[str, Any]]:
        """
        Attach exact overlapping dialogue evidence to every global candidate.
        为每个全局候选附加其时间范围内的真实对白证据。

        This prevents the text director from choosing or ordering a spoken shot
        using visual summaries alone. / 避免文字导演只看画面摘要便编排对白镜头。
        """
        transcripts = {
            str(asset.get("asset_id") or ""): [
                segment
                for segment in asset.get("transcript", [])
                if isinstance(segment, dict) and str(segment.get("text") or "").strip()
            ]
            for asset in assets
        }
        result: List[Dict[str, Any]] = []
        for raw in candidates:
            item = dict(raw)
            cut_in = float(item.get("cut_in_sec", 0) or 0)
            cut_out = float(item.get("cut_out_sec", 0) or 0)
            overlaps: List[str] = []
            ranges: List[Dict[str, Any]] = []
            for segment in transcripts.get(str(item.get("asset_id") or ""), []):
                start = float(segment.get("start_sec", 0) or 0)
                end = float(segment.get("end_sec", 0) or 0)
                if min(cut_out, end) - max(cut_in, start) < 0.15:
                    continue
                text = " ".join(str(segment.get("text") or "").split())
                overlap_start = max(cut_in, start)
                overlap_end = min(cut_out, end)
                overlaps.append(f"[{overlap_start:.1f}-{overlap_end:.1f}] {text}")
                ranges.append(
                    {
                        "start_sec": round(overlap_start, 3),
                        "end_sec": round(overlap_end, 3),
                        "text": self._compact_prompt_text(text, 180),
                    }
                )
            item["has_dialogue"] = bool(overlaps)
            item["dialogue_excerpt"] = self._compact_prompt_text(" | ".join(overlaps), 320)
            item["dialogue_ranges_sec"] = ranges
            # This is evidence for the director, never an automatic edit rule.
            # 此标记只向导演提示“可能是拍摄现场语境”，绝不自动裁切或静音。
            item["production_context_hint"] = self._is_production_chatter(item)
            result.append(item)
        return result

    def _attach_complete_transcript_atoms(
        self,
        candidates: Sequence[Dict[str, Any]],
        assets: Sequence[Dict[str, Any]],
    ) -> List[Dict[str, Any]]:
        """
        Preserve every complete Whisper segment independently of visual windows.
        将每条完整 Whisper 语段独立保留，不受视觉传输窗口边界影响。

        Parameters / 参数:
            candidates: Neutral visual atoms with dialogue overlaps. / 已附对白重叠的视觉原子。
            assets: Full extracted transcripts and source identities. / 完整字幕与素材身份。

        A 10-second image transport window must never cut a 25-second spoken
        thought into fragments.  Short segments already wholly represented by
        one visual atom are not duplicated; every other complete segment becomes
        a neutral transcript atom for the later text director to accept or reject.

        10 秒图像传输窗口绝不能截断 25 秒完整表达。已被单个视觉原子完整覆盖的短语段
        不重复；其余完整语段都会成为中立字幕原子，是否采用仍由后续导演决定。
        """
        result = [dict(item) for item in candidates if isinstance(item, dict)]
        visual_by_asset: Dict[str, List[Dict[str, Any]]] = {}
        for item in result:
            visual_by_asset.setdefault(str(item.get("asset_id") or ""), []).append(item)
        added = 0
        for source_order, asset in enumerate(assets):
            asset_id = str(asset.get("asset_id") or "")
            source_name = str(
                asset.get("proxy_file_name") or asset.get("source_video") or ""
            ).strip()
            duration = self._authoritative_asset_duration(asset)
            visual_atoms = visual_by_asset.get(asset_id, [])
            for segment_index, raw_segment in enumerate(asset.get("transcript", [])):
                if not isinstance(raw_segment, dict):
                    continue
                try:
                    start = max(0.0, float(raw_segment.get("start_sec", 0) or 0))
                    end = float(raw_segment.get("end_sec", start) or start)
                except (TypeError, ValueError):
                    continue
                if duration > 0:
                    end = min(end, duration)
                text = " ".join(str(raw_segment.get("text") or "").split())
                if end - start < 0.2 or not text:
                    continue
                segment_duration = end - start
                covering = next(
                    (
                        item for item in visual_atoms
                        if min(end, float(item.get("cut_out_sec", 0) or 0))
                        - max(start, float(item.get("cut_in_sec", 0) or 0))
                        >= segment_duration * 0.98
                    ),
                    None,
                )
                if covering is not None:
                    continue
                nearest: Dict[str, Any] = {}
                nearest_overlap = 0.0
                for visual_atom in visual_atoms:
                    overlap = max(
                        0.0,
                        min(end, float(visual_atom.get("cut_out_sec", 0) or 0))
                        - max(start, float(visual_atom.get("cut_in_sec", 0) or 0)),
                    )
                    if overlap >= 0.15 and overlap > nearest_overlap:
                        nearest = visual_atom
                        nearest_overlap = overlap
                excerpt = self._compact_prompt_text(text, 500)
                atom = {
                    "asset_id": asset_id,
                    "source_order": source_order,
                    "file_name": source_name,
                    "cut_in_sec": round(start, 3),
                    "cut_out_sec": round(end, 3),
                    "evidence_type": "transcript_atom",
                    "literal_observation": f"Timestamped spoken segment: {excerpt}",
                    "visual_summary": self._compact_prompt_text(
                        nearest.get("visual_summary")
                        or "Visual state is represented by overlapping neutral visual atoms.",
                        260,
                    ),
                    "subject_action": f"Speaker says: {excerpt}",
                    "emotion": self._compact_prompt_text(
                        nearest.get("emotion") or "Not determined from transcript alone.", 120
                    ),
                    "entry_state": "The timestamped spoken thought begins.",
                    "action_apex": self._compact_prompt_text(text, 260),
                    "exit_state": "The timestamped spoken thought ends.",
                    "screen_direction": str(nearest.get("screen_direction") or "none"),
                    "identity_tags": list(nearest.get("identity_tags") or [])[:8],
                    "action_phase": "development",
                    "shot_scale": str(nearest.get("shot_scale") or "medium"),
                    "camera_motion": str(nearest.get("camera_motion") or "static"),
                    "continuity_tags": ["complete_transcript_segment"],
                    "technical_readability": "clear",
                    "confidence": 0.95,
                    "quality_score": 0.8,
                    "has_dialogue": True,
                    "dialogue_excerpt": self._compact_prompt_text(
                        f"[{start:.1f}-{end:.1f}] {text}", 520
                    ),
                    "dialogue_ranges_sec": [
                        {
                            "start_sec": round(start, 3),
                            "end_sec": round(end, 3),
                            "text": self._compact_prompt_text(text, 400),
                        }
                    ],
                    "transcript_segment_index": segment_index,
                    "production_context_hint": False,
                }
                atom["production_context_hint"] = self._is_production_chatter(atom)
                result.append(atom)
                added += 1
        if added:
            self.logger.info(
                "已补入 %d 个跨视觉窗口的完整字幕原子 / "
                "Added %d complete transcript atoms spanning visual windows",
                added,
                added,
            )
        return sorted(
            result,
            key=lambda item: (
                int(item.get("source_order", 0) or 0),
                float(item.get("cut_in_sec", 0) or 0),
                0 if item.get("evidence_type") == "visual_atom" else 1,
            ),
        )

    def _normalize_coverage_synopsis(
        self,
        payload: Dict[str, Any],
        treatment: Dict[str, Any],
    ) -> Dict[str, Any]:
        """
        Normalize a compatible Ollama evidence audit into the canonical schema.
        将 Ollama 的兼容型证据审计归一化为标准结构。

        Qwen may return a semantically strong nested ``audit_report`` when the
        schema-constrained attempt falls back to plain JSON. Preserve that work
        instead of silently losing the critical absent-event fields.

        当 Schema 模式回退为普通 JSON 时，Qwen 可能返回语义正确的嵌套
        ``audit_report``；这里保留并标准化这些缺失事件证据。
        """
        canonical_keys = {
            "whole_footage_summary", "discovered_central_theme",
            "character_threads", "event_timeline", "visual_motifs",
            "continuity_risks", "observed_ending",
            "absent_or_unproven_events", "honest_adaptation",
        }
        if canonical_keys.issubset(payload):
            normalized = dict(payload)
            raw_absent = normalized.get("absent_or_unproven_events")
            absent_events = [
                " ".join(str(value).split())
                for value in (raw_absent if isinstance(raw_absent, list) else [])
                if str(value).strip()
            ]
            raw_risks = normalized.get("continuity_risks")
            continuity_risks = [
                " ".join(str(value).split())
                for value in (raw_risks if isinstance(raw_risks, list) else [])
                if str(value).strip()
            ]
            # Some compatible Qwen outputs put a clearly unsupported ending in
            # continuity_risks while returning an empty absent-event list. The
            # sequence prompt treats the latter as the hard factual boundary,
            # so promote explicit negative evidence instead of losing it.
            # 部分 Qwen 会把“未拍到结尾”只写进 continuity_risks；将明确否定证据
            # 同步进硬边界，避免后续导演继续假设素材中存在该动作。
            if not absent_events:
                negative_tokens = (
                    "not captured", "not shown", "no actual", "never shown",
                    "unsupported", "does not show", "only shows", "未拍到",
                    "没有拍", "未显示", "并未", "不支持", "只有准备",
                )
                absent_events = [
                    risk for risk in continuity_risks
                    if any(token in risk.casefold() for token in negative_tokens)
                ]
            normalized["continuity_risks"] = continuity_risks
            normalized["absent_or_unproven_events"] = absent_events
            if (
                (not isinstance(normalized.get("event_timeline"), list)
                 or not normalized["event_timeline"])
                and self._active_evidence_candidates
            ):
                normalized["event_timeline"] = [
                    {
                        "asset_id": str(item.get("asset_id") or ""),
                        "source_order": int(item.get("source_order", 0) or 0),
                        "event": self._compact_prompt_text(
                            item.get("subject_action") or item.get("visual_summary"), 500
                        ),
                        "story_meaning": "Literal neutral-review event; meaning remains for the director to determine.",
                    }
                    for item in self._active_evidence_candidates[:24]
                ]
            if (
                (not isinstance(normalized.get("visual_motifs"), list)
                 or not normalized["visual_motifs"])
                and self._active_evidence_candidates
            ):
                normalized["visual_motifs"] = list(dict.fromkeys(
                    "{} {}".format(
                        str(item.get("shot_scale") or "medium"),
                        str(item.get("camera_motion") or "static"),
                    )
                    for item in self._active_evidence_candidates
                ))[:10]
            return normalized

        memory = payload.get("project_memory")
        audit = payload.get("audit_report")
        revised = payload.get("revised_treatment")
        memory = memory if isinstance(memory, dict) else {}
        audit = audit if isinstance(audit, dict) else {}
        revised = revised if isinstance(revised, dict) else {}
        raw_arcs = memory.get("narrative_arc_observed")
        raw_arcs = raw_arcs if isinstance(raw_arcs, list) else []
        arcs = [
            " ".join(str(value).split())
            for value in raw_arcs
            if str(value).strip()
        ]
        characters = memory.get("characters")
        character_threads = [
            " ".join(str(value).split())
            for value in (characters if isinstance(characters, list) else [])
            if str(value).strip()
        ]
        absent = audit.get("absent_or_unproven_events")
        absent_events = [
            " ".join(str(value).split())
            for value in (absent if isinstance(absent, list) else [])
            if str(value).strip()
        ]
        continuity = memory.get("unresolved_intentions")
        continuity_risks = [
            " ".join(str(value).split())
            for value in (continuity if isinstance(continuity, list) else [])
            if str(value).strip()
        ]
        source_evidence = " ".join(str(audit.get("source_evidence") or "").split())
        observed_ending = source_evidence or (arcs[-1] if arcs else "No distinct ending action was proven.")
        summary_parts = [
            " ".join(str(memory.get("location") or "").split()),
            *arcs,
        ]
        raw_motifs = memory.get("visual_motifs")
        raw_motifs = raw_motifs if isinstance(raw_motifs, list) else []
        normalized = {
            "whole_footage_summary": " ".join(
                part for part in summary_parts if part
            ) or str(payload.get("whole_footage_summary") or "Observed project footage."),
            "discovered_central_theme": " ".join(
                str(
                    revised.get("central_theme")
                    or payload.get("discovered_central_theme")
                    or treatment.get("central_theme")
                    or "Observed human behavior and visual progression."
                ).split()
            ),
            "character_threads": character_threads,
            "event_timeline": [
                {
                    "asset_id": "multi-source",
                    "source_order": index,
                    "event": event,
                    "story_meaning": "Observed chronological project event.",
                }
                for index, event in enumerate(arcs)
            ],
            "visual_motifs": [
                " ".join(str(value).split())
                for value in raw_motifs
                if str(value).strip()
            ],
            "continuity_risks": continuity_risks,
            "observed_ending": observed_ending,
            "absent_or_unproven_events": absent_events,
            "honest_adaptation": " ".join(
                str(
                    audit.get("honest_adaptation")
                    or revised.get("ending_beat")
                    or "End on the strongest action actually observed."
                ).split()
            ),
        }
        if audit or revised:
            self.logger.warning(
                "Ollama 返回了兼容型嵌套证据审计，已标准化而未丢失缺失事件 / "
                "Normalized compatible nested evidence audit without losing absent events"
            )
        return normalized

    def _normalize_narrative_contract(
        self,
        payload: Dict[str, Any],
        candidates: Sequence[Dict[str, Any]],
        coverage_synopsis: Dict[str, Any],
    ) -> Dict[str, Any]:
        """
        Validate evidence references and downgrade unsupported causal promises.
        校验事件证据引用，并把无法证实的因果叙事降级为诚实的非剧情形式。

        Parameters / 参数:
            payload: Model-authored narrative contract. / 模型生成的叙事契约。
            candidates: Complete editable evidence ledger. / 完整可剪证据表。
            coverage_synopsis: Full-footage factual audit. / 全片事实审计。
        """
        valid_ids = {
            str(item.get("candidate_id") or "")
            for item in candidates if isinstance(item, dict)
        }
        chain: List[Dict[str, Any]] = []
        seen_ids = set()
        raw_chain = payload.get("causal_chain")
        for raw in raw_chain if isinstance(raw_chain, list) else []:
            if not isinstance(raw, dict):
                continue
            candidate_id = str(raw.get("candidate_id") or "").strip()
            if candidate_id not in valid_ids or candidate_id in seen_ids:
                continue
            seen_ids.add(candidate_id)
            chain.append(dict(raw))

        result = dict(payload)
        result["causal_chain"] = chain
        claimed_arc = bool(payload.get("has_causal_arc"))
        timeline = coverage_synopsis.get("event_timeline")
        observed_event_count = len(timeline) if isinstance(timeline, list) else 0
        proven_arc = claimed_arc and len(chain) >= 3 and observed_event_count >= 3
        result["has_causal_arc"] = proven_arc
        mode = str(payload.get("narrative_mode") or "mood_montage")
        if mode in {"causal_story", "bts_process"} and not proven_arc:
            result["narrative_mode"] = "mood_montage"
            result["contract_correction"] = (
                "The requested causal/BTS form lacked three distinct candidate-cited and "
                "chronologically audited state changes; it was downgraded to a truthful "
                "mood montage."
            )

        absent = coverage_synopsis.get("absent_or_unproven_events")
        unsupported = [
            " ".join(str(value).split())
            for value in (
                payload.get("unsupported_promises")
                if isinstance(payload.get("unsupported_promises"), list) else []
            )
            if str(value).strip()
        ]
        for value in absent if isinstance(absent, list) else []:
            normalized = " ".join(str(value).split())
            if normalized and normalized not in unsupported:
                unsupported.append(normalized)
        result["unsupported_promises"] = unsupported[:12]
        try:
            duration = float(payload.get("recommended_duration_sec", 0) or 0)
        except (TypeError, ValueError):
            duration = 0.0
        if duration <= 0:
            duration = min(90.0, max(15.0, self._active_target_duration_sec))
        result["recommended_duration_sec"] = round(min(600.0, max(10.0, duration)), 1)
        return result

    def _request_narrative_contract(
        self,
        candidates: Sequence[Dict[str, Any]],
        coverage_synopsis: Dict[str, Any],
        treatment: Dict[str, Any],
    ) -> Dict[str, Any]:
        """
        Convert footage evidence into a cited contract before editing begins.
        在开始剪辑前，将素材事实转换为逐事件引用的叙事契约。

        Parameters / 参数:
            candidates: Compact complete candidate ledger. / 紧凑的完整候选表。
            coverage_synopsis: Whole-footage evidence synthesis. / 全片证据汇总。
            treatment: Preliminary creative hypothesis. / 初步创作假设。
        """
        previous_review = self._rough_cut_feedback or {}
        def contract_prompt(
            prompt_candidates: Sequence[Dict[str, Any]], evidence_label: str
        ) -> str:
            return (
                "EVIDENCE-FIRST STORY CONTRACT. Act as an archivist/producer, not as the "
                "editor who will later defend a cut. Reconcile the preliminary treatment with "
                "the literal candidate evidence. Each causal_chain item must cite one supplied "
                "candidate_id and describe a visible or audible state change. Three distinct "
                "state changes are required before has_causal_arc may be true. Routine setup talk, "
                "a pose, a countdown, readiness, or a title is not automatically a consequence. "
                "If the evidence has no causal arc, choose mood_montage or character_vignette and "
                "do not preserve explanatory production chatter merely to manufacture a story. "
                "For bts_process, the chain must show an actual problem, attempt, and observed result. "
                "Put every promised but unseen event in unsupported_promises. Define three to six "
                "viewer-testable success criteria. The preliminary treatment is only a hypothesis "
                "and must be rejected where evidence conflicts. Return JSON only.\n"
                f"USER CREATIVE BRIEF: {self.creative_brief or '(free direction)'}\n"
                f"PRELIMINARY TREATMENT: {json.dumps(treatment, ensure_ascii=False, separators=(',', ':'))}\n"
                f"FULL-FOOTAGE AUDIT: {json.dumps(coverage_synopsis, ensure_ascii=False, separators=(',', ':'))}\n"
                f"PREVIOUS RENDERED ROUGH-CUT REVIEW: {json.dumps(previous_review, ensure_ascii=False, separators=(',', ':'))}\n"
                f"{evidence_label}: {json.dumps(list(prompt_candidates), ensure_ascii=False, separators=(',', ':'))}"
            )

        prompt_candidates = list(candidates)
        evidence_scope = "complete_candidate_ledger"
        prompt = contract_prompt(prompt_candidates, "COMPLETE CANDIDATE EVIDENCE")
        if not self._request_has_capacity(
            prompt,
            NARRATIVE_CONTRACT_SCHEMA,
            model=self.text_model,
            reserve_output_tokens=1536,
        ):
            tournament = self._active_concept_tournament
            selected_id = str(tournament.get("selected_concept_id") or "")
            selected_concept = next(
                (
                    item for item in tournament.get("concepts", [])
                    if isinstance(item, dict)
                    and str(item.get("concept_id") or "") == selected_id
                ),
                {},
            )
            proof_ids = set(
                str(value) for value in selected_concept.get("proof_candidate_ids", [])
            )
            proof_ids.add(str(selected_concept.get("ending_candidate_id") or ""))
            prompt_candidates = [
                item for item in candidates
                if str(item.get("candidate_id") or "") in proof_ids
            ]
            if not prompt_candidates:
                raise DirectorError(
                    "事件契约超过 Context，且优胜构想没有可回查证据；请提高 Context。"
                    " / Contract exceeds Context and the winning concept has no retrievable proof."
                )
            evidence_scope = "winning_concept_proof_after_all_atom_review"
            prompt = contract_prompt(
                prompt_candidates,
                "WINNING-CONCEPT PROOF (all candidates were reviewed upstream)",
            )
            if not self._request_has_capacity(
                prompt,
                NARRATIVE_CONTRACT_SCHEMA,
                model=self.text_model,
                reserve_output_tokens=1536,
            ):
                raise DirectorError(
                    "优胜构想的事件契约仍无法放入当前 Context；请提高 Context。"
                    " / Winning-concept contract evidence still exceeds Context."
                )
        self.logger.info(
            "正在建立逐事件证据契约 / Building cited event-by-event narrative contract"
        )
        payload = self._request_json(
            prompt,
            NARRATIVE_CONTRACT_SCHEMA,
            model=self.text_model,
            progress_activity="narrative_contract",
        )
        normalized = self._normalize_narrative_contract(
            payload, candidates, coverage_synopsis
        )
        normalized["evidence_scope"] = evidence_scope
        normalized["prompt_candidate_count"] = len(prompt_candidates)
        normalized["all_candidate_count"] = len(candidates)
        return normalized

    @staticmethod
    def _blind_storyboard(
        payload: Dict[str, Any],
        candidates: Sequence[Dict[str, Any]],
    ) -> List[Dict[str, Any]]:
        """
        Build what a stranger can literally see and hear, without director rationale.
        构造陌生观众实际能看到和听到的故事板，不泄露导演阐述与自我辩护。
        """
        candidate_by_id = {
            str(item.get("candidate_id") or ""): item
            for item in candidates if isinstance(item, dict)
        }
        storyboard: List[Dict[str, Any]] = []
        cursor = 0.0
        for index, raw in enumerate(payload.get("sequence", []), start=1):
            if not isinstance(raw, dict):
                continue
            candidate = candidate_by_id.get(str(raw.get("candidate_id") or ""), {})
            try:
                start = float(raw.get("trim_in_sec", candidate.get("cut_in_sec", 0)) or 0)
                end = float(raw.get("trim_out_sec", candidate.get("cut_out_sec", start)) or start)
            except (TypeError, ValueError):
                continue
            duration = max(0.0, end - start)
            intent = str(raw.get("audio_intent") or "natural_texture").casefold()
            audible_lines: List[str] = []
            if intent != "mute_for_music":
                ranges = candidate.get("dialogue_ranges_sec")
                for segment in ranges if isinstance(ranges, list) else []:
                    if not isinstance(segment, dict):
                        continue
                    try:
                        seg_start = float(segment.get("start_sec", 0) or 0)
                        seg_end = float(segment.get("end_sec", seg_start) or seg_start)
                    except (TypeError, ValueError):
                        continue
                    if min(end, seg_end) - max(start, seg_start) >= 0.05:
                        text = " ".join(str(segment.get("text") or "").split())
                        if text:
                            audible_lines.append(text)
            storyboard.append(
                {
                    "shot": index,
                    "timeline_in_sec": round(cursor, 3),
                    "timeline_out_sec": round(cursor + duration, 3),
                    "source": Path(str(candidate.get("file_name") or "")).name,
                    "visual": " ".join(str(candidate.get("visual_summary") or "").split()),
                    "action": " ".join(str(candidate.get("subject_action") or "").split()),
                    "audible_dialogue": audible_lines,
                    "transition": str(raw.get("transition_to_next") or "cut"),
                }
            )
            cursor += duration
        return storyboard

    def _request_blind_viewer_review(
        self,
        payload: Dict[str, Any],
        candidates: Sequence[Dict[str, Any]],
        narrative_contract: Dict[str, Any],
    ) -> Dict[str, Any]:
        """
        Ask a context-isolated stranger to explain the authored storyboard.
        让隔离上下文的陌生观众仅凭成片故事板复述影片。

        The prompt deliberately excludes the treatment, shot rationales, role labels,
        and intended takeaway. / 提示刻意排除导演阐述、镜头理由、功能标签和目标主题。
        """
        storyboard = self._blind_storyboard(payload, candidates)
        mode = str(narrative_contract.get("narrative_mode") or "causal_story")
        prompt = (
            "BLIND VIEWER TEST. You have never seen the creative brief, treatment, edit labels, "
            "or editor explanations. Read only the literal chronological storyboard below and "
            "report what an ordinary first-time viewer would understand. Do not infer off-screen "
            "events and do not reward attractive labels. A coherent causal film must make subject, "
            "goal, progression, and changed ending state legible. A mood montage need not have a "
            "plot, but it must have a clear subject, deliberate visual/emotional progression, and "
            "an earned payoff. Unrelated production conversations do not become a story because "
            "they share a location. Set passes=true only when coherence is at least 7 and the "
            "appropriate causal_clarity or visual_payoff score is at least 7. Return JSON only.\n"
            f"DECLARED FORM ONLY: {mode}\n"
            f"LITERAL STORYBOARD: {json.dumps(storyboard, ensure_ascii=False, separators=(',', ':'))}"
        )
        self.logger.info(
            "正在进行隔离上下文的陌生观众盲审 / Running context-isolated blind-viewer test"
        )
        review = self._request_json(
            prompt,
            BLIND_VIEWER_SCHEMA,
            model=self.text_model,
            progress_activity="blind_viewer_review",
        )
        coherence = int(review.get("coherence_score", 0) or 0)
        causal = int(review.get("causal_clarity_score", 0) or 0)
        payoff = int(review.get("visual_payoff_score", 0) or 0)
        form_score = payoff if mode in {"mood_montage", "character_vignette"} else causal
        deterministic_pass = (
            bool(review.get("passes"))
            and coherence >= 7
            and form_score >= 7
            and not bool(review.get("unsupported_or_unresolved_points"))
        )
        review["model_passes"] = bool(review.get("passes"))
        review["passes"] = deterministic_pass
        review["declared_narrative_mode"] = mode
        return review

    @staticmethod
    def _candidate_speech_intervals(
        candidate: Dict[str, Any],
    ) -> Optional[List[tuple[float, float]]]:
        """
        Return merged timestamped speech intervals for one candidate.
        返回单个候选镜头中已合并的精确语音时间区间。

        ``None`` means legacy data supplied only ``has_dialogue`` and therefore
        has no exact timing. An empty list is intentionally different: it means
        the extractor positively found no speech in this candidate.

        ``None`` 表示旧数据只有 ``has_dialogue``、没有精确时间；空列表则表示提取器
        已明确确认该候选镜头没有语音。两者不能混为一谈。
        """
        raw_ranges = candidate.get("dialogue_ranges_sec")
        if not isinstance(raw_ranges, list):
            return None
        try:
            candidate_in = float(candidate.get("cut_in_sec", 0) or 0)
            candidate_out = float(
                candidate.get("cut_out_sec", candidate_in) or candidate_in
            )
        except (TypeError, ValueError):
            return []
        intervals: List[tuple[float, float]] = []
        for raw_range in raw_ranges:
            if not isinstance(raw_range, dict):
                continue
            try:
                start = max(candidate_in, float(raw_range.get("start_sec", candidate_in)))
                end = min(candidate_out, float(raw_range.get("end_sec", candidate_out)))
            except (TypeError, ValueError):
                continue
            if end - start >= 0.05:
                intervals.append((start, end))
        intervals.sort()
        merged: List[List[float]] = []
        for start, end in intervals:
            if merged and start <= merged[-1][1] + 0.02:
                merged[-1][1] = max(merged[-1][1], end)
            else:
                merged.append([start, end])
        return [(start, end) for start, end in merged]

    @staticmethod
    def _candidate_silent_intervals(
        candidate: Dict[str, Any],
        speech_intervals: Optional[Sequence[tuple[float, float]]],
    ) -> List[tuple[float, float]]:
        """
        Return usable non-speech ranges inside a candidate.
        返回候选镜头范围内可用于纯画面剪辑的无语音区间。

        Parameters / 参数:
            candidate: Candidate metadata with source bounds. / 带源时间边界的候选镜头。
            speech_intervals: Exact merged speech ranges, or ``None`` for legacy
                unknown timing. / 精确合并语音区间；旧数据未知时为 ``None``。
        """
        if speech_intervals is None:
            return []
        try:
            candidate_in = float(candidate.get("cut_in_sec", 0) or 0)
            candidate_out = float(
                candidate.get("cut_out_sec", candidate_in) or candidate_in
            )
        except (TypeError, ValueError):
            return []
        cursor = candidate_in
        silent: List[tuple[float, float]] = []
        for start, end in speech_intervals:
            if start - cursor >= 0.25:
                silent.append((cursor, start))
            cursor = max(cursor, end)
        if candidate_out - cursor >= 0.25:
            silent.append((cursor, candidate_out))
        return silent

    @staticmethod
    def _picture_plan_metrics(
        payload: Dict[str, Any],
        candidates: Sequence[Dict[str, Any]],
    ) -> Dict[str, Any]:
        """
        Measure a draft for the supervising editor without changing the cut.
        为总剪辑师测量初稿；只报告，不修改导演剪辑。
        """
        candidate_by_id = {
            str(item.get("candidate_id") or ""): item
            for item in candidates
            if isinstance(item, dict)
        }
        sequence = payload.get("sequence")
        sequence = sequence if isinstance(sequence, list) else []
        total_duration = 0.0
        dialogue_duration = 0.0
        production_chatter_duration = 0.0
        long_dialogue_shots = 0
        static_shots = 0
        source_files = set()
        functions: List[str] = []
        music_roles: List[str] = []
        shot_scales: List[str] = []
        shot_audit: List[Dict[str, Any]] = []
        for raw in sequence:
            if not isinstance(raw, dict):
                continue
            candidate = candidate_by_id.get(str(raw.get("candidate_id") or ""), {})
            try:
                start = float(raw.get("trim_in_sec", candidate.get("cut_in_sec", 0)) or 0)
                end = float(raw.get("trim_out_sec", candidate.get("cut_out_sec", start)) or start)
            except (TypeError, ValueError):
                continue
            duration = max(0.0, end - start)
            total_duration += duration
            intent = str(raw.get("audio_intent") or "").casefold()
            try:
                volume_db = float(raw.get("volume_db", candidate.get("volume_db", 0)) or 0)
            except (TypeError, ValueError):
                volume_db = 0.0
            effectively_muted = intent == "mute_for_music" or volume_db <= -45.0
            source_speech_duration = 0.0
            shot_dialogue_duration = 0.0
            if bool(candidate.get("has_dialogue")):
                exact_intervals = AIDirector._candidate_speech_intervals(candidate)
                overlaps: List[tuple[float, float]] = []
                if exact_intervals is not None:
                    for speech_start, speech_end in exact_intervals:
                        range_start = max(start, speech_start)
                        range_end = min(end, speech_end)
                        if range_end - range_start >= 0.05:
                            overlaps.append((range_start, range_end))
                if overlaps:
                    overlaps.sort()
                    merged_ranges: List[List[float]] = []
                    for range_start, range_end in overlaps:
                        if merged_ranges and range_start <= merged_ranges[-1][1] + 0.02:
                            merged_ranges[-1][1] = max(merged_ranges[-1][1], range_end)
                        else:
                            merged_ranges.append([range_start, range_end])
                    source_speech_duration = sum(
                        range_end - range_start
                        for range_start, range_end in merged_ranges
                    )
                elif exact_intervals is None:
                    # Conservative compatibility fallback for legacy candidate
                    # ledgers that know speech exists but have no timestamps.
                    source_speech_duration = duration
                # When exact intervals exist but do not overlap the selected
                # trim, the trim is silent. The former fallback incorrectly
                # counted the whole visual trim as dialogue.
                shot_dialogue_duration = (
                    0.0 if effectively_muted else source_speech_duration
                )
            dialogue_duration += shot_dialogue_duration
            if shot_dialogue_duration > 0 and (
                bool(candidate.get("production_context_hint"))
                or AIDirector._is_production_chatter(candidate)
            ):
                production_chatter_duration += shot_dialogue_duration
            if shot_dialogue_duration > 6.0:
                long_dialogue_shots += 1
            if str(candidate.get("camera_motion") or "static").casefold() == "static":
                static_shots += 1
            source_files.add(str(candidate.get("file_name") or ""))
            functions.append(str(raw.get("narrative_function") or "unknown"))
            music_roles.append(str(raw.get("music_edit_role") or "unknown"))
            shot_scales.append(str(candidate.get("shot_scale") or "unknown"))
            shot_audit.append(
                {
                    "candidate_id": str(raw.get("candidate_id") or ""),
                    "trim_in_sec": round(start, 3),
                    "trim_out_sec": round(end, 3),
                    "duration_sec": round(duration, 3),
                    "source_speech_sec": round(source_speech_duration, 3),
                    "audible_speech_sec": round(shot_dialogue_duration, 3),
                    "audio_intent": intent or "unspecified",
                    "narrative_function": functions[-1],
                    "music_edit_role": music_roles[-1],
                }
            )

        def longest_run(values: Sequence[str]) -> int:
            best = current = 0
            previous = None
            for value in values:
                current = current + 1 if value == previous else 1
                previous = value
                best = max(best, current)
            return best

        clip_count = len([item for item in sequence if isinstance(item, dict)])
        graphics = payload.get("graphics_plan")
        graphic_items = graphics.get("items") if isinstance(graphics, dict) else []
        scale_counts = {
            scale: shot_scales.count(scale)
            for scale in sorted(set(shot_scales))
        }
        available_nonstatic = sum(
            1 for item in candidates
            if isinstance(item, dict)
            and str(item.get("camera_motion") or "static").casefold() != "static"
        )
        available_scales = {
            str(item.get("shot_scale") or "unknown")
            for item in candidates
            if isinstance(item, dict)
        }
        return {
            "clip_count": clip_count,
            "program_duration_sec": round(total_duration, 3),
            "average_shot_duration_sec": round(total_duration / clip_count, 3) if clip_count else 0.0,
            "preserved_dialogue_duration_sec": round(dialogue_duration, 3),
            "preserved_dialogue_ratio": round(dialogue_duration / total_duration, 3) if total_duration else 0.0,
            "audible_speech_duration_sec": round(dialogue_duration, 3),
            "audible_speech_ratio": round(dialogue_duration / total_duration, 3) if total_duration else 0.0,
            "production_chatter_duration_sec": round(production_chatter_duration, 3),
            "production_chatter_ratio": round(
                production_chatter_duration / total_duration, 3
            ) if total_duration else 0.0,
            "long_dialogue_shots_over_6_sec": long_dialogue_shots,
            "static_shot_ratio": round(static_shots / clip_count, 3) if clip_count else 0.0,
            "selected_nonstatic_shot_count": max(0, clip_count - static_shots),
            "available_nonstatic_candidate_count": available_nonstatic,
            "shot_scale_counts": scale_counts,
            "dominant_shot_scale_ratio": round(
                max(scale_counts.values()) / clip_count, 3
            ) if clip_count and scale_counts else 0.0,
            "available_shot_scale_count": len(available_scales),
            "unique_source_count": len({value for value in source_files if value}),
            "graphic_count": len(graphic_items) if isinstance(graphic_items, list) else 0,
            "longest_same_narrative_function_run": longest_run(functions),
            "longest_same_music_role_run": longest_run(music_roles),
            "narrative_functions": functions,
            "music_edit_roles": music_roles,
            "shot_audit": shot_audit,
        }

    def _picture_plan_quality_violations(
        self,
        payload: Dict[str, Any],
        metrics: Dict[str, Any],
        candidates: Sequence[Dict[str, Any]],
        treatment: Dict[str, Any],
        narrative_contract: Optional[Dict[str, Any]] = None,
        coverage_synopsis: Optional[Dict[str, Any]] = None,
        blind_review: Optional[Dict[str, Any]] = None,
    ) -> List[str]:
        """
        Return measurable reasons a model-authored cut needs another AI edit pass.
        返回模型剪辑需要再次交由 AI 重剪的可测量原因。

        Python never chooses replacement shots here. It only rejects obvious
        contradictions such as a supposedly visual montage that is 85% speech,
        five nearly identical static wides, or four consecutive beats with the
        same narrative and music role. The director model remains responsible
        for the revised creative decisions.

        Python 不在这里替换镜头，只退回明显自相矛盾的方案；如何重剪仍由导演模型决定。
        """
        clip_count = int(metrics.get("clip_count", 0) or 0)
        duration = float(metrics.get("program_duration_sec", 0) or 0)
        if clip_count <= 0 or duration <= 0:
            return ["The picture plan is empty or has no positive duration."]

        review = payload.get("review")
        review = review if isinstance(review, dict) else {}
        editorial_rules = treatment.get("editorial_rules")
        editorial_rules = editorial_rules if isinstance(editorial_rules, list) else []
        style_text = " ".join(
            str(value or "") for value in (
                payload.get("editorial_style"),
                payload.get("project_summary"),
                payload.get("viewer_takeaway"),
                treatment.get("edit_style"),
                treatment.get("central_theme"),
                treatment.get("logline"),
                treatment.get("development_beat"),
                review.get("dialogue_strategy"),
                " ".join(str(value) for value in editorial_rules),
            )
        ).casefold()
        brief_text = self.creative_brief.casefold()
        selected_ids = {
            str(item.get("candidate_id") or "")
            for item in payload.get("sequence", [])
            if isinstance(item, dict)
        }
        selected_candidates = [
            item for item in candidates
            if isinstance(item, dict)
            and str(item.get("candidate_id") or "") in selected_ids
        ]
        interview_count = sum(
            1 for item in selected_candidates
            if str(item.get("story_role") or "").casefold() == "interview"
        )
        explicit_dialogue_tokens = (
            "dialogue-led", "dialogue led", "interview", "conversation-led",
            "talking-head", "访谈", "采访", "口述", "对白为主", "谈话为主",
        )
        dialogue_led = (
            any(token in brief_text for token in explicit_dialogue_tokens)
            or (
                any(token in style_text for token in explicit_dialogue_tokens)
                and interview_count >= max(1, clip_count // 3)
            )
        )
        explicit_visual_tokens = (
            "kinetic_montage", "kinetic montage", "visual montage",
            "music video", "music-video", "silent film", "no dialogue",
            "dialogue-free", "pure montage", "纯视觉", "视觉蒙太奇",
            "音乐短片", "不要对白", "无对白",
        )
        explicitly_visual_led = any(
            token in brief_text or token in style_text
            for token in explicit_visual_tokens
        )

        violations: List[str] = []
        narrative_contract = (
            narrative_contract if isinstance(narrative_contract, dict) else {}
        )
        coverage_synopsis = (
            coverage_synopsis if isinstance(coverage_synopsis, dict) else {}
        )
        dialogue_ratio = float(
            metrics.get("audible_speech_ratio", metrics.get("preserved_dialogue_ratio", 0))
            or 0
        )
        if (
            explicitly_visual_led
            and not dialogue_led
            and (duration >= 15.0 or clip_count >= 4)
            and dialogue_ratio > 0.55
        ):
            violations.append(
                f"Measured preserved-dialogue ratio is {dialogue_ratio:.1%}; "
                "this is not an evidence-supported dialogue-led film and must be <=55%."
            )
        # ``production_context_hint`` is evidence for the director, never a
        # Python censorship rule.  Set talk can be premise, texture, comedy,
        # contrast, or noise; only the authored audio_intent and the blind-viewer
        # coherence review may decide whether it works in this particular film.
        # ``production_context_hint`` 只向导演提供证据，绝不是 Python 自动删词规则；
        # 现场讨论可能是主题、质感、喜剧或干扰，最终完全服从导演与陌生观众盲审。
        long_dialogue = int(metrics.get("long_dialogue_shots_over_6_sec", 0) or 0)
        if explicitly_visual_led and not dialogue_led and long_dialogue > 1:
            violations.append(
                f"There are {long_dialogue} dialogue passages longer than six seconds; "
                "retain at most one unless speech is the actual story."
            )
        static_ratio = float(metrics.get("static_shot_ratio", 0) or 0)
        available_nonstatic = int(
            metrics.get("available_nonstatic_candidate_count", 0) or 0
        )
        if clip_count >= 4 and static_ratio > 0.75 and available_nonstatic >= 3:
            violations.append(
                f"Static-shot ratio is {static_ratio:.1%} despite {available_nonstatic} "
                "available non-static candidates; create visible shot-to-shot variation."
            )
        dominant_scale = float(metrics.get("dominant_shot_scale_ratio", 0) or 0)
        available_scale_count = int(metrics.get("available_shot_scale_count", 0) or 0)
        if clip_count >= 4 and dominant_scale > 0.80 and available_scale_count >= 3:
            violations.append(
                f"One shot scale occupies {dominant_scale:.1%} of the cut although "
                f"{available_scale_count} scales are available; vary visual distance."
            )
        narrative_run = int(
            metrics.get("longest_same_narrative_function_run", 0) or 0
        )
        narrative_limit = 3 if clip_count <= 4 else 4
        if clip_count >= 4 and narrative_run >= narrative_limit:
            violations.append(
                f"{narrative_run} consecutive shots repeat the same narrative function; "
                "the middle needs escalation, contrast, or payoff rather than more context."
            )
        music_run = int(metrics.get("longest_same_music_role_run", 0) or 0)
        music_limit = 3 if clip_count <= 4 else 4
        if clip_count >= 4 and music_run >= music_limit:
            violations.append(
                f"{music_run} consecutive shots repeat one music-edit role; design an "
                "audible rhythmic progression instead of labeling every shot as build."
            )
        functions = [
            str(value).casefold() for value in metrics.get("narrative_functions", [])
        ]
        if clip_count >= 4 and not any(
            value in {"escalation", "contrast", "payoff"} for value in functions
        ):
            violations.append(
                "The cut has no authored escalation, contrast, or payoff beat between "
                "its hook and closure."
            )
        narrative_mode = str(
            narrative_contract.get("narrative_mode") or ""
        ).casefold()
        chain = narrative_contract.get("causal_chain")
        chain = chain if isinstance(chain, list) else []
        contract_ids = [
            str(item.get("candidate_id") or "")
            for item in chain if isinstance(item, dict)
        ]
        if bool(narrative_contract.get("has_causal_arc")) and len(contract_ids) >= 3:
            selected_chain_count = len(set(contract_ids) & selected_ids)
            if selected_chain_count < 3:
                violations.append(
                    "The evidence contract proves a causal arc, but the cut includes only "
                    f"{selected_chain_count} of its cited state-changing events; include at "
                    "least three or explicitly choose a non-causal form."
                )
        if (
            narrative_mode in {"causal_story", "bts_process"}
            and len(contract_ids) < 3
        ):
            violations.append(
                f"Narrative mode {narrative_mode!r} lacks three candidate-cited state "
                "changes and therefore cannot promise a causal story."
            )
        event_timeline = coverage_synopsis.get("event_timeline")
        if (
            narrative_mode in {"causal_story", "bts_process"}
            and not (isinstance(event_timeline, list) and len(event_timeline) >= 3)
        ):
            violations.append(
                "The whole-footage audit did not establish three chronological events, so "
                "the current causal/BTS claim is not grounded."
            )
        music_roles = [
            str(value).casefold() for value in metrics.get("music_edit_roles", [])
        ]
        teaser_then_chronological = (
            str(treatment.get("chronology_policy") or "").casefold()
            == "teaser_then_chronological"
            or "teaser" in str(treatment.get("opening_beat") or "").casefold()
        )
        if (
            clip_count >= 4
            and music_roles
            and music_roles[0] == "payoff_hit"
            and music_roles[-1] == "payoff_hit"
            and not teaser_then_chronological
        ):
            violations.append(
                "Both the opening teaser and ending are labeled payoff_hit; reserve the "
                "true payoff hit for the earned climax and give the hook a distinct role."
            )
        graphics_count = int(metrics.get("graphic_count", 0) or 0)
        if duration <= 120.0 and graphics_count > 2:
            violations.append(
                f"A {duration:.1f}s film contains {graphics_count} graphics; use at most "
                "two unless the footage proves distinct chapters."
            )
        if isinstance(blind_review, dict):
            coherence = int(blind_review.get("coherence_score", 0) or 0)
            causal = int(blind_review.get("causal_clarity_score", 0) or 0)
            payoff = int(blind_review.get("visual_payoff_score", 0) or 0)
            if not bool(blind_review.get("passes")):
                violations.append(
                    "The context-isolated blind viewer could not understand the cut: "
                    + " ".join(str(blind_review.get("reason") or "failed blind review").split())
                )
            elif coherence < 7:
                violations.append(
                    f"Blind-viewer coherence is {coherence}/10; at least 7/10 is required."
                )
            if narrative_mode in {"mood_montage", "character_vignette"}:
                if payoff < 7:
                    violations.append(
                        f"Blind-viewer visual payoff is {payoff}/10 for a non-causal film; "
                        "at least 7/10 is required."
                    )
            elif causal < 7:
                violations.append(
                    f"Blind-viewer causal clarity is {causal}/10; at least 7/10 is required."
                )
        return violations

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
            and name.casefold() != selected_model.casefold()
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
                "请安装视觉模型，例如："
                "ollama pull hf.co/ggml-org/Qwen3.8-27B-GGUF:Q4_K_M\n"
                "ollama cp hf.co/ggml-org/Qwen3.8-27B-GGUF:Q4_K_M qwen3.8:27b\n"
                "The selected model has no vision capability. Install a vision model."
            )
        normalized = self.model.casefold().replace("_", "-")
        known_vision_markers = (
            "qwen3.8", "qwen3.6", "qwen3.5", "qwen2.5-vl", "gemma3", "llava", "minicpm-v",
            "llama3.2-vision", "moondream",
        )
        if not any(marker in normalized for marker in known_vision_markers):
            raise DirectorError(
                f"无法确认模型 {self.model!r} 支持视觉输入。请改用 Qwen3.8 等视觉模型。\n"
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
                "单个连续审片批次超过 32 张图；请缩短传输窗口，不能静默丢帧。"
                " / A full-review batch exceeds 32 images; shorten the transport window."
            )
        images: List[str] = []
        if include_images:
            selected_frames, images = self._encode_images(selected_frames)
            expected_frames = chunk.get("keyframes", [])
            expected_count = len(expected_frames) if isinstance(expected_frames, list) else 0
            if not selected_frames or len(selected_frames) != expected_count:
                raise DirectorError(
                    "连续审片窗口的视觉证据不完整；拒绝把缺图窗口标记为已审完。"
                    "请重新运行提取阶段并检查磁盘/JPEG。 / Visual evidence is incomplete; "
                    "refusing to mark this review window complete. Rerun extraction and check JPEGs."
                )
        prompt_chunk = dict(chunk)
        prompt_chunk["keyframes"] = selected_frames
        active_schema = schema or DECISION_SCHEMA
        prompt = self.build_prompt(
            prompt_chunk, source_name, active_schema, treatment=treatment
        )
        return self._request_json(
            prompt, active_schema, images, progress_activity="visual_review"
        )

    @staticmethod
    def _director_system_prompt() -> str:
        """Return the shared grounded-director system prompt. / 返回统一的事实约束导演提示。"""
        return (
            "You are a senior documentary editor and visual storyteller. "
            "Use only supplied transcript, timestamps, and images. Return "
            "only the requested JSON and never invent content."
        )

    def _request_has_capacity(
        self,
        prompt: str,
        schema: Dict[str, Any],
        *,
        model: Optional[str] = None,
        reserve_output_tokens: int = 1024,
    ) -> bool:
        """
        Check whether a request fits without silently truncating its beginning.
        检查请求能否完整放入上下文，避免静默截断开头。

        Parameters / 参数:
            prompt: Model-facing evidence and instructions. / 模型输入证据与指令。
            schema: Required structured-output schema. / 结构化输出 Schema。
            model: Optional Ollama model override. / 可选 Ollama 模型覆盖。
            reserve_output_tokens: Minimum generation space to retain. / 最少保留输出空间。
        """
        selected_model = str(model or self.model).strip()
        request_num_ctx = self._effective_num_ctx(selected_model)
        schema_text = json.dumps(schema, ensure_ascii=False, separators=(",", ":"))
        estimated_input_tokens = self._estimate_prompt_tokens(
            self._director_system_prompt() + "\n" + prompt + "\n" + schema_text
        )
        return (
            estimated_input_tokens + max(1024, int(reserve_output_tokens)) + 256
            <= request_num_ctx
        )

    @staticmethod
    def _estimate_vision_tokens(image_count: int) -> int:
        """Conservatively estimate visual tokens for 1280px JPEG samples. / 保守估算 1280px JPEG 的视觉 token。"""
        return max(0, int(image_count)) * VISION_IMAGE_TOKEN_ESTIMATE

    def _request_has_multimodal_capacity(
        self,
        prompt: str,
        schema: Dict[str, Any],
        image_count: int,
        *,
        reserve_output_tokens: int = 1024,
    ) -> bool:
        """
        Check text, schema, and attached-image capacity together.
        联合检查文本、Schema 与附加图片的 Context 容量。

        Ollama does not expose final vision-token counts before generation, so
        the estimate intentionally assumes the extractor's maximum 1280px
        review image and retains a model-version safety margin.

        Ollama 在生成前不会公开最终视觉 token 数，因此按提取器最大 1280px
        审片图估算，并保留模型版本安全余量。
        """
        schema_text = json.dumps(schema, ensure_ascii=False, separators=(",", ":"))
        estimated_input_tokens = self._estimate_prompt_tokens(
            self._director_system_prompt() + "\n" + prompt + "\n" + schema_text
        ) + self._estimate_vision_tokens(image_count)
        return (
            estimated_input_tokens + max(1024, int(reserve_output_tokens)) + 256
            <= self._effective_num_ctx(self.model)
        )

    def _vision_image_budget(self, schema: Dict[str, Any]) -> int:
        """
        Return a conservative maximum image count for one visual request.
        返回单次视觉请求可承载的保守图片上限。

        The budget includes the real neutral-review instructions, schema, a
        worst-case rolling continuity summary, output space, and image tokens.
        预算包含真实中立审片提示、Schema、最长滚动连续性摘要、输出空间和图片 token。
        """
        skeleton = {
            "start_sec": 0.0,
            "end_sec": 10.0,
            "core_start_sec": 0.0,
            "core_end_sec": 10.0,
            "source_order": 0,
            "continuity_context": "x" * 1200,
            "transcript": [],
            "keyframes": [],
        }
        prompt = self.build_prompt(
            skeleton,
            "source.mp4",
            schema,
            treatment=None,
        )
        schema_text = json.dumps(schema, ensure_ascii=False, separators=(",", ":"))
        fixed_tokens = self._estimate_prompt_tokens(
            self._director_system_prompt() + "\n" + prompt + "\n" + schema_text
        )
        available = (
            self._effective_num_ctx(self.model)
            - fixed_tokens
            - 1024
            - 256
        )
        return min(32, max(1, available // VISION_IMAGE_TOKEN_ESTIMATE))

    def _request_json(
        self,
        prompt: str,
        schema: Dict[str, Any],
        images: Sequence[str] = (),
        model: Optional[str] = None,
        progress_activity: str = "director_generation",
    ) -> Dict[str, Any]:
        """
        Send one schema-constrained Ollama request with optional real images.
        发送一次受 Schema 约束、可包含真实图片的 Ollama 请求。
        """
        selected_model = str(model or self.model).strip()
        activity = re.sub(
            r"[^a-z0-9_]+", "_", str(progress_activity).casefold()
        ).strip("_") or "director_generation"
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
        system_prompt = self._director_system_prompt()
        schema_text = json.dumps(schema, ensure_ascii=False, separators=(",", ":"))
        estimated_input_tokens = self._estimate_prompt_tokens(
            system_prompt + "\n" + prompt + "\n" + schema_text
        ) + self._estimate_vision_tokens(len(images))
        # Ollama's num_ctx is shared by the prompt and generated response. Keep
        # a safety margin and lower num_predict only when a compact request is
        # still close to the configured window. Semantic prompt compaction is
        # performed by the caller (not by blindly truncating JSON evidence).
        # Ollama 的 num_ctx 由输入和输出共享。这里保留安全余量；语义压缩由调用方
        # 完成，绝不从中间粗暴截断 JSON 证据。
        available_output_tokens = request_num_ctx - estimated_input_tokens - 256
        if available_output_tokens < 1024:
            raise DirectorError(
                "Ollama 请求在发送前已被阻止：预计输入约 "
                f"{estimated_input_tokens} token，但模型上下文仅 {request_num_ctx}。"
                "候选必须先分层筛选、压缩，或缩短视觉窗口；禁止让 Ollama 从左侧静默截断证据。"
                " / Request blocked before Ollama: the prompt cannot leave 1024 output "
                "tokens. Compact evidence or shorten the visual window; silent left truncation is forbidden."
            )
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
        if any(marker in normalized_model for marker in ("qwen3.8", "qwen3.6")):
            # Temporal evidence extraction is a literal formatting task, but
            # concept selection and picture editing benefit from Qwen's real
            # reasoning pass. High-level calls therefore try thinking first and
            # retain the proven direct-JSON fallback when Ollama spends the
            # generation budget in its separate thinking field.
            # 时序证据提取属于事实记录；概念选择和总剪辑则需要真正推理。高层任务先
            # 启用 thinking，如 Ollama 将预算耗尽在 thinking 字段，仍回退直出 JSON。
            high_reasoning_activities = {
                "story_concepts", "director_treatment", "coverage_synthesis",
                "story_seed_page", "narrative_contract", "candidate_page_review",
                "picture_sequence", "picture_assembly", "picture_supervision",
                "picture_critique", "picture_recut", "blind_viewer",
                "blind_viewer_review", "music_direction", "music_spotting",
            }
            if activity in high_reasoning_activities:
                attempts = (
                    ("quality-reasoning", schema, True),
                    ("direct-json", schema, False),
                    ("compatibility-json", "json", False),
                )
            else:
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
            request_started = time.monotonic()
            heartbeat_stop = threading.Event()

            def progress_heartbeat() -> None:
                """Emit honest elapsed-time heartbeats while Ollama is blocking. / Ollama 阻塞时定时报告真实耗时。"""
                while not heartbeat_stop.wait(15.0):
                    self.logger.info(
                        "AI_PROGRESS state=working activity=%s elapsed=%d attempt=%d/%d",
                        activity,
                        int(time.monotonic() - request_started),
                        attempt_index,
                        len(attempts),
                    )

            self.logger.info(
                "AI_PROGRESS state=start activity=%s elapsed=0 attempt=%d/%d",
                activity,
                attempt_index,
                len(attempts),
            )
            heartbeat_thread = threading.Thread(
                target=progress_heartbeat,
                name=f"ollama-progress-{activity}",
                daemon=True,
            )
            heartbeat_thread.start()
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
                self.logger.info(
                    "AI_PROGRESS state=failed activity=%s elapsed=%d attempt=%d/%d",
                    activity,
                    int(time.monotonic() - request_started),
                    attempt_index,
                    len(attempts),
                )
                raise DirectorError(
                    f"Ollama 分块请求失败 / Ollama chunk request failed: {exc}"
                ) from exc
            finally:
                heartbeat_stop.set()
                heartbeat_thread.join(timeout=0.25)

            self.logger.info(
                "AI_PROGRESS state=complete activity=%s elapsed=%d attempt=%d/%d "
                "prompt_tokens=%s output_tokens=%s",
                activity,
                int(time.monotonic() - request_started),
                attempt_index,
                len(attempts),
                envelope.get("prompt_eval_count") if isinstance(envelope, dict) else None,
                envelope.get("eval_count") if isinstance(envelope, dict) else None,
            )

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

    def _context_capacity_guidance(self, model: str) -> str:
        """Return truthful capacity advice for the selected model. / 返回所选模型的真实容量建议。"""
        effective = self._effective_num_ctx(model)
        if effective < self.num_ctx:
            return (
                f"该 70B/72B 模型在混合内存安全策略下的有效 Context 固定为 {effective}，"
                f"界面配置的 {self.num_ctx} 不会提高本次请求上限；请依赖分页协议、减少固定证据，"
                "或改用拥有更大有效上下文的模型。 / "
                f"This 70B/72B model is hard-capped to an effective {effective}-token "
                f"context by the mixed-memory safety policy; raising the configured "
                f"{self.num_ctx}-token value does not raise this request limit. Use paging, "
                "reduce fixed evidence, or choose a model with a larger effective context."
            )
        return (
            f"当前有效 Context 为 {effective}；请减少固定证据或提高实际可用 Context。 / "
            f"The effective context is {effective}; reduce fixed evidence or raise the "
            "genuinely available context."
        )

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
        if treatment:
            review_mission = (
                "Use the treatment only to evaluate relevance after recording literal evidence. "
                "Never alter, omit, or reinterpret an observed action merely because it conflicts "
                "with the treatment. "
            )
            selection_guidance = (
                "Prefer complete sentences, expressive visuals, stable/focused shots, meaningful "
                "B-roll, and authentic moments; reject dead air, genuine repetition, camera setup, "
                "severe shake, accidental frames, and unusable audio. "
            )
        else:
            review_mission = (
                "NEUTRAL EVIDENCE PASS: no story, theme, genre, or treatment has been chosen. "
                "Act as a script supervisor, not as a selector. Record every distinct observable "
                "action, reaction, state change, complete useful spoken thought, shot-size change, "
                "camera move, focus/quality change, and possible beginning or ending. Do not rank "
                "events by an imagined film. Routine setup and static states may be recorded when "
                "they establish continuity, but describe them literally. Split consecutive actions "
                "into separate edit atoms; do not return one broad summary range. Most visual atoms "
                "should last 1.0-6.0 seconds and have a precise entry_state, action_apex, exit_state, "
                "screen_direction, and stable identity_tags. A complete spoken thought may be longer. "
            )
            selection_guidance = (
                "Do not discard a literal event merely because it looks like camera setup, dead air, "
                "or repetition: the later director, not this evidence pass, decides whether it belongs. "
                "Record its technical weakness in technical_readability/continuity_tags and omit it only when "
                "nothing changes or the image/audio is genuinely unreadable. "
            )
        return (
            "Analyze only this source-video window: "
            "{start:.3f}s to {end:.3f}s.\n"
            "{review_mission}Use "
            "absolute seconds in this source, not time relative to the chunk. "
            "Inspect every attached image in the listed IMAGE order. "
            "The images are time-ordered evidence, not independent thumbnails: compare "
            "adjacent timestamps and describe how subject action, camera motion, emotion, "
            "and shot scale progress. Infer continuity only from adjacent supplied frames; "
            "never invent an unseen action. Fill subject_action, temporal_phase, continuity_tags, "
            "entry_state, action_apex, exit_state, screen_direction, identity_tags, and "
            "technical_readability so the final director can reason about real temporal continuity. "
            "Read CONTINUITY FROM THE PREVIOUS BATCH first, then update continuity_summary "
            "into a cumulative account of the source so far. Preserve identities, locations, "
            "ongoing actions, unresolved intentions, and meaningful changes; do not reset the "
            "story at this transport boundary. OVERLAP_CONTEXT images are shown only to reconnect "
            "motion and must not create duplicate evidence atoms. "
            "{selection_guidance}Do not suggest transitions, color, filters, audio treatment, "
            "stabilization, story roles, or whether an event belongs in the final film. "
            "Every range must remain inside this transport window and cut_out_sec must be "
            "greater than cut_in_sec. Do not shorten an observed event to fit an imagined "
            "editing rhythm. Use an empty decisions "
            "array only when the window contains no distinct observable action, useful complete "
            "thought, reaction, state change, or usable visual state; explain that fact in the "
            "continuity_summary.\n"
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
            review_mission=review_mission,
            selection_guidance=selection_guidance,
            treatment=treatment_text,
            schema=schema_text,
            transcript="\n".join(transcript_lines) or "(none)",
            keyframes="\n".join(keyframe_lines) or "(none)",
        )

    @classmethod
    def _compact_story_evidence(
        cls, candidates: Sequence[Dict[str, Any]]
    ) -> List[Dict[str, Any]]:
        """Preserve every event atom in a compact story-facing ledger. / 紧凑保留每个事件原子供导演使用。"""
        return [
            {
                "candidate_id": str(item.get("candidate_id") or ""),
                "asset_id": str(item.get("asset_id") or ""),
                "source_order": int(item.get("source_order", 0) or 0),
                "in": round(float(item.get("cut_in_sec", 0) or 0), 3),
                "out": round(float(item.get("cut_out_sec", 0) or 0), 3),
                "literal_visual": cls._compact_prompt_text(
                    item.get("visual_summary") or item.get("reason_for_cut"), 180
                ),
                "action": cls._compact_prompt_text(item.get("subject_action"), 120),
                "entry": cls._compact_prompt_text(item.get("entry_state"), 100),
                "apex": cls._compact_prompt_text(item.get("action_apex"), 100),
                "exit": cls._compact_prompt_text(item.get("exit_state"), 100),
                "phase": str(item.get("action_phase") or "action"),
                "scale": str(item.get("shot_scale") or "medium"),
                "camera": str(item.get("camera_motion") or "static"),
                "direction": str(item.get("screen_direction") or "none"),
                "emotion": cls._compact_prompt_text(item.get("emotion"), 70),
                "dialogue": cls._compact_prompt_text(item.get("dialogue_excerpt"), 180),
                "quality": item.get("quality_score", 0.5),
                "temporal_review": (
                    {
                        "status": str(item["temporal_refinement"].get("status") or ""),
                        "advisory": cls._compact_prompt_text(
                            item["temporal_refinement"].get("decision_reason"), 180
                        ),
                    }
                    if isinstance(item.get("temporal_refinement"), dict)
                    else {}
                ),
            }
            for item in candidates
            if isinstance(item, dict)
        ]

    @classmethod
    def _compact_asset_coverage_for_prompt(
        cls,
        asset_coverage: Sequence[Dict[str, Any]],
        max_listed_sources: int = 24,
    ) -> Dict[str, Any]:
        """
        Build a bounded model-facing view of the durable source audit.
        为持久化逐素材审计构建有界的模型提示视图。

        Parameters / 参数:
            asset_coverage: Complete durable coverage rows. / 完整逐素材审计行。
            max_listed_sources: Maximum source identities placed in one prompt. /
                单次提示最多列出的素材身份数。

        Long rolling ``review_conclusion`` text is already represented by the
        event atoms and remains intact in ``footage_ledger.json``. Repeating up
        to 800 characters for every source made the fixed prefix alone exceed
        an 8K 72B context before recursive story reduction could begin.

        较长的 ``review_conclusion`` 已由事件原子承载，并完整保留在
        ``footage_ledger.json``。若在每个提示中为每条素材重复最多 800 字，
        固定前缀本身就会超过 72B 的 8K Context，递归归并也无从生效。
        """
        rows = [dict(item) for item in asset_coverage if isinstance(item, dict)]
        limit = max(1, int(max_listed_sources))
        if len(rows) <= limit:
            listed = rows
        else:
            # Preserve both the beginning and the ending of shooting order.
            # The roll-up below still accounts for every omitted source.
            head_count = (limit + 1) // 2
            tail_count = limit - head_count
            listed = rows[:head_count] + (rows[-tail_count:] if tail_count else [])

        disposition_counts: Dict[str, int] = {}
        total_duration = 0.0
        total_samples = 0
        total_atoms = 0
        for item in rows:
            status = cls._compact_prompt_text(
                item.get("disposition") or "unknown", 64
            )
            disposition_counts[status] = disposition_counts.get(status, 0) + 1
            try:
                total_duration += max(0.0, float(item.get("duration_sec", 0) or 0))
            except (TypeError, ValueError):
                pass
            try:
                total_samples += max(0, int(item.get("saved_visual_samples", 0) or 0))
            except (TypeError, ValueError):
                pass
            try:
                total_atoms += max(0, int(item.get("candidate_atom_count", 0) or 0))
            except (TypeError, ValueError):
                pass

        compact_rows: List[Dict[str, Any]] = []
        for item in listed:
            compact_rows.append(
                {
                    "id": cls._compact_prompt_text(item.get("asset_id"), 80),
                    "order": int(item.get("source_order", 0) or 0),
                    "file": cls._compact_prompt_text(item.get("file"), 80),
                    "duration_sec": round(float(item.get("duration_sec", 0) or 0), 2),
                    "visual_samples": int(item.get("saved_visual_samples", 0) or 0),
                    "event_atoms": int(item.get("candidate_atom_count", 0) or 0),
                    "status": cls._compact_prompt_text(
                        item.get("disposition") or "unknown", 64
                    ),
                }
            )
        return {
            "total_sources": len(rows),
            "total_duration_sec": round(total_duration, 2),
            "total_visual_samples": total_samples,
            "total_event_atoms": total_atoms,
            "disposition_counts": disposition_counts,
            "listed_sources": compact_rows,
            "omitted_source_count": max(0, len(rows) - len(compact_rows)),
            "durable_audit_note": (
                "Full per-source conclusions remain in footage_ledger.json; "
                "event atoms are the authoritative semantic evidence."
            ),
        }

    def _synthesize_story_seed_pages(
        self,
        evidence: Sequence[Dict[str, Any]],
        asset_coverage: Sequence[Dict[str, Any]],
    ) -> Dict[str, Any]:
        """
        Review every event atom in context-safe neutral pages before concept selection.
        在上下文安全的中立分页中逐一审阅所有事件原子，再进行全局构想选择。

        Parameters / 参数:
            evidence: Complete compact event ledger. / 完整紧凑事件账本。
            asset_coverage: Review disposition for every source. / 每条素材的审片结论。

        Returns / 返回:
            Reproducible page summaries and evidence-cited story seeds. / 可复核的分页摘要与证据化故事种子。

        This is a hierarchical review, not a fixed Top-N shortcut: every input id
        appears in exactly one page audit and remains available in the durable
        footage ledger. / 这是分层审阅而非固定 Top-N：每个输入 id 都恰好出现在一页
        审计中，原始事件仍完整保存在 footage ledger。
        """
        coverage_json = json.dumps(
            self._compact_asset_coverage_for_prompt(asset_coverage),
            ensure_ascii=False,
            separators=(",", ":"),
        )

        def page_prompt(page: Sequence[Dict[str, Any]], index: int) -> str:
            return (
                "NEUTRAL STORY-SEED REVIEW. This page is only one chronological portion of a "
                "complete event ledger; do not choose the final film yet. Examine EVERY atom. "
                "Identify factual progressions, character behavior, visual motifs, dialogue turns, "
                "and possible endings that a later global tournament could combine across pages. "
                "A seed must cite only supplied candidate ids. Do not promote routine production "
                "talk into BTS unless this page proves an observable state change. A pose, readiness, "
                "or countdown is not a departure. Describe limitations and missing payoff honestly. "
                "Return JSON only.\n"
                f"PAGE NUMBER: {index}\n"
                f"USER CREATIVE BRIEF: {self.creative_brief or '(free direction)'}\n"
                f"ASSET COVERAGE: {coverage_json}\n"
                f"EVENT ATOMS: {json.dumps(list(page), ensure_ascii=False, separators=(',', ':'))}"
            )

        if not self._request_has_capacity(
            page_prompt([], 1),
            STORY_SEED_PAGE_SCHEMA,
            model=self.text_model,
            reserve_output_tokens=1536,
        ):
            raise DirectorError(
                "故事证据分页的固定素材摘要已超过 Context；请减少素材数或提高 Context。"
                " / The fixed story-page coverage prefix exceeds Context before any atoms are added."
            )

        pages: List[List[Dict[str, Any]]] = []
        current: List[Dict[str, Any]] = []
        for item in evidence:
            trial = current + [dict(item)]
            if current and (
                len(trial) > 24
                or not self._request_has_capacity(
                    page_prompt(trial, len(pages) + 1),
                    STORY_SEED_PAGE_SCHEMA,
                    model=self.text_model,
                    reserve_output_tokens=1536,
                )
            ):
                pages.append(current)
                current = [dict(item)]
            else:
                current = trial
        if current:
            pages.append(current)

        audits: List[Dict[str, Any]] = []
        for page_index, page in enumerate(pages, start=1):
            prompt = page_prompt(page, page_index)
            if not self._request_has_capacity(
                prompt,
                STORY_SEED_PAGE_SCHEMA,
                model=self.text_model,
                reserve_output_tokens=1536,
            ):
                raise DirectorError(
                    "单个故事证据页仍超过 Context；请提高 Context。"
                    " / One story-evidence page still exceeds Context; increase it."
                )
            self.logger.info(
                "长片故事证据审阅 %d/%d：%d 个事件原子 / Story evidence page %d/%d: %d atoms",
                page_index, len(pages), len(page), page_index, len(pages), len(page),
            )
            payload = self._request_json(
                prompt,
                STORY_SEED_PAGE_SCHEMA,
                model=self.text_model,
                progress_activity="story_seed_page",
            )
            page_ids = {
                str(item.get("candidate_id") or "") for item in page
                if str(item.get("candidate_id") or "")
            }
            valid_seeds: List[Dict[str, Any]] = []
            for raw_seed in payload.get("story_seeds", []):
                if not isinstance(raw_seed, dict):
                    continue
                proof_ids = list(dict.fromkeys(
                    str(value) for value in raw_seed.get("proof_candidate_ids", [])
                    if str(value) in page_ids
                ))
                if not proof_ids:
                    continue
                seed = dict(raw_seed)
                ending_id = str(seed.get("possible_ending_candidate_id") or "")
                if ending_id in page_ids and ending_id not in proof_ids:
                    proof_ids = proof_ids[:9] + [ending_id]
                elif ending_id not in page_ids:
                    ending_id = proof_ids[-1]
                seed["proof_candidate_ids"] = proof_ids
                seed["possible_ending_candidate_id"] = ending_id
                valid_seeds.append(seed)
            if not valid_seeds:
                raise DirectorError(
                    f"故事证据第 {page_index} 页没有返回有效候选 id。"
                    " / Story-evidence page returned no valid candidate ids."
                )
            audits.append({
                "page": page_index,
                "input_candidate_ids": [
                    str(item.get("candidate_id") or "") for item in page
                ],
                "page_summary": self._compact_prompt_text(
                    payload.get("page_summary"), 1200
                ),
                "story_seeds": valid_seeds,
            })
        return {
            "mode": "hierarchical_all_atoms_reviewed",
            "all_candidates_considered": True,
            "input_candidate_count": len(evidence),
            "page_count": len(audits),
            "pages": audits,
        }

    @classmethod
    def _story_seed_prompt_view(
        cls,
        review: Dict[str, Any],
        nodes: Optional[Sequence[Dict[str, Any]]] = None,
        reduction_depth: int = 0,
    ) -> Dict[str, Any]:
        """
        Build a bounded prompt view without durable page provenance.
        构建不含持久化分页溯源的有界提示视图。

        Parameters / 参数:
            review: Complete durable hierarchical audit. / 完整持久化分层审计。
            nodes: Optional already-reduced seed nodes. / 可选的已归并种子节点。
            reduction_depth: Number of recursive reduction levels. / 递归归并层数。

        Candidate-id page membership remains in ``review['pages']`` on disk but
        never re-enters the model context. / 候选 ID 的分页归属仍保存在
        磁盘审计中，但不会再进入模型 Context。
        """
        source_nodes = list(nodes) if nodes is not None else [
            {
                "page_summary": page.get("page_summary", ""),
                "story_seeds": page.get("story_seeds", []),
            }
            for page in review.get("pages", [])
            if isinstance(page, dict)
        ]
        return {
            "mode": "recursive_story_seed_summary",
            "all_leaf_pages_reviewed": bool(review.get("all_candidates_considered")),
            "input_candidate_count": int(review.get("input_candidate_count", 0) or 0),
            "leaf_page_count": int(review.get("page_count", len(source_nodes)) or 0),
            "reduction_depth": int(reduction_depth),
            "nodes": [
                cls._compact_prompt_value(node)
                for node in source_nodes
                if isinstance(node, dict)
            ],
        }

    def _reduce_story_seed_nodes(
        self,
        nodes: Sequence[Dict[str, Any]],
        level: int,
    ) -> tuple[List[Dict[str, Any]], Dict[str, Any]]:
        """
        Recursively merge story-seed nodes into fewer context-safe nodes.
        递归将故事种子节点归并为更少的 Context 安全节点。

        Parameters / 参数:
            nodes: Chronological summaries from the previous level. / 上一层的时序摘要。
            level: One-based reduction level. / 从 1 开始的归并层级。

        Returns / 返回:
            Reduced prompt nodes plus a durable provenance audit. /
            归并后的提示节点与持久化溯源审计。
        """
        if not nodes:
            raise DirectorError(
                "没有可归并的故事种子 / No story-seed nodes to reduce."
            )

        def reduce_prompt(group: Sequence[Dict[str, Any]], group_index: int) -> str:
            return (
                "RECURSIVE STORY-SEED REDUCTION. These chronological nodes summarize earlier "
                "evidence pages. Examine EVERY node and merge only evidence-supported progressions. "
                "Do not select the final film. Preserve distinct possible endings and limitations. "
                "Every proof_candidate_id and possible_ending_candidate_id must already occur in "
                "the supplied nodes. Return JSON only.\n"
                f"REDUCTION LEVEL: {level}\nGROUP: {group_index}\n"
                f"USER CREATIVE BRIEF: {self.creative_brief or '(free direction)'}\n"
                "SEED NODES: "
                + json.dumps(list(group), ensure_ascii=False, separators=(",", ":"))
            )

        groups: List[List[Dict[str, Any]]] = []
        current: List[Dict[str, Any]] = []
        for raw_node in nodes:
            node = self._compact_prompt_value(raw_node)
            if not isinstance(node, dict):
                continue
            trial = current + [node]
            if current and (
                len(trial) > 8
                or not self._request_has_capacity(
                    reduce_prompt(trial, len(groups) + 1),
                    STORY_SEED_PAGE_SCHEMA,
                    model=self.text_model,
                    reserve_output_tokens=1536,
                )
            ):
                groups.append(current)
                current = [node]
            else:
                current = trial
        if current:
            groups.append(current)

        reduced: List[Dict[str, Any]] = []
        group_audits: List[Dict[str, Any]] = []
        for group_index, group in enumerate(groups, start=1):
            prompt = reduce_prompt(group, group_index)
            if not self._request_has_capacity(
                prompt,
                STORY_SEED_PAGE_SCHEMA,
                model=self.text_model,
                reserve_output_tokens=1536,
            ):
                raise DirectorError(
                    "单个故事种子归并组仍超过 Context。"
                    " / One story-seed reduction group still exceeds Context."
                )
            allowed_ids = {
                str(candidate_id).strip()
                for node in group
                for seed in (
                    node.get("story_seeds", []) if isinstance(node, dict) else []
                )
                if isinstance(seed, dict)
                for candidate_id in (
                    list(seed.get("proof_candidate_ids", []))
                    + [seed.get("possible_ending_candidate_id")]
                )
                if candidate_id is not None and str(candidate_id).strip()
            }
            payload = self._request_json(
                prompt,
                STORY_SEED_PAGE_SCHEMA,
                model=self.text_model,
                progress_activity="story_seed_reduce",
            )
            valid_seeds: List[Dict[str, Any]] = []
            for raw_seed in payload.get("story_seeds", []):
                if not isinstance(raw_seed, dict):
                    continue
                proof_ids = list(dict.fromkeys(
                    str(value) for value in raw_seed.get("proof_candidate_ids", [])
                    if str(value) in allowed_ids
                ))
                if not proof_ids:
                    continue
                seed = dict(raw_seed)
                ending_id = str(seed.get("possible_ending_candidate_id") or "")
                if ending_id in allowed_ids and ending_id not in proof_ids:
                    proof_ids = proof_ids[:9] + [ending_id]
                elif ending_id not in allowed_ids:
                    ending_id = proof_ids[-1]
                seed["proof_candidate_ids"] = proof_ids
                seed["possible_ending_candidate_id"] = ending_id
                valid_seeds.append(seed)
            if not valid_seeds:
                raise DirectorError(
                    f"故事种子归并层 {level} 组 {group_index} 没有有效证据 ID。"
                    " / Story-seed reduction returned no grounded ids."
                )
            node = {
                "page_summary": self._compact_prompt_text(
                    payload.get("page_summary"), 900
                ),
                "story_seeds": valid_seeds,
            }
            reduced.append(node)
            group_audits.append({
                "group": group_index,
                "input_node_count": len(group),
                "input_proof_candidate_ids": sorted(allowed_ids),
                "output_node": node,
            })
        return reduced, {
            "level": level,
            "input_node_count": len(nodes),
            "output_node_count": len(reduced),
            "groups": group_audits,
        }

    def request_story_concepts(
        self,
        assets: Sequence[Dict[str, Any]],
        candidates: Sequence[Dict[str, Any]],
        asset_coverage: Sequence[Dict[str, Any]],
    ) -> Dict[str, Any]:
        """
        Propose and evidence-check three genuinely different possible films.
        提出三种真正不同的成片方案，并逐一用全片证据验证。

        Parameters / 参数:
            assets: All source assets in shooting order. / 按拍摄顺序排列的全部素材。
            candidates: Complete neutral event-atom ledger. / 完整中立事件原子账本。
            asset_coverage: Explicit reviewed/rejected status for every asset.
                每条素材明确的已审看/无可剪事件状态。
        """
        evidence = self._compact_story_evidence(candidates)
        valid_ids = {str(item.get("candidate_id") or "") for item in candidates}
        coverage_prompt_view = self._compact_asset_coverage_for_prompt(
            asset_coverage
        )
        prompt_prefix = (
            "STORY CONCEPT TOURNAMENT. The complete footage was reviewed neutrally before any "
            "theme was chosen. Propose exactly three materially different films that this evidence "
            "can honestly support. At least one option should be a concise visual/style form when "
            "the material lacks a causal event; do not automatically choose BTS because production "
            "talk exists. Each concept must name exact proof_candidate_ids and an observed ending "
            "candidate. A pose, countdown, readiness, headlight, or forward lean is not a departure. "
            "Reject imagined smoke, travel, conflict, transformation, or payoff. Select the concept "
            "with the clearest blind-viewer premise, observable progression, strongest distinct "
            "ending, and least dependence on explanatory text. Prefer a focused 15-35 second film "
            "over padding when evidence is thin. Respect the user's brief. Return JSON only.\n"
            f"USER CREATIVE BRIEF: {self.creative_brief or '(free direction)'}\n"
            "ASSET COVERAGE SUMMARY (complete audit is durable outside this prompt): "
            f"{json.dumps(coverage_prompt_view, ensure_ascii=False, separators=(',', ':'))}\n"
        )
        prompt = (
            prompt_prefix
            + f"COMPLETE EVENT LEDGER ({len(evidence)} atoms): "
            + json.dumps(evidence, ensure_ascii=False, separators=(",", ":"))
        )
        hierarchical_review: Optional[Dict[str, Any]] = None
        if not self._request_has_capacity(
            prompt, STORY_CONCEPTS_SCHEMA, model=self.text_model, reserve_output_tokens=2048
        ):
            fixed_prefix_probe = (
                prompt_prefix
                + "HIERARCHICAL EVIDENCE SUMMARY: "
                + json.dumps(
                    {
                        "mode": "recursive_story_seed_summary",
                        "nodes": [],
                    },
                    ensure_ascii=False,
                    separators=(",", ":"),
                )
            )
            if not self._request_has_capacity(
                fixed_prefix_probe,
                STORY_CONCEPTS_SCHEMA,
                model=self.text_model,
                reserve_output_tokens=2048,
            ):
                raise DirectorError(
                    "故事构想的固定素材摘要已超过 Context；尚未开始昂贵的故事分页。"
                    " / The fixed concept prefix exceeds Context; story-page generation was not started."
                )
            self.logger.info(
                "完整事件账本超过单次 Context，改用全量分层审阅；不会静默截断 / "
                "Complete ledger exceeds one request; using hierarchical all-atom review"
            )
            hierarchical_review = self._synthesize_story_seed_pages(
                evidence, asset_coverage
            )
            nodes = self._story_seed_prompt_view(hierarchical_review)["nodes"]
            reduction_levels: List[Dict[str, Any]] = []
            reduction_depth = 0
            while True:
                prompt_view = self._story_seed_prompt_view(
                    hierarchical_review,
                    nodes=nodes,
                    reduction_depth=reduction_depth,
                )
                prompt = (
                    prompt_prefix
                    + "HIERARCHICAL EVIDENCE SUMMARY. Every candidate was examined in exactly "
                    "one chronological leaf page; the durable ledger and page audit remain "
                    "authoritative outside this prompt. Combine every supplied seed node and cite "
                    "only candidate ids present below: "
                    + json.dumps(
                        prompt_view, ensure_ascii=False, separators=(",", ":")
                    )
                )
                if self._request_has_capacity(
                    prompt,
                    STORY_CONCEPTS_SCHEMA,
                    model=self.text_model,
                    reserve_output_tokens=2048,
                ):
                    break
                if reduction_depth >= 8:
                    raise DirectorError(
                        "故事种子经过 8 层归并仍超过 Context。"
                        " / Story seeds still exceed Context after eight reduction levels."
                    )
                before_size = len(json.dumps(nodes, ensure_ascii=False))
                reduced_nodes, reduction_audit = self._reduce_story_seed_nodes(
                    nodes, reduction_depth + 1
                )
                after_size = len(json.dumps(reduced_nodes, ensure_ascii=False))
                if (
                    not reduced_nodes
                    or len(reduced_nodes) > len(nodes)
                    or (
                        len(reduced_nodes) == len(nodes)
                        and after_size >= before_size
                    )
                ):
                    raise DirectorError(
                        "故事种子归并未减少 Context，拒绝无限重试。"
                        " / Story-seed reduction made no context progress."
                    )
                nodes = reduced_nodes
                reduction_levels.append(reduction_audit)
                reduction_depth += 1
            hierarchical_review["reduction_levels"] = reduction_levels
            hierarchical_review["prompt_summary"] = prompt_view
        self.logger.info(
            "正在比较三种证据化成片构想 / Comparing three evidence-backed film concepts"
        )
        payload = self._request_json(
            prompt,
            STORY_CONCEPTS_SCHEMA,
            model=self.text_model,
            progress_activity="story_concepts",
        )
        concepts = payload.get("concepts")
        if not isinstance(concepts, list) or len(concepts) != 3:
            raise DirectorError(
                "导演必须返回三种成片构想 / Director must return exactly three concepts."
            )
        seen_forms = set()
        seen_concept_ids = set()
        valid_concepts: List[Dict[str, Any]] = []
        for concept in concepts:
            if not isinstance(concept, dict):
                continue
            concept_id = str(concept.get("concept_id") or "").strip()
            if not concept_id or concept_id in seen_concept_ids:
                continue
            proof_ids = [
                str(value) for value in concept.get("proof_candidate_ids", [])
                if str(value) in valid_ids
            ]
            ending_id = str(concept.get("ending_candidate_id") or "")
            minimum_proof = min(3, max(1, len(valid_ids)))
            unique_proof_ids = list(dict.fromkeys(proof_ids))
            if (
                len(unique_proof_ids) < minimum_proof
                or ending_id not in unique_proof_ids
            ):
                continue
            normalized = dict(concept)
            normalized["proof_candidate_ids"] = unique_proof_ids
            normalized["ending_candidate_id"] = ending_id
            seen_forms.add(str(normalized.get("form") or ""))
            seen_concept_ids.add(concept_id)
            valid_concepts.append(normalized)
        if len(valid_concepts) != 3 or len(seen_forms) < 2:
            raise DirectorError(
                "三种构想没有提供足够且不同的真实证据；拒绝在薄弱前提上继续剪辑。"
                " / The three concepts lack distinct, grounded proof; refusing a weak premise."
            )
        selected_id = str(payload.get("selected_concept_id") or "")
        if selected_id not in {
            str(item.get("concept_id") or "") for item in valid_concepts
        }:
            selected_id = str(max(
                valid_concepts,
                key=lambda item: int(item.get("feasibility_score", 0) or 0),
            ).get("concept_id") or "")
        result = {
            "concepts": valid_concepts,
            "selected_concept_id": selected_id,
            "selection_reason": self._compact_prompt_text(
                payload.get("selection_reason"), 800
            ),
        }
        if hierarchical_review is not None:
            result["evidence_review"] = hierarchical_review
        return result

    def request_treatment(
        self,
        assets: Sequence[Dict[str, Any]],
        evidence_candidates: Optional[Sequence[Dict[str, Any]]] = None,
        concept_tournament: Optional[Dict[str, Any]] = None,
        asset_coverage: Optional[Sequence[Dict[str, Any]]] = None,
    ) -> Dict[str, Any]:
        """
        Create a treatment only after neutral full-footage evidence exists.
        仅在中立完整审片证据形成后创建导演阐述。

        Parameters / 参数:
            assets: Validated sources in shooting order. / 按拍摄顺序的已校验素材。
            evidence_candidates: Complete neutral event atoms. / 完整中立事件原子。
            concept_tournament: Three evidence-backed concepts and the selected one.
                三种证据化构想及其优胜方案。
            asset_coverage: Explicit review status for every source. / 每条素材的审片状态。
        """
        total_duration = sum(float(item.get("duration_sec", 0)) for item in assets)
        automatic_target = min(180.0, max(20.0, total_duration * 0.08))
        requested_target = self.target_duration_sec or automatic_target
        self._active_target_duration_sec = round(requested_target, 1)
        evidence = self._compact_story_evidence(evidence_candidates or [])
        if not evidence:
            raise DirectorError(
                "导演阐述前没有完整视觉证据账本 / No full visual evidence ledger exists before treatment."
            )
        tournament = concept_tournament if isinstance(concept_tournament, dict) else {}
        selected_id = str(tournament.get("selected_concept_id") or "")
        selected_concept = next(
            (
                dict(item) for item in tournament.get("concepts", [])
                if isinstance(item, dict) and str(item.get("concept_id") or "") == selected_id
            ),
            {},
        )
        if not selected_concept:
            raise DirectorError(
                "导演阐述缺少已验证的优胜构想 / Treatment lacks a validated winning concept."
            )
        compact_assets = [
            {
                "source_order": source_order,
                "asset_id": str(asset.get("asset_id") or ""),
                "file": Path(str(asset.get("source_video") or "")).name,
                "duration_sec": round(float(asset.get("duration_sec", 0)), 2),
                "review_summary": self._compact_prompt_text(
                    self._asset_continuity_summaries.get(str(asset.get("asset_id") or ""), ""),
                    700,
                ),
            }
            for source_order, asset in enumerate(assets)
        ]
        brief = self.creative_brief or (
            "Discover the strongest truthful theme and make a concise, complete film."
        )
        tournament_for_prompt = {
            "concepts": list(tournament.get("concepts", [])),
            "selected_concept_id": selected_id,
            "selection_reason": str(tournament.get("selection_reason") or ""),
        }
        coverage_prompt_view = self._compact_asset_coverage_for_prompt(
            asset_coverage or []
        )

        def treatment_prompt(
            prompt_evidence: Sequence[Dict[str, Any]], evidence_label: str
        ) -> str:
            return (
                "Turn the winning evidence-backed concept into one executable director treatment. "
                "This happens AFTER neutral full-footage review; never replace literal evidence with "
                "genre assumptions. Use only candidate timestamps from the supplied evidence for "
                "story_anchors. The opening must promise the actual film, development must change what "
                "the viewer understands or feels, payoff must be visibly earned, and ending must be an "
                "observed complete state rather than a title pretending to create closure. Typography "
                "may heighten a real idea but cannot explain incoherent footage. Decide whether set talk "
                "is story, texture, comedy, or distraction; no automatic muting rule. Design music and "
                "one coherent color bible around the chosen emotional arc. Prefer instrumental score "
                "under dialogue. Treat duration as a ceiling, not padding. Return JSON only.\n"
                f"USER CREATIVE BRIEF: {brief}\n"
                f"REQUESTED TARGET DURATION: {self._active_target_duration_sec:.1f}s\n"
                f"CAMERA PROFILE: {self.camera_profile}\n"
                f"WINNING CONCEPT: {json.dumps(selected_concept, ensure_ascii=False, separators=(',', ':'))}\n"
                f"CONCEPT TOURNAMENT: {json.dumps(tournament_for_prompt, ensure_ascii=False, separators=(',', ':'))}\n"
                "ASSET COVERAGE SUMMARY (full audit remains in footage_ledger.json): "
                f"{json.dumps(coverage_prompt_view, ensure_ascii=False, separators=(',', ':'))}\n"
                f"SOURCE SUMMARIES: {json.dumps(compact_assets, ensure_ascii=False, separators=(',', ':'))}\n"
                f"{evidence_label}: {json.dumps(list(prompt_evidence), ensure_ascii=False, separators=(',', ':'))}"
            )

        prompt = treatment_prompt(evidence, "COMPLETE EVENT LEDGER")
        if not self._request_has_capacity(
            prompt, TREATMENT_SCHEMA, model=self.text_model, reserve_output_tokens=2048
        ):
            proof_ids = set(
                str(value) for value in selected_concept.get("proof_candidate_ids", [])
            )
            proof_ids.add(str(selected_concept.get("ending_candidate_id") or ""))
            selected_evidence = [
                item for item in evidence
                if str(item.get("candidate_id") or "") in proof_ids
            ]
            if not selected_evidence:
                raise DirectorError(
                    "优胜构想没有可用于导演阐述的真实证据 / "
                    "Winning concept has no usable evidence for treatment."
                )
            self.logger.info(
                "导演阐述使用优胜构想引用的 %d 个证据原子；全部原子此前已完成分层审阅 / "
                "Treatment uses %d winning-concept proof atoms after hierarchical all-atom review",
                len(selected_evidence), len(selected_evidence),
            )
            prompt = treatment_prompt(
                selected_evidence,
                "WINNING-CONCEPT PROOF LEDGER (all atoms were reviewed upstream)",
            )
            if not self._request_has_capacity(
                prompt,
                TREATMENT_SCHEMA,
                model=self.text_model,
                reserve_output_tokens=2048,
            ):
                raise DirectorError(
                    "优胜构想证据仍超过当前 Context；请提高 Context。"
                    " / Winning-concept evidence still exceeds Context; increase it."
                )
        self.logger.info(
            "正在生成优胜构想的导演阐述 / Creating treatment for the winning concept"
        )
        payload = self._request_json(
            prompt,
            TREATMENT_SCHEMA,
            model=self.text_model,
            progress_activity="director_treatment",
        )
        payload["concept_tournament"] = tournament
        payload["selected_concept_id"] = selected_id
        return payload

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
        treatment["viewer_takeaway"] = " ".join(
            str(payload.get("viewer_takeaway") or treatment["central_theme"]).split()
        )
        treatment["typography_intent"] = " ".join(
            str(
                payload.get("typography_intent")
                or "Use a concise opening title and only story-motivated chapter typography."
            ).split()
        )
        treatment["edit_style"] = self._enum_value(
            payload.get("edit_style"),
            {
                "narrative_documentary", "kinetic_montage", "atmospheric_poem",
                "dialogue_led", "hybrid_cinematic",
            },
            "hybrid_cinematic",
        )
        policy = str(payload.get("chronology_policy") or "").casefold()
        if policy not in {"strict_chronological", "teaser_then_chronological"}:
            policy = "strict_chronological"
        # Free direction keeps the director's structure. Explicit user wording wins.
        # 自由发挥时保留导演结构；用户明确要求时间流或预告开场时则强制遵守。
        brief_lower = self.creative_brief.casefold()
        strict_tokens = (
            "chronological", "time-flow", "time flow", "in order", "按时间",
            "时间流", "时间顺序", "顺叙",
        )
        teaser_tokens = ("teaser", "cold open", "预告", "先抛高潮", "倒叙开场")
        if any(token in brief_lower for token in strict_tokens):
            policy = "strict_chronological"
        elif any(token in brief_lower for token in teaser_tokens):
            policy = "teaser_then_chronological"
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
            if self._active_evidence_candidates and not any(
                str(item.get("asset_id") or "") == asset_id
                and min(cut_out, float(item.get("cut_out_sec", 0) or 0))
                - max(cut_in, float(item.get("cut_in_sec", 0) or 0))
                >= 0.5
                for item in self._active_evidence_candidates
            ):
                self.logger.warning(
                    "已拒绝没有视觉账本依据的导演锚点 %s %.3f-%.3f / "
                    "Rejected treatment anchor without ledger evidence",
                    asset_id, cut_in, cut_out,
                )
                continue
            anchors.append({
                "asset_id": asset_id,
                "cut_in_sec": round(cut_in, 3),
                "cut_out_sec": round(cut_out, 3),
                "beat": beat,
                "reason": reason,
            })
        if len(anchors) < 3 and self._active_evidence_candidates:
            tournament_payload = payload.get("concept_tournament")
            tournament_payload = (
                tournament_payload if isinstance(tournament_payload, dict) else {}
            )
            selected_id = str(
                payload.get("selected_concept_id")
                or tournament_payload.get("selected_concept_id")
                or ""
            )
            selected_concept = next(
                (
                    item for item in tournament_payload.get("concepts", [])
                    if isinstance(item, dict)
                    and str(item.get("concept_id") or "") == selected_id
                ),
                {},
            )
            by_candidate_id = {
                str(item.get("candidate_id") or ""): item
                for item in self._active_evidence_candidates
            }
            proof_ids = [
                str(value) for value in selected_concept.get("proof_candidate_ids", [])
                if str(value) in by_candidate_id
            ]
            if len(proof_ids) >= 3:
                generated: List[Dict[str, Any]] = []
                ending_id = str(selected_concept.get("ending_candidate_id") or "")
                for index, candidate_id in enumerate(proof_ids[:8]):
                    item = by_candidate_id[candidate_id]
                    if candidate_id == ending_id:
                        beat = "ending"
                    elif index == 0:
                        beat = "opening"
                    elif index >= len(proof_ids[:8]) - 2:
                        beat = "payoff"
                    else:
                        beat = "development"
                    generated.append({
                        "asset_id": str(item.get("asset_id") or ""),
                        "cut_in_sec": round(float(item.get("cut_in_sec", 0) or 0), 3),
                        "cut_out_sec": round(float(item.get("cut_out_sec", 0) or 0), 3),
                        "beat": beat,
                        "reason": self._compact_prompt_text(
                            item.get("reason_for_cut") or item.get("visual_summary"), 300
                        ),
                    })
                anchors = generated
                self.logger.warning(
                    "AI 导演锚点不足，已从优胜构想的真实证据补足 / "
                    "Treatment anchors were incomplete; restored winning-concept evidence"
                )
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
            explicit_endings = [
                index for index, anchor in enumerate(anchors)
                if anchor["beat"] == "ending"
            ]
            ending_index = explicit_endings[-1] if explicit_endings else latest_index
            for index in explicit_endings[:-1]:
                anchors[index]["beat"] = "development"
            anchors[ending_index]["beat"] = "ending"
            ending_asset = by_id[str(anchors[ending_index]["asset_id"])]
            ending_duration = float(ending_asset.get("duration_sec", 0))
            ending_anchor = anchors[ending_index]
            if float(ending_anchor["cut_out_sec"]) - float(ending_anchor["cut_in_sec"]) < 3.0:
                expanded_out = min(
                    ending_duration, float(ending_anchor["cut_in_sec"]) + 3.0
                )
                expanded_in = max(0.0, expanded_out - 3.0)
                ending_anchor["cut_in_sec"] = round(expanded_in, 3)
                ending_anchor["cut_out_sec"] = round(expanded_out, 3)
        treatment["story_anchors"] = anchors[:12]
        tournament = payload.get("concept_tournament")
        if isinstance(tournament, dict):
            treatment["concept_tournament"] = tournament
            treatment["selected_concept_id"] = str(
                payload.get("selected_concept_id")
                or tournament.get("selected_concept_id")
                or ""
            )
        if str(payload.get("footage_ledger") or "").strip():
            treatment["footage_ledger"] = str(payload["footage_ledger"])
        if str(payload.get("evidence_fingerprint") or "").strip():
            treatment["evidence_fingerprint"] = str(payload["evidence_fingerprint"])
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
        """Convert treatment hypotheses into unprotected fallback candidates. / 将导演假设转换为不受保护的回退候选。"""
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
                "protected_story_anchor": False,
                "candidate_origin": "treatment_fallback",
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
            and not bool(
                (item.get("vocal_audit") or {}).get("vocal_detected")
                if isinstance(item.get("vocal_audit"), dict)
                else False
            )
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

            def cue_times(field_name: str) -> List[float]:
                """Keep only analyzed events inside this cue. / 仅保留 cue 内的已分析事件。"""
                return [
                    round(float(event), 4)
                    for event in analyzed.get(field_name, [])
                    if isinstance(event, (int, float))
                    and track_in <= float(event) <= track_out
                ]

            cue_beats = cue_times("beats_sec")
            cue_strong_beats = cue_times("strong_beats_sec")
            cue_downbeats = cue_times("downbeats_sec")
            cue_sections = [
                dict(section)
                for section in analyzed.get("sections", [])
                if isinstance(section, dict)
                and float(section.get("end_sec", 0) or 0) >= track_in
                and float(section.get("start_sec", 0) or 0) <= track_out
            ]
            section_landmarks = sorted(
                {
                    round(float(value), 4)
                    for section in cue_sections
                    for value in (
                        section.get("start_sec", 0),
                        section.get("end_sec", 0),
                    )
                    if isinstance(value, (int, float))
                    and track_in <= float(value) <= track_out
                }
            )
            sync_points: List[Dict[str, Any]] = []
            for point in raw.get("sync_points", []) if isinstance(raw.get("sync_points"), list) else []:
                if not isinstance(point, dict):
                    continue
                point_type = self._enum_value(
                    point.get("type"),
                    {"beat", "strong_beat", "downbeat", "section", "energy_peak"},
                    "strong_beat",
                )
                landmarks = {
                    "beat": cue_beats,
                    "strong_beat": cue_strong_beats or cue_downbeats,
                    "downbeat": cue_downbeats,
                    "section": section_landmarks,
                    "energy_peak": cue_strong_beats or cue_downbeats,
                }[point_type]
                if not landmarks:
                    self.logger.warning(
                        "AI 卡点没有对应的实测音乐地标，已忽略：%s / "
                        "Ignoring ungrounded sync point",
                        point_type,
                    )
                    continue
                requested_track = number(
                    point.get("track_sec"), track_in, track_in, track_out
                )
                point_track = min(landmarks, key=lambda value: abs(value - requested_track))
                point_timeline = timeline_in + point_track - track_in
                sync_points.append({
                    "timeline_sec": round(point_timeline, 4),
                    "track_sec": round(point_track, 4),
                    "type": point_type,
                    "purpose": " ".join(str(point.get("purpose") or "Musical sync").split()),
                })
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
                "target_lufs": round(number(raw.get("target_lufs"), -17, -20, -14), 2),
                "fade_in_sec": round(number(raw.get("fade_in_sec"), 1.5, 0, min(12, usable / 2)), 3),
                "fade_out_sec": round(number(raw.get("fade_out_sec"), 2.0, 0, min(12, usable / 2)), 3),
                "crossfade_sec": round(number(raw.get("crossfade_sec"), 1.5, 0, min(8, usable / 2)), 3),
                "duck_under_dialogue_db": round(number(raw.get("duck_under_dialogue_db"), -9, -24, 0), 2),
                "sync_points": sync_points,
                "tempo_bpm": float(analyzed.get("tempo_bpm", 0) or 0),
                "key": str(analyzed.get("key") or ""),
                "mode": str(analyzed.get("mode") or ""),
                "integrated_lufs": analyzed.get("integrated_lufs"),
                "beats_sec": cue_beats,
                "strong_beats_sec": cue_strong_beats,
                "downbeats_sec": cue_downbeats,
                "sections": cue_sections,
                "energy_profile": dict(analyzed.get("energy_profile") or {}),
                "director_match": dict(analyzed.get("director_match") or {}),
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

    @staticmethod
    def music_plan_quality_violations(
        music_plan: Dict[str, Any], treatment: Dict[str, Any]
    ) -> List[str]:
        """Detect cue claims contradicted by measured energy. / 检测与实测能量矛盾的 cue。"""
        violations: List[str] = []
        cues = [item for item in music_plan.get("cues", []) if isinstance(item, dict)]
        desired_arc = " ".join(str(treatment.get("music_energy_arc") or "").split()).casefold()
        build_terms = {
            "build", "rise", "rising", "crescendo", "swell", "peak", "climax",
            "上升", "渐强", "推进", "高潮", "递进",
        }
        treatment_requires_build = any(term in desired_arc for term in build_terms)
        for cue in cues:
            profile = cue.get("energy_profile")
            if not isinstance(profile, dict) or not profile:
                continue
            trend = str(profile.get("trend") or "unknown").casefold()
            try:
                build_score = float(profile.get("build_score", 0) or 0)
                contrast = float(profile.get("contrast_db", 0) or 0)
            except (TypeError, ValueError):
                build_score, contrast = 0.0, 0.0
            reason = str(cue.get("reason") or "").casefold()
            claims_build = any(term in reason for term in build_terms)
            payoff = str(cue.get("story_beat") or "") == "payoff"
            sections = cue.get("sections")
            section_labels = {
                str(item.get("energy") or "").casefold()
                for item in sections if isinstance(item, dict)
            } if isinstance(sections, list) else set()
            flat_low = bool(section_labels) and section_labels <= {"low"}
            measured_build = trend == "rising" or build_score >= 0.45 or contrast >= 4.0
            if (claims_build or (payoff and treatment_requires_build)) and not measured_build:
                violations.append(
                    f"Cue {cue.get('cue_id', '?')} claims a build/payoff, but measured "
                    f"energy is trend={trend}, build_score={build_score:.2f}, contrast={contrast:.1f} dB."
                )
            if payoff and flat_low:
                violations.append(
                    f"Cue {cue.get('cue_id', '?')} assigns an all-low-energy section to payoff."
                )
        for previous, current in zip(cues, cues[1:]):
            same_track = str(previous.get("file_name") or "").casefold() == str(
                current.get("file_name") or ""
            ).casefold()
            try:
                gap = float(current.get("timeline_in_sec", 0) or 0) - float(
                    previous.get("timeline_out_sec", 0) or 0
                )
            except (TypeError, ValueError):
                gap = 999.0
            if same_track and abs(gap) <= 0.25:
                violations.append(
                    "The same track was split into adjacent cues without an audible change; "
                    "use one continuous cue or select a genuinely different musical section."
                )
        return violations

    def _request_quality_gated_music_plan(
        self,
        music_prompt: str,
        locked_duration: float,
        treatment: Dict[str, Any],
    ) -> Dict[str, Any]:
        """
        Request music at most twice and fail closed on measured mismatch.
        配乐最多请求两次；若仍与实测能量不符则关闭式失败。

        An explicitly authored empty cue list is valid intentional silence.
        导演明确返回空 cue 表示有意不用音乐，属于合法方案。
        """
        music_request = music_prompt
        for music_revision in range(2):
            music_payload = self._request_json(
                music_request,
                MUSIC_PLAN_SCHEMA,
                model=self.text_model,
                progress_activity="music_spotting",
            )
            normalized_music = self.validate_music_plan(
                music_payload.get("music_plan"), locked_duration
            )
            violations = self.music_plan_quality_violations(
                normalized_music, treatment
            )
            if not violations:
                return normalized_music
            if music_revision >= 1:
                raise DirectorError(
                    "配乐导演两次选择均违反实测能量约束，质量优先模式拒绝输出已知"
                    "不匹配的音乐床：" + "；".join(violations)
                    + " / Music directing failed measured energy constraints twice; "
                    "quality-first mode refuses a known-mismatched music bed: "
                    + "; ".join(violations)
                )
            self.logger.warning(
                "Music cue sheet contradicted measured audio energy; returning it "
                "to the director for one grounded reselection: %s",
                "; ".join(violations),
            )
            music_request = (
                music_prompt + "\nREJECTED MUSIC PLAN:\n"
                + json.dumps(normalized_music, ensure_ascii=False, separators=(",", ":"))
                + "\nMEASURED MUSIC FAILURES:\n"
                + json.dumps(violations, ensure_ascii=False)
                + "\nReturn a materially different, measurement-grounded music_plan."
            )
        raise DirectorError("Unreachable music quality-gate state.")

    def enforce_dialogue_ducking(
        self,
        clips: Sequence[Dict[str, Any]],
        music_plan: Dict[str, Any],
        minimum_duck_db: float = -10.0,
    ) -> Dict[str, Any]:
        """
        Enforce music attenuation wherever a cue overlaps spoken source audio.
        当配乐 cue 与原素材对白重叠时，强制执行足够的自动压低。

        Parameters / 参数:
            clips: Ordered picture clips carrying ``has_dialogue``. / 含对白标记的镜头序列。
            music_plan: Validated cue sheet. / 已校验配乐 cue 表。
            minimum_duck_db: Loudest allowed gain during dialogue. / 对白期间允许的最高配乐增益。
        """
        plan = dict(music_plan)
        dialogue_ranges: List[tuple[float, float]] = []
        cursor = 0.0
        for clip in clips:
            duration = max(
                0.0,
                float(clip.get("cut_out_sec", 0) or 0)
                - float(clip.get("cut_in_sec", 0) or 0),
            )
            source_in = float(clip.get("cut_in_sec", 0) or 0)
            audio_intent = str(clip.get("audio_intent") or "").casefold()
            exact_ranges = clip.get("dialogue_ranges_sec")
            added_exact = False
            if audio_intent != "mute_for_music" and isinstance(exact_ranges, list):
                for raw_range in exact_ranges:
                    if not isinstance(raw_range, dict):
                        continue
                    start = max(
                        source_in,
                        float(raw_range.get("start_sec", source_in) or source_in),
                    )
                    end = min(
                        source_in + duration,
                        float(raw_range.get("end_sec", source_in) or source_in),
                    )
                    if end - start < 0.05:
                        continue
                    dialogue_ranges.append(
                        (cursor + start - source_in, cursor + end - source_in)
                    )
                    added_exact = True
            if (
                not added_exact
                and audio_intent != "mute_for_music"
                and bool(clip.get("has_dialogue"))
                and (
                    exact_ranges is None
                    or str(clip.get("story_role") or "").casefold() == "interview"
                )
            ):
                dialogue_ranges.append((cursor, cursor + duration))
            cursor += duration
        cues: List[Dict[str, Any]] = []
        for raw in plan.get("cues", []) if isinstance(plan.get("cues"), list) else []:
            if not isinstance(raw, dict):
                continue
            cue = dict(raw)
            cue_in = float(cue.get("timeline_in_sec", 0) or 0)
            cue_out = float(cue.get("timeline_out_sec", 0) or 0)
            if any(min(cue_out, end) - max(cue_in, start) > 0.05 for start, end in dialogue_ranges):
                cue["duck_under_dialogue_db"] = round(
                    min(
                        float(cue.get("duck_under_dialogue_db", minimum_duck_db) or 0),
                        float(minimum_duck_db),
                    ),
                    2,
                )
            cues.append(cue)
        plan["cues"] = cues
        plan["dialogue_regions"] = [
            {"timeline_in_sec": round(start, 3), "timeline_out_sec": round(end, 3)}
            for start, end in dialogue_ranges
        ]
        return plan

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
        absolute_beats = sorted(
            {value for value in absolute_beats if math.isfinite(value)}
        )
        absolute_priority = sorted(
            {value for value in absolute_priority if math.isfinite(value)}
        )
        if not absolute_beats and not absolute_priority:
            return [dict(item) for item in clips]
        asset_duration = {
            str(asset.get("asset_id") or ""): self._authoritative_asset_duration(asset)
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
            has_dialogue = bool(item.get("has_dialogue")) or str(
                item.get("story_role") or ""
            ).casefold() == "interview" or any(
                min(source_out, float(segment.get("end_sec", 0)))
                - max(source_in, float(segment.get("start_sec", 0))) >= 0.15
                for segment in transcript_by_asset.get(str(item.get("asset_id") or ""), [])
            )
            music_role = str(item.get("music_edit_role") or "on_beat").casefold()
            if music_role in {"phrase_start", "payoff_hit", "release"}:
                landmarks = absolute_priority or absolute_beats
            else:
                landmarks = absolute_beats or absolute_priority
            if not has_dialogue and music_role != "natural_sound" and landmarks:
                nearest = min(landmarks, key=lambda beat: abs(beat - proposed_end))
                shift = nearest - proposed_end
                source_end = float(item.get("cut_out_sec", 0)) + shift
                reviewed_bounds = item.get("reviewed_trim_bounds")
                try:
                    reviewed_out = float(
                        reviewed_bounds.get("out_sec")
                        if isinstance(reviewed_bounds, dict)
                        else source_out
                    )
                except (TypeError, ValueError):
                    reviewed_out = source_out
                if not math.isfinite(reviewed_out):
                    reviewed_out = source_out
                # Missing reviewed bounds are fail-closed: an old plan may be
                # shortened to a beat, but it may never invent unseen handles.
                # 缺少已审句柄时采用保守策略：可缩短，不得向未审画面延长。
                maximum = max(source_in, reviewed_out)
                source_duration = asset_duration.get(
                    str(item.get("asset_id") or ""), 0.0
                )
                if source_duration > 0:
                    maximum = min(maximum, source_duration)
                try:
                    action_apex = float(item.get("action_apex_sec", source_in))
                except (TypeError, ValueError):
                    action_apex = source_in
                if not math.isfinite(action_apex):
                    action_apex = source_in
                minimum_out = max(source_in + 0.4, action_apex)
                extension_has_dialogue = source_end > source_out and any(
                    min(source_end, float(segment.get("end_sec", 0)))
                    - max(source_out, float(segment.get("start_sec", 0))) > 1e-6
                    for segment in transcript_by_asset.get(
                        str(item.get("asset_id") or ""), []
                    )
                )
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
                    and source_end >= minimum_out - 1e-6
                    and source_end > source_in
                    and not extension_has_dialogue
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
        picture_boundaries: List[float] = [0.0]
        cursor = 0.0
        for clip in clips[:-1]:
            cursor += max(
                0.0,
                float(clip.get("cut_out_sec", 0)) - float(clip.get("cut_in_sec", 0)),
            )
            picture_boundaries.append(cursor)
            role = str(clip.get("music_edit_role") or "on_beat").casefold()
            if role != "natural_sound":
                boundaries.append((cursor, role))
        if clips:
            cursor += max(
                0.0,
                float(clips[-1].get("cut_out_sec", 0))
                - float(clips[-1].get("cut_in_sec", 0)),
            )
            picture_boundaries.append(cursor)
        aligned: List[Dict[str, Any]] = []
        for cue in cues:
            timeline_in = float(cue.get("timeline_in_sec", 0) or 0)
            timeline_out = float(cue.get("timeline_out_sec", 0) or 0)
            track_in = float(cue.get("track_in_sec", 0) or 0)
            track_out = float(cue.get("track_out_sec", 0) or 0)
            cue_landmarks = picture_boundaries + [timeline_in, timeline_out]
            existing = []
            for point in cue.get("sync_points", []):
                if not isinstance(point, dict):
                    continue
                try:
                    point_time = float(point.get("timeline_sec", -999))
                except (TypeError, ValueError):
                    continue
                if any(abs(point_time - boundary) <= 0.8 for boundary in cue_landmarks):
                    existing.append(dict(point))
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

    @staticmethod
    def _require_exact_picture_page(
        payload: Dict[str, Any],
        expected_ids: Sequence[str],
        *,
        page_index: int,
        stage: str,
    ) -> List[Dict[str, Any]]:
        """
        Require one staged-output page to preserve every requested shot in order.
        要求分阶段输出页逐一、按顺序保留全部指定镜头。
        """
        raw_shots = payload.get("shots")
        shots = [item for item in raw_shots if isinstance(item, dict)] \
            if isinstance(raw_shots, list) else []
        returned_ids = [str(item.get("candidate_id") or "").strip() for item in shots]
        wanted_ids = [str(value) for value in expected_ids]
        if returned_ids != wanted_ids:
            raise DirectorError(
                f"{stage} 第 {page_index} 页镜头 ID 不完整或顺序改变："
                f"expected={wanted_ids}, returned={returned_ids}。禁止静默补齐或丢镜头。 / "
                f"{stage} page {page_index} changed or lost shot IDs. Silent filling or "
                "dropping is forbidden."
            )
        try:
            returned_page_index = int(payload.get("page_index", page_index))
        except (TypeError, ValueError):
            returned_page_index = -1
        if returned_page_index != page_index:
            raise DirectorError(
                f"{stage} 返回错误 page_index={returned_page_index}，应为 {page_index}。 / "
                f"{stage} returned the wrong page index; expected {page_index}."
            )
        return shots

    @staticmethod
    def _picture_candidate_bounds(candidate: Dict[str, Any]) -> tuple[float, float]:
        """Read bounds from compact or canonical candidate data. / 从紧凑或标准候选读取边界。"""
        try:
            start = float(candidate.get("in", candidate.get("cut_in_sec", 0)) or 0)
            end = float(candidate.get("out", candidate.get("cut_out_sec", start)) or start)
        except (TypeError, ValueError):
            return 0.0, 0.0
        return start, end

    def _request_staged_picture_plan(
        self,
        base_prompt: str,
        candidates: Sequence[Dict[str, Any]],
        *,
        include_review: bool,
        progress_activity: str,
    ) -> Dict[str, Any]:
        """
        Author a long picture lock without one unbounded verbose JSON response.
        通过紧凑全局顺序与严格分页生成长片锁画，避免一次返回无限长 JSON。

        Parameters / 参数:
            base_prompt: Global editorial evidence and instructions. / 全局剪辑证据与指令。
            candidates: Context-safe candidates eligible for selection. / 可选的上下文安全候选。
            include_review: Require supervising review scores and notes. / 是否要求总剪辑复审。
            progress_activity: Existing UI progress activity label. / 现有 UI 进度活动标签。
        """
        candidate_map = {
            str(item.get("candidate_id") or "").strip(): item
            for item in candidates
            if isinstance(item, dict) and str(item.get("candidate_id") or "").strip()
        }
        if not candidate_map:
            raise DirectorError(
                "分阶段锁画没有可用候选 / Staged picture lock has no candidates."
            )
        order_schema = (
            PICTURE_ORDER_REVIEW_SCHEMA if include_review else PICTURE_ORDER_SCHEMA
        )
        order_prompt = (
            base_prompt
            + "\n\nPICTURE ORDER MANIFEST — BOUNDED OUTPUT PROTOCOL. "
            "This instruction replaces any earlier wording that asks for a complete verbose "
            "sequence. Decide the global film now, but return only project_summary, "
            "viewer_takeaway, editorial_style, and ordered_candidate_ids. "
            "The array is the immutable complete picture order: include every selected shot "
            "exactly once and no unselected IDs. Do not return trims, annotations, effects, "
            "graphics, or a sequence object in this response. Later bounded pages will author "
            "those details without changing this order. "
            + (
                "Also return the required supervising review for this revised order. "
                if include_review else ""
            )
            + f"Select no more than {PICTURE_MAX_SHOTS} shots. Return JSON only."
        )
        order_payload = self._request_json(
            order_prompt,
            order_schema,
            model=self.text_model,
            progress_activity=progress_activity,
        )
        # Compatibility for older tests/adapters that return a complete parsed
        # plan despite the new schema. A truncated response never reaches here
        # because it is not parseable JSON. Real schema-constrained calls use
        # ``ordered_candidate_ids`` and therefore always take the paged path.
        # 兼容仍按旧协议返回完整且可解析方案的测试/适配器；真实 Schema 请求始终走分页。
        legacy_sequence = order_payload.get("sequence")
        if (
            "ordered_candidate_ids" not in order_payload
            and isinstance(legacy_sequence, list)
            and legacy_sequence
        ):
            order_payload["_staged_output_audit"] = {
                "protocol": "legacy-complete-compatible",
                "selected_shot_count": len(legacy_sequence),
                "full_verbose_sequence_requests": 1,
            }
            return order_payload

        raw_order = order_payload.get("ordered_candidate_ids")
        ordered_ids = [str(value).strip() for value in raw_order] \
            if isinstance(raw_order, list) else []
        if not ordered_ids:
            raise DirectorError(
                "锁画顺序清单为空 / Picture-order manifest is empty."
            )
        if len(ordered_ids) > PICTURE_MAX_SHOTS:
            raise DirectorError(
                f"锁画顺序超过协议上限 {PICTURE_MAX_SHOTS} / Picture order exceeds "
                f"the bounded protocol limit of {PICTURE_MAX_SHOTS}."
            )
        if len(set(ordered_ids)) != len(ordered_ids):
            raise DirectorError(
                "锁画顺序包含重复 candidate_id / Picture order contains duplicate IDs."
            )
        unknown_ids = [candidate_id for candidate_id in ordered_ids if candidate_id not in candidate_map]
        if unknown_ids:
            raise DirectorError(
                f"锁画顺序引用未知候选：{unknown_ids} / Picture order references unknown candidates."
            )

        global_direction = {
            "project_summary": order_payload.get("project_summary", ""),
            "viewer_takeaway": order_payload.get("viewer_takeaway", ""),
            "editorial_style": order_payload.get("editorial_style", "hybrid_cinematic"),
            "ordered_candidate_ids": ordered_ids,
        }
        skeleton: List[Dict[str, Any]] = []
        skeleton_pages = 0
        for offset in range(0, len(ordered_ids), PICTURE_SKELETON_PAGE_SIZE):
            skeleton_pages += 1
            page_ids = ordered_ids[offset:offset + PICTURE_SKELETON_PAGE_SIZE]
            page_candidates = [candidate_map[candidate_id] for candidate_id in page_ids]
            previous_id = ordered_ids[offset - 1] if offset else ""
            next_offset = offset + len(page_ids)
            next_id = ordered_ids[next_offset] if next_offset < len(ordered_ids) else ""
            page_prompt = (
                f"PICTURE SKELETON PAGE {skeleton_pages}. The global order is immutable. "
                "Return exactly one shots item for every REQUIRED ID, in exactly that order. "
                "Do not add, remove, or reorder IDs. Choose exact source-bounded trim_in_sec and "
                "trim_out_sec plus narrative_function, audio_intent, and music_edit_role. "
                "Preserve complete meaningful speech; visual shots should enter just before and "
                "leave just after their useful action. Return JSON only.\n"
                f"GLOBAL DIRECTION:\n{json.dumps(global_direction, ensure_ascii=False, separators=(',', ':'))}\n"
                f"PREVIOUS NEIGHBOR ID: {previous_id or '(opening)'}\n"
                f"NEXT NEIGHBOR ID: {next_id or '(ending)'}\n"
                f"REQUIRED IDS:\n{json.dumps(page_ids, ensure_ascii=False)}\n"
                f"CANDIDATE EVIDENCE:\n{json.dumps(page_candidates, ensure_ascii=False, separators=(',', ':'))}"
            )
            page_payload = self._request_json(
                page_prompt,
                PICTURE_SKELETON_PAGE_SCHEMA,
                model=self.text_model,
                progress_activity=progress_activity,
            )
            page_shots = self._require_exact_picture_page(
                page_payload,
                page_ids,
                page_index=skeleton_pages,
                stage="picture skeleton",
            )
            for shot in page_shots:
                candidate_id = str(shot["candidate_id"])
                candidate_in, candidate_out = self._picture_candidate_bounds(
                    candidate_map[candidate_id]
                )
                try:
                    trim_in = float(shot.get("trim_in_sec"))
                    trim_out = float(shot.get("trim_out_sec"))
                except (TypeError, ValueError) as exc:
                    raise DirectorError(
                        f"镜头 {candidate_id} 的剪点不是数字 / Non-numeric trim."
                    ) from exc
                if trim_in < candidate_in - 0.001 or trim_out > candidate_out + 0.001 or trim_out <= trim_in:
                    raise DirectorError(
                        f"镜头 {candidate_id} 剪点越界：{trim_in}-{trim_out}，"
                        f"候选范围 {candidate_in}-{candidate_out} / Staged trim is out of bounds."
                    )
                skeleton.append(dict(shot))

        skeleton_by_id = {
            str(item["candidate_id"]): item for item in skeleton
        }
        enrichment_by_id: Dict[str, Dict[str, Any]] = {}
        enrichment_pages = 0
        for offset in range(0, len(ordered_ids), PICTURE_ENRICHMENT_PAGE_SIZE):
            enrichment_pages += 1
            page_ids = ordered_ids[offset:offset + PICTURE_ENRICHMENT_PAGE_SIZE]
            page_skeleton = [skeleton_by_id[candidate_id] for candidate_id in page_ids]
            page_candidates = [candidate_map[candidate_id] for candidate_id in page_ids]
            neighbor_start = max(0, offset - 1)
            neighbor_end = min(len(ordered_ids), offset + len(page_ids) + 1)
            neighbor_skeleton = [
                skeleton_by_id[candidate_id]
                for candidate_id in ordered_ids[neighbor_start:neighbor_end]
            ]
            page_prompt = (
                f"PICTURE ENRICHMENT PAGE {enrichment_pages}. The global order, IDs, trims, "
                "narrative functions, audio intents, and music roles are already locked. "
                "Return exactly one shots item per REQUIRED ID in exactly that order. Do not "
                "change or omit a shot. Add concise literal viewer information, position reason, "
                "evidence claim, connection to previous, and executable technical choices. "
                "Evidence claims must remain visible/audible facts. Use a hard cut unless an "
                "explicit supported chapter boundary requires otherwise. Return JSON only.\n"
                f"GLOBAL DIRECTION:\n{json.dumps(global_direction, ensure_ascii=False, separators=(',', ':'))}\n"
                f"REQUIRED IDS:\n{json.dumps(page_ids, ensure_ascii=False)}\n"
                f"LOCKED PAGE SKELETON:\n{json.dumps(page_skeleton, ensure_ascii=False, separators=(',', ':'))}\n"
                f"NEIGHBOR CONTEXT:\n{json.dumps(neighbor_skeleton, ensure_ascii=False, separators=(',', ':'))}\n"
                f"CANDIDATE EVIDENCE:\n{json.dumps(page_candidates, ensure_ascii=False, separators=(',', ':'))}"
            )
            page_payload = self._request_json(
                page_prompt,
                PICTURE_ENRICHMENT_PAGE_SCHEMA,
                model=self.text_model,
                progress_activity=progress_activity,
            )
            page_shots = self._require_exact_picture_page(
                page_payload,
                page_ids,
                page_index=enrichment_pages,
                stage="picture enrichment",
            )
            enrichment_by_id.update(
                (str(item["candidate_id"]), dict(item)) for item in page_shots
            )

        sequence: List[Dict[str, Any]] = []
        for candidate_id in ordered_ids:
            merged = dict(skeleton_by_id[candidate_id])
            merged.update(enrichment_by_id[candidate_id])
            sequence.append(merged)
        if [str(item.get("candidate_id") or "") for item in sequence] != ordered_ids:
            raise DirectorError(
                "分页锁画合并后 ID 不一致 / Staged picture merge changed shot IDs."
            )

        graphics_evidence = [
            {
                "candidate_id": candidate_id,
                "narrative_function": skeleton_by_id[candidate_id].get("narrative_function"),
                "visual_summary": self._compact_prompt_text(
                    candidate_map[candidate_id].get("visual_summary", ""), 80
                ),
            }
            for candidate_id in ordered_ids
        ]
        graphics_prompt = (
            "PICTURE GRAPHICS PASS. Picture order and trims are locked. Design at most six "
            "executable graphics using only selected candidate IDs as anchors. Prefer zero to "
            "two graphics for a sub-two-minute film; never use text to invent a missing event. "
            "Return graphics_plan JSON only.\n"
            f"GLOBAL DIRECTION:\n{json.dumps(global_direction, ensure_ascii=False, separators=(',', ':'))}\n"
            f"SELECTED SHOTS:\n{json.dumps(graphics_evidence, ensure_ascii=False, separators=(',', ':'))}"
        )
        graphics_payload = self._request_json(
            graphics_prompt,
            PICTURE_GRAPHICS_SCHEMA,
            model=self.text_model,
            progress_activity=progress_activity,
        )
        result: Dict[str, Any] = {
            "project_summary": global_direction["project_summary"],
            "viewer_takeaway": global_direction["viewer_takeaway"],
            "editorial_style": global_direction["editorial_style"],
            "graphics_plan": graphics_payload.get(
                "graphics_plan", {"strategy": "No graphics.", "items": []}
            ),
            "sequence": sequence,
            "_staged_output_audit": {
                "protocol": "ordered-manifest+paged-skeleton+paged-enrichment+graphics-v1",
                "selected_shot_count": len(sequence),
                "skeleton_page_size": PICTURE_SKELETON_PAGE_SIZE,
                "enrichment_page_size": PICTURE_ENRICHMENT_PAGE_SIZE,
                "skeleton_pages": skeleton_pages,
                "enrichment_pages": enrichment_pages,
                "full_verbose_sequence_requests": 0,
                "max_verbose_shots_in_any_request": PICTURE_ENRICHMENT_PAGE_SIZE,
                "output_request_count": 2 + skeleton_pages + enrichment_pages,
            },
        }
        if include_review:
            result["review"] = order_payload.get("review", {})
        self.logger.info(
            "长片锁画分页完成：%d 镜头，骨架 %d 页，富化 %d 页，每次最多 %d 个富文本镜头 / "
            "Paged picture lock complete: %d shots, %d skeleton pages, %d enrichment pages, "
            "at most %d verbose shots per output request",
            len(sequence), skeleton_pages, enrichment_pages,
            PICTURE_ENRICHMENT_PAGE_SIZE,
            len(sequence), skeleton_pages, enrichment_pages,
            PICTURE_ENRICHMENT_PAGE_SIZE,
        )
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
            speech_intervals = self._candidate_speech_intervals(item)
            silent_intervals = self._candidate_silent_intervals(
                item, speech_intervals
            )
            try:
                candidate_duration = max(
                    0.0,
                    float(item.get("cut_out_sec", 0) or 0)
                    - float(item.get("cut_in_sec", 0) or 0),
                )
            except (TypeError, ValueError):
                candidate_duration = 0.0
            speech_duration = (
                sum(end - start for start, end in speech_intervals)
                if speech_intervals is not None
                else candidate_duration if bool(item.get("has_dialogue")) else 0.0
            )
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
                        item.get("visual_summary", ""), 160
                    ),
                    "subject_action": self._compact_prompt_text(
                        item.get("subject_action", ""), 100
                    ),
                    "emotion": self._compact_prompt_text(item.get("emotion", ""), 60),
                    "action_phase": item.get("action_phase", "action"),
                    "shot_scale": item.get("shot_scale", "medium"),
                    "camera_motion": item.get("camera_motion", "static"),
                    "rhythmic_potential": item.get("rhythmic_potential", 0.5),
                    "has_dialogue": bool(item.get("has_dialogue", False)),
                    # Exact merged timing lets the model trim a visual action
                    # outside speech instead of guessing from has_dialogue.
                    # 精确合并时间让导演能保留画面动作而不误留现场谈话。
                    "speech_ranges": [
                        [round(start, 3), round(end, 3)]
                        for start, end in (speech_intervals or [])
                    ],
                    "silent_ranges": [
                        [round(start, 3), round(end, 3)]
                        for start, end in silent_intervals
                    ],
                    "speech_seconds_if_full_range": round(speech_duration, 3),
                    "speech_occupancy_ratio": round(
                        speech_duration / candidate_duration, 3
                    ) if candidate_duration else 0.0,
                    "dialogue_excerpt": self._compact_prompt_text(
                        item.get("dialogue_excerpt", ""), 260
                    ),
                    "production_context_hint": bool(
                        item.get("production_context_hint", False)
                    ),
                    "confidence": item.get("confidence", 0.5),
                    "quality_score": item.get("quality_score", 0.5),
                    "temporal_review": (
                        {
                            "status": str(item["temporal_refinement"].get("status") or ""),
                            "advisory": self._compact_prompt_text(
                                item["temporal_refinement"].get("decision_reason"), 180
                            ),
                        }
                        if isinstance(item.get("temporal_refinement"), dict)
                        else {}
                    ),
                }
            )
            if speech_intervals is None:
                # Keep legacy/test ledgers as compact as before. Exact audio
                # fields are valuable only when the extractor actually supplied
                # timestamps; empty placeholders needlessly consume context.
                for key in (
                    "speech_ranges",
                    "silent_ranges",
                    "speech_seconds_if_full_range",
                    "speech_occupancy_ratio",
                ):
                    compact_candidates[-1].pop(key, None)
        asset_names = [
            {
                "source_order": source_order,
                "asset_id": asset.get("asset_id", ""),
                "file": Path(str(asset.get("source_video") or "")).name,
                "duration_sec": self._authoritative_asset_duration(asset),
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
            and not bool(
                (item.get("vocal_audit") or {}).get("vocal_detected")
                if isinstance(item.get("vocal_audit"), dict)
                else False
            )
        ] or [{"track_file": path.name} for path in self._music_files]
        tempo_matches = [
            item for item in music_choices
            if bool(item.get("director_match", {}).get("tempo_in_range"))
        ]
        if tempo_matches:
            music_choices = tempo_matches
        strong_music_matches = [
            item for item in music_choices
            if bool(item.get("director_match", {}).get("energy_arc_match", True))
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
            "saved 2-fps survey sample in chronological order, densely rewatched short "
            "event atoms, and carried state across "
            "overlapping transport batches. Synthesize the complete source summaries into "
            "one grounded project memory before selecting shots. Track people, locations, "
            "actions, reactions, cause/effect, recurring motifs, and unresolved intentions. "
            "Do not invent events. Audit the user's requested outcome against what the "
            "source summaries literally observe. Words such as preparing, ready, before, "
            "signals, implies, or a countdown do NOT prove that the next action happened. "
            "For example, a static lineup is not a departure and a rider leaning forward "
            "is not riding away. Record the last action actually visible in observed_ending, "
            "list requested but unsupported actions in absent_or_unproven_events, and use "
            "honest_adaptation to propose the strongest film the available evidence can "
            "truthfully support. The creative brief is an intention, not permission to "
            "hallucinate missing footage. You may refine its thesis when evidence requires. "
            "Return JSON only.\n"
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
            progress_activity="coverage_synthesis",
        )
        coverage_synopsis = self._normalize_coverage_synopsis(
            coverage_synopsis, compact_treatment
        )
        narrative_contract = self._request_narrative_contract(
            compact_candidates, coverage_synopsis, compact_treatment
        )
        def build_sequence_prompt(
            active_candidates: Sequence[Dict[str, Any]],
        ) -> str:
            return (
                "PICTURE ASSEMBLY STEP 1/2. You have already inspected every saved 2-fps survey frame, "
                "densely rewatched short event atoms, and read transcripts "
                "from every source video in continuous order. The FULL COVERAGE SYNOPSIS was synthesized "
                "from complete chronological evidence; use it to understand "
                "the whole action and intention, not only the editable candidates. Build one "
                "coherent documentary edit from "
                "the candidate list below. Select only useful candidate_id values, "
                "never invent or duplicate an id. Follow the treatment. Preserve the "
                "real source_order and in-file timestamp chronology; narrative quality "
                "must come from selection and juxtaposition, not scrambling the shoot. "
                "The EVIDENCE-FIRST NARRATIVE CONTRACT is the factual and structural source "
                "of truth. The earlier treatment is only a creative hypothesis; where they "
                "conflict, obey the contract. First decide exactly what the viewer should "
                "understand in one sentence, then choose an editorial_style and make every "
                "shot prove that sentence. "
                "Treat absent_or_unproven_events as a hard factual boundary. Never describe "
                "a departure, ride, reaction, ignition, or other event unless a candidate's "
                "visual_summary or dialogue explicitly observes it. Readiness is not action. "
                "If the requested ending was not filmed, reframe the film around the strongest "
                "honest ending (ritual, portrait, anticipation, friendship, or another observed "
                "idea) instead of pretending the missing event occurred. "
                "Establish context, develop the story, "
                "use B-roll to cover or bridge speech, avoid repetitive points, and "
                "finish deliberately. Candidate production_context_hint is advisory "
                "evidence, not a command. You alone decide whether production dialogue "
                "serves this film. For every selected shot explicitly choose preserve_dialogue, "
                "natural_texture, mix_with_music, or mute_for_music and justify that choice "
                "through narrative_function and reason_for_position. "
                "For each selected id, trim_in_sec and trim_out_sec must stay inside its "
                "candidate range and isolate only the exact useful action or complete thought. "
                "Kinetic montage shots should usually be 0.8-4 seconds; dialogue-led thoughts "
                "may be longer only when every second is meaningful. Preserve complete thoughts. "
                "Use normal human cutting grammar: enter just before the useful action or line, "
                "leave immediately after it, and cut on action, reaction, gaze, or sound. Adjacent "
                "shots should change at least one major visual property such as subject, shot scale, "
                "camera movement, action, or emotional pressure. Do not repeat static wide lineups "
                "when moving, medium, close, reaction, or detail candidates can advance the idea. "
                "Every selected shot must state narrative_function, viewer_information, a "
                "literal evidence_claim, connection_to_previous, and reason_for_position. "
                "evidence_claim must describe only visible or audible facts in that candidate; "
                "connection_to_previous must explain the editorial progression, not repeat mood words. "
                "Design at most six executable graphics tied to selected candidate ids. Use a title, "
                "chapter words, lower thirds, or an end card only when they clarify the premise, "
                "structure, identity, or payoff; typography may be bold and stylish but cannot name "
                "an event that the selected picture never shows. "
                "Use hard cuts for this automated deliverable: they are the only transition "
                "that the public Resolve API, FFmpeg review, and conformed audio can execute "
                "with identical timing. Express genuine chapter changes through shot choice, "
                "sound, graphics, or a cut to black, not an unsupported overlap. Keep the sum "
                "of selected clip durations at "
                f"or below {self._active_target_duration_sec * 1.10:.1f} seconds. A shorter "
                "complete film is better than padding to the target: never repeat an action, "
                "idea, lineup, countdown, or setup merely to reach runtime. Select only "
                "the strongest minority of candidates; never include everything. Design "
                "picture rhythm against AVAILABLE SCORE PROFILES now, before selecting: "
                "assign every shot a music_edit_role, preserve natural sound for speech, "
                "use action/reaction and high rhythmic_potential shots for musical builds, "
                "and reserve payoff_hit for the true narrative payoff. Do not pretend a flat "
                "track has a crescendo. "
                "Return project_summary, viewer_takeaway, editorial_style, graphics_plan, "
                "and sequence only; the next constrained call "
                "will score music against this exact edit. Return JSON only.\n"
                f"DIRECTOR TREATMENT:\n{json.dumps(compact_treatment, ensure_ascii=False, separators=(',', ':'))}\n"
                f"EVIDENCE-FIRST NARRATIVE CONTRACT:\n{json.dumps(narrative_contract, ensure_ascii=False, separators=(',', ':'))}\n"
                f"PREVIOUS RENDERED ROUGH-CUT REVIEW:\n{json.dumps(self._rough_cut_feedback, ensure_ascii=False, separators=(',', ':'))}\n"
                f"AVAILABLE SCORE PROFILES:\n{json.dumps(score_profiles, ensure_ascii=False, separators=(',', ':'))}\n"
                f"ASSETS:\n{json.dumps(asset_names, ensure_ascii=False, separators=(',', ':'))}\n"
                f"FULL COVERAGE SYNOPSIS:\n{json.dumps(coverage_synopsis, ensure_ascii=False, separators=(',', ':'))}\n"
                f"CANDIDATES:\n{json.dumps(active_candidates, ensure_ascii=False, separators=(',', ':'))}"
            )

        sequence_prompt = build_sequence_prompt(compact_candidates)
        candidate_directing: Dict[str, Any] = {
            "mode": "single_pass_full_ledger",
            "requested_context_tokens": self.num_ctx,
            "configured_context_tokens": self._effective_num_ctx(self.text_model),
            "context_policy": (
                "70b_72b_mixed_memory_hard_cap"
                if self._effective_num_ctx(self.text_model) < self.num_ctx
                else "configured_context"
            ),
            "all_candidate_count": len(compact_candidates),
            "final_assembly_candidate_count": len(compact_candidates),
            "all_candidates_considered": True,
            "narrative_contract": narrative_contract,
            "review_rounds": [],
        }
        # Prefer a single global call when the complete compact ledger fits. If
        # it does not, every candidate is reviewed by the same director in
        # chronological pages. Only director-recommended ids advance to the
        # final assembly call; no score-based Python shortlist is used.
        # 完整候选表能放下时一次提交；否则由同一导演按时间顺序分页审阅全部候选，
        # 再汇总进入总编排。不存在 Python 固定 Top-N 丢镜头。
        review_pool = list(compact_candidates)
        review_round_number = 0
        while not self._request_has_capacity(
            build_sequence_prompt(review_pool),
            PICTURE_ORDER_SCHEMA,
            model=self.text_model,
            reserve_output_tokens=1024,
        ):
            if len(review_pool) <= 1:
                raise DirectorError(
                    "即使只有一个候选，全局导演固定证据仍无法放入有效 Context。 / "
                    "Even one candidate cannot fit the global director fixed evidence. "
                    + self._context_capacity_guidance(self.text_model)
                )
            review_round_number += 1
            previous_count = len(review_pool)
            review_pool, round_audit = self._review_candidate_round(
                review_pool,
                compact_treatment,
                asset_names,
                coverage_synopsis,
                round_number=review_round_number,
            )
            candidate_directing["review_rounds"].append(round_audit)
            if len(review_pool) >= previous_count:
                raise DirectorError(
                    "分页导演未能缩小最终汇总输入。 / "
                    "Paged directing did not reduce final assembly input. "
                    + self._context_capacity_guidance(self.text_model)
                )
            if review_round_number >= 8:
                raise DirectorError(
                    "候选分页超过 8 轮仍无法放入最终上下文。"
                    " / Candidate review exceeded eight rounds without fitting the final context."
                )
        if review_round_number:
            candidate_directing["mode"] = "paged_full_ledger"
            candidate_directing["final_assembly_candidate_count"] = len(review_pool)
            sequence_prompt = build_sequence_prompt(review_pool)
            self.logger.info(
                "全量候选已由导演分页审阅：%d 个全部进入审阅，%d 个进入最终编排 / "
                "Paged director reviewed all %d candidates; %d advanced to final assembly",
                len(compact_candidates),
                len(review_pool),
                len(compact_candidates),
                len(review_pool),
            )
        self.logger.info(
            "正在进行跨素材全局编排（%d 个候选，第 1/2 步：镜头）/ "
            "Global story assembly (%d candidates, step 1/2: picture)",
            len(candidates),
            len(candidates),
        )
        sequence_payload = self._request_staged_picture_plan(
            sequence_prompt,
            review_pool,
            include_review=False,
            progress_activity="picture_assembly",
        )
        candidate_directing["draft_picture_output_protocol"] = dict(
            sequence_payload.get("_staged_output_audit") or {}
        )

        # A separate supervising-editor pass protects narrative logic and factual
        # grounding. It may revise the draft, whereas Python is not allowed to
        # silently add, delete, or reorder creative choices afterward.
        # 独立总剪辑师复审叙事与证据；此后 Python 不得再静默增删或重排镜头。
        draft_metrics = self._picture_plan_metrics(sequence_payload, candidates)
        compact_draft_metrics = {
            key: value for key, value in draft_metrics.items()
            if key != "shot_audit"
        }
        supervising_prompt = (
            "SUPERVISING EDITOR REVIEW. Audit the draft picture plan as if a viewer has "
            "never seen the prompt. Return a complete revised plan, not notes. The film "
            "must communicate one intelligible idea through visible/audible cause, contrast, "
            "or progression. Reject any claim or title unsupported by candidate evidence. "
            "A static lineup, countdown, readiness, headlight, or forward lean does not prove "
            "departure. If the desired action is absent, openly reframe the thesis around what "
            "was actually filmed. Preserve useful human behavior when it gives character. "
            "Each shot needs a unique job and a concrete connection to the preceding shot. "
            "Do not rubber-stamp the draft: problems_found and changes_made must cite concrete "
            "shot, dialogue, title, or rhythm decisions. For a film under two minutes that is "
            "not explicitly dialogue_led, preserved dialogue should normally occupy less than "
            "half the runtime. Keep only lines that change understanding, reveal character, or "
            "create a genuine turn; one concise exchange is enough for routine logistics. "
            "Avoid adjacent shots that answer the same question or repeat the same action. "
            "Most montage shots should be roughly 1.5-5 seconds; longer dialogue is earned only "
            "by a complete, meaningful thought. Prefer visible behavior, movement, reaction, "
            "composition, and contrast over people explaining setup. If visual diversity is "
            "limited, make a shorter film instead of padding with technical chatter. Use zero "
            "to two graphics by default for a sub-two-minute film; three or more require truly "
            "distinct chapters. Music-edit roles must progress across natural sound, phrase "
            "starts, build, payoff, and release rather than repeating one label. "
            "natural_texture still preserves all production audio, including speech; use "
            "mute_for_music when a line should not remain audible. "
            "Apply human cutting grammar: enter a shot immediately before its useful action or "
            "line and leave immediately after it; cut on action, reaction, gaze, or sound; make "
            "adjacent shots change subject, scale, movement, information, or emotional pressure. "
            "Do not use an explanatory title such as THE LOGISTICS to make routine footage feel "
            "important. If the process has no conflict, discovery, character turn, or real payoff, "
            "abandon the behind-the-scenes premise and make a shorter visual mood or style film. "
            "Choose the duration and number of shots yourself; the target is guidance, not a "
            "reason for software to alter your cut later. Return JSON only.\n"
            f"USER CREATIVE BRIEF: {self.creative_brief or '(free direction)'}\n"
            f"DIRECTOR TREATMENT:\n{json.dumps(compact_treatment, ensure_ascii=False, separators=(',', ':'))}\n"
            f"EVIDENCE-FIRST NARRATIVE CONTRACT:\n{json.dumps(narrative_contract, ensure_ascii=False, separators=(',', ':'))}\n"
            f"PREVIOUS RENDERED ROUGH-CUT REVIEW:\n{json.dumps(self._rough_cut_feedback, ensure_ascii=False, separators=(',', ':'))}\n"
            f"EVIDENCE AUDIT:\n{json.dumps(coverage_synopsis, ensure_ascii=False, separators=(',', ':'))}\n"
            f"DRAFT EDIT METRICS:\n{json.dumps(compact_draft_metrics, ensure_ascii=False, separators=(',', ':'))}\n"
            f"AVAILABLE CANDIDATES:\n{json.dumps(review_pool, ensure_ascii=False, separators=(',', ':'))}\n"
            f"DRAFT PICTURE PLAN:\n{json.dumps(sequence_payload, ensure_ascii=False, separators=(',', ':'))}"
        )
        if not self._request_has_capacity(
            supervising_prompt,
            PICTURE_ORDER_REVIEW_SCHEMA,
            model=self.text_model,
            reserve_output_tokens=1024,
        ):
            raise DirectorError(
                "总剪辑复审的固定证据无法完整放入有效 Context；禁止跳过叙事复审。 / "
                "Supervising-editor fixed evidence does not fit; review cannot be skipped. "
                + self._context_capacity_guidance(self.text_model)
            )
        self.logger.info(
            "正在由总剪辑师复审叙事与证据（镜头第 2/2 步）/ "
            "Supervising editor reviewing story and evidence (picture step 2/2)"
        )
        draft_sequence_payload = sequence_payload
        sequence_payload = self._request_staged_picture_plan(
            supervising_prompt,
            review_pool,
            include_review=True,
            progress_activity="picture_critique",
        )
        candidate_directing["supervised_picture_output_protocol"] = dict(
            sequence_payload.get("_staged_output_audit") or {}
        )
        quality_audit_rounds: List[Dict[str, Any]] = []
        max_quality_revisions = 3
        final_metrics: Dict[str, Any] = {}
        quality_violations: List[str] = []
        best_sequence_payload = sequence_payload
        best_metrics: Dict[str, Any] = {}
        best_violations: List[str] = []
        best_blind_review: Dict[str, Any] = {}
        best_rank: Optional[tuple[Any, ...]] = None
        previous_rank: Optional[tuple[Any, ...]] = None
        previous_signature = ""
        structural_reset_used = False
        for revision_index in range(max_quality_revisions + 1):
            final_metrics = self._picture_plan_metrics(sequence_payload, candidates)
            blind_review = self._request_blind_viewer_review(
                sequence_payload, candidates, narrative_contract
            )
            quality_violations = self._picture_plan_quality_violations(
                sequence_payload,
                final_metrics,
                candidates,
                treatment,
                narrative_contract,
                coverage_synopsis,
                blind_review,
            )
            current_signature = json.dumps(
                {
                    "sequence": sequence_payload.get("sequence", []),
                    "graphics_plan": sequence_payload.get("graphics_plan", {}),
                },
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
            # Rank complete model-authored alternatives; Python does not invent
            # or rewrite shots. A lower tuple means fewer unresolved findings,
            # less repetitive labeling, and more visual variety.
            current_rank: tuple[Any, ...] = (
                len(quality_violations),
                int(final_metrics.get("longest_same_narrative_function_run", 0) or 0),
                int(final_metrics.get("longest_same_music_role_run", 0) or 0),
                float(final_metrics.get("static_shot_ratio", 0) or 0),
                float(final_metrics.get("dominant_shot_scale_ratio", 0) or 0),
                -int(blind_review.get("coherence_score", 0) or 0),
                -int(blind_review.get("visual_payoff_score", 0) or 0),
            )
            if best_rank is None or current_rank < best_rank:
                best_rank = current_rank
                best_sequence_payload = sequence_payload
                best_metrics = final_metrics
                best_violations = list(quality_violations)
                best_blind_review = dict(blind_review)
            quality_audit_rounds.append(
                {
                    "round": revision_index,
                    "metrics": final_metrics,
                    "violations": list(quality_violations),
                    "rank": list(current_rank),
                    "review": sequence_payload.get("review", {}),
                    "blind_viewer_review": blind_review,
                }
            )
            self.logger.info(
                "总剪辑质量门：第 %d 轮，对白 %.1f%%，静态镜头 %.1f%%，字卡 %d，问题 %d / "
                "Picture quality gate round %d: dialogue %.1f%%, static %.1f%%, graphics %d, violations %d",
                revision_index,
                float(final_metrics.get("preserved_dialogue_ratio", 0)) * 100,
                float(final_metrics.get("static_shot_ratio", 0)) * 100,
                int(final_metrics.get("graphic_count", 0)),
                len(quality_violations),
                revision_index,
                float(final_metrics.get("preserved_dialogue_ratio", 0)) * 100,
                float(final_metrics.get("static_shot_ratio", 0)) * 100,
                int(final_metrics.get("graphic_count", 0)),
                len(quality_violations),
            )
            if not quality_violations:
                break
            self.logger.warning(
                "总剪辑方案被质量门退回：%s / Picture plan rejected by measured quality gate: %s",
                "；".join(quality_violations),
                "; ".join(quality_violations),
            )
            no_measurable_progress = (
                revision_index > 0
                and (
                    current_signature == previous_signature
                    or (
                        previous_rank is not None
                        and current_rank >= previous_rank
                    )
                )
            )
            force_structural_reset = (
                not structural_reset_used
                and (
                    no_measurable_progress
                    or revision_index >= max_quality_revisions - 1
                )
            )
            if revision_index >= max_quality_revisions or (
                no_measurable_progress and structural_reset_used
            ):
                if no_measurable_progress:
                    self.logger.warning(
                        "质量重剪没有产生可测量改进，停止重复请求并回退到本轮最佳完整导演方案 / "
                        "Quality recut made no measurable improvement; stopping duplicate requests "
                        "and falling back to the best complete director-authored plan"
                    )
                break
            previous_rank = current_rank
            previous_signature = current_signature
            compact_recut_metrics = {
                key: value for key, value in final_metrics.items()
                if key != "shot_audit"
            }
            compact_attempt_history = [
                {
                    "round": item.get("round"),
                    "violations": item.get("violations", []),
                    "rank": item.get("rank", []),
                }
                for item in quality_audit_rounds
            ] if revision_index > 0 else []
            has_exact_speech_timing = any(
                isinstance(item, dict) and "speech_ranges" in item
                for item in review_pool
            )
            recut_shot_audit = (
                final_metrics.get("shot_audit", [])
                if has_exact_speech_timing
                else []
            )
            audio_evidence_instruction = (
                "The candidate speech_ranges are exact merged source timestamps; silent_ranges "
                "are safe visual-only trim windows. The CURRENT SHOT AUDIT reports source speech "
                "and actually audible speech after audio_intent/volume. Do not guess. Do not mute "
                "story-critical speech merely to game a percentage; when dialogue genuinely drives "
                "the treatment, explain that creative decision in review.dialogue_strategy.\n"
                if has_exact_speech_timing
                else ""
            )
            normal_recut_prompt = (
                "PICTURE QUALITY RECUT. The previous supervising-editor plan has been rejected "
                "by measurements of the actual authored sequence. Do not defend it and do not "
                "merely rewrite the review. Return a materially revised complete picture plan "
                "plus an honest review. Every violation below must be fixed by actual candidate "
                "selection, exact trims, audio intent, narrative functions, graphics, and music "
                "roles. Python will measure the returned plan again. Enter late and leave early; "
                "most visual shots should contain one readable action and last about 1.5-5 seconds. "
                "Use dialogue only when the exact timestamped line changes understanding or reveals "
                "character. Change shot size and movement when the candidate evidence permits it. "
                "Remember that natural_texture keeps speech audible; it cannot be used to hide "
                "dialogue from the measured ratio. Use mute_for_music for unwanted production talk. "
                "A middle made entirely of context is not a story: create escalation, contrast, or "
                "a visible payoff. Reserve payoff_hit for the earned climax. If routine production "
                "logistics do not contain conflict, discovery, or character change, abandon the BTS "
                "premise and author a concise visual mood/style film from the strongest real actions. "
                "Never invent motion or an ending. Return JSON only.\n"
                f"{audio_evidence_instruction}"
                f"USER CREATIVE BRIEF: {self.creative_brief or '(free direction)'}\n"
                f"DIRECTOR TREATMENT:\n{json.dumps(compact_treatment, ensure_ascii=False, separators=(',', ':'))}\n"
                f"EVIDENCE AUDIT:\n{json.dumps(coverage_synopsis, ensure_ascii=False, separators=(',', ':'))}\n"
                f"EVIDENCE-FIRST NARRATIVE CONTRACT:\n{json.dumps(narrative_contract, ensure_ascii=False, separators=(',', ':'))}\n"
                f"BLIND VIEWER FAILURE:\n{json.dumps(blind_review, ensure_ascii=False, separators=(',', ':'))}\n"
                f"MEASURED VIOLATIONS:\n{json.dumps(quality_violations, ensure_ascii=False, separators=(',', ':'))}\n"
                f"MEASURED PLAN METRICS:\n{json.dumps(compact_recut_metrics, ensure_ascii=False, separators=(',', ':'))}\n"
                f"CURRENT SHOT AUDIT:\n{json.dumps(recut_shot_audit, ensure_ascii=False, separators=(',', ':'))}\n"
                f"EARLIER QUALITY ATTEMPTS:\n{json.dumps(compact_attempt_history, ensure_ascii=False, separators=(',', ':'))}\n"
                f"AVAILABLE CANDIDATES:\n{json.dumps(review_pool, ensure_ascii=False, separators=(',', ':'))}\n"
                f"REJECTED PICTURE PLAN:\n{json.dumps(sequence_payload, ensure_ascii=False, separators=(',', ':'))}"
            )
            selected_failed_ids = [
                str(item.get("candidate_id") or "")
                for item in sequence_payload.get("sequence", [])
                if isinstance(item, dict) and str(item.get("candidate_id") or "")
            ]
            structural_recut_prompt = (
                "PICTURE QUALITY RECUT - STRUCTURAL RESET. Incremental repairs have failed. "
                "Discard the rejected premise and build a genuinely different complete film "
                "from AVAILABLE CANDIDATES. Do not preserve the previous shot list, hook, ending, "
                "or explanatory rationale. First choose one observable subject cluster and one "
                "form: (A) a concise character micro-portrait, (B) a pure vehicle style/mood film, "
                "or (C) a factual process vignette with a visible before/change/after. Never combine "
                "unrelated banter, an isolated car showcase, and repeated static motorcycle lineups "
                "merely because all occurred at night. For a mood film, visual progression, changing "
                "scale, movement, and a deliberate final state must replace plot. Use closeup and "
                "medium alternatives when their literal evidence supports the chosen subject; do not "
                "fill runtime with near-identical wides. Each narrative_function may repeat at most "
                "three times in a row. Prefer a focused 15-35 second film with one legible idea over a "
                "long incoherent montage. FAILED SELECTED IDS are not individually forbidden, but reuse "
                "only those essential to the new concept. Return a complete plan plus honest review. "
                "Never invent an event. Return JSON only.\n"
                f"{audio_evidence_instruction}"
                f"USER CREATIVE BRIEF: {self.creative_brief or '(free direction)'}\n"
                f"DIRECTOR TREATMENT (hypothesis, not a command):\n{json.dumps(compact_treatment, ensure_ascii=False, separators=(',', ':'))}\n"
                f"EVIDENCE AUDIT:\n{json.dumps(coverage_synopsis, ensure_ascii=False, separators=(',', ':'))}\n"
                f"EVIDENCE-FIRST NARRATIVE CONTRACT:\n{json.dumps(narrative_contract, ensure_ascii=False, separators=(',', ':'))}\n"
                f"BLIND VIEWER FAILURE:\n{json.dumps(blind_review, ensure_ascii=False, separators=(',', ':'))}\n"
                f"MEASURED VIOLATIONS:\n{json.dumps(quality_violations, ensure_ascii=False, separators=(',', ':'))}\n"
                f"FAILED SELECTED IDS:\n{json.dumps(selected_failed_ids, ensure_ascii=False)}\n"
                f"EARLIER QUALITY ATTEMPTS:\n{json.dumps(compact_attempt_history, ensure_ascii=False, separators=(',', ':'))}\n"
                f"AVAILABLE CANDIDATES:\n{json.dumps(review_pool, ensure_ascii=False, separators=(',', ':'))}"
            )
            recut_prompt = (
                structural_recut_prompt
                if force_structural_reset
                else normal_recut_prompt
            )
            if not self._request_has_capacity(
                recut_prompt,
                PICTURE_ORDER_REVIEW_SCHEMA,
                model=self.text_model,
                reserve_output_tokens=1024,
            ):
                raise DirectorError(
                    "质量门重剪的固定证据无法完整放入有效 Context。 / "
                    "Quality-gate recut fixed evidence does not fit. "
                    + self._context_capacity_guidance(self.text_model)
                )
            self.logger.info(
                "正在执行质量门退回重剪 %d/%d / Quality-gate recut %d/%d",
                revision_index + 1,
                max_quality_revisions,
                revision_index + 1,
                max_quality_revisions,
            )
            if force_structural_reset:
                self.logger.warning(
                    "Incremental recuts stalled; running a structural-reset recut "
                    "%d/%d from the complete evidence pool",
                    revision_index + 1,
                    max_quality_revisions,
                )
            structural_reset_used = structural_reset_used or force_structural_reset
            sequence_payload = self._request_staged_picture_plan(
                recut_prompt,
                review_pool,
                include_review=True,
                progress_activity="picture_recut",
            )

        sequence_payload = best_sequence_payload
        final_metrics = best_metrics
        quality_violations = best_violations
        blind_review = best_blind_review
        quality_gate_degraded = bool(quality_violations)
        if quality_gate_degraded:
            # Never ship a cut that the system itself says a blind viewer cannot
            # understand.  The caller may retry a different evidence-backed
            # concept; if all concepts fail, the workflow stops before Resolve.
            # 绝不交付系统自己判定“陌生观众看不懂”的版本；调用方可改试另一构想，
            # 三种构想均失败时则在进入 Resolve 前停止。
            self.logger.error(
                "导演质量门耗尽重剪且仍未通过；拒绝把已知不合格版本送入 Resolve / "
                "Editorial gate exhausted all recuts; refusing a known-bad Resolve render"
            )
            raise EditorialQualityError(
                quality_violations,
                metrics=final_metrics,
                blind_review=blind_review,
            )

        draft_picture_signature = json.dumps(
            {
                "sequence": draft_sequence_payload.get("sequence", []),
                "graphics_plan": draft_sequence_payload.get("graphics_plan", {}),
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        final_picture_signature = json.dumps(
            {
                "sequence": sequence_payload.get("sequence", []),
                "graphics_plan": sequence_payload.get("graphics_plan", {}),
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        supervising_changed_plan = draft_picture_signature != final_picture_signature
        candidate_directing["supervising_editor_reviewed"] = True
        candidate_directing["supervising_editor_changed_plan"] = supervising_changed_plan
        candidate_directing["draft_metrics"] = draft_metrics
        candidate_directing["final_metrics"] = final_metrics
        candidate_directing["quality_gate_passed"] = not quality_gate_degraded
        candidate_directing["quality_gate_degraded_acceptance"] = quality_gate_degraded
        candidate_directing["quality_gate_status"] = (
            "degraded_best_available" if quality_gate_degraded else "passed"
        )
        candidate_directing["unresolved_quality_advisories"] = list(quality_violations)
        candidate_directing["quality_revision_count"] = max(
            0, len(quality_audit_rounds) - 1
        )
        candidate_directing["quality_audit_rounds"] = quality_audit_rounds
        candidate_directing["blind_viewer_review"] = blind_review
        candidate_directing["supervising_review"] = sequence_payload.get("review", {})
        candidate_directing["draft_picture_plan"] = draft_sequence_payload
        candidate_directing["final_picture_output_protocol"] = dict(
            sequence_payload.get("_staged_output_audit") or {}
        )
        self.logger.info(
            "总剪辑复审指标：对白占比 %.1f%% → %.1f%%，字卡 %d → %d，平均镜长 %.1fs → %.1fs / "
            "Supervising metrics: dialogue %.1f%% -> %.1f%%, graphics %d -> %d, average shot %.1fs -> %.1fs",
            float(draft_metrics.get("preserved_dialogue_ratio", 0)) * 100,
            float(final_metrics.get("preserved_dialogue_ratio", 0)) * 100,
            int(draft_metrics.get("graphic_count", 0)),
            int(final_metrics.get("graphic_count", 0)),
            float(draft_metrics.get("average_shot_duration_sec", 0)),
            float(final_metrics.get("average_shot_duration_sec", 0)),
            float(draft_metrics.get("preserved_dialogue_ratio", 0)) * 100,
            float(final_metrics.get("preserved_dialogue_ratio", 0)) * 100,
            int(draft_metrics.get("graphic_count", 0)),
            int(final_metrics.get("graphic_count", 0)),
            float(draft_metrics.get("average_shot_duration_sec", 0)),
            float(final_metrics.get("average_shot_duration_sec", 0)),
        )
        if not supervising_changed_plan:
            self.logger.warning(
                "总剪辑师未实际改变镜头或字卡；已在 timeline_cuts.json 中记录，"
                "请检查 review 是否有充分依据 / Supervising editor returned an unchanged "
                "picture plan; the audit is recorded for inspection"
            )

        # Music must be authored against the final, deterministic picture lock.
        # The older path scored the model's pre-validation list, then runtime
        # guards removed/changed shots, leaving cue hits attached to nonexistent
        # boundaries. Validate first and never move picture after this point.
        # 配乐必须基于最终锁画；禁止再用守门前的旧故事板设计卡点。
        picture_lock = self.validate_sequence(sequence_payload, candidates, treatment)
        picture_lock = self._attach_candidate_dialogue(picture_lock, assets)
        selected_storyboard = self._build_locked_music_storyboard(picture_lock)
        locked_duration = selected_storyboard[-1]["timeline_out"] if selected_storyboard else 0.0
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
                f"LOCKED PROGRAM DURATION: {locked_duration:.3f}s\n"
                f"DIRECTOR TREATMENT:\n{json.dumps(compact_treatment, ensure_ascii=False, separators=(',', ':'))}\n"
                f"FULL COVERAGE SYNOPSIS:\n{json.dumps(coverage_synopsis, ensure_ascii=False, separators=(',', ':'))}\n"
                f"SELECTED PICTURE STORYBOARD:\n{json.dumps(selected_storyboard, ensure_ascii=False, separators=(',', ':'))}\n"
                f"AVAILABLE MUSIC:\n{json.dumps(music_choices, ensure_ascii=False, separators=(',', ':'))}"
            )
            self.logger.info(
                "正在设计最终配乐（第 2/2 步：音乐）/ "
                "Designing final music (step 2/2: cue sheet)"
            )
            music_plan = self._request_quality_gated_music_plan(
                music_prompt, locked_duration, compact_treatment
            )
            candidate_directing["music_quality_gate_passed"] = True
            candidate_directing["music_quality_gate_degraded_acceptance"] = False
            candidate_directing["unresolved_music_advisories"] = []
        else:
            music_plan = {
                "strategy": "No analyzed music candidates were supplied.",
                "silence_regions": [],
                "cues": [],
            }
            candidate_directing["music_quality_gate_passed"] = True
            candidate_directing["music_quality_gate_degraded_acceptance"] = False
            candidate_directing["unresolved_music_advisories"] = []
        return {
            "project_summary": sequence_payload.get("project_summary", ""),
            "viewer_takeaway": sequence_payload.get(
                "viewer_takeaway", treatment.get("viewer_takeaway", "")
            ),
            "editorial_style": sequence_payload.get(
                "editorial_style", treatment.get("edit_style", "hybrid_cinematic")
            ),
            "graphics_plan": sequence_payload.get("graphics_plan", {}),
            "coverage_synopsis": coverage_synopsis,
            "narrative_contract": narrative_contract,
            "candidate_directing": candidate_directing,
            "sequence": sequence_payload.get("sequence", []),
            "music_plan": music_plan,
            "picture_lock_audit": {
                "locked_before_music": True,
                "duration_sec": round(float(locked_duration), 4),
                "candidate_ids": [
                    str(item.get("candidate_id") or "") for item in picture_lock
                ],
            },
        }

    def _review_candidate_round(
        self,
        candidates: Sequence[Dict[str, Any]],
        treatment: Dict[str, Any],
        assets: Sequence[Dict[str, Any]],
        coverage_synopsis: Dict[str, Any],
        *,
        round_number: int,
    ) -> tuple[List[Dict[str, Any]], Dict[str, Any]]:
        """
        Review every candidate in context-safe chronological pages.
        在上下文安全的时间顺序分页中审阅每一个候选镜头。

        Parameters / 参数:
            candidates: Complete candidate pool for this review round. / 本轮完整候选池。
            treatment: Compact director treatment. / 紧凑导演阐述。
            assets: Source identity and duration records. / 素材标识与时长记录。
            coverage_synopsis: Memory synthesized from the exhaustive visual pass. /
                从全量视觉审片中合成的全片记忆。
            round_number: One-based hierarchy level. / 从 1 开始的分层轮次。

        Returns / 返回:
            Director-recommended candidates plus a reproducible coverage audit. /
            导演推荐候选以及可复核的全量覆盖审计。
        """
        def review_schema(max_items: int) -> Dict[str, Any]:
            return {
                "type": "object",
                "properties": {
                    "page_summary": {
                        "type": "string",
                        "minLength": 1,
                        "maxLength": 900,
                    },
                    "recommendations": {
                        "type": "array",
                        "minItems": 1,
                        "maxItems": max_items,
                        "items": {
                            "type": "object",
                            "properties": {
                                "candidate_id": {"type": "string", "minLength": 1},
                                "story_value": {
                                    "type": "string",
                                    "minLength": 1,
                                    "maxLength": 320,
                                },
                                "suggested_story_role": {
                                    "type": "string",
                                    "enum": [
                                        "opening", "context", "interview", "broll",
                                        "bridge", "climax", "closing",
                                    ],
                                },
                            },
                            "required": [
                                "candidate_id", "story_value", "suggested_story_role"
                            ],
                            "additionalProperties": False,
                        },
                    },
                },
                "required": ["page_summary", "recommendations"],
                "additionalProperties": False,
            }

        def page_prompt(page: Sequence[Dict[str, Any]], max_keep: int) -> str:
            return (
                f"FULL-LEDGER CANDIDATE REVIEW, hierarchy round {round_number}. "
                "This is a transport page, not the whole film. Examine EVERY candidate "
                "on this page against the complete treatment and full-footage memory. "
                "Recommend the candidates that preserve unique story evidence, complete "
                "dialogue thoughts, action/reaction continuity, emotional change, or a "
                "necessary opening/payoff/ending. Reject only genuine repetition, weak "
                "technical material, or shots with no distinct editorial value. Do not "
                "invent ids. Chronological position is evidence, not an obligation to keep "
                "every shot. Return at most "
                f"{max_keep} recommendations as JSON.\n"
                f"DIRECTOR TREATMENT:\n{json.dumps(treatment, ensure_ascii=False, separators=(',', ':'))}\n"
                f"ASSETS:\n{json.dumps(assets, ensure_ascii=False, separators=(',', ':'))}\n"
                f"FULL COVERAGE SYNOPSIS:\n{json.dumps(coverage_synopsis, ensure_ascii=False, separators=(',', ':'))}\n"
                f"CANDIDATE PAGE:\n{json.dumps(page, ensure_ascii=False, separators=(',', ':'))}"
            )

        pages: List[List[Dict[str, Any]]] = []
        page: List[Dict[str, Any]] = []
        for candidate in candidates:
            trial = page + [dict(candidate)]
            trial_keep = 1 if len(trial) <= 2 else max(2, math.ceil(len(trial) * 0.55))
            trial_schema = review_schema(trial_keep)
            if page and (
                len(trial) > 24
                or not self._request_has_capacity(
                    page_prompt(trial, trial_keep),
                    trial_schema,
                    model=self.text_model,
                    reserve_output_tokens=1536,
                )
            ):
                pages.append(page)
                page = [dict(candidate)]
            else:
                page = trial
        if page:
            pages.append(page)

        selected_by_id: Dict[str, Dict[str, Any]] = {}
        page_audits: List[Dict[str, Any]] = []
        for page_index, candidate_page in enumerate(pages, start=1):
            max_keep = (
                1
                if len(candidate_page) <= 2
                else max(2, math.ceil(len(candidate_page) * 0.55))
            )
            schema = review_schema(max_keep)
            prompt = page_prompt(candidate_page, max_keep)
            if not self._request_has_capacity(
                prompt,
                schema,
                model=self.text_model,
                reserve_output_tokens=1024,
            ):
                raise DirectorError(
                    "单个候选审阅页仍超过上下文，请提高 Context。"
                    " / One candidate review page still exceeds Context; increase it."
                )
            self.logger.info(
                "全量候选审阅：第 %d 轮，第 %d/%d 页，%d 个候选 / "
                "Full-ledger review: round %d, page %d/%d, %d candidates",
                round_number,
                page_index,
                len(pages),
                len(candidate_page),
                round_number,
                page_index,
                len(pages),
                len(candidate_page),
            )
            payload = self._request_json(
                prompt,
                schema,
                model=self.text_model,
                progress_activity="candidate_page_review",
            )
            page_by_id = {
                str(item.get("candidate_id") or ""): item for item in candidate_page
            }
            selected_ids: List[str] = []
            for recommendation in payload.get("recommendations", []):
                if not isinstance(recommendation, dict):
                    continue
                candidate_id = str(recommendation.get("candidate_id") or "").strip()
                if candidate_id not in page_by_id or candidate_id in selected_by_id:
                    continue
                selected = dict(page_by_id[candidate_id])
                selected["page_review"] = self._compact_prompt_text(
                    recommendation.get("story_value", ""), 180
                )
                selected["page_suggested_story_role"] = str(
                    recommendation.get("suggested_story_role") or "context"
                )
                selected_by_id[candidate_id] = selected
                selected_ids.append(candidate_id)
            if not selected_ids:
                raise DirectorError(
                    f"候选审阅第 {round_number} 轮第 {page_index} 页没有返回有效 id。"
                    " / Candidate review page returned no valid ids."
                )
            page_audits.append(
                {
                    "page": page_index,
                    "input_candidate_ids": list(page_by_id),
                    "recommended_candidate_ids": selected_ids,
                    "page_summary": self._compact_prompt_text(
                        payload.get("page_summary", ""), 600
                    ),
                }
            )

        ordered = [
            selected_by_id[str(item.get("candidate_id") or "")]
            for item in candidates
            if str(item.get("candidate_id") or "") in selected_by_id
        ]
        return ordered, {
            "round": round_number,
            "input_candidate_count": len(candidates),
            "recommended_candidate_count": len(ordered),
            "page_count": len(pages),
            "pages": page_audits,
        }

    @staticmethod
    def _compact_prompt_text(value: object, limit: int) -> str:
        """Normalize and bound one model-facing string. / 规范并限制模型输入字符串。"""
        text = " ".join(str(value or "").split())
        return text if len(text) <= limit else text[: max(1, limit - 1)].rstrip() + "…"

    @classmethod
    def _compact_prompt_value(cls, value: Any, depth: int = 0) -> Any:
        """
        Recursively bound a schema value and remove durable audit structures.
        递归限制 Schema 值，并移除只应持久化的审片结构。

        Parameters / 参数:
            value: Validated value crossing into a model prompt. / 将进入模型提示的已校验值。
            depth: Current recursion depth. / 当前递归深度。
        """
        if isinstance(value, str):
            return cls._compact_prompt_text(value, 320 if depth <= 1 else 180)
        if isinstance(value, (bool, int, float)) or value is None:
            return value
        if depth >= 5:
            return [] if isinstance(value, (list, tuple)) else {}
        if isinstance(value, (list, tuple)):
            return [
                cls._compact_prompt_value(item, depth + 1)
                for item in list(value)[:16]
            ]
        if isinstance(value, dict):
            blocked = {
                "candidate_audit", "evidence_review", "pages",
                "input_candidate_ids", "footage_ledger", "evidence_fingerprint",
                "generated_at_utc", "director_model",
            }
            return {
                str(key): cls._compact_prompt_value(nested, depth + 1)
                for key, nested in list(value.items())[:24]
                if str(key) not in blocked
            }
        return cls._compact_prompt_text(value, 180)

    @classmethod
    def _compact_treatment_for_prompt(cls, treatment: Dict[str, Any]) -> Dict[str, Any]:
        """
        Keep treatment semantics without embedding durable audit pages.
        保留导演意图，但绝不将持久化审片分页嵌入模型提示。

        ``concept_tournament.evidence_review`` can contain every hierarchical
        ledger page and is provenance, not a directing instruction. Copying it
        into every downstream prompt causes recursive context growth. Only the
        selected concept and bounded treatment-schema fields cross this boundary.

        ``concept_tournament.evidence_review`` 可能包含全部分层账本，它是
        溯源记录而非导演指令。此边界只传递优胜构想与有界的阐述字段。
        """
        allowed = set(TREATMENT_SCHEMA.get("properties", {})) | {
            "selected_concept_id",
        }
        compact: Dict[str, Any] = {}
        for key, value in treatment.items():
            if key not in allowed or key == "story_anchors":
                continue
            compact[key] = cls._compact_prompt_value(value)

        tournament = treatment.get("concept_tournament")
        if isinstance(tournament, dict):
            selected_id = str(
                treatment.get("selected_concept_id")
                or tournament.get("selected_concept_id")
                or ""
            ).strip()
            selected_concept = next(
                (
                    item for item in tournament.get("concepts", [])
                    if isinstance(item, dict)
                    and str(item.get("concept_id") or "") == selected_id
                ),
                {},
            )
            compact["concept_tournament"] = {
                "selected_concept_id": selected_id,
                "selection_reason": cls._compact_prompt_text(
                    tournament.get("selection_reason"), 320
                ),
                "selected_concept": cls._compact_prompt_value(selected_concept),
            }
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

    @classmethod
    def _build_locked_music_storyboard(
        cls, clips: Sequence[Dict[str, Any]]
    ) -> List[Dict[str, Any]]:
        """
        Describe the validated picture lock with exact final timeline offsets.
        使用最终校验后的精确时间线位置构建配乐故事板。

        Parameters / 参数:
            clips: Final ordered and duration-fitted picture clips. / 已排序并完成时长守门的镜头。
        """
        result: List[Dict[str, Any]] = []
        cursor = 0.0
        for clip in clips:
            duration = max(
                0.0,
                float(clip.get("cut_out_sec", 0))
                - float(clip.get("cut_in_sec", 0)),
            )
            result.append(
                {
                    "candidate_id": str(clip.get("candidate_id") or ""),
                    "timeline_in": round(cursor, 3),
                    "timeline_out": round(cursor + duration, 3),
                    "source_in": round(float(clip.get("cut_in_sec", 0)), 3),
                    "source_out": round(float(clip.get("cut_out_sec", 0)), 3),
                    "narrative_function": str(
                        clip.get("narrative_function") or "context"
                    ),
                    "viewer_information": cls._compact_prompt_text(
                        clip.get("viewer_information", ""), 180
                    ),
                    "story_role": clip.get("story_role", "context"),
                    "music_edit_role": clip.get("music_edit_role", "natural_sound"),
                    "audio_intent": clip.get("audio_intent", "mix_with_music"),
                    "has_dialogue": bool(clip.get("has_dialogue")),
                    "dialogue_ranges_sec": list(clip.get("dialogue_ranges_sec") or [])[:12],
                    "action_phase": clip.get("action_phase", "action"),
                    "emotion": cls._compact_prompt_text(clip.get("emotion", ""), 80),
                    "rhythmic_potential": clip.get("rhythmic_potential", 0.5),
                    "visual": cls._compact_prompt_text(
                        clip.get("visual_summary", ""), 180
                    ),
                    "position_reason": cls._compact_prompt_text(
                        clip.get("reason_for_position", ""), 160
                    ),
                }
            )
            cursor += duration
        return result

    def validate_graphics_plan(
        self,
        payload: Any,
        clips: Sequence[Dict[str, Any]],
        treatment: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """
        Resolve candidate-anchored typography to exact final timeline seconds.
        将候选镜头锚定的字体设计解析为最终时间线秒数。

        Invalid or removed anchors are discarded. A restrained opening title is
        added when the model returns no executable graphics, so a finished short
        always has an optional, editable statement of intent.
        无效或被删镜头的字卡会被丢弃；模型未返回可执行方案时补一个克制的开场标题。
        """
        value = payload if isinstance(payload, dict) else {}
        active_treatment = treatment or self._active_treatment
        positions: Dict[str, tuple[float, float]] = {}
        cursor = 0.0
        for clip in clips:
            duration = max(
                0.0,
                float(clip.get("cut_out_sec", 0))
                - float(clip.get("cut_in_sec", 0)),
            )
            candidate_id = str(clip.get("candidate_id") or "")
            if candidate_id:
                positions[candidate_id] = (cursor, cursor + duration)
            cursor += duration
        styles = {"minimal", "bold_cinematic", "kinetic", "editorial"}
        kinds = {"title_card", "chapter", "lower_third", "end_card"}
        placements = {"clip_start", "clip_middle", "clip_end"}
        items: List[Dict[str, Any]] = []
        for index, raw in enumerate(
            value.get("items", []) if isinstance(value.get("items"), list) else []
        ):
            if not isinstance(raw, dict):
                continue
            anchor_id = str(raw.get("anchor_candidate_id") or "").strip()
            bounds = positions.get(anchor_id)
            text = " ".join(str(raw.get("text") or "").split())[:80]
            if bounds is None or not text:
                continue
            clip_start, clip_end = bounds
            clip_duration = max(0.0, clip_end - clip_start)
            duration = min(
                6.0,
                clip_duration,
                max(0.8, float(raw.get("duration_sec", 2.5) or 2.5)),
            )
            placement = str(raw.get("placement") or "clip_start").casefold()
            placement = placement if placement in placements else "clip_start"
            if placement == "clip_end":
                start = max(clip_start, clip_end - duration)
            elif placement == "clip_middle":
                start = max(clip_start, (clip_start + clip_end - duration) / 2.0)
            else:
                start = clip_start
            kind = str(raw.get("kind") or "chapter").casefold()
            style = str(raw.get("style") or "minimal").casefold()
            items.append(
                {
                    "graphic_id": str(raw.get("graphic_id") or f"G{index + 1}"),
                    "kind": kind if kind in kinds else "chapter",
                    "anchor_candidate_id": anchor_id,
                    "timeline_in_sec": round(start, 3),
                    "timeline_out_sec": round(min(cursor, start + duration), 3),
                    "text": text,
                    "subtitle": " ".join(str(raw.get("subtitle") or "").split())[:140],
                    "style": style if style in styles else "minimal",
                    "purpose": " ".join(str(raw.get("purpose") or "Story clarity").split())[:240],
                }
            )
        if not items and clips and cursor >= 0.8:
            first_id = str(clips[0].get("candidate_id") or "")
            title = " ".join(str(active_treatment.get("title") or "UNTITLED").split())[:80]
            takeaway = " ".join(
                str(active_treatment.get("viewer_takeaway") or "").split()
            )[:140]
            items.append(
                {
                    "graphic_id": "G_TITLE",
                    "kind": "title_card",
                    "anchor_candidate_id": first_id,
                    "timeline_in_sec": 0.0,
                    "timeline_out_sec": round(min(cursor, 3.5), 3),
                    "text": title,
                    "subtitle": takeaway,
                    "style": "bold_cinematic"
                    if str(active_treatment.get("edit_style")) in {
                        "kinetic_montage", "hybrid_cinematic"
                    }
                    else "minimal",
                    "purpose": "State the film's premise before the visual progression.",
                }
            )
        items.sort(key=lambda item: (float(item["timeline_in_sec"]), item["graphic_id"]))
        return {
            "strategy": " ".join(
                str(value.get("strategy") or active_treatment.get("typography_intent") or "").split()
            ),
            "renderer": "ffmpeg_preview_and_resolve_text_plus",
            "items": items[:6],
        }

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
            candidate_in = float(clip.get("cut_in_sec", 0) or 0)
            candidate_out = float(clip.get("cut_out_sec", candidate_in) or candidate_in)
            # Preserve the neutral/dense-reviewed candidate envelope before the
            # text director applies an optional tighter trim. Beat snapping may
            # use this verified handle but must never cross it.
            # 文字导演二次收窄前保留中立/密集复审边界；卡点只能使用该已审句柄。
            clip["reviewed_trim_bounds"] = {
                "in_sec": round(candidate_in, 3),
                "out_sec": round(candidate_out, 3),
            }
            trim_in = self._finite_float(
                sequence_item.get("trim_in_sec", candidate_in),
                f"sequence[{index}].trim_in_sec",
            )
            trim_out = self._finite_float(
                sequence_item.get("trim_out_sec", candidate_out),
                f"sequence[{index}].trim_out_sec",
            )
            trim_in = min(candidate_out, max(candidate_in, trim_in))
            trim_out = min(candidate_out, max(trim_in, trim_out))
            if trim_out - trim_in >= 0.4:
                clip["cut_in_sec"] = round(trim_in, 3)
                clip["cut_out_sec"] = round(trim_out, 3)
            clip["narrative_function"] = self._enum_value(
                sequence_item.get("narrative_function"),
                {"hook", "context", "escalation", "contrast", "payoff", "closure"},
                "context",
            )
            clip["viewer_information"] = " ".join(
                str(
                    sequence_item.get("viewer_information")
                    or clip.get("visual_summary")
                    or clip.get("subject_action")
                    or "Advances the selected story beat."
                ).split()
            )
            clip["reason_for_position"] = " ".join(
                str(
                    sequence_item.get("reason_for_position")
                    or clip.get("reason_for_cut")
                    or "Supports the preceding and following story beats."
                ).split()
            )
            clip["evidence_claim"] = " ".join(
                str(
                    sequence_item.get("evidence_claim")
                    or clip.get("visual_summary")
                    or clip.get("subject_action")
                    or "Observed source action."
                ).split()
            )
            clip["connection_to_previous"] = " ".join(
                str(
                    sequence_item.get("connection_to_previous")
                    or ("Opens the film." if not final else "Continues the director's selected progression.")
                ).split()
            )
            clip["audio_intent"] = self._enum_value(
                sequence_item.get("audio_intent"),
                {
                    "preserve_dialogue", "natural_texture",
                    "mute_for_music", "mix_with_music",
                },
                "preserve_dialogue" if clip.get("story_role") == "interview"
                else "mix_with_music",
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
            if clip["transition_to_next"] != "cut":
                # Resolve's public API cannot place a general transition with
                # deterministic overlap. Keeping it only in the FFmpeg preview
                # shortens that render but not Resolve/program audio. Preserve the
                # creative request as an audit hint and execute a matched hard cut.
                # Resolve 公开 API 无法可靠创建带重叠的通用转场；为保证审片、原声和
                # Resolve 同长，记录导演意图但实际统一执行硬切。
                clip["requested_transition_to_next"] = clip["transition_to_next"]
                clip["requested_transition_duration_sec"] = clip[
                    "transition_duration_sec"
                ]
                clip["transition_execution"] = "hard_cut_for_cross_engine_sync"
                clip["transition_to_next"] = "cut"
                clip["transition_duration_sec"] = 0.0
            volume_db = self._finite_float(
                sequence_item.get("volume_db", clip.get("volume_db", 0.0)),
                f"sequence[{index}].volume_db",
            )
            clip["volume_db"] = round(min(12.0, max(-60.0, volume_db)), 2)
            if clip["audio_intent"] == "mute_for_music":
                clip["volume_db"] = -60.0
            smart_reframe = sequence_item.get(
                "smart_reframe", clip.get("smart_reframe", False)
            )
            clip["smart_reframe"] = (
                smart_reframe if isinstance(smart_reframe, bool) else False
            )
            final.append(clip)
        # Preserve the director's exact picture lock. Older code reordered,
        # deduplicated, padded missing beats, and then score-trimmed the model's
        # cut. That produced un-authored shots and destroyed narrative structure.
        # 完整保留导演锁画；禁止 Python 再重排、去重、补镜头或按分数删镜头。
        active_treatment = treatment or self._active_treatment
        global_look = {
            "clean_neutral": "neutral",
            "cinematic_warm": "warm",
            "cool_steel": "cool",
            "high_contrast": "contrast",
        }.get(str(active_treatment.get("creative_look") or ""), "neutral")
        for clip in final:
            clip["color_look"] = global_look
        final_duration = sum(
            float(clip["cut_out_sec"]) - float(clip["cut_in_sec"])
            for clip in final
        )
        target = float(active_treatment.get("target_duration_sec") or 0.0)
        if target > 0 and final_duration > target * 1.10:
            self.logger.warning(
                "导演锁画 %.1fs 超过建议目标 %.1fs；已尊重导演决定，不再由 Python 删镜头 / "
                "Director picture lock %.1fs exceeds suggested %.1fs; preserving it without Python cuts",
                final_duration, target, final_duration, target,
            )
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
            for key in (
                "visual_summary", "subject_action", "reason_for_cut",
                "transcript_excerpt", "dialogue_excerpt",
            )
        ).strip()

    @classmethod
    def _is_production_chatter(cls, item: Dict[str, Any]) -> bool:
        """
        Flag possible recording-process dialogue as advisory evidence only.
        标记可能涉及录制流程的对白；此结果仅供导演参考，不触发自动剪辑。
        """
        text = cls._story_text(item)
        phrases = (
            "microphone", "record sound", "recording sound", "no sound",
            "camera setting", "camera settings", "beauty filter", "skin smoothing",
            "retake", "another take", "blocking", "move the camera", "shoot setup",
            "pretend we are chatting", "pretend to chat", "test shot", "camera operator",
            "smoke machine", "smoke device", "wait five minutes", "on the count",
            "three two one", "3 2 1", "director wants", "photographer wants",
            "麦克风", "录声音", "不需要录", "相机设置", "滤镜", "美颜", "磨皮",
            "重拍", "重来一遍", "走位", "机位", "拍摄设置", "摄影师", "导演要求",
            "假装我们", "假装闲聊", "等五分钟", "煙霧", "烟雾", "试验一下",
            "試驗一下", "拍一条", "拍一條", "倒计时", "倒計時", "321上",
        )
        return any(phrase in text for phrase in phrases)

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
        failures: List[str] = []
        for frame in frames:
            path_text = str(frame.get("image_path") or "").strip()
            if not path_text:
                failures.append(str(frame.get("file_name") or "(missing image_path)"))
                continue
            path = Path(path_text).expanduser()
            if not path.is_file():
                self.logger.warning(
                    "关键帧不存在，跳过：%s / Missing keyframe: %s", path, path
                )
                failures.append(str(path))
                continue
            try:
                data = path.read_bytes()
            except OSError as exc:
                self.logger.warning(
                    "无法读取关键帧 %s：%s / Cannot read keyframe", path, exc
                )
                failures.append(f"{path}: {exc}")
                continue
            available.append(dict(frame))
            encoded.append(base64.b64encode(data).decode("ascii"))
        if failures:
            preview = "; ".join(failures[:5])
            suffix = f" (+{len(failures) - 5})" if len(failures) > 5 else ""
            raise DirectorError(
                "关键帧证据缺失或不可读，不能继续完整审片："
                f"{preview}{suffix} / Missing or unreadable visual evidence; full review aborted."
            )
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
        neutral_evidence: bool = False,
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
            if not neutral_evidence and cut_out - cut_in > maximum_duration:
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
                str(
                    decision.get("reason_for_cut")
                    or decision.get("visual_summary")
                    or decision.get("subject_action")
                    or ""
                ).split()
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
            readability = self._enum_value(
                decision.get("technical_readability"),
                {"clear", "limited", "unreadable"},
                "clear",
            )
            quality_value = self._finite_float(
                decision.get(
                    "quality_score",
                    {"clear": 0.85, "limited": 0.55, "unreadable": 0.2}[readability],
                ),
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
            validated_item = {
                    "file_name": source_name,
                    "cut_in_sec": round(cut_in, 3),
                    "cut_out_sec": round(cut_out, 3),
                    "confidence": round(confidence_value, 3),
                    "quality_score": round(quality_value, 3),
                    "technical_readability": readability,
                    "evidence_type": (
                        "visual_atom" if neutral_evidence else "edit_candidate"
                    ),
                    "visual_summary": " ".join(
                        str(decision.get("visual_summary") or reason).split()
                    ),
                    "subject_action": " ".join(
                        str(decision.get("subject_action") or reason).split()
                    ),
                    "emotion": " ".join(
                        str(
                            decision.get("observable_emotion")
                            or decision.get("emotion")
                            or "observational"
                        ).split()
                    ),
                    "entry_state": " ".join(
                        str(decision.get("entry_state") or "State visible at the first supplied frame.").split()
                    ),
                    "action_apex": " ".join(
                        str(decision.get("action_apex") or decision.get("subject_action") or reason).split()
                    ),
                    "exit_state": " ".join(
                        str(decision.get("exit_state") or "State visible at the last supplied frame.").split()
                    ),
                    "screen_direction": self._enum_value(
                        decision.get("screen_direction"),
                        {"left", "right", "toward_camera", "away_from_camera", "mixed", "none"},
                        "none",
                    ),
                    "identity_tags": [
                        " ".join(str(value).split())
                        for value in (
                            decision.get("identity_tags")
                            if isinstance(decision.get("identity_tags"), list) else []
                        )[:8]
                        if str(value).strip()
                    ],
                    "action_phase": self._enum_value(
                        decision.get("temporal_phase") or decision.get("action_phase"),
                        {
                            "state", "onset", "development", "apex", "reaction",
                            "aftermath", "setup", "build", "action", "payoff",
                        },
                        "development",
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
                                -60.0,
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
            if neutral_evidence:
                # No creative labels or executable finishing choices may leak
                # into the immutable footage ledger.
                for key in (
                    "story_role", "transition_to_next", "transition_duration_sec",
                    "audio_cleanup", "color_look", "motion", "volume_db",
                    "drx_preset", "stabilization", "tracking", "smart_reframe",
                    "rhythmic_potential",
                ):
                    validated_item.pop(key, None)
                validated_item["literal_observation"] = reason
            else:
                validated_item["reason_for_cut"] = reason
            validated.append(validated_item)
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
    parser.add_argument(
        "--timeout",
        type=int,
        default=7200,
        help=(
            "单次 Ollama 请求读取超时秒数 / per-request Ollama read timeout seconds "
            "(default: 7200 for slow 27B/70B mixed-memory inference)"
        ),
    )
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
        "--rough-cut-feedback",
        help=(
            "上一版低清成片的隔离盲审 JSON；用于证据化重剪 / "
            "blind review JSON from the previous rendered rough cut"
        ),
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
        "--reassemble-existing",
        action="store_true",
        help="复用 candidate_audit，仅重跑全局画面/音乐导演 / reuse visual audit and rerun global directing",
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
            rough_cut_feedback=args.rough_cut_feedback,
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
        elif args.reassemble_existing:
            director.reassemble_existing_plan(args.raw_data, args.output)
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
