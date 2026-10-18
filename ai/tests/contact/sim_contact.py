"""시뮬레이터 40 cm 단상 시나리오로 ContactEstimator 를 잰다 (부록 D-G2-03·D-G2-05·D-G2-07 의 SIM 대응, 명세 15.1 G2).

ai/ 에서 실행한다. 기본 설치(dev 그룹)로 돈다.

    uv run python tests/contact/sim_contact.py           # 표 (Markdown)
    uv run python tests/contact/sim_contact.py --json    # JSON

**SIM 입력의 상한 성능(`input_provenance.is_upper_bound = true`)이다. 제품 성능이나 G2 합격·결론으로 쓰지 않는다.**
시뮬레이터 발목은 늘 지지 표면 위 같은 높이이고(걸을 때 발이 뜨지 않음) 발 미끄러짐·신발이 없다.

- 입력은 시뮬레이터 `observations/` 와 `calibration.json` 만 쓴다(tests/tracking/sim_world_track.py 와 같은 변환).
  파이프라인: LocalTrackletBuffer → CrossViewAssociator → WorldTrackManager → ContactEstimator → world_state 조립.
- 시나리오: 내장 podium 의 단상 높이를 0.4 m 로 바꾼 podium40 (교사는 단상 위, 학생은 바닥 → 단상 → 바닥).
  등록 작업면은 시나리오 상자(축 정렬)를 임시 형식(`contact.surfaces`)으로 옮긴 Operational Geometry 다.
  - podium40: 그대로.
  - single_view: 교사를 2.0~4.0 s 동안 cam1 에만 보이게 가린다(나머지 카메라 검출 삭제, 끝나면 새 로컬 트랙 ID).
  - long_hide: 같은 방식으로 1.5~6.0 s 동안 가린다. 유지 상한(3 s)과 World Track 소실(3 s)이 거의 함께 온다.
  - short_hold: long_hide 를 유지 상한 1.5 s 로 돈다. 소실 전에 유지가 끝나 UNKNOWN 이 되는지(바닥이 아닌지) 본다.
  - unregistered: podium40 을 등록 작업면 없이(바닥만) 돈다(작업면 미등록).
- 정답 사람은 평가에만 쓴다: 이번 tick 에 갱신된 Track 은 클러스터 view 다수의 사람, 갱신되지 않은 Track 은 마지막에
  붙었던 사람.

지표 (시나리오·시드마다):
- person_ticks: 사람이 정해진 (tick, Track) 수. podium_ticks: 그중 정답 지지 표면이 단상인 수.
- floor_misread (D-G2-05): podium_ticks 중 support_surface_id 가 floor 인 수 (VALID·UNCERTAIN 모두). 목표 0.
- z0_collapse (D-G2-03): podium_ticks 중 world_state 의 ground_anchor_xyz Z 가 단상 높이 절반보다 낮은 수. 목표 0.
  single_view_podium_ticks / single_view_z0_collapse: 그중 그 사람이 2대 미만에 보인 tick.
- wrong_surface: 지지 표면이 VALID 인데 정답 표면과 다른 (tick, Track) 수.
- valid / uncertain / unknown / held: 지지 표면 VALID / UNCERTAIN / UNKNOWN, 유지(surface_source inferred) 수.
- hold_expired: 유지 상한이 지나 유지를 끝낸(HOLD_EXPIRED) 수.
- unknown_ratio (D-G2-07): (UNCERTAIN + UNKNOWN) / person_ticks. 측정·기록 항목이다.
- anchor_z_max_error_m: VALID 접지점 Z 와 정답 지지 표면 높이의 최대 차.
- schema_errors: world_state 메시지를 계약 스키마로 검증한 오류 수. validator 를 넘기지 않으면(CLI 기본) None 이다
  ("0건 검증"으로 읽히지 않게). 스키마 검증은 tests/contact/test_contact_sim.py 가 시드 1 에서 한다.
"""

from __future__ import annotations

import argparse
import copy
import importlib.util
import json
import tempfile
import uuid
from collections import Counter, defaultdict
from collections.abc import Callable, Iterable
from pathlib import Path
from typing import Any

from simai_ai.contact import FLOOR_ID, ContactConfig, ContactEstimator, ContactReason, SurfaceSet
from simai_ai.geometry.rig import CameraRig
from simai_ai.providers.files import FileCalibrationProvider
from simai_ai.providers.provenance import build_input_provenance
from simai_ai.tracking.cross_view import CrossViewAssociator, LocalTrackletBuffer
from simai_ai.tracking.world_state import world_state_message, world_state_payload
from simai_ai.tracking.world_track import WorldIdIssuer, WorldTrackManager

