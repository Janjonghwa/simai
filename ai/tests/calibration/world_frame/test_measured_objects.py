"""실측 정물 축척(-100): 목록 형식, 노트북 윗판 재기(원본 윤곽·점구름 두 방법, 연·닫힌 덮개), 버림, 다시점 묶기·삼각측량,
사람 마스크는 윗변 끝점만, 퍼짐 거부, measure-objects → fix.

합성 교실(SIM, 상한, 제품 성능 아님): -102 시험의 상자 방·카메라 4대에 책상과 노트북(방향·연 각도가 다른 상자)을 놓고
모델 해상도(518×294, 실제 MapAnything 입력과 같은 크기) 깊이와 펴진 그림 해상도(×4) 검출 마스크를 광선 추적으로 만든다.
MapAnything world 는 cam_A 축 × SCALE_MA 라 정답 배율은 1 / SCALE_MA 다. YOLO 는 정답 마스크를 돌려주는 가짜로 바꿔 끼운다.
"""

from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass

import numpy as np
import pytest

from simai_ai.calibration.world_frame import cli
from simai_ai.calibration.world_frame import measured_objects as mo
from simai_ai.calibration.world_frame.inputs import MapAnythingRun, View

from .conftest import CAMS, TARGET, _box_hit, intrinsics, intrinsics_record, look, raycast

pytest.importorskip("cv2")  # 마스크 해상도 옮기기(extras calibration)

HW = (294, 518)
FACTOR = 4  # 펴진 그림 = 모델 × 4 (실제 2304 → 518 은 약 4.45)
SCALE_MA = 0.8  # MapAnything 길이 = 정답 × 0.8 → 정답 배율 1.25 (w1 임시 추정 1.3 과 비슷)
DESK_Z = 0.72
NOW = 1_792_600_000_000


@dataclass(frozen=True)
class Box:
    center: tuple
    rows: np.ndarray  # 상자 축(행) = World 방향
    half: tuple
    label: str  # 검출 이름(없으면 "")


def _yaw(psi):
    f = np.array([math.cos(psi), math.sin(psi), 0.0])
    lat = np.array([-math.sin(psi), math.cos(psi), 0.0])
    return f, lat


def laptop(x, y, psi, opening_deg=None, width=0.355, depth=0.255, label="laptop"):
    """책상 위 노트북. opening_deg None 이면 닫힘(판 width × depth). 덮개 방향 g = cosθ·앞 + sinθ·위."""
    f, lat = _yaw(psi)
    z = np.array([0, 0, 1.0])
    p = np.array([x, y, DESK_Z])
    if opening_deg is None:
        return [Box(tuple(p + z * 0.01), np.vstack([lat, f, z]), (width / 2, depth / 2, 0.01), label)]
    base = Box(tuple(p + z * 0.0075), np.vstack([lat, f, z]), (width / 2, 0.1225, 0.0075), label)
    th = math.radians(opening_deg)
    g = math.cos(th) * f + math.sin(th) * z
    hinge = p - f * 0.1225 + z * 0.015
    nrm = np.cross(lat, g)
    lid = Box(tuple(hinge + g * depth / 2), np.vstack([lat, g, nrm]), (width / 2, depth / 2, 0.003), label)
    return [base, lid]


def desk(x0, y0, x1, y1):
    return Box(((x0 + x1) / 2, (y0 + y1) / 2, DESK_Z / 2), np.eye(3), ((x1 - x0) / 2, (y1 - y0) / 2, DESK_Z / 2), "")


def _rays(R, K, hw, region=None):
    h, w = hw
    v0, v1, u0, u1 = region or (0, h, 0, w)
    v, u = np.mgrid[v0:v1, u0:u1]
    rc = np.linalg.solve(K, np.vstack([u.ravel() + 0.0, v.ravel() + 0.0, np.ones(u.size)]))
    return (R.T @ rc).T, (v1 - v0, u1 - u0)


def _hits(c, d, boxes):
    out = []
    for b in boxes:
        oc = b.rows @ (np.asarray(c) - np.asarray(b.center))
        out.append(_box_hit(oc, d @ b.rows.T, -np.asarray(b.half), np.asarray(b.half)))
    return np.vstack(out) if out else np.zeros((0, len(d)))


