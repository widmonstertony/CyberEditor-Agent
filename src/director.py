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
import json
import logging
import os
from pathlib import Path
import re
import sys
from typing import Any, Dict, List, Optional, Sequence


LOGGER_NAME = "cybereditor.director"

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
                },
                "required": ["candidate_id"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["project_summary", "sequence"],
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
        base_url: str = "http://localhost:11434",
        chunk_minutes: float = 12.0,
        project_fps: float = 25.0,
        num_ctx: int = 8192,
        timeout_sec: int = 1800,
        merge_gap_sec: float = 0.4,
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

        self.model = model.strip()
        self.base_url = base_url.rstrip("/")
        self.chunk_duration_sec = chunk_minutes * 60.0
        self.project_fps = float(project_fps)
        self.num_ctx = int(num_ctx)
        self.timeout_sec = int(timeout_sec)
        self.merge_gap_sec = float(merge_gap_sec)
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
        for asset in assets:
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
                chunks.append(chunk)

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
            for index, chunk in enumerate(chunks, start=1):
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
                )
                decisions = self.validate_chunk_decisions(
                    response_payload,
                    chunk,
                    str(chunk["source_name"]),
                )
                for decision in decisions:
                    decision["asset_id"] = str(chunk["asset_id"])
                candidates.extend(decisions)

            candidates = self.merge_decisions(candidates)
            candidate_limit = max(24, min(160, self.num_ctx // 128))
            candidates = self._limit_candidates(
                candidates, limit=candidate_limit
            )
            for index, candidate in enumerate(candidates, start=1):
                candidate["candidate_id"] = f"C{index:04d}"
            if not candidates:
                raise DirectorError(
                    "视觉导演未找到任何可用片段 / Visual director found no usable clips."
                )
            sequence_payload = self.request_sequence(candidates, assets)
            final_clips = self.validate_sequence(sequence_payload, candidates)
        finally:
            self.unload_model()

        for index, clip in enumerate(final_clips, start=1):
            clip["clip_id"] = index
        output: Dict[str, Any] = {
            "schema_version": "2.0",
            "generated_at_utc": datetime.now(timezone.utc).isoformat(),
            "project_fps": self.project_fps,
            "source_raw_data": str(raw_path),
            "director_model": self.model,
            "chunk_duration_sec": self.chunk_duration_sec,
            "asset_count": len(assets),
            "candidate_count": len(candidates),
            "project_summary": str(sequence_payload.get("project_summary") or "").strip(),
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

    def check_ollama(self, require_vision: bool = False) -> None:
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
        requested_base = self.model.split(":", 1)[0]
        if self.model not in names and not any(
            name.split(":", 1)[0] == requested_base for name in names
        ):
            available = ", ".join(sorted(name for name in names if name)) or "(none)"
            raise DirectorError(
                f"Ollama 中未找到模型 {self.model!r}。请先执行：ollama pull {self.model}\n"
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
            and name != self.model
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
        prompt = self.build_prompt(prompt_chunk, source_name, active_schema)
        return self._request_json(prompt, active_schema, images)

    def _request_json(
        self,
        prompt: str,
        schema: Dict[str, Any],
        images: Sequence[str] = (),
    ) -> Dict[str, Any]:
        """
        Send one schema-constrained Ollama request with optional real images.
        发送一次受 Schema 约束、可包含真实图片的 Ollama 请求。
        """
        payload: Dict[str, Any] = {
            "model": self.model,
            "system": (
                "You are a senior documentary editor and visual storyteller. "
                "Use only supplied transcript, timestamps, and images. Return "
                "only the requested JSON and never invent content."
            ),
            "prompt": prompt,
            "stream": False,
            "format": schema,
            "think": "high",
            "keep_alive": "10m",
            "options": {
                "temperature": 0,
                "seed": 42,
                "num_ctx": self.num_ctx,
            },
        }
        if images:
            payload["images"] = list(images)
        url = self.base_url + "/api/generate"
        try:
            response = self.session.post(
                url, json=payload, timeout=(10, self.timeout_sec)
            )
            if getattr(response, "status_code", 0) == 400:
                self.logger.warning(
                    "Ollama 拒绝高强度思考参数，使用兼容模式重试 / Retrying without high thinking"
                )
                payload.pop("think", None)
                response = self.session.post(
                    url, json=payload, timeout=(10, self.timeout_sec)
                )
            # Older Ollama builds accept "json" but not a schema object.
            if getattr(response, "status_code", 0) == 400:
                self.logger.warning(
                    "Ollama 拒绝 JSON Schema，回退到 format=json / Falling back to format=json"
                )
                payload["format"] = "json"
                response = self.session.post(
                    url, json=payload, timeout=(10, self.timeout_sec)
                )
            response.raise_for_status()
            envelope = response.json()
        except Exception as exc:
            raise DirectorError(
                f"Ollama 分块请求失败 / Ollama chunk request failed: {exc}"
            ) from exc
        if not isinstance(envelope, dict) or not envelope.get("done", False):
            raise DirectorError(
                "Ollama 返回未完成响应 / Ollama returned an incomplete response."
            )
        generated = envelope.get("response")
        if not isinstance(generated, str) or not generated.strip():
            raise DirectorError(
                "Ollama response 字段为空 / Ollama response field is empty."
            )
        return self.parse_generated_json(generated)

    def build_prompt(
        self,
        chunk: Dict[str, Any],
        source_name: str,
        schema: Optional[Dict[str, Any]] = None,
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
        return (
            "Analyze only this source-video window: "
            "{start:.3f}s to {end:.3f}s.\n"
            "Select coherent ranges worth keeping as documentary candidates. Use "
            "absolute seconds in this source, not time relative to the chunk. "
            "Inspect every attached image in the listed IMAGE order. Prefer complete "
            "sentences, expressive visuals, stable/focused shots, meaningful B-roll, "
            "and authentic moments; reject dead air, repetition, camera setup, severe "
            "shake, accidental frames, and unusable audio. Suggest restrained effects "
            "only from the schema enums. Strong audio cleanup is for visibly/noisily "
            "problematic speech; transitions should serve the story, not decorate it. "
            "Every range must remain inside this window and cut_out_sec must be "
            "greater than cut_in_sec. An empty decisions array is allowed.\n"
            "Source/proxy media: {source}\n"
            "Required JSON schema: {schema}\n\n"
            "TRANSCRIPT:\n{transcript}\n\nKEYFRAMES:\n{keyframes}"
        ).format(
            start=float(chunk["start_sec"]),
            end=float(chunk["end_sec"]),
            source=source_name,
            schema=schema_text,
            transcript="\n".join(transcript_lines) or "(none)",
            keyframes="\n".join(keyframe_lines) or "(none)",
        )

    def request_sequence(
        self,
        candidates: Sequence[Dict[str, Any]],
        assets: Sequence[Dict[str, Any]],
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
                }
            )
        asset_names = [
            {
                "asset_id": asset.get("asset_id", ""),
                "file": Path(str(asset.get("source_video") or "")).name,
                "duration_sec": asset.get("duration_sec", 0),
            }
            for asset in assets
        ]
        prompt = (
            "You have already inspected representative frames and transcripts "
            "from every source video. Build one coherent documentary edit from "
            "the candidate list below. Select only useful candidate_id values, "
            "never invent or duplicate an id, and order them for narrative flow "
            "rather than source-file order. Establish context, develop the story, "
            "use B-roll to cover or bridge speech, avoid repetitive points, and "
            "finish deliberately. Preserve complete thoughts. Choose restrained "
            "transitions and effects from the schema; default to hard cuts, use "
            "cross dissolves for genuine time/mood changes, and fade_black only "
            "for major chapter endings. Return JSON only.\n"
            f"Required JSON schema: {json.dumps(SEQUENCE_SCHEMA, ensure_ascii=False)}\n"
            f"ASSETS:\n{json.dumps(asset_names, ensure_ascii=False)}\n"
            f"CANDIDATES:\n{json.dumps(compact_candidates, ensure_ascii=False)}"
        )
        self.logger.info(
            "正在进行跨素材全局编排（%d 个候选）/ Global story assembly (%d candidates)",
            len(candidates),
            len(candidates),
        )
        return self._request_json(prompt, SEQUENCE_SCHEMA)

    def validate_sequence(
        self,
        payload: Dict[str, Any],
        candidates: Sequence[Dict[str, Any]],
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
            final.append(clip)
        return final

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
            story_role = self._enum_value(
                decision.get("story_role"),
                {
                    "opening",
                    "context",
                    "interview",
                    "broll",
                    "bridge",
                    "climax",
                    "closing",
                },
                "context",
            )
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
                }
            )
        return validated

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
            touches = (
                float(current["cut_in_sec"])
                <= float(previous["cut_out_sec"]) + self.merge_gap_sec
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
            else:
                merged.append(current)
        return merged

    def unload_model(self) -> None:
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
        try:
            response = self.session.post(
                self.base_url + "/api/generate",
                json={
                    "model": self.model,
                    "prompt": "",
                    "stream": False,
                    "keep_alive": 0,
                },
                timeout=(5, 60),
            )
            response.raise_for_status()
            self.logger.info(
                "已请求 Ollama 卸载模型 %s / Requested Ollama unload for %s",
                self.model,
                self.model,
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
    parser.add_argument("--proxy-file-name")
    parser.add_argument("--ollama-url", default="http://localhost:11434")
    parser.add_argument("--chunk-minutes", type=float, default=12.0)
    parser.add_argument("--project-fps", type=float, default=25.0)
    parser.add_argument("--num-ctx", type=int, default=8192)
    parser.add_argument("--timeout", type=int, default=1800)
    parser.add_argument("--merge-gap", type=float, default=0.4)
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
            base_url=args.ollama_url,
            chunk_minutes=args.chunk_minutes,
            project_fps=args.project_fps,
            num_ctx=args.num_ctx,
            timeout_sec=args.timeout,
            merge_gap_sec=args.merge_gap,
            logger=logger,
        )
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
