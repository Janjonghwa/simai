"""EXP-01 텍스처 메시(texture_provenance.py·texture_mesh.py, S15P31A101-298): 작은 합성 장면(SIM, 제품 성능 아님)으로
텍스처 후보 거르기(사람 마스크·깊이 경계·가림), 구멍 판정(미터·픽셀·둘러싸임), 출처 분류·텍셀 지도, 빼고 굽기 지표, texrecon 파일 형식.
Open3D·OpenCV·Docker 없이 돈다.

반례: 꼭짓점·무게중심은 마스크 밖이고 변 중점만 사람 위인 면도 뺀다, 이 카메라 깊이가 무효인 곳을 가림으로 보지 않는다,
메시 테두리에 닿은 작은 구멍은 보간하지 않는다, 면적 경계값(같으면 허용·넘으면 거절), 가까운 카메라에서 픽셀로 크면 거절,
라벨이 있어도 최종 텍스처에 없는 면·작은 구멍인데 texrecon 이 못 채운 면은 미관측(지어내지 않음), 보간 면과 관측 면이 겹친 텍셀은
보간(승격 없음), 카메라가 엇갈린 텍셀은 카메라 모름, mve .cam 주점 0.5 픽셀 규약, 같은 정점 세 개의 면 두 개는 거절.
"""

from __future__ import annotations

import importlib.util
import json
import struct
import sys
from pathlib import Path

import numpy as np
import pytest

AI = Path(__file__).resolve().parents[3]
STATIC = AI / "experiments" / "static_scene"


