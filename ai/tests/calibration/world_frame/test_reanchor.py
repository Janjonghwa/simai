"""같은 현장 재보정을 이전 채택 World 에 다시 맞추기 시험 (S15P31A101-584).

합성 4대 코너 배치(SIM)로 그대로·작은 yaw/이동/축척 회복·움직인 카메라 거부를 보고, 노트북에 12·13차 실제 보정 문서가 있으면
그 둘도 비교한다(없으면 건너뛴다).
"""

from __future__ import annotations

import copy
import io
import json
import math
import os
import tarfile
from pathlib import Path
from typing import Any

import numpy as np
import pytest

from simai_ai.calibration.document import content_hash
from simai_ai.calibration.world_frame.reanchor import (
    ANCHORED,
    CENTER_RESIDUAL_EXCEEDED,
    DEGENERATE_LAYOUT,
    DISABLED,
    INSUFFICIENT_CAMERAS,
    ORIENTATION_CHANGE_EXCEEDED,
    PREVIOUS_NOT_USABLE,
    SCALE_OUT_OF_RANGE,
    SITE_MISMATCH,
    ReanchorConfig,
    apply_reanchor,
    estimate_reanchor,
    reanchor_calibration,
)
from simai_ai.providers.contract import check_calibration, schema_errors
from simai_ai.providers.testing import load_example

CAL_SCHEMA = {"$ref": "https://simai.dev/contracts/providers/calibration.schema.json"}
# 로비와 비슷한 배치: 7.4 × 7 m 코너, 높이 2.44~2.48 m, 방 가운데 바닥 근처를 본다
CORNERS = {"cam_A": (7.5, 7.3, 2.44), "cam_B": (7.4, 0.7, 2.44), "cam_C": (0.0, 0.6, 2.48), "cam_D": (0.0, 7.3, 2.48)}
TARGET = np.array([3.7, 4.0, 0.8])


def _look_at(center: np.ndarray, target: np.ndarray) -> np.ndarray:
    """OpenCV 카메라(z 앞, y 아래)의 World → 카메라 회전."""
    z = target - center
    z /= np.linalg.norm(z)
    x = np.cross(z, [0.0, 0.0, 1.0])
    x /= np.linalg.norm(x)
    y = np.cross(z, x)
    return np.stack([x, y, z])


def _rz(yaw: float) -> np.ndarray:
    c, s = math.cos(yaw), math.sin(yaw)
    return np.array([[c, -s, 0.0], [s, c, 0.0], [0.0, 0.0, 1.0]])


def _doc(centers: dict[str, Any], rotations: dict[str, np.ndarray] | None = None, version: str = "cal-prev") -> dict[str, Any]:
    cams = []
    for cid, c in centers.items():
        c = np.asarray(c, float)
        R = rotations[cid] if rotations else _look_at(c, TARGET)
        cams.append({"camera_id": cid, "R": R.tolist(), "t": (-R @ c).tolist(), "camera_height_m": round(float(c[2]), 4)})
    return {"calibration_version": version, "state": "PROVISIONAL", "provider_source": "G1_AUTO",
            "coordinate_frame": {"frame": "WORLD", "unit": "m", "handedness": "right", "up_axis": "+Z", "site_id": "lobby"},
            "cameras": cams}


def _to_new_world(prev: dict[str, Any], yaw: float, shift: tuple[float, float], scale: float = 1.0,
                  jitter: dict[str, tuple[float, float]] | None = None) -> dict[str, Any]:
    """이전 World 의 같은 리그를 다른 World(새 세팅)로 표현한다: X_new = Rz(yaw)ᵀ·(s·X_prev 수평 − T), Z 그대로."""
    Rz = _rz(yaw)
    T = np.array([shift[0], shift[1], 0.0])
    new = copy.deepcopy(prev)
    new["calibration_version"] = "cal-new"
    for cam in new["cameras"]:
        R = np.asarray(cam["R"], float)
        c = -R.T @ np.asarray(cam["t"], float)
        c_xy = np.array([scale * c[0], scale * c[1], c[2]])
        dj = (jitter or {}).get(cam["camera_id"], (0.0, 0.0))
        c_new = Rz.T @ (c_xy - T) + np.array([dj[0], dj[1], 0.0])
        R_new = R @ Rz  # x_cam = R·X_prev = R·Rz·X_new (회전 부분)
        cam["R"] = R_new.tolist()
        cam["t"] = (-R_new @ c_new).tolist()
    return new