def render(scene, *, scale_ma=SCALE_MA):
    """scene: 상자 묶음(list[list[Box]], 묶음 하나 = 검출 하나 또는 배경). → (views, 검출 마스크, 펴진 그림 크기)."""
    K = intrinsics(HW, fov_deg=85.0)
    Kf = K.copy()
    Kf[:2, :2] *= FACTOR
    Kf[:2, 2] = FACTOR * (K[:2, 2] + 0.5) - 0.5
    hf, wf = HW[0] * FACTOR, HW[1] * FACTOR
    RA = look(CAMS["cam_A"], TARGET)
    M = np.eye(4)
    M[:3, :3] = scale_ma * RA
    M[:3, 3] = scale_ma * (-RA @ np.asarray(CAMS["cam_A"]))
    boxes = [b for group in scene for b in group]
    owner = np.array([gi for gi, group in enumerate(scene) for _ in group])
    views, dets = [], {}
    for cid, c in CAMS.items():
        R = look(c, TARGET)
        room, _ = raycast(R, c, K, HW, desk=False, person=False)
        d, _ = _rays(R, K, HW)
        depth = np.minimum(room.ravel(), _hits(c, d, boxes).min(axis=0)).reshape(HW)
        T = np.eye(4)
        T[:3, :3] = RA @ R.T
        T[:3, 3] = (M @ np.append(c, 1.0))[:3]
        views.append(View(cid, cid, scale_ma * depth, K, T, np.isfinite(depth), None))
        dets[cid] = []
        for gi, group in enumerate(scene):
            if not group[0].label:
                continue
            Xc = (_corners(group) - c) @ R.T
            if np.any(Xc[:, 2] <= 0):
                continue
            uv = (Xc @ Kf.T)[:, :2] / Xc[:, 2:3]
            u0, v0 = np.floor(uv.min(axis=0)).astype(int) - 2
            u1, v1 = np.ceil(uv.max(axis=0)).astype(int) + 3
            u0, v0, u1, v1 = max(u0, 0), max(v0, 0), min(u1, wf), min(v1, hf)
            if u1 <= u0 or v1 <= v0:
                continue
            dd, (rh, rw) = _rays(R, Kf, (hf, wf), (v0, v1, u0, u1))
            t = _hits(c, dd, boxes)
            lab = np.where(np.isfinite(t.min(axis=0)), owner[np.argmin(t, axis=0)], -1).reshape(rh, rw)
            mask = np.zeros((hf, wf), bool)
            mask[v0:v1, u0:u1] = lab == gi
            if mask.sum() > 20:
                dets[cid].append(mo.Detection(mask, 0.9, group[0].label))
    run = MapAnythingRun(tuple(views), {"run_dir": "sim", "cameras_json": {"sha256": "sha256:" + "0" * 64},
                                        "raw_npz_sha256": "sha256:" + "1" * 64})
    return run, dets, {cid: [wf, hf] for cid in CAMS}


def _corners(group):
    pts = []
    for b in group:
        for s in np.ndindex(2, 2, 2):
            sign = np.array(s) * 2 - 1
            pts.append(np.asarray(b.center) + b.rows.T @ (sign * np.asarray(b.half)))
    return np.array(pts)


DESKS = [[desk(2.4, 1.9, 5.6, 2.6)], [desk(2.4, 3.9, 5.6, 4.6)]]
# 책상 두 줄(앞줄 y 1.9~2.6, 뒷줄 3.9~4.6), 사람은 각 줄 바깥쪽에 앉아 덮개가 줄 안쪽을 본다고 가정. 방향·연 각도를 섞는다
TEAM = [
    laptop(2.9, 2.25, -math.pi / 2 + 0.1, 110),
    laptop(3.7, 2.25, -math.pi / 2 - 0.15, 120),
    laptop(4.5, 2.25, -math.pi / 2, 100),
    laptop(5.2, 2.25, -math.pi / 2 + 0.3, 115),
    laptop(2.9, 4.25, math.pi / 2, 105),
    laptop(3.7, 4.25, math.pi / 2 + 0.2, 125),
    laptop(4.5, 4.25, math.pi / 2 - 0.1, None),  # 닫힘
    laptop(5.2, 4.25, math.pi / 2 + 0.5, None),  # 닫힘
]


