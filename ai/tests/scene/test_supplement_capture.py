"""세팅 보조 촬영(S15P31A101-605): 프레임 고르기·섞인 입력 폴더·등록·관문·묶음 병합을 GPU 없이 합성 자료로 확인한다.

MapAnything 은 가짜(`_FakeMapAnything`)로 바꾼다. 상자 모양 방(6×6×3 m)을 광선 투사해 깊이를 만들고, 자세·깊이를 알려진
similarity(축척 0.5, z 축 30°, 이동)로 옮겨 '모델 세계'로 낸다. 정렬이 고정 카메라만으로 그 similarity 를 되찾는지 본다.
"""

from __future__ import annotations

import json
import math
from pathlib import Path

import numpy as np
import pytest

cv2 = pytest.importorskip("cv2")

from simai_ai.scene import supplement_capture as sc  # noqa: E402
from simai_ai.scene.intrinsics_check import expected_crop_resize_k  # noqa: E402

IMG_WH = (160, 90)
MODEL_HW = (36, 64)
ROOM = (6.0, 6.0, 3.0)
FIXED = ("cam_A", "cam_B", "cam_C", "cam_D")


# --------------------------------------------------------------------------- 합성 기하
def _look_at(center, target, up=(0.0, 0.0, 1.0)) -> np.ndarray:
    """cam2world(OpenCV: +x 오른쪽, +y 아래, +z 앞)."""
    c, tgt = np.asarray(center, float), np.asarray(target, float)
    z = tgt - c
    z /= np.linalg.norm(z)
    x = np.cross(z, np.asarray(up, float))
    x /= np.linalg.norm(x)
    y = np.cross(z, x)
    T = np.eye(4)
    T[:3, :3] = np.stack([x, y, z], axis=1)
    T[:3, 3] = c
    return T


def _K(f: float, wh=IMG_WH) -> np.ndarray:
    return np.array([[f, 0, wh[0] / 2 - 0.5], [0, f, wh[1] / 2 - 0.5], [0, 0, 1.0]])


def _render_depth(T: np.ndarray, K_model: np.ndarray) -> np.ndarray:
    h, w = MODEL_HW
    vv, uu = np.mgrid[:h, :w]
    rays_c = np.linalg.inv(K_model) @ np.stack([uu.ravel(), vv.ravel(), np.ones(h * w)])
    d = T[:3, :3] @ rays_c
    c = T[:3, 3][:, None]
    ts = []
    for axis, size in enumerate(ROOM):
        for plane in (0.0, size):
            with np.errstate(divide="ignore", invalid="ignore"):
                t = (plane - c[axis]) / d[axis]
            t[~np.isfinite(t) | (t <= 1e-6)] = np.inf
            ts.append(t)
    t = np.min(np.stack(ts), axis=0)
    return t.reshape(h, w).astype(np.float32)  # rays_c 의 z = 1 → t 가 곧 카메라 z 깊이


S_MA = 0.5
_ang = math.radians(30)
R_MA = np.array([[math.cos(_ang), -math.sin(_ang), 0], [math.sin(_ang), math.cos(_ang), 0], [0, 0, 1.0]])
B_MA = np.array([0.7, -0.4, 0.2])


def _to_ma(T_world: np.ndarray) -> np.ndarray:
    """World → 모델 세계: X_ma = S_MA·R_MA·X + B_MA (깊이는 S_MA 배)."""
    T = np.eye(4)
    T[:3, :3] = R_MA @ T_world[:3, :3]
    T[:3, 3] = S_MA * (R_MA @ T_world[:3, 3]) + B_MA
    return T


FIXED_POSES = {
    "cam_A": _look_at((0.3, 0.3, 2.4), (3.2, 3.0, 0.6)),
    "cam_B": _look_at((5.7, 0.3, 2.4), (2.8, 3.1, 0.6)),
    "cam_C": _look_at((5.7, 5.7, 2.4), (3.0, 2.9, 0.6)),
    "cam_D": _look_at((0.3, 5.7, 2.4), (3.1, 3.0, 0.6)),
}
FIXED_K = _K(90.0)


