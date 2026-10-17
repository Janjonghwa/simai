"""시뮬레이터 고정 시나리오로 Cross-view Association 의 오병합·ID 연결 정확도를 잰다 (docs/design/tier0-cross-view.md 의 표).

ai/ 에서 실행한다. 기본 설치(dev 그룹)로 돈다.

    uv run python tests/tracking/sim_cross_view.py                 # 표 (Markdown)
    uv run python tests/tracking/sim_cross_view.py --json          # 시험(test_cross_view_sim.py)이 읽는 JSON
    uv run python tests/tracking/sim_cross_view.py --no-exclusive  # 음성 대조: 같은 카메라 금지 제약을 끈 결과

- 시나리오: crossing(내장, 0.6 m 떨어진 평행선을 마주 걸어 엇갈림), side_by_side(이 스크립트에서 정의, 0.5 m 간격으로
  나란히 대각선·직선을 걸음). 시드 1·2·3 으로 각각 세 번 돌린다(픽셀 잡음·촬영 지터·보고 시각 오차가 바뀐다).
- 입력은 시뮬레이터 `observations/` 와 `calibration.json` 만 쓴다. 시뮬레이터가 붙인 카메라 내 트랙 ID 를
  Local Tracker 출력(`observation_2d`)으로 본다. 정답 사람은 그 ID 의 사람 번호(`{camera}-{i+1}`)로만 평가에 쓴다.
- 변형: stable(시뮬레이터 트랙 ID 그대로), fragmented(카메라마다 1.5 s 마다 트랙 ID 를 새로 붙여 Local Tracker 의
  ID 조각남을 흉내 냄. 카메라마다 시작 위상이 다르다).
- 공통 시각 t 는 world_tracks.jsonl 의 tick(10 Hz)이다. tick t 는 촬영 시각 t + 60 ms 까지의 프레임이 도착한 뒤 푼다.
- 결과는 SIM 입력의 상한 성능이다(`input_provenance.is_upper_bound = true`). 제품 성능이나 G2 결론으로 쓰지 않는다.

지표 (시나리오·시드·변형마다):
- same_camera_merges: 같은 카메라의 view 두 개가 든 (tick, 클러스터) 수. 부록 D-G2-04 의 SIM 대응. 목표 0.
- impure_clusters: 서로 다른 정답 사람의 view 가 섞인 (tick, 클러스터) 수(카메라 무관 오병합).
- pair_precision / pair_recall: 같은 tick 의 카메라가 다른 view 쌍마다 "같은 클러스터" 예측을 정답과 비교.
- full_association: 2대 이상에 보인 (tick, 사람) 중 그 사람의 view 가 전부 한 클러스터에 있고 그 클러스터가 순수한 비율.
- composition_changes: 직전 tick 과 지금 모두 있는 로컬 트랙끼리의 묶음이 달라진 tick 수(구성 흔들림).
- id_switches: 사람마다 view 를 가장 많이 담은 클러스터의 association_id 가 직전 tick 과 달라진 횟수.
- uncertain_clusters: IDENTITY_UNCERTAIN 사유가 붙은 (tick, 클러스터) 수.
"""

from __future__ import annotations

import argparse
import copy
import itertools
import json
import os
import subprocess
import sys
import tempfile
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

from simai_ai.geometry.rig import CameraRig
from simai_ai.providers.files import FileCalibrationProvider
from simai_ai.providers.provenance import build_input_provenance
from simai_ai.tracking.cross_view import CrossViewAssociator, CrossViewConfig, LocalTrackletBuffer
from simai_ai.tracking.local_tracker import Observation2DFrame, PoseDetection, TrackedDetection

AI_DIR = Path(__file__).resolve().parents[2]
SCENARIOS = ("crossing", "side_by_side")
SEEDS = (1, 2, 3)
VARIANTS = ("stable", "fragmented")
FRAGMENT_PERIOD_MS = 1500
FRAGMENT_PHASE_MS = 400
ARRIVAL_LAG_MS = 60


def scenario_doc(name: str) -> dict[str, Any]:
    crossing = json.loads((AI_DIR / "sim" / "scenarios" / "crossing.json").read_text(encoding="utf-8"))
    if name == "crossing":
        return crossing
    if name != "side_by_side":
        raise ValueError(f"모르는 시나리오: {name}")
    doc = copy.deepcopy(crossing)
    doc.update(
        name="side_by_side",
        description="나란히 걷기: 두 사람이 0.5 m 간격으로 나란히 대각선을 걷고 꺾어 직선으로 돌아온다",
        seed=46034,
        duration_ms=8500,
        people=[
            {
                "id": "left",
                "height_m": 1.70,
                "waypoints": [
                    {"t_ms": 0, "xy": [1.5, 2.0]},
                    {"t_ms": 4500, "xy": [6.5, 5.0]},
                    {"t_ms": 8000, "xy": [2.0, 5.0]},
                ],
            },
            {
                "id": "right",
                "height_m": 1.66,
                "waypoints": [
                    {"t_ms": 0, "xy": [1.5, 2.5]},
                    {"t_ms": 4500, "xy": [6.5, 5.5]},
                    {"t_ms": 8000, "xy": [2.0, 5.5]},
                ],
            },
        ],
    )
    return doc