def _catalog(**agg):
    doc = json.loads(mo.DEFAULT_CATALOG.read_text(encoding="utf-8"))
    doc["objects"] = [o for o in doc["objects"] if o["id"] == "laptop_lid_team"]
    doc["objects"][0]["aggregation"].update(agg)
    return doc


def _measure(scene, persons=None, **agg):
    run, dets, sizes = render(scene)
    (obj,) = mo.parse_catalog(_catalog(**agg))
    doc = mo.build_evidence([obj], run, run, {obj.id: dets}, sizes, input_provenance=None,
                            catalog_record={"path": "sim"}, detectors={}, created_at=NOW, person_masks=persons)
    return doc, doc["objects"][0]


@pytest.fixture(scope="module")
def team_doc():
    return _measure(DESKS + TEAM)


# --------------------------------------------------------------------------- 목록


def test_default_catalog_is_valid_and_has_laptop_lid():
    objs, rec = mo.load_catalog()
    lid = next(o for o in objs if o.id == "laptop_lid_team")
    assert lid.edges == {"lid_width": 0.355, "lid_depth": 0.255}
    assert lid.measurement["open_edge"] == "lid_width" and lid.measurement["closed_edges"] == ["lid_width", "lid_depth"]
    assert lid.raw["measured"]["date"] == "2026-10-23" and lid.detector["classes"] == [63]
    assert lid.agreement_tolerance == 0.07 and lid.min_samples == 3
    assert rec["sha256"].startswith("sha256:") and rec["format_version"] == "0.1.0"


@pytest.mark.parametrize("patch, msg", [
    (lambda d: d.update(format="x"), "형식"),
    (lambda d: d["objects"][0]["measured"].pop("date"), "measured.date"),
    (lambda d: d["objects"][0]["edges"][0].update(length_m=0), "length_m"),
    (lambda d: d["objects"][0]["detector"].update(kind="YOLOE"), "detector.kind"),
    (lambda d: d["objects"][0]["measurement"].update(kind="CONE"), "measurement.kind"),
    (lambda d: d["objects"][0]["measurement"].update(closed_edges=["lid_depth", "lid_width"]), "긴 변"),
    (lambda d: d["objects"][0]["measurement"].update(open_edge="lid_height"), "open_edge"),
    (lambda d: d["objects"][0]["measurement"].update(min_edge_pxx=3), "모르는 키"),
    (lambda d: d["objects"][0]["aggregation"].update(agreement_tolerance=0.6), "agreement_tolerance"),
    (lambda d: d["objects"][0]["aggregation"].update(max_outlier_fraction=0.5), "과반"),
    (lambda d: d["objects"].append(dict(d["objects"][0])), "겹친다"),
])
def test_catalog_rejects_malformed(patch, msg):
    doc = _catalog()
    patch(doc)
    with pytest.raises(ValueError, match=msg):
        mo.parse_catalog(doc)


def test_catalog_measurement_overrides_config():
    doc = _catalog()
    doc["objects"][0]["measurement"]["min_edge_px"] = 20.0
    (obj,) = mo.parse_catalog(doc)
    assert mo.MEASURERS["LAPTOP_LID"].config(obj.measurement).min_edge_px == 20.0


# --------------------------------------------------------------------------- 재기·묶기


def test_recovers_known_scale_from_open_and_closed_lids(team_doc):
    doc, rec = team_doc
    assert rec["status"] == "OK", rec["aggregation"]
    (est,) = doc["estimates"]
    assert est["basis"] == "MEASURED_OBJECT" and est["label"] == "laptop_lid_team"
    assert rec["method"] == mo.IMAGE_EDGE
    assert est["scale"] == pytest.approx(1 / SCALE_MA, rel=0.02)
    # 비교용 점구름 방법도 정답 3% 안이고, 닫힌 덮개(두 변)는 점구름 방법만 잰다(원본 윤곽은 판 옆면 때문에 쓰지 않음)
    pc = rec["by_method"][mo.POINT_CLOUD]
    assert pc["status"] == "OK" and pc["aggregation"]["scale"] == pytest.approx(1 / SCALE_MA, rel=0.03)
    assert {s for g in pc["groups"] for s in g["shape"]} == {"OPEN", "CLOSED"}
    assert rec["rejected_by_reason"].get("IMG_CLOSED_LID")
    # 이 배율로 본 카메라 높이 = 정답(2.55~2.65 m) 3% 안
    for cid, h in rec["camera_heights_m"].items():
        assert h == pytest.approx(CAMS[cid][2], rel=0.03)


