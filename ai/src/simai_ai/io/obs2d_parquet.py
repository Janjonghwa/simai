"""2D 관측(observation_2d)을 RunPod Network Volume 에 시간 단위 Parquet 로 추가만 하는 기록기와 리더 (명세 11.2, 11.4, 11.5).

- 입력은 `tracking.local_tracker.observation_batch_payload` 가 내는 observation_batch payload(또는 봉투째 메시지)를
  그대로 받는다(contracts/messages/observation_batch.schema.json). 계약을 바꾸지 않는다.
- 저장하는 것은 좌표·식별자뿐이다: 촬영 시각, 카메라, 프레임 식별자, 시각 출처·불확실성, 보정 버전, 관절 배치,
  세션, input_provenance, 카메라 내 트랙 ID, world_id, bbox, score, keypoint. 계약에 없는 필드(영상·crop 등)는
  열 목록에 없어서 저장되지 않는다(얼굴 픽셀·영상 저장 금지, 11.5).
- 행 하나 = 검출 하나. 사람이 없는 프레임도 "처리됨"을 남기려고 detection_index 가 null 인 행 하나로 쓴다.
- 파일은 카메라 × 촬영 시각 구간(기본 1시간) × 세션 × part 다. 경로·보관 기간은 `obs2d_layout`.
- 쓰는 중에는 `.inprogress` 이름으로 쓰고 닫을 때 최종 이름으로 바꾼다. 같은 이름이 있으면 덮어쓰지 않는다.
  새 기록기는 이미 있는 part(닫힌 것·쓰는 중인 것 모두) 다음 번호로 새 파일을 연다(재시작 이어쓰기).
- 수는 float32 로 저장한다(픽셀 좌표 2304 px 에서 정밀도 약 0.0002 px). 왕복 일치는 float32 로 반올림한 값 기준이다.
- 현재 시각을 읽지 않는다. 파일 구간·솎기는 모두 촬영 시각 기준이다.

pyarrow 는 extras `io` 에 있다. 이 모듈만 pyarrow 를 import 하므로 pyarrow 가 없어도 다른 모듈 import 는 깨지지 않는다.
"""

from __future__ import annotations

import json
import math
import os
import sys
from collections.abc import Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, BinaryIO

try:
    import pyarrow as pa
    import pyarrow.compute as pc
    import pyarrow.parquet as pq
except ImportError as exc:  # pragma: no cover - 설치 환경에 따라
    raise ImportError(
        "simai_ai.io.obs2d_parquet 는 pyarrow 가 필요하다: uv sync --extra io (또는 dev 그룹)"
    ) from exc

from simai_ai.io.obs2d_layout import (
    DECIMATION_TOLERANCE,
    FILE_PREFIX,
    TEMP_SUFFIX,
    Obs2DStoreConfig,
    StoredFile,
    list_files,
    parse_file_name,
    require_session_id,
)
from simai_ai.providers.base import TIMESTAMP_SOURCES
from simai_ai.tracking.local_tracker import JOINT_COUNTS, JOINT_LAYOUTS, require_camera_id
from simai_ai.tracking.time_model import require_ms

FORMAT_VERSION = "1.0.0"
"""Parquet 열 구성 버전. 열을 더하면 마이너, 빼거나 뜻을 바꾸면 메이저"""

METADATA_KEY = b"simai.obs2d"

SCHEMA = pa.schema(
    [
        pa.field("capture_time_ms", pa.int64(), nullable=False),
        pa.field("camera_id", pa.dictionary(pa.int32(), pa.string()), nullable=False),
        pa.field("stream_epoch", pa.int64(), nullable=False),
        pa.field("frame_sequence", pa.int64(), nullable=False),
        pa.field("timestamp_source", pa.dictionary(pa.int32(), pa.string()), nullable=False),
        pa.field("timestamp_uncertainty_ms", pa.float64(), nullable=False),
        pa.field("calibration_version", pa.dictionary(pa.int32(), pa.string()), nullable=False),
        pa.field("joint_layout", pa.dictionary(pa.int32(), pa.string()), nullable=False),
        pa.field("session_id", pa.dictionary(pa.int32(), pa.string()), nullable=False),
        pa.field("input_provenance", pa.dictionary(pa.int32(), pa.string()), nullable=False),
        # 검출 열. 사람이 없는 프레임은 detection_index 부터 모두 null
        pa.field("detection_index", pa.int32()),
        pa.field("local_track_id", pa.string()),
        pa.field("world_id", pa.string()),
        pa.field("bbox", pa.list_(pa.float32(), 4)),
        pa.field("score", pa.float32()),
        pa.field("keypoints_2d", pa.list_(pa.list_(pa.float32(), 3))),
    ]
)

