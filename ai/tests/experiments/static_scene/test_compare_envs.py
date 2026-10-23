"""MapAnything 환경 동등성 비교(compare_envs.py, S15P31A101-250): 합성 Run(깊이·카메라·신뢰도)으로 합격·불합격·비교 불가.

반례: bf16 수준 잡음은 통과, 깊이 척도 2%·카메라 하나 0.5° 회전·FOV 0.5°·척도 계수 2% 는 불합격, 한 시점의 유효 픽셀이 없거나
척도 계수가 한쪽에 없으면(잴 수 없음) 불합격, 입력·가중치·인자·시점 목록이 다르거나 SUCCESS 가 아니면 비교 불가, 결과 파일을 덮어쓰지 않음.
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import numpy as np
import pytest

AI = Path(__file__).resolve().parents[3]
STATIC = AI / "experiments" / "static_scene"
NAMES = ["cam_A", "cam_B", "cam_C", "cam_D"]
H, W = 48, 64


@pytest.fixture(scope="module")
def ce():
    spec = importlib.util.spec_from_file_location("static_scene_compare_envs", STATIC / "compare_envs.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _rot_z(deg: float) -> np.ndarray:
    t = np.radians(deg)
    return np.array([[np.cos(t), -np.sin(t), 0], [np.sin(t), np.cos(t), 0], [0, 0, 1]])


def _write_run(d: Path, *, depth_scale=1.0, noise=0.0, shift=0.0, rot_cam=None, fov_delta=0.0, scale=1.04, result="SUCCESS",
               input_hash="sha256:in", weights_hash="sha256:w", amp_dtype="bf16", names=NAMES, empty_view=None, seed=1,
               cam_patch=None, pv_patch=None, drop_raw=(), args_patch=None, small_view=None, same_pos=None) -> Path:
    """방 모서리 4대(원점 = cam_A). 깊이는 기울어진 면(3~6 m), 신뢰도는 픽셀마다 다르다.
    반례용 손대기: cam_patch/pv_patch {시점: {키: 값}}(cameras.json·per_view), drop_raw raw 키 빼기, args_patch(None 값이면 args 통째로 뺌),
    small_view 그 시점 배열을 한 줄 작게, same_pos 그 시점 카메라를 cam_A 자리로."""
    rng = np.random.default_rng(seed)
    d.mkdir(parents=True)
    K = np.array([[50.0, 0, W / 2], [0, 50.0, H / 2], [0, 0, 1]])
    raw, views, per_view = {"names": np.array(names)}, [], []
    yy, xx = np.mgrid[0:H, 0:W]
    for i, name in enumerate(names):
        T = np.eye(4)
        T[:3, :3] = _rot_z(90 * i)
        T[:3, 3] = np.array([[0, 0, 0], [6, 0, 0], [6, 5, 0], [0, 5, 0]][i]) + [shift, 0, 0]
        if rot_cam == name:
            T[:3, :3] = T[:3, :3] @ _rot_z(0.5)
        if same_pos == name:
            T[:3, 3] = [shift, 0, 0]
        depth = (3.0 + 3.0 * xx / W + 0.5 * yy / H) * depth_scale
        depth = depth * (1 + noise * rng.standard_normal(depth.shape))
        if small_view == name:
            depth = depth[1:]
        mask = np.ones(depth.shape, bool)
        mask[:2] = False
        if empty_view == name:
            mask[:] = False
        conf = 1 + rng.random(depth.shape)
        Kv = K.copy()
        Kv[0, 0] = Kv[1, 1] = 50.0 / (1 + fov_delta)
        raw |= {f"{name}__depth_z": depth.astype(np.float32), f"{name}__conf": conf.astype(np.float32), f"{name}__mask": mask,
                f"{name}__intrinsics": Kv, f"{name}__camera_pose": T}
        views.append({"name": name, "model_input_hw": [H, W], "intrinsics_model_res": Kv.tolist(), "cam2world": T.tolist(),
                      "metric_scaling_factor": scale, "fov_x_deg": float(np.degrees(2 * np.arctan(W / 2 / Kv[0, 0])))})
        per_view.append({"name": name, "depth_m": {"p50": float(np.median(depth[mask]))} if mask.any() else {}})
        views[-1] |= (cam_patch or {}).get(name, {})
        per_view[-1]["depth_m"] |= (pv_patch or {}).get(name, {})
    for key in drop_raw:
        raw.pop(key)
    np.savez_compressed(d / "raw.npz", **raw)
    (d / "cameras.json").write_text(json.dumps({"views": views}), encoding="utf-8")
    metrics = {"result": result, "input_hash": input_hash, "weights_hash": weights_hash, "images": [f"{n}.jpg" for n in names],
               "prepared_sha256": None, "per_view": per_view, "gpu": {"model": "NVIDIA A40", "torch": "x", "cuda": "y"},
               "args": {"mode": "images", "amp": 1, "amp_dtype": amp_dtype, "minibatch": 0, "conf_percentile": 10.0,
                        "use_poses": 0, "pose_scale": 1.0, "known_from": None}}
    if args_patch is not None:
        metrics["args"] |= args_patch
        if args_patch.get("__drop__"):
            del metrics["args"]
    (d / "metrics.json").write_text(json.dumps(metrics), encoding="utf-8")
    return d


def _verdict(ce, tmp_path, **new_kw):
    base = _write_run(tmp_path / "base")
    new = _write_run(tmp_path / "new", **new_kw)
    try:
        res = ce.compare(ce.load_run(base), ce.load_run(new))
    except ce.Incomparable as e:
        return "INCOMPARABLE", {"reason": str(e)}
    return res["verdict"], res


def _failed(res) -> set[str]:
    return {c["name"] for c in res["checks"] if not c["ok"]}


def test_identical_and_bf16_level_noise_pass(ce, tmp_path):
    v, res = _verdict(ce, tmp_path / "same")
    assert v == "PASS" and res["values"]["point_rel_median"] == 0.0 and res["record_only"]["chamfer_median_rel"] == 0.0
    v, res = _verdict(ce, tmp_path / "noise", noise=0.002, seed=7)  # 픽셀당 0.2% 잡음(bf16 ↔ fp32 깊이 중앙값 0.13~0.17% 수준)
    assert v == "PASS", _failed(res)
    assert 0 < res["values"]["pixel_depth_rel_median"] < 0.01 and res["record_only"]["pixel_depth_rel_p95"] > 0


@pytest.mark.parametrize(("kw", "expect"), [
    ({"depth_scale": 1.02}, {"pixel_depth_rel_median", "depth_p50_rel_diff", "point_rel_median"}),
    ({"shift": 0.3}, {"point_rel_median"}),  # 모든 카메라가 같이 옮겨짐: 거리 비·회전은 그대로, 같은 픽셀의 점이 옮겨진다
    ({"rot_cam": "cam_C"}, {"rot_diff_max_deg"}),
    ({"fov_delta": 0.02}, {"fov_x_diff_max_deg"}),
    ({"scale": 1.04 * 1.02}, {"scale_rel_diff"}),
], ids=["depth-2pct", "world-shift", "camera-rot-0.5deg", "fov", "metric-scale-2pct"])
def test_out_of_limit_fails_on_the_right_check(ce, tmp_path, kw, expect):
    v, res = _verdict(ce, tmp_path, **kw)
    assert v == "FAIL" and expect <= _failed(res), _failed(res)


def test_boundary_depth_just_inside_passes(ce, tmp_path):
    v, res = _verdict(ce, tmp_path, depth_scale=1.009)
    assert v == "PASS", _failed(res)


def test_unmeasurable_values_fail_not_pass(ce, tmp_path):
    """한 시점의 유효 픽셀이 없음·척도 계수 없음은 '잴 수 없음' → 불합격(모르면 통과로 두지 않는다)."""
    v, res = _verdict(ce, tmp_path / "empty", empty_view="cam_B")
    assert v == "FAIL" and {"pixel_depth_rel_median", "depth_p50_rel_diff"} <= _failed(res)
    v, res = _verdict(ce, tmp_path / "noscale", scale=None)
    assert v == "FAIL" and "scale_rel_diff" in _failed(res)


@pytest.mark.parametrize("kw", [{"input_hash": "sha256:other"}, {"weights_hash": "sha256:apache"}, {"amp_dtype": "fp32"},
                                {"names": NAMES[:3]}, {"result": "OOM"}],
                         ids=["input", "weights", "args", "views", "not-success"])
def test_different_inputs_are_incomparable(ce, tmp_path, kw):
    v, _ = _verdict(ce, tmp_path, **kw)
    assert v == "INCOMPARABLE"


def test_cli_exit_codes_and_no_overwrite(ce, tmp_path, capsys):
    base = _write_run(tmp_path / "base")
    good = _write_run(tmp_path / "good", noise=0.001)
    bad = _write_run(tmp_path / "bad", depth_scale=1.05)
    other = _write_run(tmp_path / "other", input_hash="sha256:x")
    out = tmp_path / "cmp.json"
    assert ce.main(["--base", str(base), "--new", str(good), "-o", str(out)]) == 0
    assert json.loads(out.read_text(encoding="utf-8"))["verdict"] == "PASS"
    assert ce.main(["--base", str(base), "--new", str(good), "-o", str(out)]) == 2  # 덮어쓰지 않는다
    assert ce.main(["--base", str(base), "--new", str(bad)]) == 1
    assert ce.main(["--base", str(base), "--new", str(other)]) == 2
    assert ce.main(["--base", str(base), "--new", str(tmp_path / "missing")]) == 2
    capsys.readouterr()


# ---------------------------------------------------------------- 리뷰 반례(Argon -250): 뒤 시점 NaN, 시점별 척도, known_from, 손상 Run


NAN = float("nan")


@pytest.mark.parametrize("kw, check", [
    ({"cam_patch": {"cam_D": {"fov_x_deg": NAN}}}, "fov_x_diff_max_deg"),
    ({"pv_patch": {"cam_D": {"p50": NAN}}}, "depth_p50_rel_diff"),
    ({"cam_patch": {"cam_C": {"cam2world": [[NAN] * 4] * 3 + [[0, 0, 0, 1]]}}}, "rot_diff_max_deg"),
], ids=["fov-nan-last-view", "depth-p50-nan-last-view", "pose-nan-third-view"])
def test_nan_in_a_later_view_is_not_dropped_by_max(ce, tmp_path, kw, check):
    """내장 max 는 첫 시점이 유한하면 뒤 시점 NaN 을 버린다. 하나라도 잴 수 없으면 그 항목은 불합격이어야 한다."""
    v, res = _verdict(ce, tmp_path, **kw)
    assert v == "FAIL" and check in _failed(res), _failed(res)


@pytest.mark.parametrize("kw", [
    {"cam_patch": {"cam_C": {"metric_scaling_factor": None}}},  # 뒤 시점 척도 없음
    {"cam_patch": {"cam_D": {"metric_scaling_factor": 1.04 * 1.03}}},  # 뒤 시점만 3% 벌어짐
    {"scale": True},  # bool 은 수가 아니다(isinstance(True, int) 가 참)
    {"scale": -1.04},  # 음수 척도
], ids=["later-view-none", "later-view-3pct", "bool", "negative"])
def test_metric_scale_checked_in_every_view(ce, tmp_path, kw):
    if kw.get("scale") is not None:  # 두 Run 이 같은 이상한 값이면 차이 0 이라 예전에는 통과했다
        base = _write_run(tmp_path / "base", scale=kw["scale"])
        new = _write_run(tmp_path / "new", scale=kw["scale"])
        res = ce.compare(ce.load_run(base), ce.load_run(new))
    else:
        _v, res = _verdict(ce, tmp_path, **kw)
    assert res["verdict"] == "FAIL" and "scale_rel_diff" in _failed(res), _failed(res)


def test_known_mode_with_different_camera_file_is_incomparable(ce, tmp_path):
    base = _write_run(tmp_path / "base", args_patch={"mode": "known", "known_from": "/srv/simai/runs/exp01/C1/cameras.json"})
    new = _write_run(tmp_path / "new", args_patch={"mode": "known", "known_from": "/srv/simai/runs/exp01/C2/cameras.json"})
    with pytest.raises(ce.Incomparable, match="known_from"):
        ce.compare(ce.load_run(base), ce.load_run(new))


@pytest.mark.parametrize("kw", [
    {"drop_raw": ("cam_C__conf",)},
    {"args_patch": {"__drop__": True}},
    {"small_view": "cam_B"},
    {"cam_patch": {"cam_D": {"cam2world": [[1, 0, 0]]}}},
], ids=["raw-key-missing", "args-missing", "array-size-mismatch", "bad-pose-shape"])
def test_damaged_run_is_incomparable_not_a_traceback(ce, tmp_path, capsys, kw):
    base = _write_run(tmp_path / "base")
    new = _write_run(tmp_path / "new", **kw)
    assert ce.main(["--base", str(base), "--new", str(new)]) == 2
    assert json.loads(capsys.readouterr().out)["verdict"] == "INCOMPARABLE"


def test_degenerate_base_camera_pair_is_unmeasurable_and_json_has_no_nan(ce, tmp_path, capsys):
    """기준 카메라 두 대가 한 점이면 거리 비를 잴 수 없다(작은 수로 나눠 꾸미지 않음). 결과 JSON 은 NaN 대신 null."""
    base = _write_run(tmp_path / "base", same_pos="cam_B")
    new = _write_run(tmp_path / "new", same_pos="cam_B")
    assert ce.main(["--base", str(base), "--new", str(new)]) == 1
    res = json.loads(capsys.readouterr().out)  # allow_nan=False 로 썼으므로 NaN 이 있으면 여기 오기 전에 실패
    ratio = next(c for c in res["checks"] if c["name"] == "cam_dist_ratio")
    assert ratio["ok"] is False and ratio["value"] == [None, None]


# ---------------------------------------------------------------- Codex 리뷰 R2·R3: 회전 Inf, 손상 zip·문자열 배열


def test_infinite_rotation_entry_fails_instead_of_zero_degrees(ce, tmp_path):
    """clip(+inf) = 1 이 0도 차이로 보이던 결함: 비유한 회전은 잴 수 없음(FAIL)."""
    pose = np.eye(4).tolist()
    pose[0][0] = float("inf")
    v, res = _verdict(ce, tmp_path, cam_patch={"cam_A": {"cam2world": pose}})
    assert v == "FAIL" and "rot_diff_max_deg" in _failed(res), _failed(res)


def _corrupt_raw_zip(d: Path) -> None:
    (d / "raw.npz").write_bytes(b"PK\x03\x04BROKEN")


def _string_depth(d: Path) -> None:
    with np.load(d / "raw.npz") as z:
        raw = {k: z[k] for k in z.files}
    raw["cam_C__depth_z"] = np.full(raw["cam_C__depth_z"].shape, "broken")
    np.savez_compressed(d / "raw.npz", **raw)


@pytest.mark.parametrize("damage", [_corrupt_raw_zip, _string_depth], ids=["bad-zip", "string-dtype"])
def test_unreadable_raw_is_incomparable_with_standard_json(ce, tmp_path, capsys, damage):
    base = _write_run(tmp_path / "base")
    new = _write_run(tmp_path / "new")
    damage(new)
    assert ce.main(["--base", str(base), "--new", str(new)]) == 2
    out = capsys.readouterr().out
    assert json.loads(out)["verdict"] == "INCOMPARABLE" and "NaN" not in out
