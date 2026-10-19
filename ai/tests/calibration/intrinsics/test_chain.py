"""백엔드 순서(AnyCalib 기본 → GeoCalib 예비 → 고전 기준)와 학습 결과의 직선성 확인.

학습 백엔드는 설치하지 않고 가짜 추정기(주입)로 시험한다. 가짜 AnyCalib 은 정답 카메라(SIM) 값이나 일부러 틀린 값을 낸다.
"""

from __future__ import annotations

import numpy as np
import pytest

from simai_ai.calibration.intrinsics import (
    METHOD_ANYCALIB,
    METHOD_GEOCALIB,
    METHOD_NONE,
    METHOD_PLUMB_LINE_VP,
    STATUS_FAILED,
    STATUS_OK,
    AnyCalibEstimator,
    GeoCalibEstimator,
    IntrinsicsBackendChain,
    LearnedBackendUnavailable,
    ModelRef,
    PlumbLineVPEstimator,
    WeightsRef,
    build_default_chain,
    combine_shared_lens,
)
from simai_ai.calibration.intrinsics import learned as learned_module
from simai_ai.calibration.intrinsics.chain import (
    ATTEMPT_ERROR,
    ATTEMPT_OK,
    ATTEMPT_UNAVAILABLE,
    CHECK_FAILED,
    CHECK_PASSED,
    CHECK_UNVERIFIED,
)
from simai_ai.calibration.intrinsics.learned import learned_estimate

WEIGHTS = WeightsRef("/workspace/simai/models/anycalib/anycalib_dist.pt", "sha256:" + "b" * 64)


class FakeLearned:
    """가짜 학습 백엔드. raises 가 있으면 그 예외, 아니면 주어진 (f, cx, cy, k1, k2) 를 learned_estimate 로 낸다."""

    def __init__(self, method, params=None, *, raises=None, success=True):
        self.method = method
        self.params = params
        self.raises = raises
        self.success = success
        self.calls = 0
        self.model_ref = ModelRef(method.title(), "fake", "fake", WEIGHTS, "Apache-2.0", "Apache-2.0")

    def estimate(self, image, *, camera_id):
        self.calls += 1
        if self.raises is not None:
            raise self.raises
        h, w = np.asarray(image).shape[:2]
        f, cx, cy, k1, k2 = self.params
        return learned_estimate(
            camera_id=camera_id,
            image_size=(w, h),
            fx=f,
            fy=f,
            cx=cx,
            cy=cy,
            radial=[k1, k2],
            focal_std_px=None,
            method=self.method,
            model=self.model_ref,
            principal_point_assumed=self.method != METHOD_ANYCALIB,
            success=self.success,
        )


def truth_params(camera):
    K = camera["K"]
    k1, k2 = camera["distortion"]["coeffs"][:2]
    return (K[0][0], K[0][2], K[1][2], k1, k2)


def chain_of(*backends, verify=True):
    kwargs = {} if verify else {"verify": None}
    return IntrinsicsBackendChain(list(backends), **kwargs)


def test_anycalib_is_default_and_passes_straightness(wide_case):
    cam = wide_case.cameras["cam1"]
    any_ = FakeLearned(METHOD_ANYCALIB, truth_params(cam))
    geo = FakeLearned(METHOD_GEOCALIB, truth_params(cam))
    chain = chain_of(("anycalib", any_), ("geocalib", geo), ("plumbline", PlumbLineVPEstimator()))
    res = chain.estimate_with_trace(wide_case.image("cam1"), camera_id="cam1")
    assert res.estimate.method == METHOD_ANYCALIB and res.estimate.status == STATUS_OK
    assert res.backend == "anycalib" and not res.fallback
    assert res.verification is not None and res.verification.status == CHECK_PASSED
    assert res.verification.learned_rms_px < 1.0
    assert geo.calls == 0  # 기본 백엔드가 돌면 예비를 부르지 않는다
    rec = res.to_record()
    assert rec["selected"] == "anycalib" and rec["attempts"] == [
        {"backend": "anycalib", "method": METHOD_ANYCALIB, "outcome": ATTEMPT_OK, "status": STATUS_OK}
    ]


