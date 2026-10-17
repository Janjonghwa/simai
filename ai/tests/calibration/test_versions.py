"""보정 버전 저장소 시험 (명세 4.5 원자적 전환·calibration_version, 4.3 동결, 규칙 6 덮어쓰지 않는다)."""

import dataclasses
import json
import threading

import pytest

from simai_ai.calibration import (
    CalibrationEvidence,
    CalibrationState,
    CalibrationVersionStore,
    CameraObservation,
    RejectReason,
    VersionedCalibrationProvider,
)
from simai_ai.calibration.drift import DriftFinding, DriftReason
from simai_ai.providers import contract
from simai_ai.providers.testing import CalibrationProviderContract, assert_no_violations

S = CalibrationState
WEAK = CalibrationEvidence()  # 근거 없음 → PROVISIONAL


def _cams(cameras, median):
    return [dataclasses.replace(c, reprojection_median_px=median) for c in cameras]


def _propose(store, cameras, evidence, at_ms, source="G1_AUTO"):
    return store.propose(
        provider_source=source, provider_version="g1-0.1.0", cameras=cameras, evidence=evidence, at_ms=at_ms
    )


@pytest.fixture
def store():
    return CalibrationVersionStore(site_id="classroom-1")


def test_empty_store_is_uncalibrated(store):
    assert store.state is S.UNCALIBRATED
    assert store.snapshot() is None
    assert store.versions == ()
    with pytest.raises(LookupError):
        VersionedCalibrationProvider(store).current()


def test_first_provisional_adopted(store, cameras, t0):
    result = _propose(store, cameras, WEAK, t0)
    assert result.adopted
    assert result.doc["calibration_version"] == "cal-1"
    assert result.doc["state"] == "PROVISIONAL"
    assert store.state is S.PROVISIONAL
    assert [(t.from_state, t.to_state, t.from_version, t.to_version) for t in store.transitions] == [
        (S.UNCALIBRATED, S.PROVISIONAL, None, "cal-1")
    ]
    assert store.transitions[0].reasons, "미달 사유를 기록한다"


def test_provisional_switch_needs_meaningful_improvement(store, cameras, t0):
    _propose(store, _cams(cameras, 3.0), WEAK, t0)
    small = _propose(store, _cams(cameras, 2.8), WEAK, t0 + 1)  # 6.7% 개선
    assert not small.adopted and small.reject_reason is RejectReason.NOT_IMPROVED
    assert len(store.versions) == 1
    big = _propose(store, _cams(cameras, 2.7), WEAK, t0 + 2)  # 10% 개선
    assert big.adopted and big.doc["calibration_version"] == "cal-2"
    assert store.snapshot().calibration_version == "cal-2"


def test_unknown_reprojection_is_not_improvement(store, cameras, t0):
    _propose(store, _cams(cameras, 3.0), WEAK, t0)
    result = _propose(store, _cams(cameras, None), WEAK, t0 + 1)
    assert result.reject_reason is RejectReason.NOT_IMPROVED


def test_calibrated_candidate_replaces_provisional(store, cameras, evidence, t0):
    _propose(store, _cams(cameras, 1.0), WEAK, t0)
    result = _propose(store, _cams(cameras, 1.5), evidence, t0 + 1)  # 재투영은 나빠졌지만 독립 검증 통과
    assert result.adopted and store.state is S.CALIBRATED


def test_calibrated_is_frozen(store, cameras, evidence, t0):
    _propose(store, cameras, evidence, t0)
    for cand_evidence in (evidence, WEAK):
        result = _propose(store, _cams(cameras, 0.1), cand_evidence, t0 + 1)
        assert not result.adopted and result.reject_reason is RejectReason.FROZEN
    assert len(store.versions) == 1


def test_versions_are_append_only(store, cameras, evidence, t0):
    first = _propose(store, _cams(cameras, 3.0), WEAK, t0).doc
    _propose(store, cameras, evidence, t0 + 1)
    assert store.versions[0] == first
    assert [v["calibration_version"] for v in store.versions] == ["cal-1", "cal-2"]


