"""Licensed local music search and CPU beat analysis. / 本地授权配乐检索与 CPU 鼓点分析。"""

from __future__ import annotations

import argparse
import json
import logging
import math
import os
import sys
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence


LOGGER_NAME = "cybereditor.music"
AUDIO_SUFFIXES = {".wav", ".flac", ".mp3", ".m4a", ".aac", ".ogg"}


class MusicAnalysisError(RuntimeError):
    """Expected music-analysis failure. / 可预期的配乐分析错误。"""


class LicensedMusicAnalyzer:
    """
    Search a user-authorized library and extract beat timestamps on CPU.
    搜索用户授权的本地曲库，并在 CPU 上提取鼓点时间戳。

    A ``library.json`` manifest may describe tracks with ``file``, ``title``,
    ``tags``, ``mood``, ``license`` and ``licensed`` fields. Files omitted from
    the manifest are still treated as user-supplied, never as remotely licensed.

    ``library.json`` 可通过 ``file``、``title``、``tags``、``mood``、``license``
    与 ``licensed`` 描述曲目。清单外文件仅标记为“用户提供”，不会虚构网络授权。
    """

    def __init__(self, library: os.PathLike, logger: Optional[logging.Logger] = None) -> None:
        """
        Validate the library root without importing heavy audio packages.
        校验曲库根目录，但此时不导入较重的音频包。

        Parameters / 参数:
            library: Directory containing user-authorized audio. / 用户授权音频目录。
            logger: Optional application logger. / 可选应用日志器。
        """
        self.library = Path(library).expanduser().resolve()
        if not self.library.is_dir():
            raise MusicAnalysisError(f"配乐目录不存在 / Music library not found: {self.library}")
        self.logger = logger or logging.getLogger(LOGGER_NAME)
        self._manifest = self._load_manifest()

    def _load_manifest(self) -> Dict[str, Dict[str, Any]]:
        """Load optional license metadata keyed by normalized relative path. / 读取可选授权元数据。"""
        path = self.library / "library.json"
        if not path.is_file():
            return {}
        try:
            value = json.loads(path.read_text(encoding="utf-8-sig"))
        except (OSError, json.JSONDecodeError) as exc:
            raise MusicAnalysisError(f"无法解析 library.json / Cannot parse library.json: {exc}") from exc
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
        """Discover audio files and attach honest license provenance. / 查找音频并附加真实授权来源。"""
        tracks: List[Dict[str, Any]] = []
        for path in sorted(self.library.rglob("*"), key=lambda p: str(p).casefold()):
            if not path.is_file() or path.suffix.casefold() not in AUDIO_SUFFIXES:
                continue
            relative = path.relative_to(self.library)
            metadata = self._manifest.get(str(relative).replace("\\", "/").casefold(), {})
            explicitly_licensed = bool(metadata.get("licensed"))
            tracks.append(
                {
                    "file_name": str(path),
                    "relative_file": str(relative),
                    "title": str(metadata.get("title") or path.stem),
                    "tags": [str(v) for v in metadata.get("tags", []) if str(v).strip()]
                    if isinstance(metadata.get("tags"), list) else [],
                    "mood": str(metadata.get("mood") or ""),
                    "license": str(metadata.get("license") or ("manifest-approved" if explicitly_licensed else "user-supplied")),
                    "license_url": str(metadata.get("license_url") or ""),
                    "license_provenance": "manifest" if explicitly_licensed else "user_supplied",
                }
            )
        return tracks

    @staticmethod
    def rank(tracks: Iterable[Dict[str, Any]], query: str) -> List[Dict[str, Any]]:
        """Rank local tracks by simple multilingual keyword overlap. / 按多语言关键词重合度排序本地曲目。"""
        terms = {part.casefold() for part in str(query).replace(",", " ").split() if part.strip()}
        ranked: List[Dict[str, Any]] = []
        for track in tracks:
            haystack = " ".join(
                [str(track.get("title") or ""), str(track.get("mood") or ""), " ".join(track.get("tags") or [])]
            ).casefold()
            score = sum(2 if term in str(track.get("mood") or "").casefold() else 1 for term in terms if term in haystack)
            item = dict(track)
            item["search_score"] = score
            ranked.append(item)
        return sorted(ranked, key=lambda item: (-int(item["search_score"]), str(item["file_name"]).casefold()))

    def analyze_track(self, track: Dict[str, Any]) -> Dict[str, Any]:
        """
        Extract tempo and beat timestamps using librosa without CUDA.
        使用 librosa 提取速度与鼓点时间戳，不使用 CUDA。

        Parameters / 参数:
            track: One record returned by :meth:`discover`. / ``discover`` 返回的一条记录。
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
            onset = librosa.onset.onset_strength(y=audio, sr=sample_rate)
            tempo, beats = librosa.beat.beat_track(onset_envelope=onset, sr=sample_rate)
            beat_times = librosa.frames_to_time(beats, sr=sample_rate)
            duration = float(librosa.get_duration(y=audio, sr=sample_rate))
            tempo_value = float(np.asarray(tempo).reshape(-1)[0]) if np.asarray(tempo).size else 0.0
        except Exception as exc:
            raise MusicAnalysisError(f"配乐鼓点分析失败 / Beat analysis failed for {path}: {exc}") from exc
        result = dict(track)
        result.update(
            {
                "duration_sec": round(duration, 3),
                "tempo_bpm": round(tempo_value, 3) if math.isfinite(tempo_value) else 0.0,
                "beats_sec": [round(float(value), 4) for value in beat_times if math.isfinite(float(value))],
                "analysis_engine": "librosa-cpu",
            }
        )
        return result

    def run(self, output: os.PathLike, query: str = "", limit: int = 8) -> Dict[str, Any]:
        """Analyze the best matching authorized tracks and atomically write JSON. / 分析最匹配的授权曲目并原子写入 JSON。"""
        tracks = self.rank(self.discover(), query)
        if not tracks:
            raise MusicAnalysisError(f"配乐目录中没有支持的音频 / No supported audio in: {self.library}")
        analyzed: List[Dict[str, Any]] = []
        for index, track in enumerate(tracks[: max(1, min(32, int(limit)))], start=1):
            self.logger.info("分析配乐 %d/%d：%s / Analyzing music", index, min(len(tracks), limit), track["title"])
            analyzed.append(self.analyze_track(track))
        payload = {
            "schema_version": "1.0",
            "library_root": str(self.library),
            "query": str(query),
            "tracks": analyzed,
        }
        destination = Path(output).expanduser().resolve()
        destination.parent.mkdir(parents=True, exist_ok=True)
        temporary = destination.with_suffix(destination.suffix + ".tmp")
        temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(str(temporary), str(destination))
        return payload


def configure_logging(level: str) -> logging.Logger:
    """Configure standalone bilingual logging. / 配置独立双语日志。"""
    logging.basicConfig(level=getattr(logging, level.upper()), format="%(asctime)s | %(levelname)s | %(message)s")
    return logging.getLogger(LOGGER_NAME)


def build_parser() -> argparse.ArgumentParser:
    """Create CLI arguments. / 创建命令行参数。"""
    parser = argparse.ArgumentParser(description="Analyze a licensed local music library.")
    parser.add_argument("--library", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--query", default="")
    parser.add_argument("--limit", type=int, default=8)
    parser.add_argument("--log-level", default="INFO", choices=("DEBUG", "INFO", "WARNING", "ERROR"))
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    """Run the music CLI. / 运行配乐命令行。"""
    args = build_parser().parse_args(argv)
    try:
        LicensedMusicAnalyzer(args.library, configure_logging(args.log_level)).run(args.output, args.query, args.limit)
        return 0
    except MusicAnalysisError as exc:
        logging.getLogger(LOGGER_NAME).error("%s", exc)
        return 2


if __name__ == "__main__":
    sys.exit(main())
