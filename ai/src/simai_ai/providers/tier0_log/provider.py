"""Tier 0 기록 → WorldTrackProvider (TIER0_LOG, 명세 20.4 규칙 1·2·3·5).

Tier 0 가 남긴 I1 `world_state` 를 `world_tracks.schema.json` 문서로 옮기고, 같은 기록의 `observation_batch`
에서 카메라별 관측(`per_camera`)을 붙인다. Tier 0 결과를 **읽기만** 한다. 삼각측량·추적·보간을 다시 하지 않고
(규칙 5) 값을 고치거나 채우지 않는다(규칙 1).

## world_state → world_tracks

| world_tracks | 출처 |
|---|---|
| `t` | `payload.t_latest_global` |
| `calibration_version`, `scene_version` | payload 값 그대로 (문서마다. 스트림 중간에 바뀌면 바뀐 대로 낸다) |
| `tracks[*]` | payload 트랙 객체 그대로(모르는 필드 포함), 순서 유지. world_state 2.x 의 UNCERTAIN `head_z` 값도 그대로 옮기므로 `head_z` 는 `head_z_validity` 와 함께 읽는다(1.x 는 UNCERTAIN 이면 null) |
| `tracks[*].per_camera` | 아래 연결 규칙 |
| `provider_source` / `provider_version` | `TIER0_LOG` / 리더 버전(+ 기록을 만든 Tier 0 빌드) |
| `coordinate_frame` | World(m, 오른손, +Z). world_state 는 World 좌표만 쓴다(I1 계약) |

## 시각 순서

- 기록은 받은 순서라 시각이 거꾸로 올 수 있다. `t` 로 정렬해 엄격히 증가하는 순서로 낸다.
- 같은 `msg_id` 는 한 번만 쓴다(재전송).
- `msg_id` 가 다른데 `t` 가 같으면 `sent_at` 이 가장 늦은 것을 쓴다(같은 관측 시각을 다시 계산한 결과,
  예: ID 교정 뒤의 identity_revision). 버린 수는 `stats.replaced_same_t` 에 남는다.
- 빈 시각(기록 공백)은 채우지 않는다. 보간하지 않고 그 시각 문서가 없을 뿐이다.

## per_camera 연결 규칙

같은 `calibration_version` 의 `observation_batch` 프레임 중, 카메라마다 촬영 시각이 `t` 에 가장 가까운 프레임
하나를 고른다(차이가 `max_obs_offset_ms` 이하일 때만, 같은 거리면 이른 프레임). 그 프레임에서 `world_id` 가 트랙과
같은 검출이 정확히 하나면 per_camera 에 넣는다. 둘 이상이면 같은 카메라의 두 검출을 한 World ID 에 묶지 않도록
그 카메라를 넣지 않는다(명세 5.2). world_id 는 Tier 0 의 카메라 간 연결 결과를 그대로 쓴다.
관측 기록이 없거나 맞는 프레임이 없으면 per_camera 는 빈 배열이다(관측을 지어내지 않음). 각 관측은 자기 프레임의
촬영 시각·`frame_ref`(stream_epoch, frame_sequence)·`timestamp_source` 를 그대로 가진다.

## 거부하는 기록

계약을 깨는 기록은 고치지 않고 위치와 함께 `Tier0LogError` 로 알린다: 필수 필드·형식 위반, 지원하지 않는 major
스키마 버전, 접지 미확인인데 `ground_anchor_xyz` 를 채운 트랙(Z=0 대체 금지), 여러 Tier 0(`source_id`)의 기록 섞임.
"""

from __future__ import annotations

import bisect
import copy
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from simai_ai.providers import contract
from simai_ai.providers.base import WorldTrackProvider, WorldTracksDoc
from simai_ai.providers.tier0_log.source import (
    OBSERVATION_BATCH,
    SUPPORTED_MESSAGE_MAJORS,
    WORLD_STATE,
    Tier0LogError,
    Tier0LogRecord,
    Tier0LogSource,
    jsonl_sources,
)

