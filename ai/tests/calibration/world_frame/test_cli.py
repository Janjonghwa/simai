"""World 고정 실행 도구(-102 fix): PROVISIONAL 보정 문서 계약 검증, -214 World 정렬 입력 연결, 실패 시 비움, 덮어쓰기 금지."""

from __future__ import annotations

import json

import numpy as np
import pytest

from simai_ai.calibration.extrinsics.initial import load_mapanything_cameras, load_world_alignment
from simai_ai.calibration.world_frame import cli
from simai_ai.providers.contract import camera_center, check_calibration, check_input_provenance, schema_errors

from .conftest import CAMS, intrinsics_record, make_run

NOW = 1_792_600_000_000


def _intr(tmp_path, source="SIM"):
    p = tmp_path / f"intr-{source}.json"
    p.write_text(json.dumps(intrinsics_record(source=source)), encoding="utf-8")
    return p


def _scale(tmp_path, value):
    p = tmp_path / "scale.json"
    p.write_text(json.dumps({"estimates": [
        {"basis": "MEASURED_OBJECT", "scale": value, "label": "cone-1"},
        {"basis": "MEASURED_OBJECT", "scale": value, "label": "pole-1"},
    ]}), encoding="utf-8")
    return p


def _fix(tmp_path, run, *extra, out=None, intr_source="SIM"):
    out = out or tmp_path / "wf.json"
    code = cli.main(["fix", "--run-dir", str(run.run_dir), "--intrinsics", str(_intr(tmp_path, intr_source)), "--out", str(out),
                     *extra], now_ms=NOW)
    return code, (json.loads(out.read_text(encoding="utf-8")) if out.exists() else None), out


def test_fix_writes_provisional_contract_doc_and_214_alignment(tmp_path):
    pytest.importorskip("cv2")  # 사람 마스크(extras calibration)
    sim = make_run(tmp_path, masks=True)
    code, rec, out = _fix(tmp_path, sim, "--prepared", str(sim.prepared_dir), "--scale-evidence",
                          str(_scale(tmp_path, 1 / 1.04)), "--site-id", "classroom-1")
    assert code == cli.EXIT_OK, rec and rec.get("detail")
    doc = rec["calibration"]
    assert schema_errors(doc, "calibration") == [] and check_calibration(doc) == []
    assert doc["state"] == "PROVISIONAL" and doc["provider_source"] == "SIM"  # SIM 입력 → SIM 출처(CALIBRATED 불가)
    assert doc["scale_basis"] == "MEASURED_OBJECT" and "MODEL_ESTIMATE" in doc["scale_cross_check_basis"]
    assert "validation" not in doc  # 독립 검증 전(모르는 값을 채우지 않는다)
    assert all("reprojection_error_px" not in c for c in doc["cameras"])  # 관측 재투영 오차는 번들 조정 몫
    prov = rec["input_provenance"]
    assert prov["is_upper_bound"] is True and prov["calibration"]["source"] == "SIM"  # SIM 입력이 섞여 상한
    assert check_input_provenance(prov) == [] and rec["upper_bound_inputs"] == {"frames": "SIM", "intrinsics": "SIM"}
    assert rec["inputs"]["mapanything"]["person_masks_applied"] is True
    # 근거 보고: 재투영(카메라 간), 원점·축 규칙, 스케일
    assert set(rec["reprojection"]["cameras"]) == set(CAMS)
    assert rec["evidence"]["axes"]["rule"] and rec["evidence"]["origin"]["point_ma"]
    assert rec["evidence"]["scale"]["basis"] == "MEASURED_OBJECT"
    # 카메라 중심 = 정답 위치(모서리 원점·축 순서만 다를 수 있다): 높이와 카메라 사이 거리
    centers = {c["camera_id"]: np.array(camera_center(c["R"], c["t"])) for c in doc["cameras"]}
    for cid, c in CAMS.items():
        assert centers[cid][2] == pytest.approx(c[2], abs=0.02)
    assert np.linalg.norm(centers["cam_A"] - centers["cam_C"]) == pytest.approx(
        np.linalg.norm(np.subtract(CAMS["cam_A"], CAMS["cam_C"])), abs=0.03)
    # -214 가 그대로 읽고, 같은 cameras.json 이면 통과한다
    align = load_world_alignment(out)
    cams = load_mapanything_cameras(sim.run_dir / "cameras.json")
    assert align.record["cameras_json_sha256"] == cams.record["sha256"]
    ext = align.apply(cams.extrinsics)
    for c in doc["cameras"]:
        R, t = ext[c["camera_id"]]
        assert np.allclose(R, c["R"], atol=1e-9) and np.allclose(t, c["t"], atol=1e-9)
    assert align.record["input_provenance"]["is_upper_bound"] is True


