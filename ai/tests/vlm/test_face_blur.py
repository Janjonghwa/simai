"""VLM 입력 얼굴 처리 관문 (S15P31A101-270, 명세 5.4 "VLM 입력 프레임").

합성 이미지: 배경·안전모·얼굴·몸통에 서로 다른 무작위 무늬를 칠하고, 얼굴 위치를 아는 가짜 keypoint 를 준다.
실제 사람 사진은 쓰지 않는다.
"""

from __future__ import annotations

import numpy as np
import pytest

from simai_ai.vlm import face_blur as fb
from simai_ai.vlm.face_blur import Person, PoseDetectorProvider, VlmFrame, apply_face_policy, prepare_vlm_frames

H, W = 320, 240
BBOX = (65.0, 60.0, 155.0, 300.0)  # 사람 상자 폭 90 → 머리 폭 30
HELMET = (slice(60, 84), slice(95, 126))  # 안전모(눈높이 100 에서 s×0.30 = 9 위인 91 보다 위)
HELMET_TOP = (slice(60, 80), slice(95, 126))
FACE = (slice(92, 126), slice(96, 125))  # 실제 얼굴(이마 아래~턱): 처리돼야 하는 곳
TORSO = (slice(140, 300), slice(70, 150))
BACKGROUND = (slice(0, 50), slice(0, 240))

# COCO17 앞 5점(코, 왼눈, 오른눈, 왼귀, 오른귀) + 나머지 12점은 없음
FACE_KPS = ((110.0, 110.0, 0.9), (104.0, 100.0, 0.9), (116.0, 100.0, 0.9), (95.0, 102.0, 0.8), (125.0, 102.0, 0.8))


def _image(seed: int = 0) -> np.ndarray:
    return np.random.default_rng(seed).integers(0, 256, size=(H, W, 3), dtype=np.uint8)


def _person(kps=FACE_KPS) -> Person:
    return Person(bbox=BBOX, keypoints=tuple(kps) + (None,) * (17 - len(kps)))


class FakeProvider:
    def __init__(self, people=None, error: Exception | None = None) -> None:
        self._people = [] if people is None else people
        self._error = error
        self.calls = 0

    def people(self, image):
        self.calls += 1
        if self._error is not None:
            raise self._error
        return self._people


def _same(a: np.ndarray, b: np.ndarray, region) -> bool:
    return np.array_equal(a[region], b[region])


def _scrambled(orig: np.ndarray, out: np.ndarray, region) -> None:
    """얼굴이 알아볼 수 없게 바뀌었다: 원본과 크게 다르고, 색이 모자이크 칸 수 이하로 줄었다."""
    o, p = orig[region].astype(int), out[region].astype(int)
    assert np.abs(o - p).mean() > 40
    assert len(np.unique(p.reshape(-1, 3), axis=0)) <= fb.MOSAIC_CELLS**2 * 4  # 영역 경계가 칸을 자르면 칸이 조금 늘 수 있다


def test_blurred_scrambles_face_and_keeps_helmet_torso_background():
    img = _image()
    orig = img.copy()
    out, record, regions = apply_face_policy(img, [_person()], "BLURRED")
    _scrambled(orig, out, FACE)
    for keep in (HELMET, TORSO, BACKGROUND):
        assert _same(orig, out, keep)
    assert np.array_equal(img, orig)  # 원본 불변
    assert record == fb.FaceRecord(people=1, regions=1, bbox_fallback=0)
    assert regions[0].source == "keypoints"


def test_excluded_paints_face_region():
    img = _image()
    out, _, (region,) = apply_face_policy(img, [_person()], "EXCLUDED")
    assert (out[region.y1 : region.y2, region.x1 : region.x2] == fb.EXCLUDED_FILL).all()
    assert (out[FACE] == fb.EXCLUDED_FILL).all()
    assert _same(img, out, HELMET) and _same(img, out, TORSO)


