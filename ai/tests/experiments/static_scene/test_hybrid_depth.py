"""EXP-01 혼합 경로(run_hybrid_depth.py, S15P31A101-283): 합성 상자 교실(SIM, 상한, 제품 성능 아님)로 깊이 다시 표본·정합·
카메라 간 일관성·융합·가장자리 지표와 가짜 어댑터 끝까지 경로(GPU·torch·OpenCV 없음).

반례: 불연속을 가로질러 섞지 않음, 이상점 25% 에서도 축척 복원, 표본 부족·인라이어 부족은 정합 실패(1 로 채우지 않음),
한 카메라 깊이 5% 어긋남은 일관성 지표가 잡음, 빈 공간에 뜬 점은 융합이 뺌, 퍼진 가장자리는 떠 있는 점 비율이 높음,
다른 입력 폴더·다른 MapAnything 실행의 World 정렬은 거절(종료 2), 결과 폴더를 덮어쓰지 않음.
"""

from __future__ import annotations

import ast
import hashlib
import importlib.util
import json
import sys
from pathlib import Path

import numpy as np
import pytest

AI = Path(__file__).resolve().parents[3]
SCRIPT = AI / "experiments" / "static_scene" / "run_hybrid_depth.py"
ROOM = (8.0, 6.5, 2.9)  # 상자 교실 X·Y·Z(m), 바닥 Z = 0
NAMES = ["cam_A", "cam_B", "cam_C"]


