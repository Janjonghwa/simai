"""외형 분할 FP16(half)·불필요한 CUDA 동기화 제거·retina 마스크 해상도 검증(cv2/torch 없이 실행)."""

import hashlib
import sys
from types import SimpleNamespace

import numpy as np
import pytest

from simai_ai.calibration.intrinsics.estimate import WeightsRef
from simai_ai.hmr.appearance import AppearanceConfig
from simai_ai.hmr.appearance_stream import appearance_from_env
from simai_ai.hmr.prompt import HmrImageMap, SegInstance
from simai_ai.hmr.segmentation import AvatarSegmentation, YoloPersonSegmenter, segmentation_from_env


class _FakeTensor:
    def __init__(self, array):
        self._array = np.asarray(array)

    def cpu(self):
        return self

    def numpy(self):
        return self._array


def _make_weights(tmp_path):
    path = tmp_path / "yolo26x-seg.pt"
    path.write_bytes(b"synthetic yolo seg weights")
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    return WeightsRef(str(path), f"sha256:{digest}")


def _simple_inputs():
    imap = HmrImageMap.from_intrinsics(
        "cam",
        [[45.0, 0.0, 32.0], [0.0, 45.0, 24.0], [0.0, 0.0, 1.0]],
        (64, 48),
        "NONE",
        (),
    )
    image = np.full((48, 64, 3), [10, 20, 30], np.uint8)
    jobs = [
        (
            {"world_id": "w-1", "identity_revision": 2},
            {"camera_id": "cam", "bbox": [12.0, 8.0, 50.0, 40.0]},
        )
    ]
    rig = SimpleNamespace(cameras={"cam": imap.camera}, calibration_version="cal-1")
    return imap, image, jobs, rig


def test_yolo_segmenter_uses_fp16_on_cuda_and_fp32_on_cpu_with_retina_masks(tmp_path, monkeypatch):
    weights = _make_weights(tmp_path)
    calls = []

    def predict(images, **kwargs):
        calls.append(kwargs)
        out = []
        for image in images:
            h, w = image.shape[:2]
            # FP16 출력 텐서를 흉내 내어 > 0.5 이진화와 retina 원본 해상도 유지를 확인한다.
            masks = np.zeros((2, h, w), dtype=np.float16)
            masks[0, 10:30, 12:40] = np.float16(0.75)
            masks[0, 0:5, 0:5] = np.float16(0.25)
            masks[1, :, :] = np.float16(0.9)
            out.append(
                SimpleNamespace(
                    masks=SimpleNamespace(data=_FakeTensor(masks)),
                    boxes=SimpleNamespace(
                        xyxy=_FakeTensor(np.array([[12.0, 10.0, 40.0, 30.0], [0.0, 0.0, 5.0, 5.0]], dtype=np.float16)),
                        conf=_FakeTensor(np.array([0.92, 0.85], dtype=np.float16)),
                        cls=_FakeTensor(np.array([0.0, 1.0], dtype=np.float16)),
                    ),
                )
            )
        return out

    monkeypatch.setitem(sys.modules, "ultralytics", SimpleNamespace(YOLO=lambda path, task: SimpleNamespace(predict=predict)))
    images = [np.zeros((48, 64, 3), np.uint8), np.zeros((36, 48, 3), np.uint8)]

    cuda_seg = YoloPersonSegmenter(weights, device="cuda:0")
    assert cuda_seg.half is True
    batch = cuda_seg.predict(images)
    assert calls[-1]["half"] is True
    assert calls[-1]["retina_masks"] is True
    assert calls[-1]["imgsz"] == 1280
    assert calls[-1]["classes"] == [0]
    assert batch[0][0].mask.dtype == np.bool_
    assert batch[0][0].mask.shape == (48, 64)
    assert batch[1][0].mask.shape == (36, 48)
    assert batch[0][0].mask[15, 20] and not batch[0][0].mask[1, 1]

    # torch.device 객체 형태(model.device)도 CUDA로 인식해 FP16을 켠다.
    torch_like_cuda = SimpleNamespace(type="cuda", index=0)
    assert YoloPersonSegmenter(weights, device=torch_like_cuda).half is True
    assert YoloPersonSegmenter(weights, device="cuda:0", half=False).half is False

    # CPU에서는 half=True를 넘겨도 FP32로 고정한다.
    cpu_seg = YoloPersonSegmenter(weights, device="cpu", half=True)
    assert cpu_seg.half is False
    cpu_seg.predict(images)
    assert calls[-1]["half"] is False


def test_yolo_segmenter_rejects_mismatched_retina_mask_shape_under_fp16(tmp_path, monkeypatch, caplog):
    weights = _make_weights(tmp_path)

    def predict(images, **kwargs):
        assert kwargs["half"] is True and kwargs["retina_masks"] is True
        # 원본 (48, 64)가 아니라 letterbox 저해상도 (24, 32) 마스크를 반환하는 오류 상황
        bad_masks = np.ones((1, 24, 32), dtype=np.float16)
        return [
            SimpleNamespace(
                masks=SimpleNamespace(data=_FakeTensor(bad_masks)),
                boxes=SimpleNamespace(
                    xyxy=_FakeTensor(np.array([[12.0, 8.0, 50.0, 40.0]], dtype=np.float16)),
                    conf=_FakeTensor(np.array([0.9], dtype=np.float16)),
                    cls=_FakeTensor(np.array([0.0], dtype=np.float16)),
                ),
            )
            for _ in images
        ]

    monkeypatch.setitem(sys.modules, "ultralytics", SimpleNamespace(YOLO=lambda path, task: SimpleNamespace(predict=predict)))
    imap, image, jobs, rig = _simple_inputs()

    seg = YoloPersonSegmenter(weights, device="cuda")
    with pytest.raises(ValueError, match="retina 마스크 해상도"):
        seg.predict([image])

    caplog.set_level("WARNING")
    pipeline = AvatarSegmentation(seg, device="cuda")
    assert pipeline.prepare(jobs, {"cam": image}, rig) == {}
    assert pipeline.failed and "bbox만 사용" in caplog.text


