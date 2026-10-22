"""HMR 입력 정리 (S15P31A101-238): 알려진 K 우선순위·출처 기록·왜곡 처리, E-GATE-0 대조 판정. GPU·torch·cv2 를 쓰지 않는다.

반례: 넘긴 순서가 바뀌어도 보정 K 가 데이터셋 K 보다 먼저, 크기가 다른 보정 K 는 늘리거나 줄이지 않고 다음 후보(이유 기록),
FAILED·카메라 모호·핀홀 아닌 K·어안 왜곡은 쓰지 않음, 아무 K 도 못 쓰면 MoGe 추정이고 보정 출처를 적지 않음,
왜곡이 있으면 원본 화소를 모두 담는 새 K(alpha 1)로 펴고 없으면 cv2 를 부르지 않음, split 모드에 K 파일을 주면 실행 전에 거부,
지연 판정은 (GPU, torch) 기대값 표로 하고 표에 없으면 기록만, 구조가 다르면 지연이 맞아도 HOLD, 처음 보는 모양·인원 수의 첫 실행은 지연에서 뺌.
"""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
import types
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[4]
HMR = REPO / "ai" / "experiments" / "hmr"
K_C110 = [[1257.83, 0.0, 1150.91], [0.0, 1257.83, 647.77], [0.0, 0.0, 1.0]]
D_C110 = {"model": "OPENCV_PINHOLE", "coeffs": [-0.2722, 0.0459, 0.0, 0.0, 0.0]}
SIZE = [2304, 1296]


@pytest.fixture
def c(tmp_path, monkeypatch):
    monkeypatch.setenv("SIMAI_ROOT", str(tmp_path))
    monkeypatch.syspath_prepend(str(HMR))
    sys.modules.pop("fs3db_common", None)
    import fs3db_common
    return fs3db_common


