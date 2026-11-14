"""녹화 세그먼트: stream_epoch 하나 = 파일 세 개 (형식은 README 의 "녹화 형식").

    <camera_id>/epoch_NNNN.h264              H.264 Annex B. 받은 접근 단위를 받은 순서대로 이어 붙인다
    <camera_id>/epoch_NNNN.frames.jsonl      한 줄 = 한 프레임 {"header": I0 frame_header, "offset": int, "size": int}
    <camera_id>/epoch_NNNN.codec_info.json   I0 codec_info (명세 12.7)

header 는 I0 프레임 헤더 계약(contracts/messages/frame_header.schema.json, S15P31A101-20) 그대로이므로,
(header, .h264[offset:offset+size]) 를 그대로 I0 메시지로 다시 보낼 수 있다(Tier 0 녹화 재생).
접근 단위를 먼저 쓰고 그 뒤에 사이드카 줄을 쓴다. 중간에 멈춰도 사이드카가 가리키는 바이트는 항상 파일에 있다.
"""

from __future__ import annotations

import base64
import json
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any, BinaryIO, TextIO

from simai_recorder.annexb import START_CODE

H264_SUFFIX = ".h264"
FRAMES_SUFFIX = ".frames.jsonl"
CODEC_INFO_SUFFIX = ".codec_info.json"


def segment_stem(stream_epoch: int) -> str:
    return f"epoch_{stream_epoch:04d}"


@dataclass(frozen=True)
class SegmentPaths:
    h264: Path
    frames: Path
    codec_info: Path

    @property
    def closed(self) -> Path:
        return self.h264.with_suffix(".closed")

    @classmethod
    def of(cls, camera_dir: Path, stream_epoch: int) -> SegmentPaths:
        stem = segment_stem(stream_epoch)
        return cls(camera_dir / f"{stem}{H264_SUFFIX}", camera_dir / f"{stem}{FRAMES_SUFFIX}",
                   camera_dir / f"{stem}{CODEC_INFO_SUFFIX}")


class SegmentWriter:
    """한 stream_epoch 의 세그먼트를 쓴다. 첫 프레임은 키프레임이어야 한다."""

    def __init__(self, camera_dir: Path, codec_info: dict[str, Any], prefix_parameter_sets: bool) -> None:
        self._camera_dir = camera_dir
        self._codec_info = codec_info
        self.rotate_after_ms: int | None = None
        self._part_started_ms: int | None = None
        self.paths = SegmentPaths.of(camera_dir, int(codec_info["stream_epoch"]))
        for p in (self.paths.h264, self.paths.frames, self.paths.codec_info):
            if p.exists():
                raise FileExistsError(f"덮어쓰지 않는다: {p}")
        camera_dir.mkdir(parents=True, exist_ok=True)
        self.paths.codec_info.write_text(json.dumps(codec_info, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        self._h264: BinaryIO = self.paths.h264.open("xb")
        self._frames: TextIO = self.paths.frames.open("x", encoding="utf-8", newline="\n")
        if prefix_parameter_sets:
            # 첫 키프레임에 SPS/PPS 가 없으면 파일만으로 디코딩되도록 앞에 붙인다. 프레임 바이트에는 들지 않는다.
            for key in ("sps", "pps"):
                self._h264.write(START_CODE + base64.b64decode(codec_info[key]))
        self.frame_count = 0

    def write(self, header: dict[str, Any], payload: bytes) -> None:
        capture_ms = int(header["estimated_capture_time_ms"])
        if (self.rotate_after_ms is not None and self._part_started_ms is not None and header["keyframe"]
                and capture_ms - self._part_started_ms >= self.rotate_after_ms):
            # 닫힘 표시는 두 파일 핸들을 닫은 뒤에만 공개한다. I0 epoch·sequence는 바꾸지 않는다.
            self.close()
            stem = f"{segment_stem(int(self._codec_info['stream_epoch']))}_part_{self.frame_count:012d}"
            self.paths = SegmentPaths(self._camera_dir / f"{stem}{H264_SUFFIX}",
                                      self._camera_dir / f"{stem}{FRAMES_SUFFIX}",
                                      self._camera_dir / f"{stem}{CODEC_INFO_SUFFIX}")
            with self.paths.codec_info.open("x", encoding="utf-8") as info:
                info.write(json.dumps(self._codec_info, ensure_ascii=False) + "\n")
            self._h264 = self.paths.h264.open("xb")
            self._frames = self.paths.frames.open("x", encoding="utf-8", newline="\n")
            for key in ("sps", "pps"):
                if key in self._codec_info:
                    self._h264.write(START_CODE + base64.b64decode(self._codec_info[key]))
            self._part_started_ms = None
        if self._part_started_ms is None:
            self._part_started_ms = capture_ms
        if self.frame_count == 0 and not header["keyframe"]:
            raise ValueError("세그먼트는 키프레임부터 시작한다")
        offset = self._h264.tell()
        self._h264.write(payload)
        self._h264.flush()
        line = {"header": header, "offset": offset, "size": len(payload)}
        self._frames.write(json.dumps(line, ensure_ascii=False, separators=(",", ":")) + "\n")
        self._frames.flush()
        self.frame_count += 1

    def close(self) -> None:
        self._h264.close()
        self._frames.close()
        self.paths.closed.touch(exist_ok=True)


def read_frames(frames_path: Path) -> Iterator[dict[str, Any]]:
    with frames_path.open(encoding="utf-8") as f:
        for line in f:
            if line.strip():
                yield json.loads(line)


def read_segment(paths: SegmentPaths) -> tuple[dict[str, Any], Iterator[tuple[dict[str, Any], bytes]]]:
    """(codec_info, (header, 압축 프레임) 반복자). Tier 0 녹화 재생이 I0 메시지로 되돌리는 순서와 같다."""
    codec_info = json.loads(paths.codec_info.read_text(encoding="utf-8"))

    def frames() -> Iterator[tuple[dict[str, Any], bytes]]:
        with paths.h264.open("rb") as h264:
            for row in read_frames(paths.frames):
                h264.seek(row["offset"])
                payload = h264.read(row["size"])
                if len(payload) != row["size"]:
                    raise ValueError(f"{paths.h264}: offset {row['offset']} 에서 {row['size']} 바이트를 읽지 못했다")
                yield row["header"], payload

    return codec_info, frames()


def camera_segments(camera_dir: Path) -> list[SegmentPaths]:
    """카메라 디렉터리의 세그먼트를 stream_epoch 순서로."""
    return [SegmentPaths(codec.with_name(codec.name.removesuffix(CODEC_INFO_SUFFIX) + H264_SUFFIX),
                         codec.with_name(codec.name.removesuffix(CODEC_INFO_SUFFIX) + FRAMES_SUFFIX), codec)
            for codec in sorted(camera_dir.glob(f"epoch_*{CODEC_INFO_SUFFIX}"),
                                key=lambda p: tuple(int(n) for n in p.name.removesuffix(CODEC_INFO_SUFFIX)
                                                    .removeprefix("epoch_").split("_part_")))]