def test_same_laptop_in_several_cameras_counts_once(team_doc):
    doc, rec = team_doc
    (est,) = doc["estimates"]
    assert est["sample_count"] == rec["aggregation"]["samples"] - rec["aggregation"]["excluded"]
    assert rec["aggregation"]["samples"] <= len(TEAM)  # 독립 표본은 노트북 수를 넘지 않는다
    multi = [g for g in rec["groups"] if len({m.split("#")[0] for m in g["members"]}) > 1]
    assert multi, "여러 카메라에서 채택된 노트북이 하나는 있어야 한다"
    for g in multi:
        assert g["member_spread"] < 0.05  # 같은 노트북은 카메라끼리 가깝다
        # 원본 끝점을 카메라끼리 삼각측량하면 그 값을 쓴다(합성은 자세가 정확해 광선이 1 cm 안에서 만난다)
        assert g["source"] == "TRIANGULATED" and max(g["triangulation"]["residual_ma"]) < 0.01
    pos = np.array([g["position_ma"] for g in rec["groups"]])
    dist = np.linalg.norm(pos[:, None] - pos[None], axis=-1) + np.eye(len(pos)) * 9
    assert dist.min() > 0.2  # 묶음끼리는 묶는 거리보다 멀다


def test_ambiguous_shapes_are_dropped():
    scene = DESKS + [
        laptop(2.9, 2.25, -math.pi / 2 + 0.1, 20),  # 반쯤 닫힘(높이 폭 0.05~0.10 m 의 사이)
        [Box((4.2, 2.3, DESK_Z + 0.45), np.vstack([[1, 0, 0], [0, 0, 1.0], [0, 1.0, 0]]), (0.25, 0.15, 0.01),
             "laptop")],  # 책상 위 모니터(바닥 판 없음)
        laptop(5.0, 2.25, -math.pi / 2, 110, width=0.12, depth=0.2),  # 폭이 좁은 판(윗변 화면 길이 15 px 미만)
    ]
    _, rec = _measure(scene)
    assert rec["accepted"] == 0 and rec["status"] == "REJECTED"
    by_index = {}
    for m in rec["measurements"]:
        by_index.setdefault(m["index"], set()).add(m["reason"])
    assert by_index[0] == {"AMBIGUOUS_OPENING"}, by_index  # 카메라 4대 모두
    assert by_index[1] == {"BASE_NOT_SEEN"}, by_index  # 세운 화면만(자판 없음) = 모니터
    assert by_index[2] <= {"NOT_LAPTOP_SHAPE", "IMG_EDGE_TOO_SHORT", "IMG_EDGE_NOT_FOUND", "TOO_FEW_POINTS",
                           "TOO_FEW_EDGE_POINTS"}, by_index
    assert "NOT_LAPTOP_SHAPE" in by_index[2], by_index  # 가로/덮개 높이 0.6 (목록 1.39)


def test_monitor_sized_lid_with_keyboard_is_not_a_laptop_lid():
    # 자판이 있어도 덮개 비율이 크게 다르면(가로 0.6 × 높이 0.22 = 2.7) 버린다
    _, rec = _measure(DESKS + [laptop(3.7, 2.25, -math.pi / 2, 105, width=0.6, depth=0.22)], min_samples=1)
    reasons = {m["reason"] for m in rec["measurements"]}
    assert "NOT_LAPTOP_SHAPE" in reasons and rec["accepted"] == 0, reasons