def _centers(doc: dict[str, Any]) -> dict[str, np.ndarray]:
    return {c["camera_id"]: -np.asarray(c["R"], float).T @ np.asarray(c["t"], float) for c in doc["cameras"]}


PREV = _doc(CORNERS)


def test_identical_rig_is_anchored_with_identity() -> None:
    new = copy.deepcopy(PREV)
    new["calibration_version"] = "cal-new"
    doc, res = reanchor_calibration(PREV, new)
    assert res is not None and res.accepted and res.reason == ANCHORED
    assert abs(res.yaw_rad) < 1e-9 and np.allclose(res.translation_m, 0.0, atol=1e-9) and res.scale == pytest.approx(1.0)
    for a, b in zip(doc["cameras"], new["cameras"]):
        assert np.allclose(a["R"], b["R"]) and np.allclose(a["t"], b["t"])
    rec = doc["world_anchor"]
    assert rec["accepted"] is True and rec["reference_calibration_version"] == "cal-prev" and rec["max_center_residual_m"] == 0.0


@pytest.mark.parametrize("yaw_deg", [-90.3, 1.1, 37.0, 180.0])
def test_small_jitter_recovers_yaw_translation_and_keeps_floor(yaw_deg: float) -> None:
    yaw = math.radians(yaw_deg)
    jitter = {"cam_A": (0.04, -0.03), "cam_B": (-0.05, 0.02), "cam_C": (0.03, 0.04), "cam_D": (-0.02, -0.05)}
    new = _to_new_world(PREV, yaw, (-0.2, 8.0), scale=1.0 / 0.99, jitter=jitter)
    res = estimate_reanchor(PREV, new)
    assert res.accepted, res
    assert math.degrees(abs(math.remainder(res.yaw_rad - yaw, math.tau))) < 1.0
    assert res.scale == pytest.approx(0.99, abs=0.01)  # 5 cm 흔들림이 축척을 ±0.5% 남짓 흔든다
    assert max(res.center_residuals_m.values()) < 0.08
    assert max(res.orientation_changes_rad.values()) < math.radians(1.5)

    doc = apply_reanchor(new, res)
    after, before_prev, before_new = _centers(doc), _centers(PREV), _centers(new)
    for cid in CORNERS:
        # 이전 World 로 옮겨졌고(수평 차이는 잔차·축척 차이 수준), 높이(Z, 바닥 기준)는 새 보정 그대로다
        assert np.linalg.norm(after[cid][:2] - before_prev[cid][:2]) < 0.15
        assert after[cid][2] == pytest.approx(before_new[cid][2], abs=1e-9)
    # 옮긴 뒤에도 회전 행렬·카메라 높이가 계약 검사를 통과하고(+Z 상방 유지), content_hash 를 다시 계산했다
    template = load_example("calibration.site_setup.json")
    assert check_calibration({**template, "cameras": [
        {**template["cameras"][0], "camera_id": c["camera_id"], "R": c["R"], "t": c["t"],
         "camera_height_m": round(float(after[c["camera_id"]][2]), 4)} for c in doc["cameras"]]}) == []
    assert doc["content_hash"] == content_hash(doc)


def test_moved_camera_is_rejected_and_new_world_is_kept() -> None:
    new = _to_new_world(PREV, math.radians(20.0), (1.0, -2.0), jitter={"cam_C": (0.45, 0.0)})
    doc, res = reanchor_calibration(PREV, new)
    assert res is not None and not res.accepted and res.reason == CENTER_RESIDUAL_EXCEEDED
    for a, b in zip(doc["cameras"], new["cameras"]):
        assert a["R"] == b["R"] and a["t"] == b["t"]  # 새 World 그대로
    rec = doc["world_anchor"]
    assert rec["accepted"] is False and rec["max_center_residual_m"] > 0.15 and rec["yaw_rad"] is not None