def test_segmentation_skips_cuda_synchronize_by_default_and_enables_via_env(monkeypatch):
    sync_calls = []
    fake_torch = SimpleNamespace(
        cuda=SimpleNamespace(
            is_available=lambda: True,
            synchronize=lambda device=None: sync_calls.append(device),
        )
    )
    monkeypatch.setitem(sys.modules, "torch", fake_torch)

    imap, image, jobs, rig = _simple_inputs()
    mask = np.zeros((48, 64), dtype=bool)
    mask[10:38, 14:48] = True
    fake_seg = SimpleNamespace(half=True, predict=lambda imgs: [[SegInstance((12.0, 8.0, 50.0, 40.0), 0.9, mask)] for _ in imgs])

    default_seg = segmentation_from_env({}, cuda_available=True, device="cuda:0")
    assert default_seg.segmenter.half is True
    assert default_seg.sync_timing is False
    assert default_seg.synchronize is None
    default_seg.segmenter = fake_seg
    assert ("w-1", "cam") in default_seg.prepare(jobs, {"cam": image}, rig)
    assert sync_calls == []

    fp32_seg = segmentation_from_env({"SIMAI_AVATAR_SEG_HALF": "0"}, cuda_available=True, device="cuda:0")
    assert fp32_seg.segmenter.half is False
    assert fp32_seg.sync_timing is False

    timed_seg = segmentation_from_env({"SIMAI_AVATAR_SEG_SYNC_TIMING": "1"}, cuda_available=True, device="cuda:1")
    assert timed_seg.sync_timing is True and timed_seg.synchronize is not None
    timed_seg.segmenter = fake_seg
    assert ("w-1", "cam") in timed_seg.prepare(jobs, {"cam": image}, rig)
    assert sync_calls == ["cuda:1", "cuda:1"]

    with pytest.raises(ValueError, match="SIMAI_AVATAR_SEG_HALF"):
        segmentation_from_env({"SIMAI_AVATAR_SEG_HALF": "fp16"}, cuda_available=True)
    with pytest.raises(ValueError, match="SIMAI_AVATAR_SEG_SYNC_TIMING"):
        segmentation_from_env({"SIMAI_AVATAR_SEG_SYNC_TIMING": "yes"}, cuda_available=True)


def test_appearance_sync_timing_defaults_off_and_toggles_via_env():
    assert AppearanceConfig().sync_timing is False
    assert appearance_from_env({}, cuda_available=True).config.sync_timing is False
    assert appearance_from_env({"SIMAI_AVATAR_TEXTURE_SYNC_TIMING": "1"}, cuda_available=True).config.sync_timing is True
    with pytest.raises(ValueError, match="SIMAI_AVATAR_TEXTURE_SYNC_TIMING"):
        appearance_from_env({"SIMAI_AVATAR_TEXTURE_SYNC_TIMING": "yes"}, cuda_available=True)


def test_absorb_publish_false_restores_texels_and_publish_state_without_carry_publish():
    from simai_ai.hmr.appearance import AppearanceAccumulator, TexelSnapshot

    def bare_acc(fusion="best"):
        acc = AppearanceAccumulator.__new__(AppearanceAccumulator)
        acc.config = AppearanceConfig(resolution=4, fusion=fusion)
        acc.valid = np.ones(16, dtype=bool)
        acc.colors = np.zeros((16, 3), dtype=np.float32)
        acc.weights = np.zeros(16, dtype=np.float32)
        acc.peaks = np.zeros(16, dtype=np.float32)
        acc.rejects = np.zeros(16, dtype=np.float32)
        acc.inherited = np.zeros(16, dtype=bool)
        acc.carry_publish = False
        acc.dirty = False
        acc.ready = False
        acc.last_publish = -1
        acc.published_coverage = 0.0
        acc.last_confirmed = None
        return acc

    snap = TexelSnapshot(
        colors=np.full((16, 3), [10.0, 20.0, 30.0], dtype=np.float32),
        weights=np.ones(16, dtype=np.float32),
        inherited=np.zeros(16, dtype=bool),
        last_confirmed="confirmed-result",
        peaks=np.ones(16, dtype=np.float32),
        published_coverage=0.8,
        last_publish=50_000,
        dirty=False,
    )
    for fusion in ("best", "mean"):
        acc = bare_acc(fusion)
        assert acc.absorb(snap, inherit=False, publish=False) is True
        assert acc.carry_publish is False
        assert acc.dirty is False
        assert acc.published_coverage == pytest.approx(0.8)
        assert acc.last_publish == 50_000
        assert acc.last_confirmed == "confirmed-result"
        assert acc.due(50_200) is False
        np.testing.assert_array_equal(acc.colors, snap.colors)