class _FakeMapAnything:
    """prepared 폴더 → MapAnything 출력 모양(cameras.json·raw.npz·metrics.json). 보조 view 의 실제 자세·K 는 truth 에서."""

    def __init__(self, truth: dict[str, tuple[np.ndarray, np.ndarray, np.ndarray | None]]) -> None:
        self.truth = truth  # media_id → (실제 cam2world, 실제 영상 K, 보고할 cam2world 또는 None)
        self.calls: list[list[str]] = []

    def __call__(self, prepared_dir: Path, ma_dir: Path) -> None:
        raw_bytes = (prepared_dir / "prepared.json").read_bytes()
        doc = json.loads(raw_bytes)
        ma_dir.mkdir(parents=True)
        raw, cams = {}, []
        self.calls.append([v["name"] for v in doc["views"]])
        for v in doc["views"]:
            name = v["name"]
            if v.get("K_undistorted") is not None:
                T_true, K_img, T_rep = FIXED_POSES[name], np.asarray(v["K_undistorted"]), None
            else:
                T_true, K_img, T_rep = self.truth[v["source_media"]["media_id"]]
            Km = expected_crop_resize_k(K_img, v["image_size"], MODEL_HW[::-1])
            depth = _render_depth(T_true, Km) * S_MA
            T_out = _to_ma(T_rep if T_rep is not None else T_true)
            raw[f"{name}__depth_z"] = depth
            raw[f"{name}__conf"] = np.ones_like(depth)
            raw[f"{name}__mask"] = np.isfinite(depth)
            raw[f"{name}__intrinsics"] = Km.astype(np.float32)
            raw[f"{name}__camera_pose"] = T_out.astype(np.float32)
            cams.append({"name": name, "model_input_hw": list(MODEL_HW), "intrinsics_model_res": Km.tolist(),
                         "cam2world": T_out.tolist()})
        np.savez_compressed(ma_dir / "raw.npz", names=np.array([v["name"] for v in doc["views"]]), **raw)
        (ma_dir / "cameras.json").write_text(json.dumps({"convention": "cam2world 4x4, OpenCV, MapAnything world", "views": cams}))
        import hashlib

        (ma_dir / "metrics.json").write_text(json.dumps({
            "result": "SUCCESS", "run_id": ma_dir.name, "model_version": "fake", "args": {"mode": "prepared"},
            "prepared_sha256": "sha256:" + hashlib.sha256(raw_bytes).hexdigest()}))


