"""시나리오 재생 정답 비교 (S15P31A101-41, 명세 21.3 '시나리오 재생', 부록 D 계층 SC).

시나리오(cases/<이름>/input.json) → SIM 장면(sim.run, 2D keypoint 정답) → 합성 녹화 세션(gateway/recorder 형식, 프레임
번호를 화소로 새긴 H.264, 사람 이미지 없음) → 파일 모드 Tier 0(input.mode "file", speed "max", 녹화 시각 주입 시계) →
world_state.jsonl·risk_event.jsonl → 기대 이벤트 파일(cases/<이름>/expected.json, 시나리오 설계에서 손으로 정한 값)과 비교.

- 검출기·Local Tracker 백엔드는 대역이다(SIM keypoint 를 그대로 낸다, 한 사람 장면). 결과는 SIM 상한이고 제품 성능이 아니다
  (AGENTS 규칙 7). 이 시험은 회귀(같은 입력 → 같은 이벤트)와 규칙 기대(시나리오 설계상 나와야 할 이벤트)를 본다.
- 장애 입력(faults)은 녹화·검출 단계에서 만든다: 카메라 연결 끊김(세그먼트 공백·재접속), 카메라별 수신 지연, 검출 누락(가림).
- 같은 시나리오를 두 번 돌려 출력이 event_id(UUIDv7 무작위 비트) 정규화 뒤 바이트 단위로 같은지도 본다.
- 형식·허용 오차 근거: docs/design/scenario-replay-golden.md.

실행(ai/ 에서, 종료 코드 0 = 모두 통과, 1 = 차이 있음, 2 = 사용법·시나리오 파일 오류):

    uv run python tests/scenarios/golden.py                  # 전체
    uv run python tests/scenarios/golden.py r1_enter_exit    # 고른 시나리오만
    uv run python tests/scenarios/golden.py --out <디렉터리>  # 재생 산출물(JSONL)을 남긴다(통합 시험 입력)
"""

from __future__ import annotations