def test_drift_marks_state_without_new_version(store, cameras, evidence, t0):
    _propose(store, cameras, evidence, t0)
    before = store.snapshot()
    findings = store.observe([CameraObservation("cam3", (1280, 720))], t0 + 10)
    assert [f.reason for f in findings] == [DriftReason.RESOLUTION_CHANGED]
    assert store.state is S.DRIFTED
    assert len(store.versions) == 1 and store.versions[0]["state"] == "CALIBRATED"
    assert before.state is S.CALIBRATED, "이전 스냅숏은 바뀌지 않는다"
    current = VersionedCalibrationProvider(store).current()
    assert current["state"] == "DRIFTED" and current["calibration_version"] == "cal-1"
    assert contract.check_calibration_schema(current) == []
    last = store.transitions[-1]
    assert (last.from_state, last.to_state, last.to_version) == (S.CALIBRATED, S.DRIFTED, "cal-1")


def test_no_drift_keeps_state(store, cameras, evidence, t0):
    _propose(store, cameras, evidence, t0)
    assert store.observe([CameraObservation("cam1", (1920, 1080), 1.0, 3.0)], t0 + 1) == ()
    assert store.state is S.CALIBRATED


def test_recalibration_after_drift(store, cameras, evidence, t0):
    _propose(store, cameras, evidence, t0)
    store.observe([CameraObservation("cam1", reprojection_median_px=4.0)], t0 + 1)
    assert not store.report_drift([DriftFinding("cam1", DriftReason.REPROJECTION_INCREASED, "x")], t0 + 2)
    result = _propose(store, _cams(cameras, 1.9), WEAK, t0 + 3)  # PROVISIONAL 재보정도 받는다
    assert result.adopted and store.state is S.PROVISIONAL
    result = _propose(store, cameras, evidence, t0 + 4)
    assert result.adopted and store.state is S.CALIBRATED
    assert [v["calibration_version"] for v in store.versions] == ["cal-1", "cal-2", "cal-3"]


def test_provisional_can_drift(store, cameras, t0):
    _propose(store, cameras, WEAK, t0)
    store.observe([CameraObservation("cam2", field_of_view_changed=True)], t0 + 1)
    assert store.state is S.DRIFTED


@pytest.mark.parametrize("source", sorted(contract.NEVER_CALIBRATED_SOURCES))
def test_never_calibrated_sources_stay_provisional(store, cameras, evidence, t0, source):
    result = _propose(store, cameras, evidence, t0, source=source)
    assert result.adopted and result.doc["state"] == "PROVISIONAL"
    assert store.observe([CameraObservation("cam1", (640, 480))], t0 + 1)
    assert store.state is S.PROVISIONAL, "계약상 DRIFTED 가 될 수 없다"


def test_manual_pnp_stays_provisional(store, cameras, evidence, t0):
    assert _propose(store, cameras, evidence, t0, source="MANUAL_PNP").doc["state"] == "PROVISIONAL"


def test_contract_violation_rejected(store, cameras, t0):
    bad = [dataclasses.replace(cameras[0], R=[[1.0, 0.0, 0.0], [0.0, 1.0, 0.0], [0.0, 0.0, 2.0]])] + cameras[1:]
    result = _propose(store, bad, WEAK, t0)
    assert not result.adopted and result.reject_reason is RejectReason.CONTRACT_VIOLATION
    assert result.violations
    assert store.state is S.UNCALIBRATED and store.versions == ()


def test_time_must_not_go_back(store, cameras, t0):
    _propose(store, cameras, WEAK, t0)
    with pytest.raises(ValueError):
        _propose(store, _cams(cameras, 0.5), WEAK, t0 - 1)
    with pytest.raises(TypeError):
        _propose(store, cameras, WEAK, float(t0))


def test_snapshot_is_isolated(store, cameras, evidence, t0):
    _propose(store, cameras, evidence, t0)
    snap = store.snapshot()
    doc = snap.doc()
    doc["cameras"][0]["t"][0] = 99.0
    store.versions[0]["cameras"][0]["t"][0] = 99.0
    assert snap.doc()["cameras"][0]["t"][0] != 99.0
    assert store.versions[0]["cameras"][0]["t"][0] != 99.0


def test_concurrent_proposals_get_unique_versions(cameras, t0):
    store = CalibrationVersionStore()
    _propose(store, _cams(cameras, 100.0), WEAK, t0)
    medians = [100.0 * 0.85**i for i in range(1, 9)]

    def run(m):
        _propose(store, _cams(cameras, m), WEAK, t0 + 1)

    threads = [threading.Thread(target=run, args=(m,)) for m in medians]
    for th in threads:
        th.start()
    for th in threads:
        th.join()
    ids = [v["calibration_version"] for v in store.versions]
    assert len(ids) == len(set(ids))
    assert ids == [f"cal-{i}" for i in range(1, len(ids) + 1)]
    assert store.snapshot().calibration_version == ids[-1]
    assert len(store.transitions) == len(ids)