FRAME_COLUMNS = ("camera_id", "stream_epoch", "frame_sequence", "timestamp_source", "timestamp_uncertainty_ms")


# --------------------------------------------------------------------------- 입력 검사


def _finite(name: str, value: Any) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ValueError(f"{name}: 유한한 수여야 한다 (받은 값 {value!r})")
    return float(value)


def _count(name: str, value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{name}: 0 이상 정수여야 한다 (받은 값 {value!r})")
    return value


def _text(name: str, value: Any) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError(f"{name}: 비어 있지 않은 문자열이어야 한다 (받은 값 {value!r})")
    return value


def _payload(message: Mapping[str, Any]) -> Mapping[str, Any]:
    """봉투째 메시지면 payload 를 꺼낸다."""
    if "msg_type" in message:
        if message["msg_type"] != "observation_batch":
            raise ValueError(f"msg_type {message['msg_type']!r}: observation_batch 만 받는다")
        message = message["payload"]
    if not isinstance(message, Mapping):
        raise ValueError("observation_batch payload 가 객체가 아니다")
    return message


def _provenance_json(provenance: Mapping[str, Any]) -> str:
    if not isinstance(provenance, Mapping):
        raise ValueError("input_provenance 가 객체가 아니다")
    frames = provenance.get("frames")
    if not isinstance(frames, Mapping) or not isinstance(frames.get("source"), str):
        raise ValueError("input_provenance.frames.source 가 없다 (contracts common input_provenance)")
    if not isinstance(provenance.get("is_upper_bound"), bool):
        raise ValueError("input_provenance.is_upper_bound 가 없다 (contracts common input_provenance)")
    return json.dumps(provenance, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


@dataclass(frozen=True)
class _Frame:
    """검사를 마친 프레임 하나와 그 행들."""

    camera_id: str
    capture_time_ms: int
    rows: list[dict[str, Any]]


def _frame_rows(frame: Mapping[str, Any], calibration_version: str, joint_layout: str, common: Mapping[str, str]):
    camera_id = require_camera_id(frame.get("camera_id"))
    where = f"{camera_id} ({frame.get('stream_epoch')}, {frame.get('frame_sequence')})"
    capture = require_ms(f"{where} estimated_capture_time_ms", frame.get("estimated_capture_time_ms"))
    source = frame.get("timestamp_source")
    if source not in TIMESTAMP_SOURCES:
        raise ValueError(f"{where}: timestamp_source {source!r} 는 {TIMESTAMP_SOURCES} 중 하나여야 한다")
    uncertainty = frame.get("timestamp_uncertainty_ms")
    if uncertainty is None:
        raise ValueError(f"{where}: timestamp_uncertainty_ms 가 없다 (0 으로 채우지 않는다)")
    uncertainty = _finite(f"{where} timestamp_uncertainty_ms", uncertainty)
    if uncertainty < 0:
        raise ValueError(f"{where}: timestamp_uncertainty_ms 가 음수다")
    base = {
        "capture_time_ms": capture,
        "camera_id": camera_id,
        "stream_epoch": _count(f"{where} stream_epoch", frame.get("stream_epoch")),
        "frame_sequence": _count(f"{where} frame_sequence", frame.get("frame_sequence")),
        "timestamp_source": source,
        "timestamp_uncertainty_ms": uncertainty,
        "calibration_version": calibration_version,
        "joint_layout": joint_layout,
        **common,
    }
    detections = frame.get("detections")
    if not isinstance(detections, Sequence) or isinstance(detections, (str, bytes)):
        raise ValueError(f"{where}: detections 가 배열이 아니다")
    expected = JOINT_COUNTS.get(joint_layout)
    rows: list[dict[str, Any]] = []
    for i, det in enumerate(detections):
        d = f"{where} detections[{i}]"
        if not isinstance(det, Mapping):
            raise ValueError(f"{d}: 검출이 객체가 아니다")
        bbox = det.get("bbox")
        if not isinstance(bbox, Sequence) or len(bbox) != 4:
            raise ValueError(f"{d}: bbox 는 [x1, y1, x2, y2] 여야 한다")
        score = _finite(f"{d} score", det.get("score"))
        if not 0.0 <= score <= 1.0:
            raise ValueError(f"{d}: score 는 0~1 이어야 한다 (받은 값 {score})")
        kps = det.get("keypoints_2d")
        if not isinstance(kps, Sequence) or isinstance(kps, (str, bytes)):
            raise ValueError(f"{d}: keypoints_2d 가 배열이 아니다")
        if expected is not None and len(kps) != expected:
            raise ValueError(f"{d}: keypoint {len(kps)}개, {joint_layout} 는 {expected}개")
        keypoints = []
        for j, kp in enumerate(kps):
            if kp is None:
                keypoints.append(None)
                continue
            if not isinstance(kp, Sequence) or len(kp) != 3:
                raise ValueError(f"{d} keypoints_2d[{j}]: [u, v, confidence] 또는 null 이어야 한다")
            keypoints.append([_finite(f"{d} keypoints_2d[{j}]", x) for x in kp])
        world_id = det.get("world_id")
        if world_id is not None:
            _text(f"{d} world_id", world_id)
        rows.append(
            {
                **base,
                "detection_index": i,
                "local_track_id": _text(f"{d} local_track_id", det.get("local_track_id")),
                "world_id": world_id,
                "bbox": [_finite(f"{d} bbox", x) for x in bbox],
                "score": score,
                "keypoints_2d": keypoints,
            }
        )
    if not rows:
        rows.append(
            {**base, "detection_index": None, "local_track_id": None, "world_id": None, "bbox": None, "score": None,
             "keypoints_2d": None}
        )
    return _Frame(camera_id, capture, rows)


# --------------------------------------------------------------------------- 파일 열기·닫기


def publish_no_overwrite(temp: Path, final: Path) -> None:
    """temp 를 final 로 원자적으로 바꾼다. final 이 이미 있으면 덮어쓰지 않고 FileExistsError.

    Windows 의 os.rename 은 대상이 있으면 실패한다. POSIX 에서는 os.link(있으면 실패) 후 temp 를 지운다.
    링크를 지원하지 않는 파일 시스템이면 있는지 확인한 뒤 rename 한다(이름에 세션·part 가 있어 경쟁 기록기가 없다는 전제).
    """
    if sys.platform == "win32":
        os.rename(temp, final)
        return
    try:
        os.link(temp, final)
    except FileExistsError:
        raise
    except OSError:
        if final.exists():
            raise FileExistsError(final) from None
        os.rename(temp, final)
        return
    os.unlink(temp)


@dataclass
class _OpenFile:
    camera_id: str
    window_start_ms: int
    part: int
    temp_path: Path
    final_path: Path
    handle: BinaryIO
    writer: pq.ParquetWriter
    buffer: list[dict[str, Any]] = field(default_factory=list)
    rows_written: int = 0


@dataclass
class WriterStats:
    """기록기 누적 수. skipped_by_rate: 저장 빈도 상한으로 솎은 프레임, skipped_out_of_order: 같은 카메라의 직전 저장
    프레임보다 촬영 시각이 이르거나 같아 버린 프레임(Local Tracker 가 이미 걸러 정상이면 0)."""

    frames_written: int = 0
    rows_written: int = 0
    skipped_by_rate: int = 0
    skipped_out_of_order: int = 0
    files_closed: list[Path] = field(default_factory=list)


class Obs2DParquetWriter:
    """2D 관측을 카메라·시간 구간별 Parquet 파일에 추가만 하는 기록기.

    session_id 는 이 기록기(보통 Tier 0 프로세스 한 번)의 세션이다. 참여자 삭제 요청(18.4)도 세션 단위로 찾는다.
    input_provenance 는 contracts common input_provenance 문서다(관측과 추정, 정답 입력 여부를 구분, 20.4).
    같은 카메라의 촬영 시각은 앞으로만 간다고 본다(Local Tracker 가 보장). 새 구간 프레임이 오면 그 카메라의 이전 파일을 닫는다.
    """

    def __init__(
        self,
        config: Obs2DStoreConfig,
        *,
        session_id: str,
        input_provenance: Mapping[str, Any],
        source_id: str | None = None,
    ) -> None:
        self._config = config
        self._session_id = require_session_id(session_id)
        self._provenance_json = _provenance_json(input_provenance)
        self._source_id = source_id
        self._open: dict[str, _OpenFile] = {}
        self._last_kept: dict[str, int] = {}
        self._next_due: dict[str, float] = {}
        self._closed = False
        self.stats = WriterStats()

    @property
    def config(self) -> Obs2DStoreConfig:
        return self._config

    @property
    def session_id(self) -> str:
        return self._session_id

    def open_paths(self) -> list[Path]:
        """지금 쓰는 중인 파일(.inprogress) 경로."""
        return [f.temp_path for f in self._open.values()]

    def __enter__(self) -> Obs2DParquetWriter:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # ---------------------------------------------------------------- 기록

    def write_batch(self, message: Mapping[str, Any]) -> int:
        """observation_batch payload(또는 봉투째 메시지)를 기록한다. 저장한 프레임 수를 돌려준다.

        묶음 전체를 먼저 검사하고 하나라도 틀리면 아무것도 쓰지 않는다.
        """
        if self._closed:
            raise RuntimeError("닫힌 기록기다")
        payload = _payload(message)
        calibration_version = _text("calibration_version", payload.get("calibration_version"))
        joint_layout = payload.get("joint_layout")
        if joint_layout not in JOINT_LAYOUTS:
            raise ValueError(f"joint_layout {joint_layout!r} 는 {JOINT_LAYOUTS} 중 하나여야 한다")
        frames = payload.get("frames")
        if not isinstance(frames, Sequence) or isinstance(frames, (str, bytes)) or not frames:
            raise ValueError("frames 가 비어 있다 (계약은 1개 이상)")
        common = {"session_id": self._session_id, "input_provenance": self._provenance_json}
        checked = [_frame_rows(f, calibration_version, joint_layout, common) for f in frames]
        written = 0
        for frame in checked:
            if self._accept(frame):
                self._append(frame)
                written += 1
        return written

    def _accept(self, frame: _Frame) -> bool:
        """카메라별 저장 빈도 상한(명세 11.2 저장 5~15 Hz) 솎기. obs2d_layout.DECIMATION_TOLERANCE 참고."""
        t = frame.capture_time_ms
        last = self._last_kept.get(frame.camera_id)
        if last is not None and t <= last:
            self.stats.skipped_out_of_order += 1
            return False
        period = self._config.store_period_ms
        due = self._next_due.get(frame.camera_id)
        if due is not None and t < due - period * DECIMATION_TOLERANCE:
            self.stats.skipped_by_rate += 1
            return False
        # 격자를 따라 다음 예정 시각을 잡는다. 입력이 끊겨 예정 시각을 한 주기 이상 넘겼으면 이 프레임에서 다시 시작한다
        self._next_due[frame.camera_id] = (due if due is not None and t - due < period else t) + period
        self._last_kept[frame.camera_id] = t
        return True

    def _append(self, frame: _Frame) -> None:
        window = self._config.window_start(frame.capture_time_ms)
        current = self._open.get(frame.camera_id)
        if current is not None and current.window_start_ms != window:
            self._close_file(current)
            current = None
        if current is None:
            current = self._open_file(frame.camera_id, window)
        current.buffer.extend(frame.rows)
        self.stats.frames_written += 1
        if len(current.buffer) >= self._config.row_group_rows:
            self._flush_file(current)

    def _next_part(self, camera_id: str, window_start_ms: int) -> int:
        directory = self._config.directory(camera_id, window_start_ms, self._session_id)
        used = [-1]
        if directory.is_dir():
            for path in directory.glob(f"{FILE_PREFIX}.{camera_id}.*"):
                parsed = parse_file_name(path.name)
                if parsed and parsed[0] == camera_id and parsed[1] == window_start_ms and parsed[2] == self._session_id:
                    used.append(parsed[3])
        return max(used) + 1

    def _open_file(self, camera_id: str, window_start_ms: int) -> _OpenFile:
        directory = self._config.directory(camera_id, window_start_ms, self._session_id)
        directory.mkdir(parents=True, exist_ok=True)
        part = self._next_part(camera_id, window_start_ms)
        while True:
            final = self._config.file_path(camera_id, window_start_ms, self._session_id, part)
            temp = final.with_name(final.name + TEMP_SUFFIX)
            if final.exists():
                part += 1
                continue
            try:
                handle = open(temp, "xb")  # 같은 이름이 있으면 실패: 남의 파일을 덮어쓰지 않는다
            except FileExistsError:
                part += 1
                continue
            break
        metadata = {
            "format_version": FORMAT_VERSION,
            "camera_id": camera_id,
            "window_start_ms": window_start_ms,
            "roll_interval_ms": self._config.roll_interval_ms,
            "session_id": self._session_id,
            "part": part,
            "source_id": self._source_id,
            "input_provenance": json.loads(self._provenance_json),
        }
        schema = SCHEMA.with_metadata({METADATA_KEY: json.dumps(metadata, ensure_ascii=False).encode("utf-8")})
        writer = pq.ParquetWriter(handle, schema, compression=self._config.compression)
        opened = _OpenFile(camera_id, window_start_ms, part, temp, final, handle, writer)
        self._open[camera_id] = opened
        return opened

    def _flush_file(self, f: _OpenFile) -> None:
        if not f.buffer:
            return
        table = pa.Table.from_pylist(f.buffer, schema=f.writer.schema)
        f.writer.write_table(table)
        f.rows_written += len(f.buffer)
        self.stats.rows_written += len(f.buffer)
        f.buffer.clear()

    def _close_file(self, f: _OpenFile) -> None:
        try:
            self._flush_file(f)
            f.writer.close()
        finally:
            f.handle.close()
            self._open.pop(f.camera_id, None)
        publish_no_overwrite(f.temp_path, f.final_path)
        self.stats.files_closed.append(f.final_path)

    def flush(self) -> None:
        """모은 행을 row group 으로 쓴다. 파일은 닫지 않으므로 아직 읽을 수 없다(footer 는 닫을 때 쓴다)."""
        for f in list(self._open.values()):
            self._flush_file(f)

    def close_before(self, capture_time_ms: int) -> list[Path]:
        """구간이 capture_time_ms 이전에 끝난 열린 파일을 닫는다(입력이 끊긴 카메라의 파일을 닫는 데 쓴다)."""
        require_ms("capture_time_ms", capture_time_ms)
        closed = []
        for f in list(self._open.values()):
            if f.window_start_ms + self._config.roll_interval_ms <= capture_time_ms:
                self._close_file(f)
                closed.append(f.final_path)
        return closed

    def close(self) -> list[Path]:
        """열린 파일을 모두 닫아 최종 이름으로 바꾼다. 닫은 파일 경로를 돌려준다."""
        if self._closed:
            return []
        closed = []
        errors: list[BaseException] = []
        for f in list(self._open.values()):
            try:
                self._close_file(f)
                closed.append(f.final_path)
            except Exception as exc:  # 한 파일이 실패해도 나머지는 닫는다
                errors.append(exc)
        self._closed = True
        if errors:
            raise errors[0]
        return closed


# --------------------------------------------------------------------------- 읽기


def file_metadata(path: Path | str) -> dict[str, Any]:
    """파일의 simai 메타데이터(세션, 구간, input_provenance 등)."""
    schema = pq.read_schema(path)
    raw = (schema.metadata or {}).get(METADATA_KEY)
    if raw is None:
        raise ValueError(f"{path}: simai 2D 관측 파일이 아니다 (메타데이터 {METADATA_KEY!r} 없음)")
    return json.loads(raw.decode("utf-8"))


def read_observations(
    root: Path | str,
    *,
    start_ms: int | None = None,
    end_ms: int | None = None,
    camera_ids: Iterable[str] | None = None,
    session_ids: Iterable[str] | None = None,
    columns: Sequence[str] | None = None,
) -> pa.Table:
    """닫힌 파일에서 촬영 시각 [start_ms, end_ms)·카메라·세션으로 골라 읽는다. (촬영 시각, 카메라, 프레임, 검출 순서) 정렬.

    보정 재계산(calibration_ba)·분석 도구용. 쓰는 중인 파일은 읽지 않는다. 고른 행이 없으면 빈 표다.
    """
    if start_ms is not None:
        require_ms("start_ms", start_ms)
    if end_ms is not None:
        require_ms("end_ms", end_ms)
    cams = None if camera_ids is None else list(camera_ids)
    files = list_files(root, camera_ids=cams, session_ids=session_ids, start_ms=start_ms, end_ms=end_ms)
    return read_files(files, start_ms=start_ms, end_ms=end_ms, camera_ids=cams, columns=columns)


def read_files(
    files: Sequence[StoredFile | Path],
    *,
    start_ms: int | None = None,
    end_ms: int | None = None,
    camera_ids: Iterable[str] | None = None,
    columns: Sequence[str] | None = None,
) -> pa.Table:
    """주어진 파일들을 읽어 하나의 표로 합친다 (필터는 read_observations 와 같다)."""
    sort_keys = ("capture_time_ms", "camera_id", "stream_epoch", "frame_sequence", "detection_index")
    if columns is not None:
        unknown = set(columns) - set(SCHEMA.names)
        if unknown:
            raise ValueError(f"모르는 열: {sorted(unknown)}")
    read_columns = None if columns is None else list(dict.fromkeys([*columns, *sort_keys]))
    tables = []
    for f in files:
        path = f.path if isinstance(f, StoredFile) else Path(f)
        if path.name.endswith(TEMP_SUFFIX):
            continue
        table = pq.read_table(path, columns=read_columns).replace_schema_metadata(None)
        mask = None
        if start_ms is not None:
            mask = pc.greater_equal(table["capture_time_ms"], start_ms)
        if end_ms is not None:
            m = pc.less(table["capture_time_ms"], end_ms)
            mask = m if mask is None else pc.and_(mask, m)
        if camera_ids is not None:
            m = pc.is_in(pc.cast(table["camera_id"], pa.string()), value_set=pa.array(list(camera_ids), pa.string()))
            mask = m if mask is None else pc.and_(mask, m)
        if mask is not None:
            table = table.filter(mask)
        tables.append(table)
    schema = SCHEMA if read_columns is None else pa.schema([SCHEMA.field(c) for c in read_columns])
    if not tables:
        return schema.empty_table().select(list(columns) if columns is not None else SCHEMA.names)
    # 파일마다 사전(dictionary)이 달라 문자열로 풀어 합친 뒤 다시 사전 형식으로 맞춘다
    table = pa.concat_tables([t.cast(_plain(t.schema)) for t in tables])
    table = table.sort_by([(k, "ascending") for k in sort_keys]).cast(schema)
    return table.select(list(columns)) if columns is not None else table


def _plain(schema: pa.Schema) -> pa.Schema:
    return pa.schema(
        [pa.field(f.name, f.type.value_type, f.nullable) if pa.types.is_dictionary(f.type) else f for f in schema]
    )


def iter_frames(table: pa.Table) -> Iterator[dict[str, Any]]:
    """read_observations 의 표(모든 열)를 observation_batch frame_observation 모양으로 되돌린다.

    각 항목은 계약의 frame_observation 필드에 calibration_version·joint_layout·session_id 를 더한 것이다.
    수는 float32 로 저장한 값이다.
    """
    current: dict[str, Any] | None = None
    key = None
    for row in table.to_pylist():
        k = (row["camera_id"], row["stream_epoch"], row["frame_sequence"], row["capture_time_ms"], row["session_id"])
        if k != key:
            if current is not None:
                yield current
            key = k
            current = {
                "camera_id": row["camera_id"],
                "stream_epoch": row["stream_epoch"],
                "frame_sequence": row["frame_sequence"],
                "estimated_capture_time_ms": row["capture_time_ms"],
                "timestamp_source": row["timestamp_source"],
                "timestamp_uncertainty_ms": row["timestamp_uncertainty_ms"],
                "calibration_version": row["calibration_version"],
                "joint_layout": row["joint_layout"],
                "session_id": row["session_id"],
                "detections": [],
            }
        if row["detection_index"] is not None:
            assert current is not None
            current["detections"].append(
                {
                    "local_track_id": row["local_track_id"],
                    "bbox": row["bbox"],
                    "score": row["score"],
                    "keypoints_2d": row["keypoints_2d"],
                    "world_id": row["world_id"],
                }
            )
    if current is not None:
        yield current