def test_occluded_lid_end_is_dropped():
    # cam_A 가까운 책상의 연 노트북(화면 길이 넉넉)
    near = [[desk(1.3, 1.2, 2.3, 1.9)]]
    psi, th = -math.pi / 2, math.radians(110)
    lap = laptop(1.8, 1.55, psi, 110)
    _, clear = _measure(near + [lap], min_samples=1)
    assert ("cam_A", mo.ACCEPTED) in {(m["camera_id"], m["status"]) for m in clear["measurements"]}
    # 덮개 윗변 한쪽 끝 3 cm 를 cam_A 쪽에서 가리는 작은 상자(손·컵 등)를 놓으면 cam_A 에서 버린다
    f, lat = _yaw(psi)
    hinge = np.array([1.8, 1.55, DESK_Z]) - f * 0.1225 + np.array([0, 0, 0.015])
    corner = hinge + (math.cos(th) * f + math.sin(th) * np.array([0, 0, 1.0])) * 0.255 + lat * (0.1775 - 0.03)
    cam = np.asarray(CAMS["cam_A"])
    post = [Box(tuple(cam + 0.85 * (corner - cam)), np.eye(3), (0.03, 0.03, 0.03), "")]
    _, occl = _measure(near + [lap, post], min_samples=1)
    a = next(m for m in occl["measurements"] if m["camera_id"] == "cam_A")
    assert a["status"] == mo.REJECTED and a["reason"] == "END_OCCLUDED", a


def test_band_covering_part_of_edge_drops_point_cloud_only():
    # 덮개 절반의 깊이를 못 쓰면(MapAnything 마스크 밖·검은 화면 등) 점구름 띠가 변 일부만 잡는다 → 끝점 너머로 마스크가
    # 이어져 점구름 방법은 버린다. 원본 윤곽 방법은 윤곽에서 끝점을 찾으므로 그대로 잰다(깊이 번짐 영향을 줄이는 목적)
    from simai_ai.calibration.world_frame.inputs import mask_to_model_res

    run, dets, sizes = render([[desk(1.3, 1.2, 2.3, 1.9)], laptop(1.8, 1.55, -math.pi / 2, 110)])
    floor, _ = mo.floor_plane(run)
    views = list(run.views)
    a = next(i for i, v in enumerate(views) if v.camera_id == "cam_A")
    v = views[a]
    m = mask_to_model_res(dets["cam_A"][0].mask, HW, min_fraction=0.5) & v.valid
    X, uv = v.points(m)
    h = floor.distance(X)
    # 윗변 띠(위 5 cm)의 왼쪽 절반 깊이를 지운다
    left = uv[:, 0] < np.median(uv[uv[:, 1] < np.percentile(uv[:, 1], 30), 0])
    cut = (h >= h.max() - 0.05) & left
    bad = np.zeros_like(m)
    bad[uv[cut, 1].astype(int), uv[cut, 0].astype(int)] = True
    views[a] = View(v.camera_id, v.name, v.depth, v.K, v.cam2world, v.valid & ~bad, None)
    run = MapAnythingRun(tuple(views), run.record)
    (obj,) = mo.parse_catalog(_catalog(min_samples=1))
    doc = mo.build_evidence([obj], run, run, {obj.id: dets}, sizes, input_provenance=None, catalog_record={},
                            detectors={}, created_at=NOW)
    rec = next(x for x in doc["objects"][0]["measurements"] if x["camera_id"] == "cam_A")
    assert rec["methods"][mo.POINT_CLOUD]["reason"] == "EDGE_INCOMPLETE", rec["methods"]
    assert rec["methods"][mo.IMAGE_EDGE]["status"] == mo.ACCEPTED, rec["methods"]
    assert rec["methods"][mo.IMAGE_EDGE]["scale"] == pytest.approx(1 / SCALE_MA, rel=0.03)


def test_person_mask_drops_only_when_it_touches_an_edge_end():
    near = [[desk(1.3, 1.2, 2.3, 1.9)]]
    lap = laptop(1.8, 1.55, -math.pi / 2, 110)
    doc, rec = _measure(near + [lap], min_samples=1)
    a = next(m for m in rec["measurements"] if m["camera_id"] == "cam_A")
    assert a["status"] == mo.ACCEPTED
    (u0, v0), (u1, v1) = a["image_edge"]["ends_px"]
    hf, wf = HW[0] * FACTOR, HW[1] * FACTOR
    # 덮개 가운데 위쪽에 사람(끝점에서 떨어짐) → 그대로 채택
    mid = np.zeros((hf, wf), bool)
    um, vm = int((u0 + u1) / 2), int((v0 + v1) / 2)
    mid[max(vm - 60, 0):vm - 8, um - 10:um + 10] = True
    _, rec = _measure(near + [lap], persons={"cam_A": mid}, min_samples=1)
    assert next(m for m in rec["measurements"] if m["camera_id"] == "cam_A")["status"] == mo.ACCEPTED
    # 끝점에 닿는 사람 → 원본 방법은 버림(END_ON_PERSON)
    end = np.zeros((hf, wf), bool)
    end[int(v0) - 30:int(v0) + 2, int(u0) - 2:int(u0) + 30] = True
    _, rec = _measure(near + [lap], persons={"cam_A": end}, min_samples=1)
    a = next(m for m in rec["measurements"] if m["camera_id"] == "cam_A")
    assert a["methods"][mo.IMAGE_EDGE]["reason"] == "END_ON_PERSON", a["methods"]


