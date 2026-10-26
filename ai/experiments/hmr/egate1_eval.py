"""EXP-02 E-GATE-1 평가: 카메라별 HMR 결과 묶음 + 같은 시각 Tier 0 world_tracks → 지표·Run 기록 (S15P31A101-87).

노트북 CPU 에서 돈다(GPU·torch·cv2 없음). 지표 이름·정의는 docs/experiments/EXP-02_design.md 'E-GATE-1', 설계는
docs/design/hmr-multiview-aggregation.md. 집계는 `simai_ai.hmr.multiview` 를 그대로 부른다(-227 워커와 같은 함수).

    uv run python experiments/hmr/egate1_eval.py --bundle <GPU 결과 묶음> --world-tracks <world_tracks.jsonl> \\
        --calibration <보정 문서.json> --run-id egate1-<날짜>-<n> --out <결과 폴더>

입력
- 묶음(egate1_bundle 형식): 카메라별 HMR 후보(원본 픽셀 bbox, 카메라 좌표 3D 관절·메시, 몸 방향·관절 회전, 마스크)
- world_tracks JSONL(contracts/providers/world_tracks): TIER0_LOG(녹화 재생), DATASET_GT, SIM. 카메라별 bbox·COCO17 keypoint 가 있어야 한다
- 보정 문서(contracts/providers/calibration): world_tracks 와 같은 calibration_version

묶음 그림과 world_tracks 문서의 카메라 관측은 frame_ref(stream_epoch, frame_sequence)가 같거나, 없으면 촬영 시각 차가 기준 안일 때 잇는다.

출력(<out>, 이미 있으면 거절): metrics.json(지표 전체), pose_frames.jsonl(human_mesh_output.pose_frame), run_record.json(run_record 계약).
SIM·DATASET_GT 입력이면 is_upper_bound = true 이고 지표는 상한이다(제품 성능으로 쓰지 않는다, AGENTS 규칙 7).
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import subprocess
import sys
import time
from collections import Counter, defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))
import egate1_bundle as B  # noqa: E402

from simai_ai.geometry.rig import CameraRig, RigCamera  # noqa: E402
from simai_ai.geometry.triangulation import triangulate_point  # noqa: E402
from simai_ai.hmr.multiview import (  # noqa: E402
    COCO17_INDEX,
    MHR70_FEET,
    AggregationConfig,
    HmrCandidate,
    PersonAggregate,
    aggregate_frame,
    aggregate_person,
    hip_mid,
    observed_coco17,
    pose_frame_record,
    reprojection_px,
    single_view_world_keypoints,
)
from simai_ai.providers.contract import schema_errors  # noqa: E402
from simai_ai.providers.files import FileCalibrationProvider, FileWorldTrackProvider  # noqa: E402
from simai_ai.providers.provenance import build_input_provenance  # noqa: E402

EVAL_VERSION = "egate1-eval-0.1.0"
RUN_RECORD_SCHEMA_VERSION = "2.2.0"
REPO = Path(__file__).resolve().parents[3]
COCO17_L_HIP, COCO17_R_HIP = 11, 12
COCO17_ANKLES = (15, 16)
COCO17_LOWER = (11, 12, 13, 14, 15, 16)
G5_FOOT_TARGET_M = 0.05  # 명세 15.1 G5 발-지지면 5 cm
VARIANTS = ("hmr_multiview", "hmr_single_root", "hmr_single_raw", "tier0_triangulated")
TAGS = ("all", "crowded", "crossing", "occluded", "reappeared")


@dataclass(frozen=True)
class EvalConfig:
    """초기값. 근거는 설계 문서 '명세에 없어 정한 수치'."""

    join_tolerance_ms: int = 20  # 25 fps 프레임 간격(40 ms)의 절반
    min_keypoint_confidence: float = 0.3
    min_loo_joints: int = 3
    crossing_distance_m: float = 0.8
    max_triangle_area_frac: float = 0.25  # 실루엣 래스터에서 삼각형 하나의 bbox 넓이 상한(그림 넓이 비, 카메라 바로 앞 퇴화 삼각형)


# ------------------------------------------------------------------ 기하 보조


def pinhole_project(K: np.ndarray, camera: RigCamera, X_world: np.ndarray) -> np.ndarray:
    """World → 왜곡 없는 그림(K) 픽셀. 카메라 앞이 아니면 NaN."""
    Y = np.asarray(X_world, dtype=float).reshape(-1, 3) @ camera.R.T + camera.t
    out = np.full((len(Y), 2), np.nan)
    ok = np.isfinite(Y).all(axis=1) & (Y[:, 2] > 1e-6)
    out[ok] = ((Y[ok] / Y[ok, 2:3]) @ K.T)[:, :2]
    return out


def rasterize(px: np.ndarray, faces: np.ndarray, size: tuple[int, int], max_triangle_px: int) -> np.ndarray:
    """삼각형 메시 투영 → (H, W) 실루엣. 화소 중심이 삼각형 안(경계 포함)이면 칠한다. 정점이 NaN 인 면은 버린다."""
    w, h = size
    out = np.zeros((h, w), dtype=bool)
    tri = px[faces]  # (F, 3, 2)
    tri = tri[np.isfinite(tri).all(axis=(1, 2))]
    if not len(tri):
        return out
    x0 = np.clip(np.floor(tri[..., 0].min(1)), 0, w - 1).astype(np.int64)
    x1 = np.clip(np.ceil(tri[..., 0].max(1)), 0, w - 1).astype(np.int64)
    y0 = np.clip(np.floor(tri[..., 1].min(1)), 0, h - 1).astype(np.int64)
    y1 = np.clip(np.ceil(tri[..., 1].max(1)), 0, h - 1).astype(np.int64)
    bw, bh = x1 - x0 + 1, y1 - y0 + 1
    keep = (bw * bh <= max_triangle_px) & (tri[..., 0].max(1) >= 0) & (tri[..., 1].max(1) >= 0) & (tri[..., 0].min(1) < w) & (tri[..., 1].min(1) < h)
    tri, x0, y0, bw, bh = tri[keep], x0[keep], y0[keep], bw[keep], bh[keep]
    counts = bw * bh
    if not counts.sum():
        return out
    idx = np.repeat(np.arange(len(tri)), counts)
    local = np.arange(counts.sum()) - np.repeat(np.cumsum(counts) - counts, counts)
    px_x = x0[idx] + local % bw[idx]
    px_y = y0[idx] + local // bw[idx]
    cx, cy = px_x + 0.5, px_y + 0.5
    a, b, c = tri[idx, 0], tri[idx, 1], tri[idx, 2]

    def edge(p, q):
        return (q[:, 0] - p[:, 0]) * (cy - p[:, 1]) - (q[:, 1] - p[:, 1]) * (cx - p[:, 0])

    e0, e1, e2 = edge(a, b), edge(b, c), edge(c, a)
    inside = ((e0 >= 0) & (e1 >= 0) & (e2 >= 0)) | ((e0 <= 0) & (e1 <= 0) & (e2 <= 0))
    out[px_y[inside], px_x[inside]] = True
    return out


def iou(a: np.ndarray, b: np.ndarray) -> float | None:
    union = np.logical_or(a, b).sum()
    return None if union == 0 else float(np.logical_and(a, b).sum() / union)


def world_vertices(vertices_cam: np.ndarray, keypoints_cam: np.ndarray, camera: RigCamera, anchor: np.ndarray | None) -> np.ndarray:
    """메시 정점을 World 로: anchor 가 있으면 엉덩이 중점을 그 점에, 없으면 HMR 자기 translation 그대로."""
    V = np.asarray(vertices_cam, dtype=float)
    if anchor is None:
        return (V - camera.t) @ camera.R
    return (V - hip_mid(keypoints_cam)) @ camera.R + anchor


def triangulate_coco17(rig: CameraRig, track: Mapping[str, Any], cameras: Sequence[str], min_conf: float) -> np.ndarray:
    """Tier 0 2D keypoint 로 관절마다 2대 이상에서 삼각측량 (17, 3). 못 하면 NaN(기준선 '기본 골격')."""
    per_cam = {o["camera_id"]: o for o in track.get("per_camera") or ()}
    obs = {}
    for cid in cameras:
        kps = per_cam.get(cid, {}).get("keypoints_2d")
        if kps and cid in rig.cameras:
            und = rig.cameras[cid].undistort_keypoints(list(kps)[:17] + [None] * max(0, 17 - len(kps)))
            obs[cid] = (und, und.usable(min_conf))
    out = np.full((17, 3), np.nan)
    for j in range(17):
        cams = [cid for cid, (_u, ok) in obs.items() if ok[j]]
        if len(cams) < 2:
            continue
        pix = np.array([obs[cid][0].keypoints[j][:2] for cid in cams], dtype=float)
        X = triangulate_point([rig.cameras[cid] for cid in cams], pix)
        if X is not None:
            out[j] = X
    return out


# ------------------------------------------------------------------ 입력 잇기


def candidates_of(image: B.BundleImage) -> list[HmrCandidate]:
    out = []
    for p in image.people:
        a = p.arrays
        out.append(HmrCandidate(
            camera_id=image.camera_id, candidate_id=p.candidate_id, bbox=p.bbox,
            keypoints_cam=np.asarray(a["keypoints_cam"], dtype=float),
            camera_from_rig=np.asarray(a["camera_from_rig"], dtype=float) if "camera_from_rig" in a else None,
            joint_rotations=B.joint_rotations_list(a["joint_rotations"]) if "joint_rotations" in a else None,
            score=p.score,
        ))
    return out


def join(doc: Mapping[str, Any], images: Sequence[B.BundleImage], tol_ms: int) -> tuple[dict[str, B.BundleImage], Counter]:
    """문서의 카메라 관측 ↔ 묶음 그림. 카메라마다 한 장. frame_ref 가 같으면 그것, 아니면 촬영 시각 차 ≤ tol_ms 중 가장 가까운 것."""
    stats: Counter = Counter()
    by_cam: dict[str, list[B.BundleImage]] = defaultdict(list)
    for im in images:
        by_cam[im.camera_id].append(im)
    want: dict[str, tuple[dict | None, int]] = {}
    for track in doc["tracks"]:
        for o in track.get("per_camera") or ():
            ref = o.get("frame_ref")
            prev = want.get(o["camera_id"])
            if prev is not None and prev[1] != int(o["estimated_capture_time"]):
                stats["camera_mixed_times"] += 1
            want.setdefault(o["camera_id"], (ref, int(o["estimated_capture_time"])))
    out: dict[str, B.BundleImage] = {}
    for cid, (ref, t_cap) in want.items():
        cands = by_cam.get(cid, [])
        exact = [im for im in cands if ref and im.frame_ref and all(im.frame_ref.get(k) == ref.get(k) for k in ("stream_epoch", "frame_sequence"))]
        if exact:
            out[cid] = exact[0]
            stats["join_frame_ref"] += 1
            continue
        near = sorted((abs(im.capture_time_ms - t_cap), im.instant, im) for im in cands)
        if near and near[0][0] <= tol_ms:
            out[cid] = near[0][2]
            stats["join_time"] += 1
        else:
            stats["join_missing"] += 1
    return out, stats


# ------------------------------------------------------------------ 지표


def _median(xs: Sequence[float]) -> float | None:
    xs = [x for x in xs if x is not None and math.isfinite(x)]
    return float(np.median(xs)) if xs else None


def summary(xs: Sequence[float | None]) -> dict[str, float | int | None]:
    v = np.asarray([x for x in xs if x is not None and math.isfinite(x)], dtype=float)
    if not len(v):
        return {"n": 0, "p50": None, "p95": None, "mean": None}
    return {"n": int(len(v)), "p50": round(float(np.median(v)), 4), "p95": round(float(np.percentile(v, 95)), 4),
            "mean": round(float(v.mean()), 4)}


def tags_for(track: Mapping[str, Any], doc: Mapping[str, Any], rig: CameraRig, history: Mapping[str, Any], cfg: EvalConfig) -> list[str]:
    tags = ["all"]
    others = [t for t in doc["tracks"] if t["world_id"] != track["world_id"]]
    if others:
        tags.append("crowded")
    xy = np.asarray(track["position_xy"], dtype=float)
    if any(np.linalg.norm(np.asarray(t["position_xy"], dtype=float) - xy) <= cfg.crossing_distance_m for t in others):
        tags.append("crossing")
    if len(track.get("per_camera") or ()) < len(rig.cameras):
        tags.append("occluded")
    h = history.get(track["world_id"])
    if h is not None and not h["present_last"]:
        tags.append("reappeared")
    return tags


def loo_samples(person: PersonAggregate, track: Mapping[str, Any], doc: Mapping[str, Any], rig: CameraRig,
                images: Mapping[str, B.BundleImage], faces: np.ndarray | None, cfg: EvalConfig, acfg: AggregationConfig) -> list[dict]:
    """한 카메라 제외: 제외한 카메라 c 의 Tier 0 2D keypoint·마스크에 나머지로 만든 결과를 비춘다."""
    per_cam = {o["camera_id"]: o for o in track.get("per_camera") or ()}
    rows = []
    for c, obs in sorted(per_cam.items()):
        if c not in rig.cameras or not obs.get("keypoints_2d"):
            continue
        cam_c = rig.cameras[c]
        observed, _ = observed_coco17(cam_c, obs["keypoints_2d"], cfg.min_keypoint_confidence)
        others_t0 = [k for k in per_cam if k != c]
        X_t0 = triangulate_coco17(rig, track, others_t0, cfg.min_keypoint_confidence)
        anchor = 0.5 * (X_t0[COCO17_L_HIP] + X_t0[COCO17_R_HIP])
        row: dict[str, Any] = {"world_id": person.world_id, "camera_id": c}
        if not np.isfinite(anchor).all():
            row["skipped"] = "NO_ANCHOR_FROM_OTHER_CAMERAS"
            rows.append(row)
            continue
        seen = np.isfinite(observed).all(axis=1)
        tier0_ok = np.isfinite(X_t0).all(axis=1)
        common, extra = seen & tier0_ok, seen & ~tier0_ok
        others = {k: v for k, v in person.candidates.items() if k != c}
        errs: dict[str, np.ndarray] = {"tier0_triangulated": reprojection_px(cam_c, X_t0, observed)}
        sub = None
        if others:
            # 대표 선택도 c 를 뺀 기준점으로 한다(Tier 0 pelvis 는 c 를 썼을 수 있다)
            sub = aggregate_person({**track, "pelvis_xyz": anchor.tolist()}, person.t, others, rig, acfg)
            errs["hmr_multiview"] = reprojection_px(cam_c, sub.relative_keypoints[COCO17_INDEX] + anchor, observed)
            single_root, single_raw = [], []
            for k, cand in others.items():
                rel = (np.asarray(cand.keypoints_cam) - hip_mid(cand.keypoints_cam)) @ rig.cameras[k].R
                single_root.append(reprojection_px(cam_c, rel[COCO17_INDEX] + anchor, observed))
                single_raw.append(reprojection_px(cam_c, single_view_world_keypoints(cand, rig.cameras[k])[COCO17_INDEX], observed))
            errs["hmr_single_root"] = np.stack(single_root)
            errs["hmr_single_raw"] = np.stack(single_raw)
        for name, e in errs.items():
            for subset, mask in (("common", common), ("hmr_only", extra)):
                if name == "tier0_triangulated" and subset == "hmr_only":
                    continue
                if mask.sum() < (cfg.min_loo_joints if subset == "common" else 1):
                    row[f"{subset}.{name}"] = None
                    continue
                vals = np.atleast_2d(e)[:, mask]
                row[f"{subset}.{name}"] = _median([float(np.nanmedian(v)) if np.isfinite(v).any() else None for v in vals])
        row["joints_common"], row["joints_hmr_only"] = int(common.sum()), int(extra.sum())
        row.update(silhouette(person, sub, c, anchor, rig, images, faces, cfg))
        rows.append(row)
    return rows


def silhouette(person: PersonAggregate, sub: PersonAggregate | None, c: str, anchor: np.ndarray, rig: CameraRig,
               images: Mapping[str, B.BundleImage], faces: np.ndarray | None, cfg: EvalConfig) -> dict[str, float | None]:
    out: dict[str, float | None] = {"iou.hmr_multiview": None, "iou.hmr_single_raw": None, "iou.hmr_self": None}
    im = images.get(c)
    own = person.candidates.get(c)
    if faces is None or im is None or own is None or im.K_used is None:
        return out
    cam_c = rig.cameras[c]
    if not im.undistorted and cam_c.distortion_model != "NONE" and any(cam_c.distortion_coeffs):
        return out  # 편 그림이 아니면 K_used 핀홀 투영이 마스크와 맞지 않는다
    bp = next((p for p in im.people if p.candidate_id == own.candidate_id), None)
    mask = None if bp is None else B.unpack_mask(bp.arrays, im.image_size)
    if mask is None or not mask.any():
        return out

    def score(vertices_world: np.ndarray) -> float | None:
        limit = int(cfg.max_triangle_area_frac * im.image_size[0] * im.image_size[1])
        return iou(rasterize(pinhole_project(im.K_used, cam_c, vertices_world), faces, im.image_size, limit), mask)

    def verts(cid: str) -> tuple[np.ndarray, np.ndarray] | None:
        img = images.get(cid)
        cand = person.candidates[cid]
        p = None if img is None else next((q for q in img.people if q.candidate_id == cand.candidate_id), None)
        if p is None or "vertices_cam" not in p.arrays:
            return None
        return np.asarray(p.arrays["vertices_cam"], dtype=float), np.asarray(cand.keypoints_cam, dtype=float)

    mine = verts(c)
    if mine is not None:
        out["iou.hmr_self"] = score(world_vertices(*mine, cam_c, None))
    if sub is not None:
        rv = verts(sub.representative_camera)
        if rv is not None:
            out["iou.hmr_multiview"] = score(world_vertices(*rv, rig.cameras[sub.representative_camera], anchor))
        raw = []
        for k in sub.candidates:
            v = verts(k)
            if v is not None:
                raw.append(score(world_vertices(*v, rig.cameras[k], None)))
        out["iou.hmr_single_raw"] = _median(raw)
    return out


def foot_errors(person: PersonAggregate, track: Mapping[str, Any]) -> dict[str, float | None]:
    """발-ground_anchor 오차(m, 부호 있음): 접지가 VALID 이고 ground_anchor 가 있을 때만. Z=0 을 가정하지 않는다."""
    out = {"foot.hmr_multiview": None, "foot.tier0_ankle": None}
    anchor = track.get("ground_anchor_xyz")
    if anchor is None or track.get("support_surface_validity") != "VALID":
        return out
    world = person.world_keypoints
    if world is not None:
        feet = world[list(MHR70_FEET)]
        feet = feet[np.isfinite(feet).all(axis=1)]
        if len(feet):
            out["foot.hmr_multiview"] = float(feet[:, 2].min() - anchor[2])
    sk = track.get("skeleton")
    if sk and sk.get("joint_layout") == "COCO17":
        z = [sk["joints"][j][2] for j in COCO17_ANKLES
             if j < len(sk["joints"]) and sk["joints"][j] is not None and sk["validity"][j] == "VALID"]
        if z:
            out["foot.tier0_ankle"] = float(min(z) - anchor[2])
    return out


def lower_body(track: Mapping[str, Any], person: PersonAggregate) -> dict[str, int]:
    """책상 뒤 하체 누락(D-G5-02): Tier 0 골격에 없는 하체 관절 수와 그중 HMR(inferred)이 위치를 낸 수."""
    sk = track.get("skeleton")
    joints = sk["joints"] if sk and sk.get("joint_layout") == "COCO17" else []
    missing = [j for j in COCO17_LOWER if j >= len(joints) or joints[j] is None]
    world = person.world_keypoints
    filled = 0 if world is None else sum(1 for j in missing if np.isfinite(world[COCO17_INDEX[j]]).all())
    return {"lower_joints": len(COCO17_LOWER), "lower_missing_tier0": len(missing), "lower_filled_hmr": filled}


# ------------------------------------------------------------------ 실행


def evaluate(images: Sequence[B.BundleImage], faces: np.ndarray | None, docs: Sequence[Mapping[str, Any]], rig: CameraRig,
             cfg: EvalConfig = EvalConfig(), acfg: AggregationConfig = AggregationConfig()) -> dict[str, Any]:
    """평가 본체. 반환: {"rows": [...], "people": [...], "metrics": {...}, "records": [(PersonAggregate, doc, hmr_ms)]}."""
    stats: Counter = Counter()
    loo_rows, person_rows, records = [], [], []
    history: dict[str, dict] = {}
    for doc in sorted(docs, key=lambda d: d["t"]):
        joined, js = join(doc, images, cfg.join_tolerance_ms)
        stats.update(js)
        cands = [c for im in joined.values() for c in candidates_of(im)]
        agg = aggregate_frame(cands, doc, rig, acfg)
        stats["docs"] += 1
        stats["tracks_seen"] += sum(1 for t in doc["tracks"] if t.get("per_camera"))
        stats["people_aggregated"] += len(agg.people)
        stats.update(f"rejected.{r.reason.value}" for r in agg.rejected)
        stats.update(f"selection.{p.selection.value}" for p in agg.people)
        tracks = {t["world_id"]: t for t in doc["tracks"]}
        present = set()
        for person in agg.people:
            track = tracks[person.world_id]
            tags = tags_for(track, doc, rig, history, cfg)
            rows = loo_samples(person, track, doc, rig, joined, faces, cfg, acfg)
            for r in rows:
                r["t"], r["tags"] = person.t, tags
            loo_rows += rows
            single_offsets = []
            if person.pelvis_xyz is not None:
                for k, c in person.candidates.items():
                    w = single_view_world_keypoints(c, rig.cameras[k])
                    single_offsets.append(float(np.linalg.norm(0.5 * (w[9] + w[10]) - np.asarray(person.pelvis_xyz))))
            h = history.get(person.world_id)
            jitter = None
            if h is not None and h["present_last"] and h["identity_revision"] == person.identity_revision:
                d = np.linalg.norm(person.relative_keypoints[COCO17_INDEX] - h["relative"][COCO17_INDEX], axis=1)
                jitter = float(np.nanmean(d))
            person_rows.append({
                "t": person.t, "world_id": person.world_id, "tags": tags, "cameras": len(person.candidates),
                "selection": person.selection.value, "representative_camera": person.representative_camera,
                "root_offset_multiview_m": person.hmr_root_offset_m, "root_offset_single_raw_m": _median(single_offsets),
                "identity_revision": person.identity_revision, "jitter_m": jitter,
                "revision_changed": h is not None and h["identity_revision"] != person.identity_revision,
                "rep_switched": h is not None and h["present_last"] and h["rep"] != person.representative_camera,
                **foot_errors(person, track), **lower_body(track, person),
            })
            hmr_ms = (joined[person.representative_camera].hmr_ms if person.representative_camera in joined else None) or 0.0
            records.append((person, doc, hmr_ms, rows))
            history[person.world_id] = {"present_last": True, "identity_revision": person.identity_revision,
                                        "relative": person.relative_keypoints, "rep": person.representative_camera}
            present.add(person.world_id)
        for wid, h in history.items():
            if wid not in present:
                h["present_last"] = False
    return {"rows": loo_rows, "people": person_rows, "stats": dict(stats), "records": records,
            "metrics": metrics_from(loo_rows, person_rows, stats)}


def metrics_from(loo_rows: Sequence[dict], person_rows: Sequence[dict], stats: Mapping[str, int]) -> dict[str, Any]:
    m: dict[str, Any] = {"counts": dict(sorted(stats.items()))}
    usable = [r for r in loo_rows if "skipped" not in r]
    m["counts"]["loo_samples"] = len(usable)
    m["counts"]["loo_skipped_no_anchor"] = sum(1 for r in loo_rows if r.get("skipped"))
    for tag in TAGS:
        rows = [r for r in usable if tag in r["tags"]]
        block: dict[str, Any] = {}
        for v in VARIANTS:
            block[f"loo_common_px.{v}"] = summary([r.get(f"common.{v}") for r in rows])
            if v != "tier0_triangulated":
                block[f"loo_hmr_only_px.{v}"] = summary([r.get(f"hmr_only.{v}") for r in rows])
        for v in ("hmr_multiview", "hmr_single_raw", "hmr_self"):
            block[f"silhouette_iou.{v}"] = summary([r.get(f"iou.{v}") for r in rows])
        ppl = [p for p in person_rows if tag in p["tags"]]
        for v in ("hmr_multiview", "tier0_ankle"):
            vals = [p[f"foot.{v}"] for p in ppl if p[f"foot.{v}"] is not None]
            block[f"foot_anchor_abs_m.{v}"] = summary([abs(x) for x in vals])
            block[f"foot_anchor_within_g5.{v}"] = round(sum(abs(x) <= G5_FOOT_TARGET_M for x in vals) / len(vals), 4) if vals else None
        block["root_offset_m.multiview"] = summary([p["root_offset_multiview_m"] for p in ppl])
        block["root_offset_m.single_raw"] = summary([p["root_offset_single_raw_m"] for p in ppl])
        m[tag] = block
    lower = sum(p["lower_missing_tier0"] for p in person_rows)
    total = sum(p["lower_joints"] for p in person_rows)
    m["lower_body"] = {"tier0_missing_rate": round(lower / total, 4) if total else None,
                       "hmr_filled_of_missing": round(sum(p["lower_filled_hmr"] for p in person_rows) / lower, 4) if lower else None}
    by_id: dict[str, list[dict]] = defaultdict(list)
    for p in person_rows:
        by_id[p["world_id"]].append(p)
    m["continuity"] = {
        "world_ids": len(by_id),
        "reappearances": sum(1 for p in person_rows if "reappeared" in p["tags"]),
        "identity_revision_changes": sum(1 for p in person_rows if p["revision_changed"]),
        "representative_switches": sum(1 for p in person_rows if p["rep_switched"]),
        "jitter_m": summary([p["jitter_m"] for p in person_rows]),
    }
    return m


def flatten(obj: Any, prefix: str = "") -> dict[str, float | str | bool | None]:
    out: dict[str, Any] = {}
    if isinstance(obj, Mapping):
        for k, v in obj.items():
            out.update(flatten(v, f"{prefix}{k}."))
    elif obj is None or isinstance(obj, (int, float, str, bool)):
        out[prefix[:-1]] = obj
    return out


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return "sha256:" + h.hexdigest()


def git_info() -> dict[str, Any]:
    def run(*args: str) -> str | None:
        try:
            return subprocess.run(["git", "-C", str(REPO), *args], capture_output=True, text=True, timeout=30).stdout.strip() or None
        except (OSError, subprocess.SubprocessError):
            return None

    commit = run("rev-parse", "HEAD")
    status = run("status", "--porcelain")
    return {"git_commit": commit or "0000000", "branch": run("rev-parse", "--abbrev-ref", "HEAD") or "unknown",
            "dirty_worktree": bool(status)}


def run(args: argparse.Namespace) -> int:
    out = Path(args.out)
    if out.exists():
        print(f"egate1_eval: {out} 가 이미 있다(덮어쓰지 않는다)", file=sys.stderr)
        return 2
    started = int(time.time() * 1000)
    cal = FileCalibrationProvider(args.calibration)
    rig = CameraRig.from_provider(cal)
    tracks = FileWorldTrackProvider(args.world_tracks)
    docs = list(tracks.frames())
    if args.max_docs:
        docs = docs[: args.max_docs]
    images, faces = B.load_bundle(Path(args.bundle))
    meta = json.loads((Path(args.bundle) / "metrics.json").read_text(encoding="utf-8")) if (Path(args.bundle) / "metrics.json").is_file() else {}
    cfg, acfg = EvalConfig(join_tolerance_ms=args.join_tolerance_ms), AggregationConfig()
    res = evaluate(images, faces, docs, rig, cfg, acfg)

    frames_prov = meta.get("input_provenance", {}).get("frames") or {"source": "FILE", "dataset_id": f"egate1-bundle/{Path(args.bundle).name}"}
    prov = build_input_provenance(frames_prov, cal.provenance(), tracks.provenance())
    bundle_hash = sha256_file(Path(args.bundle) / B.FRAMES_FILE)
    input_hash = "sha256:" + hashlib.sha256((bundle_hash + sha256_file(Path(args.world_tracks)) + sha256_file(Path(args.calibration))).encode()).hexdigest()
    model_version = meta.get("model_version") or "unknown (묶음 metrics.json 없음)"

    out.mkdir(parents=True)
    pose_lines, bad = [], 0
    for person, doc, hmr_ms, rows in res["records"]:
        q_loo = _median([r.get("common.hmr_multiview") for r in rows])
        q_iou = _median([r.get("iou.hmr_multiview") for r in rows])
        foot = foot_errors(person, next(t for t in doc["tracks"] if t["world_id"] == person.world_id))["foot.hmr_multiview"]
        rec = pose_frame_record(person, doc, run_id=args.run_id, model_version=model_version, input_hash=input_hash,
                                input_provenance=prov, hmr_latency_ms=float(hmr_ms), lag_vs_tier0_ms=0.0, fallback=False,
                                quality={"leave_one_camera_out_reproj_px": q_loo, "silhouette_iou": q_iou,
                                         "foot_to_anchor_error_m": None if foot is None else abs(foot)})
        if rec is None:
            continue
        if schema_errors(rec, "human_mesh_output"):
            bad += 1
            continue
        pose_lines.append(json.dumps(rec, ensure_ascii=False))
    (out / "pose_frames.jsonl").write_text("\n".join(pose_lines) + ("\n" if pose_lines else ""), encoding="utf-8")
    metrics = {"eval_version": EVAL_VERSION, "config": asdict(cfg), "aggregation": asdict(acfg), "input_provenance": prov,
               "pose_frames": len(pose_lines), "pose_frames_schema_rejected": bad, **res["metrics"]}
    (out / "metrics.json").write_text(json.dumps(metrics, ensure_ascii=False, indent=1), encoding="utf-8")
    (out / "loo_rows.jsonl").write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in res["rows"]), encoding="utf-8")

    git = git_info()
    status = "SUCCEEDED" if res["metrics"]["counts"].get("loo_samples") else "FAILED"
    record = {
        "schema_version": RUN_RECORD_SCHEMA_VERSION, "run_id": args.run_id, "experiment_id": "EXP-02", "e_gate": "E-GATE-1",
        "jira_key": "S15P31A101-87", "owner": args.owner, **git,
        "env_hash": sha256_file(REPO / "ai" / "uv.lock"), "model_version": model_version,
        "config": {"path": "ai/experiments/hmr/egate1_eval.py", "sha256": sha256_file(Path(__file__)),
                   "overrides": {"join_tolerance_ms": cfg.join_tolerance_ms, "bundle": Path(args.bundle).name, "max_docs": args.max_docs}},
        "input_provenance": prov,
        "gpu": {"model": "none (CPU 평가, HMR 추론은 묶음을 만든 GPU Run 기록)", "driver": "n/a", "cuda": "n/a", "provider": "LOCAL"},
        "started_at": started, "finished_at": int(time.time() * 1000), "status": status, "experiment_state": "BENCHMARK",
        "metrics": {k: v for k, v in flatten({k: v for k, v in res["metrics"].items()}).items()},
        "results": [{"kind": "REPORT", "uri": (out / "metrics.json").as_posix(), "sha256": sha256_file(out / "metrics.json")},
                    {"kind": "HUMAN_MESH_OUTPUT", "uri": (out / "pose_frames.jsonl").as_posix(), "sha256": sha256_file(out / "pose_frames.jsonl")}],
        "notes": ("입력이 SIM·DATASET_GT 라 지표는 상한이다(제품 성능 아님). " if prov["is_upper_bound"] else "")
                 + f"묶음 {Path(args.bundle).name} ({bundle_hash}), world_tracks 문서 {len(docs)}개",
    }
    if status == "FAILED":
        record["failure_reason"] = "한 카메라 제외 표본이 없다(묶음·world_tracks 가 이어지지 않았거나 카메라 2대 미만)"
    errors = schema_errors(record, "run_record")
    if errors:
        print("egate1_eval: Run 기록이 계약을 통과하지 못했다: " + "; ".join(errors[:5]), file=sys.stderr)
        return 1
    (out / "run_record.json").write_text(json.dumps(record, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"egate1_eval: {status} 문서 {len(docs)} 사람-시각 {len(res['people'])} 한 카메라 제외 표본 "
          f"{res['metrics']['counts']['loo_samples']} → {out}")
    return 0 if status == "SUCCEEDED" else 3


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--bundle", required=True, help="GPU 결과 묶음 폴더(egate1_bundle 형식)")
    ap.add_argument("--world-tracks", required=True, help="world_tracks JSONL (TIER0_LOG·DATASET_GT·SIM)")
    ap.add_argument("--calibration", required=True, help="보정 문서(contracts/providers/calibration)")
    ap.add_argument("--run-id", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--owner", default="S15P31A101-87 Owner")
    ap.add_argument("--join-tolerance-ms", type=int, default=EvalConfig.join_tolerance_ms)
    ap.add_argument("--max-docs", type=int, default=0)
    return run(ap.parse_args(argv))


if __name__ == "__main__":
    raise SystemExit(main())