def _noise_png(seed: int, wh=(320, 180)) -> bytes:
    rng = np.random.default_rng(seed)
    img = cv2.resize(rng.integers(0, 255, (wh[1] // 4, wh[0] // 4, 3), dtype=np.uint8), wh, interpolation=cv2.INTER_NEAREST)
    ok, data = cv2.imencode(".png", img)
    assert ok
    return data.tobytes()


def _site(tmp_path: Path) -> Path:
    """고정 카메라만으로 prepare·mapanything 이 끝난 작업 폴더."""
    root = tmp_path / "root"
    prep = root / "prepared"
    prep.mkdir(parents=True)
    cams, views = [], []
    for i, cid in enumerate(FIXED):
        T = FIXED_POSES[cid]
        R = T[:3, :3].T
        cams.append({"camera_id": cid, "K": FIXED_K.tolist(), "R": R.tolist(), "t": (-R @ T[:3, 3]).tolist(),
                     "image_size": list(IMG_WH), "distortion": {"model": "NONE", "coeffs": []}})
        (prep / f"{cid}.jpg").write_bytes(_noise_png(100 + i, IMG_WH))
        views.append({"name": cid, "camera_id": cid, "image": f"{cid}.jpg", "mask": None, "image_size": list(IMG_WH),
                      "K_undistorted": FIXED_K.tolist()})
    (root / "calibration.json").write_text(json.dumps({"calibration_version": "cal-1", "cameras": cams}))
    (root / "job.json").write_text(json.dumps({"run_id": "r1", "input": {
        "images": [{"camera_id": c, "kind": "CLEAN_BACKGROUND"} for c in FIXED],
        "input_provenance": {"frames": {"source": "LIVE"}, "is_upper_bound": False}, "options": {}}}))
    (prep / "prepared.json").write_text(json.dumps({"format": sc.PREPARED_FORMAT, "format_version": "0.1.0",
                                                    "views": views, "person_masks": False}))
    _FakeMapAnything({})(prep, root / "mapanything")
    return root


GOOD_POSES = [
    _look_at((2.0, 2.0, 1.4), (5.9, 2.5, 1.0)),
    _look_at((2.5, 3.5, 1.5), (5.9, 4.0, 0.8)),
    _look_at((3.0, 2.5, 1.2), (3.0, 0.1, 0.9)),
    _look_at((3.5, 3.0, 1.6), (0.1, 3.5, 1.0)),
    _look_at((2.2, 4.0, 1.3), (2.5, 5.9, 0.7)),
    _look_at((4.0, 4.0, 1.5), (5.9, 5.0, 1.2)),
]


def _request(root: Path, items: list[tuple[str, int]], **config) -> None:
    rows = []
    (root / "supplement" / "media").mkdir(parents=True, exist_ok=True)
    for mid, seed in items:
        (root / "supplement" / "media" / f"{mid}.image").write_bytes(_noise_png(seed))
        rows.append({"media_id": mid, "kind": "IMAGE", "path": f"media/{mid}.image", "captured_at_ms": 1_000 + seed,
                     "source": "PHONE_UPLOAD", "device": {"make": "acme", "model": "x1"}})
    sc.write_request(root, rows, config)


def _no_people(image: np.ndarray) -> np.ndarray:
    return np.zeros(image.shape[:2], bool)


def _summary_errors(doc: dict) -> list[str]:
    from simai_ai.providers.contract import schema_errors

    return schema_errors(doc, {"$ref": "https://simai.dev/contracts/messages/site_setup.schema.json#/$defs/supplement_summary"})


# --------------------------------------------------------------------------- 프레임 고르기
def _write_video(path: Path, frames: list[np.ndarray], fps: float = 10.0) -> None:
    h, w = frames[0].shape[:2]
    vw = cv2.VideoWriter(str(path), cv2.VideoWriter_fourcc(*"MJPG"), fps, (w, h))
    assert vw.isOpened()
    for f in frames:
        vw.write(f)
    vw.release()


def _walk_frames(n: int, *, blur_every: int = 0, still_from: int | None = None) -> list[np.ndarray]:
    """넓은 무늬를 옆으로 미는 '걷는 화면'. blur_every 마다 흐린 프레임, still_from 부터는 멈춘 화면."""
    rng = np.random.default_rng(7)
    big = cv2.resize(rng.integers(0, 255, (60, 400, 3), dtype=np.uint8), (1600, 240), interpolation=cv2.INTER_NEAREST)
    out = []
    for i in range(n):
        x = 12 * (min(i, still_from) if still_from is not None else i)
        f = big[:, x:x + 320].copy()
        if blur_every and i % blur_every == blur_every - 1:
            f = cv2.GaussianBlur(f, (0, 0), 6)
        out.append(f)
    return out


def test_video_selection_respects_cap_spacing_blur_and_duplicates(tmp_path: Path) -> None:
    path = tmp_path / "walk.avi"
    _write_video(path, _walk_frames(100, blur_every=5, still_from=60), fps=10.0)  # 10 s, 6 s 뒤 멈춤
    cfg = sc.SupplementConfig(max_frames=12, sample_fps=5.0)
    sel = sc.select_frames([sc.MediaInput("v1", "VIDEO", path)], cfg)
    st = sel.media["v1"]
    assert st["status"] is None and st["fps"] == pytest.approx(10.0, abs=0.01)
    assert 1 <= len(sel.frames) <= 12
    assert st["blurry_dropped"] >= 5  # 흐린 프레임(5장마다 1장)을 버렸다
    times = sorted(f.t_s for f in sel.frames)
    assert min(b - a for a, b in zip(times, times[1:])) >= 0.5 - 1e-9
    assert all(f.index % 5 != 4 for f in sel.frames)  # 흐린 프레임 번호는 고르지 않았다
    # 멈춘 구간(6 s 뒤)은 거의 같은 화면이라 한 장만 남는다
    assert sum(1 for f in sel.frames if f.index >= 60) <= 1
    assert st["duplicates_dropped"] >= 1
    assert all(f.image.shape[:2] == (240, 320) for f in sel.frames)
    assert len({f.name for f in sel.frames}) == len(sel.frames) and all(not f.name.split("_")[-1].isdigit() for f in sel.frames)


def test_selection_splits_cap_across_media_and_reports_bad_files(tmp_path: Path) -> None:
    video = tmp_path / "a.avi"
    _write_video(video, _walk_frames(60), fps=10.0)
    img = tmp_path / "p.png"
    img.write_bytes(_noise_png(3))
    bad = tmp_path / "bad.image"
    bad.write_bytes(b"not an image")
    media = [sc.MediaInput("v", "VIDEO", video), sc.MediaInput("p", "IMAGE", img), sc.MediaInput("x", "IMAGE", bad),
             sc.MediaInput("d", "VIDEO", None, status=sc.M_DOWNLOAD_FAILED)]
    sel = sc.select_frames(media, sc.SupplementConfig(max_frames=5, sample_fps=4.0))
    assert len(sel.frames) <= 5
    assert {f.media_id for f in sel.frames} == {"v", "p"}  # 상한 안에서 파일마다 적어도 한 장
    assert sel.media["x"]["status"] == sc.M_UNREADABLE and sel.media["d"]["status"] == sc.M_DOWNLOAD_FAILED


def test_allocate_is_proportional_and_gives_each_file_one() -> None:
    assert sc._allocate(10, {"a": 3, "b": 2}) == {"a": 3, "b": 2}
    alloc = sc._allocate(10, {"a": 90, "b": 10, "c": 1})
    assert sum(alloc.values()) == 10 and alloc["c"] == 1 and alloc["a"] > alloc["b"] >= 1
    assert sum(sc._allocate(2, {"a": 5, "b": 5, "c": 5}).values()) == 2


def test_wide_lens_photo_is_rejected_by_exif(tmp_path: Path) -> None:
    Image = pytest.importorskip("PIL.Image")
    path = tmp_path / "wide.jpg"
    im = Image.fromarray(np.random.default_rng(0).integers(0, 255, (90, 160, 3), dtype=np.uint8))
    exif = Image.Exif()
    exif[0x8769] = {0xA405: 13}
    im.save(path, exif=exif)
    assert sc.exif_focal_35mm(path) == 13
    sel = sc.select_frames([sc.MediaInput("w", "IMAGE", path)], sc.SupplementConfig())
    assert sel.frames == [] and sel.media["w"]["status"] == sc.M_WIDE_FOV_REJECTED


def test_normalize_frame_crops_to_fixed_aspect_without_upscaling() -> None:
    portrait = np.zeros((1920, 1080, 3), np.uint8)
    out = sc.normalize_frame(portrait, (2304, 1296))
    assert out.shape[1] == 1080 and abs(out.shape[1] / out.shape[0] - 16 / 9) < 0.01
    big = np.zeros((3000, 4000, 3), np.uint8)
    out = sc.normalize_frame(big, (2304, 1296))
    assert out.shape[:2] == (1296, 2304)


def test_prefix_matches_calibration_unit_check_exclusion() -> None:
    pytest.importorskip("jsonschema")
    from simai_ai.calibration.extrinsics.initial import SUPPLEMENT_VIEW_PREFIX

    assert sc.SUP_PREFIX == SUPPLEMENT_VIEW_PREFIX


# --------------------------------------------------------------------------- 입력 폴더·K
def test_joint_prepared_mixes_known_k_fixed_views_and_k_less_supplement(tmp_path: Path) -> None:
    root = _site(tmp_path)
    frames = [sc.SelectedFrame("m1", 0, None, 10.0, np.full((360, 480, 3), 120, np.uint8))]
    media = {"m1": sc.MediaInput("m1", "IMAGE", tmp_path / "x", captured_at_ms=5, device={"model": "x1"})}
    doc = sc.build_joint_prepared(root / "prepared", tmp_path / "joint", frames, media,
                                  lambda img: np.pad(np.ones((10, 10), bool), ((0, img.shape[0] - 10), (0, img.shape[1] - 10))))
    roles = [v["view_role"] for v in doc["views"]]
    assert roles == [sc.VIEW_ROLE_FIXED] * 4 + [sc.VIEW_ROLE_SUPPLEMENT]  # 고정 view 가 앞(기준 view)
    sup = doc["views"][-1]
    assert sup["K_undistorted"] is None and sup["image_size"] == [160, 90] and sup["mask"]
    assert sup["source_media"]["captured_at_ms"] == 5 and doc["supplement"]["input_role"] == sc.INPUT_ROLE
    assert doc["supplement"]["media"] == [{"media_id": "m1", "kind": "IMAGE", "captured_at_ms": 5, "device": {"model": "x1"}}]
    mask = cv2.imread(str(tmp_path / "joint" / sup["mask"]), cv2.IMREAD_GRAYSCALE)
    assert mask.shape == (90, 160) and mask[0, 0] == 255  # 사람 마스크(팽창)
    torch = pytest.importorskip("torch")  # noqa: F841 - run_mapanything 은 모듈 맨 위에서 torch 를 import 한다
    from simai_ai.scene import run_mapanything

    specs = run_mapanything.prepared_views(tmp_path / "joint")
    assert [s["K"] is None for s in specs] == [False] * 4 + [True]


def test_model_k_roundtrips_crop_resize() -> None:
    K = _K(700.0, (1920, 1080))
    Km = expected_crop_resize_k(K, (1920, 1080), (518, 294))
    np.testing.assert_allclose(sc.model_k_to_image_k(Km, (1920, 1080), (294, 518)), K, atol=1e-6)


# --------------------------------------------------------------------------- 관문
@pytest.mark.parametrize(("kw", "reason"), [
    ({}, None),
    ({"registered": 2}, sc.R_TOO_FEW_REGISTERED),  # 2/10 < 30 %
    ({"registered": 0, "selected": 0}, sc.R_TOO_FEW_REGISTERED),
    ({"chunk_center_m": 0.081}, sc.R_ALIGNMENT_WORSE),  # 절대 안전선 8 cm 초과(-652)
    ({"chunk_center_m": 0.079}, None),
    ({"chunk_center_m": 0.079, "baseline_center_m": 0.00001}, None),  # 기준(고정 4대 자기 맞춤 ≈0)과 비교하지 않는다
    ({"chunk_center_m": None}, sc.R_ALIGNMENT_WORSE),  # 모르면 통과로 채우지 않는다
    # 14차 GPU 세션 실측(40/40 등록, 재투영 4.546 → 4.777 px, 중심 0.00001 → 0.063 m): 예전 2 cm 상대 기준은 거부, 지금은 통과
    ({"baseline_center_m": 0.00001, "chunk_center_m": 0.063, "baseline_px": 4.546, "chunk_px": 4.777,
      "selected": 40, "registered": 40}, None),
    ({"chunk_center_m": 0.2, "chunk_px": 9.0}, sc.R_FIXED_REPROJECTION_WORSE),  # 재투영이 주 판정(먼저 본다)
    ({"chunk_px": 4.0 * 1.25 + 0.51}, sc.R_FIXED_REPROJECTION_WORSE),
    ({"chunk_px": 4.0 * 1.25 + 0.49}, None),
    ({"chunk_px": None}, sc.R_FIXED_REPROJECTION_WORSE),
    ({"baseline_px": None, "chunk_px": None}, None),  # 기준을 모르면 그 항목은 비교하지 않는다
])
def test_gate_chunk(kw: dict, reason: str | None) -> None:
    args = {"selected": 10, "registered": 8, "baseline_center_m": 0.05, "chunk_center_m": 0.06,
            "baseline_px": 4.0, "chunk_px": 4.2, "cfg": sc.SupplementConfig()}
    args.update(kw)
    assert sc.gate_chunk(**args) == reason


# --------------------------------------------------------------------------- 전체(가짜 MapAnything)
def test_supplement_applies_registered_frames_aligns_with_fixed_cameras_only(tmp_path: Path) -> None:
    root = _site(tmp_path)
    wrong = _look_at((1.0, 1.0, 0.5), (1.0, 5.0, 2.5))
    truth = {"g0": (GOOD_POSES[0], _K(120.0), None), "g1": (GOOD_POSES[1], _K(110.0), None),
             "g2": (GOOD_POSES[2], _K(130.0), None), "bad": (GOOD_POSES[3], _K(120.0), wrong)}
    _request(root, [("g0", 1), ("g1", 2), ("g2", 3), ("bad", 4)])
    fake = _FakeMapAnything(truth)
    out = sc.run_supplement(root, infer=fake, person_mask=_no_people, cfg=sc.SupplementConfig())
    assert _summary_errors(out) == []
    assert out["status"] == "APPLIED" and out["reason"] is None
    assert out["frames_selected"] == 4 and out["frames_registered"] == 3
    states = {m["media_id"]: m["status"] for m in out["media"]}
    assert states == {"g0": "USED", "g1": "USED", "g2": "USED", "bad": "NOT_REGISTERED"}
    assert out["captured_from_ms"] == 1001 and out["captured_to_ms"] == 1003
    assert out["alignment"]["supplement_center_residual_m"] < 1e-3
    # 작업 폴더가 World 좌표 병합본으로 바뀌었다(원래 것은 *_base)
    assert (root / "prepared_base" / "prepared.json").is_file() and (root / "mapanything_base").is_dir()
    prep = json.loads((root / "prepared" / "prepared.json").read_text(encoding="utf-8"))
    sup = [v for v in prep["views"] if v["view_role"] == sc.VIEW_ROLE_SUPPLEMENT]
    assert len(sup) == 3 and all(v["k_source"] == "MODEL_ESTIMATE" for v in sup)
    np.testing.assert_allclose(sup[0]["K_undistorted"], _K(120.0), atol=1e-3)
    wa = json.loads((root / "world_alignment.json").read_text(encoding="utf-8"))
    np.testing.assert_allclose(wa["mapanything_to_world"], np.eye(4))
    import hashlib

    assert wa["cameras_json_sha256"] == "sha256:" + hashlib.sha256((root / "mapanything" / "cameras.json").read_bytes()).hexdigest()
    cams = {v["name"]: v for v in json.loads((root / "mapanything" / "cameras.json").read_text(encoding="utf-8"))["views"]}
    # 보조 자세 = World 실제 자세(바닥 제약 정렬이 남기는 1 cm 안팎 차이 허용)
    np.testing.assert_allclose(cams[sup[0]["name"]]["cam2world"], GOOD_POSES[0], atol=2e-2)
    np.testing.assert_allclose(cams["cam_B"]["cam2world"], FIXED_POSES["cam_B"], atol=2e-2)
    with np.load(root / "mapanything" / "raw.npz") as raw:
        Km = expected_crop_resize_k(_K(120.0), IMG_WH, MODEL_HW[::-1])
        np.testing.assert_allclose(raw[f"{sup[0]['name']}__depth_z"], _render_depth(GOOD_POSES[0], Km), rtol=1e-2)
    metrics = json.loads((root / "mapanything" / "metrics.json").read_text(encoding="utf-8"))
    assert metrics["prepared_sha256"] == "sha256:" + hashlib.sha256((root / "prepared" / "prepared.json").read_bytes()).hexdigest()
    # 하류 읽기(load_mapanything_run)가 보조 view 사이 거리 검사에 걸리지 않는다
    from simai_ai.calibration.world_frame.inputs import load_mapanything_run, load_prepared

    run = load_mapanything_run(root / "mapanything", prepared=load_prepared(root / "prepared"))
    assert len(run.views) == 7
    assert sc.restore_baseline(root) and not (root / "prepared_base").exists()
    assert len(json.loads((root / "prepared" / "prepared.json").read_text(encoding="utf-8"))["views"]) == 4


def test_supplement_falls_back_when_too_few_frames_register(tmp_path: Path) -> None:
    root = _site(tmp_path)
    wrong = _look_at((1.0, 1.0, 0.5), (1.0, 5.0, 2.5))
    truth = {f"b{i}": (GOOD_POSES[i], _K(120.0), wrong) for i in range(3)}
    _request(root, [(f"b{i}", 10 + i) for i in range(3)])
    out = sc.run_supplement(root, infer=_FakeMapAnything(truth), person_mask=_no_people, cfg=sc.SupplementConfig())
    assert _summary_errors(out) == []
    assert out["status"] == "FALLBACK" and out["reason"] == sc.R_TOO_FEW_REGISTERED
    assert not (root / "prepared_base").exists()  # 작업 폴더는 그대로(보조 촬영 없는 맵)
    assert all(m["status"] == "NOT_REGISTERED" for m in out["media"])


def test_supplement_rejects_wide_fov_media_and_skips_without_segmenter(tmp_path: Path) -> None:
    root = _site(tmp_path)
    truth = {"ok": (GOOD_POSES[0], _K(120.0), None), "fish": (GOOD_POSES[1], _K(28.0), None)}  # 가로 화각 ≈ 141°
    _request(root, [("ok", 21), ("fish", 22)])
    out = sc.run_supplement(root, infer=_FakeMapAnything(truth), person_mask=_no_people, cfg=sc.SupplementConfig())
    states = {m["media_id"]: m["status"] for m in out["media"]}
    assert out["status"] == "APPLIED" and states == {"ok": "USED", "fish": "WIDE_FOV_REJECTED"}
    root2 = _site(tmp_path / "b")
    _request(root2, [("ok", 21)])
    out2 = sc.run_supplement(root2, infer=_FakeMapAnything(truth), person_mask=None, cfg=sc.SupplementConfig())
    assert out2["status"] == "SKIPPED" and out2["reason"] == sc.R_SEGMENTER_MISSING and _summary_errors(out2) == []


def test_gate_rejected_chunk_keeps_registered_media_distinct_from_not_registered(tmp_path: Path) -> None:
    """S15P31A101-652: 묶음이 관문에 걸려 FALLBACK 이어도 자리 잡은 프레임이 있던 파일은 REGISTERED_NOT_USED, 없던 파일만 NOT_REGISTERED."""
    root = _site(tmp_path)
    wrong = _look_at((1.0, 1.0, 0.5), (1.0, 5.0, 2.5))
    truth = {f"g{i}": (GOOD_POSES[i], _K(120.0), None) for i in range(3)}
    truth["bad"] = (GOOD_POSES[3], _K(120.0), wrong)
    _request(root, [("g0", 1), ("g1", 2), ("g2", 3), ("bad", 4)])
    cfg = sc.SupplementConfig(align_max_center_m=-1.0)  # 등록은 되지만 중심 잔차 안전선에 반드시 걸리게
    out = sc.run_supplement(root, infer=_FakeMapAnything(truth), person_mask=_no_people, cfg=cfg)
    assert _summary_errors(out) == []
    assert out["status"] == "FALLBACK" and out["reason"] == sc.R_ALIGNMENT_WORSE and out["frames_registered"] == 3
    rows = {m["media_id"]: (m["status"], m["frames_registered"]) for m in out["media"]}
    assert rows == {"g0": ("REGISTERED_NOT_USED", 1), "g1": ("REGISTERED_NOT_USED", 1), "g2": ("REGISTERED_NOT_USED", 1),
                    "bad": ("NOT_REGISTERED", 0)}
    assert not (root / "prepared_base").exists()  # 기준 맵 유지


def test_chunked_inference_keeps_fixed_views_in_every_chunk_and_drops_only_bad_chunk(tmp_path: Path) -> None:
    root = _site(tmp_path)
    wrong = _look_at((1.0, 1.0, 0.5), (1.0, 5.0, 2.5))
    truth = {f"g{i}": (GOOD_POSES[i], _K(115.0), None) for i in range(4)}
    truth.update({"b0": (GOOD_POSES[4], _K(115.0), wrong), "b1": (GOOD_POSES[5], _K(115.0), wrong)})
    _request(root, [("g0", 31), ("g1", 32), ("b0", 33), ("b1", 34), ("g2", 35), ("g3", 36)])
    fake = _FakeMapAnything(truth)
    cfg = sc.SupplementConfig(max_frames=6, chunk_frames=2, bridge_frames=1)
    out = sc.run_supplement(root, infer=fake, person_mask=_no_people, cfg=cfg)
    assert len(fake.calls) == 3 and all(call[:4] == list(FIXED) for call in fake.calls)  # 묶음마다 같은 고정 4 view
    detail = json.loads((root / "supplement" / "result.json").read_text(encoding="utf-8"))["detail"]
    reasons = [c["reason"] for c in detail["chunks"]]
    assert reasons == [None, sc.R_TOO_FEW_REGISTERED, None]  # 나쁜 묶음만 뺐다
    assert out["status"] == "APPLIED" and _summary_errors(out) == []
    states = {m["media_id"]: m["status"] for m in out["media"]}
    assert states == {"g0": "USED", "g1": "USED", "b0": "NOT_REGISTERED", "b1": "NOT_REGISTERED", "g2": "USED", "g3": "USED"}
    prep = json.loads((root / "prepared" / "prepared.json").read_text(encoding="utf-8"))
    names = [v["name"] for v in prep["views"]]
    assert len(names) == len(set(names)) == 8  # 고정 4 + 보조 4(다리 프레임 중복 없음)


def test_time_budget_stops_adding_chunks(tmp_path: Path) -> None:
    root = _site(tmp_path)
    truth = {f"g{i}": (GOOD_POSES[i], _K(115.0), None) for i in range(4)}
    _request(root, [(f"g{i}", 40 + i) for i in range(4)])
    clock = iter(range(0, 10_000, 400))
    cfg = sc.SupplementConfig(max_frames=4, chunk_frames=2, time_budget_s=500.0)
    fake = _FakeMapAnything(truth)
    out = sc.run_supplement(root, infer=fake, person_mask=_no_people, cfg=cfg, monotonic=lambda: float(next(clock)))
    assert len(fake.calls) == 1 and out["status"] == "APPLIED" and out["frames_selected"] == 2


def test_annotate_bake_records_provenance_and_keeps_only_fixed_map_cameras(tmp_path: Path) -> None:
    root = _site(tmp_path)
    truth = {"g0": (GOOD_POSES[0], _K(120.0), None)}
    _request(root, [("g0", 1)])
    out = sc.run_supplement(root, infer=_FakeMapAnything(truth), person_mask=_no_people, cfg=sc.SupplementConfig())
    sup_name = next(v["name"] for v in json.loads((root / "prepared" / "prepared.json").read_text(encoding="utf-8"))["views"]
                    if v["view_role"] == sc.VIEW_ROLE_SUPPLEMENT)
    (root / "bake.json").write_text(json.dumps({"map_cameras": {"cam_A": {}, sup_name: {}}}))
    sc.annotate_bake(root, out)
    bake = json.loads((root / "bake.json").read_text(encoding="utf-8"))
    assert list(bake["map_cameras"]) == ["cam_A"]
    assert bake["supplement_capture"]["input_role"] == sc.INPUT_ROLE and bake["supplement_capture"]["applied"]
    assert bake["supplement_capture"]["provenance"]["media"][0]["device"] == {"make": "acme", "model": "x1"}


# --------------------------------------------------------------------------- 오프라인 비교 실행기
def test_runner_media_items_infers_kind_and_unique_ids(tmp_path: Path) -> None:
    from simai_ai.scene.supplement_runner import media_items

    d = tmp_path / "m"
    d.mkdir()
    for name in ("a b.MP4", "a_b.jpg", "notes.txt"):
        (d / name).write_bytes(b"x")
    items = media_items([str(d)])
    assert [(m, k) for m, k, _ in items] == [("a_b", "VIDEO"), ("a_b-2", "IMAGE")]


def test_runner_compares_baseline_and_supplement_with_fake_stages(tmp_path: Path, monkeypatch) -> None:
    from simai_ai.scene import reconstruct_process, supplement_runner

    base = _site(tmp_path)
    (base / "images").mkdir()
    for cid in FIXED:
        (base / "images" / f"{cid}.image").write_bytes(b"jpg")
    truth = {"g0": (GOOD_POSES[0], _K(120.0), None), "g1": (GOOD_POSES[1], _K(120.0), None)}
    media_dir = tmp_path / "phone"
    media_dir.mkdir()
    for i, mid in enumerate(("g0", "g1")):
        (media_dir / f"{mid}.png").write_bytes(_noise_png(50 + i))

    class _Stages:
        def run(self, stage: str, root: Path, *, timeout_s: float, cancelled) -> None:
            if stage == "supplement":
                sc.run_supplement(root, infer=_FakeMapAnything(truth), person_mask=_no_people, cfg=sc.SupplementConfig())
            elif stage == "bake":
                (root / "scene.glb").write_bytes(b"g")
                (root / "bake.json").write_text(json.dumps({"assets": [{"kind": "GLTF_VISUAL", "path": "scene.glb"}]}))

    monkeypatch.setattr(reconstruct_process, "ProcessStages", _Stages)
    out = tmp_path / "cmp"
    code = supplement_runner.main(["--base", str(base), "--media", str(media_dir), "--out", str(out), "--reuse-base",
                                   "--device-model", "x1"])
    cmp = json.loads((out / "comparison.json").read_text(encoding="utf-8"))
    assert code == 0, cmp["errors"]
    assert cmp["supplement"]["summary"]["status"] == "APPLIED" and cmp["supplement"]["summary"]["frames_registered"] == 2
    assert cmp["supplement"]["coverage"]["views"] == 6 and cmp["baseline"]["coverage"]["views"] == 4
    assert cmp["supplement"]["coverage"]["bbox_xy_m"] == cmp["baseline"]["coverage"]["bbox_xy_m"]
    assert cmp["baseline"]["bake"]["baked"] and cmp["supplement"]["bake"]["baked"]
    assert "delta" in cmp
    rec = json.loads((out / "run_record.json").read_text(encoding="utf-8"))
    from simai_ai.providers.contract import schema_errors

    assert schema_errors(rec, {"$ref": "https://simai.dev/contracts/experiments/run_record.schema.json"}) == []