@pytest.mark.parametrize(
    "visible",
    [
        pytest.param({4}, id="right-ear-only"),
        pytest.param({3}, id="left-ear-only"),
        pytest.param({0}, id="nose-only"),
        pytest.param({0, 2, 4}, id="right-half"),
        pytest.param({1}, id="one-eye"),
    ],
)
def test_partial_keypoints_still_cover_the_whole_face(visible):
    kps = tuple(kp if j in visible else None for j, kp in enumerate(FACE_KPS))
    img = _image()
    out, record, _ = apply_face_policy(img, [_person(kps)], "EXCLUDED")
    assert (out[FACE] == fb.EXCLUDED_FILL).all()
    # 눈이 안 보이면 눈높이를 위로 올려 잡아 안전모 아래쪽이 함께 처리될 수 있다. 안전모 위쪽은 남는다
    assert _same(img, out, HELMET_TOP)
    assert record.bbox_fallback == 0


def test_low_confidence_points_are_ignored_and_fall_back_to_bbox():
    kps = tuple((x, y, fb.KEYPOINT_CONF_MIN / 2) for x, y, _ in FACE_KPS)
    img = _image()
    out, record, (region,) = apply_face_policy(img, [_person(kps)], "EXCLUDED")
    assert region.source == "bbox" and record.bbox_fallback == 1
    assert (out[FACE] == fb.EXCLUDED_FILL).all()


def test_person_without_face_points_gets_top_of_box_band():
    """뒤돌아선 사람(얼굴 점 없음): 상자 5%~30% 띠를 처리하고 맨 위는 남긴다(fail-closed)."""
    img = _image()
    out, record, (region,) = apply_face_policy(img, [_person(())], "BLURRED")
    bh = BBOX[3] - BBOX[1]
    assert (region.x1, region.y1, region.x2, region.y2) == (65, 72, 155, 132)
    assert region.y1 == int(BBOX[1] + fb.NO_FACE_TOP * bh) and region.y2 == int(np.ceil(BBOX[1] + fb.NO_FACE_BOTTOM * bh))
    _scrambled(img, out, FACE)
    assert _same(img, out, (slice(60, 72), slice(65, 155)))  # 상자 맨 위 5%
    assert _same(img, out, TORSO) and _same(img, out, BACKGROUND)
    assert record.bbox_fallback == 1


def test_no_people_means_unchanged_copy():
    img = _image()
    out, record, regions = apply_face_policy(img, [], "BLURRED")
    assert np.array_equal(out, img) and out is not img
    assert record.people == 0 and regions == ()


def test_region_clipped_to_image_and_outside_person_ignored():
    edge = Person(bbox=(-50.0, -40.0, 30.0, 200.0), keypoints=((0.0, -5.0, 0.9),) + (None,) * 16)
    outside = Person(bbox=(500.0, 500.0, 600.0, 700.0), keypoints=())
    out, record, regions = apply_face_policy(_image(), [edge, outside], "BLURRED")
    assert record.people == 2 and record.regions == 1
    r = regions[0]
    assert 0 <= r.x1 < r.x2 <= W and 0 <= r.y1 < r.y2 <= H


def test_invalid_policy_and_image_rejected():
    with pytest.raises(ValueError):
        apply_face_policy(_image(), [], "OBSERVED")  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        apply_face_policy(np.zeros((10, 10), np.uint8), [], "BLURRED")


# --- 관문 ---


def _decode_ok(data: bytes) -> np.ndarray:
    return _image(int(data.decode()))


def _decode_fail(data: bytes) -> np.ndarray:
    raise ValueError("깨진 JPEG")


def test_gate_returns_only_processed_frames():
    provider = FakeProvider([_person()])
    result = prepare_vlm_frames([("s3://f/1.jpg", b"1"), ("s3://f/2.jpg", b"2")], "BLURRED", provider, decode=_decode_ok)
    assert [f.source for f in result.frames] == ["s3://f/1.jpg", "s3://f/2.jpg"] and result.dropped == ()
    for frame, seed in zip(result.frames, (1, 2), strict=True):
        assert frame.face_policy == "BLURRED" and frame.record.regions == 1
        _scrambled(_image(seed), frame.pixels, FACE)
        assert _same(_image(seed), frame.pixels, HELMET)


