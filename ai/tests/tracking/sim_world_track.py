"""시뮬레이터 시나리오로 World Track Manager 의 ID 유지를 잰다 (명세 15.1 G2 "ID 유지 시간 비율"의 SIM 대응, 부록 D-G2-04).

ai/ 에서 실행한다. 기본 설치(dev 그룹)로 돈다.

    uv run python tests/tracking/sim_world_track.py           # 표 (Markdown)
    uv run python tests/tracking/sim_world_track.py --json    # JSON

**SIM 입력의 상한 성능(`input_provenance.is_upper_bound = true`)이다. 제품 성능이나 G2 합격·결론으로 쓰지 않는다.**
G2 판정은 팀원이 단 ID 정답으로 실제 녹화에서 한다(명세 15.1).

- 입력은 시뮬레이터 `observations/` 와 `calibration.json` 만 쓴다. 변환·시나리오 정의는 sim_cross_view.py 와 같다.
  시뮬레이터 트랙 ID 를 Local Tracker 출력으로 보고, 정답 사람은 평가에만 쓴다.
- 파이프라인: LocalTrackletBuffer → CrossViewAssociator → WorldTrackManager → world_state 조립. tick(10 Hz) t 는
  촬영 시각 t + 60 ms 까지 도착한 프레임으로 풀고, t_latest_global 은 그때까지 받은 최신 촬영 시각이다.
- 시나리오:
  - crossing: 내장. 0.6 m 떨어진 평행선을 마주 걸어 엇갈림.
  - side_by_side: 0.5 m 간격으로 나란히 걷기 (sim_cross_view.py 정의).
  - occlusion: crossing 에서 east 를 2.0~3.2 s 동안 cam1 에만 보이게 가린다(나머지 카메라 검출 삭제). 가림이 끝나면
    가렸던 카메라의 로컬 트랙 ID 를 새로 붙인다(Local Tracker 가 놓쳤다가 다시 잡은 경우).
  - exit_reentry: crossing 에서 east 를 2.0~3.5 s 동안 모든 카메라에서 지우고 새 로컬 트랙 ID 로 다시 나타나게 한다.
    소실 시간(lost_after_ms 3 s)보다 짧다.
  - exit_long: 같은 방식으로 1.5~5.0 s 동안 지운다. 소실 시간보다 길어 새 ID 가 나와야 한다(늦은 재연결은
    S15P31A101-40 범위라 ID 유지 비율이 낮게 나오는 것이 지금 동작이다).
  - id_switch: crossing 에서 엇갈리는 3.0 s 부터 cam1 의 두 사람 로컬 트랙 ID 를 맞바꾼다(로컬 트래커 ID 전환). 같은
    로컬 트랙 ID 가 시각에 따라 다른 사람이라 정답은 시각별로 본다. 두 사람이 한 ID 로 합쳐지면 안 된다.

지표 (시나리오·시드마다):
- trackable: 2대 이상에 보인 (tick, 사람) 수 (가림 처리 후 입력 기준).
- id_retention: trackable 중 그 사람에게 붙은 World ID 가 그 사람의 주 ID(가장 많이 붙은 ID)인 비율. 붙은 ID 가 없는
  tick 도 실패로 센다. 사람에게 붙은 ID = 이번 tick 에 갱신된 Track 중 클러스터 view 다수가 그 사람인 Track.
- id_changes: 사람마다 붙은 ID 가 직전에 붙었던 ID 와 달라진 횟수.
- duplicate_confirmed: 같은 사람에게 CONFIRMED·OBSERVED Track 이 둘 이상인 (tick, 사람) 수 (같은 사람 두 World ID 동시 확정).
- merged_ids: 두 사람 이상에게 "붙은 ID"(클러스터 view 다수 기준)였던 적이 있는 World ID 수 (두 사람 한 ID 병합).
- impure_updates: 붙은 클러스터에 다른 사람 view 가 섞였던 (tick, Track) 수. Cross-view 단계의 섞임이다(id_switch 는
  ID 를 바꾼 tick 의 view 는 보간에 쓴 프레임 중 tick 에 가장 가까운 프레임 시각으로 정답을 붙인다).
- undecided_updates: 붙은 클러스터에서 다수 사람이 없는(동수) (tick, Track) 수. 그 tick 은 누구에게 붙었는지 판정하지
  않는다(이름 순서로 다수를 정하지 않음).
- id_reuse: LOST 로 나간 뒤 다시 나온 World ID 수 + 중복 발급 수. 목표 0.
- issued, revisions, uncertain_track_ticks: 발급 ID 수, ID 교정(병합) 수, IDENTITY_UNCERTAIN 인 (tick, Track) 수.
- schema_errors: world_state 메시지를 계약 스키마로 검증한 오류 수 (validator 를 넘겼을 때만).
"""

