"""HMR 프롬프트 구성 (S15P31A101-87, simai_ai.hmr.prompt). GPU·cv2 를 쓰지 않는다.

반례: 손목은 프롬프트에 넣지 않음, 낮은 신뢰도·없는 관절은 빼고 채우지 않음, 마스크 하나를 두 사람에게 주지 않음,
같은 카메라 같은 World ID 관측 둘은 둘 다 건너뜀, 왜곡을 편 그림 좌표로 옮김(가장자리 bbox 가 펴진 그림에서 바깥으로 넓어짐),
옮길 수 없는 bbox 는 지어내지 않고 건너뜀.
"""

from __future__ import annotations

import numpy as np
import pytest

from simai_ai.hmr.prompt import (
    COCO17_TO_MHR70,
    DUMMY_PROMPT_LABEL,
    HmrImageMap,
    PersonObservation,
    SegInstance,
    build_prompts,
    keypoint_prompt_array,
    observations_from_world_tracks,
)
from tests.hmr.multiview_scene import Person, make_rig, track_doc

K = [[1000.0, 0.0, 960.0], [0.0, 1000.0, 540.0], [0.0, 0.0, 1.0]]
SIZE = (1920, 1080)


def identity_map(camera_id="cam_A"):
    return HmrImageMap.from_intrinsics(camera_id, K, SIZE)


def kps(conf=0.9, **override):
    out = [[500.0 + 3 * j, 300.0 + 20 * j, conf] for j in range(17)]
    for j, v in override.items():
        out[int(j.removeprefix("j"))] = v
    return tuple(out)


def mask_for(box, size=SIZE):
    m = np.zeros((size[1], size[0]), dtype=bool)
    x0, y0, x1, y1 = (int(round(v)) for v in box)
    m[y0:y1, x0:x1] = True
    return m


def test_mhr70_mapping_keeps_coco_order_and_wrist_indices():
    assert COCO17_TO_MHR70[:9] == tuple(range(9))
    assert (COCO17_TO_MHR70[9], COCO17_TO_MHR70[10]) == (62, 41)  # 왼·오른 손목
    assert COCO17_TO_MHR70[11:] == (9, 10, 11, 12, 13, 14)


def test_only_torso_and_hip_keypoints_are_prompted_wrist_never():
    obs = PersonObservation("cam_A", (480.0, 280.0, 560.0, 660.0), kps())
    prompts, skipped = build_prompts([obs], [], identity_map())
    assert skipped == []
    idx = [i for i, _u, _v in prompts[0].keypoints]
    assert idx == [5, 6, 9, 10]  # MHR70 어깨·엉덩이
    assert 41 not in idx and 62 not in idx
    arr = keypoint_prompt_array(prompts[0])
    assert arr.shape == (4, 3) and arr[:, 2].tolist() == [5, 6, 9, 10]


def test_low_confidence_missing_and_outside_box_keypoints_are_dropped_not_filled():
    obs = PersonObservation("cam_A", (480.0, 280.0, 560.0, 560.0), kps(j5=[515.0, 400.0, 0.2], j6=None, j12=[536.0, 9000.0, 0.9]))
    prompts, _ = build_prompts([obs], [], identity_map())
    assert [i for i, _u, _v in prompts[0].keypoints] == [9]  # 11(왼 엉덩이)만 남음: 5 저신뢰, 6 없음, 12 bbox 밖
    arr = keypoint_prompt_array(prompts[0])
    assert arr[1:, 2].tolist() == [DUMMY_PROMPT_LABEL] * 3 and arr[1:, :2].tolist() == [[0, 0]] * 3


def test_one_mask_is_given_to_one_person_only():
    """반례: 겹친 두 사람과 인스턴스 하나. 더 많이 겹치는 사람만 마스크를 받고 다른 사람은 bbox 만."""
    a = PersonObservation("cam_A", (100.0, 100.0, 300.0, 600.0), world_id="w-1")
    b = PersonObservation("cam_A", (150.0, 100.0, 350.0, 600.0), world_id="w-2")
    inst = SegInstance((105.0, 100.0, 305.0, 600.0), 0.9, mask_for((105, 100, 305, 600)))
    prompts, _ = build_prompts([a, b], [inst], identity_map())
    assert [p.mask_index for p in prompts] == [0, None]
    assert prompts[1].mask is None and prompts[1].mask_iou is None