HERE = Path(__file__).resolve().parent
AI_DIR = HERE.parents[1]
PODIUM_HEIGHT_M = 0.4
SCENARIOS = ("podium40", "single_view", "long_hide", "short_hold", "unregistered")
SEEDS = (1, 2, 3)
ARRIVAL_LAG_MS = 60
# (사람, 시작 ms, 끝 ms, 남길 카메라) — 시나리오 시작 시각 기준
HIDE = {
    "single_view": ("teacher", 2000, 4000, ("cam1",)),
    "long_hide": ("teacher", 1500, 6000, ("cam1",)),
    "short_hold": ("teacher", 1500, 6000, ("cam1",)),
}
# 시나리오별 ContactEstimator 설정 (없으면 기본값)
CONFIGS = {"short_hold": ContactConfig(hold_max_ms=1500)}


def _load(name: str, path: Path) -> Any:
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


wt = _load("sim_world_track", HERE.parent / "tracking" / "sim_world_track.py")
xv = wt.xv


def scenario_doc() -> dict[str, Any]:
    """내장 podium 의 단상 높이를 0.4 m 로 바꾼 시나리오."""
    doc = json.loads((AI_DIR / "sim" / "scenarios" / "podium.json").read_text(encoding="utf-8"))
    doc = copy.deepcopy(doc)
    doc["name"] = "podium40"
    doc["description"] = "단상 0.4 m: 교사는 단상 위를 오가고 학생 한 명은 바닥에서 단상에 올랐다 내려온다"
    for s in doc["surfaces"]:
        s["box_max"][2] = PODIUM_HEIGHT_M
    return doc


def operational_geometry(doc: dict[str, Any]) -> dict[str, Any]:
    """시나리오 상자를 등록 작업면 임시 형식으로 옮긴다 (관리자가 OBB 와 상판 높이를 입력한 것에 해당)."""
    surfaces = []
    for s in doc["surfaces"]:
        (x0, y0, _), (x1, y1, z1) = s["box_min"], s["box_max"]
        surfaces.append(
            {
                "surface_id": s["id"],
                "top_height_m": z1,
                "center_xy": [(x0 + x1) / 2, (y0 + y1) / 2],
                "size_xy": [x1 - x0, y1 - y0],
                "yaw_rad": 0.0,
            }
        )
    return {"version": f"geom-{doc['name']}", "work_surfaces": surfaces, "static_objects": []}


def make_outputs(workdir: Path, seeds: Iterable[int] = SEEDS) -> dict[int, Path]:
    doc = scenario_doc()
    return {seed: xv.run_sim(doc, seed, workdir) for seed in seeds}