from __future__ import annotations

import argparse
import dataclasses
import importlib.util
import json
import tempfile
import uuid
from collections import Counter, defaultdict
from collections.abc import Callable, Iterable
from pathlib import Path
from typing import Any

from simai_ai.geometry.rig import CameraRig
from simai_ai.providers.files import FileCalibrationProvider
from simai_ai.providers.provenance import build_input_provenance
from simai_ai.tracking.cross_view import CrossViewAssociator, LocalTrackletBuffer
from simai_ai.tracking.local_tracker import Observation2DFrame
from simai_ai.tracking.world_state import world_state_message, world_state_payload
from simai_ai.tracking.world_track import (
    IdentityConfidence,
    TrackLifecycle,
    WorldIdIssuer,
    WorldTrackConfig,
    WorldTrackManager,
)

HERE = Path(__file__).resolve().parent
SCENARIOS = ("crossing", "side_by_side", "occlusion", "exit_reentry", "exit_long", "id_switch")
SEEDS = (1, 2, 3)
ARRIVAL_LAG_MS = 60
OBSERVED_GAP_MS = 200
# (사람, 시작 ms, 끝 ms, 남길 카메라) — 시나리오 시작 시각 기준
HIDE = {
    "occlusion": ("east", 2000, 3200, ("cam1",)),
    "exit_reentry": ("east", 2000, 3500, ()),
    "exit_long": ("east", 1500, 5000, ()),
}
BASE = {"crossing": "crossing", "side_by_side": "side_by_side", "occlusion": "crossing", "exit_reentry": "crossing"}
BASE["exit_long"] = "crossing"
BASE["id_switch"] = "crossing"
# (카메라, 시작 ms): 그 시각부터 이 카메라의 두 사람 로컬 트랙 ID 를 맞바꾼다 (교차 중 로컬 트래커 ID 전환)
SWITCH = {"id_switch": ("cam1", 3000)}