def test_different_size_laptops_excluded_or_rejected():
    # 앞줄 두 대를 다른 기종(가로 0.30 × 0.21)으로 바꾼다: 정답과 1.18 배 다른 배율
    other = [laptop(2.9, 2.25, -math.pi / 2 + 0.1, 110, width=0.30, depth=0.21),
             laptop(4.5, 2.25, -math.pi / 2, 100, width=0.30, depth=0.21)]
    scene = DESKS + [other[0], TEAM[1], other[1]] + TEAM[3:]
    doc, rec = _measure(scene)
    agg = rec["aggregation"]
    assert agg["excluded"] >= 1 and agg["spread_all"] > 0.07
    # 벗어난 표본이 상한(34%) 안이면 빼고 나머지(팀 노트북)로 낸다
    assert rec["status"] == "OK" and agg["excluded"] <= 0.34 * agg["samples"]
    assert doc["estimates"][0]["scale"] == pytest.approx(1 / SCALE_MA, rel=0.02)
    # 같은 장면에서 상한을 낮추면 근거를 내지 않는다(지어내지 않음, 다음 근거로)
    doc, rec = _measure(scene, max_outlier_fraction=agg["excluded"] / agg["samples"] - 0.01)
    assert doc["estimates"] == [] and rec["status"] == "REJECTED" and "SPREAD" in rec["reason"]
    # 잔여 위험(설계 문서): 보이는 노트북 과반이 같은 다른 기종이면 그 크기로 틀린 근거를 낸다(팀 노트북 지정 전까지)
    doc, rec = _measure(DESKS + [laptop(x, y, p, a, width=0.30, depth=0.21) for x, y, p, a in (
        (2.9, 2.25, -math.pi / 2 + 0.1, 110), (3.7, 2.25, -math.pi / 2 - 0.15, 120), (4.5, 2.25, -math.pi / 2, 100),
        (5.2, 2.25, -math.pi / 2 + 0.3, 115))] + TEAM[4:])
    assert doc["estimates"][0]["scale"] == pytest.approx(0.355 / 0.30 / SCALE_MA, rel=0.04)


# --------------------------------------------------------------------------- 묶음 규칙(순수 함수)


def _m(scale, pos, cam="cam_A", i=0):
    return mo.Measurement(cam, i, mo.ACCEPTED, None, {}, mo.OPEN, np.asarray(pos, dtype=float), {mo.IMAGE_EDGE: scale})


def _obj(**agg):
    (o,) = mo.parse_catalog(_catalog(**agg))
    return o


def test_grouping_merges_close_positions_only():
    ms = [_m(1.0, (0, 0, 0)), _m(1.02, (0.1, 0, 0), "cam_B"), _m(1.3, (0.65, 0, 0)), _m(1.1, (0, 0, 1), "cam_C"),
          mo.Measurement("cam_D", 0, mo.REJECTED, "TRUNCATED", {})]
    groups = mo.group_measurements(ms, 0.2)
    assert sorted(len(g) for g in groups) == [1, 1, 2]