def evaluate(
    out: Path, scenario: str, validator: Callable[[str, Any], list[str]] | None = None
) -> dict[str, Any]:
    rig = CameraRig.from_provider(FileCalibrationProvider(out / "calibration.json"))
    frames, truth = xv.load_frames(out, "stable")
    if scenario in HIDE:
        frames = wt.hide(frames, truth, *HIDE[scenario])
    surfaces = (
        SurfaceSet.floor_only()
        if scenario == "unregistered"
        else SurfaceSet.from_operational_geometry(operational_geometry(scenario_doc()))
    )
    truth_rows = {row["t"]: {p["person_id"]: p for p in row["people"]} for row in xv._jsonl(out / "truth" / "world_truth.jsonl")}
    provenance = build_input_provenance(
        {"source": "SIM", "camera_ids": sorted(rig.cameras)}, calibration=rig.calibration_provenance
    )
    associator = CrossViewAssociator(rig)
    buffer = LocalTrackletBuffer()
    manager = WorldTrackManager(WorldIdIssuer("sim1"), input_provenance=provenance)
    estimator = ContactEstimator(CONFIGS.get(scenario))

    m: Counter[str] = Counter()
    track_person: dict[str, str] = {}
    z_err = 0.0
    latest = None
    i = 0
    for t in sorted(truth_rows):
        while i < len(frames) and frames[i].estimated_capture_time_ms <= t + ARRIVAL_LAG_MS:
            buffer.add(frames[i])
            latest = max(latest or 0, frames[i].estimated_capture_time_ms)
            i += 1
        views, _ = buffer.views_at(t)
        assoc = associator.associate(t, views)
        buffer.prune(t)
        result = manager.update(assoc, rig)
        contact_frame = estimator.update(result, surfaces)
        t_latest = max(latest or t, t)
        payload = world_state_payload(result, t_latest, "scene-sim", contacts=contact_frame.contacts())
        if validator is not None:
            msg = world_state_message(payload, msg_id=str(uuid.UUID(int=t)), sent_at=t_latest)
            m["schema_errors"] += len(validator("world_state", msg))
        docs = {d["world_id"]: d for d in payload["tracks"]}

        clusters = {c.association_id: c for c in assoc.clusters}
        cams_of: dict[str, set[str]] = defaultdict(set)
        for v in views:
            cams_of[truth[v.key]].add(v.camera_id)
        for tr in result.tracks:
            if tr.association_id is not None:
                votes = Counter(truth[v.key] for v in clusters[tr.association_id].views).most_common()
                if len(votes) == 1 or votes[0][1] > votes[1][1]:
                    track_person[tr.world_id] = votes[0][0]
            person = track_person.get(tr.world_id)
            if person is None:
                continue
            gt = truth_rows[t][person]
            doc = docs[tr.world_id]
            est = contact_frame.estimates[tr.world_id]
            m["person_ticks"] += 1
            validity = doc["support_surface_validity"]
            m[validity.lower()] += 1
            if est.contact.surface_source == "inferred":
                m["held"] += 1
            if ContactReason.HOLD_EXPIRED in est.reasons:
                m["hold_expired"] += 1
            anchor = doc["ground_anchor_xyz"]
            on_podium = gt["support_surface_id"] != FLOOR_ID
            single_view = len(cams_of.get(person, ())) < 2
            if on_podium:
                m["podium_ticks"] += 1
                if doc["support_surface_id"] == FLOOR_ID:
                    m["floor_misread"] += 1
                collapsed = anchor is not None and anchor[2] < gt["support_z"] / 2
                m["z0_collapse"] += int(collapsed)
                if single_view:
                    m["single_view_podium_ticks"] += 1
                    m["single_view_z0_collapse"] += int(collapsed)
            if validity == "VALID":
                if doc["support_surface_id"] != gt["support_surface_id"]:
                    m["wrong_surface"] += 1
                if anchor is not None:
                    z_err = max(z_err, abs(anchor[2] - gt["support_z"]))

    keys = (
        "person_ticks",
        "podium_ticks",
        "floor_misread",
        "z0_collapse",
        "single_view_podium_ticks",
        "single_view_z0_collapse",
        "wrong_surface",
        "valid",
        "uncertain",
        "unknown",
        "held",
        "hold_expired",
    )
    n = m["person_ticks"]
    return {
        "input_provenance": provenance,
        "scenario": scenario,
        **{k: int(m[k]) for k in keys},
        "schema_errors": int(m["schema_errors"]) if validator is not None else None,
        "unknown_ratio": (m["uncertain"] + m["unknown"]) / n if n else None,
        "anchor_z_max_error_m": round(z_err, 4),
        "issued": len(manager.issuer.issued),
    }


def evaluate_all(
    outputs: dict[int, Path],
    scenarios: Iterable[str] = SCENARIOS,
    validator: Callable[[str, Any], list[str]] | None = None,
) -> dict[str, dict[str, Any]]:
    return {f"{s}/{seed}": evaluate(out, s, validator) for s in scenarios for seed, out in outputs.items()}


def run_all(scenarios: Iterable[str] = SCENARIOS, seeds: Iterable[int] = SEEDS) -> dict[str, dict[str, Any]]:
    with tempfile.TemporaryDirectory() as tmp:
        return evaluate_all(make_outputs(Path(tmp), tuple(seeds)), tuple(scenarios))


def markdown(results: dict[str, dict[str, Any]]) -> str:
    lines = [
        "| 시나리오/시드 | (tick, 사람) | 단상 tick | 바닥 오인 | Z=0 내려앉음 | 1대 관측 단상 tick | 그중 내려앉음 "
        "| 잘못된 VALID 표면 | VALID | UNCERTAIN | UNKNOWN | 유지 | 유지 만료 | UNKNOWN 비율 | 접지 Z 최대 오차 (m) | 발급 ID |",
        "|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|",
    ]
    for key, r in results.items():
        ratio = "-" if r["unknown_ratio"] is None else f"{r['unknown_ratio']:.3f}"
        lines.append(
            f"| {key} | {r['person_ticks']} | {r['podium_ticks']} | {r['floor_misread']} | {r['z0_collapse']} "
            f"| {r['single_view_podium_ticks']} | {r['single_view_z0_collapse']} | {r['wrong_surface']} | {r['valid']} "
            f"| {r['uncertain']} | {r['unknown']} | {r['held']} | {r['hold_expired']} | {ratio} | {r['anchor_z_max_error_m']:.4f} | {r['issued']} |"
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