def test_fix_failure_leaves_alignment_empty_and_214_refuses(tmp_path):
    sim = make_run(tmp_path, drop_labels=(0,))
    code, rec, out = _fix(tmp_path, sim, "--frames-source", "SIM")
    assert code == cli.EXIT_NOT_FIXED
    assert rec["status"] == "FAILED" and rec["reason"] == "FLOOR_NOT_FOUND"
    assert "mapanything_to_world" not in rec and "calibration" not in rec
    assert any("사람 마스크" in w for w in rec["warnings"])
    with pytest.raises(ValueError, match="FLOOR_NOT_FOUND"):
        load_world_alignment(out)


def test_fix_refuses_overwrite_and_missing_provenance(tmp_path, capsys):
    sim = make_run(tmp_path)
    out = tmp_path / "exists.json"
    out.write_text("old", encoding="utf-8")
    code = cli.main(["fix", "--run-dir", str(sim.run_dir), "--intrinsics", str(_intr(tmp_path)), "--frames-source", "SIM",
                     "--out", str(out)], now_ms=NOW)
    assert code == cli.EXIT_CONFIG and out.read_text(encoding="utf-8") == "old"
    code, rec, _ = _fix(tmp_path, sim, out=tmp_path / "new.json")  # 입력 영상 출처를 모르면 시작하지 않는다
    assert code == cli.EXIT_CONFIG and rec is None and "--frames-source" in capsys.readouterr().err


def test_fix_without_scale_evidence_records_model_estimate(tmp_path):
    sim = make_run(tmp_path)
    code, rec, _ = _fix(tmp_path, sim, "--frames-source", "FILE", "--dataset-id", "classroom-stills-test",
                        intr_source="LIVE")
    assert code == cli.EXIT_OK
    doc = rec["calibration"]
    assert doc["provider_source"] == "G1_AUTO" and doc["state"] == "PROVISIONAL"
    assert doc["scale_basis"] == "MODEL_ESTIMATE" and "scale_cross_check_basis" not in doc
    assert rec["input_provenance"]["is_upper_bound"] is False
    assert "accuracy_note" in rec["evidence"]["scale"]
    # 모델 길이(정답 × 1.04)를 그대로 쓴 것이 기록과 맞는다
    assert rec["evidence"]["room"]["camera_heights_m"]["cam_A"] == pytest.approx(CAMS["cam_A"][2] * 1.04, abs=0.02)


def test_sim_scale_evidence_marks_result_upper_bound(tmp_path):
    sim = make_run(tmp_path)
    p = tmp_path / "scale-sim.json"
    p.write_text(json.dumps({"estimates": [{"basis": "KNOWN_PERSON_HEIGHT", "scale": 0.96}],
                             "input_provenance": {"frames": {"source": "SIM"}, "is_upper_bound": True}}), encoding="utf-8")
    code, rec, out = _fix(tmp_path, sim, "--frames-source", "FILE", "--scale-evidence", str(p), intr_source="LIVE")
    assert code == cli.EXIT_OK
    assert rec["input_provenance"]["is_upper_bound"] is True and rec["upper_bound_inputs"] == {"scale_evidence": "SIM"}
    assert check_input_provenance(rec["input_provenance"]) == []  # 출처와 상한 표시가 맞는다
    assert rec["input_provenance"]["calibration"]["source"] == "SIM"
    assert load_world_alignment(out).record["input_provenance"]["is_upper_bound"] is True  # -214 가 읽는다
    assert rec["calibration"]["provider_source"] == "SIM"  # 상한 근거가 섞이면 G1_AUTO 로 두지 않는다


def test_sim_intrinsics_record_is_not_laundered_to_g1_auto(tmp_path):
    """영상은 FILE 이어도 내부 파라미터 기록이 SIM 이면 결과 전체가 상한이고 -214 로 넘어가도 유지된다."""
    sim = make_run(tmp_path)
    code, rec, out = _fix(tmp_path, sim, "--frames-source", "FILE", intr_source="SIM")
    assert code == cli.EXIT_OK
    prov = rec["input_provenance"]
    assert prov["is_upper_bound"] is True and prov["calibration"]["source"] == "SIM" and prov["frames"]["source"] == "FILE"
    assert rec["upper_bound_inputs"] == {"intrinsics": "SIM"}
    assert check_input_provenance(prov) == [] and check_calibration(rec["calibration"]) == []
    assert rec["calibration"]["provider_source"] == "SIM"
    align = load_world_alignment(out)
    assert align.record["input_provenance"] == prov


def test_sim_foot_points_also_mark_upper_bound(tmp_path):
    sim = make_run(tmp_path)
    rng = np.random.default_rng(1)
    pts = np.column_stack([rng.uniform(1, 7, 30), rng.uniform(1, 5.5, 30), np.zeros(30)])
    ma = (sim.world_to_ma[:3, :3] @ pts.T).T + sim.world_to_ma[:3, 3]
    p = tmp_path / "feet.json"
    p.write_text(json.dumps({"format": "simai.foot_contacts", "points_mapanything": ma.tolist(),
                             "input_provenance": {"frames": {"source": "SIM"}, "is_upper_bound": True}}), encoding="utf-8")
    code, rec, out = _fix(tmp_path, sim, "--frames-source", "LIVE", "--foot-points", str(p), intr_source="LIVE")
    assert code == cli.EXIT_OK and rec["evidence"]["floor"]["foot_check"]["status"] == "CONSISTENT"
    assert rec["upper_bound_inputs"] == {"foot_points": "SIM"} and rec["calibration"]["provider_source"] == "SIM"
    load_world_alignment(out)