def _load(name: str) -> Any:
    spec = importlib.util.spec_from_file_location(name, HERE / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


xv = _load("sim_cross_view")


def make_outputs(workdir: Path, scenarios: Iterable[str] = SCENARIOS, seeds: Iterable[int] = SEEDS) -> dict:
    """(기반 시나리오, 시드)마다 시뮬레이터를 한 번 돌린다. 가림 시나리오는 crossing 출력을 다시 쓴다."""
    bases = sorted({BASE[s] for s in scenarios})
    return {(b, seed): xv.run_sim(xv.scenario_doc(b), seed, workdir) for b in bases for seed in seeds}


def hide(
    frames: list[Observation2DFrame],
    truth: dict[tuple[str, str], str],
    person: str,
    start_ms: int,
    end_ms: int,
    keep: tuple[str, ...],
) -> list[Observation2DFrame]:
    """[start, end) 동안 person 의 검출을 keep 밖 카메라에서 지우고, 그 카메라의 뒤 검출은 새 로컬 트랙 ID 로 바꾼다."""
    t0 = min(f.estimated_capture_time_ms for f in frames)
    out = []
    for f in frames:
        rel = f.estimated_capture_time_ms - t0
        dets = []
        for d in f.detections:
            key = (f.camera_id, d.local_track_id)
            if truth.get(key) != person or f.camera_id in keep:
                dets.append(d)
                continue
            if start_ms <= rel < end_ms:
                continue
            if rel >= end_ms:
                new_id = f"{d.local_track_id}-re"
                truth[(f.camera_id, new_id)] = person
                d = dataclasses.replace(d, local_track_id=new_id)
            dets.append(d)
        out.append(dataclasses.replace(f, detections=tuple(dets)))
    return out


def switch_ids(
    frames: list[Observation2DFrame], truth: dict[tuple[str, str], str], camera: str, start_ms: int
) -> tuple[list[Observation2DFrame], dict[tuple[str, str], tuple[int, str]]]:
    """start 부터 camera 의 두 사람 로컬 트랙 ID 를 맞바꾼다. (키 → (바뀐 촬영 시각, 그 뒤의 정답 사람))도 돌려준다."""
    t0 = min(f.estimated_capture_time_ms for f in frames)
    ids = sorted(k[1] for k in truth if k[0] == camera)
    if len(ids) != 2:
        raise ValueError(f"{camera} 의 로컬 트랙이 둘이 아니다: {ids}")
    swap = {ids[0]: ids[1], ids[1]: ids[0]}
    after = {(camera, ids[0]): (t0 + start_ms, truth[(camera, ids[1])]), (camera, ids[1]): (t0 + start_ms, truth[(camera, ids[0])])}
    out = []
    for f in frames:
        if f.camera_id == camera and f.estimated_capture_time_ms - t0 >= start_ms:
            dets = tuple(dataclasses.replace(d, local_track_id=swap[d.local_track_id]) for d in f.detections)
            f = dataclasses.replace(f, detections=dets)
        out.append(f)
    return out, after


def evaluate(
    out: Path,
    scenario: str,
    config: WorldTrackConfig | None = None,
    validator: Callable[[str, Any], list[str]] | None = None,
) -> dict[str, Any]:
    rig = CameraRig.from_provider(FileCalibrationProvider(out / "calibration.json"))
    frames, truth = xv.load_frames(out, "stable")
    if scenario in HIDE:
        frames = hide(frames, truth, *HIDE[scenario])
    switched: dict[tuple[str, str], tuple[int, str]] = {}
    if scenario in SWITCH:
        frames, switched = switch_ids(frames, truth, *SWITCH[scenario])

    def who(view: Any, t: int) -> str:
        """view 의 정답 사람. ID 전환 시나리오는 바뀐 시각부터 다른 사람이므로, 보간에 쓴 프레임 중 tick 에 가장 가까운
        프레임의 촬영 시각으로 본다(바꾸기 직전·직후 프레임을 보간한 view 를 tick 시각으로 라벨하면 어긋난다)."""
        key = view.key
        if key in switched:
            t_src = min(view.view.source_times, key=lambda s: (abs(s - t), s))
            if t_src >= switched[key][0]:
                return switched[key][1]
        return truth[key]

    ticks = [doc["t"] for doc in xv._jsonl(out / "world_tracks.jsonl")]
    provenance = build_input_provenance(
        {"source": "SIM", "camera_ids": sorted(rig.cameras)}, calibration=rig.calibration_provenance
    )
    associator = CrossViewAssociator(rig)
    buffer = LocalTrackletBuffer()
    manager = WorldTrackManager(WorldIdIssuer("sim1"), config, input_provenance=provenance)

    m: Counter[str] = Counter()
    matched: dict[str, list[str | None]] = defaultdict(list)
    track_person: dict[str, str] = {}
    persons_of_id: dict[str, set[str]] = defaultdict(set)
    lost_ids: set[str] = set()
    seen_ids: set[str] = set()
    latest = None
    i = 0
    for t in ticks:
        while i < len(frames) and frames[i].estimated_capture_time_ms <= t + ARRIVAL_LAG_MS:
            buffer.add(frames[i])
            latest = max(latest or 0, frames[i].estimated_capture_time_ms)
            i += 1
        views, _ = buffer.views_at(t)
        assoc = associator.associate(t, views)
        buffer.prune(t)
        result = manager.update(assoc, rig)
        t_latest = max(latest or t, t)
        payload = world_state_payload(result, t_latest, "scene-sim")
        if validator is not None:
            msg = world_state_message(payload, msg_id=str(uuid.UUID(int=t)), sent_at=t_latest)
            m["schema_errors"] += len(validator("world_state", msg))
        m["revisions"] += len(result.revisions)

        clusters = {c.association_id: c for c in assoc.clusters}
        tick_person: dict[str, str] = {}
        for tr in result.tracks:
            if tr.world_id in lost_ids:
                m["id_reuse"] += 1
            seen_ids.add(tr.world_id)
            if tr.lifecycle is TrackLifecycle.LOST:
                lost_ids.add(tr.world_id)
            if tr.identity_confidence is IdentityConfidence.IDENTITY_UNCERTAIN:
                m["uncertain_track_ticks"] += 1
            if tr.association_id is None:
                continue
            votes = Counter(who(v, t) for v in clusters[tr.association_id].views)
            if len(votes) > 1:
                m["impure_updates"] += 1
            ranked = votes.most_common()
            if len(ranked) > 1 and ranked[0][1] == ranked[1][1]:
                m["undecided_updates"] += 1  # 다수가 없다(2대 클러스터 1:1 섞임 등): 누구에게 붙었는지 판정하지 않는다
                continue
            person = ranked[0][0]
            persons_of_id[tr.world_id].add(person)
            track_person[tr.world_id] = person
            if tr.updated:
                tick_person[tr.world_id] = person

        visible: dict[str, set[str]] = defaultdict(set)
        for v in views:
            visible[who(v, t)].add(v.camera_id)
        for person, cams in sorted(visible.items()):
            if len(cams) < 2:
                continue
            m["trackable"] += 1
            ids = sorted(w for w, p in tick_person.items() if p == person)
            matched[person].append(ids[0] if ids else None)
        confirmed: Counter[str] = Counter()
        for tr in result.tracks:
            if (
                tr.lifecycle is TrackLifecycle.CONFIRMED
                and t_latest - tr.t_last_valid <= OBSERVED_GAP_MS
                and tr.world_id in track_person
            ):
                confirmed[track_person[tr.world_id]] += 1
        m["duplicate_confirmed"] += sum(1 for n in confirmed.values() if n > 1)

    kept = 0
    per_person = {}
    for person, ids in sorted(matched.items()):
        counts = Counter(w for w in ids if w is not None)
        main = min(counts, key=lambda w: (-counts[w], w)) if counts else None
        kept += counts[main] if main else 0
        per_person[person] = {"main_id": main, "ids": sorted(counts), "kept": counts[main] if main else 0, "of": len(ids)}
        prev = None
        for w in ids:
            if w is not None:
                if prev is not None and w != prev:
                    m["id_changes"] += 1
                prev = w
    issued = manager.issuer.issued
    m["id_reuse"] += len(issued) - len(set(issued))
    keys_out = (
        "trackable",
        "id_changes",
        "duplicate_confirmed",
        "id_reuse",
        "revisions",
        "impure_updates",
        "undecided_updates",
        "uncertain_track_ticks",
        "schema_errors",
    )
    return {
        "input_provenance": provenance,
        "scenario": scenario,
        **{k: int(m[k]) for k in keys_out},
        "issued": len(issued),
        "merged_ids": sum(1 for ps in persons_of_id.values() if len(ps) > 1),
        "id_retention": kept / m["trackable"] if m["trackable"] else None,
        "per_person": per_person,
    }


def evaluate_all(
    outputs: dict,
    scenarios: Iterable[str] = SCENARIOS,
    seeds: Iterable[int] = SEEDS,
    config: WorldTrackConfig | None = None,
    validator: Callable[[str, Any], list[str]] | None = None,
) -> dict[str, dict[str, Any]]:
    return {
        f"{s}/{seed}": evaluate(outputs[(BASE[s], seed)], s, config, validator) for s in scenarios for seed in seeds
    }


def run_all(scenarios: Iterable[str] = SCENARIOS, seeds: Iterable[int] = SEEDS) -> dict[str, dict[str, Any]]:
    scenarios, seeds = tuple(scenarios), tuple(seeds)
    with tempfile.TemporaryDirectory() as tmp:
        return evaluate_all(make_outputs(Path(tmp), scenarios, seeds), scenarios, seeds)


def markdown(results: dict[str, dict[str, Any]]) -> str:
    lines = [
        "| 시나리오/시드 | ID 유지 비율 | (tick, 사람) | ID 바뀜 | 같은 사람 두 ID 동시 확정 | 두 사람 한 ID | ID 재사용 "
        "| 발급 ID | ID 교정 | 섞인 클러스터 갱신 | 판정 불가 갱신 | IDENTITY_UNCERTAIN (tick, Track) |",
        "|---|---|---|---|---|---|---|---|---|---|---|---|",
    ]
    for key, r in results.items():
        ratio = "-" if r["id_retention"] is None else f"{r['id_retention']:.3f}"
        lines.append(
            f"| {key} | {ratio} | {r['trackable']} | {r['id_changes']} | {r['duplicate_confirmed']} | {r['merged_ids']} "
            f"| {r['id_reuse']} | {r['issued']} | {r['revisions']} | {r['impure_updates']} "
            f"| {r['undecided_updates']} | {r['uncertain_track_ticks']} |"
        )
    lines.append("\nSIM 입력의 상한 성능(is_upper_bound = true)이다. 제품 성능·G2 결론이 아니다.")
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()
    results = run_all()
    print(json.dumps(results, ensure_ascii=False, indent=2) if args.json else markdown(results))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
