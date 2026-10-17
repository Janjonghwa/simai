"""시뮬레이터 고정 시나리오로 관절 유효성 게이트의 판정 분포와 VALID 관절 오차를 잰다 (docs/design/tier0-joint-gate.md 의 표).

ai/ 에서 실행한다. 시뮬레이터 실행·입력 읽기는 sim_accuracy.py 를 그대로 쓴다.

    uv run python tests/geometry/sim_joint_gate.py           # 표 (Markdown)
    uv run python tests/geometry/sim_joint_gate.py --json    # 시험(test_sim_joint_gate.py)이 읽는 JSON

- 입력은 시뮬레이터 `observations/` 와 `calibration.json` 만 쓴다. `truth/` 는 오차 계산에만 쓴다.
- 카메라 간 연결은 시뮬레이터 정답 연결을 쓴다. 2대 관측 경쟁 후보 검사의 `others` 는 같은 시각 다른 사람들의 보간 keypoint 다.
- 결과는 SIM 입력의 상한 성능이다(`input_provenance.is_upper_bound = true`). 제품 성능으로 쓰지 않는다.

변형은 sim_accuracy.py 의 clean·noisy 에 더해, crossing/noisy 를 cam1·cam2 두 대만으로 푼 `crossing/noisy/2cam`
(모든 관절이 2대 관측이라 경쟁 후보 검사가 도는 경우)이 있다.
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
import tempfile
from collections import Counter
from pathlib import Path
from typing import Any

import numpy as np
from sim_accuracy import (
    DISCONTINUITY_MARGIN_MS,
    SCENARIOS,
    VARIANTS,
    _jsonl,
    _pct,
    load_association,
    load_discontinuities,
    load_samples,
    run_sim,
    scenario_doc,
)

from simai_ai.geometry.interpolation import InterpolationConfig, interpolate_views
from simai_ai.geometry.joint_gate import (
    JointGate,
    JointGateConfig,
    epipolar_distance_px,
)
from simai_ai.geometry.rig import CameraRig
from simai_ai.geometry.triangulation import JointIssue, JointValidity
from simai_ai.providers.files import FileCalibrationProvider
from simai_ai.providers.provenance import build_input_provenance

# 시뮬레이터 몸(sim/body.py)의 귀·눈 Z 평균에서 머리 꼭대기까지: 키 1.70 m 기준 1.70 − (1.645·2 + 1.62·2)/4 = 0.0675 m,
# 키에 비례한다. 실제 사람의 초기값(JointGateConfig.head_top_offset_m = 0.12)과 다르므로 SIM head_z 오차는
# 이 값으로 잰다(삼각측량·게이트 부분만 보려는 것). 오프셋 자체는 G2 실측으로 정한다.
SIM_HEAD_OFFSET_PER_M = 0.0675 / 1.70
LARGE_ERROR_M = 0.05
# 정답 기준 D-G2-06 경쟁 후보의 에피폴라 띠(왜곡 제거 픽셀). 게이트 설정과 독립으로 고정한다.
TRUTH_COMPETITOR_BAND_PX = 10.0
EXTRA = {"crossing/noisy/2cam": ("crossing", "noisy", ("cam1", "cam2"))}


def evaluate(
    out: Path, discontinuities: dict[str, list[int]], cameras: tuple[str, ...] | None = None, config=None
) -> dict[str, Any]:
    rig = CameraRig.from_provider(FileCalibrationProvider(out / "calibration.json"))
    samples = load_samples(out)
    assoc = load_association(out)
    interp = InterpolationConfig()
    config = config or JointGateConfig()
    gates: dict[str, JointGate] = {}

    validity = Counter()
    geometry_validity = Counter()
    reasons = Counter()
    errors = {"VALID": [], "UNCERTAIN": []}
    valid_smooth: list[float] = []
    large = {"gate_valid": 0, "geometry_valid": 0}
    head_errors: list[float] = []
    head_validity = Counter()
    competitor_ticks = 0
    truth_competitor = {"two_view_joints": 0, "valid": 0}
    if any(cam.distortion_model != "NONE" for cam in rig.cameras.values()):
        raise ValueError("정답 투영 D-G2-06 계산은 왜곡 없는 SIM 카메라만 지원한다")
    fabricated = 0
    for truth in _jsonl(out / "truth" / "world_truth.jsonl"):
        t = truth["t"]
        aligned = {}
        for person in truth["people"]:
            tracks = {c: lid for c, lid in assoc.get(person["world_id"], {}).items() if cameras is None or c in cameras}
            aligned[person["world_id"]], _ = interpolate_views(
                {cid: samples[(cid, lid)] for cid, lid in tracks.items()}, t, interp
            )
        for person in truth["people"]:
            wid = person["world_id"]
            views = aligned[wid]
            if not views:
                validity[str(JointValidity.UNAVAILABLE)] += len(person["joints"])
                continue
            others: dict[str, list] = {}
            for other_id, other_views in aligned.items():
                if other_id != wid:
                    for cid, v in other_views.items():
                        others.setdefault(cid, []).append(v.keypoints)
            gate = gates.setdefault(wid, JointGate(config))
            sk = gate.update(views, rig, others=others)
            competitor_ticks += sk.identity_uncertain_candidate
            near_jump = any(abs(t - d) <= DISCONTINUITY_MARGIN_MS for d in discontinuities[wid])
            for j, gj in enumerate(sk.joints):
                if len(gj.camera_ids) == 2 and _truth_competitor(j, gj.camera_ids, person, truth, aligned, rig, config):
                    truth_competitor["two_view_joints"] += 1
                    truth_competitor["valid"] += gj.validity == JointValidity.VALID
            for gj, true_xyz in zip(sk.joints, person["joints"]):
                validity[str(gj.validity)] += 1
                # 기하만: 삼각측량 최소 기준(26 의 판정)만 본 VALID. 게이트가 더한 사유는 무시한다
                geometry_ok = gj.xyz is not None and not any(isinstance(r, JointIssue) for r in gj.reasons)
                geometry_validity["VALID" if geometry_ok else "other"] += 1
                if gj.validity == JointValidity.UNCERTAIN:
                    reasons.update(str(r) for r in gj.reasons)
                if gj.xyz is None:
                    continue
                if len(gj.camera_ids) < 2:
                    fabricated += 1
                err = float(np.linalg.norm(np.subtract(gj.xyz, true_xyz)))
                if gj.validity == JointValidity.VALID:
                    errors["VALID"].append(err)
                    if not near_jump:
                        valid_smooth.append(err)
                    large["gate_valid"] += err > LARGE_ERROR_M
                else:
                    errors["UNCERTAIN"].append(err)
                if geometry_ok:
                    large["geometry_valid"] += err > LARGE_ERROR_M
            hz = sk.head_z(SIM_HEAD_OFFSET_PER_M * person["height_m"])
            head_validity[str(hz.validity)] += 1
            if hz.value is not None and hz.validity == JointValidity.VALID:
                head_errors.append(abs(hz.value - person["head_top_xyz"][2]))

    total = sum(validity.values())

    def stats(values: list[float]) -> dict[str, float | None]:
        return {
            "median": statistics.median(values) if values else None,
            "p95": _pct(values, 95),
            "max": max(values) if values else None,
        }

    return {
        "input_provenance": build_input_provenance(
            {"source": "SIM", "camera_ids": sorted(cameras or rig.cameras)}, calibration=rig.calibration_provenance
        ),
        "joints_total": total,
        "validity": dict(sorted(validity.items())),
        "valid_ratio": validity["VALID"] / total if total else 0.0,
        "geometry_valid_ratio": geometry_validity["VALID"] / total if total else 0.0,
        "uncertain_reasons": dict(sorted(reasons.items())),
        "fabricated_joints": fabricated,
        "valid_error_m": {**stats(errors["VALID"]), "max_smooth": max(valid_smooth) if valid_smooth else None},
        "uncertain_error_m": stats(errors["UNCERTAIN"]),
        "large_error_joints": large,
        "head_z_error_m": stats(head_errors),
        "head_z_validity": dict(sorted(head_validity.items())),
        "identity_uncertain_ticks": competitor_ticks,
        "truth_competitor": truth_competitor,
    }


def _truth_competitor(j, camera_ids, person, truth, aligned, rig, config) -> bool:
    """정답 기준 D-G2-06 경쟁 후보: 게이트의 관측 픽셀 대신 정답 3D 를 투영해 독립적으로 센다.

    두 카메라 중 한쪽에서 이 사람의 정답 관절 투영의 에피폴라선 `TRUTH_COMPETITOR_BAND_PX` 안에, 다른 쪽 카메라에
    보인(그 카메라 관측 신뢰도가 기준 이상) 다른 사람의 같은 관절 정답 투영이 있으면 경쟁 후보다.
    """
    min_conf = config.triangulation.min_keypoint_confidence
    a, b = (rig.cameras[c] for c in camera_ids)
    own = np.asarray(person["joints"][j])
    for cam_own, cam_other in ((a, b), (b, a)):
        uv_own = _ideal_projection(cam_own, own)
        for other in truth["people"]:
            if other["world_id"] == person["world_id"]:
                continue
            seen = aligned.get(other["world_id"], {}).get(cam_other.camera_id)
            if seen is None or seen.keypoints[j] is None or not seen.keypoints[j][2] >= min_conf:
                continue
            uv_other = _ideal_projection(cam_other, np.asarray(other["joints"][j]))
            if uv_own is None or uv_other is None:
                continue
            if epipolar_distance_px(cam_own, uv_own, cam_other, uv_other) <= TRUTH_COMPETITOR_BAND_PX:
                return True
    return False


def _ideal_projection(cam, X):
    xc = cam.to_camera(X)[0]
    return None if xc[2] <= 0 else cam.ideal_pixels(xc[:2] / xc[2])[0]


def run_all(scenarios=SCENARIOS, variants=VARIANTS, extra: bool = True) -> dict[str, dict[str, Any]]:
    jobs = [(f"{n}/{v}", n, v, None) for n in scenarios for v in variants]
    if extra:
        jobs += [(key, n, v, cams) for key, (n, v, cams) in EXTRA.items() if n in scenarios]
    results: dict[str, dict[str, Any]] = {}
    for key, name, variant, cams in jobs:
        doc = scenario_doc(name, variant)
        with tempfile.TemporaryDirectory() as tmp:
            out = run_sim(doc, Path(tmp))
            results[key] = evaluate(out, load_discontinuities(out, doc), cams)
    return results


def _cm(v: float | None) -> str:
    return "-" if v is None else f"{v * 100:.2f}"


def markdown(results: dict[str, dict[str, Any]]) -> str:
    lines = [
        (
            "| 시나리오/변형 | VALID 비율 | 기하만 VALID 비율 | VALID 오차 중앙값 cm | VALID p95 cm | VALID 최대 cm "
            "| 불연속 밖 VALID 최대 cm | UNCERTAIN 오차 p95 cm | 5 cm 넘는 VALID (게이트/기하만) | head_z 오차 p95 cm "
            "| UNCERTAIN 사유 | IDENTITY_UNCERTAIN 후보 tick | 정답 경쟁 후보 2대 관절 (VALID) |"
        ),
        "|---|---|---|---|---|---|---|---|---|---|---|---|---|",
    ]
    for key, r in results.items():
        v, u, h = r["valid_error_m"], r["uncertain_error_m"], r["head_z_error_m"]
        reasons = ", ".join(f"{k} {n}" for k, n in r["uncertain_reasons"].items()) or "-"
        lines.append(
            f"| {key} | {r['valid_ratio']:.3f} | {r['geometry_valid_ratio']:.3f} | {_cm(v['median'])} | {_cm(v['p95'])} "
            f"| {_cm(v['max'])} | {_cm(v['max_smooth'])} | {_cm(u['p95'])} "
            f"| {r['large_error_joints']['gate_valid']}/{r['large_error_joints']['geometry_valid']} "
            f"| {_cm(h['p95'])} | {reasons} | {r['identity_uncertain_ticks']} "
            f"| {r['truth_competitor']['two_view_joints']} ({r['truth_competitor']['valid']}) |"
        )
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="SIM 고정 시나리오 관절 유효성 게이트 (상한 성능)")
    parser.add_argument("--json", action="store_true", help="JSON 으로 출력")
    parser.add_argument("--scenario", action="append", choices=SCENARIOS, help="일부 시나리오만 (여러 번 줄 수 있음)")
    args = parser.parse_args(argv)
    results = run_all(tuple(args.scenario) if args.scenario else SCENARIOS)
    print(json.dumps(results, ensure_ascii=False, indent=2) if args.json else markdown(results))
    return 0


if __name__ == "__main__":
    sys.exit(main())
