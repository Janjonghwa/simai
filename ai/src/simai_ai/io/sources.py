"""FrameSource 구현: I0 중계 스트림, 시뮬레이터 출력, 녹화 파일 (명세 5.1 입력 추상화, 20.4).

셋 모두 같은 형식(providers.Frame: 카메라 ID + 촬영 시각 + 프레임 식별자 + 디코딩된 이미지 또는 2D keypoint)을 낸다.

- I0FrameSource: I0Ingest 를 거쳐 최신성 정책이 고른 프레임만 낸다(LIVE). 측정값은 tickets() 로 함께 받는다.
- SimObservationFrameSource: `python -m sim.run` 출력의 observations/*.jsonl 을 읽는다. 2D keypoint 단계부터 넣는다.
  simai_ai 는 sim 을 import 하지 않으므로 파일 형식만 따른다(sim/README.md).
- VideoFileFrameSource: 녹화 파일을 처음부터 끝까지 모두 낸다(오프라인·시나리오 재생). 기본 리더는 OpenCV(io extra).
"""

from __future__ import annotations

import heapq
import json
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from simai_ai.io.freshness import InferenceTicket
from simai_ai.io.ingest import I0Ingest
from simai_ai.providers.base import Detection2D, Frame, FrameSource, FramesProvenance
from simai_ai.tracking.time_model import require_ms


def merge_by_capture_time(streams: Sequence[Iterable[Frame]]) -> Iterator[Frame]:
    """카메라별 스트림(각각 촬영 시각이 줄지 않음)을 촬영 시각 순서로 합친다. 같은 시각이면 streams 순서."""
    return heapq.merge(*streams, key=lambda f: f.estimated_capture_time)


# --------------------------------------------------------------------------- I0 (LIVE)


class I0FrameSource(FrameSource):
    """I0 수신 → 디코딩 → latest-frame-wins 대기열을 거친 프레임 (명세 5.1, 12.7).

    feed 는 입력을 진행시키는 함수다: 도착한 메시지를 ingest.receive 로 넘기고(없으면 올 때까지 기다리고),
    입력이 끝났으면 False 를 돌려준다. LIVE 에서는 수신 스레드가 receive 를 부르므로 feed 는 기다리기만 한다.
    """

    def __init__(self, ingest: I0Ingest, feed: Callable[[], bool], source: str = "LIVE") -> None:
        self._ingest = ingest
        self._feed = feed
        self._source = source

    @property
    def source(self) -> str:
        return self._source

    @property
    def camera_ids(self) -> tuple[str, ...]:
        return self._ingest.camera_ids

    def tickets(self) -> Iterator[InferenceTicket]:
        """추론할 프레임과 frame_age_ms·queue_wait_ms. 입력이 끝나면 디코더에 남은 그림까지 낸 뒤 끝난다.

        한 장씩, 소비자가 다음 프레임을 달라고 하는 순간에 고르고 잰다. 그래서 앞 프레임을 추론하는 동안 상한을 넘긴
        프레임은 넘기지 않고, 그동안 온 더 새 프레임을 넘긴다.
        """
        while True:
            ticket = self._ingest.next_frame()
            if ticket is not None:
                yield ticket
                continue
            if not self._feed():
                break
        self._ingest.close()
        while (ticket := self._ingest.next_frame()) is not None:
            yield ticket

    def frames(self) -> Iterator[Frame]:
        return (ticket.frame for ticket in self.tickets())


# --------------------------------------------------------------------------- 시뮬레이터 출력


def _detection(row: Mapping[str, Any]) -> Detection2D:
    kps = tuple(None if kp is None else (float(kp[0]), float(kp[1]), float(kp[2])) for kp in row["keypoints_2d"])
    x1, y1, x2, y2 = (float(v) for v in row["bbox"])
    return Detection2D(
        bbox=(x1, y1, x2, y2),
        joint_layout=row["joint_layout"],
        keypoints_2d=kps,
        local_track_id=row.get("local_track_id"),
    )


def sim_observation_frame(row: Mapping[str, Any]) -> Frame:
    """observations/<camera>.jsonl 한 줄 → Frame. foot_tips_2d 는 Detection2D 에 자리가 없어 넣지 않는다."""
    return Frame(
        camera_id=row["camera_id"],
        estimated_capture_time=require_ms("estimated_capture_time_ms", row["estimated_capture_time_ms"]),
        timestamp_source=row["timestamp_source"],
        frame_ref={"stream_epoch": row["stream_epoch"], "frame_sequence": row["frame_sequence"]},
        timestamp_uncertainty_ms=row.get("timestamp_uncertainty_ms"),
        detections=tuple(_detection(d) for d in row["detections"]),
    )