SOURCE = "TIER0_LOG"
READER_VERSION = "tier0-log-reader-0.1.0"
# 출력 문서의 schema_version (contracts 버전, contracts/README.md)
CONTRACTS_VERSION = "1.1.0"
# per_camera 프레임 선택 허용 차이. world_state 10 Hz 한 주기의 절반 (초기값)
DEFAULT_MAX_OBS_OFFSET_MS = 50
WORLD_FRAME = {"frame": "WORLD", "unit": "m", "handedness": "right", "up_axis": "+Z"}


@dataclass
class Tier0LogStats:
    """읽기 결과 집계. Run 기록·로그에 남겨 기록 품질을 본다."""

    records: int = 0
    world_states: int = 0
    observation_frames: int = 0
    ignored_messages: int = 0
    duplicate_messages: int = 0
    replaced_same_t: int = 0
    out_of_order: int = 0
    out_of_window: int = 0
    duplicate_frames: int = 0
    ambiguous_detections: int = 0
    ignored_types: dict[str, int] = field(default_factory=dict)


@dataclass(frozen=True)
class _State:
    t: int
    sent_at: int
    order: int
    msg_id: str
    calibration_version: str
    scene_version: str
    tracks: tuple[Mapping[str, Any], ...]


@dataclass(frozen=True)
class _ObsFrame:
    time: int
    camera_id: str
    joint_layout: str
    frame: Mapping[str, Any]


def _is_int(v: Any) -> bool:
    return isinstance(v, int) and not isinstance(v, bool)


def _nonempty_str(v: Any) -> bool:
    return isinstance(v, str) and bool(v)