# --- 리뷰 회귀: 유효하지 않은 후보(M1)·카메라 누락(M2)·오래된 drift(M4) ---


def _state_of(store):
    return store.versions, store.transitions, store.snapshot().calibration_version, store.state


def test_nan_evidence_never_adopted_as_calibrated(store, cameras, evidence, t0):
    result = _propose(store, cameras, dataclasses.replace(evidence, floor_coverage_ratio=float("nan")), t0)
    assert not result.adopted and result.reject_reason is RejectReason.CONTRACT_VIOLATION
    assert store.state is S.UNCALIBRATED and store.versions == ()


def test_invalid_candidate_keeps_previous_version(store, cameras, evidence, t0):
    """계약 검증에 실패하는 후보(음수 바닥 평면 차이)는 DRIFTED 이전 버전도 교체하지 못한다."""
    assert _propose(store, cameras, evidence, t0).adopted
    store.observe([CameraObservation("cam1", field_of_view_changed=True)], t0 + 1)
    before = _state_of(store)
    result = _propose(store, cameras, dataclasses.replace(evidence, floor_plane_disagreement_m=-0.01), t0 + 2)
    assert not result.adopted and result.reject_reason is RejectReason.CONTRACT_VIOLATION
    assert result.violations
    assert _state_of(store) == before
    assert store.state is S.DRIFTED and store.snapshot().calibration_version == "cal-1"


@pytest.mark.parametrize(
    "camera_change",
    [
        pytest.param({"R": [[float("nan"), 0.0, 0.0], [0.0, 1.0, 0.0], [0.0, 0.0, 1.0]]}, id="R-nan"),
        pytest.param({"reprojection_median_px": float("nan")}, id="median-nan"),
        pytest.param({"reprojection_p95_px": -1.0}, id="p95-negative"),
    ],
)
def test_non_finite_or_out_of_range_camera_rejected(store, cameras, evidence, t0, camera_change):
    bad = [dataclasses.replace(cameras[0], **camera_change), *cameras[1:]]
    result = _propose(store, bad, evidence, t0)
    assert not result.adopted and result.reject_reason is RejectReason.CONTRACT_VIOLATION
    assert store.versions == ()


_K = [[1000.0, 0.0, 960.0], [0.0, 1000.0, 540.0], [0.0, 0.0, 1.0]]


@pytest.mark.parametrize(
    "camera_change",
    [
        pytest.param({"K": _K[:2]}, id="K-2x3"),
        pytest.param({"K": [*_K[:2], [0.0, 0.0, 1.0, 0.0]]}, id="K-row-4"),
        pytest.param({"t": "append"}, id="t-4"),
        pytest.param({"t": "short"}, id="t-2"),
        pytest.param({"distortion_model": "UNSUPPORTED"}, id="distortion-unknown-model"),
        pytest.param({"image_size": (1920, 1080, 3)}, id="image_size-3"),
        pytest.param({"image_size": (0, 1080)}, id="image_size-zero"),
        pytest.param({"camera_id": "cam 1"}, id="camera_id-pattern"),
    ],
)
def test_malformed_candidate_keeps_previous_version(store, cameras, evidence, t0, camera_change):
    """계약 형식(행렬 크기·벡터 길이·enum·패턴)이 틀린 후보는 DRIFTED 이전 버전도 교체하지 못한다 (리뷰 r2 F1)."""
    assert _propose(store, cameras, evidence, t0).adopted
    store.observe([CameraObservation("cam1", field_of_view_changed=True)], t0 + 1)
    before = _state_of(store)
    change = dict(camera_change)
    if change.get("t") == "append":
        change["t"] = [*cameras[0].t, 99.0]
    elif change.get("t") == "short":
        change["t"] = list(cameras[0].t[:2])
    bad = [dataclasses.replace(cameras[0], **change), *cameras[1:]]
    result = _propose(store, bad, evidence, t0 + 2)
    assert not result.adopted and result.reject_reason is RejectReason.CONTRACT_VIOLATION
    assert result.violations
    assert _state_of(store) == before
    assert store.state is S.DRIFTED and store.snapshot().calibration_version == "cal-1"


def test_unknown_provider_source_rejected(store, cameras, t0):
    assert _propose(store, cameras, WEAK, t0).adopted
    before = _state_of(store)
    result = _propose(store, _cams(cameras, 0.5), WEAK, t0 + 1, source="BOGUS")
    assert not result.adopted and result.reject_reason is RejectReason.CONTRACT_VIOLATION
    assert _state_of(store) == before