@pytest.mark.parametrize("values, ok, excluded", [
    ([1.0, 1.02, 0.99, 1.01], True, 0),
    ([1.0, 1.02, 0.99, 1.01, 1.2], True, 1),  # 1/5 벗어남 ≤ 34%
    ([1.0, 1.02, 1.01, 1.2, 1.25], False, 2),  # 2/5 벗어남 > 34%
    ([1.0, 1.01], False, 0),  # 표본 부족
    ([1.0, 1.069, 0.935], True, 0),  # 경계 안(중앙값에서 6.9%·6.5%)
    ([1.0, 1.08, 0.92, 1.0, 1.0, 1.0], True, 2),
    ([1.0, 1.01, 1.3], False, 1),  # 1/3 은 상한 안이지만 빼면 2개 < 3개
])
def test_aggregate_rules(values, ok, excluded):
    s, n, rec = mo.aggregate(_obj(), values)
    assert (s is not None) is ok, rec
    assert rec.get("excluded", 0) == excluded
    if ok:
        assert n == len(values) - excluded and rec["spread"] <= 0.07


def test_choose_scale_takes_laptop_evidence_as_one_object(team_doc):
    from simai_ai.calibration.criteria import ScaleBasis, ScaleEstimate
    from simai_ai.calibration.world_frame.frame import choose_scale

    doc, _ = team_doc
    est = [ScaleEstimate(ScaleBasis(e["basis"]), e["scale"], e["sample_count"], e["label"]) for e in doc["estimates"]]
    choice = choose_scale(est)
    assert choice.basis == "MEASURED_OBJECT" and choice.record["notes"]["MEASURED_OBJECT"]["objects"] == 1


# --------------------------------------------------------------------------- 실행 도구: measure-objects → fix


def _write_run(tmp_path, scene):
    """render 결과를 MapAnything 결과 폴더·입력 폴더(펴진 그림 + 사람 마스크 없음)로 쓴다."""
    import cv2

    run, dets, sizes = render(scene)
    run_dir, prep = tmp_path / "run", tmp_path / "prepared"
    run_dir.mkdir()
    prep.mkdir()
    raw, views, pviews = {}, [], []
    for k, v in enumerate(run.views):
        raw[f"{v.name}__depth_z"] = v.depth.astype(np.float32)
        raw[f"{v.name}__conf"] = np.ones(HW, np.float32) * 2
        raw[f"{v.name}__mask"] = np.isfinite(v.depth)
        raw[f"{v.name}__intrinsics"] = v.K.astype(np.float32)
        raw[f"{v.name}__camera_pose"] = v.cam2world.astype(np.float32)
        views.append({"name": v.name, "model_input_hw": list(HW), "cam2world": v.cam2world.tolist()})
        img = np.zeros((HW[0] * FACTOR, HW[1] * FACTOR, 3), np.uint8)
        img[:8, :8] = k * 40 + 10  # 가짜 검출기가 카메라를 알아보는 표시
        cv2.imwrite(str(prep / f"{v.name}.png"), img)
        pviews.append({"name": v.name, "camera_id": v.camera_id, "image": f"{v.name}.png", "mask": None,
                       "image_size": sizes[v.camera_id]})
    np.savez_compressed(run_dir / "raw.npz", names=np.array([v["name"] for v in views]), **raw)
    (run_dir / "cameras.json").write_text(json.dumps({"run_id": "SIM_lids", "views": views,
                                                      "convention": "cam2world 4x4, OpenCV, MapAnything world, m"}),
                                          encoding="utf-8")
    (prep / "prepared.json").write_text(json.dumps({
        "format": "simai.mapanything_inputs", "format_version": "0.1.0",
        "input_provenance": {"frames": {"source": "SIM", "dataset_id": "sim/lids"}, "is_upper_bound": True},
        "person_masks": False, "views": pviews}), encoding="utf-8")
    sha = "sha256:" + hashlib.sha256((prep / "prepared.json").read_bytes()).hexdigest()
    (run_dir / "metrics.json").write_text(json.dumps({"run_id": "SIM_lids", "result": "SUCCESS",
                                                      "args": {"mode": "prepared"}, "prepared_sha256": sha}),
                                          encoding="utf-8")
    by_marker = {k * 40 + 10: dets[v.camera_id] for k, v in enumerate(run.views)}
    return run_dir, prep, by_marker


class FakeDetector:
    def __init__(self, by_marker):
        self.by_marker = by_marker

    def describe(self):
        return {"kind": "FAKE"}

    def detect(self, image):
        return self.by_marker[int(image[0, 0, 0])]