@pytest.fixture(scope="module")
def hd():
    spec = importlib.util.spec_from_file_location("static_scene_run_hybrid_depth", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module  # dataclass(지연 주석)가 모듈을 sys.modules 에서 찾는다
    try:
        spec.loader.exec_module(module)
        yield module
    finally:
        sys.modules.pop(spec.name, None)


# --------------------------------------------------------------------------- 합성 장면


def _K(w: int, h: int, fov_x_deg: float = 85.0) -> np.ndarray:
    f = (w / 2) / np.tan(np.radians(fov_x_deg) / 2)
    return np.array([[f, 0, (w - 1) / 2], [0, f, (h - 1) / 2], [0, 0, 1.0]])


def _look_at(c, target) -> np.ndarray:
    """OpenCV cam2world(+x 오른쪽, +y 아래, +z 앞), world +Z 상방."""
    c, target = np.asarray(c, float), np.asarray(target, float)
    z = target - c
    z /= np.linalg.norm(z)
    x = np.cross(z, [0, 0, 1.0])
    x /= np.linalg.norm(x)
    y = np.cross(z, x)
    T = np.eye(4)
    T[:3, :3] = np.column_stack([x, y, z])
    T[:3, 3] = c
    return T


CAMS = {
    "cam_A": _look_at([0.3, 0.3, 2.6], [4.0, 3.25, 0.6]),
    "cam_B": _look_at([7.7, 0.3, 2.6], [4.0, 3.25, 0.6]),
    "cam_C": _look_at([7.7, 6.2, 2.6], [4.0, 3.25, 0.6]),
}


def _render(T: np.ndarray, K: np.ndarray, hw) -> np.ndarray:
    """상자 방 안쪽 벽·바닥·천장까지의 z 깊이(볼록 내부라 나가는 평면 중 가장 가까운 것)."""
    h, w = hw
    v, u = np.mgrid[0:h, 0:w]
    rays_c = np.linalg.solve(K, np.vstack([u.ravel() + 0.0, v.ravel() + 0.0, np.ones(u.size)]))  # z = 1
    d = T[:3, :3] @ rays_c
    c = T[:3, 3]
    t = np.full(u.size, np.inf)
    for axis, size in enumerate(ROOM):
        for bound in (0.0, size):
            with np.errstate(divide="ignore", invalid="ignore"):
                ti = (bound - c[axis]) / d[axis]
            t = np.where((ti > 1e-9) & (ti < t), ti, t)
    return t.reshape(h, w).astype(np.float32)  # 광선 z 성분이 1 이라 t = z 깊이


# --------------------------------------------------------------------------- 다시 표본


def test_resample_depth_matches_render_at_original_resolution(hd):
    T = CAMS["cam_A"]
    Km, Ko = _K(84, 48), _K(336, 192)
    dm = _render(T, Km, (48, 84))
    out, ok = hd.resample_depth(dm, np.ones_like(dm, bool), Km, Ko, (192, 336))
    truth = _render(T, Ko, (192, 336))
    rel = np.abs(out[ok] - truth[ok]) / truth[ok]
    assert ok.mean() > 0.9
    assert np.median(rel) < 0.002  # 방 모서리(깊이 꺾임)에서만 보간 오차


def test_sample_depth_does_not_mix_across_step(hd):
    src = np.array([[2.0, 2.0, 4.0, 4.0]] * 3, np.float32)
    val, ok = hd.sample_depth(src, np.ones_like(src, bool), np.array([1.4, 1.6, 0.5]), np.array([1.0, 1.0, 1.0]))
    assert ok.all()
    assert val.tolist() == [2.0, 4.0, 2.0]  # 가운데 3 m(떠 있는 점)를 만들지 않음, 매끈하면 보간


def test_sample_depth_needs_valid_neighbors(hd):
    src = np.full((3, 3), 2.0, np.float32)
    valid = np.ones((3, 3), bool)
    valid[1, 1] = False
    _, ok = hd.sample_depth(src, valid, np.array([0.5, 1.0, 5.0]), np.array([0.5, 1.0, 0.0]))
    assert ok.tolist() == [False, False, False]  # 무효 이웃 → 섞지 않음(가장 가까운 이웃도 무효), 영상 밖


# --------------------------------------------------------------------------- 정합


def test_fit_scale_recovers_with_outliers(hd):
    rng = np.random.default_rng(0)
    ref = rng.uniform(1, 10, 20_000)
    d = ref / 1.07
    bad = rng.random(len(d)) < 0.25
    d[bad] *= rng.uniform(0.5, 2.0, bad.sum())
    r = hd.fit_scale_shift(d, ref, "scale")
    assert r.status == "OK"
    assert abs(r.scale - 1.07) < 0.003
    assert 0.70 < r.inlier_ratio < 0.80


def test_fit_scale_shift_recovers_affine(hd):
    rng = np.random.default_rng(1)
    d = rng.uniform(1, 10, 10_000)
    ref = 1.05 * d + 0.2
    r = hd.fit_scale_shift(d, ref, "scale_shift")
    assert r.status == "OK" and abs(r.scale - 1.05) < 1e-3 and abs(r.shift - 0.2) < 1e-2
    assert r.rel_err_after["p50"] < 1e-4


def test_fit_fails_instead_of_inventing_scale(hd):
    rng = np.random.default_rng(2)
    few = hd.fit_scale_shift(np.ones(100), np.ones(100), "scale")
    assert few.status == "FAILED" and few.scale is None  # 표본 부족: 1 로 채우지 않는다
    noise = hd.fit_scale_shift(rng.uniform(1, 10, 5000), rng.uniform(1, 10, 5000), "scale")
    assert noise.status == "FAILED"  # 서로 상관없는 깊이: 인라이어 부족
    none = hd.fit_scale_shift(np.full(1000, 2.0), np.full(1000, 2.2), "none")
    assert none.scale == 1.0 and none.status == "FAILED"  # 맞추지 않으면 10% 차이는 인라이어가 아니다


def test_register_to_reference_recovers_camera_scale(hd):
    T = CAMS["cam_B"]
    Km, Ko = _K(84, 48), _K(336, 192)
    ref = _render(T, Km, (48, 84))
    pred = _render(T, Ko, (192, 336)) / 1.1
    r = hd.register_to_reference(pred, np.ones_like(pred, bool), Ko, ref, np.ones_like(ref, bool), Km, "scale")
    assert r.status == "OK" and abs(r.scale - 1.1) < 0.002 and r.inlier_ratio > 0.95


# --------------------------------------------------------------------------- 일관성·융합


def _views(hd, hw=(96, 168), perturb=None):
    K = _K(hw[1], hw[0])
    out = []
    for n in NAMES:
        d = _render(CAMS[n], K, hw)
        if perturb and n in perturb:
            d = d * perturb[n]
        out.append(hd.FuseView(n, d, np.isfinite(d), K, CAMS[n]))
    return out


def test_cross_view_consistency_exact_and_perturbed(hd):
    exact = hd.cross_view_consistency(_views(hd), samples=3000)["cameras"]
    for rec in exact.values():
        assert rec["abs_rel_depth"]["p50"] < 0.002
        assert rec["roundtrip_px"]["p50"] < 0.6
    bad = hd.cross_view_consistency(_views(hd, perturb={"cam_B": 1.05}), samples=3000)["cameras"]
    assert 0.03 < bad["cam_B"]["abs_rel_depth"]["p50"] < 0.07
    assert bad["cam_B"]["within_1pct"] < 0.2 < exact["cam_B"]["within_1pct"]


def test_visibility_filter_drops_points_floating_in_free_space(hd):
    views = _views(hd)
    va = views[0]
    blob = np.zeros_like(va.valid)
    blob[40:56, 70:100] = True  # 방 가운데 쪽을 보는 픽셀
    va.depth[blob] *= 0.6  # 다른 카메라에서는 빈 공간
    kept, rec = hd.visibility_filter(views)
    X_blob = va.backproject(*np.nonzero(blob))
    kept_a = kept[0]["xyz"]
    dist = np.min(np.linalg.norm(kept_a[:, None, :] - X_blob[None, ::25], axis=-1), axis=0)
    assert np.all(dist > 1e-6)  # 뜬 점은 하나도 남지 않음
    assert rec["cam_A"]["kept"] >= rec["cam_A"]["points"] - int(blob.sum()) - int(0.01 * rec["cam_A"]["points"])
    assert rec["cam_B"]["dropped_conflict_ratio"] == 0.0  # 스치는 천장·모서리의 보간 오차를 충돌로 보지 않음


def test_voxel_merge_averages_within_cell(hd):
    chunks = [{"xyz": np.array([[0.001, 0.001, 0.001], [0.003, 0.003, 0.003], [0.5, 0.5, 0.5]]),
               "rgb": np.array([[0, 0, 0], [100, 100, 100], [10, 20, 30]], np.uint8), "support": None}]
    xyz, rgb, cnt = hd.voxel_merge(chunks, 0.01)
    assert len(xyz) == 2 and sorted(cnt.tolist()) == [1, 2]
    i = int(np.argmax(cnt))
    assert np.allclose(xyz[i], 0.002) and rgb[i].tolist() == [50, 50, 50]


def test_edge_sharpness_sharp_vs_blurred(hd):
    sharp = np.tile(np.r_[np.full(20, 2.0), np.full(20, 4.0)], (10, 1)).astype(np.float32)
    blur = sharp.copy()
    blur[:, 19:22] = [2.5, 3.0, 3.5]
    s = hd.edge_sharpness(sharp, np.ones_like(sharp, bool))
    b = hd.edge_sharpness(blur, np.ones_like(blur, bool))
    assert s["discontinuity_pixels"] > 0 and s["flying_ratio"] == 0.0
    assert b["flying_ratio"] > 0.3


def test_k_for_depth_grid_and_pose_difference(hd):
    K = _K(2304, 1296)
    Kd, note = hd.k_for_depth_grid(K, (294, 518), (1296, 2304))
    assert note == "SCALED_FROM_IMAGE" and abs(Kd[0, 2] - (518 - 1) / 2) < 0.2
    same, note2 = hd.k_for_depth_grid(Kd, (294, 518), (1296, 2304))
    assert note2 == "DEPTH_GRID" and np.allclose(same, Kd)
    w2c = np.stack([np.linalg.inv(CAMS[n]) for n in NAMES])
    moved = w2c.copy()
    moved[1] = np.linalg.inv(CAMS["cam_B"] @ np.diag([1.0, 1, 1, 1]))
    R = np.array([[np.cos(0.01), -np.sin(0.01), 0], [np.sin(0.01), np.cos(0.01), 0], [0, 0, 1]])
    moved[2, :3, :3] = R @ moved[2, :3, :3]
    rec = hd.pose_difference(w2c, moved)
    assert rec[0]["rot_deg"] == 0 and abs(rec[2]["rot_deg"] - np.degrees(0.01)) < 1e-3


def test_predict_side_imports_only_stdlib_and_numpy():
    """predict 는 깊이 모델 전용 가상환경(simai_ai 없음)에서도 돈다: 모듈 최상위 import 는 표준 라이브러리와 numpy 뿐."""
    # 실험 진입점은 scene 라이브러리를 재사용한다. 무거운 모델 import 지연은 구현에서 검사한다.
    library = AI / "src" / "simai_ai" / "scene" / "run_hybrid_depth.py"
    tree = ast.parse(library.read_text(encoding="utf-8"))
    names = set()
    for node in tree.body:
        if isinstance(node, ast.Import):
            names |= {a.name.split(".")[0] for a in node.names}
        elif isinstance(node, ast.ImportFrom):
            names.add((node.module or "").split(".")[0])
    assert names <= {"__future__", "argparse", "hashlib", "json", "math", "os", "sys", "time", "dataclasses", "datetime",
                     "pathlib", "typing", "numpy"}


# --------------------------------------------------------------------------- 끝까지(가짜 어댑터, CLI)


def _write_inputs(root: Path) -> tuple[Path, Path]:
    """MapAnything Run(모델 해상도 48×84) + 입력 폴더(원본 192×336, 마스크 없음). world = 방 좌표."""
    ma, prep = root / "ma", root / "prepared"
    ma.mkdir()
    prep.mkdir()
    Km, Ko = _K(84, 48), _K(336, 192)
    views = [{"name": n, "image": f"{n}.jpg", "image_size": [336, 192], "K_undistorted": Ko.tolist()} for n in NAMES]
    pdoc = {"format": "simai.mapanything_inputs", "format_version": "0.1.0",
            "input_provenance": {"frames": {"source": "SIM", "dataset_id": "synthetic-box"}, "is_upper_bound": True},
            "views": views}
    raw_p = json.dumps(pdoc).encode()
    (prep / "prepared.json").write_bytes(raw_p)
    raw, cams = {"names": np.array(NAMES)}, []
    for n in NAMES:
        d = _render(CAMS[n], Km, (48, 84))
        raw |= {f"{n}__depth_z": d, f"{n}__conf": np.full(d.shape, 5.0, np.float32), f"{n}__mask": np.ones(d.shape, bool),
                f"{n}__intrinsics": Km, f"{n}__camera_pose": CAMS[n]}
        cams.append({"name": n, "model_input_hw": [48, 84], "intrinsics_model_res": Km.tolist(), "cam2world": CAMS[n].tolist()})
    np.savez_compressed(ma / "raw.npz", **raw)
    (ma / "cameras.json").write_text(json.dumps({"run_id": "SIM", "convention": "cam2world 4x4, OpenCV, MapAnything world, m",
                                                 "views": cams}), encoding="utf-8")
    (ma / "metrics.json").write_text(json.dumps({"run_id": "SIM", "result": "SUCCESS", "model_input_hw": [48, 84],
                                                 "prepared_sha256": "sha256:" + hashlib.sha256(raw_p).hexdigest()}),
                                     encoding="utf-8")
    return ma, prep


def test_all_with_fake_adapter_end_to_end(hd, tmp_path):
    ma, prep = _write_inputs(tmp_path)
    out = tmp_path / "out"
    args = ["all", "--ma-run", str(ma), "--prepared", str(prep), "--adapter", "fake", "--fake-scale", "cam_B=1.08",
            "--out", str(out), "--no-color", "--no-mesh", "--voxel", "0.02"]
    assert hd.main(args) == hd.EXIT_OK
    rec = json.loads((out / "fused" / "hybrid_metrics.json").read_text(encoding="utf-8"))
    regs = rec["registration"]["cameras"]
    assert abs(regs["cam_B"]["scale"] - 1 / 1.08) < 0.002 and abs(regs["cam_A"]["scale"] - 1) < 0.001
    for c in rec["consistency"]["cameras"].values():
        assert c["abs_rel_depth"]["p50"] < 0.005  # 정합 뒤 다시 맞음
    assert rec["output_frame"]["frame"] == "MAPANYTHING_WORLD"
    # 3 cm 평면 허용 안에 벽 아래 점이 들어와 정확한 장면도 rms 0.6 cm(world_frame fix 와 같은 지표라 그대로 둔다)
    assert rec["floor"]["status"] == "OK" and rec["floor"]["rms"] < 0.01
    assert abs(rec["floor"]["camera_height"]["p50"] - 2.6) < 0.02
    assert rec["density"]["native_px_per_sample"]["cam_A"] == 4.0
    assert rec["g4"]["status"] == "NOT_MEASURED"
    assert rec["inputs"]["input_provenance"]["is_upper_bound"] is True
    ply = (out / "fused" / "fused.ply").read_bytes()
    assert ply.startswith(b"ply\n") and rec["fusion"]["points"] > 1000
    assert hd.main(args) == hd.EXIT_CONFIG  # 덮어쓰지 않는다


def test_fuse_world_alignment_and_mismatched_inputs(hd, tmp_path):
    ma, prep = _write_inputs(tmp_path)
    depth = tmp_path / "depth"
    assert hd.main(["predict", "--ma-run", str(ma), "--prepared", str(prep), "--adapter", "fake", "--out", str(depth)]) == 0
    sha = "sha256:" + hashlib.sha256((ma / "cameras.json").read_bytes()).hexdigest()
    wa = tmp_path / "wa.json"
    wa.write_text(json.dumps({"format": "simai.world_alignment", "status": "OK", "cameras_json_sha256": sha,
                              "mapanything_to_world": np.eye(4).tolist(), "calibration": {"state": "PROVISIONAL"}}), encoding="utf-8")
    base = ["fuse", "--ma-run", str(ma), "--prepared", str(prep), "--depth", str(depth), "--no-color", "--no-mesh",
            "--voxel", "0.02"]
    assert hd.main(base + ["--out", str(tmp_path / "w"), "--world-alignment", str(wa)]) == 0
    rec = json.loads((tmp_path / "w" / "hybrid_metrics.json").read_text(encoding="utf-8"))
    assert rec["output_frame"]["frame"] == "WORLD" and rec["floor"]["world_z0_rms"] < 0.015
    # 다른 MapAnything 실행으로 만든 World 정렬
    wa.write_text(json.dumps({"format": "simai.world_alignment", "status": "OK", "cameras_json_sha256": "sha256:other",
                              "mapanything_to_world": np.eye(4).tolist()}), encoding="utf-8")
    assert hd.main(base + ["--out", str(tmp_path / "w2"), "--world-alignment", str(wa)]) == hd.EXIT_CONFIG
    # FAILED World 정렬은 MapAnything world 그대로(World 로 쓰지 않음)
    wa.write_text(json.dumps({"format": "simai.world_alignment", "status": "FAILED", "reason": "AXES_AMBIGUOUS",
                              "cameras_json_sha256": sha}), encoding="utf-8")
    assert hd.main(base + ["--out", str(tmp_path / "w3"), "--world-alignment", str(wa)]) == 0
    rec3 = json.loads((tmp_path / "w3" / "hybrid_metrics.json").read_text(encoding="utf-8"))
    assert rec3["output_frame"]["frame"] == "MAPANYTHING_WORLD" and "world_z0_rms" not in rec3["floor"]
    # 다른 입력 폴더로 돈 MapAnything 결과
    doc = json.loads((prep / "prepared.json").read_text(encoding="utf-8"))
    doc["format_version"] = "0.1.1"
    (prep / "prepared.json").write_text(json.dumps(doc), encoding="utf-8")
    assert hd.main(base + ["--out", str(tmp_path / "w4")]) == hd.EXIT_CONFIG


def test_predict_failure_is_recorded_and_not_fused(hd, tmp_path):
    """GPU 세션에서 깊이 모델이 실패(OOM 등)하면 depth.json 에 결과를 남기고 종료 3, fuse 는 그 폴더를 거절한다."""
    ma, prep = _write_inputs(tmp_path)

    class Boom:
        name = "boom"

        def describe(self):
            return {"adapter": "boom"}

        def predict(self, views):
            raise MemoryError("가짜 메모리 부족")

    depth = tmp_path / "depth"
    with pytest.raises(RuntimeError):
        hd.run_predict(Boom(), ma, hd.load_prepared(prep), depth)
    doc = json.loads((depth / "depth.json").read_text(encoding="utf-8"))
    assert doc["result"] == "ERROR" and "MemoryError" in doc["error"]
    assert hd.main(["fuse", "--ma-run", str(ma), "--prepared", str(prep), "--depth", str(depth), "--out", str(tmp_path / "o"),
                    "--no-color", "--no-mesh"]) == hd.EXIT_CONFIG
