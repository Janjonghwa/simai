"""MapAnything 입력 해상도 비교(-282): run_mapanything 해상도 인자, 해상도별 큐 항목, 비교 표(GPU·torch 없음)."""

from __future__ import annotations

import importlib.util
import json
import sys
import types
from pathlib import Path

import pytest

AI = Path(__file__).resolve().parents[3]
STATIC = AI / "experiments" / "static_scene"


def _load(name):
    spec = importlib.util.spec_from_file_location(f"static_scene_{name}", STATIC / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def ma(monkeypatch):
    monkeypatch.setitem(sys.modules, "torch", types.ModuleType("torch"))  # 노트북 시험 환경에는 torch 가 없을 수 있다
    return _load("run_mapanything")


def test_resize_kwargs_maps_to_official_arguments(ma):
    assert ma.resize_kwargs("fixed_mapping", None) == {}  # 공식 기본과 같다(인자 없음)
    assert ma.resize_kwargs("longest_side", "1036") == {"resize_mode": "longest_side", "size": 1036}
    assert ma.resize_kwargs("fixed_size", "2304x1296") == {"resize_mode": "fixed_size", "size": (2304, 1296)}
    assert ma.resize_kwargs("square", "518") == {"resize_mode": "square", "size": 518}


@pytest.mark.parametrize("mode, size", [("fixed_mapping", "1036"), ("longest_side", None), ("longest_side", "10x2"),
                                        ("fixed_size", "2304"), ("fixed_size", "ax1")])
def test_resize_kwargs_rejects_wrong_size(ma, mode, size):
    with pytest.raises(SystemExit):
        ma.resize_kwargs(mode, size)


def test_sweep_jobs_run_w1_once_per_resolution_and_default_stays_w1_w2():
    sj = _load("session3_jobs")
    base = sj.build("/srv/simai", "/repo", "/py", "/srv/simai/datasets/p", "classroom-stills-20261026", "s4")
    assert [j["name"] for j in base["jobs"]] == ["w1_mapanything_known_k_default", "w2_mapanything_known_k_apache"]
    assert all("--resize-mode" not in j["command"] for j in base["jobs"])  # 기존 큐는 그대로
    doc = sj.build("/srv/simai", "/repo", "/py", "/srv/simai/datasets/p", "classroom-stills-20261026", "s4",
                   ("r518", "r1036", "r1554", "rfull"))
    names = [j["name"] for j in doc["jobs"]]
    assert names == [f"w1_mapanything_known_k_default_{r}" for r in ("r518", "r1036", "r1554", "rfull")]
    cmds = {j["name"].rsplit("_", 1)[1]: j["command"] for j in doc["jobs"]}
    assert "--resize-mode" not in cmds["r518"]
    assert cmds["r1036"][-4:] == ["--resize-mode", "longest_side", "--size", "1036"]
    assert cmds["rfull"][-4:] == ["--resize-mode", "fixed_size", "--size", "2304x1296"]
    runs = [j["command"][j["command"].index("--run-id") + 1] for j in doc["jobs"]]
    assert len(set(runs)) == 4 and all(r.endswith("_s4") for r in runs)  # 결과 폴더가 겹치지 않는다
    assert all("/weights/map-anything" in c[c.index("--model") + 1] for c in cmds.values())  # w1 기본 가중치 하나
    assert doc["jobs"][3]["record"]["config"]["overrides"]["resolution"] == "rfull"
    with pytest.raises(SystemExit):
        sj.build("/srv/simai", "/repo", "/py", "/p", "d", "s4", ("r2000",))
    dup = sj.build("/srv/simai", "/repo", "/py", "/p", "d", "s4", ("r518", "rfull", "r518"))
    assert [j["name"].rsplit("_", 1)[1] for j in dup["jobs"]] == ["r518", "rfull"]


def _run(tmp_path: Path, name: str, hw, fix: dict | None) -> Path:
    d = tmp_path / name
    d.mkdir()
    (d / "metrics.json").write_text(json.dumps({
        "result": "SUCCESS", "model_input_hw": hw, "infer_ms_warm": 450.0, "peak_alloc_mb": 6359.0,
        "points_conf_filtered": 446975, "resize": {},
        "per_view": [{"valid_ratio": 0.8}, {"valid_ratio": 0.6}]}), encoding="utf-8")
    if fix is not None:
        (d / "world-fix").write_text(json.dumps(fix), encoding="utf-8")
    return d


def test_compare_rows_split_calibration_and_background_values(tmp_path, capsys):
    cr = _load("compare_resolutions")
    fix = {"status": "FAILED", "reason": "AXES_AMBIGUOUS",
           "evidence": {"floor": {"inliers": 48858, "rms_ma": 0.0115}},
           "reprojection": {"cameras": {"cam_A": {"median_px": 12.0, "p95_px": 90.0},
                                        "cam_B": {"median_px": 9.0, "p95_px": 78.0},
                                        "cam_C": {"median_px": 16.0, "p95_px": 80.0}, "cam_D": None}}}
    a = _run(tmp_path, "r518", [294, 518], fix)
    b = _run(tmp_path, "rfull", [1288, 2296], None)  # fix 를 아직 안 돌린 Run
    r = cr.row(a, 2304)
    assert r["px_per_point"] == 4.45 and r["valid_ratio_mean"] == 0.7 and r["points"] == 446975
    assert r["fix_status"] == "FAILED AXES_AMBIGUOUS" and r["floor_rms"] == 0.0115
    assert r["reproj_median_px"] == 12.0 and r["reproj_p95_px"] == 80.0
    r2 = cr.row(b, 2304)
    assert r2["px_per_point"] == 1.0 and r2["fix_status"] is None and r2["reproj_median_px"] is None  # 모르면 비운다
    assert cr.main([str(a), str(b), "--json", str(tmp_path / "t.json")]) == 0
    out = capsys.readouterr().out
    assert "294×518" in out and "1288×2296" in out
    assert json.loads((tmp_path / "t.json").read_text(encoding="utf-8"))[1]["run"] == "rfull"
    assert cr.main([str(tmp_path / "없음")]) == 2
