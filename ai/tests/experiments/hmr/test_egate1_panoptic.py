"""E-GATE-1 평가 경로를 실제 다시점 기하(CMU Panoptic 171204_pose1 정답 3D·보정)로 돌린다 (marker dataset, SIMAI_PANOPTIC_DIR).

정답 관절을 카메라 4대에 놓고 '단안 HMR 오차'(체형·깊이 크기 (1+s), s ~ N(0, 5%), 관절 잡음 2 cm)를 더한 후보와, 정답을 왜곡까지 투영한
Tier 0 2D keypoint(잡음 1 px)로 집계·한 카메라 제외·root 오차 계산 경로를 시험한다. DATASET_GT 상한이며 경로 검증이다(제품 성능 아님,
AGENTS 규칙 7, docs/data/cmu_panoptic.md 연구·비상업 목적). 메시·접지 정답이 없어 실루엣·발 오차는 비어야 한다(지어내지 않음).
SIMAI_EGATE1_REPORT 를 주면 지표를 그 파일에 쓴다(설계 문서 '노트북 검증' 표의 출처).
"""

from __future__ import annotations

import importlib
import json
import os
import sys
from pathlib import Path

import numpy as np
import pytest

from simai_ai.geometry.rig import CameraRig
from simai_ai.hmr.prompt import COCO17_TO_MHR70
from simai_ai.providers.dataset_gt import PanopticCalibrationProvider, PanopticWorldTrackProvider
from simai_ai.providers.dataset_gt.panoptic import COCO17_FROM_COCO19, panoptic_to_world

pytestmark = pytest.mark.dataset  # 등록은 tests/providers/dataset_gt/conftest.py

HMR = Path(__file__).resolve().parents[3] / "experiments" / "hmr"
CAMERAS = ["hd_00_00", "hd_00_08", "hd_00_16", "hd_00_24"]
FRAME_STEP = 900
MAX_DOCS = 30


@pytest.fixture(scope="module")
def panoptic_dir():
    value = os.environ.get("SIMAI_PANOPTIC_DIR")
    if not value:
        pytest.skip("SIMAI_PANOPTIC_DIR 가 없어 실제 데이터 시험을 건너뛴다")
    return Path(value)


@pytest.fixture
def mods(monkeypatch):
    monkeypatch.syspath_prepend(str(HMR))
    for name in ("egate1_bundle", "egate1_eval"):
        sys.modules.pop(name, None)
    return importlib.import_module("egate1_bundle"), importlib.import_module("egate1_eval")


def test_panoptic_path_verification(panoptic_dir, mods):
    B, E = mods
    cal = PanopticCalibrationProvider(panoptic_dir, CAMERAS)
    rig = CameraRig.from_provider(cal)
    provider = PanopticWorldTrackProvider(cal)
    proj = {c.camera_id: c for c in cal.projection_cameras}
    rng = np.random.default_rng(87)
    docs, images, skipped = [], [], 0
    for n, frame_index in enumerate(provider.frame_indices[::FRAME_STEP][:MAX_DOCS]):
        scene = json.loads((provider.pose_dir / f"body3DScene_{frame_index:08d}.json").read_text(encoding="utf-8"))
        doc = provider.document(frame_index, scene)
        per_image: dict[str, list] = {cid: [] for cid in CAMERAS}
        bodies = {f"w-{b['id']:04d}": b for b in scene.get("bodies", [])}
        for track in doc["tracks"]:
            joints = track["skeleton"]["joints"]
            # HMR 은 전신을 낸다: 후보는 화면 수와 상관없는 정답 19관절(신뢰도 > 0.1)로 만들고, Tier 0 쪽(골격·2D)은 어댑터 그대로 둔다
            j19 = bodies[track["world_id"]]["joints19"]
            full = [panoptic_to_world(j19[4 * j : 4 * j + 3], cal.floor_y_cm) if j19[4 * j + 3] > 0.1 else None for j in COCO17_FROM_COCO19]
            for obs in track["per_camera"]:
                uv = [proj[obs["camera_id"]].project(p) if p is not None else None for p in joints]
                obs["joint_layout"] = "COCO17"
                obs["keypoints_2d"] = [None if q is None else [q[0] + rng.normal(0, 1.0), q[1] + rng.normal(0, 1.0), 0.9] for q in uv]
            if any(p is None for p in full):
                skipped += 1
                continue
            W = np.asarray(full, dtype=float)
            for obs in track["per_camera"]:
                cam = rig.cameras[obs["camera_id"]]
                kp = np.full((70, 3), np.nan)
                kp[list(COCO17_TO_MHR70)] = W @ cam.R.T + cam.t
                kp = kp * (1.0 + rng.normal(0, 0.05)) + rng.normal(0, 0.02, size=kp.shape)
                per_image[obs["camera_id"]].append(B.BundlePerson(f"{n}:{obs['camera_id']}:{track['world_id']}", tuple(obs["bbox"]), 0.9,
                                                                  None, {}, {"keypoints_cam": kp}))
        docs.append(doc)
        for cid, people in per_image.items():
            images.append(B.BundleImage(n, cid, doc["t"], None, (1920, 1080), None, False, "SUCCESS", 100.0, people))
    res = E.evaluate(images, None, docs, rig)
    m = res["metrics"]
    if os.environ.get("SIMAI_EGATE1_REPORT"):
        Path(os.environ["SIMAI_EGATE1_REPORT"]).write_text(json.dumps({"skipped_partial_gt": skipped, **m}, ensure_ascii=False, indent=1),
                                                         encoding="utf-8")
    a = m["all"]
    assert m["counts"]["loo_samples"] >= 50
    assert a["loo_common_px.tier0_triangulated"]["p50"] < 3.0
    assert a["loo_common_px.hmr_multiview"]["p50"] < a["loo_common_px.hmr_single_raw"]["p50"]
    assert a["root_offset_m.multiview"]["p50"] < a["root_offset_m.single_raw"]["p50"]
    assert a["silhouette_iou.hmr_multiview"]["n"] == 0 and a["foot_anchor_abs_m.hmr_multiview"]["n"] == 0
    assert m["counts"].get("rejected.CAMERA_SLOT_TAKEN", 0) == 0