def test_gate_drops_frames_it_could_not_process():
    """디코드 실패·검출 실패 프레임은 VLM 에 넘기지 않는다(흐리지 않은 프레임으로 대신하지 않는다)."""
    bad_decode = prepare_vlm_frames([("a", b"x")], "BLURRED", FakeProvider([_person()]), decode=_decode_fail)
    assert bad_decode.frames == () and [(d.source, d.reason) for d in bad_decode.dropped] == [("a", "decode")]

    failing = FakeProvider(error=RuntimeError("CUDA out of memory"))
    bad_detect = prepare_vlm_frames([("b", b"1")], "BLURRED", failing, decode=_decode_ok)
    assert bad_detect.frames == () and bad_detect.dropped[0].reason == "detect"
    assert "CUDA out of memory" in bad_detect.dropped[0].detail

    no_decoder = prepare_vlm_frames([("c", b"1")], "BLURRED", FakeProvider())
    assert no_decoder.dropped[0].reason == "decode"


def test_gate_mixed_success_keeps_good_frames():
    def decode(data: bytes) -> np.ndarray:
        if data == b"bad":
            raise ValueError("깨짐")
        return _image(1)

    result = prepare_vlm_frames([("ok", b"1"), ("bad", b"bad")], "EXCLUDED", FakeProvider([_person()]), decode=decode)
    assert [f.source for f in result.frames] == ["ok"]
    assert [d.source for d in result.dropped] == ["bad"]
    assert fb.summarize(result)["dropped"] == [{"source": "bad", "reason": "decode"}]


def test_gate_accepts_clip_frame_arrays_without_changing_them():
    """incident_summary 클립에서 뽑은 프레임 배열도 같은 관문을 거친다(S15P31A101-271)."""
    clip = [_image(s) for s in range(3)]
    before = [c.copy() for c in clip]
    result = prepare_vlm_frames([(f"clip-1#{i}", c) for i, c in enumerate(clip)], "BLURRED", FakeProvider([_person()]))
    assert len(result.frames) == 3
    for c, b, f in zip(clip, before, result.frames, strict=True):
        assert np.array_equal(c, b)
        _scrambled(b, f.pixels, FACE)


def test_gate_rejects_unknown_policy():
    with pytest.raises(ValueError):
        prepare_vlm_frames([], "OBSERVED", FakeProvider())  # type: ignore[arg-type]


def test_vlm_frame_cannot_be_made_outside_the_gate_and_is_read_only():
    """VLM 입력 타입은 관문 밖에서 만들 수 없다(우회 0건)."""
    with pytest.raises(TypeError):
        VlmFrame(object(), "s", "BLURRED", fb.FaceRecord(0, 0, 0), _image())
    (frame,) = prepare_vlm_frames([("s", _image())], "BLURRED", FakeProvider()).frames
    with pytest.raises(ValueError):
        frame.pixels[0, 0, 0] = 1


def test_pose_detector_provider_converts_detector_output():
    class Detector:
        def detect(self, images):
            assert len(images) == 1
            return [[{"bbox": [1, 2, 3, 4], "score": 0.9, "keypoints_2d": [[5, 6, 0.7], None]}]]

    (person,) = PoseDetectorProvider(Detector()).people(_image())
    assert person.bbox == (1.0, 2.0, 3.0, 4.0)
    assert person.keypoints == ((5.0, 6.0, 0.7), None)


def test_gate_stops_decode_and_detect_when_budget_or_cancelled():
    provider = FakeProvider([_person()])
    rem = [1.0, 1.0, 0.0, 0.0]
    result = prepare_vlm_frames(
        [("f1", b"1"), ("f2", b"2")],
        "BLURRED",
        provider,
        decode=_decode_ok,
        remaining_s=lambda: rem.pop(0),
    )
    assert [f.source for f in result.frames] == ["f1"]
    assert [(d.source, d.reason) for d in result.dropped] == [("f2", "budget")]
    assert provider.calls == 1

    provider_cancelled = FakeProvider([_person()])
    cancelled_res = prepare_vlm_frames(
        [("f1", b"1")],
        "BLURRED",
        provider_cancelled,
        decode=_decode_ok,
        cancelled=lambda: True,
    )
    assert cancelled_res.frames == ()
    assert [(d.source, d.reason) for d in cancelled_res.dropped] == [("f1", "cancelled")]
    assert provider_cancelled.calls == 0