def test_scale_and_orientation_limits() -> None:
    res = estimate_reanchor(PREV, _to_new_world(PREV, 0.3, (0.5, 0.5), scale=1.06))
    assert not res.accepted and res.reason == SCALE_OUT_OF_RANGE

    turned = _to_new_world(PREV, 0.3, (0.5, 0.5))
    cam = turned["cameras"][1]
    R = np.asarray(cam["R"], float)
    c = -R.T @ np.asarray(cam["t"], float)
    a = math.radians(15.0)  # 중심은 그대로 두고 카메라만 15° 돌렸다
    R_turn = np.array([[math.cos(a), 0, math.sin(a)], [0, 1, 0], [-math.sin(a), 0, math.cos(a)]]) @ R
    cam["R"], cam["t"] = R_turn.tolist(), (-R_turn @ c).tolist()
    res2 = estimate_reanchor(PREV, turned)
    assert not res2.accepted and res2.reason == ORIENTATION_CHANGE_EXCEEDED
    assert res2.orientation_changes_rad["cam_B"] == pytest.approx(a, abs=1e-6)
    # 한도를 넓히면 같은 입력이 채택된다(한도는 설정값)
    assert estimate_reanchor(PREV, turned, ReanchorConfig(max_orientation_change_rad=math.radians(20))).accepted


def test_unusable_inputs_are_rejected_without_inventing() -> None:
    new = _to_new_world(PREV, 0.1, (0.0, 0.0))
    two = {**new, "cameras": new["cameras"][:2]}
    assert estimate_reanchor(PREV, two).reason == INSUFFICIENT_CAMERAS
    assert estimate_reanchor({**PREV, "state": "UNCALIBRATED"}, new).reason == PREVIOUS_NOT_USABLE
    other = copy.deepcopy(new)
    other["coordinate_frame"]["site_id"] = "classroom"
    assert estimate_reanchor(PREV, other).reason == SITE_MISMATCH
    tight = _doc({"cam_A": (1.0, 1.0, 2.4), "cam_B": (1.2, 1.0, 2.4), "cam_C": (1.1, 1.2, 2.4)})
    assert estimate_reanchor(tight, copy.deepcopy(tight)).reason == DEGENERATE_LAYOUT
    doc, res = reanchor_calibration(None, new)
    assert res is None and "world_anchor" not in doc and doc == new


def test_anchored_site_setup_document_matches_contract() -> None:
    base = load_example("calibration.site_setup.json")
    prev = copy.deepcopy(base)
    prev["calibration_version"] = "cal-0-jba2-setup"
    new = copy.deepcopy(base)
    for cam in new["cameras"]:  # 같은 리그를 yaw 30°·이동 (1, −2) 다른 World 로
        R, t = np.asarray(cam["R"], float), np.asarray(cam["t"], float)
        Rz, T = _rz(math.radians(30.0)), np.array([1.0, -2.0, 0.0])
        cam["R"] = (R @ Rz).tolist()
        cam["t"] = (t + R @ T).tolist()
    doc, res = reanchor_calibration(prev, new)
    assert res is not None and res.accepted and math.degrees(res.yaw_rad) == pytest.approx(30.0, abs=1e-6)
    doc["schema_version"] = "2.5.0"
    assert schema_errors(doc, CAL_SCHEMA) == []
    for a, b in zip(doc["cameras"], base["cameras"]):
        assert np.allclose(a["R"], b["R"], atol=1e-3) and np.allclose(a["t"], b["t"], atol=1e-3)  # 예시 R 은 4자리 반올림
    rejected = apply_reanchor(new, estimate_reanchor(prev, _to_new_world(prev, 0.0, (0.0, 0.0), scale=1.2)))
    assert rejected["world_anchor"]["reason"] == SCALE_OUT_OF_RANGE and schema_errors(rejected, CAL_SCHEMA) == []