def test_anycalib_unavailable_falls_back_to_geocalib(wide_case):
    cam = wide_case.cameras["cam1"]
    any_ = FakeLearned(METHOD_ANYCALIB, raises=LearnedBackendUnavailable("anycalib 를 import 할 수 없다"))
    geo = FakeLearned(METHOD_GEOCALIB, truth_params(cam))
    chain = chain_of(("anycalib", any_), ("geocalib", geo), ("plumbline", PlumbLineVPEstimator()))
    res = chain.estimate_with_trace(wide_case.image("cam1"), camera_id="cam1")
    assert res.estimate.method == METHOD_GEOCALIB and res.backend == "geocalib" and res.fallback
    assert [a.outcome for a in res.attempts] == [ATTEMPT_UNAVAILABLE, ATTEMPT_OK]
    assert "import" in res.attempts[0].reason
    assert res.to_record()["fallback"] is True


def test_all_learned_unavailable_falls_to_classical_and_is_labeled(wide_case):
    """학습 백엔드가 모두 없으면 고전 기준 값이 나오되 AnyCalib 결과처럼 보이지 않는다."""
    any_ = FakeLearned(METHOD_ANYCALIB, raises=LearnedBackendUnavailable("없음"))
    geo = FakeLearned(METHOD_GEOCALIB, raises=LearnedBackendUnavailable("없음"))
    plumb = PlumbLineVPEstimator()
    chain = chain_of(("anycalib", any_), ("geocalib", geo), ("plumbline", plumb))
    res = chain.estimate_with_trace(wide_case.image("cam1"), camera_id="cam1")
    assert res.estimate.method == METHOD_PLUMB_LINE_VP and res.estimate.model is None
    assert res.backend == "plumbline" and res.fallback
    assert [(a.backend, a.outcome) for a in res.attempts] == [
        ("anycalib", ATTEMPT_UNAVAILABLE),
        ("geocalib", ATTEMPT_UNAVAILABLE),
        ("plumbline", ATTEMPT_OK),
    ]
    assert res.verification is None  # 고전 기준 결과는 자기 자신으로 확인하지 않는다
    truth = wide_case.cameras["cam1"]["K"][0][0]
    assert abs(res.estimate.focal_px - truth) / truth < 0.01


def test_missing_weights_and_hash_mismatch_are_unavailable(tmp_path, monkeypatch, wide_case):
    """가중치 파일 없음(FileNotFoundError)·해시 불일치(WeightsMismatchError)는 사용 불가로 보고 다음으로 넘어간다."""
    monkeypatch.setattr(learned_module, "_require", lambda module, hint: object())  # 패키지는 있다고 친다
    wrong = tmp_path / "geocalib-distorted.tar"
    wrong.write_bytes(b"not the real weights")
    any_ = AnyCalibEstimator(WeightsRef(str(tmp_path / "missing.pt"), "sha256:" + "c" * 64))
    geo = GeoCalibEstimator(WeightsRef(str(wrong), "sha256:" + "d" * 64))
    chain = chain_of(("anycalib", any_), ("geocalib", geo), ("plumbline", PlumbLineVPEstimator()))
    res = chain.estimate_with_trace(wide_case.image("cam1"), camera_id="cam1")
    assert res.estimate.method == METHOD_PLUMB_LINE_VP
    assert res.attempts[0].outcome == ATTEMPT_UNAVAILABLE and "FileNotFoundError" in res.attempts[0].reason
    assert res.attempts[1].outcome == ATTEMPT_UNAVAILABLE and "WeightsMismatchError" in res.attempts[1].reason


