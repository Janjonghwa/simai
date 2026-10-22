"""MapAnything known-K 입력 준비(-102 prepare): 왜곡 펴기와 펴진 K, 사람 마스크, 덮어쓰기 금지, 크기 불일치 거부."""

from __future__ import annotations

import json

import numpy as np
import pytest

cv2 = pytest.importorskip("cv2")  # extras calibration
from simai_ai.calibration.world_frame.prepare import prepare_inputs, undistort  # noqa: E402

from .conftest import intrinsics_record  # noqa: E402

PROV = {"frames": {"source": "SIM", "dataset_id": "sim/world-frame"}, "is_upper_bound": True}
SIZE = (896, 504)


def _pattern(w, h):
    """부드러운 무늬(보간 오차가 작다)."""
    y, x = np.mgrid[0:h, 0:w].astype(np.float32)
    img = np.stack([127 + 100 * np.sin(x / 23.0), 127 + 100 * np.cos(y / 19.0), 127 + 60 * np.sin((x + y) / 31.0)], -1)
    return img.astype(np.uint8)


def test_undistort_maps_same_ray_to_new_k():
    w, h = SIZE
    rec = intrinsics_record(size=SIZE, k=(-0.27, 0.046))
    cam = rec["shared_lens"]["common"]["camera"]
    K, dist = np.array(cam["K"], float), np.array(cam["distortion"]["coeffs"], float)
    img = _pattern(w, h)
    und, newK = undistort(img, K, dist)
    assert und.shape == img.shape and newK[0, 0] > 0 and newK[0, 1] == 0
    assert newK[0, 2] == pytest.approx((w - 1) / 2, abs=1) and newK[1, 2] == pytest.approx((h - 1) / 2, abs=1)
    # 같은 광선: 원본(왜곡 모델)과 펴진 영상(새 K, 왜곡 0)에서 같은 색
    rng = np.random.default_rng(0)
    rays = np.column_stack([rng.uniform(-0.5, 0.5, 40), rng.uniform(-0.3, 0.3, 40), np.ones(40)])
    pd, _ = cv2.projectPoints(rays, np.zeros(3), np.zeros(3), K, dist)
    pu, _ = cv2.projectPoints(rays, np.zeros(3), np.zeros(3), newK, np.zeros(5))
    a = np.array([img[int(round(v)), int(round(u))] for u, v in pd.reshape(-1, 2)], float)
    b = np.array([und[int(round(v)), int(round(u))] for u, v in pu.reshape(-1, 2)], float)
    assert np.median(np.abs(a - b)) < 12


class FakeSegmenter:
    def describe(self):
        return {"model": "fake-seg", "sha256": "sha256:" + "1" * 64}

    def person_mask(self, image):
        m = np.zeros(image.shape[:2], bool)
        m[200:260, 400:430] = True
        return m, 1


def _images(tmp_path, size=SIZE):
    out = {}
    for cid in ("cam_A", "cam_B"):
        p = tmp_path / f"{cid}_01.jpg"
        cv2.imwrite(str(p), _pattern(*size))
        out[cid] = p
    return out


def test_prepare_writes_known_k_inputs_and_dilated_masks(tmp_path):
    rec = intrinsics_record(size=SIZE, cams=("cam_A", "cam_B"))
    out = tmp_path / "prepared"
    doc = prepare_inputs(_images(tmp_path), rec, out, segmenter=FakeSegmenter(), input_provenance=PROV, dilate_px=5)
    saved = json.loads((out / "prepared.json").read_text(encoding="utf-8"))
    assert saved == json.loads(json.dumps(doc))
    assert saved["format"] == "simai.mapanything_inputs" and saved["person_masks"] is True
    for v in saved["views"]:
        assert (out / v["image"]).is_file() and v["image_size"] == list(SIZE)
        K = np.array(v["K_undistorted"])
        assert K.shape == (3, 3) and K[2].tolist() == [0, 0, 1]
        assert v["source_camera"]["distortion"]["coeffs"][:2] == [-0.05, 0.01]
        m = cv2.imread(str(out / v["mask"]), cv2.IMREAD_GRAYSCALE) > 127
        assert m[200:260, 400:430].all() and m[197, 415] and not m[150, 415]  # 팽창 5 px
        assert v["person_count"] == 1 and v["person_mask_ratio"] > 0


def test_prepare_without_masks_records_it_and_refuses_overwrite_and_size_mismatch(tmp_path):
    rec = intrinsics_record(size=SIZE, cams=("cam_A", "cam_B"))
    out = tmp_path / "p"
    doc = prepare_inputs(_images(tmp_path), rec, out, segmenter=None, input_provenance=PROV)
    assert doc["person_masks"] is False and all(v["mask"] is None for v in doc["views"])
    with pytest.raises(FileExistsError):
        prepare_inputs(_images(tmp_path), rec, out, segmenter=None, input_provenance=PROV)
    small = tmp_path / "small"
    small.mkdir()
    with pytest.raises(ValueError, match="영상 크기"):
        prepare_inputs(_images(small, size=(640, 360)), rec, tmp_path / "p2", segmenter=None, input_provenance=PROV)
    assert not (tmp_path / "p2").exists()  # 실패하면 폴더를 만들지 않는다
    with pytest.raises(ValueError, match="없는 카메라"):
        prepare_inputs({"cam_Z": tmp_path / "cam_A_01.jpg"}, rec, tmp_path / "p3", segmenter=None, input_provenance=PROV)



def test_prepare_leaves_no_folder_when_a_later_camera_fails(tmp_path, monkeypatch):
    """두 번째 카메라에서 펴기가 실패하면 출력 폴더를 만들지 않는다(반쯤 된 입력 폴더를 남기지 않는다)."""
    from simai_ai.calibration.world_frame import prepare as mod

    real, calls = mod.undistort, []

    def flaky(img, K, dist):
        calls.append(1)
        if len(calls) == 2:
            raise ValueError("펴진 영상이 원본 밖을 읽는다")
        return real(img, K, dist)

    monkeypatch.setattr(mod, "undistort", flaky)
    rec = intrinsics_record(size=SIZE, cams=("cam_A", "cam_B"))
    with pytest.raises(ValueError, match="원본 밖"):
        prepare_inputs(_images(tmp_path), rec, tmp_path / "out", segmenter=None, input_provenance=PROV)
    assert not (tmp_path / "out").exists()