def test_mixed_sign_foot_offsets_block_the_cli(tmp_path):
    """발 절반은 10 cm 아래, 절반은 10 cm 위: 부호 중앙값은 0 이지만 절대 거리 중앙값 10 cm → 고정하지 않는다."""
    sim = make_run(tmp_path)
    rng = np.random.default_rng(2)
    z = np.r_[np.full(15, -0.10), np.full(15, 0.10)]
    pts = np.column_stack([rng.uniform(1, 7, 30), rng.uniform(1, 5.5, 30), z])
    ma = (sim.world_to_ma[:3, :3] @ pts.T).T + sim.world_to_ma[:3, 3]
    p = tmp_path / "feet.json"
    p.write_text(json.dumps({"format": "simai.foot_contacts", "points_mapanything": ma.tolist()}), encoding="utf-8")
    code, rec, _ = _fix(tmp_path, sim, "--frames-source", "SIM", "--foot-points", str(p))
    assert code == cli.EXIT_NOT_FIXED and rec["reason"] == "FLOOR_FOOT_MISMATCH"
    assert "mapanything_to_world" not in rec and "calibration" not in rec
    check = rec["evidence"]["floor"]["foot_check"]
    assert abs(check["signed_median_distance_ma"]) < 0.02 and check["median_abs_distance_ma"] > 0.09


def test_inputs_from_other_runs_are_refused(tmp_path, capsys):
    pytest.importorskip("cv2")
    sim = make_run(tmp_path, masks=True)
    prep = json.loads((sim.prepared_dir / "prepared.json").read_text(encoding="utf-8"))
    # 1) 입력 폴더를 만든 내부 파라미터 기록과 다르다
    prep["intrinsics"] = {"path": "other.json", "sha256": "sha256:" + "0" * 64}
    (sim.prepared_dir / "prepared.json").write_text(json.dumps(prep), encoding="utf-8")
    code, rec, _ = _fix(tmp_path, sim, "--prepared", str(sim.prepared_dir), out=tmp_path / "a.json")
    assert code == cli.EXIT_CONFIG and rec is None and "내부 파라미터" in capsys.readouterr().err
    # 2) MapAnything 이 다른 prepared.json 으로 돌았다(metrics.json 의 prepared_sha256)
    prep.pop("intrinsics")
    (sim.prepared_dir / "prepared.json").write_text(json.dumps(prep), encoding="utf-8")
    (sim.run_dir / "metrics.json").write_text(json.dumps({"run_id": "x", "args": {"mode": "prepared"},
                                                          "prepared_sha256": "sha256:" + "1" * 64}), encoding="utf-8")
    code, rec, _ = _fix(tmp_path, sim, "--prepared", str(sim.prepared_dir), out=tmp_path / "b.json")
    assert code == cli.EXIT_CONFIG and rec is None and "다른 입력 폴더" in capsys.readouterr().err


def test_prepared_run_requires_its_prepared_folder(tmp_path, capsys):
    """prepared 모드 실행 결과를 --prepared 없이 돌리면 마스크·출처(SIM)·내부 파라미터 확인이 빠진다 → 거절."""
    pytest.importorskip("cv2")
    sim = make_run(tmp_path, masks=True)
    code, rec, _ = _fix(tmp_path, sim, "--frames-source", "FILE", intr_source="LIVE", out=tmp_path / "a.json")
    assert code == cli.EXIT_CONFIG and rec is None and "prepared 모드" in capsys.readouterr().err
    # metrics.json 의 mode 만 있어도(prepared_sha256 없음) 같다
    (sim.run_dir / "metrics.json").write_text(json.dumps({"run_id": "x", "args": {"mode": "prepared"}}), encoding="utf-8")
    code, rec, _ = _fix(tmp_path, sim, "--frames-source", "FILE", intr_source="LIVE", out=tmp_path / "b.json")
    assert code == cli.EXIT_CONFIG and rec is None


@pytest.mark.parametrize("metrics", [None, {"run_id": "x", "args": {"mode": "images"}, "prepared_sha256": None}])
def test_prepared_folder_on_images_run_is_refused(tmp_path, capsys, metrics):
    """왜곡을 펴지 않은 images 모드 결과(또는 실행 기록 없음)에 --prepared 를 붙이면 마스크 좌표·K 가 어긋난다 → 거절."""
    pytest.importorskip("cv2")
    sim = make_run(tmp_path, masks=True)
    (sim.run_dir / "metrics.json").unlink()
    if metrics is not None:
        (sim.run_dir / "metrics.json").write_text(json.dumps(metrics), encoding="utf-8")
    code, rec, _ = _fix(tmp_path, sim, "--prepared", str(sim.prepared_dir))
    assert code == cli.EXIT_CONFIG and rec is None and "prepared_sha256" in capsys.readouterr().err