def test_learned_failure_is_not_replaced_by_fallback(wide_case):
    """반례: AnyCalib 이 돌았는데 실패를 알리면 GeoCalib·고전 값으로 바꿔 채우지 않고 비운다."""
    cam = wide_case.cameras["cam1"]
    any_ = FakeLearned(METHOD_ANYCALIB, truth_params(cam), success=False)
    geo = FakeLearned(METHOD_GEOCALIB, truth_params(cam))
    chain = chain_of(("anycalib", any_), ("geocalib", geo), ("plumbline", PlumbLineVPEstimator()))
    res = chain.estimate_with_trace(wide_case.image("cam1"), camera_id="cam1")
    assert res.estimate.status == STATUS_FAILED and res.estimate.method == METHOD_ANYCALIB
    assert res.estimate.focal_px is None and res.estimate.distortion_coeffs is None
    assert geo.calls == 0 and res.backend == "anycalib"


def test_inference_exception_fails_camera_without_fallback(wide_case):
    any_ = FakeLearned(METHOD_ANYCALIB, raises=RuntimeError("CUDA out of memory"))
    geo = FakeLearned(METHOD_GEOCALIB, truth_params(wide_case.cameras["cam1"]))
    chain = chain_of(("anycalib", any_), ("geocalib", geo))
    res = chain.estimate_with_trace(wide_case.image("cam1"), camera_id="cam1")
    assert res.estimate.status == STATUS_FAILED and res.estimate.method == METHOD_ANYCALIB
    assert res.attempts[-1].outcome == ATTEMPT_ERROR and "RuntimeError" in res.attempts[-1].reason
    assert geo.calls == 0


def test_straightness_check_blanks_wrong_learned_result(wide_case):
    """반례: 학습 모델이 왜곡을 놓친 값(k1 = k2 = 0)을 내면 직선성 확인이 FAILED 로 비운다. 확인을 끄면 그대로 통과한다(음성 대조)."""
    f, cx, cy, _, _ = truth_params(wide_case.cameras["cam1"])
    wrong = (f, cx, cy, 0.0, 0.0)
    image = wide_case.image("cam1")
    chain = chain_of(("anycalib", FakeLearned(METHOD_ANYCALIB, wrong)), ("plumbline", PlumbLineVPEstimator()))
    res = chain.estimate_with_trace(image, camera_id="cam1")
    assert res.verification.status == CHECK_FAILED
    assert res.estimate.status == STATUS_FAILED and res.estimate.method == METHOD_ANYCALIB
    assert res.estimate.focal_px is None and res.estimate.radial_pixel is None
    assert res.verification.learned_rms_px > res.verification.limit_px
    assert "직선성 확인 실패" in res.estimate.uncertainty.notes[-1]
    unchecked = chain_of(("anycalib", FakeLearned(METHOD_ANYCALIB, wrong)), verify=False).estimate_with_trace(
        image, camera_id="cam1"
    )
    assert unchecked.estimate.status == STATUS_OK and unchecked.verification is None


def test_straightness_check_catches_under_corrected_distortion(wide_case):
    """경계: 왜곡을 절반쯤만 편 결과(k × 0.6)는 절대 한도(1 px) 안이어도 휜 정도를 절반 넘게 남겨 실패다."""
    f, cx, cy, k1, k2 = truth_params(wide_case.cameras["cam1"])
    res = chain_of(("anycalib", FakeLearned(METHOD_ANYCALIB, (f, cx, cy, 0.6 * k1, 0.6 * k2)))).estimate_with_trace(
        wide_case.image("cam1"), camera_id="cam1"
    )
    check = res.verification
    assert check.status == CHECK_FAILED and check.learned_rms_px <= check.limit_px
    assert check.learned_rms_px > 0.5 * check.uncorrected_rms_px and "덜 폈다" in check.reason
    assert res.estimate.status == STATUS_FAILED


