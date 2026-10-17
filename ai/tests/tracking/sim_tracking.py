"""시뮬레이터 고정 시나리오로 카메라별 Local Tracker 의 ID 전환을 잰다 (docs/design/tier0-local-tracker.md 의 표).

ai/ 에서 실행한다. extras detection(ultralytics, lap)이 필요하다.

    uv run --extra detection python tests/tracking/sim_tracking.py          # 표 (Markdown)
    uv run --extra detection python tests/tracking/sim_tracking.py --json   # JSON

- 입력은 시뮬레이터 `observations/` 의 2D keypoint 만 쓴다. 시뮬레이터가 붙인 카메라 내 트랙 ID 는 추적기에 넣지 않고
  정답 연결로만 쓴다. 신뢰도는 보인 관절(conf > 0) 신뢰도 평균이다(SIM 변환 가정). bbox 는 keypoint 로 다시 만들고
  두 변형을 잰다:
  - amodal: 투영된 관절 전부(가려져 conf 0 인 관절 포함)를 감싼다. 검출기가 가려진 부분까지 사람 박스를 내는 경우.
  - visible: 보인 관절만 감싼다. 가림이 생기면 박스가 갑자기 줄어드는, 더 나쁜 경우.
- 카메라별 프레임을 촬영 시각 순서로 합친 뒤 카메라마다 한 장씩 묶어(묶음 추론 흉내) MultiCameraLocalTracker 에 넣는다.
- 결과는 SIM 입력의 상한 성능이다(`input_provenance.is_upper_bound = true`). 제품 성능으로 쓰지 않는다.

지표 (카메라마다 세고 합친다):
- gt_detections: 시뮬레이터가 낸 검출 수. tracked: 트랙 ID 가 붙어 observation_2d 에 들어간 수.
- id_switches: 같은 정답 사람에게 붙은 카메라 내 트랙 ID 가 직전에 붙었던 ID 와 다른 횟수 (MOT IDSW 정의).
- merged_ids: 서로 다른 정답 사람 둘 이상에게 붙은 적이 있는 트랙 ID 수 (ID 뒤섞임).
- fragments: 정답 사람마다 (받은 트랙 ID 수 − 1)의 합.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
from collections import defaultdict
from collections.abc import Iterator, Sequence
from pathlib import Path
from typing import Any

from simai_ai.io import SimObservationFrameSource
from simai_ai.providers.base import Frame
from simai_ai.providers.provenance import provenance_from_providers
from simai_ai.tracking.local_config import TrackerConfig, TrackerKind, tracker_config
from simai_ai.tracking.local_tracker import (
    CameraDetections,
    MultiCameraLocalTracker,
    Observation2DFrame,
    PoseDetection,
)

AI_DIR = Path(__file__).resolve().parents[2]
SCENARIOS = ("basic", "podium", "crossing")
TRACKERS = tuple(TrackerKind)
BOX_MODES = ("amodal", "visible")


def run_sim(name: str, workdir: Path) -> Path:
    out = workdir / name
    result = subprocess.run(
        [sys.executable, "-m", "sim.run", "--scenario", name, "--out", str(out)],
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


def bbox_from_keypoints(keypoints: Sequence[Any], mode: str = "amodal") -> tuple[float, float, float, float] | None:
    """관절을 감싸는 박스. 시뮬레이터와 같은 여백(2 px + 높이 5%). 감쌀 관절이 없으면 None.

    mode amodal 은 투영된 관절 전부(conf 0 포함), visible 은 보인 관절(conf > 0)만 감싼다.
    """
    if mode not in BOX_MODES:
        raise ValueError(f"모르는 박스 변형: {mode}")
    seen = [kp for kp in keypoints if kp is not None and (mode == "amodal" or kp[2] > 0)]
    if not seen:
        return None
    us, vs = [kp[0] for kp in seen], [kp[1] for kp in seen]
    pad = 2.0 + 0.05 * (max(vs) - min(vs))
    return (max(0.0, min(us) - pad), max(0.0, min(vs) - pad), max(us) + pad, max(vs) + pad)


def score_from_keypoints(keypoints: Sequence[Any]) -> float:
    confs = [kp[2] for kp in keypoints if kp is not None and kp[2] > 0]
    return sum(confs) / len(confs) if confs else 0.0


def to_camera_detections(frame: Frame, mode: str = "amodal", features: dict[str, Sequence[float]] | None = None):
    """시뮬레이터 Frame → (CameraDetections, 검출 순서의 정답 라벨). 정답 라벨은 시뮬레이터의 카메라 내 트랙 ID 다.

    features 를 주면 정답 라벨별 ReID 특징을 붙인다(ReID 연결 경로 시험용, 정답을 쓰므로 성능 지표가 아니다).
    """
    dets, labels = [], []
    for d in frame.detections or ():
        box = bbox_from_keypoints(d.keypoints_2d, mode)
        if box is None:
            continue
        feat = tuple(features[d.local_track_id]) if features is not None else None
        dets.append(PoseDetection(box, score_from_keypoints(d.keypoints_2d), d.keypoints_2d, feat))
        labels.append(d.local_track_id)
    cd = CameraDetections(
        camera_id=frame.camera_id,
        stream_epoch=frame.frame_ref["stream_epoch"],
        frame_sequence=frame.frame_ref["frame_sequence"],
        estimated_capture_time_ms=frame.estimated_capture_time,
        timestamp_source=frame.timestamp_source,
        timestamp_uncertainty_ms=frame.timestamp_uncertainty_ms,
        detections=tuple(dets),
    )
    return cd, tuple(labels)


def batches(frames: Sequence[Any]) -> Iterator[list[Any]]:
    """촬영 시각 순서의 프레임을 카메라마다 한 장씩 묶는다 (묶음 추론 흉내). 항목은 (CameraDetections, labels)."""
    batch: list[Any] = []
    for item in frames:
        if any(other[0].camera_id == item[0].camera_id for other in batch):
            yield batch
            batch = []
        batch.append(item)
    if batch:
        yield batch


def load_frames(
    out: Path, mode: str = "amodal", features: dict[str, Sequence[float]] | None = None
) -> list[tuple[CameraDetections, tuple]]:
    return [to_camera_detections(f, mode, features) for f in SimObservationFrameSource(out).frames()]


def run_tracker(
    frames: Sequence[tuple[CameraDetections, tuple]], config: TrackerConfig, camera_ids: Sequence[str] | None = None
) -> tuple[MultiCameraLocalTracker, list[tuple[Observation2DFrame, CameraDetections, tuple]]]:
    """frames 를 묶음으로 넣고 (추적기, [(observation_2d, 넣은 프레임, 그 프레임의 정답 라벨)]) 를 돌려준다."""
    tracker = MultiCameraLocalTracker(config, camera_ids=camera_ids)
    results = []
    for batch in batches(frames):
        obs = tracker.update_batch([cd for cd, _ in batch])
        assert len(obs) == len(batch), "시뮬레이터 프레임은 순서가 맞아 버려지지 않아야 한다"
        results.extend((o, cd, labels) for o, (cd, labels) in zip(obs, batch))
    return tracker, results


def id_metrics(results: Sequence[tuple[Observation2DFrame, CameraDetections, tuple]]):
    """카메라별 ID 전환 지표. 검출 원본 객체가 그대로 나오므로 객체 동일성으로 정답 라벨을 찾는다."""
    per_cam: dict[str, dict[str, Any]] = defaultdict(
        lambda: {"gt_detections": 0, "tracked": 0, "id_switches": 0, "merged_ids": 0, "fragments": 0}
    )
    last_id: dict[tuple[str, str], str] = {}
    ids_of_gt: dict[tuple[str, str], set[str]] = defaultdict(set)
    gts_of_id: dict[tuple[str, str], set[str]] = defaultdict(set)
    for obs, cd, labels in results:
        cam = obs.camera_id
        per_cam[cam]["gt_detections"] += len(cd.detections)
        label_of = {id(d): lab for d, lab in zip(cd.detections, labels)}
        for t in obs.detections:
            gt = label_of[id(t.detection)]
            per_cam[cam]["tracked"] += 1
            key = (cam, gt)
            if key in last_id and last_id[key] != t.local_track_id:
                per_cam[cam]["id_switches"] += 1
            last_id[key] = t.local_track_id
            ids_of_gt[key].add(t.local_track_id)
            gts_of_id[(cam, t.local_track_id)].add(gt)
    for (cam, _gt), ids in ids_of_gt.items():
        per_cam[cam]["fragments"] += len(ids) - 1
    for (cam, _tid), gts in gts_of_id.items():
        per_cam[cam]["merged_ids"] += int(len(gts) > 1)
    keys = ("gt_detections", "tracked", "id_switches", "merged_ids", "fragments")
    totals = {k: sum(c[k] for c in per_cam.values()) for k in keys}
    return dict(sorted(per_cam.items())), totals


def evaluate(out: Path, kind: TrackerKind, mode: str) -> dict[str, Any]:
    frames = load_frames(out, mode)
    tracker, results = run_tracker(frames, tracker_config(kind))
    per_cam, totals = id_metrics(results)
    provenance = provenance_from_providers(SimObservationFrameSource(out))
    return {
        "input_provenance": provenance,
        "tracker": kind.value,
        "box_mode": mode,
        "cameras": per_cam,
        "totals": totals,
        "resets": sum(s.resets for s in tracker.states().values()),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()
    results: dict[str, Any] = {}
    with tempfile.TemporaryDirectory() as tmp:
        for name in SCENARIOS:
            out = run_sim(name, Path(tmp))
            for mode in BOX_MODES:
                for kind in TRACKERS:
                    results[f"{name}/{mode}/{kind.value}"] = evaluate(out, kind, mode)
    if args.json:
        print(json.dumps(results, ensure_ascii=False, indent=2))
        return 0
    print("| 시나리오 | 박스 | 추적기 | 검출 | 트랙 붙음 | ID 전환 | 뒤섞인 ID | 조각 |")
    print("|---|---|---|---|---|---|---|---|")
    for key, r in results.items():
        name, mode, kind = key.split("/")
        t = r["totals"]
        print(
            f"| {name} | {mode} | {kind} | {t['gt_detections']} | {t['tracked']} | {t['id_switches']} "
            f"| {t['merged_ids']} | {t['fragments']} |"
        )
    print("\nSIM 입력의 상한 성능(is_upper_bound = true). 제품 성능이 아니다.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