@pytest.mark.parametrize(
    "source", [["G1_AUTO"], {"name": "G1_AUTO"}, 3, None, True], ids=["list", "dict", "int", "none", "bool"]
)
def test_wrong_type_provider_source_rejected(store, cameras, evidence, t0, source):
    """잘못된 타입의 provider_source 는 TypeError 가 아니라 CONTRACT_VIOLATION 이다 (-115 리뷰 r3 권장).

    DRIFTED 상태(어떤 후보든 전환 가능)에서 넣어도 이전 버전이 그대로 남는다.
    """
    assert _propose(store, cameras, evidence, t0).adopted
    store.observe([CameraObservation("cam1", field_of_view_changed=True)], t0 + 1)
    before = _state_of(store)
    result = _propose(store, cameras, evidence, t0 + 2, source=source)
    assert not result.adopted and result.reject_reason is RejectReason.CONTRACT_VIOLATION
    assert any("provider_source" in v for v in result.violations), result.violations
    assert _state_of(store) == before


def test_wrong_type_provider_version_rejected(store, cameras, t0):
    result = store.propose(provider_source="G1_AUTO", provider_version=["g1"], cameras=cameras, evidence=WEAK, at_ms=t0)
    assert not result.adopted and result.reject_reason is RejectReason.CONTRACT_VIOLATION
    assert any("provider_version" in v for v in result.violations), result.violations
    assert store.versions == ()


def test_evidence_record_keeps_raw_scale_basis(store, cameras, evidence, t0):
    """채택한 버전마다 스케일 근거 원자료(정물 이름·배율·표본 수)와 검산점 관측을 감사 기록으로 남긴다."""
    assert _propose(store, _cams(cameras, 3.0), WEAK, t0).adopted
    assert _propose(store, cameras, evidence, t0 + 1).adopted
    rejected = _propose(store, cameras, evidence, t0 + 2)  # CALIBRATED 동결로 거절 → 기록하지 않는다
    assert not rejected.adopted

    record = store.evidence_record("cal-2")
    scales = record["evidence"]["scale_estimates"]
    assert [(s["basis"], s["scale"], s["sample_count"], s["label"]) for s in scales] == [
        ("MEASURED_OBJECT", 1.004, 1, "안전봉"),
        ("MEASURED_OBJECT", 0.996, 1, "라바콘"),
        ("KNOWN_PERSON_HEIGHT", 1.012, 1, "팀원 A"),
    ]
    assert [c["point_id"] for c in record["evidence"]["checkpoints"]] == ["p1", "p2", "p3", "p4"]
    assert record["evidence"]["floor_plane_camera_ids"] == ["cam1", "cam2", "cam3", "cam4"]
    assert record["scale_decision"]["primary"] == "MEASURED_OBJECT"
    assert record["scale_decision"]["cross_checks"] == ["KNOWN_PERSON_HEIGHT"]
    assert store.evidence_record("cal-1")["evidence"]["scale_estimates"] == []

    record["evidence"]["scale_estimates"].clear()
    assert store.evidence_record("cal-2")["evidence"]["scale_estimates"], "사본을 준다"
    with pytest.raises(LookupError):
        store.evidence_record("cal-3")


def test_stored_documents_are_strict_json(store, cameras, evidence, t0):
    _propose(store, cameras, evidence, t0)
    json.dumps(store.versions, allow_nan=False)
    assert contract.check_calibration(VersionedCalibrationProvider(store).current()) == []


def test_missing_camera_not_calibrated_in_store(store, cameras, evidence, t0):
    one = dataclasses.replace(evidence, floor_plane_camera_ids=("cam1",))
    result = _propose(store, cameras[:1], one, t0)
    assert result.doc["state"] == "PROVISIONAL"
    assert store.state is not S.CALIBRATED


def test_store_checks_installed_camera_set(cameras, evidence, t0):
    store = CalibrationVersionStore(camera_ids=["cam1", "cam2", "cam3", "cam4", "cam5"])
    assert _propose(store, cameras, evidence, t0).doc["state"] == "PROVISIONAL"
    store = CalibrationVersionStore(camera_ids=["cam1", "cam2", "cam3", "cam4"])
    assert _propose(store, cameras, evidence, t0).doc["state"] == "CALIBRATED"