def test_straightness_check_fails_when_learned_model_folds(wide_case):
    """경계: 학습 모델의 왜곡이 영상 안 직선 위에서 접히면(강한 음의 k1, 양의 k2 없음) 비운다."""
    f, cx, cy, _, _ = truth_params(wide_case.cameras["cam1"])
    res = chain_of(("anycalib", FakeLearned(METHOD_ANYCALIB, (f, cx, cy, -0.9, 0.0)))).estimate_with_trace(
        wide_case.image("cam1"), camera_id="cam1"
    )
    assert res.verification.status == CHECK_FAILED and res.estimate.status == STATUS_FAILED


def test_unverifiable_scene_keeps_learned_result_marked():
    """직선이 없는 영상은 확인할 수 없다(UNVERIFIED): 학습 결과를 지우지 않되 확인하지 못했음을 남긴다."""
    rng = np.random.default_rng(1)
    image = (rng.random((324, 576)) * 0.02 + 0.5).astype(np.float32)
    res = chain_of(("anycalib", FakeLearned(METHOD_ANYCALIB, (300.0, 288.0, 162.0, -0.2, 0.05)))).estimate_with_trace(
        image, camera_id="camX"
    )
    assert res.estimate.status == STATUS_OK and res.verification.status == CHECK_UNVERIFIED
    assert res.to_record()["straightness_check"]["status"] == CHECK_UNVERIFIED


def test_no_available_backend_without_classical_is_empty():
    """순서에 고전 기준이 없고 학습 백엔드가 모두 없으면 method NONE·FAILED (어느 방법의 결과처럼도 보이지 않는다)."""
    chain = chain_of(("anycalib", FakeLearned(METHOD_ANYCALIB, raises=LearnedBackendUnavailable("없음"))), verify=False)
    res = chain.estimate_with_trace(np.zeros((40, 60)), camera_id="cam1")
    assert res.estimate.method == METHOD_NONE and res.estimate.status == STATUS_FAILED and res.backend is None
    assert "사용할 수 있는 백엔드가 없다" in res.estimate.uncertainty.notes[0]


def test_unavailable_backend_is_not_retried_for_other_cameras(wide_case):
    any_ = FakeLearned(METHOD_ANYCALIB, raises=LearnedBackendUnavailable("없음"))
    geo = FakeLearned(METHOD_GEOCALIB, truth_params(wide_case.cameras["cam1"]))
    chain = chain_of(("anycalib", any_), ("geocalib", geo), verify=False)
    for cid in ("cam1", "cam2"):
        res = chain.estimate_with_trace(wide_case.image("cam1"), camera_id=cid)
        assert res.attempts[0].outcome == ATTEMPT_UNAVAILABLE and res.estimate.method == METHOD_GEOCALIB
    assert any_.calls == 1 and geo.calls == 2
    assert set(chain.unavailable) == {"anycalib"}


def test_build_default_chain_without_weights_records_why(wide_case):
    chain = build_default_chain()
    assert chain.order == ("anycalib", "geocalib", "plumbline")
    res = chain.estimate_with_trace(wide_case.image("cam1"), camera_id="cam1")
    assert res.estimate.method == METHOD_PLUMB_LINE_VP
    assert all("가중치" in a.reason for a in res.attempts[:2])
    with pytest.raises(ValueError):
        build_default_chain(["anycalib", "magic"])


def test_default_anycalib_camera_model_is_radial2():
    est = AnyCalibEstimator(WEIGHTS)
    assert est.model_id == "anycalib_dist" and est.cam_id == "radial:2"


def test_shared_lens_uses_estimated_principal_point():
    """AnyCalib 처럼 주점을 추정한 카메라가 있으면 공통 주점은 그 중앙값이고 가정이 아니다."""
    ests = [
        learned_estimate(camera_id=c, image_size=(1152, 648), fx=442.0, fy=442.0, cx=cx, cy=cy, radial=[-0.3, 0.09],
                         focal_std_px=None, method=METHOD_ANYCALIB,
                         model=ModelRef("AnyCalib", "1.0", "anycalib_dist", WEIGHTS, "Apache-2.0", "Apache-2.0"),
                         principal_point_assumed=False)
        for c, cx, cy in (("a", 570.0, 320.0), ("b", 574.0, 326.0), ("c", 580.0, 330.0))
    ]
    shared = combine_shared_lens(ests)
    assert shared.common.principal_point == (574.0, 326.0)
    assert shared.common.principal_point_assumed is False


