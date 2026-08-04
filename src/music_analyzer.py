#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Rights-aware music acquisition and CPU musical analysis.
带版权来源审计的配乐获取与 CPU 音乐听诊。

The module intentionally uses no CUDA. Local files, Jamendo downloads, and an
explicitly gated yt-dlp mode all converge on the same analyzed-candidate JSON.
本模块刻意不使用 CUDA；本地曲库、Jamendo 与显式授权后的 yt-dlp 模式最终都会
生成同一种候选曲目分析 JSON。
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
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
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple
from urllib import error as urllib_error
from urllib import parse as urllib_parse
from urllib import request as urllib_request


LOGGER_NAME = "cybereditor.music"
AUDIO_SUFFIXES = {".wav", ".flac", ".mp3", ".m4a", ".aac", ".ogg", ".opus"}

# Search providers return every kind of video, not only music.  Keep this
# deliberately conservative: a false negative merely removes one candidate,
# while a false positive can put spoken English under the finished film.
# 搜索平台返回的不只有音乐。这里宁可少一个候选，也不能把访谈/解说混进成片。
SPEECH_RISK_TERMS = (
    "interview", "podcast", "conversation", "spoken word", "speech",
    "audiobook", "news", "tutorial", "reaction", "review", "vlog",
    "shows off", "collected |", "explains", "talks about", "q&a",
    "访谈", "采访", "播客", "解说", "教程", "评测", "新闻", "有声书",
)
MUSIC_SIGNAL_TERMS = (
    "instrumental", "music", "score", "soundtrack", "bgm", "background music",
    "cinematic", "ambient", "beat", "track", "synthwave", "orchestral",
    "royalty free", "no copyright", "underscore", "theme", "ost",
    "纯音乐", "配乐", "原声", "音乐", "伴奏", "氛围音乐", "电影音乐",
)


class MusicAnalysisError(RuntimeError):
    """Expected music-pipeline failure. / 可预期的配乐流水线错误。"""