import argparse
import copy
import json
import re
import sys
import tempfile
import threading
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
AI_DIR = HERE.parents[1]
for _p in (AI_DIR, AI_DIR / "tests" / "services" / "tier0", AI_DIR / "tests" / "io"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

import live_video as lv  # noqa: E402
import tier0_site as ts  # noqa: E402
from recording_session import Segment, write_session  # noqa: E402
from recording_session import header as frame_header  # noqa: E402

from services.tier0.file_input import RISK_EVENT_FILE, WORLD_STATE_FILE  # noqa: E402
from services.tier0.service import Tier0Service  # noqa: E402
from services.tier0.settings import load_settings  # noqa: E402
from simai_ai.io import SimObservationFrameSource  # noqa: E402
from simai_ai.providers.contract import schema_errors  # noqa: E402
from simai_ai.tracking.local_tracker import CameraDetections, PoseDetection  # noqa: E402

CASES_DIR = HERE / "cases"
RECEIVE_LAG_MS = 30  # 노트북 수신 = 촬영 + 30 ms (중계 지연은 녹화에 없다, -215 시험과 같음)
DEFAULT_LIFECYCLE = {"clear_hold_ms": 1500, "persistence": [2, 3], "resume_window_ms": 10000}
FORMAT_VERSION = 1  # expected.json 형식 버전(docs/design/scenario-replay-golden.md)


class CaseError(ValueError):
    """시나리오·기대 파일 형식 오류(종료 코드 2)."""


# --------------------------------------------------------------------------- 시나리오 읽기


def case_names() -> list[str]:
    return sorted(p.name for p in CASES_DIR.iterdir() if (p / "input.json").exists())


def load_case(name: str) -> tuple[dict[str, Any], dict[str, Any]]:
    d = CASES_DIR / name
    try:
        case = json.loads((d / "input.json").read_text(encoding="utf-8"))
        expected = json.loads((d / "expected.json").read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        raise CaseError(f"{name}: 시나리오 파일을 읽지 못했다 ({type(e).__name__}: {e})") from None
    for doc, what in ((case, "input"), (expected, "expected")):
        if doc.get("scenario_id") != name:
            raise CaseError(f"{name}: {what}.json scenario_id 가 디렉터리 이름과 다르다")
    if expected.get("format_version") != FORMAT_VERSION:
        raise CaseError(f"{name}: expected.json format_version 은 {FORMAT_VERSION} 이다")
    if case.get("d_ids") != expected.get("d_ids") or not case.get("d_ids"):
        raise CaseError(f"{name}: input·expected 의 d_ids 가 같고 비어 있지 않아야 한다")
    _check_keys(f"{name} input", case, INPUT_KEYS)
    _check_keys(f"{name} input.faults", case.get("faults", {}), FAULT_KEYS)
    _check_keys(f"{name} expected", expected, EXPECTED_KEYS)
    _check_keys(f"{name} expected.world_state", expected.get("world_state", {}), {"min_lines", "max_tracks"})
    names = {e.get("name") for e in expected.get("events", [])}
    specs = [(f"events[{i}]", e) for i, e in enumerate(expected.get("events", []))]
    for j, lim in enumerate(expected.get("known_limitations", [])):
        _check_keys(f"{name} expected.known_limitations[{j}]", lim, LIMITATION_KEYS)
        if not (lim.get("id") and lim.get("tracked_by") and lim.get("events")):
            raise CaseError(f"{name} known_limitations[{j}] 는 id·tracked_by·events 가 있어야 한다")
        for i, e in enumerate(lim["events"]):
            if "same_as" in e and e["same_as"] not in names:
                raise CaseError(f"{name} known_limitations[{j}].events[{i}].same_as 가 events 의 name 이 아니다")
            specs.append((f"known_limitations[{j}].events[{i}]", e))
    for where, event in specs:
        _check_keys(f"{name} expected.{where}", event, EVENT_KEYS)
        steps = event.get("transitions", []) + event.get("optional_tail", [])
        for t in steps + event.get("updates", []):
            _check_keys(f"{name} expected.{where} 전이·updates", t, CONDITION_KEYS)
            _window(t.get("at_ms"), f"{name} expected.{where} at_ms")
        if not all(t.get("kind") in MILESTONES for t in steps):
            raise CaseError(f"{name} expected.{where} transitions.kind 는 {MILESTONES} 중 하나다")
    return case, expected


# 기대·입력 파일의 키(밑줄로 시작하는 키는 설명 메모라 무시한다). 오타 키가 조용히 무시돼 시험이 비지 않게 모르는 키는 오류다
INPUT_KEYS = {"scenario_id", "d_ids", "description", "sim", "seed", "zones", "work_surfaces", "lifecycle", "faults"}
FAULT_KEYS = {"camera_gaps", "receive_lag_ms", "detection_dropouts"}
EXPECTED_KEYS = {"scenario_id", "d_ids", "format_version", "exhaustive", "ignore_rules", "world_state", "events",
                 "known_limitations"}
EVENT_KEYS = {"name", "rule_id", "zone_id", "severity", "world_id_count", "transitions", "optional_tail", "updates",
              "same_as", "reasons_every_line"}
LIMITATION_KEYS = {"id", "tracked_by", "events"}
CONDITION_KEYS = {"kind", "at_ms", "verification", "needs_check", "resume_count", "severity", "metrics", "reasons_absent"}
MILESTONES = ("RAISED", "RESUMED", "RESOLVED")


def _check_keys(where: str, doc: Mapping[str, Any], allowed: set[str]) -> None:
    unknown = sorted(k for k in doc if not k.startswith("_") and k not in allowed)
    if unknown:
        raise CaseError(f"{where}: 모르는 키 {unknown}")


def sim_doc(case: Mapping[str, Any]) -> dict[str, Any]:
    """tier0_site 의 교실(basic.json 배치)에 시나리오의 sim 값(people·surfaces·duration_ms 등)을 덮어쓴다."""
    doc = ts.scenario(case["sim"]["duration_ms"])
    for key, value in case["sim"].items():
        doc[key] = copy.deepcopy(value)
    doc["name"] = case["scenario_id"]
    doc["description"] = case.get("description", "")
    return doc


# --------------------------------------------------------------------------- 장면 → 합성 녹화


@dataclass
class Scene:
    run_dir: Path
    start_ms: int  # SIM 장면 시작(UTC ms). 기대 파일의 시각은 이 값 기준 상대 ms
    frames: dict[str, list[Any]]  # 카메라별 SIM 프레임(그림 번호 = 목록 위치)
    session: Path
    calibration: dict[str, Any]


def _in_window(rel_ms: int, window: Mapping[str, int]) -> bool:
    return window["from_ms"] <= rel_ms < window["to_ms"]


def build_scene(case: Mapping[str, Any], workdir: Path) -> Scene:
    doc = sim_doc(case)
    workdir.mkdir(parents=True, exist_ok=True)
    run = ts.run_sim(workdir, doc, seed=case.get("seed", 1))
    start = doc["start_time_ms"]
    src = SimObservationFrameSource(run)
    frames: dict[str, list[Any]] = {c: [] for c in src.camera_ids}
    for f in src.frames():
        frames[f.camera_id].append(f)
    faults = case.get("faults", {})
    lag = faults.get("receive_lag_ms", {})
    gaps = faults.get("camera_gaps", [])
    cameras: dict[str, list[Segment]] = {}
    for cam, fs in frames.items():
        # 끊김 구간의 프레임은 녹화에 없고, 그 뒤는 새 epoch(재접속) 세그먼트다
        runs: list[list[int]] = [[]]
        for i, f in enumerate(fs):
            rel = f.estimated_capture_time - start
            if any(g["camera_id"] == cam and _in_window(rel, g) for g in gaps):
                if runs[-1]:
                    runs.append([])
                continue
            runs[-1].append(i)
        segments = []
        for epoch, idx in enumerate(r for r in runs if r):
            video = lv.encode(idx, gop=10)
            seg = Segment(epoch, lv.codec_meta(cam, epoch, video))
            for i, (au, key) in zip(idx, video.frames, strict=True):
                f = fs[i]
                t = f.estimated_capture_time
                seg.frames.append((frame_header(cam, epoch, f.frame_ref["frame_sequence"], t, key,
                                                received_ms=t + RECEIVE_LAG_MS + lag.get(cam, 0)), au))
            segments.append(seg)
        cameras[cam] = segments
    session = write_session(workdir, f"rec-golden-{case['scenario_id'].replace('_', '-')}", cameras)
    calibration = json.loads((run / "calibration.json").read_text(encoding="utf-8"))
    return Scene(run, start, frames, session, calibration)


# --------------------------------------------------------------------------- 대역(검출기·추적기)


class TruthDetector:
    """검출기 대역: 디코딩한 그림의 번호 → 그 SIM 프레임의 2D keypoint. 시나리오의 detection_dropouts 구간·카메라에서는
    아무것도 검출하지 않는다(가림). 그림 번호와 헤더(촬영 시각·frame_sequence)가 다르면 결합 오류로 센다(D-G3-08)."""

    def __init__(self, scene: Scene, dropouts: Sequence[Mapping[str, Any]]) -> None:
        self.scene = scene
        self.dropouts = dropouts
        self.inferred = 0
        self.misbound = 0
        self.lock = threading.Lock()

    def detect(self, batch):
        out = []
        for f in batch:
            sim = self.scene.frames[f.camera_id][lv.read_index(f.image)]
            ok = (sim.frame_ref["frame_sequence"] == f.frame_ref["frame_sequence"]
                  and sim.estimated_capture_time == f.estimated_capture_time)
            rel = f.estimated_capture_time - self.scene.start_ms
            hidden = any(_in_window(rel, d) and (d.get("cameras") is None or f.camera_id in d["cameras"])
                         for d in self.dropouts)
            with self.lock:
                self.inferred += 1
                self.misbound += 0 if ok else 1
            dets = []
            for d in () if hidden else (sim.detections or ()):
                confs = [k[2] for k in d.keypoints_2d if k is not None and k[2] > 0]
                score = sum(confs) / len(confs) if confs else 0.0
                dets.append(PoseDetection(tuple(d.bbox), min(1.0, score), tuple(d.keypoints_2d)))
            out.append(CameraDetections(f.camera_id, f.frame_ref["stream_epoch"], f.frame_ref["frame_sequence"],
                                        f.estimated_capture_time, f.timestamp_source, f.timestamp_uncertainty_ms,
                                        tuple(dets)))
        return out


class OneTrackBackend:
    """Local Tracker 백엔드 대역: 검출 순서대로 트랙 1, 2, … (한 사람 장면이라 ID 가 섞이지 않는다)."""

    def __init__(self, _config) -> None:
        self._last: set[int] = set()

    def update(self, boxes, scores, features=None):
        out = [(i + 1, i) for i in range(len(boxes))]
        self._last = {t for t, _ in out}
        return out

    def live_track_ids(self):
        return frozenset(self._last), frozenset()


# --------------------------------------------------------------------------- 파일 모드 Tier 0 실행


@dataclass
class Replay:
    out_dir: Path
    world_lines: list[str]
    event_lines: list[str]
    stats: dict[str, Any] = field(default_factory=dict)

    @property
    def worlds(self) -> list[dict[str, Any]]:
        return [json.loads(x) for x in self.world_lines]

    @property
    def events(self) -> list[dict[str, Any]]:
        return [json.loads(x) for x in self.event_lines]


def sequential_ids() -> Callable[[], str]:
    """msg_id 대역(결정적): 실행마다 0 부터 센다. event_id 는 lifecycle 이 만든다(UUIDv7, 정규화로 비교)."""
    n = iter(range(10**12))
    return lambda: f"00000000-0000-4000-8000-{next(n):012d}"


def run_replay(case: Mapping[str, Any], scene: Scene, workdir: Path, *,
               new_msg_id: Callable[[], str] | None = None,
               service_kwargs: Mapping[str, Any] | None = None) -> Replay:
    workdir.mkdir(parents=True, exist_ok=True)
    ts.archive_bundle(workdir / "boot", scene.calibration, "cfg-1", scene.start_ms - 1000,
                      zones=case.get("zones"), work_surfaces=case.get("work_surfaces", []))
    out = workdir / "out"
    doc = {
        "gateway_id": ts.GATEWAY,
        "state_dir": str(workdir / "state"),
        "scene_version": "scene-sim",
        "calibration_file": str(scene.run_dir / "calibration.json"),
        "input": {"mode": "file", "session_dir": str(scene.session), "decoder": "cpu", "speed": "max",
                  "jsonl_dir": str(out)},
        "bootstrap": {"archive": str(workdir / "boot"), "config_version": "cfg-1"},
        "lifecycle": case.get("lifecycle", DEFAULT_LIFECYCLE),
        "tick_ms": 100,
        "checkpoint_interval_s": 0.5,
    }
    cfg = workdir / "tier0-file.json"
    cfg.write_text(json.dumps(doc), encoding="utf-8")
    settings = load_settings(cfg, env={})
    detector = TruthDetector(scene, case.get("faults", {}).get("detection_dropouts", []))
    service = Tier0Service(settings, detector_factory=lambda: detector, tracker_backend_factory=OneTrackBackend,
                           new_msg_id=new_msg_id or sequential_ids(), **dict(service_kwargs or {}))
    service.start()
    try:
        finished = service.finished.wait(120)
        status = service.status()
    finally:
        stopped = service.stop(timeout_s=30)
    fi = status.get("file_input") or {}
    stats = {
        "finished": finished, "stopped": stopped, "misbound": detector.misbound, "inferred": detector.inferred,
        "local_alerts": service.judge.stats.local_alerts, "journal_offset": service.journal.next_offset,
        "origin": str(service.tracker.origin), "file_errors": fi.get("errors"), "file_failed": fi.get("failed"),
        "output_failed": service.output_failed,
        "decode_failed": sum(c.get("decode_failed", 0) for c in fi.get("cameras", {}).values()),
    }
    read = (lambda name: (out / name).read_text(encoding="utf-8").splitlines() if (out / name).exists() else [])
    return Replay(out, read(WORLD_STATE_FILE), read(RISK_EVENT_FILE), stats)


# --------------------------------------------------------------------------- 비교


@dataclass
class Milestone:
    kind: str  # RAISED | RESUMED | RESOLVED
    at_ms: int  # 장면 시작 기준
    payload: dict[str, Any]


@dataclass
class ActualEvent:
    event_id: str
    rule_id: str
    zone_id: str | None
    lines: list[dict[str, Any]]  # risk_event payload, 파일 순서
    milestones: list[Milestone]


def actual_events(events: Sequence[Mapping[str, Any]], start_ms: int) -> list[ActualEvent]:
    """risk_event 줄 → 이벤트별 전이 요약. 이정표: RAISED, RESUMED(같은 event_id 의 resume_count 증가), RESOLVED.
    수치만 바뀐 UPDATED 는 이정표가 아니다(updates 조건으로만 본다)."""
    by_id: dict[str, ActualEvent] = {}
    for m in events:
        p = m["payload"]
        ev = by_id.get(p["event_id"])
        if ev is None:
            ev = by_id[p["event_id"]] = ActualEvent(p["event_id"], p["rule_id"], p["zone_id"], [], [])
        prev = ev.lines[-1] if ev.lines else None
        ev.lines.append(p)
        at = p["updated_at"] - start_ms
        if p["lifecycle"] in ("RAISED", "RESOLVED"):
            ev.milestones.append(Milestone(p["lifecycle"], at, p))
        elif prev is not None and p["resume_count"] > prev["resume_count"]:
            ev.milestones.append(Milestone("RESUMED", at, p))
    return list(by_id.values())


def _window(value: Any, name: str) -> tuple[int, int]:
    if not (isinstance(value, list) and len(value) == 2 and all(isinstance(v, int) for v in value) and value[0] <= value[1]):
        raise CaseError(f"{name} 는 [하한, 상한] ms 정수 구간이다")
    return value[0], value[1]


def _value(payload: Mapping[str, Any], key: str) -> Any:
    return len(payload["world_ids"]) if key == "world_id_count" else payload.get(key)


def _fields_match(payload: Mapping[str, Any], want: Mapping[str, Any]) -> list[str]:
    """want 의 필드(at_ms·kind 제외)가 payload 와 같은지. metrics 는 {이름: [하한, 상한]} 범위, reasons_absent 는
    confidence_reasons 에 없어야 할 이유 목록이다."""
    bad = []
    for key, value in want.items():
        if key in ("at_ms", "kind") or key.startswith("_"):
            continue
        if key == "reasons_absent":
            present = sorted(set(value) & set(payload["confidence_reasons"]))
            if present:
                bad.append(f"confidence_reasons 에 {present} 가 없어야 한다")
            continue
        if key == "metrics":
            for name, (lo, hi) in value.items():
                got = payload["metrics"].get(name)
                if not (isinstance(got, (int, float)) and lo <= got <= hi):
                    bad.append(f"metrics.{name} 기대 [{lo}, {hi}] / 실제 {got!r}")
            continue
        got = _value(payload, key)
        if got != value:
            bad.append(f"{key} 기대 {value!r} / 실제 {got!r}")
    return bad


def invariants(replay: Replay) -> list[str]:
    """기대 파일과 무관하게 모든 재생 출력(첫 번째·두 번째 실행 모두)이 지켜야 할 것."""
    diffs: list[str] = []
    st = replay.stats
    # 재생이 끝까지 돌았고, 출처·알림·결합이 맞다(LIVE 만 실제 알림, AGENTS 규칙 5)
    for key, want in (("finished", True), ("stopped", True), ("file_failed", None), ("output_failed", None),
                      ("file_errors", 0), ("decode_failed", 0), ("misbound", 0), ("local_alerts", 0),
                      ("journal_offset", 0), ("origin", "REPLAY")):
        if st.get(key) != want:
            diffs.append(f"[불변식] {key} 기대 {want!r} / 실제 {st.get(key)!r}")
    worlds, events = replay.worlds, replay.events
    if not worlds:
        diffs.append("[불변식] world_state 줄이 없다")
    for i, m in enumerate(worlds, 1):
        errs = schema_errors(m, "world_state")
        if errs:
            diffs.append(f"[계약] world_state {i}번째 줄: {errs[0]}")
            break
        if m["payload"]["input_provenance"]["frames"]["source"] != "FILE":
            diffs.append(f"[불변식] world_state {i}번째 줄 input_provenance.frames.source 가 FILE 이 아니다")
            break
    t_latest = [m["payload"]["t_latest_global"] for m in worlds]
    if t_latest != sorted(t_latest):
        diffs.append("[불변식] world_state t_latest_global 이 거꾸로 간다")
    for i, m in enumerate(events, 1):
        errs = schema_errors(m, "risk_event")
        if errs:
            diffs.append(f"[계약] risk_event {i}번째 줄: {errs[0]}")
            break
        if m["payload"]["source"] != "REPLAY":
            diffs.append(f"[불변식] risk_event {i}번째 줄 source {m['payload']['source']!r}")
            break
    seen_ids = {t["world_id"] for m in worlds for t in m["payload"]["tracks"]}
    for a in actual_events(events, 0):
        diffs += [f"[전이 일관성] {a.rule_id} {a.event_id[-6:]}: {d}" for d in _line_consistency(a, seen_ids)]
    return diffs


# 한 이벤트의 모든 줄에서 같아야 하는 필드(대상·출처). ID 교정·병합처럼 대상이 바뀌는 시나리오는 아직 없다(생기면 따로 선언한다)
_FIXED_FIELDS = ("rule_id", "rule_version", "zone_id", "world_ids", "raised_at", "gateway_id", "source")


def _line_consistency(a: ActualEvent, seen_world_ids: set[str]) -> list[str]:
    """줄 사이 의미 검사(계약 검증이 대신하지 못함, 명세 6.4): RAISED 로 시작해 event_sequence 가 1 씩 오르고, 시각이 거꾸로
    가지 않으며, 해소 뒤에는 재개(resume_count + 1 인 UPDATED)만 오고, resolved_at 은 해소 줄에만 있고, 대상(World ID·구역)이
    바뀌지 않으며, 대상 World ID 가 world_state 에 실제로 있다."""
    bad: list[str] = []
    first = a.lines[0]
    if first["lifecycle"] != "RAISED" or first["event_sequence"] != 1 or first["resume_count"] != 0:
        bad.append(f"첫 줄이 RAISED·event_sequence 1·resume_count 0 이 아니다 ({first['lifecycle']}, "
                   f"{first['event_sequence']}, {first['resume_count']})")
    for p in a.lines:
        missing = sorted(set(p["world_ids"]) - seen_world_ids)
        if missing:
            bad.append(f"@{p['updated_at']} world_state 에 없는 대상 World ID {missing}")
            break
    for prev, p in zip(a.lines, a.lines[1:], strict=False):
        at = p["updated_at"]
        if p["event_sequence"] != prev["event_sequence"] + 1:
            bad.append(f"@{at} event_sequence {prev['event_sequence']} 다음이 {p['event_sequence']}")
        if at < prev["updated_at"]:
            bad.append(f"@{at} updated_at 이 거꾸로 간다(앞 줄 {prev['updated_at']})")
        if p["lifecycle"] == "RAISED":
            bad.append(f"@{at} 같은 event_id 에 RAISED 가 다시 나왔다")
        resumed = p["resume_count"] == prev["resume_count"] + 1
        if prev["lifecycle"] == "RESOLVED" and not (p["lifecycle"] == "UPDATED" and resumed):
            bad.append(f"@{at} 해소 뒤에 재개 아닌 전이({p['lifecycle']}, resume_count {p['resume_count']})")
        elif prev["lifecycle"] != "RESOLVED" and p["resume_count"] != prev["resume_count"]:
            bad.append(f"@{at} 해소 없이 resume_count 가 {prev['resume_count']} → {p['resume_count']}")
        for key in _FIXED_FIELDS:
            if p.get(key) != first.get(key):
                bad.append(f"@{at} {key} 가 바뀌었다 {first.get(key)!r} → {p.get(key)!r}")
    for p in a.lines:
        want = p["updated_at"] if p["lifecycle"] == "RESOLVED" else None
        if p.get("resolved_at") != want:
            bad.append(f"@{p['updated_at']} {p['lifecycle']} 줄의 resolved_at 기대 {want!r} / 실제 {p.get('resolved_at')!r}")
    return bad


def compare(expected: Mapping[str, Any], replay: Replay, start_ms: int) -> list[str]:
    """기대 파일과 재생 출력을 비교해 사람이 읽을 차이 목록을 돌려준다(빈 목록 = 통과). 현재 한계는 evaluate 의 알림으로만."""
    return evaluate(expected, replay, start_ms)[0]


def evaluate(expected: Mapping[str, Any], replay: Replay, start_ms: int) -> tuple[list[str], list[str]]:
    """(차이, 현재 한계 알림). 차이는 규칙 정답(events)·공통 불변식 위반이라 실패다. 알림은 known_limitations(지금 구현의
    한계로 나는 동작, 별도 추적)가 더 이상 맞지 않는다는 뜻이라 실패가 아니고 기대 파일 갱신 대상임을 알린다."""
    diffs = invariants(replay)
    worlds = replay.worlds
    world = expected.get("world_state", {})
    if "min_lines" in world and len(worlds) < world["min_lines"]:
        diffs.append(f"[world_state] 줄 수 기대 {world['min_lines']} 이상 / 실제 {len(worlds)}")
    if "max_tracks" in world:
        most = max((len(m["payload"]["tracks"]) for m in worlds), default=0)
        if most > world["max_tracks"]:
            diffs.append(f"[world_state] 한 tick 의 트랙 수 기대 {world['max_tracks']} 이하 / 실제 최대 {most}")

    actual = actual_events(replay.events, start_ms)
    ignored = set(expected.get("ignore_rules", []))
    pending = [a for a in actual if a.rule_id not in ignored]
    matched: dict[str, ActualEvent] = {}
    for n, want in enumerate(expected.get("events", []), 1):
        label = f"[이벤트 {want.get('name', n)}]"
        match = _take(pending, want)
        if match is None:
            diffs.append(f"{label} {want['rule_id']} 이벤트가 없다")
            continue
        matched[want.get("name", str(n))] = match
        diffs += [f"{label} {d}" for d in _check_event(want, match, start_ms)]
    notices: list[str] = []
    for lim in expected.get("known_limitations", []):
        head = f"[현재 한계 {lim['id']}, {lim['tracked_by']}]"
        for want in lim["events"]:
            label = want.get("name") or want.get("same_as")
            match = matched.get(want["same_as"]) if "same_as" in want else _take(pending, want)
            if match is None:
                notices.append(f"{head} {label}: 이 이벤트가 더 이상 없다 → 한계가 바뀌었으면 기대 파일을 갱신한다")
                continue
            notices += [f"{head} {label}: {d} → 한계가 바뀌었으면 기대 파일을 갱신한다"
                        for d in _check_event(want, match, start_ms)]
    if expected.get("exhaustive", True):
        for a in pending:
            first = a.milestones[0] if a.milestones else None
            diffs.append(f"[예상 밖 이벤트] {a.rule_id} zone={a.zone_id} "
                         f"{'' if first is None else f'{first.kind} @{first.at_ms}ms'} "
                         f"근거={a.lines[0]['metrics'] if a.lines else None}")
    return diffs, notices


def _take(pending: list[ActualEvent], want: Mapping[str, Any]) -> ActualEvent | None:
    match = next((a for a in pending if a.rule_id == want["rule_id"]
                  and ("zone_id" not in want or a.zone_id == want["zone_id"])), None)
    if match is not None:
        pending.remove(match)
    return match


def _check_event(want: Mapping[str, Any], a: ActualEvent, start_ms: int) -> list[str]:
    """transitions 는 처음부터 정확히 같아야 한다. optional_tail 이 있으면 그 뒤에 optional_tail 의 앞부분이 더 와도 된다
    (예: 지금은 해소되지 않지만 해소된다면 이 창 안이어야 한다)."""
    diffs = []
    want_ms = want.get("transitions", [])
    tail = want.get("optional_tail", [])
    extra = a.milestones[len(want_ms):]
    kinds = [m.kind for m in a.milestones]
    if (kinds[:len(want_ms)] != [t["kind"] for t in want_ms] or len(extra) > len(tail)
            or [m.kind for m in extra] != [t["kind"] for t in tail[:len(extra)]]):
        shown = [t["kind"] for t in want_ms] + ([f"(+ {[t['kind'] for t in tail]} 앞부분)"] if tail else [])
        diffs.append(f"전이 순서 기대 {shown} / 실제 {[f'{m.kind}@{m.at_ms}' for m in a.milestones]}")
    else:
        for t, m in zip(want_ms + tail, a.milestones, strict=False):
            lo, hi = _window(t["at_ms"], f"{want['rule_id']} {t['kind']}.at_ms")
            if not lo <= m.at_ms <= hi:
                diffs.append(f"{t['kind']} 시각 기대 [{lo}, {hi}] ms / 실제 {m.at_ms} ms")
            diffs += [f"{t['kind']} {d}" for d in _fields_match(m.payload, t)]
    for key in ("severity", "world_id_count"):  # 이벤트의 모든 줄에서 같아야 한다(등급을 내리지 않음, 대상이 바뀌지 않음)
        if key in want:
            other = sorted({str(_value(p, key)) for p in a.lines if _value(p, key) != want[key]})
            if other:
                diffs.append(f"{key} 는 모든 줄에서 {want[key]!r} 여야 한다 / 다른 값 {other}")
    need = set(want.get("reasons_every_line", []))  # 모든 줄의 confidence_reasons 에 있어야 할 이유(D-G3-03 100% 등)
    lacking = [p["updated_at"] - start_ms for p in a.lines if not need <= set(p["confidence_reasons"])]
    if lacking:
        diffs.append(f"confidence_reasons 에 {sorted(need)} 가 없는 줄 {len(lacking)}/{len(a.lines)} (@{lacking[:5]} ms)")
    for cond in want.get("updates", []):  # 그 구간에 그 필드 값을 가진 줄이 하나 이상 있어야 한다
        lo, hi = _window(cond["at_ms"], "updates.at_ms")
        if not any(lo <= p["updated_at"] - start_ms <= hi and not _fields_match(p, cond) for p in a.lines):
            shown = {k: v for k, v in cond.items() if k != "at_ms" and not k.startswith("_")}
            diffs.append(f"[{lo}, {hi}] ms 안에 {shown} 인 줄이 없다")
    return diffs


# --------------------------------------------------------------------------- 결정성


_UUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")


def normalized(replay: Replay) -> bytes:
    """두 JSONL 을 이어 붙이고 event_id(·related_event_id) 만 처음 나온 순서 번호로 바꾼다. 나머지는 바이트 그대로."""
    ids: dict[str, str] = {}
    for m in replay.events:
        for key in ("event_id", "related_event_id"):
            v = m["payload"].get(key)
            if v is not None:
                ids.setdefault(v, f"event-{len(ids):04d}")
    text = "\n".join(replay.world_lines + ["--"] + replay.event_lines)
    return _UUID.sub(lambda m: ids.get(m.group(0), m.group(0)), text).encode("utf-8")


def determinism_diff(a: Replay, b: Replay) -> list[str]:
    na, nb = normalized(a), normalized(b)
    if na == nb:
        return []
    la, lb = na.split(b"\n"), nb.split(b"\n")
    for i, (x, y) in enumerate(zip(la, lb, strict=False), 1):
        if x != y:
            return [f"[결정성] 두 번 실행한 출력이 {i}번째 줄에서 다르다: {x[:160]!r} / {y[:160]!r}"]
    return [f"[결정성] 두 번 실행한 출력 줄 수가 다르다: {len(la)} / {len(lb)}"]


# --------------------------------------------------------------------------- 한 시나리오 / 진입점


@dataclass
class Outcome:
    name: str
    d_ids: list[str]
    diffs: list[str]
    replay: Replay | None = None
    start_ms: int = 0
    notices: list[str] = field(default_factory=list)  # 현재 한계(known_limitations)가 바뀐 곳. 실패가 아니다


def summary(replay: Replay, start_ms: int) -> list[str]:
    """실제 이벤트 요약(실패 출력용): 규칙·구역·등급·이정표 시각."""
    return [f"{a.rule_id} zone={a.zone_id} severity={a.lines[0]['severity']} world_ids={len(a.lines[0]['world_ids'])} "
            f"{' → '.join(f'{m.kind}@{m.at_ms}' for m in a.milestones)} (줄 {len(a.lines)})"
            for a in actual_events(replay.events, start_ms)]


def run_case(name: str, workdir: Path, *, repeat: bool = True, case_override: Callable[[dict], None] | None = None,
             expected_override: Callable[[dict], None] | None = None,
             service_kwargs: Mapping[str, Any] | None = None) -> Outcome:
    case, expected = load_case(name)
    if case_override is not None:
        case_override(case)
    if expected_override is not None:
        expected_override(expected)
    scene = build_scene(case, workdir / "scene")
    first = run_replay(case, scene, workdir / "run-1", service_kwargs=service_kwargs)
    diffs, notices = evaluate(expected, first, scene.start_ms)
    if repeat:
        second = run_replay(case, scene, workdir / "run-2", service_kwargs=service_kwargs)
        # 두 번째 실행도 기대·불변식을 따로 본다(바이트가 같아도 stats 의 알림·저널·결합 오류는 출력에 없다)
        diffs += [f"[두 번째 실행] {d}" for d in compare(expected, second, scene.start_ms)]
        diffs += determinism_diff(first, second)
    return Outcome(name, case["d_ids"], diffs, first, scene.start_ms, notices)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="시나리오 재생 정답 비교 (명세 21.3, 부록 D SC)")
    parser.add_argument("names", nargs="*", help="시나리오 이름(생략하면 전체)")
    parser.add_argument("--out", type=Path, help="재생 산출물을 남길 디렉터리(없거나 비어 있어야 한다)")
    parser.add_argument("--once", action="store_true", help="결정성 비교(두 번째 실행)를 건너뛴다")
    args = parser.parse_args(argv)
    names = args.names or case_names()
    unknown = [n for n in names if n not in case_names()]
    if unknown:
        print(f"없는 시나리오: {', '.join(unknown)} (있는 것: {', '.join(case_names())})")
        return 2
    if args.out is not None and args.out.exists() and any(args.out.iterdir()):
        print(f"--out 디렉터리가 비어 있지 않다(덮어쓰지 않음): {args.out}")
        return 2
    failed = 0
    with tempfile.TemporaryDirectory(prefix="simai-golden-") as tmp:
        root = args.out or Path(tmp)
        for name in names:
            try:
                outcome = run_case(name, root / name, repeat=not args.once)
            except CaseError as e:
                print(f"[오류] {e}")
                return 2
            head = f"{'통과' if not outcome.diffs else '실패'}  {name}  ({', '.join(outcome.d_ids)})"
            print(head)
            for d in outcome.diffs:
                print(f"    - {d}")
            for d in outcome.notices:
                print(f"    ! {d}")
            if outcome.diffs and outcome.replay is not None:
                print("    실제 이벤트:")
                for line in summary(outcome.replay, outcome.start_ms):
                    print(f"      {line}")
            failed += bool(outcome.diffs)
    print(f"시나리오 {len(names)}개 중 실패 {failed}개")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