def test_file_not_found_during_inference_is_failure_not_unavailable():
    """반례(리뷰 F2): 추론 중 FileNotFoundError 는 사용 불가가 아니다. 그 카메라를 비우고 GeoCalib 을 부르지 않는다."""
    any_ = FakeLearned(METHOD_ANYCALIB, raises=FileNotFoundError("inference-resource.bin"))
    geo = FakeLearned(METHOD_GEOCALIB, (40.0, 30.0, 20.0, 0.0, 0.0))
    chain = chain_of(("anycalib", any_), ("geocalib", geo), verify=False)
    for cid in ("cam1", "cam2"):
        res = chain.estimate_with_trace(np.zeros((40, 60, 3), dtype=np.uint8), camera_id=cid)
        assert res.estimate.status == STATUS_FAILED and res.estimate.method == METHOD_ANYCALIB
        assert res.attempts[-1].outcome == ATTEMPT_ERROR and "FileNotFoundError" in res.attempts[-1].reason
        assert not res.fallback
    assert geo.calls == 0 and any_.calls == 2 and chain.unavailable == {}


def test_loaded_anycalib_inference_file_error_is_failed(monkeypatch):
    """이미 읽은 실제 AnyCalib 어댑터의 predict 가 FileNotFoundError 를 내도 FAILED 다(가짜 모델·torch 대역)."""

    class LoadedModel:
        def predict(self, image, *, cam_id):
            raise FileNotFoundError("missing-inference-side-resource.bin")

    class NoGrad:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    class FakeTorch:
        @staticmethod
        def no_grad():
            return NoGrad()

    any_ = AnyCalibEstimator(WEIGHTS)
    any_._model, any_._torch = LoadedModel(), FakeTorch()
    monkeypatch.setattr(learned_module, "_image_tensor", lambda torch, image, order, device: image)
    geo = FakeLearned(METHOD_GEOCALIB, (40.0, 30.0, 20.0, 0.0, 0.0))
    res = chain_of(("anycalib", any_), ("geocalib", geo), verify=False).estimate_with_trace(
        np.zeros((40, 60, 3), dtype=np.uint8), camera_id="cam1"
    )
    assert res.estimate.status == STATUS_FAILED and res.estimate.method == METHOD_ANYCALIB and geo.calls == 0


def test_unverified_camera_is_flagged_for_bundle_adjustment():
    """UNVERIFIED 학습 결과는 지우지 않되 번들 조정 초기값에 STRAIGHTNESS_UNVERIFIED 표시가 붙는다."""
    from simai_ai.calibration.intrinsics import to_bundle_adjustment_init

    model = ModelRef("AnyCalib", "1.0", "anycalib_dist", WEIGHTS, "Apache-2.0", "Apache-2.0")
    ests = [
        learned_estimate(camera_id=c, image_size=(1152, 648), fx=442.0, fy=442.0, cx=576.0, cy=324.0,
                         radial=[-0.3, 0.09], focal_std_px=None, method=METHOD_ANYCALIB, model=model,
                         principal_point_assumed=False)
        for c in ("a", "b", "c")
    ]
    source = {"cameras": [
        {"camera_id": "a", "backend": {"straightness_check": {"status": CHECK_UNVERIFIED}}},
        {"camera_id": "b", "backend": {"straightness_check": {"status": CHECK_PASSED}}},
        {"camera_id": "c", "backend": {}},
    ]}
    init = to_bundle_adjustment_init(combine_shared_lens(ests), source)
    assert "STRAIGHTNESS_UNVERIFIED" in init.camera("a").flags
    assert "STRAIGHTNESS_UNVERIFIED" not in init.camera("b").flags + init.camera("c").flags
