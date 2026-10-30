"""-357 합성 스툴(SIM, 제품 성능 아님): 색 분할·붙은 면·1.05 축척·독립 표본·보류·CLI."""

from __future__ import annotations

import json

import numpy as np
import pytest

cv2 = pytest.importorskip("cv2")

from simai_ai.calibration.world_frame import cli
from simai_ai.calibration.world_frame.measured_objects import Detection, load_catalog
from simai_ai.calibration.world_frame.stool_tops import HSVStoolDetector, measure_stools
from simai_ai.geometry.rig import RigCamera

from .conftest import look

HW = (720, 960)
K = np.array([[850., 0, 480], [0, 850., 360], [0, 0, 1.]])


def stool_object():
    return next(o for o in load_catalog()[0] if o.id == "lobby_stool_top")


def scene(scale=1.05, *, centers=((-0.65, 0), (0.65, 0), (0, 1.1)), side=True, sizes=None):
    obj = stool_object()
    cameras, images, corners, specs = {}, {}, {}, []
    for cid, origin in {"A": (0, -3, 3), "B": (3, 0, 3), "C": (-3, 1, 3)}.items():
        origin = np.array(origin, float)
        R = look(origin, (0, .3, .425))
        spec = {"camera_id": cid, "image_size": list(HW[::-1]), "K": K.tolist(), "R": R.tolist(),
                "t": (-R @ (origin / scale)).tolist(), "distortion": {"model": "NONE", "coeffs": []}}
        specs.append(spec)
        cameras[cid] = RigCamera.from_calibration(spec)
        image = np.full((*HW, 3), 70, np.uint8)
        corners[cid] = []
        for i, (x, y) in enumerate(centers):
            half = (sizes[i] if sizes else .415) / 2
            points = np.array([[x-half, y-half, .425], [x+half, y-half, .425],
                               [x+half, y+half, .425], [x-half, y+half, .425]])
            def project(p):
                xc = (p - origin) @ R.T
                uv = xc @ K.T
                return np.rint(uv[:, :2] / uv[:, 2:3]).astype(np.int32)
            q = project(points)
            if side:
                # 옆면은 더 어둡고 윗면과 같은 색상이다.
                bottom = project(points * [1, 1, 0])
                for j in range(4):
                    poly = np.array([q[j], q[(j+1)%4], bottom[(j+1)%4], bottom[j]])
                    cv2.fillConvexPoly(image, poly, (150, 115, 40))
            color = cv2.cvtColor(np.uint8([[[90 if i % 2 == 0 else 106, 135, 245]]]), cv2.COLOR_HSV2BGR)[0, 0]
            cv2.fillConvexPoly(image, q, tuple(int(v) for v in color))
            corners[cid].append(q)
        images[cid] = image
    detector = HSVStoolDetector(obj.detector)
    dets = {cid: detector.detect(im) for cid, im in images.items()}
    return obj, cameras, images, dets, corners, specs


def test_catalog_has_measured_square_stool_and_source():
    obj = stool_object()
    assert obj.edges == {"top_width": .415}
    assert obj.raw["dimensions_m"] == {"width": .415, "depth": .415, "height": .425}
    assert obj.raw["measured"]["date"] == "2026-10-28"
    assert obj.raw["measured"]["source"] == "2026-10-28 사용자 실측, -100"


def test_detects_tops_and_recovers_scale_with_three_independent_stools():
    obj, cams, images, dets, _, _ = scene()
    assert all(len(ds) == 3 for ds in dets.values())
    scale, count, rec = measure_stools(obj, cams, dets)
    assert scale == pytest.approx(1.05, rel=.015)
    assert count == rec["multiview_samples"] == 3  # 네 변 × 카메라 수로 표본 수를 부풀리지 않는다.
    assert all(g["source"] == "TRIANGULATED" for g in rec["groups"])
    assert rec["aggregation"]["median_standard_error"] < .02


def test_separates_two_touching_tops():
    obj, cams, _, dets, _, _ = scene(centers=((-0.2075, 0), (.2075, 0)), side=False)
    assert all(len(ds) >= 2 for ds in dets.values())  # 전체 색역의 합쳐진 가설은 기하 검사에서 버린다.
    scale, count, rec = measure_stools(obj, cams, dets)
    assert rec["multiview_samples"] == 2
    assert scale is None and count == 0  # 최소 세 개 조건 유지.
    assert all(g["scale"] == pytest.approx(1.05, rel=.02) for g in rec["groups"])


def test_one_camera_is_auxiliary_only():
    obj, cams, _, dets, _, _ = scene()
    scale, count, rec = measure_stools(obj, cams, {"A": dets["A"]})
    assert scale is None and count == rec["multiview_samples"] == 0
    assert rec["stool_candidates"] == 3
    assert all(g["source"] == "HEIGHT_PLANE_AUXILIARY" for g in rec["groups"])
    assert "TOO_FEW_SAMPLES" in rec["reason"]