# ---------------------------------------------------------------------------- 12·13차 실제 보정(로컬에만)
_LOCAL = Path(os.environ.get("SIMAI_LOCAL_DIR", "C:/Users/SSAFY/simai-local"))
_S12 = _LOCAL / "session12/results/lobby-s12/state/calibrations/9b03503b3371dd5588706fbd66fac466e7486090599770a2d070584765909d80.json"
_S13_TGZ = _LOCAL / "session13/results-1109-1811/pod.tgz"
_S13_DIR = "runs/lobby-s13-0724/state/calibrations/"
_S13 = {
    "first": "b502bf08787909730b3847e1ad00b653667808e6ccdd4e507a2dfe852e7e1dee.json",  # cal-lobby-init-878286ab
    "final": "f2ba547c70f01e3577396c89f77435194040ec0fbb3eebd0d36d383d73ba45bb.json",  # cal-lobby-init-079f00cd
}


@pytest.fixture(scope="module")
def real_docs() -> dict[str, dict[str, Any]]:
    if not (_S12.is_file() and _S13_TGZ.is_file()):
        pytest.skip(f"로컬 12·13차 보정 문서 없음: {_LOCAL}")
    out = {"s12": json.loads(_S12.read_text(encoding="utf-8"))}
    want = {_S13_DIR + v: k for k, v in _S13.items()}
    with tarfile.open(_S13_TGZ, "r:gz") as tar:  # JSON 두 개만 메모리로 읽는다(풀어 쓰지 않음)
        for member in tar:
            if member.name in want and member.isfile():
                fh = tar.extractfile(member)
                assert fh is not None
                out[want[member.name]] = json.load(io.TextIOWrapper(fh, encoding="utf-8"))
            if len(out) == 3:
                break
    if len(out) != 3:
        pytest.skip("13차 묶음에 보정 문서가 없다")
    return out


def test_real_session12_to_session13_is_anchored(real_docs: dict[str, dict[str, Any]]) -> None:
    """12차(기울기 보정 전)와 13차(같은 로비·같은 거치대) 보정: 카메라는 그대로라 채택되고 잔차는 수 cm 다."""
    s12, first, final = real_docs["s12"], real_docs["first"], real_docs["final"]
    res = estimate_reanchor(s12, first)
    assert res.accepted and res.reason == ANCHORED
    assert res.scale == pytest.approx(0.9896, abs=0.002)  # 사용자 Umeyama 관측(0.9896)과 같다
    assert max(res.center_residuals_m.values()) < 0.10  # 관측 4~7 cm
    assert math.degrees(max(res.orientation_changes_rad.values())) < 6.0
    assert math.degrees(res.yaw_rad) == pytest.approx(-90.3, abs=0.5)  # 13차 첫 세팅은 +X 벽이 90° 다르게 잡혔다
    for prev, new in ((s12, final), (first, final)):
        r = estimate_reanchor(prev, new)
        assert r.accepted and max(r.center_residuals_m.values()) < 0.10, r
    doc = apply_reanchor(first, res)
    moved, ref = _centers(doc), _centers(s12)
    assert max(np.linalg.norm(moved[c][:2] - ref[c][:2]) for c in ref) < 0.15  # 적용(축척 1) 뒤 이전 World 와 수평 차이


def test_disabled_keeps_new_world_and_records_reason() -> None:
    """운영자가 끄면(SIMAI_SETUP_WORLD_ANCHOR=off) 맞출 수 있는 리그여도 변환하지 않고 DISABLED 만 남긴다."""
    new = _to_new_world(PREV, 0.4, (1.0, 2.0))
    doc, res = reanchor_calibration(PREV, new, enabled=False)
    assert res is not None and not res.accepted and res.reason == DISABLED and res.yaw_rad is None
    for a, b in zip(doc["cameras"], new["cameras"]):
        assert a["R"] == b["R"] and a["t"] == b["t"]
    rec = doc["world_anchor"]
    assert rec["accepted"] is False and rec["reason"] == "DISABLED" and rec["reference_calibration_version"] == "cal-prev"
    full = {**load_example("calibration.site_setup.json"), "world_anchor": rec, "schema_version": "2.5.0"}
    assert schema_errors(full, CAL_SCHEMA) == []
