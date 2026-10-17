"""보정 시험 공용 픽스처: 교실(8m x 6m) 네 모서리 2.5m 높이에서 가운데를 보는 카메라 4대와 기준을 통과하는 근거."""

import math

import pytest

from simai_ai.calibration import CalibrationEvidence, CameraCalibration, HeightCheckpoint, ScaleBasis, ScaleEstimate

T0 = 1_760_342_400_000
ROOM_CENTER = (4.0, 3.0, 0.8)
CORNERS = {"cam1": (0.2, 0.2, 2.5), "cam2": (7.8, 0.2, 2.5), "cam3": (7.8, 5.8, 2.5), "cam4": (0.2, 5.8, 2.5)}


def _normalize(v):
    n = math.sqrt(sum(x * x for x in v))
    return [x / n for x in v]


def _cross(a, b):
    return [a[1] * b[2] - a[2] * b[1], a[2] * b[0] - a[0] * b[2], a[0] * b[1] - a[1] * b[0]]


def look_at(center, target):
    """OpenCV 카메라(x 오른쪽, y 아래, z 앞)의 World → 카메라 R, t. 소수 4자리로 반올림한다(계약 예시와 같게)."""
    forward = _normalize([t - c for t, c in zip(target, center, strict=True)])
    right = _normalize(_cross(forward, [0.0, 0.0, 1.0]))
    down = _cross(forward, right)
    R = [[round(v, 4) + 0.0 for v in row] for row in (right, down, forward)]
    t = [round(-sum(R[i][j] * center[j] for j in range(3)), 4) for i in range(3)]
    return R, t


def make_camera(camera_id, median=1.2, p95=3.5, image_size=(1920, 1080)):
    R, t = look_at(CORNERS[camera_id], ROOM_CENTER)
    return CameraCalibration(
        camera_id=camera_id,
        image_size=image_size,
        K=[[1000.0, 0.0, 960.0], [0.0, 1000.0, 540.0], [0.0, 0.0, 1.0]],
        R=R,
        t=t,
        distortion_model="OPENCV_PINHOLE",
        distortion_coeffs=[-0.30, 0.09, 0.0, 0.0, -0.01],
        reprojection_median_px=median,
        reprojection_p95_px=p95,
    )


@pytest.fixture
def camera_factory():
    return make_camera


@pytest.fixture
def cameras():
    return [make_camera(cid) for cid in CORNERS]


def passing_evidence():
    """수정 6 CALIBRATED 기준을 모두 통과하는 근거."""
    return CalibrationEvidence(
        floor_coverage_ratio=0.85,
        floor_plane_disagreement_m=0.02,
        floor_plane_camera_ids=tuple(CORNERS),
        scale_estimates=(
            ScaleEstimate(ScaleBasis.MEASURED_OBJECT, 1.004, label="안전봉"),
            ScaleEstimate(ScaleBasis.MEASURED_OBJECT, 0.996, label="라바콘"),
            ScaleEstimate(ScaleBasis.KNOWN_PERSON_HEIGHT, 1.012, label="팀원 A"),
        ),
        checkpoints=(
            HeightCheckpoint("p1", 1.75, 1.81, 0.08),
            HeightCheckpoint("p2", 1.75, 1.69, 0.11),
            HeightCheckpoint("p3", 1.75, 1.78, 0.06),
            HeightCheckpoint("p4", 1.75, 1.72, 0.09),
        ),
    )


@pytest.fixture
def evidence():
    return passing_evidence()


@pytest.fixture
def t0():
    return T0