def test_one_wrong_size_is_excluded_by_existing_aggregation():
    obj, cams, _, dets, _, _ = scene(centers=((-0.65, 0), (.65, 0), (-.65, 1.1), (.65, 1.1)),
                                    sizes=[.415, .415, .415, .32])
    scale, count, rec = measure_stools(obj, cams, dets)
    assert scale == pytest.approx(1.05, rel=.015) and count == 3
    assert rec["aggregation"]["excluded"] == 1


def test_mapanything_adapter_uses_floor_offset_and_prepared_pixel_transform():
    from simai_ai.calibration.world_frame.frame import Plane
    from simai_ai.calibration.world_frame.inputs import MapAnythingRun, View
    from simai_ai.calibration.world_frame.measured_objects import measure_object

    obj, cams, _, dets, _, _ = scene()
    Q = look((1, -2, 3), (0, 0, 0))
    shift = np.array([2., -1., 3.])
    views = []
    for cid, cam in cams.items():
        T = np.eye(4)
        T[:3, :3] = Q @ cam.R.T
        T[:3, 3] = Q @ cam.center + shift
        # 크기 1/2 모델 K 와 준비 영상 픽셀 중심 규약.
        A = np.array([[.5, 0, -.25], [0, .5, -.25], [0, 0, 1.]])
        depth = np.ones((HW[0] // 2, HW[1] // 2))
        views.append(View(cid, cid, depth, A @ K, T, np.ones(depth.shape, bool), None))
    n = Q @ [0., 0., 1.]
    floor = Plane(n, float(n @ shift), 100, 0)
    scale, count, _ = measure_object(obj, MapAnythingRun(tuple(views), {}), floor, dets,
                                     {cid: HW[::-1] for cid in cams})
    assert scale == pytest.approx(1.05, rel=.015) and count == 3


def test_person_and_truncated_top_are_rejected():
    obj, cams, _, dets, _, _ = scene()
    person = np.ones(HW, bool)
    _, _, rec = measure_stools(obj, cams, {"A": dets["A"]}, person_masks={"A": person})
    assert rec["accepted"] == 0 and rec["rejected_by_reason"]["ON_PERSON"] == 3
    mask = np.zeros(HW, np.uint8)
    cv2.rectangle(mask, (0, 200), (100, 300), 1, -1)
    _, _, rec = measure_stools(obj, cams, {"A": [Detection(mask.astype(bool), 1)]})
    assert rec["rejected_by_reason"] == {"TRUNCATED": 1}


def test_detection_order_does_not_change_independent_counts():
    obj, cams, _, dets, _, _ = scene()
    # 카메라별 검출 순서가 달라도 보정 대응으로 같은 스툴에 연결한다.
    dets["B"] = dets["B"][1:] + dets["B"][:1]
    scale, count, _ = measure_stools(obj, cams, dets)
    assert count == 3 and scale == pytest.approx(1.05, rel=.015)


def test_cli_records_provenance_hashes_and_refuses_overwrite(tmp_path):
    _, _, images, _, _, specs = scene()
    calibration = {"schema_version": "2.1.0", "calibration_version": "sim-stools", "provider_source": "SIM",
                   "provider_version": "test", "state": "PROVISIONAL", "scale_basis": "MODEL_ESTIMATE",
                   "coordinate_frame": {"frame": "WORLD", "unit": "m", "handedness": "right", "up_axis": "+Z"},
                   "extrinsic_convention": "OPENCV_WORLD_TO_CAMERA", "cameras": specs}
    path = tmp_path / "calibration.json"
    path.write_text(json.dumps(calibration), encoding="utf-8")
    out = tmp_path / "stools.json"
    argv = ["measure-stools", "--calibration", str(path), "--capture-id", "sim-stools", "--out", str(out)]
    for cid, im in images.items():
        image = tmp_path / f"{cid}.png"
        cv2.imwrite(str(image), im)
        argv.extend(["--image", f"{cid}={image}"])
    assert cli.main(argv, now_ms=1234) == cli.EXIT_OK
    doc = json.loads(out.read_text(encoding="utf-8"))
    assert doc["sample_count"] == 3 and doc["input_provenance"]["is_upper_bound"]
    assert doc["input_provenance"]["calibration"]["calibration_version"] == "sim-stools"
    assert all(v["sha256"].startswith("sha256:") for v in doc["images"].values())
    before = out.read_bytes()
    assert cli.main(argv, now_ms=2345) == cli.EXIT_CONFIG
    assert out.read_bytes() == before