def test_cli_measure_objects_then_fix_uses_measured_object(tmp_path, monkeypatch):
    run_dir, prep, by_marker = _write_run(tmp_path, DESKS + TEAM)
    monkeypatch.setitem(mo.DETECTORS, "ULTRALYTICS_SEG", lambda spec, wdir, dev: FakeDetector(by_marker))
    ev = tmp_path / "evidence.json"
    code = cli.main(["measure-objects", "--run-dir", str(run_dir), "--prepared", str(prep), "--out", str(ev),
                     "--object", "laptop_lid_team", "--debug-dir", str(tmp_path / "diag")], now_ms=NOW)
    assert code == cli.EXIT_OK
    assert sorted(f.name for f in (tmp_path / "diag").iterdir()) == [f"det_laptop_lid_team_{c}.jpg" for c in sorted(CAMS)]
    doc = json.loads(ev.read_text(encoding="utf-8"))
    assert doc["format"] == "simai.scale_evidence" and doc["input_provenance"]["frames"]["source"] == "SIM"
    (est,) = doc["estimates"]
    assert est["scale"] == pytest.approx(1 / SCALE_MA, rel=0.03)
    # 덮어쓰지 않는다
    assert cli.main(["measure-objects", "--run-dir", str(run_dir), "--prepared", str(prep), "--out", str(ev)],
                    now_ms=NOW) == cli.EXIT_CONFIG
    intr = tmp_path / "intr.json"
    intr.write_text(json.dumps(intrinsics_record(size=(HW[1] * FACTOR, HW[0] * FACTOR))), encoding="utf-8")
    out = tmp_path / "wf.json"
    code = cli.main(["fix", "--run-dir", str(run_dir), "--intrinsics", str(intr), "--prepared", str(prep),
                     "--scale-evidence", str(ev), "--out", str(out)], now_ms=NOW)
    rec = json.loads(out.read_text(encoding="utf-8"))
    assert rec["evidence"]["scale"]["basis"] == "MEASURED_OBJECT"
    assert rec["evidence"]["scale"]["scale"] == pytest.approx(est["scale"])
    assert rec["inputs"]["scale_evidence"]["input_provenance"]["frames"]["source"] == "SIM"
    if code == cli.EXIT_OK:  # 축은 이 시험의 관심이 아니다(합성 방은 벽 두 방향이라 보통 OK)
        assert rec["calibration"]["scale_basis"] == "MEASURED_OBJECT"


def test_fix_refuses_scale_evidence_from_another_run(tmp_path):
    run_dir, prep, _ = _write_run(tmp_path, DESKS)
    ev = tmp_path / "evidence.json"
    ev.write_text(json.dumps({"format": "simai.scale_evidence", "cameras_json_sha256": "sha256:" + "a" * 64,
                              "estimates": [{"basis": "MEASURED_OBJECT", "scale": 1.2, "sample_count": 3,
                                             "label": "laptop_lid_team"}]}), encoding="utf-8")
    intr = tmp_path / "intr.json"
    intr.write_text(json.dumps(intrinsics_record(size=(HW[1] * FACTOR, HW[0] * FACTOR))), encoding="utf-8")
    code = cli.main(["fix", "--run-dir", str(run_dir), "--intrinsics", str(intr), "--prepared", str(prep),
                     "--scale-evidence", str(ev), "--out", str(tmp_path / "wf.json")], now_ms=NOW)
    assert code == cli.EXIT_CONFIG and not (tmp_path / "wf.json").exists()


def test_cli_measure_objects_without_estimate_exits_3(tmp_path, monkeypatch):
    run_dir, prep, by_marker = _write_run(tmp_path, DESKS + TEAM[:1])
    monkeypatch.setitem(mo.DETECTORS, "ULTRALYTICS_SEG", lambda spec, wdir, dev: FakeDetector(by_marker))
    ev = tmp_path / "evidence.json"
    code = cli.main(["measure-objects", "--run-dir", str(run_dir), "--prepared", str(prep), "--out", str(ev)], now_ms=NOW)
    doc = json.loads(ev.read_text(encoding="utf-8"))
    assert code == cli.EXIT_NOT_FIXED and doc["estimates"] == [] and "TOO_FEW_SAMPLES" in doc["objects"][0]["reason"]