def test_mask_below_iou_gate_is_not_used():
    a = PersonObservation("cam_A", (100.0, 100.0, 300.0, 600.0))
    inst = SegInstance((250.0, 100.0, 450.0, 600.0), 0.9, mask_for((250, 100, 450, 600)))
    prompts, _ = build_prompts([a], [inst], identity_map())
    assert prompts[0].mask is None


def test_duplicate_world_id_in_one_camera_skips_both():
    a = PersonObservation("cam_A", (100.0, 100.0, 300.0, 600.0), world_id="w-1")
    b = PersonObservation("cam_A", (400.0, 100.0, 600.0, 600.0), world_id="w-1")
    c = PersonObservation("cam_A", (700.0, 100.0, 900.0, 600.0), world_id="w-2")
    prompts, skipped = build_prompts([a, b, c], [], identity_map())
    assert [p.observation.world_id for p in prompts] == ["w-2"]
    assert [s.reason for s in skipped] == ["DUPLICATE_WORLD_ID_IN_CAMERA"] * 2


def test_mixed_cameras_or_wrong_map_are_refused():
    a = PersonObservation("cam_A", (100.0, 100.0, 300.0, 600.0))
    b = PersonObservation("cam_B", (100.0, 100.0, 300.0, 600.0))
    with pytest.raises(ValueError):
        build_prompts([a, b], [], identity_map())
    with pytest.raises(ValueError):
        build_prompts([b], [], identity_map("cam_A"))


def test_undistorted_map_moves_edge_box_outward_and_matches_rig_normalization():
    """배럴 왜곡(k1 −0.27): 원본 가장자리 bbox 는 펴진 그림(K_used = K)에서 바깥으로 넓어진다. 점 사상은 K·정규화와 같다."""
    D = [-0.27, 0.046, 0.0, 0.0, 0.0]
    m = HmrImageMap.from_intrinsics("cam_A", K, SIZE, "OPENCV_PINHOLE", D, K_used=K)
    edge = (1600.0, 300.0, 1800.0, 900.0)
    mapped = m.bbox(edge)
    assert mapped[2] > edge[2] and mapped[0] > edge[0]
    pts = np.array([[1700.0, 600.0]])
    xn = m.camera.normalize(pts)
    np.testing.assert_allclose(m.points(pts), xn @ np.asarray(K)[:2, :2].T + np.asarray(K)[:2, 2], atol=1e-9)
    center = m.points(np.array([[960.0, 540.0]]))
    np.testing.assert_allclose(center, [[960.0, 540.0]], atol=1e-6)


def test_unmappable_box_is_skipped_not_invented():
    """반례: 왜곡 식이 접히는 먼 바깥(k1 강한 음수)으로 나간 bbox 는 옮길 수 없다. 건너뛰고 이유를 남긴다."""
    m = HmrImageMap.from_intrinsics("cam_A", K, SIZE, "OPENCV_PINHOLE", [-0.9, 0.0, 0.0, 0.0, 0.0], K_used=K)
    obs = PersonObservation("cam_A", (-6000.0, -6000.0, 8000.0, 8000.0))
    prompts, skipped = build_prompts([obs], [], m)
    assert prompts == [] and [s.reason for s in skipped] == ["BBOX_NOT_MAPPABLE"]


def test_observations_from_world_tracks_carry_world_id_and_revision():
    rig = make_rig()
    doc = track_doc([Person("w-1", (4.0, 3.0), 0.0, identity_revision=3)], rig)
    obs = observations_from_world_tracks(doc, "cam_B")
    assert len(obs) == 1
    assert (obs[0].world_id, obs[0].identity_revision, obs[0].local_track_id) == ("w-1", 3, "cam_B-w-1")
    assert len(obs[0].keypoints) == 17
    prompts, _ = build_prompts(obs, [], HmrImageMap(rig.cameras["cam_B"], np.asarray(K)))
    assert [i for i, _u, _v in prompts[0].keypoints] == [5, 6, 9, 10]