def _load(name: str):
    spec = importlib.util.spec_from_file_location(f"static_scene_{name}", STATIC / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module  # dataclass 가 모듈을 sys.modules 에서 찾는다
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def tp():
    m = _load("texture_provenance")
    yield m
    sys.modules.pop(m.__name__, None)


@pytest.fixture(scope="module")
def tm():
    m = _load("texture_mesh")
    yield m
    sys.modules.pop(m.__name__, None)


# --------------------------------------------------------------------------- 합성 장면: 바닥 격자 + 위에서 내려다보는 카메라


def _K(w=400, h=300, f=300.0):
    return np.array([[f, 0, (w - 1) / 2], [0, f, (h - 1) / 2], [0, 0, 1.0]])


def _down_camera(height=3.0, x=0.0, y=0.0):
    """+Z 상방 World 에서 (x, y, height) 에 서서 바로 아래(−Z)를 보는 OpenCV 카메라."""
    T = np.eye(4)
    T[:3, :3] = np.array([[1, 0, 0], [0, -1, 0], [0, 0, -1.0]])  # 열 = 카메라 x·y·z 축(World)
    T[:3, 3] = [x, y, height]
    return T


def _grid(n=10, size=2.0):
    """바닥(Z=0) n×n 칸 격자, 칸마다 삼각형 두 개. 반환 (정점, 면, 칸 크기)."""
    s = size / n
    xs = np.arange(n + 1) * s - size / 2
    X, Y = np.meshgrid(xs, xs)
    V = np.column_stack([X.ravel(), Y.ravel(), np.zeros(X.size)])
    idx = np.arange((n + 1) ** 2).reshape(n + 1, n + 1)
    a, b, c, d = idx[:-1, :-1].ravel(), idx[:-1, 1:].ravel(), idx[1:, :-1].ravel(), idx[1:, 1:].ravel()
    F = np.concatenate([np.column_stack([a, b, c]), np.column_stack([b, d, c])])
    return V, F, s


def _view(tp, **kw):
    K, T = _K(), _down_camera()
    return tp.TexView("cam_A", K, T, (300, 400), **kw)


# --------------------------------------------------------------------------- texrecon 파일


def test_spt_roundtrip_and_truncation(tp, tmp_path):
    e = np.zeros(3, tp.SPT_DTYPE)
    e["face"], e["view"], e["cost"] = [0, 5, 7], [1, 0, 3], [0.25, 0.5, 1.0]
    costs = tp.DataCosts("0.2", 8, 4, e)
    tp.write_spt(tmp_path / "c.spt", costs)
    raw = (tmp_path / "c.spt").read_bytes()
    assert raw.startswith(b"SPT 0.2 8 4 3\n") and len(raw) == len(b"SPT 0.2 8 4 3\n") + 3 * 10
    back = tp.read_spt(tmp_path / "c.spt")
    assert (back.faces, back.views, back.version) == (8, 4, "0.2") and np.array_equal(back.entries, e)
    (tmp_path / "bad.spt").write_bytes(raw[:-3])
    with pytest.raises(ValueError, match="크기"):
        tp.read_spt(tmp_path / "bad.spt")


def test_cam_file_matches_opencv_projection(tp):
    """mve: K_mve = [[f·W, 0, W·ppx], [0, f·W·paspect, H·ppy]], 픽셀 = K_mve·X/z − 0.5 가 OpenCV 투영과 같아야 한다."""
    K = np.array([[1161.0, 0, 1151.5], [0, 1163.0, 647.5], [0, 0, 1]])
    T = _down_camera(2.5, 0.3, -0.2)
    rot = np.array([[np.cos(0.3), -np.sin(0.3), 0], [np.sin(0.3), np.cos(0.3), 0], [0, 0, 1]])
    T[:3, :3] = rot @ T[:3, :3]
    line1, line2 = tp.cam_file_text(K, T, (1296, 2304)).splitlines()
    ext = np.array(line1.split(), float)
    f, d0, d1, pa, ppx, ppy = np.array(line2.split(), float)
    assert d0 == 0 and d1 == 0
    t, R = ext[:3], ext[3:].reshape(3, 3)
    Kmve = np.array([[f * 2304, 0, 2304 * ppx], [0, f * 2304 * pa, 1296 * ppy], [0, 0, 1]])
    X = np.array([[0.5, 0.4, 0.0], [-1.0, 0.2, 0.3], [0.0, 0.0, 0.0]])
    p = (X @ R.T + t) @ Kmve.T
    mve_px = p[:, :2] / p[:, 2:] - 0.5
    cv_px, _ = tp.project(K, T, X)
    np.testing.assert_allclose(mve_px, cv_px, atol=1e-6)


def test_parse_obj_and_map_faces(tp, tmp_path):
    (tmp_path / "tex.mtl").write_text("newmtl material0000\nmap_Kd tex_material0000_map_Kd.png\n", encoding="ascii")
    (tmp_path / "tex.obj").write_text(
        "mtllib tex.mtl\nv 0 0 0\nv 1 0 0\nv 0 1 0\nv 1 1 0\nvt 0.1 0.9\nvt 0.2 0.9\nvt 0.1 0.8\nvt 0.2 0.8\n"
        "vn 0 0 1\nvn 0 0 1\nvn 0 0 1\nvn 0 0 1\nusemtl material0000\nf 2/2/2 4/4/4 3/3/3\nf 1/1/1 2/2/2 3/3/3\n", encoding="ascii")
    obj = tp.parse_obj(tmp_path / "tex.obj")
    assert obj["materials"] == ["material0000"] and obj["textures"]["material0000"] == "tex_material0000_map_Kd.png"
    assert obj["Fv"].tolist() == [[1, 3, 2], [0, 1, 2]] and obj["Ft"][0].tolist() == [1, 3, 2]
    F_in = np.array([[0, 1, 2], [1, 3, 2]])
    assert tp.map_faces(F_in, obj["Fv"]).tolist() == [1, 0]  # 정점 순서가 돌아도 같은 면
    with pytest.raises(ValueError, match="입력 메시에 없다"):
        tp.map_faces(F_in, np.array([[0, 1, 3]]))
    with pytest.raises(ValueError, match="둘 이상"):
        tp.map_faces(np.array([[0, 1, 2], [2, 1, 0]]), obj["Fv"])


# --------------------------------------------------------------------------- 텍스처 후보 거르기


def test_person_mask_excludes_faces_including_edge_midpoint_only(tp):
    V, F, _ = _grid(10, 2.0)
    K, T = _K(), _down_camera()
    person = np.zeros((300, 400), bool)
    # 칸 하나(오른쪽 위 삼각형)의 빗변 중점 근처에만 사람 픽셀: 꼭짓점·무게중심은 마스크 밖
    tri = 5 * 10 + 5 + 100  # 두 번째 묶음(b, d, c) 의 (행 5, 열 5)
    a, b, c = V[F[tri]]
    mid_bc = (b + c) / 2
    uv, _ = tp.project(K, T, mid_bc[None])
    u, v = np.rint(uv[0]).astype(int)
    person[v, u] = True
    pts, _ = tp.project(K, T, np.array([a, b, c, (a + b + c) / 3]))
    assert not person[np.rint(pts[:, 1]).astype(int), np.rint(pts[:, 0]).astype(int)].any()
    view = tp.TexView("cam_A", K, T, (300, 400), person=person)
    ok, why = tp.texture_candidates(V, F, view, np.arange(len(F)), 0.05)
    assert not ok[tri] and why["person"] >= 1
    assert ok.sum() == len(F) - why["person"] and why["outside"] == 0


def test_depth_edge_band_and_occlusion(tp):
    V, F, _ = _grid(10, 2.0)
    K, T = _K(), _down_camera()
    depth = np.full((300, 400), 3.0, np.float32)
    valid = np.ones_like(depth, bool)
    # 왼쪽 위 사분면 앞에 메시에 없는 가림체(깊이 2 m), 오른쪽 아래 사분면은 깊이 무효(모름)
    depth[:150, :200] = 2.0
    valid[150:, 200:] = False
    view = tp.TexView("cam_A", K, T, (300, 400), depth=depth, depth_valid=valid, K_depth=K)
    ok, why = tp.texture_candidates(V, F, view, np.arange(len(F)), 0.05)
    cen, _ = tp.project(K, T, V[F].mean(axis=1))
    occl = (cen[:, 0] < 199) & (cen[:, 1] < 149)
    unknown = (cen[:, 0] > 200) & (cen[:, 1] > 150)
    assert not ok[occl].any() and why["occluded"] == int(occl.sum())
    assert ok[unknown].all()  # 깊이를 모르는 곳은 가림으로 보지 않는다(기하 가시성은 texrecon 광선 검사)
    # 경계 띠: 같은 깊이 격자에 경계 표시를 주면 그 면을 뺀다
    edge = np.zeros_like(valid)
    edge[:, 195:205] = True
    view2 = tp.TexView("cam_A", K, T, (300, 400), edge=edge, K_depth=K)
    ok2, why2 = tp.texture_candidates(V, F, view2, np.arange(len(F)), 0.05)
    assert why2["depth_edge"] > 0 and ok2.sum() == len(F) - why2["depth_edge"]


def test_discontinuity_mask_uses_cut_not_confidence_holes(tp):
    d = np.full((20, 30), 3.0)
    valid = np.ones_like(d, bool)
    valid[5, 5] = False  # 신뢰도 하위로 뺀 픽셀(깊이 모델은 냈다)
    cut = np.zeros_like(valid)
    cut[:, 20] = True  # 깊이 모델이 경계로 지운 줄
    valid[:, 20] = False
    d[:, 25:] = 6.0  # 깊이 점프
    e = tp.discontinuity_mask(d, valid, 0.10, cut)
    assert e[:, 19].all() and e[:, 21].all()  # cut 옆
    assert not e[4:7, 4:7].any()  # 신뢰도 구멍 옆은 경계 아님
    assert e[:, 24].all() and e[:, 25].all()  # 점프
    assert not e[0, 0] and not e[19, 10]  # 영상 테두리는 경계 아님
    assert tp.dilate(e, 1)[:, 18].all() and not tp.dilate(e, 1)[:, 17].any()


def test_filter_costs_excludes_view_and_checks_sizes(tp):
    V, F, _ = _grid(4, 2.0)
    e = np.zeros(2 * len(F), tp.SPT_DTYPE)
    e["face"] = np.tile(np.arange(len(F)), 2)
    e["view"] = np.repeat([0, 1], len(F))
    costs = tp.DataCosts("0.2", len(F), 2, e)
    views = [_view(tp), tp.TexView("cam_B", _K(), _down_camera(3.0, 0.1, 0.0), (300, 400))]
    kept, rec = tp.filter_costs(costs, V, F, views, 0.05, exclude=(1,))
    assert (kept["view"] == 0).all() and len(kept) == len(F)
    assert rec["cam_B"]["excluded_view"] and rec["cam_B"]["kept"] == 0
    with pytest.raises(ValueError, match="크기"):
        tp.filter_costs(costs, V, F[:-1], views, 0.05)


# --------------------------------------------------------------------------- 구멍 판정


def _labels_with_hole(F, faces, n=10):
    labels = np.ones(len(F), np.int64)
    labels[faces] = 0
    return labels


def _cell(n, r, c):
    """(행 r, 열 c) 칸의 삼각형 두 개."""
    return [r * n + c, n * n + r * n + c]


def test_small_enclosed_hole_is_eligible_but_open_or_large_is_not(tp):
    n = 40
    V, F, _ = _grid(n, 2.0)  # 칸 5 cm
    views = [_view(tp)]
    rule = tp.HoleRule(max_area_m2=0.0025, max_inradius_m=0.025, max_inradius_px=8.0)
    # 가운데 칸 하나(5×5 cm = 25 cm², 경계까지 최대 거리 = 삼각형 무게중심 1.67 cm), 카메라 3 m·초점 300 px → 약 1.7 px
    labels = _labels_with_hole(F, _cell(n, 20, 20))
    elig, recs = tp.classify_holes(V, F, labels, views, rule)
    assert elig[_cell(n, 20, 20)].all() and elig.sum() == 2
    r = recs[0]
    assert r["enclosed"] and r["area_m2"] == pytest.approx(0.0025) and r["inradius_px"] < 8
    # 같은 크기인데 메시 테두리에 닿음 → 둘레 관측이 없다
    labels = _labels_with_hole(F, _cell(n, 0, 20))
    elig, recs = tp.classify_holes(V, F, labels, views, rule)
    assert not elig.any() and not recs[0]["enclosed"]
    # 칸 두 개(50 cm²) → 면적 초과
    labels = _labels_with_hole(F, _cell(n, 20, 20) + _cell(n, 20, 21))
    elig, recs = tp.classify_holes(V, F, labels, views, rule)
    assert not elig.any() and recs[0]["area_m2"] > rule.max_area_m2 and recs[0]["inradius_m"] is None
    # 면적 경계값: 같으면 허용, 조금이라도 넘으면 거절
    labels = _labels_with_hole(F, _cell(n, 20, 20))
    area = float(tp.face_areas(V, F)[_cell(n, 20, 20)].sum())
    elig, _ = tp.classify_holes(V, F, labels, views, tp.HoleRule(max_area_m2=area))
    assert elig.sum() == 2
    elig, _ = tp.classify_holes(V, F, labels, views, tp.HoleRule(max_area_m2=area * (1 - 1e-6)))
    assert not elig.any()


def test_hole_too_large_in_pixels_for_near_camera(tp):
    n = 40
    V, F, _ = _grid(n, 2.0)
    labels = _labels_with_hole(F, _cell(n, 20, 20))
    near = [tp.TexView("cam_A", _K(f=3000.0), _down_camera(0.5), (300, 400))]  # 0.5 m, 초점 3000 px → 약 88 px
    elig, recs = tp.classify_holes(V, F, labels, near, tp.HoleRule())
    assert not elig.any() and recs[0]["inradius_px"] > 8 and recs[0]["inradius_m"] < 0.025


def test_hole_metric_thresholds_follow_world_scale(tp, tm):
    """World 정렬 닮음 변환의 축척 s 만큼 미터 기준이 바뀐다(면적 s², 길이 s). 픽셀 반경은 단위가 없어 그대로다."""
    n = 40
    V, F, _ = _grid(n, 2.0)  # 메시 단위 칸 0.05
    labels = _labels_with_hole(F, _cell(n, 20, 20))
    views = [_view(tp)]
    rule = tp.HoleRule()  # 25 cm², 2.5 cm, 8 px
    rot = np.array([[0, -1, 0], [1, 0, 0], [0, 0, 1.0]])
    for s, ok in ((0.5, True), (1.0, True), (2.0, False)):
        M = np.eye(4)
        M[:3, :3] = s * rot
        M[:3, 3] = [3.0, -1.0, 0.2]
        scale = tm.unit_scale(M)
        assert scale == pytest.approx(s)
        elig, recs = tp.classify_holes(V, F, labels, views, rule, unit_scale=scale)
        assert bool(elig.any()) is ok
        assert recs[0]["area_m2"] == pytest.approx(0.0025 * s ** 2)
        if ok:
            assert recs[0]["inradius_m"] == pytest.approx(0.05 / 3 * s, abs=1e-5)
            assert recs[0]["inradius_px"] == pytest.approx(300 * (0.05 / 3) / 3.0, abs=0.01)  # 축척과 무관
    # 경계값: 면적이 기준과 같아지는 축척에서는 허용, 조금 넘으면 거절
    s_eq = np.sqrt(rule.max_area_m2 / float(tp.face_areas(V, F)[_cell(n, 20, 20)].sum()))
    assert tp.classify_holes(V, F, labels, views, rule, unit_scale=s_eq)[0].any()
    assert not tp.classify_holes(V, F, labels, views, rule, unit_scale=s_eq * (1 + 1e-6))[0].any()
    assert tm.unit_scale(None) == 1.0


def test_two_separate_holes_are_separate_components(tp):
    n = 40
    V, F, _ = _grid(n, 2.0)
    labels = _labels_with_hole(F, _cell(n, 10, 10) + _cell(n, 30, 30) + _cell(n, 30, 31) + _cell(n, 31, 30) + _cell(n, 31, 31))
    elig, recs = tp.classify_holes(V, F, labels, [_view(tp)], tp.HoleRule())
    assert len(recs) == 2
    assert elig[_cell(n, 10, 10)].all() and not elig[_cell(n, 30, 30)].any()


# --------------------------------------------------------------------------- 출처 분류(지어내지 않음)


def test_face_provenance_never_fabricates(tp):
    labels = np.array([2, 1, 0, 0, 0, 3])
    eligible = np.array([False, False, True, True, False, False])
    present = np.array([True, False, True, False, True, True])
    cls, cam = tp.face_provenance(labels, eligible, present)
    # 0: 관측(카메라 1). 1: 라벨 있으나 텍스처에 없음 → 미관측. 2: 작은 구멍·채움 → 보간. 3: 작은 구멍·못 채움 → 미관측.
    # 4: 작은 구멍 아님인데 출력에 있음(있어선 안 되는 경우) → 미관측. 5: 관측(카메라 2)
    assert cls.tolist() == [2, 0, 1, 0, 0, 2]
    assert cam.tolist() == [1, -1, -1, -1, -1, 2]


def test_face_provenance_marks_plane_faces_estimated_not_observed(tp):
    """-314: 평면에 맞춘 면은 관측 색이면 ESTIMATED_PLANE(관측 아님), 보간·미관측은 그대로(더 낮은 신뢰), 카메라는 남는다."""
    labels = np.array([2, 2, 0, 0, 1])
    eligible = np.array([False, False, True, False, False])
    present = np.array([True, True, True, False, False])
    plane = np.array([True, False, True, True, True])
    cls, cam = tp.face_provenance(labels, eligible, present, plane)
    assert cls.tolist() == [tp.ESTIMATED_PLANE, tp.OBSERVED, tp.INTERPOLATED, tp.UNOBSERVED, tp.UNOBSERVED]
    assert cam.tolist() == [1, 1, -1, -1, -1]
    assert "ESTIMATED_PLANE" in tp.SURFACE_VALUES and "ESTIMATED_PLANE" not in tp.RESERVED_VALUES


def test_provenance_maps_overlap_follows_trust_order(tp):
    """겹친 텍셀: 추정 평면(3)은 관측(2)보다 낮은 신뢰라 남고, 보간(1)보다는 높은 신뢰라 보간이 남는다(숫자 최소가 아니다)."""
    uv_px = np.array([[[0, 0], [8, 0], [8, 8]], [[0, 0], [8, 8], [0, 8]]], float)
    hw = (10, 10)
    uv = np.stack([uv_px[..., 0] / hw[1], 1.0 - uv_px[..., 1] / hw[0]], -1)
    src, cam, _ = tp.provenance_maps(uv, np.array([2, 3], np.uint8), np.array([0, 0]), hw)
    assert src[4, 4] == 3 and src[1, 6] == 2 and src[6, 1] == 3
    assert cam[4, 4] == 1 and cam[6, 1] == 1  # 추정 평면도 색은 그 카메라 관측
    src2, cam2, _ = tp.provenance_maps(uv, np.array([3, 1], np.uint8), np.array([0, -1]), hw)
    assert src2[4, 4] == 1 and cam2[4, 4] == 0 and src2[1, 6] == 3


def test_rasterize_keeps_lowest_value_on_shared_texels(tp):
    # 정사각형 [0,8]² 를 대각선으로 나눈 두 삼각형: 관측(2)과 보간(1). 대각선 위 텍셀은 보간이어야 한다
    t_obs = [[0, 0], [8, 0], [8, 8]]
    t_int = [[0, 0], [8, 8], [0, 8]]
    lo, hi, cov = tp.rasterize_uv(np.array([t_obs, t_int], float), np.array([2, 1], np.uint8), (12, 12))
    diag = [lo[i, i] for i in range(9)]
    assert diag == [1] * 9 and hi[4, 4] == 2
    assert lo[1, 6] == 2 and lo[6, 1] == 1  # 위쪽 = 관측 삼각형, 아래쪽 = 보간
    assert lo[10, 10] == 0 and not cov[10, 10] and cov[4, 4]
    # 큰 삼각형과 작은 삼각형이 섞여도(상자 크기 묶음) 같은 결과
    big = [[0, 0], [11, 0], [0, 11]]
    lo2, _, cov2 = tp.rasterize_uv(np.array([big], float), np.array([2], np.uint8), (12, 12))
    assert cov2.sum() == 78 and (lo2[cov2] == 2).all()


def test_provenance_maps_camera_conflict_is_unknown(tp):
    uv_px = np.array([[[0, 0], [8, 0], [8, 8]], [[0, 0], [8, 8], [0, 8]]], float)
    hw = (10, 10)
    uv = np.stack([uv_px[..., 0] / hw[1], 1.0 - uv_px[..., 1] / hw[0]], -1)  # obj_uv_to_px 의 역
    cls = np.array([2, 2], np.uint8)
    src, cam, _ = tp.provenance_maps(uv, cls, np.array([0, 3]), hw)
    assert src[4, 4] == 2 and cam[4, 4] == 0  # 두 카메라가 겹친 대각선
    assert cam[1, 6] == 1 and cam[6, 1] == 4
    src2, cam2, _ = tp.provenance_maps(uv, np.array([2, 1], np.uint8), np.array([0, -1]), hw)
    assert cam2[6, 1] == 0 and src2[6, 1] == 1  # 보간 텍셀에는 카메라가 없다


def test_ratios(tp):
    V, F, _ = _grid(2, 2.0)
    cls = np.array([2, 2, 1, 0, 2, 2, 0, 0], np.uint8)
    r = tp.area_ratios(V, F, cls)
    assert r == {"OBSERVED": 0.5, "INTERPOLATED": 0.125, "UNOBSERVED": 0.375, "ESTIMATED_PLANE": 0.0}
    r3 = tp.area_ratios(V, F, np.array([3, 3, 1, 0, 2, 2, 0, 0], np.uint8))
    assert r3["ESTIMATED_PLANE"] == 0.25 and r3["OBSERVED"] == 0.25
    m = np.array([[2, 1], [0, 2]], np.uint8)
    cov = np.array([[True, True], [False, True]])
    assert tp.texel_ratios([m], [cov]) == {"OBSERVED": round(2 / 3, 5), "INTERPOLATED": round(1 / 3, 5), "UNOBSERVED": 0.0,
                                          "ESTIMATED_PLANE": 0.0}
    assert tp.texel_ratios([m], [np.zeros_like(cov)])["OBSERVED"] is None


# --------------------------------------------------------------------------- 빼고 굽기 지표


def test_psnr_ssim_masked(tp):
    rng = np.random.default_rng(0)
    photo = rng.integers(0, 256, (60, 80, 3)).astype(np.uint8)
    mask = np.zeros((60, 80), bool)
    mask[10:50, 10:70] = True
    assert tp.psnr(photo, photo, mask) == float("inf") and tp.ssim(photo, photo, mask) == pytest.approx(1.0)
    noisy = photo.astype(int).copy()
    noisy[mask] = np.clip(noisy[mask] + 10, 0, 255)
    err = (noisy[mask] - photo[mask].astype(int)).astype(float)
    expect = 10 * np.log10(255 ** 2 / np.mean(err ** 2))
    assert tp.psnr(noisy.astype(np.uint8), photo, mask) == pytest.approx(expect, abs=1e-3)
    # 마스크 밖(사람 영역)이 아무리 달라도 지표는 그대로
    wild = noisy.copy()
    wild[~mask] = 255 - photo[~mask]
    assert tp.psnr(wild.astype(np.uint8), photo, mask) == tp.psnr(noisy.astype(np.uint8), photo, mask)
    assert tp.ssim(wild.astype(np.uint8), photo, mask) == tp.ssim(noisy.astype(np.uint8), photo, mask)
    assert tp.psnr(photo, photo, np.zeros_like(mask)) is None and tp.ssim(photo, photo, np.zeros_like(mask)) is None


def test_blurred_psnr_ignores_pixels_outside_mask(tp):
    """정규화 흐림: 비교 영역 밖(사람·미관측) 색이 경계에서 섞이지 않는다. 같은 그림이면 마스크 밖이 달라도 무한대."""
    rng = np.random.default_rng(1)
    photo = rng.integers(0, 256, (80, 90, 3)).astype(np.uint8)
    mask = np.zeros((80, 90), bool)
    mask[20:60, 15:70] = True
    render = photo.copy()
    render[~mask] = 255 - photo[~mask]
    assert tp.psnr_blurred(render, photo, mask, 8.0) == float("inf")
    assert tp.psnr_blurred(render, photo, np.zeros_like(mask), 8.0) is None
    # 흐림 자체: 상수 그림은 그대로, 마스크 안 상수는 마스크 밖 값과 무관
    const = np.full((30, 40), 7.0)
    np.testing.assert_allclose(tp.gaussian_blur(const, 3.0), const)
    x = np.where(mask, 50.0, 200.0)
    np.testing.assert_allclose(tp.masked_blur(x, mask, 4.0)[mask], 50.0)


def test_bilinear_and_rays(tp):
    img = np.arange(12, dtype=float).reshape(3, 4)
    assert tp.bilinear(img, np.array([1.5]), np.array([1.0]))[0, 0] == pytest.approx(5.5)
    K, T = _K(), _down_camera(3.0)
    rays = tp.pinhole_rays(K, T, (300, 400))
    hit = rays[..., :3] + rays[..., 3:] * 3.0  # z = 1 기준 방향이라 깊이 3 m 에서 바닥
    np.testing.assert_allclose(hit[..., 2], 0.0, atol=1e-9)
    uv, _ = tp.project(K, T, hit[100:101, 50:51].reshape(-1, 3))
    np.testing.assert_allclose(uv[0], [50, 100], atol=1e-6)


# --------------------------------------------------------------------------- texture_mesh 파일 쓰기


def test_write_ply_header_and_layout(tm, tmp_path):
    V = np.array([[0, 0, 0], [1, 0, 0], [0, 1, 0]], float)
    F = np.array([[0, 1, 2]])
    tm.write_ply(tmp_path / "m.ply", V, F)
    raw = (tmp_path / "m.ply").read_bytes()
    head, body = raw.split(b"end_header\n", 1)
    assert b"property list uchar int vertex_indices" in head
    assert len(body) == 3 * 12 + (1 + 12)
    assert struct.unpack_from("<B3i", body, 36) == (3, 0, 1, 2)


def test_glb_points_follow_gltf_y_up_contract(tm):
    """contracts scene_output assets.axis_convention: GLTF_VISUAL 은 glTF 규약(+Y 상방, World (x, y, z) → (x, z, −y)).
    World 정렬 M(닮음 변환)을 적용한 뒤 그 변환을 한 번만 한다. M 이 없으면(MapAnything world) 그대로."""
    M = np.eye(4)
    M[:3, :3] = 2.0 * np.array([[0, -1.0, 0], [1, 0, 0], [0, 0, 1]])  # 축척 2, Z 축 90° 회전
    M[:3, 3] = [0.5, -1.0, 0.25]
    X = np.array([[0.0, 0, 0], [1, 2, 3], [0, 0, 1]])
    W = X @ M[:3, :3].T + M[:3, 3]  # World 점
    G = tm.to_glb_points(M, X)
    np.testing.assert_allclose(G, np.column_stack([W[:, 0], W[:, 2], -W[:, 1]]))
    up = tm.to_glb_points(M, X[2:]) - tm.to_glb_points(M, X[:1])  # World +Z 방향 → glTF +Y
    assert up[0, 1] > 0 and abs(up[0, 0]) < 1e-12 and abs(up[0, 2]) < 1e-12
    assert np.isclose(np.linalg.det(tm.GLTF_Y_UP), 1.0)  # 회전(면 감김 방향 유지)
    assert tm.glb_axis_convention(M) == "GLTF_Y_UP"
    np.testing.assert_array_equal(tm.to_glb_points(None, X), X)
    assert tm.glb_axis_convention(None) != "GLTF_Y_UP"


def test_inject_extras_keeps_glb_valid(tm):
    doc = json.dumps({"asset": {"version": "2.0"}}).encode()
    doc += b" " * (-len(doc) % 4)
    binc = b"\x00" * 8
    glb = (struct.pack("<4sII", b"glTF", 2, 12 + 8 + len(doc) + 8 + len(binc)) + struct.pack("<I4s", len(doc), b"JSON") + doc
           + struct.pack("<I4s", len(binc), b"BIN\x00") + binc)
    out = tm.inject_extras(glb, {"frame": "MAPANYTHING_WORLD", "surface_source": {"values": {"OBSERVED": 2}}})
    magic, ver, total = struct.unpack_from("<4sII", out, 0)
    jlen, jtype = struct.unpack_from("<I4s", out, 12)
    assert (magic, ver, total, jtype) == (b"glTF", 2, len(out), b"JSON") and jlen % 4 == 0
    j = json.loads(out[20:20 + jlen])
    assert j["extras"]["simai"]["frame"] == "MAPANYTHING_WORLD" and j["asset"]["version"] == "2.0"
    assert out[20 + jlen:] == glb[20 + len(doc):]
    with pytest.raises(ValueError):
        tm.inject_extras(b"xxxx" + glb[4:], {})


def test_texrecon_args_map_into_container(tm, tmp_path):
    tr = tm.Texrecon("docker:simai/texrecon:dev", tmp_path)
    assert tr._arg(tmp_path / "bake-area" / "costs_filtered.spt") == "/work/bake-area/costs_filtered.spt"
    with pytest.raises(ValueError):
        tr._arg(tmp_path.parent / "elsewhere.ply")  # 결과 폴더 밖 경로는 넣지 않는다
    with pytest.raises(tm.TexreconError, match="컨테이너가 없다"):
        tr.run(tmp_path / "scene", tmp_path / "m.ply", tmp_path / "o" / "tex", [])


def test_cli_rejects_nonempty_out_and_bad_terms(tm, tmp_path):
    (tmp_path / "x").write_text("keep", encoding="utf-8")
    base = ["--ma-run", str(tmp_path / "ma"), "--prepared", str(tmp_path / "p")]
    assert tm.main([*base, "--out", str(tmp_path), "--terms", "area"]) == tm.EXIT_CONFIG
    assert (tmp_path / "x").read_text(encoding="utf-8") == "keep"
    assert tm.main([*base, "--out", str(tmp_path / "new"), "--terms", "foo"]) == tm.EXIT_CONFIG
    assert not (tmp_path / "new").exists()


def test_resolve_num_threads_caps_default_bake_threads_and_process_stages_limits_env(monkeypatch):
    """-672: 224코어 호스트·24 vCPU 컨테이너에서 num_threads=0 이 224개 스레드를 만들지 않고 가용 코어의 절반(12)으로 묶이며,
    ProcessStages 기본 환경변수(수치 연산 절반·OpenCV 1)와 reconstruct_tasks.main 진입점이 스레드를 제한한다."""
    from simai_ai.scene import cpu_budget
    from simai_ai.scene import reconstruct_tasks as tasks
    from simai_ai.scene import texture_mesh as scene_tm
    from simai_ai.scene.reconstruct_process import ProcessStages

    monkeypatch.delenv("SIMAI_BAKE_NUM_THREADS", raising=False)
    monkeypatch.setattr(cpu_budget.os, "cpu_count", lambda: 224)
    monkeypatch.setattr(cpu_budget, "cgroup_cpu_quota", lambda: 24)
    if hasattr(cpu_budget.os, "sched_getaffinity"):
        monkeypatch.setattr(cpu_budget.os, "sched_getaffinity", lambda _pid: set(range(224)))
    assert scene_tm.resolve_num_threads(0) == 12
    assert scene_tm.resolve_num_threads(8) == 8

    monkeypatch.setattr(cpu_budget, "usable_cpu_count", lambda: 2)
    assert scene_tm.resolve_num_threads(0) == 1

    monkeypatch.setenv("SIMAI_BAKE_NUM_THREADS", "6")
    assert scene_tm.resolve_num_threads(0) == 6
    monkeypatch.delenv("SIMAI_BAKE_NUM_THREADS", raising=False)

    monkeypatch.setattr(cpu_budget, "usable_cpu_count", lambda: 24)
    stage_env = ProcessStages(env={}).env
    for var in ("OMP_NUM_THREADS", "MKL_NUM_THREADS", "OPENBLAS_NUM_THREADS", "NUMEXPR_NUM_THREADS"):
        assert stage_env[var] == "12"
    assert stage_env["SIMAI_CV2_THREADS"] == "1"

    cv2_calls: list[int] = []
    import types

    monkeypatch.setitem(sys.modules, "cv2", types.SimpleNamespace(setNumThreads=cv2_calls.append))
    monkeypatch.setattr(sys, "argv", ["reconstruct_tasks", "bake", "dummy_root"])
    monkeypatch.setattr(tasks, "bake", lambda _root: None)
    assert tasks.main() == 0
    assert cv2_calls == [1]