@pytest.fixture
def cmp():
    spec = importlib.util.spec_from_file_location("hmr_compare_egate0", HMR / "compare_egate0.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _write(path: Path, doc: dict) -> Path:
    path.write_text(json.dumps(doc), encoding="utf-8")
    return path


def _intrinsics_record(tmp_path: Path, **over) -> Path:
    """calibration.intrinsics.cli 기록(simai.intrinsics_estimate 0.2.0) 모양. 교실 C110 4대 중 2대."""
    est = [{"camera_id": cam, "status": "OK", "method": "ANYCALIB", "image_size": SIZE,
            "camera": {"camera_id": cam, "image_size": SIZE, "K": K_C110, "distortion": D_C110}} for cam in ("cam_A", "cam_B")]
    est[1]["status"] = over.pop("cam_b_status", "OK")
    return _write(tmp_path / "intrinsics.json", {"format": "simai.intrinsics_estimate", "format_version": "0.2.0", "estimates": est, **over})


def _dataset_k(tmp_path: Path, size=SIZE, K=None) -> Path:
    return _write(tmp_path / "dataset_k.json", {"dataset_id": "cmu_panoptic/171204_pose1",
                                                "cameras": [{"camera_id": "hd_00_00", "image_size": size, "K": K or [[1400.0, 0, 960], [0, 1400.0, 540], [0, 0, 1]],
                                                             "distortion": {"model": "OPENCV_PINHOLE", "coeffs": [0.0, 0.0, 0.0, 0.0, 0.0]}}]})


# ---------------------------------------------------------------- 우선순위·출처


def test_calibration_beats_dataset_whatever_the_order(c, tmp_path):
    cal, ds = _intrinsics_record(tmp_path), _dataset_k(tmp_path)
    for cands in ([(c.K_CALIBRATION, cal), (c.K_DATASET, ds)], [(c.K_DATASET, ds), (c.K_CALIBRATION, cal)]):
        k = c.resolve_k(SIZE, cands, camera_id="cam_A")
        assert k["source"] == c.K_CALIBRATION and k["camera_id"] == "cam_A" and k["skipped"] == []
        # AnyCalib 1단계 초기값: 무마커 자동 보정 출처, 번들 조정 전이라 PROVISIONAL. 파일 해시로 어떤 보정인지 남긴다
        assert k["calibration_source"] == "G1_AUTO" and k["calibration_state"] == "PROVISIONAL"
        assert k["file"] == "intrinsics.json" and k["file_sha256"].startswith("sha256:") and k["K_given"] == K_C110


def test_wrong_size_calibration_is_not_rescaled_but_skipped_with_reason(c, tmp_path):
    cal = _intrinsics_record(tmp_path)
    ds = _dataset_k(tmp_path, size=[1152, 648], K=[[628.9, 0, 575.5], [0, 628.9, 323.9], [0, 0, 1]])
    k = c.resolve_k([1152, 648], [(c.K_CALIBRATION, cal), (c.K_DATASET, ds)], camera_id=None, stem="cam_A_01")
    assert k["source"] == c.K_DATASET and k["calibration_source"] == "DATASET_GT" and k["dataset_id"].startswith("cmu_panoptic")
    assert k["skipped"][0]["source"] == c.K_CALIBRATION and "image_size" in k["skipped"][0]["why"]


def test_nothing_usable_falls_back_to_moge_without_calibration_fields(c, tmp_path):
    cal = _intrinsics_record(tmp_path, cam_b_status="FAILED")
    k = c.resolve_k(SIZE, [(c.K_CALIBRATION, cal)], camera_id="cam_B")
    assert k["source"] == c.K_MOGE and "status FAILED" in k["skipped"][0]["why"]
    # 모델이 영상만 보고 추정한 K 다: 보정 출처·상태를 적지 않는다(input_provenance.calibration 생략 규칙과 같다)
    assert not {"calibration_source", "calibration_state", "K_given"} & set(k)
    assert c.resolve_k(SIZE, [])["source"] == c.K_MOGE


@pytest.mark.parametrize("stem, camera_id, want", [
    ("cam_B_03", None, "cam_B"),        # 파일 이름 접두로 고른다
    ("cam_A_01", "cam_B", "cam_B"),     # 명시한 camera_id 가 이름보다 먼저
    ("cam_AB_01", None, None),          # 'cam_A' 로 시작하지만 'cam_A_' 는 아니다
    ("frame_0001", None, None),         # 정할 수 없으면 쓰지 않는다
    ("cam_A_01", "cam_Z", None),
])
def test_camera_pick(c, tmp_path, stem, camera_id, want):
    k = c.resolve_k(SIZE, [(c.K_CALIBRATION, _intrinsics_record(tmp_path))], camera_id=camera_id, stem=stem)
    assert (k.get("camera_id") if k["source"] == c.K_CALIBRATION else None) == want


def test_calibration_contract_document_keeps_its_own_state(c, tmp_path):
    doc = json.loads((REPO / "contracts" / "examples" / "calibration.g1_auto.json").read_text(encoding="utf-8"))
    cam = doc["cameras"][0]
    k = c.resolve_k(cam["image_size"], [(c.K_CALIBRATION, _write(tmp_path / "cal.json", doc))], camera_id=cam["camera_id"])
    assert k["source"] == c.K_CALIBRATION
    assert (k["calibration_source"], k["calibration_state"], k["calibration_version"]) == (doc["provider_source"], doc["state"], doc["calibration_version"])


@pytest.mark.parametrize("cam, why", [
    ({"K": [[0.0, 0, 960], [0, 1400.0, 540], [0, 0, 1]]}, "핀홀"),
    ({"K": [[float("nan"), 0, 960], [0, 1400.0, 540], [0, 0, 1]]}, "핀홀"),
    ({"K": [[1400.0, 0, 960], [0, 1400.0, 540], [0, 0, 2]]}, "핀홀"),
    ({"distortion": {"model": "OPENCV_FISHEYE", "coeffs": [0.1, 0, 0, 0]}}, "왜곡"),
    ({"K": [[1400.0, 0], [0, 1400.0]]}, "형식"),
])
def test_bad_known_k_is_not_used(c, tmp_path, cam, why):
    base = {"camera_id": "x", "image_size": [1920, 1080], "K": [[1400.0, 0, 960], [0, 1400.0, 540], [0, 0, 1]],
            "distortion": {"model": "NONE", "coeffs": []}}
    k = c.resolve_k([1920, 1080], [(c.K_DATASET, _write(tmp_path / "k.json", {**base, **cam}))])
    assert k["source"] == c.K_MOGE and why in k["skipped"][0]["why"]


def test_unreadable_file_is_skipped_not_crash(c, tmp_path):
    (tmp_path / "broken.json").write_text("{", encoding="utf-8")
    k = c.resolve_k(SIZE, [(c.K_CALIBRATION, tmp_path / "broken.json"), (c.K_DATASET, tmp_path / "missing.json")])
    assert k["source"] == c.K_MOGE and [s["source"] for s in k["skipped"]] == [c.K_CALIBRATION, c.K_DATASET]


# ---------------------------------------------------------------- 왜곡: 펴서 넣음


def _fake_cv2(calls, scale=0.6):
    fake = types.ModuleType("cv2")

    def new_k(K, D, size, alpha, new_size, centerPrincipalPoint=False):
        calls.append(("newK", alpha, tuple(size), tuple(new_size), centerPrincipalPoint))
        K1 = K.copy()
        K1[0, 0] *= scale
        K1[1, 1] *= scale
        return K1, (0, 0, size[0], size[1])
    fake.getOptimalNewCameraMatrix = new_k
    fake.undistort = lambda img, K, D, R, newK: calls.append(("undistort", K.tolist(), D.tolist(), R, newK.tolist())) or "펴진 그림"
    return fake


def test_distortion_is_undistorted_keeping_all_source_pixels(c, tmp_path, monkeypatch):
    calls = []
    monkeypatch.setitem(sys.modules, "cv2", _fake_cv2(calls))
    k = c.resolve_k(SIZE, [(c.K_CALIBRATION, _intrinsics_record(tmp_path))], camera_id="cam_A")
    img = types.SimpleNamespace(shape=(SIZE[1], SIZE[0], 3))
    out, K = c.prepare_image(img, k)
    # 새 K = alpha 1(원본 화소를 모두 담음), 같은 크기, 주점 중앙. 같은 K 로 펴면 교실 사진에서 가장자리 사람이 빠진다
    assert calls[0] == ("newK", 1, tuple(SIZE), tuple(SIZE), True)
    assert out == "펴진 그림" and calls[1][1] == K_C110 and calls[1][2] == D_C110["coeffs"] and calls[1][4] == K.tolist()
    assert K[0, 0] == pytest.approx(K_C110[0][0] * 0.6)
    assert k["undistorted"] is True and k["K_used"] == K.tolist() and k["K_given"] == K_C110 and k["image_space"] == "UNDISTORTED_ALPHA1"


def test_bad_new_k_fails_the_image_instead_of_guessing(c, tmp_path, monkeypatch):
    monkeypatch.setitem(sys.modules, "cv2", _fake_cv2([], scale=float("nan")))
    k = c.resolve_k(SIZE, [(c.K_CALIBRATION, _intrinsics_record(tmp_path))], camera_id="cam_A")
    with pytest.raises(ValueError, match="새 K"):
        c.prepare_image(types.SimpleNamespace(shape=(SIZE[1], SIZE[0], 3)), k)
    assert "K_used" not in k


def test_zero_distortion_and_moge_do_not_touch_cv2(c, tmp_path, monkeypatch):
    monkeypatch.setitem(sys.modules, "cv2", None)  # import 하면 실패한다
    k = c.resolve_k([1920, 1080], [(c.K_DATASET, _dataset_k(tmp_path, size=[1920, 1080]))])
    img, K = c.prepare_image("원본", k)
    assert img == "원본" and k["undistorted"] is False and K[0, 0] == 1400.0
    moge = c.resolve_k([1920, 1080], [])
    assert c.prepare_image("원본", moge) == ("원본", None) and "K_used" not in moge


def test_k_summary_counts_and_mixed(c):
    assert c.k_summary([{"source": "CALIBRATION", "undistorted": True}] * 2) == {"source": "CALIBRATION", "count": {"CALIBRATION": 2}, "undistorted": 2}
    mixed = c.k_summary([{"source": "CALIBRATION"}, {"source": "MOGE_ESTIMATE"}])
    assert mixed["source"] == "MIXED" and mixed["count"] == {"CALIBRATION": 1, "MOGE_ESTIMATE": 1}
    assert c.k_summary([])["source"] is None


def test_env_summary_records_comparison_keys_without_gpu(c, monkeypatch):
    fake = types.SimpleNamespace(__version__="2.7.1+cu128", version=types.SimpleNamespace(cuda="12.8"),
                                 cuda=types.SimpleNamespace(is_available=lambda: False),
                                 backends=types.SimpleNamespace(cudnn=types.SimpleNamespace(version=lambda: 90701)))
    monkeypatch.setitem(sys.modules, "torch", fake)
    monkeypatch.setenv("SIMAI_IMAGE_DIGEST", "sha256:" + "d" * 64)
    d = c.env_summary()
    assert d["cudnn"] == 90701 and d["image_digest"].endswith("d" * 8) and d["cpu_count"] >= 1


def test_split_mode_refuses_known_k_before_loading_anything(tmp_path):
    r = subprocess.run([sys.executable, str(HMR / "run_image.py"), "--run-id", "x", "--images", str(tmp_path), "--mode", "split",
                        "--calibration", str(tmp_path / "k.json")], capture_output=True, text=True, encoding="utf-8", errors="replace")
    assert r.returncode == 2 and "official" in r.stderr


# ---------------------------------------------------------------- E-GATE-0 대조 판정


def _img(name, size, n, ms, peak=4046.7):
    return {"image": name, "size": size, "n_people": n, "result": "SUCCESS", "finite": True, "process_one_image_ms": ms, "peak_alloc_mb": peak}


def _h1(torch_v, scale, gpu="NVIDIA H100 80GB HBM3"):
    """E-GATE-0 H1 순서를 줄인 모양: 첫 장(적재), 새 크기, 같은 크기(warm), 다인원 5명 첫 실행, 4명 첫 실행, 4명 다시(warm)."""
    rows = [_img("a1", "576x1024", 1, 9816), _img("b1", "610x840", 1, 551), _img("a2", "576x1024", 1, 321 * scale),
            _img("c1", "544x960", 1, 288), _img("c2", "544x960", 1, 330 * scale), _img("c3", "544x960", 1, 339 * scale),
            _img("m5", "768x432", 5, 2707, 4828.1), _img("m4", "768x432", 4, 2904, 4629.7), _img("m4b", "768x432", 4, 372 * scale, 4629.7)]
    load = [{"component": c, "result": "OK"} for c in ("hmr_dinov3", "detector_yolo_pose", "fov_moge2", "estimator_init(warmup)")]
    return {"env": {"gpu": gpu, "torch": torch_v}, "images": rows, "load": {"load": load}}


def test_warm_rows_skip_first_of_each_shape_and_people_count(cmp):
    warm = [r["image"] for r in cmp.warm_rows(_h1("x", 1.0)["images"])]
    # 4명 첫 실행(m4, 2.9 s)은 '첫 장·첫 다인원 제외' 옛 규칙으로는 남아 중앙값을 끌어올렸다
    assert warm == ["a2", "c2", "c3", "m4b"]


def test_same_gpu_new_image_judged_against_its_own_expected_value(cmp, monkeypatch):
    monkeypatch.setitem(cmp.EXPECTED, ("NVIDIA H100 80GB HBM3", "2.5.1+cu124"), {**cmp.EXPECTED[("NVIDIA H100 80GB HBM3", "2.5.1+cu124")], "h1_warm_p50_ms": 334.5})
    monkeypatch.setitem(cmp.EXPECTED, ("NVIDIA H100 80GB HBM3", "2.7.1+cu128"), {**cmp.EXPECTED[("NVIDIA H100 80GB HBM3", "2.7.1+cu128")], "h1_warm_p50_ms": 224.1})
    base = _h1("2.5.1+cu124", 1.0)
    fast = _h1("2.7.1+cu128", 0.67)   # 2차 세션처럼 0.67배: 새 판의 기대값으로는 통과
    assert cmp.compare(base, fast)["verdict"] == "PASS"
    # 같은 빠른 결과를 옛 판 기대값으로 보면 HOLD(이번 HOLD 의 원인: 판이 다른데 E-GATE-0 값 하나로 봤다)
    fast["env"]["torch"] = "2.5.1+cu124"
    res = cmp.compare(base, fast)
    assert res["verdict"] == "HOLD" and res["latency"]["status"] == "HOLD"
    assert cmp.compare(base, _h1("2.7.1+cu128", 1.3))["verdict"] == "HOLD"  # 같은 판에서 느려짐


def test_unknown_gpu_is_record_only_but_structure_still_required(cmp):
    base = _h1("2.5.1+cu124", 1.0)
    other = _h1("2.7.1+cu128", 0.4, gpu="NVIDIA B200")
    res = cmp.compare(base, other)
    assert res["verdict"] == "PASS_STRUCTURE_ONLY" and res["latency"]["status"] == "RECORD_ONLY"
    other["images"][6]["n_people"] = 4  # 5명 장면에서 4명
    assert cmp.compare(base, other)["verdict"] == "HOLD"


def test_structure_mismatch_is_hold_even_if_latency_matches(cmp):
    base = _h1("2.5.1+cu124", 1.0)
    for broken in ({"result": "NAN_OUTPUT"}, {"finite": False}, {"n_people": 2}):
        new = _h1("2.5.1+cu124", 1.0)
        new["images"][2].update(broken)
        assert cmp.compare(base, new)["verdict"] == "HOLD", broken
    new = _h1("2.5.1+cu124", 1.0)
    new["images"].pop()  # 그림이 빠짐
    assert cmp.compare(base, new)["verdict"] == "HOLD"
    assert cmp.compare(base, {"env": base["env"], "images": [], "load": base["load"]})["verdict"] == "HOLD"
    new = _h1("2.5.1+cu124", 1.0)
    new["load"]["load"][2]["result"] = "OOM_AT_LOAD"  # 적재 실패
    assert cmp.compare(base, new)["verdict"] == "HOLD"
    new.pop("load")  # 적재 기록이 없으면 통과로 두지 않는다
    assert cmp.compare(base, new)["verdict"] == "HOLD"


def test_missing_or_grown_peak_alloc_is_hold(cmp):
    base = _h1("2.5.1+cu124", 1.0)
    new = _h1("2.5.1+cu124", 1.0)
    for r in new["images"]:
        r.pop("peak_alloc_mb")
    assert cmp.compare(base, new)["verdict"] == "HOLD"  # 모르면 통과로 두지 않는다
    new = _h1("2.5.1+cu124", 1.0)
    new["images"][6]["peak_alloc_mb"] = 4828.1 * 1.11
    assert cmp.compare(base, new)["verdict"] == "HOLD"


def test_too_few_warm_images_is_record_only(cmp):
    load = {"load": [{"component": "hmr_dinov3", "result": "OK"}]}
    base = {"images": [_img("t1", "544x960", 1, 20157)], "load": load}
    new = {"env": {"gpu": "NVIDIA H100 80GB HBM3", "torch": "2.7.1+cu128"}, "images": [_img("t1", "544x960", 1, 4064)], "load": load}
    res = cmp.compare(base, new)
    assert res["verdict"] == "PASS_STRUCTURE_ONLY" and "warm" in res["latency"]["why"]


def _det(torch_v, p50s, people=5):
    rows = [{"imgsz": s, "batch": b, "people_detected": people, "ms": {"p50": v}} for (s, b), v in zip(((640, 1), (640, 4), (1280, 1), (1280, 4)), p50s)]
    return {"env": {"gpu": "NVIDIA H100 80GB HBM3", "torch": torch_v}, "rows": rows}


def test_h6_rows_use_per_version_expected_and_people_count(cmp):
    base = _det("2.5.1+cu124", [15.38, 21.47, 18.40, 38.15])
    assert cmp.compare(base, _det("2.7.1+cu128", [8.9, 12.5, 10.0, 31.0]))["verdict"] == "PASS"
    assert cmp.compare(base, _det("2.5.1+cu124", [8.9, 12.5, 10.0, 31.0]))["verdict"] == "HOLD"
    assert cmp.compare(base, _det("2.7.1+cu128", [8.9, 12.5, 10.0, 31.0], people=4))["verdict"] == "HOLD"
    missing = _det("2.7.1+cu128", [8.9, 12.5, 10.0, 31.0])
    missing["rows"][0]["ms"] = {}
    assert cmp.compare(base, missing)["verdict"] == "HOLD"


def test_cli_exit_codes(cmp, tmp_path, capsys):
    b = _write(tmp_path / "b.json", _h1("2.5.1+cu124", 1.0))
    assert cmp.main(["--base", str(b), "--new", str(b)]) == 0
    assert cmp.main(["--base", str(b), "--new", str(_write(tmp_path / "n.json", _h1("2.5.1+cu124", 2.0)))]) == 1
    assert cmp.main(["--base", str(b), "--new", str(tmp_path / "none.json")]) == 2
    capsys.readouterr()


# ---------------------------------------------------------------- 리뷰 수정 (M1·M3, R3·R4, 왕복)


def test_priority_picks_calibration_k_value_when_both_are_valid_for_same_camera(c, tmp_path):
    # 두 후보 모두 같은 카메라·크기로 쓸 수 있고 K 만 다르다: 고른 값이 보정 K 여야 한다(리뷰 R3)
    cal = _intrinsics_record(tmp_path)
    ds = _write(tmp_path / "ds_same.json", {"cameras": [{"camera_id": "cam_A", "image_size": SIZE, "K": [[999.0, 0, 1152], [0, 999.0, 648], [0, 0, 1]]}]})
    for cands in ([(c.K_CALIBRATION, cal), (c.K_DATASET, ds)], [(c.K_DATASET, ds), (c.K_CALIBRATION, cal)]):
        k = c.resolve_k(SIZE, cands, stem="cam_A_01")
        assert k["source"] == c.K_CALIBRATION and k["K_given"] == K_C110 and k["skipped"] == []


@pytest.mark.parametrize("doc, why", [
    ({"cameras": [1, 2]}, "객체 목록"),
    ({"cameras": 42}, "객체 목록"),
    ({"cameras": []}, "객체 목록"),
    ([1, 2], "객체"),
    ({"cameras": [{"camera_id": "cam_A", "image_size": SIZE, "K": K_C110, "distortion": {"model": "OPENCV_PINHOLE", "coeffs": [0.1, 0.2]}}]}, "계수 2개"),
    ({"cameras": [{"camera_id": "cam_A", "image_size": SIZE, "K": K_C110, "distortion": {"model": "OPENCV_PINHOLE", "coeffs": [0.1] * 12}}]}, "계수 12개"),
    ({"cameras": [{"camera_id": "cam_A", "image_size": SIZE, "K": K_C110, "distortion": {"model": "NONE", "coeffs": [-0.2]}}]}, "NONE"),
    ({"cameras": [{"camera_id": "cam_A", "image_size": SIZE, "K": K_C110, "distortion": None}]}, "distortion"),
    ({"cameras": [{"camera_id": "cam_A", "image_size": SIZE, "K": K_C110, "distortion": {"model": "OPENCV_PINHOLE", "coeffs": "x"}}]}, "distortion"),
    ({"cameras": [{"camera_id": "cam_A", "image_size": 5, "K": K_C110}]}, "형식"),
    ({"cameras": [{"camera_id": "cam_A", "image_size": SIZE}]}, "형식"),
])
def test_malformed_calibration_is_skipped_with_reason_and_valid_dataset_k_is_used(c, tmp_path, doc, why):
    bad = _write(tmp_path / "bad.json", doc)
    ds = _write(tmp_path / "ds.json", {"cameras": [{"camera_id": "cam_A", "image_size": SIZE, "K": K_C110, "distortion": D_C110}]})
    k = c.resolve_k(SIZE, [(c.K_CALIBRATION, bad), (c.K_DATASET, ds)], stem="cam_A_01")
    assert k["source"] == c.K_DATASET and k["skipped"][0]["source"] == c.K_CALIBRATION and why in k["skipped"][0]["why"]
    assert c.resolve_k(SIZE, [(c.K_CALIBRATION, bad)], stem="cam_A_01")["source"] == c.K_MOGE  # 예외로 끊기지 않는다


@pytest.mark.parametrize("coeffs", [[-0.2722, 0.0459, 0.0, 0.0, 0.0], [-0.2722, 0.0459, 0.0012, -0.0008],
                                    [-0.2722, 0.0459, 0.0012, -0.0008, 0.004, 0.01, 0.002, 0.0005]])
def test_undistorted_points_map_back_to_original_pixels(c, coeffs):
    """원본 픽셀 → (geometry.distortion 로 펴기, 새 K) → 펴진 그림 픽셀 → to_original_pixels → 원본. 펴진 그림 안에 드는 원본 점만."""
    import numpy as np

    from simai_ai.geometry import distortion as gd

    K = np.asarray(K_C110)
    K1 = np.array([[750.0, 0, 1152.0], [0, 750.0, 648.0], [0, 0, 1]])  # alpha=1 처럼 초점이 줄고 주점이 중앙인 새 K
    kin = {"source": c.K_CALIBRATION, "K_given": K_C110, "distortion": {"model": "OPENCV_PINHOLE", "coeffs": coeffs}}
    kin.update(K_used=K1.tolist(), undistorted=True)  # prepare_image 는 cv2 가 필요해 그것이 남기는 기록만 같은 모양으로 채운다
    u, v = np.meshgrid(np.linspace(0, SIZE[0] - 1, 9), np.linspace(0, SIZE[1] - 1, 7))  # 모서리까지
    p_orig = np.column_stack([u.ravel(), v.ravel()])
    xd = (p_orig - K[:2, 2]) / [K[0, 0], K[1, 1]]
    xu, ok = gd.undistort(xd, "OPENCV_PINHOLE", coeffs)
    assert ok.all()
    p_und = xu * [K1[0, 0], K1[1, 1]] + K1[:2, 2]
    inside = (p_und[:, 0] >= 0) & (p_und[:, 0] < SIZE[0]) & (p_und[:, 1] >= 0) & (p_und[:, 1] < SIZE[1])
    assert inside.sum() >= 50
    back = c.to_original_pixels(p_und[inside], kin)
    assert np.abs(back - p_orig[inside]).max() < 1e-6
    # 같은 식인지: 정방향이 geometry.distortion 과 같다
    assert np.allclose(c._distort_normalized(xu, coeffs), gd.distort(xu, "OPENCV_PINHOLE", coeffs), atol=1e-12)
    # 펴지 않은 입력(왜곡 0·MoGe)은 그대로
    assert np.array_equal(c.to_original_pixels(p_orig, {"source": c.K_MOGE}), p_orig)


def test_k_summary_gives_run_record_provenance_only_for_one_known_file(c, tmp_path):
    cal = c.resolve_k(SIZE, [(c.K_CALIBRATION, _intrinsics_record(tmp_path))], camera_id="cam_A")
    s = c.k_summary([cal, dict(cal)])
    prov = s["input_provenance"]
    assert prov["calibration"]["source"] == "G1_AUTO" and prov["calibration"]["calibration_version"].startswith("intrinsics.json@sha256:")
    assert prov["is_upper_bound"] is False
    ds = c.resolve_k([1920, 1080], [(c.K_DATASET, _dataset_k(tmp_path, size=[1920, 1080]))])
    assert c.k_summary([ds])["input_provenance"]["is_upper_bound"] is True  # 데이터셋 정답 K 는 상한
    assert "input_provenance" not in c.k_summary([cal, c.resolve_k(SIZE, [])])  # 섞이면 비운다
    assert "input_provenance" not in c.k_summary([c.resolve_k(SIZE, [])])      # 모델 추정은 calibration 생략


@pytest.mark.parametrize("mutate, what", [
    (lambda ld: ld.pop(2), "빠짐"),                                    # MoGe 적재 기록이 없다
    (lambda ld: ld.__setitem__(1, {"component": "detector_vitdet", "result": "OK"}), "빠짐"),  # 이름 바뀜
    (lambda ld: ld.append(dict(ld[0])), "중복"),
    (lambda ld: ld[:] and ld.__delitem__(slice(1, None)), "빠짐"),      # 리뷰 재현: hmr 하나만 남김
])
def test_missing_renamed_or_duplicate_load_components_are_hold(cmp, mutate, what):
    base, new = _h1("2.5.1+cu124", 1.0), _h1("2.5.1+cu124", 1.0)
    mutate(new["load"]["load"])
    res = cmp.compare(base, new)
    assert res["verdict"] == "HOLD" and any(what in d.get("what", "") for d in res["structure"]["diffs"])