class Tier0LogWorldTrackProvider(WorldTrackProvider):
    """Tier 0 기록(I1 world_state·observation_batch) → `world_tracks.schema.json` 문서 스트림.

    sources 는 하나 이상의 `Tier0LogSource` 다. world_state 와 observation_batch 가 다른 저장소에 있어도 된다
    (예: world_state 는 Spatial DB, 2D 관측은 Network Volume). 생성 시 전부 읽어 색인하고 `frames()` 는
    매번 새 문서(복사본)를 낸다.

    - tier0_build: 기록을 만든 Tier 0 빌드 식별자(예: git 커밋). 주면 provider_version 에 붙는다.
    - t_start_ms·t_end_ms: 문서 시각 구간 [t_start_ms, t_end_ms) 만 낸다(명세 11.4 리플레이 질의).
    - max_obs_offset_ms: per_camera 프레임 선택 허용 차이(모듈 설명).
    """

    def __init__(
        self,
        sources: Sequence[Tier0LogSource],
        *,
        tier0_build: str | None = None,
        t_start_ms: int | None = None,
        t_end_ms: int | None = None,
        max_obs_offset_ms: int = DEFAULT_MAX_OBS_OFFSET_MS,
    ) -> None:
        if not sources:
            raise ValueError("Tier 0 기록 source 가 없다")
        for name, v in (("t_start_ms", t_start_ms), ("t_end_ms", t_end_ms)):
            if v is not None and not _is_int(v):
                raise TypeError(f"{name} 는 UTC epoch ms 정수여야 한다 ({v!r})")
        if t_start_ms is not None and t_end_ms is not None and t_start_ms >= t_end_ms:
            raise ValueError(f"시각 구간이 비었다 [{t_start_ms}, {t_end_ms})")
        if not _is_int(max_obs_offset_ms) or max_obs_offset_ms < 0:
            raise ValueError(f"max_obs_offset_ms 는 0 이상 정수여야 한다 ({max_obs_offset_ms!r})")
        if tier0_build is not None and not _nonempty_str(tier0_build):
            raise ValueError("tier0_build 는 빈 문자열이 아니어야 한다")

        self.sources = tuple(sources)
        self.tier0_build = tier0_build
        self.t_start_ms = t_start_ms
        self.t_end_ms = t_end_ms
        self.max_obs_offset_ms = max_obs_offset_ms
        self.stats = Tier0LogStats()
        self.source_id: str | None = None

        self._states: list[_State] = []
        # (calibration_version, camera_id) → 촬영 시각 순서의 프레임
        self._obs: dict[tuple[str, str], list[_ObsFrame]] = {}
        self._obs_times: dict[tuple[str, str], list[int]] = {}
        self._load()
        if not self._states:
            where = ", ".join(s.describe() for s in self.sources)
            raise Tier0LogError(f"{where}: 구간 안에 world_state 기록이 없다")

    @classmethod
    def from_paths(cls, paths: Sequence[str | Path] | str | Path, **kwargs: Any) -> Tier0LogWorldTrackProvider:
        """I1 메시지 JSONL 파일(또는 그 파일들이 든 디렉터리) 경로로 만든다."""
        if isinstance(paths, (str, Path)):
            paths = [paths]
        return cls(jsonl_sources(paths), **kwargs)

    # ------------------------------------------------------------------ WorldTrackProvider

    @property
    def source(self) -> str:
        return SOURCE

    @property
    def provider_version(self) -> str:
        return READER_VERSION if self.tier0_build is None else f"{READER_VERSION}+{self.tier0_build}"

    def frames(self) -> Iterator[WorldTracksDoc]:
        for state in self._states:
            yield self._document(state)

    # ------------------------------------------------------------------ 조회

    @property
    def times(self) -> tuple[int, ...]:
        return tuple(s.t for s in self._states)

    @property
    def calibration_versions(self) -> tuple[str, ...]:
        """나온 순서(시각 순)의 calibration_version (중복 제거)."""
        return tuple(dict.fromkeys(s.calibration_version for s in self._states))

    # ------------------------------------------------------------------ 읽기

    def _load(self) -> None:
        by_msg_id: set[str] = set()
        frame_keys: set[tuple[str, int, int]] = set()
        by_t: dict[int, _State] = {}
        last_t: int | None = None
        order = 0
        lo = None if self.t_start_ms is None else self.t_start_ms - self.max_obs_offset_ms
        hi = None if self.t_end_ms is None else self.t_end_ms + self.max_obs_offset_ms
        for src in self.sources:
            for rec in src.records(self.t_start_ms, self.t_end_ms):
                self.stats.records += 1
                msg_type = rec.message.get("msg_type")
                if msg_type not in (WORLD_STATE, OBSERVATION_BATCH):
                    self.stats.ignored_messages += 1
                    key = str(msg_type)
                    self.stats.ignored_types[key] = self.stats.ignored_types.get(key, 0) + 1
                    continue
                msg_id = self._check_envelope(rec)
                if msg_id in by_msg_id:
                    self.stats.duplicate_messages += 1
                    continue
                by_msg_id.add(msg_id)
                if msg_type == WORLD_STATE:
                    state = self._parse_world_state(rec, order)
                    order += 1
                    if last_t is not None and state.t < last_t:
                        self.stats.out_of_order += 1
                    last_t = state.t
                    if not self._in_window(state.t):
                        self.stats.out_of_window += 1
                        continue
                    prev = by_t.get(state.t)
                    if prev is not None:
                        self.stats.replaced_same_t += 1
                        if (prev.sent_at, prev.order) > (state.sent_at, state.order):
                            continue
                    by_t[state.t] = state
                else:
                    for obs in self._parse_observation_batch(rec):
                        if (lo is not None and obs.time < lo) or (hi is not None and obs.time >= hi):
                            continue
                        f = obs.frame
                        key3 = (obs.camera_id, f["stream_epoch"], f["frame_sequence"])
                        if key3 in frame_keys:
                            self.stats.duplicate_frames += 1
                            continue
                        frame_keys.add(key3)
                        cv = rec.message["payload"]["calibration_version"]
                        linked = [d.get("world_id") for d in f["detections"] if d.get("world_id") is not None]
                        self.stats.ambiguous_detections += len(linked) - len(set(linked))
                        self._obs.setdefault((cv, obs.camera_id), []).append(obs)
                        self.stats.observation_frames += 1
        self._states = [by_t[t] for t in sorted(by_t)]
        self.stats.world_states = len(self._states)
        for key, frames in self._obs.items():
            frames.sort(key=lambda o: (o.time, o.frame["stream_epoch"], o.frame["frame_sequence"]))
            self._obs_times[key] = [o.time for o in frames]

    def _in_window(self, t: int) -> bool:
        return (self.t_start_ms is None or t >= self.t_start_ms) and (self.t_end_ms is None or t < self.t_end_ms)

    def _check_envelope(self, rec: Tier0LogRecord) -> str:
        m, where = rec.message, rec.where
        version = m.get("schema_version")
        if not isinstance(version, str) or not version.split(".")[0].isdigit():
            raise Tier0LogError(f"{where}: schema_version {version!r} 이 semver 가 아니다")
        majors = SUPPORTED_MESSAGE_MAJORS.get(str(m.get("msg_type")), frozenset())
        if int(version.split(".")[0]) not in majors:
            raise Tier0LogError(
                f"{where}: {m.get('msg_type')} schema_version {version} 는 지원하지 않는다 "
                f"(major {sorted(majors)} 만)"
            )
        if not _nonempty_str(m.get("msg_id")):
            raise Tier0LogError(f"{where}: msg_id 가 없다")
        if not _is_int(m.get("sent_at")):
            raise Tier0LogError(f"{where}: sent_at 이 UTC epoch ms 정수가 아니다")
        source_id = m.get("source_id")
        if not _nonempty_str(source_id):
            raise Tier0LogError(f"{where}: source_id 가 없다")
        if self.source_id is None:
            self.source_id = source_id
        elif source_id != self.source_id:
            raise Tier0LogError(
                f"{where}: 다른 Tier 0 기록이 섞였다 (source_id {source_id} != {self.source_id}). 한 실행의 기록만 준다"
            )
        if not isinstance(m.get("payload"), dict):
            raise Tier0LogError(f"{where}: payload 가 객체가 아니다")
        return m["msg_id"]

    def _parse_world_state(self, rec: Tier0LogRecord, order: int) -> _State:
        p, where = rec.message["payload"], rec.where
        t = p.get("t_latest_global")
        if not _is_int(t) or t < 0:
            raise Tier0LogError(f"{where}: t_latest_global 이 UTC epoch ms 정수가 아니다 ({t!r})")
        for key in ("calibration_version", "scene_version"):
            if not _nonempty_str(p.get(key)):
                raise Tier0LogError(f"{where}: {key} 가 없다 (위치는 보정·Scene 버전을 알아야 한다, 명세 11.4)")
        tracks = p.get("tracks")
        if not isinstance(tracks, list):
            raise Tier0LogError(f"{where}: tracks 가 배열이 아니다")
        for i, tr in enumerate(tracks):
            if not isinstance(tr, dict) or not _nonempty_str(tr.get("world_id")):
                raise Tier0LogError(f"{where} tracks[{i}]: world_id 가 있는 트랙 객체가 아니다")
        # Z=0 대체 기록은 고치지 않고 거부한다 (공통 계약 시험과 같은 규칙, 명세 5.2)
        violations = contract.check_ground_anchor({"t": t, "tracks": tracks})
        if violations:
            raise Tier0LogError(f"{where}: " + "; ".join(violations))
        return _State(
            t=t,
            sent_at=rec.message["sent_at"],
            order=order,
            msg_id=rec.message["msg_id"],
            calibration_version=p["calibration_version"],
            scene_version=p["scene_version"],
            tracks=tuple(copy.deepcopy(tracks)),
        )

    def _parse_observation_batch(self, rec: Tier0LogRecord) -> list[_ObsFrame]:
        p, where = rec.message["payload"], rec.where
        if not _nonempty_str(p.get("calibration_version")):
            raise Tier0LogError(f"{where}: calibration_version 이 없다")
        layout = p.get("joint_layout")
        if not _nonempty_str(layout):
            raise Tier0LogError(f"{where}: joint_layout 이 없다")
        frames = p.get("frames")
        if not isinstance(frames, list):
            raise Tier0LogError(f"{where}: frames 가 배열이 아니다")
        out = []
        for i, f in enumerate(frames):
            fw = f"{where} frames[{i}]"
            if not isinstance(f, dict) or not _nonempty_str(f.get("camera_id")):
                raise Tier0LogError(f"{fw}: camera_id 가 있는 프레임 객체가 아니다")
            for key in ("stream_epoch", "frame_sequence", "estimated_capture_time_ms"):
                if not _is_int(f.get(key)) or f[key] < 0:
                    raise Tier0LogError(f"{fw}: {key} 가 0 이상 정수가 아니다")
            if not _nonempty_str(f.get("timestamp_source")):
                raise Tier0LogError(f"{fw}: timestamp_source 가 없다 (촬영 시각 계약, 명세 3.9)")
            if not isinstance(f.get("detections"), list):
                raise Tier0LogError(f"{fw}: detections 가 배열이 아니다")
            for k, det in enumerate(f["detections"]):
                if not isinstance(det, dict) or not _nonempty_str(det.get("local_track_id")):
                    raise Tier0LogError(f"{fw} detections[{k}]: local_track_id 가 있는 검출 객체가 아니다")
            out.append(_ObsFrame(f["estimated_capture_time_ms"], f["camera_id"], layout, copy.deepcopy(f)))
        return out

    # ------------------------------------------------------------------ 문서 만들기

    def _document(self, state: _State) -> WorldTracksDoc:
        frames = self._frames_near(state.calibration_version, state.t)
        tracks = []
        for tr in state.tracks:
            out = copy.deepcopy(dict(tr))
            out["per_camera"] = self._per_camera(tr["world_id"], frames)
            tracks.append(out)
        return {
            "schema_version": CONTRACTS_VERSION,
            "t": state.t,
            "provider_source": SOURCE,
            "provider_version": self.provider_version,
            "calibration_version": state.calibration_version,
            "scene_version": state.scene_version,
            "coordinate_frame": dict(WORLD_FRAME),
            "tracks": tracks,
        }

    def _frames_near(self, calibration_version: str, t: int) -> list[_ObsFrame]:
        """카메라마다 t 에 가장 가까운 프레임 하나 (허용 차이 안, 같은 거리면 이른 쪽). camera_id 순서."""
        out = []
        for (cv, camera_id), times in sorted(self._obs_times.items()):
            if cv != calibration_version:
                continue
            i = bisect.bisect_left(times, t)
            candidates = [j for j in (i - 1, i) if 0 <= j < len(times)]
            if not candidates:
                continue
            best = min(candidates, key=lambda j: (abs(times[j] - t), times[j]))
            if abs(times[best] - t) <= self.max_obs_offset_ms:
                out.append(self._obs[(cv, camera_id)][best])
        return out

    def _per_camera(self, world_id: str, frames: Sequence[_ObsFrame]) -> list[dict[str, Any]]:
        out = []
        for obs in frames:
            matches = [d for d in obs.frame["detections"] if d.get("world_id") == world_id]
            if len(matches) != 1:
                continue
            det, f = matches[0], obs.frame
            entry: dict[str, Any] = {
                "camera_id": obs.camera_id,
                "local_track_id": det["local_track_id"],
                "bbox": list(det["bbox"]),
            }
            if det.get("keypoints_2d") is not None:
                entry["keypoints_2d"] = copy.deepcopy(det["keypoints_2d"])
                entry["joint_layout"] = obs.joint_layout
            entry["frame_ref"] = {"stream_epoch": f["stream_epoch"], "frame_sequence": f["frame_sequence"]}
            entry["estimated_capture_time"] = f["estimated_capture_time_ms"]
            entry["timestamp_source"] = f["timestamp_source"]
            if f.get("timestamp_uncertainty_ms") is not None:
                entry["timestamp_uncertainty_ms"] = f["timestamp_uncertainty_ms"]
            out.append(entry)
        return out