def test_stale_drift_observation_does_not_taint_new_version(store, cameras, evidence, t0, monkeypatch):
    """cal-1 스냅숏으로 감지하는 도중 cal-2 로 전환되면, cal-1 기준 근거를 cal-2 에 적용하지 않는다."""
    from simai_ai.calibration import versions

    assert _propose(store, cameras, evidence, t0).adopted
    original = versions.detect_drift
    calls = []

    def interleaved(doc, observations, config):
        calls.append(doc["calibration_version"])
        if len(calls) == 1:  # cal-1 스냅숏을 잡은 직후 다른 경로가 DRIFTED 표시 → 해상도를 바꾼 cal-2 채택
            store.report_drift([DriftFinding("cam2", DriftReason.FIELD_OF_VIEW_CHANGED, "x")], t0 + 1)
            new_cams = [dataclasses.replace(cameras[0], image_size=(1280, 720)), *cameras[1:]]
            assert _propose(store, new_cams, evidence, t0 + 2).adopted
        return original(doc, observations, config)

    monkeypatch.setattr(versions, "detect_drift", interleaved)
    findings = store.observe([CameraObservation("cam1", (1280, 720))], t0 + 3)
    assert store.snapshot().calibration_version == "cal-2"
    assert store.state is S.CALIBRATED, [t.reasons for t in store.transitions]
    assert findings == (), "최신 버전으로 다시 감지한 결과를 돌려준다"
    assert calls == ["cal-1", "cal-2"]


def test_report_drift_ignores_other_version(store, cameras, evidence, t0):
    _propose(store, cameras, evidence, t0)
    finding = DriftFinding("cam1", DriftReason.FIELD_OF_VIEW_CHANGED, "x")
    assert not store.report_drift([finding], t0 + 1, calibration_version="cal-0")
    assert store.state is S.CALIBRATED
    assert store.report_drift([finding], t0 + 2, calibration_version="cal-1")
    assert store.state is S.DRIFTED


# --- calibration_version 태깅 ---


def test_tag_adds_calibration_version(store, cameras, t0):
    _propose(store, cameras, WEAK, t0)
    snap = store.snapshot()
    result = {"world_id": "w-1", "position_xy": [1.0, 2.0]}
    tagged = snap.tag(result)
    assert tagged == {**result, "calibration_version": "cal-1"}
    assert "calibration_version" not in result, "원본을 바꾸지 않는다"
    assert snap.tag(tagged) == tagged


def test_tag_refuses_other_version(store, cameras, t0):
    _propose(store, cameras, WEAK, t0)
    with pytest.raises(ValueError):
        store.snapshot().tag({"calibration_version": "cal-0"})


def test_snapshot_pins_version_during_switch(store, cameras, evidence, t0):
    """계산 하나가 시작할 때 잡은 스냅숏은 도중에 새 버전으로 전환돼도 같은 버전을 붙인다."""
    _propose(store, _cams(cameras, 3.0), WEAK, t0)
    snap = store.snapshot()
    _propose(store, cameras, evidence, t0 + 1)
    assert snap.tag({})["calibration_version"] == "cal-1"
    assert store.snapshot().tag({})["calibration_version"] == "cal-2"


def test_provenance_matches_doc(store, cameras, evidence, t0):
    _propose(store, cameras, evidence, t0)
    provider = VersionedCalibrationProvider(store)
    assert provider.provenance() == {"source": "G1_AUTO", "calibration_version": "cal-1"}
    assert_no_violations(contract.check_calibration_provenance(provider))


# --- CalibrationProvider 공통 계약 시험 ---


class TestG1AutoCalibratedProvider(CalibrationProviderContract):
    require_upright_cameras = True

    @pytest.fixture
    def provider(self, cameras, evidence, t0):
        store = CalibrationVersionStore(site_id="classroom-1")
        assert _propose(store, cameras, evidence, t0).adopted
        return VersionedCalibrationProvider(store)


class TestG1AutoProvisionalProvider(CalibrationProviderContract):
    @pytest.fixture
    def provider(self, cameras, t0):
        store = CalibrationVersionStore()
        _propose(store, cameras, WEAK, t0)
        return VersionedCalibrationProvider(store)


class TestCharucoDriftedProvider(CalibrationProviderContract):
    @pytest.fixture
    def provider(self, cameras, evidence, t0):
        store = CalibrationVersionStore()
        _propose(store, cameras, evidence, t0, source="CHARUCO")
        store.observe([CameraObservation("cam1", (1280, 720))], t0 + 1)
        return VersionedCalibrationProvider(store)