class SimObservationFrameSource(FrameSource):
    """시뮬레이터 출력 디렉터리의 2D keypoint 를 카메라 간 촬영 시각 순서로 낸다 (frame_source SIM, 상한 지표).

    camera_ids 를 주지 않으면 manifest.json 의 input_provenance.frames.camera_ids, 그것도 없으면
    observations/ 의 파일 이름 순서를 쓴다.
    """

    def __init__(self, run_dir: Path | str, camera_ids: Sequence[str] | None = None) -> None:
        self._dir = Path(run_dir)
        manifest_path = self._dir / "manifest.json"
        self._manifest_frames: dict[str, Any] = {}
        if manifest_path.exists():
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            self._manifest_frames = dict(manifest.get("input_provenance", {}).get("frames", {}))
        if camera_ids is None:
            camera_ids = self._manifest_frames.get("camera_ids") or sorted(
                p.stem for p in (self._dir / "observations").glob("*.jsonl")
            )
        if not camera_ids:
            raise ValueError(f"{self._dir}: 카메라 관측 파일이 없다")
        self._camera_ids = tuple(camera_ids)
        for cam in self._camera_ids:
            if not self._path(cam).exists():
                raise FileNotFoundError(self._path(cam))

    def _path(self, camera_id: str) -> Path:
        return self._dir / "observations" / f"{camera_id}.jsonl"

    @property
    def source(self) -> str:
        return "SIM"

    @property
    def camera_ids(self) -> tuple[str, ...]:
        return self._camera_ids

    def _read(self, camera_id: str) -> Iterator[Frame]:
        with self._path(camera_id).open(encoding="utf-8") as f:
            for line in f:
                if line.strip():
                    frame = sim_observation_frame(json.loads(line))
                    if frame.camera_id != camera_id:
                        raise ValueError(f"{self._path(camera_id)}: 다른 카메라 {frame.camera_id} 의 줄이 있다")
                    yield frame

    def frames(self) -> Iterator[Frame]:
        return merge_by_capture_time([self._read(c) for c in self._camera_ids])

    def provenance(self) -> FramesProvenance:
        prov: FramesProvenance = {"source": "SIM"}
        for key in ("dataset_id", "dataset_hash"):
            if key in self._manifest_frames:
                prov[key] = self._manifest_frames[key]
        prov["camera_ids"] = list(self._camera_ids)
        return prov


# --------------------------------------------------------------------------- 녹화 파일


class VideoReader(Protocol):
    """녹화 파일 한 개를 앞에서부터 읽는다. read 는 다음 그림(없으면 None)을 돌려준다."""

    @property
    def fps(self) -> float: ...

    def read(self) -> Any | None: ...

    def close(self) -> None: ...


class OpenCvVideoReader:
    """OpenCV(io extra) 리더. 그림은 HxWx3 BGR numpy 배열이다. CPU 디코딩."""

    def __init__(self, path: Path) -> None:
        import cv2  # io extra. 기본 설치에는 없으므로 쓸 때만 불러온다.

        self._cap = cv2.VideoCapture(str(path))
        if not self._cap.isOpened():
            raise OSError(f"녹화 파일을 열 수 없다: {path}")
        self._fps = float(self._cap.get(cv2.CAP_PROP_FPS))

    @property
    def fps(self) -> float:
        return self._fps

    def read(self) -> Any | None:
        ok, image = self._cap.read()
        return image if ok else None

    def close(self) -> None:
        self._cap.release()


@dataclass(frozen=True)
class Recording:
    """카메라 한 대의 녹화 파일.

    start_time_ms: 첫 프레임의 촬영 시각(UTC ms). 녹화 manifest 에서 받는다.
    file_uri: frame_ref.file_uri 에 넣을 값(예: volume://AP-JP-1/recordings/...). 없으면 로컬 경로 URI.
    fps: 파일 메타데이터 대신 쓸 프레임률. 촬영 시각 = start_time_ms + round(frame_index * 1000 / fps).
    """

    camera_id: str
    path: Path
    start_time_ms: int
    file_uri: str | None = None
    fps: float | None = None
    timestamp_uncertainty_ms: float | None = None


class VideoFileFrameSource(FrameSource):
    """녹화 파일(시나리오 데이터셋)의 모든 프레임을 카메라 간 촬영 시각 순서로 낸다 (timestamp_source FILE).

    source 는 직접 촬영한 녹화면 FILE, 공개 데이터 영상이면 DATASET 이다. 프레임을 건너뛰지 않는다
    (실시간 속도 재생에서 최신성 정책을 시험하려면 I0 녹화를 I0FrameSource 로 재생한다).
    """

    def __init__(
        self,
        recordings: Sequence[Recording],
        source: str = "FILE",
        open_reader: Callable[[Path], VideoReader] = OpenCvVideoReader,
        dataset_id: str | None = None,
    ) -> None:
        if not recordings or len({r.camera_id for r in recordings}) != len(recordings):
            raise ValueError("recordings 는 비어 있지 않고 카메라마다 하나여야 한다")
        for r in recordings:
            require_ms("start_time_ms", r.start_time_ms)
        self._recordings = tuple(recordings)
        self._source = source
        self._open = open_reader
        self._dataset_id = dataset_id

    @property
    def source(self) -> str:
        return self._source

    @property
    def camera_ids(self) -> tuple[str, ...]:
        return tuple(r.camera_id for r in self._recordings)

    def _read(self, rec: Recording) -> Iterator[Frame]:
        reader = self._open(rec.path)
        try:
            fps = rec.fps if rec.fps is not None else reader.fps
            if not fps > 0:
                raise ValueError(f"{rec.path}: 프레임률을 모른다 (Recording.fps 로 지정)")
            uri = rec.file_uri or Path(rec.path).resolve().as_uri()
            index = 0
            while (image := reader.read()) is not None:
                yield Frame(
                    camera_id=rec.camera_id,
                    estimated_capture_time=rec.start_time_ms + round(index * 1000 / fps),
                    timestamp_source="FILE",
                    frame_ref={"file_uri": uri, "frame_index": index},
                    timestamp_uncertainty_ms=rec.timestamp_uncertainty_ms,
                    image=image,
                )
                index += 1
        finally:
            reader.close()

    def frames(self) -> Iterator[Frame]:
        return merge_by_capture_time([self._read(r) for r in self._recordings])

    def provenance(self) -> FramesProvenance:
        prov: FramesProvenance = {"source": self._source}
        if self._dataset_id is not None:
            prov["dataset_id"] = self._dataset_id
        prov["camera_ids"] = list(self.camera_ids)
        return prov
