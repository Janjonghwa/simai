"""출력 K 검사(intrinsics_check.py, S15P31A101-312): 합성 K 로 모드별 기대 K, 허용치 판정, 가져온 Run 다시 보기(GPU·torch 없음).

기대 K 정답값은 map-anything@3d10cf7 `crop_resize_if_necessary` 를 CPU 로 돌려 얻은 값이다(펴진 2304×1296, fx 1161,
주점 가운데). 반례: 정상 Run 의 출력 K 흔들림(2%)은 OK, 4차 세션처럼 세로만 틀어진 출력(fy/fx 0.57)은 MISMATCH,
라이브러리가 모델에 넘긴 K 가 틀어진 경우는 input 으로 갈라 보인다, K 를 넘기지 않은 Run 은 OK 로 채우지 않는다(SKIPPED).
"""

from __future__ import annotations

import ast
import importlib.util
import json
from pathlib import Path

import numpy as np
import pytest

STATIC = Path(__file__).resolve().parents[3] / "experiments" / "static_scene"
SCRIPT = STATIC / "intrinsics_check.py"
K0 = np.array([[1161.0, 0, 1151.5], [0, 1161.0, 647.5], [0, 0, 1]])
SRC = (2304, 1296)


@pytest.fixture(scope="module")
def ic():
    spec = importlib.util.spec_from_file_location("static_scene_intrinsics_check", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# 모드 → 모델 입력(W, H)·라이브러리 기대 K(fx, cx, cy). r518 fixed_mapping, r1036·r1554 longest_side, rfull fixed_size(14 배수 내림)
LIBRARY = {
    "r518": ((518, 294), (263.375, 258.5, 146.5)),
    "r1036": ((1036, 588), (526.75, 518.0, 293.5)),
    "r1554": ((1554, 868), (783.07, 776.5, 433.5)),
    "rfull": ((2296, 1288), (1156.969, 1147.5, 643.0)),
}


@pytest.mark.parametrize("key", list(LIBRARY))
def test_expected_crop_resize_k_matches_library_per_mode(ic, key):
    dst, (fx, cx, cy) = LIBRARY[key]
    K = ic.expected_crop_resize_k(K0, SRC, dst)
    assert abs(K[0, 0] - fx) < 0.01 and K[0, 0] == K[1, 1]  # 덮는 배율 하나(등방): fy/fx 는 1 그대로
    assert abs(K[0, 2] - cx) < 0.6 and abs(K[1, 2] - cy) < 0.6  # 가운데 자르기 반 픽셀 반올림 차이까지
    assert abs(K[0, 2] - (dst[0] - 1) / 2) < 0.6 and abs(K[1, 2] - (dst[1] - 1) / 2) < 0.6  # 가운데 주점은 가운데로


def test_expected_axis_resize_k_matches_da3_record(ic):
    """DA3 h3(1008): 1008×574 로 축마다 늘인 기록값 fx 508.0, fy 514.2 (4차 세션 depth.json)."""
    K = ic.expected_axis_resize_k(K0, SRC, (1008, 574))
    assert abs(K[0, 0] - 508.0) < 0.1 and abs(K[1, 1] - 514.2) < 0.1
    assert abs(K[0, 2] - 503.5) < 0.05 and abs(K[1, 2] - 286.5) < 0.05


def _view(ic, name, out_scale=(1.0, 1.0), pp=(0.0, 0.0), inp=None, dst=(1036, 588)):
    exp = ic.expected_crop_resize_k(K0, SRC, dst)
    out = exp.copy()
    out[0, 0] *= out_scale[0]
    out[1, 1] *= out_scale[1]
    out[0, 2] += pp[0]
    out[1, 2] += pp[1]
    return {"name": name, "expected": exp, "output": out, "input": inp}


def test_check_ok_for_normal_drift_and_mismatch_for_vertical_squash(ic):
    ok = ic.check([_view(ic, "cam_A", (1.021, 1.018)), _view(ic, "cam_B", (0.99, 0.991), (1.0, -1.0))])
    assert ok["status"] == "OK" and ok["max_rel_err"] == pytest.approx(0.021, abs=1e-4) and ic.warning(ok) is None
    # 4차 세션 r1036 cam_A 와 같은 모양: fx +22.5%, fy −30.1%
    bad = ic.check([_view(ic, "cam_A", (1.225, 0.699)), _view(ic, "cam_B", (1.0, 1.0))])
    assert bad["status"] == "MISMATCH" and bad["worst"] == "cam_A output fy"
    assert bad["views"][0]["fy_over_fx"] == pytest.approx(0.699 / 1.225, abs=1e-3)
    assert "30.1%" in ic.warning(bad) and "S15P31A101-312" in ic.warning(bad)
    pp = ic.check([_view(ic, "cam_A", pp=(0.0, 0.06 * 293.5))])  # 주점만 6% 밀려도 잡는다
    assert pp["status"] == "MISMATCH" and pp["worst"] == "cam_A output cy"


def test_check_separates_preprocess_input_from_model_output(ic):
    """라이브러리가 모델에 넘긴 K 가 기대와 다르면(전처리 쪽 원인) input 으로 표시된다. 같으면 출력만 본다."""
    exp = ic.expected_crop_resize_k(K0, SRC, (1036, 588))
    squashed = exp.copy()
    squashed[1, 1] *= 0.5625  # 세로 배율을 가로로 잘못 옮긴 전처리
    rec = ic.check([_view(ic, "cam_A", inp=squashed)])
    assert rec["status"] == "MISMATCH" and rec["worst"] == "cam_A input fy" and rec["views"][0]["max_rel"] == 0
    same = ic.check([_view(ic, "cam_A", inp=exp)])
    assert same["status"] == "OK" and same["views"][0]["input_max_rel"] == 0


def test_check_without_given_k_is_skipped_not_ok(ic):
    rec = ic.check([])
    assert rec["status"] == "SKIPPED" and ic.warning(rec) is None


def test_tolerance_is_between_normal_and_broken_runs(ic):
    """허용치 근거: 정상 최대 2.1%(로비 r518), 틀어진 최소 29.7%(교실 r1036 fy)."""
    assert 0.021 * 2 < ic.TOL_REL < 0.297 / 5


def _write_run(root: Path, name: str, fy_scale: float, dst=(1036, 588)) -> tuple[Path, Path]:
    prep, run = root / "prepared", root / name
    prep.mkdir(exist_ok=True)
    run.mkdir()
    views = [{"name": n, "image": f"{n}.jpg", "image_size": list(SRC), "K_undistorted": K0.tolist()} for n in ("cam_A", "cam_B")]
    (prep / "prepared.json").write_text(json.dumps({"format": "simai.mapanything_inputs", "views": views}), encoding="utf-8")
    raw = {"names": np.array(["cam_A", "cam_B"])}
    for n in ("cam_A", "cam_B"):
        s = max(dst[0] / SRC[0], dst[1] / SRC[1])
        K = np.array([[1161.0 * s, 0, (dst[0] - 1) / 2], [0, 1161.0 * s * fy_scale, (dst[1] - 1) / 2], [0, 0, 1]])
        raw |= {f"{n}__intrinsics": K, f"{n}__depth_z": np.ones((dst[1], dst[0]), np.float32)}
    np.savez_compressed(run / "raw.npz", **raw)
    return run, prep


def test_check_fetched_run_and_cli_exit_code(ic, tmp_path, capsys):
    good, prep = _write_run(tmp_path, "r518", 1.0, (518, 294))
    bad, _ = _write_run(tmp_path, "r1036", 0.57)
    assert ic.check_mapanything_run(good, prep)["status"] == "OK"
    rec = ic.check_mapanything_run(bad, prep)
    assert rec["status"] == "MISMATCH" and rec["max_rel_err"] == pytest.approx(0.43, abs=1e-3)
    assert ic.main([str(good), "--prepared", str(prep)]) == 0
    assert ic.main([str(good), str(bad), "--prepared", str(prep)]) == 1
    assert "MISMATCH" in capsys.readouterr().out


def test_module_imports_only_stdlib_and_numpy():
    """DA3 전용 가상환경(simai_ai·torch 없음)의 predict 에서도 불린다."""
    names = set()
    library = STATIC.parents[1] / "src" / "simai_ai" / "scene" / "intrinsics_check.py"
    for node in ast.parse(library.read_text(encoding="utf-8")).body:
        if isinstance(node, ast.Import):
            names |= {a.name.split(".")[0] for a in node.names}
        elif isinstance(node, ast.ImportFrom):
            names.add((node.module or "").split(".")[0])
    assert names <= {"__future__", "argparse", "json", "sys", "pathlib", "numpy"}


def test_compare_resolutions_shows_k_check(tmp_path):
    spec = importlib.util.spec_from_file_location("static_scene_compare_resolutions", STATIC / "compare_resolutions.py")
    cr = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(cr)
    run = tmp_path / "r1036"
    run.mkdir()
    (run / "metrics.json").write_text(json.dumps({"result": "SUCCESS", "model_input_hw": [588, 1036],
                                                  "intrinsics_check": {"status": "MISMATCH", "max_rel_err": 0.319}}),
                                      encoding="utf-8")
    old = tmp_path / "r518"
    old.mkdir()
    (old / "metrics.json").write_text(json.dumps({"result": "SUCCESS", "model_input_hw": [294, 518]}), encoding="utf-8")
    assert cr.row(run, 2304)["k_check"] == "MISMATCH 31.9%"
    assert cr.row(old, 2304)["k_check"] is None  # 검사 전 Run 은 비운다
    assert "출력 K 검사" in cr.table([cr.row(run, 2304)])
