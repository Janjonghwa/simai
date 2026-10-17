"""운영 중 틀어짐 감지 시험 (명세 4.4 DRIFTED, 4.5)."""

from simai_ai.calibration import CameraObservation, DriftConfig, DriftReason, detect_drift


def _doc(camera_factory, **kwargs):
    return {"cameras": [camera_factory("cam1", **kwargs).to_contract(), camera_factory("cam2").to_contract()]}


def test_no_drift(camera_factory):
    obs = [CameraObservation("cam1", (1920, 1080), 1.5, 4.0), CameraObservation("cam2", (1920, 1080))]
    assert detect_drift(_doc(camera_factory), obs) == ()


def test_resolution_changed(camera_factory):
    findings = detect_drift(_doc(camera_factory), [CameraObservation("cam2", (1280, 720))])
    assert [(f.camera_id, f.reason) for f in findings] == [("cam2", DriftReason.RESOLUTION_CHANGED)]


def test_field_of_view_changed(camera_factory):
    findings = detect_drift(_doc(camera_factory), [CameraObservation("cam1", field_of_view_changed=True)])
    assert [f.reason for f in findings] == [DriftReason.FIELD_OF_VIEW_CHANGED]


def test_reprojection_increased(camera_factory):
    findings = detect_drift(_doc(camera_factory), [CameraObservation("cam1", reprojection_median_px=2.5)])
    assert [f.reason for f in findings] == [DriftReason.REPROJECTION_INCREASED]
    findings = detect_drift(_doc(camera_factory), [CameraObservation("cam1", reprojection_p95_px=5.5)])
    assert [f.reason for f in findings] == [DriftReason.REPROJECTION_INCREASED]


def test_reprojection_baseline_above_limit(camera_factory):
    """처음부터 상한을 넘던 카메라는 보정 당시 값보다 커질 때만 틀어짐이다."""
    doc = _doc(camera_factory, median=3.0, p95=6.0)
    assert detect_drift(doc, [CameraObservation("cam1", reprojection_median_px=2.9, reprojection_p95_px=6.0)]) == ()
    assert detect_drift(doc, [CameraObservation("cam1", reprojection_median_px=3.1)])


def test_unknown_values_are_not_drift(camera_factory):
    assert detect_drift(_doc(camera_factory), [CameraObservation("cam1")]) == ()


def test_camera_not_in_calibration_ignored(camera_factory):
    assert detect_drift(_doc(camera_factory), [CameraObservation("cam9", (640, 480), 9.0, 9.0)]) == ()


def test_config(camera_factory):
    obs = [CameraObservation("cam1", reprojection_median_px=1.5)]
    assert detect_drift(_doc(camera_factory), obs, DriftConfig(max_reprojection_median_px=1.4))


def test_invalid_observation_values_are_unknown(camera_factory):
    """NaN·무한대·음수 관측값은 틀어짐 근거로 쓰지 않는다(모르는 값과 같다)."""
    obs = [CameraObservation("cam1", reprojection_median_px=float("nan"), reprojection_p95_px=float("inf"))]
    assert detect_drift(_doc(camera_factory), obs) == ()
    assert detect_drift(_doc(camera_factory), [CameraObservation("cam1", reprojection_median_px=-3.0)]) == ()