def run_sim(doc: dict[str, Any], seed: int, workdir: Path) -> Path:
    path = workdir / f"{doc['name']}.json"
    path.write_text(json.dumps(doc, ensure_ascii=False), encoding="utf-8")
    out = workdir / f"{doc['name']}-{seed}"
    result = subprocess.run(
        [sys.executable, "-m", "sim.run", "--scenario", str(path), "--out", str(out), "--seed", str(seed)],
        check=False,
        cwd=AI_DIR,
        capture_output=True,
        text=True,
        encoding="utf-8",
        env=dict(os.environ, PYTHONUTF8="1"),
    )
    if result.returncode != 0:
        raise RuntimeError(f"시뮬레이터 실패: {result.stderr}")
    return out


def _jsonl(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def load_frames(out: Path, variant: str) -> tuple[list[Observation2DFrame], dict[tuple[str, str], str]]:
    """시뮬레이터 관측 → 촬영 시각 순서의 observation_2d 와 (카메라, 로컬 트랙) → 정답 사람."""
    if variant not in VARIANTS:
        raise ValueError(f"모르는 변형: {variant}")
    manifest = json.loads((out / "manifest.json").read_text(encoding="utf-8"))
    people = [p["person_id"] for p in manifest["people"]]
    cameras = sorted(p.stem for p in (out / "observations").glob("*.jsonl"))
    start = min(
        row["estimated_capture_time_ms"] for cam in cameras for row in _jsonl(out / "observations" / f"{cam}.jsonl")
    )
    frames, truth = [], {}
    for ci, cam in enumerate(cameras):
        for row in _jsonl(out / "observations" / f"{cam}.jsonl"):
            dets = []
            for d in row["detections"]:
                sim_id = d["local_track_id"]
                person = people[int(sim_id.rsplit("-", 1)[1]) - 1]
                local_id = sim_id
                if variant == "fragmented":
                    k = (row["estimated_capture_time_ms"] - start + ci * FRAGMENT_PHASE_MS) // FRAGMENT_PERIOD_MS
                    local_id = f"{sim_id}-f{k}"
                truth[(cam, local_id)] = person
                kps = tuple(tuple(kp) if kp is not None else None for kp in d["keypoints_2d"])
                confs = [kp[2] for kp in kps if kp is not None and kp[2] > 0]
                score = sum(confs) / len(confs) if confs else 0.0
                dets.append(TrackedDetection(local_id, PoseDetection(tuple(d["bbox"]), score, kps)))
            frames.append(
                Observation2DFrame(
                    camera_id=row["camera_id"],
                    stream_epoch=row["stream_epoch"],
                    frame_sequence=row["frame_sequence"],
                    estimated_capture_time_ms=row["estimated_capture_time_ms"],
                    timestamp_source=row["timestamp_source"],
                    timestamp_uncertainty_ms=row["timestamp_uncertainty_ms"],
                    detections=tuple(dets),
                )
            )
    frames.sort(key=lambda f: (f.estimated_capture_time_ms, f.camera_id))
    return frames, truth


def evaluate(out: Path, variant: str, exclusive: bool = True) -> dict[str, Any]:
    rig = CameraRig.from_provider(FileCalibrationProvider(out / "calibration.json"))
    frames, truth = load_frames(out, variant)
    ticks = [doc["t"] for doc in _jsonl(out / "world_tracks.jsonl")]
    associator = CrossViewAssociator(rig, CrossViewConfig(same_camera_exclusive=exclusive))
    buffer = LocalTrackletBuffer()

    m: Counter[str] = Counter()
    reasons: Counter[str] = Counter()
    last_id: dict[str, str] = {}
    last_group: dict[tuple[str, str], frozenset] = {}
    i = 0
    for t in ticks:
        while i < len(frames) and frames[i].estimated_capture_time_ms <= t + ARRIVAL_LAG_MS:
            buffer.add(frames[i])
            i += 1
        views, _ = buffer.views_at(t)
        result = associator.associate(t, views)
        buffer.prune(t)
        m["ticks"] += 1
        m["views"] += len(views)
        # 구성 흔들림: 직전 tick 과 지금 모두 있는 트랙끼리의 묶음이 달라졌는가
        group = {k: frozenset(c.keys) for c in result.clusters for k in c.keys}
        common = group.keys() & last_group.keys()
        if any(group[k] & common != last_group[k] & common for k in common):
            m["composition_changes"] += 1
        last_group = group

        cluster_of = {}
        for idx, c in enumerate(result.clusters):
            m["clusters"] += 1
            persons = {truth[k] for k in c.keys}
            cams = c.camera_ids
            if len(set(cams)) != len(cams):
                m["same_camera_merges"] += 1
            if len(persons) > 1:
                m["impure_clusters"] += 1
            if c.identity_uncertain:
                m["uncertain_clusters"] += 1
                reasons.update(str(r) for r in c.uncertain_reasons)
            for k in c.keys:
                cluster_of[k] = idx

        keys = sorted(cluster_of)
        for a, b in itertools.combinations(keys, 2):
            if a[0] == b[0]:
                continue
            same_gt, same_pred = truth[a] == truth[b], cluster_of[a] == cluster_of[b]
            m["pair_tp"] += same_gt and same_pred
            m["pair_fp"] += (not same_gt) and same_pred
            m["pair_fn"] += same_gt and not same_pred

        by_person: dict[str, list[tuple[str, str]]] = defaultdict(list)
        for k in keys:
            by_person[truth[k]].append(k)
        for person, ks in sorted(by_person.items()):
            if len({k[0] for k in ks}) < 2:
                continue
            m["person_ticks"] += 1
            idxs = {cluster_of[k] for k in ks}
            if len(idxs) == 1:
                c = result.clusters[idxs.pop()]
                if {truth[k] for k in c.keys} == {person}:
                    m["full_association"] += 1
            counts = Counter(result.clusters[cluster_of[k]].association_id for k in ks)
            aid = min(counts, key=lambda a: (-counts[a], a))
            if person in last_id and last_id[person] != aid:
                m["id_switches"] += 1
            last_id[person] = aid

    tp, fp, fn = m["pair_tp"], m["pair_fp"], m["pair_fn"]
    provenance = build_input_provenance(
        {"source": "SIM", "camera_ids": sorted(rig.cameras)}, calibration=rig.calibration_provenance
    )
    keys_out = (
        "ticks",
        "views",
        "clusters",
        "same_camera_merges",
        "impure_clusters",
        "uncertain_clusters",
        "person_ticks",
        "full_association",
        "id_switches",
        "composition_changes",
        "pair_tp",
        "pair_fp",
        "pair_fn",
    )
    return {
        "input_provenance": provenance,
        "variant": variant,
        "same_camera_exclusive": exclusive,
        **{k: int(m[k]) for k in keys_out},
        "pair_precision": tp / (tp + fp) if tp + fp else None,
        "pair_recall": tp / (tp + fn) if tp + fn else None,
        "full_association_ratio": m["full_association"] / m["person_ticks"] if m["person_ticks"] else None,
        "uncertain_reasons": dict(sorted(reasons.items())),
    }


def make_outputs(workdir: Path, scenarios=SCENARIOS, seeds=SEEDS) -> dict[tuple[str, int], Path]:
    """시나리오 × 시드마다 시뮬레이터를 한 번 돌린 출력 디렉터리 (정상·음성 대조가 함께 쓴다)."""
    return {(name, seed): run_sim(scenario_doc(name), seed, workdir) for name in scenarios for seed in seeds}


def evaluate_all(
    outputs: dict[tuple[str, int], Path], exclusive: bool = True, variants=VARIANTS
) -> dict[str, dict[str, Any]]:
    return {
        f"{name}/{seed}/{variant}": evaluate(out, variant, exclusive)
        for (name, seed), out in outputs.items()
        for variant in variants
    }


def run_all(exclusive: bool = True, scenarios=SCENARIOS, seeds=SEEDS, variants=VARIANTS) -> dict[str, dict[str, Any]]:
    with tempfile.TemporaryDirectory() as tmp:
        return evaluate_all(make_outputs(Path(tmp), scenarios, seeds), exclusive, variants)


def _ratio(v: float | None) -> str:
    return "-" if v is None else f"{v:.3f}"


def markdown(results: dict[str, dict[str, Any]]) -> str:
    header = (
        "| 시나리오/시드/변형 | 같은 카메라 오병합 | 사람 섞인 클러스터 | 쌍 정밀도 | 쌍 재현율 | 완전 연결 비율 "
        "| ID 전환 | 구성 흔들림 tick | IDENTITY_UNCERTAIN 클러스터 | (tick, 사람) |"
    )
    lines = [
        header,
        "|---|---|---|---|---|---|---|---|---|---|",
    ]
    for key, r in results.items():
        lines.append(
            f"| {key} | {r['same_camera_merges']} | {r['impure_clusters']} | {_ratio(r['pair_precision'])} "
            f"| {_ratio(r['pair_recall'])} | {_ratio(r['full_association_ratio'])} | {r['id_switches']} "
            f"| {r['composition_changes']}/{r['ticks']} | {r['uncertain_clusters']}/{r['clusters']} | {r['person_ticks']} |"
        )
    lines.append("\nSIM 입력의 상한 성능(is_upper_bound = true)이다. 제품 성능·G2 결론이 아니다.")
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--no-exclusive", action="store_true", help="음성 대조: 같은 카메라 금지 제약을 끈다")
    args = parser.parse_args()
    results = run_all(exclusive=not args.no_exclusive)
    print(json.dumps(results, ensure_ascii=False, indent=2) if args.json else markdown(results))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