def atomic_write_json(payload: Dict[str, Any], destination: Path) -> None:
    """Atomically write one UTF-8 JSON artifact. / 原子写入一份 UTF-8 JSON 产物。"""
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_suffix(destination.suffix + ".tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    os.replace(str(temporary), str(destination))


def sha256_file(path: Path) -> str:
    """Hash one downloaded or user-supplied audio file. / 计算音频文件 SHA-256。"""
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def load_music_brief(path: Optional[os.PathLike]) -> Dict[str, Any]:
    """Read an optional first-director ``music_brief.json``. / 读取可选的第一次导演音乐简报。"""
    if not path:
        return {}
    source = Path(path).expanduser().resolve()
    try:
        payload = json.loads(source.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError) as exc:
        raise MusicAnalysisError(
            f"无法读取 music_brief.json / Cannot read music brief: {exc}"
        ) from exc
    if not isinstance(payload, dict):
        raise MusicAnalysisError("music_brief.json 根节点必须是对象 / root must be an object.")
    return payload


class MusicCandidateAcquirer:
    """
    Acquire auditable candidates from a chosen provider without using CUDA.
    从指定来源获取可审计的候选曲目，且不使用 CUDA。
    """

    def __init__(self, cache_dir: os.PathLike, logger: Optional[logging.Logger] = None) -> None:
        """Prepare a project-local download cache. / 准备项目本地下载缓存。"""
        self.cache_dir = Path(cache_dir).expanduser().resolve()
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.logger = logger or logging.getLogger(LOGGER_NAME)

    @staticmethod
    def _queries(brief: Dict[str, Any], fallback: str) -> List[str]:
        """Return deduplicated director search queries. / 返回去重后的导演检索词。"""
        raw = brief.get("search_queries")
        queries = [
            " ".join(str(value).split())
            for value in (raw if isinstance(raw, list) else [])
            if str(value).strip()
        ]
        if not queries and fallback.strip():
            queries = [" ".join(fallback.split())]
        if not queries:
            queries = ["cinematic documentary instrumental emotional arc"]
        result: List[str] = []
        seen = set()
        instrumental_only = (
            str(brief.get("vocal_policy") or "").casefold() == "instrumental_only"
        )
        for query in queries:
            if instrumental_only and not any(
                token in query.casefold() for token in ("instrumental", "纯音乐", "no vocal")
            ):
                query = f"{query} instrumental background music no vocals"
            key = query.casefold()
            if key not in seen:
                seen.add(key)
                result.append(query)
        return result[:6]

    @staticmethod
    def _classify_search_entry(entry: Dict[str, Any]) -> Dict[str, bool]:
        """Classify obvious speech/video results before download. / 下载前识别明显的人声节目结果。"""
        categories = entry.get("categories") if isinstance(entry.get("categories"), list) else []
        tags = entry.get("tags") if isinstance(entry.get("tags"), list) else []
        haystack = " ".join(
            str(value or "")
            for value in (
                entry.get("title"), entry.get("description"), entry.get("uploader"),
                entry.get("channel"), " ".join(map(str, categories)),
                " ".join(map(str, tags)),
            )
        ).casefold()
        return {
            "speech_risk": any(term in haystack for term in SPEECH_RISK_TERMS),
            "instrumental_match": any(term in haystack for term in MUSIC_SIGNAL_TERMS),
        }

    def acquire_ytdlp(
        self,
        brief: Dict[str, Any],
        fallback_query: str,
        limit: int,
        rights_confirmed: bool,
        rights_claim: str,
    ) -> Path:
        """
        Search/download arbitrary supported audio only after explicit confirmation.
        仅在用户明确确认权利与平台条款后，搜索并下载任意受支持音频。

        Parameters / 参数:
            brief: First-director music brief. / 第一次导演音乐简报。
            fallback_query: CLI fallback query. / 命令行兜底检索词。
            limit: Maximum downloaded candidates. / 最大下载候选数。
            rights_confirmed: Explicit per-run consent gate. / 每次运行的显式确认门。
            rights_claim: User's rights/permission statement. / 用户的权利或许可声明。
        """
        if not rights_confirmed or not rights_claim.strip():
            raise MusicAnalysisError(
                "任意在线音频模式已阻止：必须确认你拥有下载、改编和使用这些音频的权利，"
                "并遵守来源平台条款；商用还需取得商用授权。警告本身不会授予版权。 / "
                "Arbitrary online audio is blocked until you confirm download, adaptation, "
                "and usage rights plus platform-term compliance. A warning grants no rights."
            )
        try:
            import yt_dlp  # noqa: F401
        except ImportError as exc:
            raise MusicAnalysisError(
                "缺少 yt-dlp。请运行 pip install -r requirements.txt。 / yt-dlp is missing."
            ) from exc

        queries = self._queries(brief, fallback_query)
        metadata: List[Dict[str, Any]] = []
        seen_urls = set()
        per_query = max(3, min(8, int(math.ceil(limit * 1.8 / len(queries)))))
        for query in queries:
            command = [
                sys.executable,
                "-m",
                "yt_dlp",
                "--dump-single-json",
                "--flat-playlist",
                "--no-warnings",
                "--playlist-end",
                str(per_query),
                f"ytsearch{per_query}:{query}",
            ]
            completed = subprocess.run(
                command,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
                check=False,
            )
            if completed.returncode != 0:
                self.logger.warning(
                    "在线检索失败：%s / Online search failed: %s",
                    query,
                    completed.stderr.strip(),
                )
                continue
            try:
                envelope = json.loads(completed.stdout)
            except json.JSONDecodeError:
                continue
            entries = envelope.get("entries", []) if isinstance(envelope, dict) else []
            for entry in entries if isinstance(entries, list) else []:
                if not isinstance(entry, dict):
                    continue
                url = str(entry.get("webpage_url") or entry.get("url") or "").strip()
                identifier = str(entry.get("id") or "").strip()
                if url and not url.startswith(("http://", "https://")) and identifier:
                    url = f"https://www.youtube.com/watch?v={identifier}"
                if not url or url in seen_urls:
                    continue
                try:
                    duration = float(entry.get("duration") or 0)
                except (TypeError, ValueError):
                    duration = 0.0
                # Avoid downloading hour-long mixes as individual candidates.
                # 避免把数小时合集当作单条候选下载。
                if duration and not 45.0 <= duration <= 1200.0:
                    continue
                classification = self._classify_search_entry(entry)
                if classification["speech_risk"]:
                    self.logger.info(
                        "跳过疑似访谈/解说：%s / Skipping likely spoken-word result",
                        entry.get("title") or url,
                    )
                    continue
                if (
                    str(brief.get("vocal_policy") or "").casefold() == "instrumental_only"
                    and not classification["instrumental_match"]
                ):
                    self.logger.info(
                        "跳过无法确认是纯音乐的结果：%s / Skipping result without a music signal",
                        entry.get("title") or url,
                    )
                    continue
                seen_urls.add(url)
                item = dict(entry)
                item["source_url"] = url
                item["search_query"] = query
                item.update(classification)
                metadata.append(item)

        if not metadata:
            raise MusicAnalysisError(
                "在线检索没有返回候选。请检查网络与 yt-dlp。 / Online search returned no candidates."
            )
        manifest_tracks: List[Dict[str, Any]] = []
        for index, item in enumerate(metadata[: max(1, min(12, limit))], start=1):
            url = str(item["source_url"])
            self.logger.info(
                "下载候选配乐 %d/%d：%s / Downloading music candidate",
                index,
                min(len(metadata), limit),
                item.get("title") or url,
            )
            command = [
                sys.executable,
                "-m",
                "yt_dlp",
                "--no-playlist",
                "--no-warnings",
                "--extract-audio",
                "--audio-format",
                "wav",
                "--audio-quality",
                "0",
                "--print",
                "after_move:filepath",
                "--output",
                str(self.cache_dir / "%(id)s.%(ext)s"),
                url,
            ]
            completed = subprocess.run(
                command,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
                check=False,
            )
            if completed.returncode != 0:
                self.logger.warning(
                    "候选下载失败，已跳过：%s / Candidate download failed: %s",
                    url,
                    completed.stderr.strip(),
                )
                continue
            output_lines = [line.strip() for line in completed.stdout.splitlines() if line.strip()]
            downloaded = Path(output_lines[-1]).expanduser().resolve() if output_lines else Path()
            if not downloaded.is_file():
                identifier = str(item.get("id") or "")
                downloaded = self.cache_dir / f"{identifier}.wav"
            if not downloaded.is_file():
                continue
            manifest_tracks.append({
                "file": downloaded.name,
                "title": str(item.get("title") or downloaded.stem),
                "artist": str(item.get("artist") or item.get("uploader") or item.get("channel") or ""),
                "mood": str(item.get("search_query") or ""),
                "tags": [str(item.get("search_query") or "")],
                "license": "user-confirmed rights; verify source terms",
                "license_url": "",
                "licensed": False,
                "license_provenance": "user_confirmation",
                "source_url": url,
                "provider": "yt_dlp_unverified",
                "speech_risk": bool(item.get("speech_risk")),
                "instrumental_match": bool(item.get("instrumental_match")),
                "sha256": sha256_file(downloaded),
                "rights_claim": rights_claim.strip(),
            })
        if not manifest_tracks:
            raise MusicAnalysisError(
                "候选音频均下载失败 / Every candidate audio download failed."
            )
        atomic_write_json(
            {"managed_provider_cache": True, "tracks": manifest_tracks},
            self.cache_dir / "library.json",
        )
        audit = {
            "schema_version": "1.0",
            "provider": "yt_dlp_unverified",
            "confirmed_at_utc": datetime.now(timezone.utc).isoformat(),
            "warning": (
                "User confirmation is an audit record, not a license. The user must "
                "hold all required rights and comply with each source platform's terms."
            ),
            "rights_claim": rights_claim.strip(),
            "sources": [
                {key: item.get(key, "") for key in (
                    "file", "title", "artist", "source_url", "sha256", "license"
                )}
                for item in manifest_tracks
            ],
        }
        atomic_write_json(audit, self.cache_dir / "rights_audit.json")
        return self.cache_dir

    def acquire_jamendo(
        self,
        brief: Dict[str, Any],
        fallback_query: str,
        limit: int,
        client_id: str,
    ) -> Path:
        """
        Download audiodownload-enabled Jamendo candidates with license URLs.
        下载 Jamendo 明确允许下载且带许可证链接的候选曲目。
        """
        if not client_id.strip():
            raise MusicAnalysisError(
                "Jamendo 模式需要 API client_id / Jamendo provider requires an API client_id."
            )
        queries = self._queries(brief, fallback_query)
        license_intent = str(brief.get("license_intent") or "commercial_safe")
        manifest_tracks: List[Dict[str, Any]] = []
        seen_ids = set()
        for query in queries:
            parameters = urllib_parse.urlencode({
                "client_id": client_id.strip(),
                "format": "json",
                "limit": max(3, min(10, limit)),
                "search": query,
                "include": "musicinfo",
                "audioformat": "flac",
                "order": "relevance",
            })
            url = "https://api.jamendo.com/v3.0/tracks/?" + parameters
            try:
                with urllib_request.urlopen(url, timeout=45) as response:
                    envelope = json.loads(response.read().decode("utf-8"))
            except (OSError, ValueError, urllib_error.URLError) as exc:
                raise MusicAnalysisError(f"Jamendo 检索失败 / search failed: {exc}") from exc
            results = envelope.get("results", []) if isinstance(envelope, dict) else []
            for item in results if isinstance(results, list) else []:
                if not isinstance(item, dict) or not item.get("audiodownload_allowed"):
                    continue
                track_id = str(item.get("id") or "")
                if not track_id or track_id in seen_ids:
                    continue
                license_url = str(item.get("license_ccurl") or "")
                normalized_license = license_url.casefold()
                if "nd" in normalized_license:
                    continue
                if license_intent == "commercial_safe" and "nc" in normalized_license:
                    continue
                download_url = str(item.get("audiodownload") or "")
                if not download_url:
                    continue
                extension = ".flac" if "flac" in download_url.casefold() else ".mp3"
                destination = self.cache_dir / f"jamendo_{track_id}{extension}"
                if not destination.is_file():
                    try:
                        with urllib_request.urlopen(download_url, timeout=120) as response:
                            destination.write_bytes(response.read())
                    except (OSError, urllib_error.URLError) as exc:
                        self.logger.warning("Jamendo 下载失败 %s：%s", track_id, exc)
                        continue
                seen_ids.add(track_id)
                manifest_tracks.append({
                    "file": destination.name,
                    "title": str(item.get("name") or destination.stem),
                    "artist": str(item.get("artist_name") or ""),
                    "mood": query,
                    "tags": [query],
                    "license": license_url or "Jamendo license; verify project terms",
                    "license_url": license_url,
                    "licensed": True,
                    "license_provenance": "jamendo_api",
                    "source_url": str(item.get("shareurl") or ""),
                    "provider": "jamendo",
                    "sha256": sha256_file(destination),
                })
                if len(manifest_tracks) >= limit:
                    break
            if len(manifest_tracks) >= limit:
                break
        if not manifest_tracks:
            raise MusicAnalysisError(
                "Jamendo 未找到符合当前授权策略的候选 / No Jamendo candidates match the license policy."
            )
        atomic_write_json({"tracks": manifest_tracks}, self.cache_dir / "library.json")
        atomic_write_json(
            {
                "schema_version": "1.0",
                "provider": "jamendo",
                "generated_at_utc": datetime.now(timezone.utc).isoformat(),
                "sources": manifest_tracks,
            },
            self.cache_dir / "rights_audit.json",
        )
        return self.cache_dir


class LicensedMusicAnalyzer:
    """
    Discover user-authorized audio and extract editorial music features on CPU.
    查找用户授权的音频，并在 CPU 上提取剪辑所需的音乐特征。
    """

    def __init__(self, library: os.PathLike, logger: Optional[logging.Logger] = None) -> None:
        """Validate a music-library root without importing librosa. / 校验曲库根目录，但暂不导入 librosa。"""
        self.library = Path(library).expanduser().resolve()
        if not self.library.is_dir():
            raise MusicAnalysisError(f"配乐目录不存在 / Music library not found: {self.library}")
        self.logger = logger or logging.getLogger(LOGGER_NAME)
        self._manifest = self._load_manifest()

    def _load_manifest(self) -> Dict[str, Dict[str, Any]]:
        """Load optional rights metadata keyed by relative path. / 按相对路径读取可选权利元数据。"""
        path = self.library / "library.json"
        if not path.is_file():
            return {}
        try:
            value = json.loads(path.read_text(encoding="utf-8-sig"))
        except (OSError, json.JSONDecodeError) as exc:
            raise MusicAnalysisError(f"无法解析 library.json / Cannot parse: {exc}") from exc
        tracks = value.get("tracks") if isinstance(value, dict) else value
        if not isinstance(tracks, list):
            raise MusicAnalysisError("library.json 必须包含 tracks 数组 / requires a tracks array.")
        result: Dict[str, Dict[str, Any]] = {}
        for item in tracks:
            if not isinstance(item, dict) or not str(item.get("file") or "").strip():
                continue
            key = str(Path(str(item["file"]))).replace("\\", "/").casefold()
            result[key] = dict(item)
        return result

    def discover(self) -> List[Dict[str, Any]]:
        """Discover files and attach honest rights provenance. / 查找音频并附加真实权利来源。"""
        tracks: List[Dict[str, Any]] = []
        manifest_exists = (self.library / "library.json").is_file()
        for path in sorted(self.library.rglob("*"), key=lambda p: str(p).casefold()):
            if not path.is_file() or path.suffix.casefold() not in AUDIO_SUFFIXES:
                continue
            relative = path.relative_to(self.library)
            relative_key = str(relative).replace("\\", "/").casefold()
            # A manifest makes this a managed candidate cache.  Files left by
            # older searches are not user-supplied music and must not silently
            # bypass the current candidate list.
            # 有清单时即视为受管缓存；旧搜索残留文件不能冒充用户本地音乐。
            if manifest_exists and relative_key not in self._manifest:
                self.logger.info(
                    "忽略未登记的旧配乐缓存：%s / Ignoring unmanifested stale audio cache",
                    relative,
                )
                continue
            metadata = self._manifest.get(relative_key, {})
            classification = MusicCandidateAcquirer._classify_search_entry(metadata)
            speech_risk = bool(metadata.get("speech_risk", classification["speech_risk"]))
            if speech_risk:
                self.logger.warning(
                    "拒绝疑似访谈/解说候选：%s / Rejecting likely spoken-word candidate",
                    metadata.get("title") or relative,
                )
                continue
            explicitly_licensed = bool(metadata.get("licensed"))
            tracks.append({
                "file_name": str(path),
                "relative_file": str(relative),
                "title": str(metadata.get("title") or path.stem),
                "artist": str(metadata.get("artist") or ""),
                "tags": [str(v) for v in metadata.get("tags", []) if str(v).strip()]
                if isinstance(metadata.get("tags"), list) else [],
                "mood": str(metadata.get("mood") or ""),
                "license": str(metadata.get("license") or (
                    "manifest-approved" if explicitly_licensed else "user-supplied"
                )),
                "license_url": str(metadata.get("license_url") or ""),
                "license_provenance": str(metadata.get("license_provenance") or (
                    "manifest" if explicitly_licensed else "user_supplied"
                )),
                "source_url": str(metadata.get("source_url") or ""),
                "provider": str(metadata.get("provider") or "local"),
                "speech_risk": speech_risk,
                "instrumental_match": bool(
                    metadata.get("instrumental_match", classification["instrumental_match"])
                ),
                "sha256": str(metadata.get("sha256") or sha256_file(path)),
                "rights_claim": str(metadata.get("rights_claim") or ""),
            })
        return tracks

    @staticmethod
    def rank(tracks: Iterable[Dict[str, Any]], query: str) -> List[Dict[str, Any]]:
        """Rank candidates by multilingual keyword overlap. / 按多语言关键词重合度排列候选。"""
        terms = {
            part.casefold() for part in re.split(r"[,;，；\s]+", str(query))
            if part.strip()
        }
        ranked: List[Dict[str, Any]] = []
        for track in tracks:
            mood = str(track.get("mood") or "").casefold()
            haystack = " ".join([
                str(track.get("title") or ""), str(track.get("artist") or ""),
                mood, " ".join(track.get("tags") or []),
            ]).casefold()
            score = sum(2 if term in mood else 1 for term in terms if term in haystack)
            item = dict(track)
            item["search_score"] = score
            ranked.append(item)
        return sorted(
            ranked,
            key=lambda item: (-int(item["search_score"]), str(item["file_name"]).casefold()),
        )

    @staticmethod
    def _estimate_key(chroma: Any, np: Any) -> Tuple[str, str, float]:
        """Estimate key/mode with Krumhansl pitch profiles. / 使用 Krumhansl 音高模板估计调性。"""
        names = ("C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B")
        major = np.asarray([6.35, 2.23, 3.48, 2.33, 4.38, 4.09, 2.52, 5.19, 2.39, 3.66, 2.29, 2.88])
        minor = np.asarray([6.33, 2.68, 3.52, 5.38, 2.60, 3.53, 2.54, 4.75, 3.98, 2.69, 3.34, 3.17])
        profile = np.mean(chroma, axis=1)
        if not np.any(profile):
            return "", "", 0.0
        scores: List[Tuple[float, str, str]] = []
        for tonic in range(12):
            for mode, template in (("major", major), ("minor", minor)):
                rolled = np.roll(template, tonic)
                score = float(np.corrcoef(profile, rolled)[0, 1])
                if math.isfinite(score):
                    scores.append((score, names[tonic], mode))
        if not scores:
            return "", "", 0.0
        score, key, mode = max(scores)
        return key, mode, round(max(0.0, min(1.0, (score + 1.0) / 2.0)), 3)

    @staticmethod
    def _ffmpeg_lufs(path: str) -> Optional[float]:
        """Measure integrated EBU R128 loudness with FFmpeg. / 使用 FFmpeg 测量 EBU R128 综合响度。"""
        executable = shutil.which("ffmpeg")
        if not executable:
            return None
        completed = subprocess.run(
            [executable, "-hide_banner", "-nostats", "-i", path, "-af", "ebur128=peak=true", "-f", "null", "-"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
        )
        matches = re.findall(r"\bI:\s*(-?\d+(?:\.\d+)?)\s+LUFS", completed.stderr)
        return round(float(matches[-1]), 2) if matches else None

    def analyze_track(self, track: Dict[str, Any]) -> Dict[str, Any]:
        """
        Extract beats, strong beats, approximate downbeats, sections, key and LUFS.
        提取节拍、强拍、近似小节首拍、段落、调性与 LUFS。

        Parameters / 参数:
            track: One record returned by :meth:`discover`. / ``discover`` 返回的一条曲目。
        """
        try:
            import librosa
            import numpy as np
        except ImportError as exc:
            raise MusicAnalysisError(
                "缺少 librosa。请运行 pip install -r requirements.txt。 / librosa is missing."
            ) from exc
        path = str(track["file_name"])
        try:
            audio, sample_rate = librosa.load(path, sr=22050, mono=True)
            if not len(audio):
                raise ValueError("empty audio")
            hop = 512
            onset = librosa.onset.onset_strength(y=audio, sr=sample_rate, hop_length=hop)
            tempo, beat_frames = librosa.beat.beat_track(
                onset_envelope=onset, sr=sample_rate, hop_length=hop
            )
            beat_times = librosa.frames_to_time(beat_frames, sr=sample_rate, hop_length=hop)
            duration = float(librosa.get_duration(y=audio, sr=sample_rate))
            tempo_value = float(np.asarray(tempo).reshape(-1)[0]) if np.asarray(tempo).size else 0.0
            strengths = np.asarray([
                float(onset[int(frame)]) if 0 <= int(frame) < len(onset) else 0.0
                for frame in beat_frames
            ])
            threshold = float(np.percentile(strengths, 65)) if strengths.size else 0.0
            strong_indexes = [index for index, strength in enumerate(strengths) if strength >= threshold]
            strong_beats = [float(beat_times[index]) for index in strong_indexes]
            if len(beat_times) >= 4:
                phase = max(
                    range(4),
                    key=lambda candidate: float(np.sum(strengths[candidate::4])),
                )
                downbeats = [float(value) for value in beat_times[phase::4]]
            else:
                downbeats = [float(value) for value in beat_times]
            rms = librosa.feature.rms(y=audio, hop_length=hop)[0]
            rms_db = librosa.amplitude_to_db(np.maximum(rms, 1e-8), ref=1.0)
            rms_times = librosa.frames_to_time(np.arange(len(rms_db)), sr=sample_rate, hop_length=hop)
            stride = max(1, int(math.ceil(len(rms_db) / 240)))
            energy_curve = [
                {"time_sec": round(float(rms_times[index]), 3), "dbfs": round(float(rms_db[index]), 2)}
                for index in range(0, len(rms_db), stride)
            ]
            smooth_frames = max(3, int(2.0 * sample_rate / hop))
            kernel = np.ones(smooth_frames) / smooth_frames
            smoothed = np.convolve(rms_db, kernel, mode="same")
            novelty = np.abs(np.diff(smoothed, prepend=smoothed[0]))
            wanted = max(1, min(10, int(duration // 35)))
            candidates = np.argsort(novelty)[::-1]
            boundaries = [0.0, duration]
            for frame in candidates:
                timestamp = float(rms_times[min(int(frame), len(rms_times) - 1)])
                if 8.0 <= timestamp <= duration - 8.0 and all(
                    abs(timestamp - existing) >= 10.0 for existing in boundaries
                ):
                    boundaries.append(timestamp)
                    if len(boundaries) >= wanted + 2:
                        break
            boundaries = sorted(boundaries)
            median_energy = float(np.median(rms_db))
            sections: List[Dict[str, Any]] = []
            for start, end in zip(boundaries, boundaries[1:]):
                mask = (rms_times >= start) & (rms_times < end)
                mean_energy = float(np.mean(rms_db[mask])) if np.any(mask) else median_energy
                sections.append({
                    "start_sec": round(start, 3),
                    "end_sec": round(end, 3),
                    "energy": "high" if mean_energy > median_energy + 2 else (
                        "low" if mean_energy < median_energy - 2 else "medium"
                    ),
                    "mean_dbfs": round(mean_energy, 2),
                })
            chroma = librosa.feature.chroma_stft(y=audio, sr=sample_rate, hop_length=hop)
            key, mode, key_confidence = self._estimate_key(chroma, np)
            finite_rms = rms_db[np.isfinite(rms_db)]
            dynamic_range = float(np.percentile(finite_rms, 95) - np.percentile(finite_rms, 10))
        except Exception as exc:
            raise MusicAnalysisError(
                f"配乐听诊失败 / Music analysis failed for {path}: {exc}"
            ) from exc
        result = dict(track)
        result.update({
            "duration_sec": round(duration, 3),
            "tempo_bpm": round(tempo_value, 3) if math.isfinite(tempo_value) else 0.0,
            "beats_sec": [round(float(value), 4) for value in beat_times if math.isfinite(float(value))],
            "strong_beats_sec": [round(value, 4) for value in strong_beats],
            "downbeats_sec": [round(value, 4) for value in downbeats],
            "sections": sections,
            "energy_curve": energy_curve,
            "key": key,
            "mode": mode,
            "key_confidence": key_confidence,
            "integrated_lufs": self._ffmpeg_lufs(path),
            "dynamic_range_db": round(dynamic_range, 2),
            "analysis_engine": "librosa+ffmpeg-cpu-v2",
            "downbeat_method": "four-beat-onset-phase-estimate",
        })
        return result

    def run(
        self,
        output: os.PathLike,
        query: str = "",
        limit: int = 8,
        brief: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """Analyze the best candidates and atomically publish JSON. / 分析最佳候选并原子发布 JSON。"""
        brief = brief or {}
        combined_query = " ".join([
            query,
            str(brief.get("mood") or ""),
            str(brief.get("emotion_arc") or ""),
            " ".join(brief.get("instrumentation") or [])
            if isinstance(brief.get("instrumentation"), list) else "",
        ]).strip()
        tracks = self.rank(self.discover(), combined_query)
        if not tracks:
            raise MusicAnalysisError(f"曲库中没有受支持的音频 / No supported audio in: {self.library}")
        analyzed: List[Dict[str, Any]] = []
        maximum = max(1, min(32, int(limit)))
        for index, track in enumerate(tracks[:maximum], start=1):
            self.logger.info(
                "CPU 音乐听诊 %d/%d：%s / CPU music analysis",
                index, min(len(tracks), maximum), track["title"],
            )
            analyzed.append(self.analyze_track(track))
        tempo = brief.get("tempo_bpm") if isinstance(brief.get("tempo_bpm"), dict) else {}
        tempo_min = float(tempo.get("min", 0) or 0)
        tempo_max = float(tempo.get("max", 999) or 999)
        for item in analyzed:
            bpm = float(item.get("tempo_bpm", 0) or 0)
            item["director_match"] = {
                "tempo_in_range": tempo_min <= bpm <= tempo_max,
                "keyword_score": int(item.get("search_score", 0)),
            }
        analyzed.sort(
            key=lambda item: (
                not bool(item["director_match"]["tempo_in_range"]),
                -int(item["director_match"]["keyword_score"]),
            )
        )
        payload = {
            "schema_version": "2.0",
            "generated_at_utc": datetime.now(timezone.utc).isoformat(),
            "library_root": str(self.library),
            "query": combined_query,
            "music_brief": brief,
            "rights_audit_file": str(self.library / "rights_audit.json")
            if (self.library / "rights_audit.json").is_file() else "",
            "tracks": analyzed,
        }
        atomic_write_json(payload, Path(output).expanduser().resolve())
        return payload


def configure_logging(level: str) -> logging.Logger:
    """Configure standalone bilingual logging. / 配置独立双语日志。"""
    logging.basicConfig(
        level=getattr(logging, level.upper()),
        format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    )
    return logging.getLogger(LOGGER_NAME)


def build_parser() -> argparse.ArgumentParser:
    """Create music acquisition/analysis CLI arguments. / 创建配乐获取与分析命令行参数。"""
    parser = argparse.ArgumentParser(description="Acquire and analyze auditable music candidates.")
    parser.add_argument("--provider", choices=("local", "jamendo", "yt_dlp"), default="local")
    parser.add_argument("--library")
    parser.add_argument("--cache-dir", default="data/music-cache")
    parser.add_argument("--brief")
    parser.add_argument("--output", required=True)
    parser.add_argument("--query", default="")
    parser.add_argument("--limit", type=int, default=8)
    parser.add_argument("--jamendo-client-id", default="")
    parser.add_argument("--rights-confirmed", action="store_true")
    parser.add_argument("--rights-claim", default="")
    parser.add_argument("--log-level", default="INFO", choices=("DEBUG", "INFO", "WARNING", "ERROR"))
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    """Run acquisition then CPU analysis with deterministic exits. / 运行获取与 CPU 听诊并返回确定退出码。"""
    args = build_parser().parse_args(argv)
    logger = configure_logging(args.log_level)
    try:
        brief = load_music_brief(args.brief)
        library: Optional[Path]
        if args.provider == "local":
            if not args.library:
                raise MusicAnalysisError("本地模式需要 --library / Local provider requires --library.")
            library = Path(args.library).expanduser().resolve()
        else:
            acquirer = MusicCandidateAcquirer(args.cache_dir, logger)
            if args.provider == "jamendo":
                library = acquirer.acquire_jamendo(
                    brief, args.query, args.limit, args.jamendo_client_id
                )
            else:
                library = acquirer.acquire_ytdlp(
                    brief,
                    args.query,
                    args.limit,
                    args.rights_confirmed,
                    args.rights_claim,
                )
        LicensedMusicAnalyzer(library, logger).run(
            args.output, args.query, args.limit, brief
        )
        return 0
    except MusicAnalysisError as exc:
        logger.error("%s", exc)
        return 2
    except KeyboardInterrupt:
        logger.warning("用户中断配乐阶段 / Music stage interrupted.")
        return 130
    except Exception:
        logger.exception("未预期配乐错误 / Unexpected music-pipeline error.")
        return 1


if __name__ == "__main__":
    sys.exit(main())
