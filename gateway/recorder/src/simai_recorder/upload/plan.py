"""녹화 폴더 → 작업 구간과 겹치는 키프레임 조각 (S15P31A101-668).

조각은 카메라·stream_epoch 하나 안에서만 자른다(epoch 마다 SPS/PPS·시각선이 따로다). 첫 프레임은 키프레임이고, 작업 구간 시작 직전의
마지막 키프레임부터 시작한다(구간 앞 GOP 하나까지 더 들어간다, EC2 는 30 s 까지 받는다). 조각 길이가 5분을 넘으면 다음 키프레임에서 자른다.
조각 끝 시각 = 마지막 프레임 촬영 시각 + 그 프레임 길이(다음 프레임과의 차, 1 s 로 묶음. epoch 끝이면 공칭 프레임 간격).
같은 epoch 의 이웃 조각은 빈틈 없이 이어지고, 화면은 currentTime = (재생 시각 − 조각 시작) / 1000 으로 그 시각 프레임을 본다.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path

from simai_recorder.segment import CODEC_INFO_SUFFIX, SegmentPaths, camera_segments

#: 조각 목표 길이. 넘으면 다음 키프레임에서 자른다(카메라당 약 8 MB/분이라 약 40 MB)
CHUNK_MS = 300_000
#: 조각이 키프레임을 못 만나도 이 길이·크기에서는 끊는다(EC2 상한 10분·서명 상한 128 MiB 아래)
HARD_CHUNK_MS = 540_000
HARD_CHUNK_BYTES = 120 * 1024 * 1024
#: 구간 시작 앞 키프레임을 이만큼까지 거슬러 쓴다(EC2 LEAD_SLACK 과 같다)
LEAD_MS = 30_000
#: 조각 마지막 프레임 길이 상한(EC2 TAIL_SLACK 과 같다). 프레임이 빠진 자리 뒤 긴 빈칸을 조각 끝에 넣지 않는다
MAX_TAIL_MS = 1_000
DEFAULT_FRAME_MS = 40


@dataclass(frozen=True)
class FrameRow:
    capture_ms: int
    keyframe: bool
    sequence: int
    offset: int
    size: int


@dataclass(frozen=True)
class EpochTrack:
    camera_id: str
    stream_epoch: int
    paths: SegmentPaths
    frames: tuple[FrameRow, ...]
    frame_ms: int


@dataclass(frozen=True)
class Window:
    work_id: int
    start_ms: int
    end_ms: int


@dataclass(frozen=True)
class Chunk:
    segment_id: str
    recording_id: str
    work_id: int
    camera_id: str
    stream_epoch: int
    paths: SegmentPaths
    frames: tuple[FrameRow, ...]
    start_ms: int
    end_ms: int
    has_gaps: bool

    @property
    def frame_count(self) -> int:
        return len(self.frames)

    def pts_ms(self) -> list[int]:
        """mp4 PTS(ms) = 촬영 시각 − 조각 시작. 같은 ms 가 오면 1 ms 씩 민다(녹화가 이미 단조를 보장하지만 방어)."""
        out: list[int] = []
        for row in self.frames:
            pts = row.capture_ms - self.start_ms
            out.append(pts if not out or pts > out[-1] else out[-1] + 1)
        return out


def _read_rows(paths: SegmentPaths) -> list[FrameRow]:
    """사이드카를 읽는다. 비정상 종료로 마지막 줄이 잘렸거나 바이트가 파일에 없으면 그 앞까지만 쓴다."""
    try:
        h264_size = paths.h264.stat().st_size
        text = paths.frames.read_text(encoding="utf-8")
    except OSError:
        return []
    rows: list[FrameRow] = []
    for line in text.splitlines():
        if not line.strip():
            continue
        try:
            raw = json.loads(line)
            header = raw["header"]
            row = FrameRow(int(header["estimated_capture_time_ms"]), bool(header["keyframe"]),
                           int(header["frame_sequence"]), int(raw["offset"]), int(raw["size"]))
        except (ValueError, KeyError, TypeError):
            break
        if row.offset + row.size > h264_size or (rows and row.capture_ms < rows[-1].capture_ms):
            break
        rows.append(row)
    # 세그먼트는 키프레임으로 시작한다(SegmentWriter). 아니면 쓰지 않는다
    return rows if rows and rows[0].keyframe else []


def _frame_ms(paths: SegmentPaths) -> int:
    try:
        fps = float(json.loads(paths.codec_info.read_text(encoding="utf-8")).get("nominal_fps") or 0)
    except (OSError, ValueError, AttributeError):
        fps = 0
    return round(1000 / fps) if fps > 0 else DEFAULT_FRAME_MS


def read_tracks(recording_dir: Path, *, closed_only: bool = False) -> list[EpochTrack]:
    """녹화 폴더의 카메라 디렉터리(epoch_*.codec_info.json 이 있는 하위 폴더)마다 epoch 순서로."""
    tracks: list[EpochTrack] = []
    for camera_dir in sorted(p for p in recording_dir.iterdir() if p.is_dir()):
        if not any(camera_dir.glob(f"epoch_*{CODEC_INFO_SUFFIX}")):
            continue
        for paths in camera_segments(camera_dir):
            if closed_only and not paths.closed.exists():
                continue
            rows = _read_rows(paths)
            if rows:
                epoch = int(json.loads(paths.codec_info.read_text(encoding="utf-8"))["stream_epoch"])
                tracks.append(EpochTrack(camera_dir.name, epoch, paths, tuple(rows), _frame_ms(paths)))
    return tracks


def recording_span(tracks: Iterable[EpochTrack]) -> tuple[int, int] | None:
    """녹화 범위 = 카메라별 첫·마지막 촬영 시각. manifest ended_at_ms 가 null(비정상 종료)이어도 파일에서 계산한다."""
    firsts = [t.frames[0].capture_ms for t in tracks]
    lasts = [t.frames[-1].capture_ms for t in tracks]
    return (min(firsts), max(lasts)) if firsts else None


def segment_uuid(capture_ms: int, key: str) -> str:
    """UUIDv7: 시각 칸 = 조각 첫 프레임 촬영 시각, 나머지 = key 해시. 다시 돌려도 같은 조각은 같은 id·같은 객체 키다."""
    digest = hashlib.sha256(key.encode("utf-8")).digest()
    raw = bytearray(capture_ms.to_bytes(6, "big") + digest[:10])
    raw[6] = 0x70 | (raw[6] & 0x0F)
    raw[8] = 0x80 | (raw[8] & 0x3F)
    return str(uuid.UUID(bytes=bytes(raw)))


def _start_index(frames: Sequence[FrameRow], first: int, last: int, window_start: int) -> int | None:
    """구간 첫 프레임 직전 마지막 키프레임(LEAD_MS 안), 없으면 구간 안 첫 키프레임."""
    for i in range(first, -1, -1):
        if frames[i].keyframe:
            if frames[i].capture_ms >= window_start - LEAD_MS:
                return i
            break
    for i in range(first, last + 1):
        if frames[i].keyframe:
            return i
    return None


def _chunk_end_ms(track: EpochTrack, j: int) -> int:
    frames = track.frames
    if j + 1 < len(frames):
        tail = min(frames[j + 1].capture_ms - frames[j].capture_ms, MAX_TAIL_MS)
    else:
        tail = track.frame_ms
    return frames[j].capture_ms + max(tail, 1)


def _track_chunks(recording_id: str, track: EpochTrack, window: Window) -> list[Chunk]:
    frames = track.frames
    inside = [i for i, f in enumerate(frames) if window.start_ms <= f.capture_ms <= window.end_ms]
    if not inside:
        return []
    first, last = inside[0], inside[-1]
    start = _start_index(frames, first, last, window.start_ms)
    chunks: list[Chunk] = []
    i = start
    while i is not None and i <= last:
        j, size = i, frames[i].size
        cut_hard = False
        while j + 1 <= last:
            nxt = frames[j + 1]
            span = nxt.capture_ms - frames[i].capture_ms
            if nxt.keyframe and span >= CHUNK_MS:
                break
            if span >= HARD_CHUNK_MS or size + nxt.size > HARD_CHUNK_BYTES:
                cut_hard = True
                break
            j += 1
            size += nxt.size
        rows = frames[i: j + 1]
        gaps = any(b.sequence != a.sequence + 1 for a, b in zip(rows, rows[1:], strict=False))
        key = f"{recording_id}|{track.camera_id}|{track.stream_epoch}|{rows[0].sequence}|{window.work_id}"
        chunks.append(Chunk(segment_uuid(rows[0].capture_ms, key), recording_id, window.work_id, track.camera_id,
                            track.stream_epoch, track.paths, tuple(rows), rows[0].capture_ms, _chunk_end_ms(track, j), gaps))
        i = j + 1
        if cut_hard:
            # 키프레임 없이 끊었으면 다음 조각은 다음 키프레임부터(조각만으로 재생 가능해야 한다)
            i = next((k for k in range(j + 1, last + 1) if frames[k].keyframe), None)
    return chunks


def plan_chunks(recording_id: str, tracks: Sequence[EpochTrack], windows: Sequence[Window]) -> list[Chunk]:
    """작업 구간마다 카메라·epoch 별 조각. 시작 시각·카메라 순."""
    chunks = [c for w in windows for t in tracks for c in _track_chunks(recording_id, t, w)]
    return sorted(chunks, key=lambda c: (c.start_ms, c.camera_id))
