"""MapAnything 환경 동등성: torch 2.7.1 이미지(deploy/mapanything) Run 과 통합 환경(torch 2.11.0, unified) Run 비교 (S15P31A101-250).

같은 입력·같은 가중치·같은 인자로 돈 두 Run(run_mapanything.py 출력 폴더)의 카메라·깊이·점구름 차이를 재고 합격 기준으로 판정한다.
기준과 근거는 docs/design/unified-env.md "3단계" (E-GATE-0 대조 기준 docs/design/exp-images.md 와 같은 허용 폭에 픽셀·점구름 기준을 더함).
노트북에서 결과 묶음을 받아 돌린다(numpy·scipy, GPU·torch 없음).

    uv run --project ai python ai/experiments/static_scene/compare_envs.py \\
        --base <runs/exp01/C1_room4_bf16_img_<세션>> --new <runs/exp01/C1_room4_bf16_uni_<세션>> [-o compare.json]

종료 코드: 0 PASS, 1 FAIL(기준 밖 또는 잴 수 없는 값), 2 INCOMPARABLE(입력·가중치·인자가 달라 비교가 아니다, 결과 없음).
"""

from __future__ import annotations

import argparse
import json
import sys
import zipfile
from pathlib import Path

import numpy as np

# 합격 기준. 카메라·척도·FOV·깊이 중앙값은 E-GATE-0 대조 기준(cu126 → cu128 이미지 교체)과 같은 값이다.
LIMITS = {
    "cam_dist_ratio": (0.99, 1.01),  # 카메라 쌍 거리 비(new / base)의 최소·최대
    "rot_diff_max_deg": 0.2,  # 카메라별 자세 회전 차이의 최대
    "fov_x_diff_max_deg": 0.3,  # 카메라별 가로 FOV 차이의 최대
    "scale_rel_diff": 0.01,  # metric_scaling_factor 상대 차이
    "depth_p50_rel_diff": 0.01,  # 시점별 유효 깊이 중앙값의 상대 차이
    "pixel_depth_rel_median": 0.01,  # 시점별, 두 Run 모두 유효한 픽셀의 |Δd| / d 중앙값
    "point_rel_median": 0.01,  # 같은 픽셀의 world 점 거리 / 기준 깊이 중앙값(모든 시점). 점 = 깊이 × 광선이라 깊이와 같은 예산
}
# 기록만(판정 안 함): 근거 값이 없다. 3차 세션 결과로 정한다(설계 문서 "잔여 위험").
# chamfer_*: 점구름 대칭 최근접 거리 / 기준 점구름 대각선(p1~p99). 면을 따라 미끄러진 차이(깊이 척도·전체 이동)를 잘 못 잡아
# 판정은 픽셀 대응(point_rel_median)이 하고, 이 값은 유효 영역(마스크)이 달라졌을 때의 모양 차이를 보는 기록이다
RECORD_ONLY = ("pixel_depth_rel_p95", "mask_iou_min", "chamfer_median_rel", "chamfer_p95_rel", "center_diff_max_m")
# known_from: known 모드 입력 카메라 파일(run_mapanything.py 는 그 해시를 남기지 않아 경로로 본다. 다른 파일로 돈 Run 은 비교가 아니다)
SAME_ARGS = ("mode", "known_from", "amp", "amp_dtype", "minibatch", "conf_percentile", "use_poses", "pose_scale")
RAW_KEYS = ("depth_z", "conf", "mask", "intrinsics", "camera_pose")
SAMPLE = 20000  # 점구름 비교 표본(시드 고정). 두 구름에서 각각
SEED = 0
NAN = float("nan")


class Incomparable(Exception):
    pass


def load_run(d: Path) -> dict:
    """Run 폴더를 읽는다. 읽지 못하거나 모양이 틀리면(손상·불완전 Run) Incomparable."""
    try:
        metrics = json.loads((d / "metrics.json").read_text(encoding="utf-8"))
        cams = json.loads((d / "cameras.json").read_text(encoding="utf-8"))
        with np.load(d / "raw.npz") as z:
            raw = {k: z[k] for k in z.files}
        if not isinstance(metrics, dict) or not isinstance(metrics.get("args"), dict):
            raise ValueError("metrics.json 에 args 가 없다")
        views = {v["name"]: v for v in cams["views"]}
        names = [str(x) for x in raw["names"]]
    # 입력 해석 단계에 한정한 예외(손상 zip·잘린 파일·깨진 JSON·빠진 키). 비교 계산의 프로그래밍 오류는 숨기지 않는다
    except (OSError, ValueError, KeyError, TypeError, AttributeError, EOFError, zipfile.BadZipFile) as e:
        raise Incomparable(f"{d}: Run 결과를 읽지 못함({type(e).__name__}: {e})") from e
    return {"dir": str(d), "metrics": metrics, "cams": views, "raw": raw, "names": names}


def _check_shapes(r: dict, side: str) -> None:
    """시점마다 카메라 항목과 raw 배열이 다 있고 모양이 맞는지(없으면 트레이스백이 아니라 비교 불가)."""
    for k in r["names"]:
        cam = r["cams"].get(k)
        missing = [key for key in RAW_KEYS if f"{k}__{key}" not in r["raw"]]
        if not isinstance(cam, dict) or missing or not all(x in cam for x in ("cam2world", "fov_x_deg", "model_input_hw")):
            raise Incomparable(f"{side} {k}: 카메라 항목 또는 raw 배열 {missing} 이 없다")
        g = lambda key: r["raw"][f"{k}__{key}"]  # noqa: E731
        bad = [key for key in RAW_KEYS if not (np.issubdtype(g(key).dtype, np.number) or g(key).dtype == np.bool_)]
        if bad:  # 문자열·객체 배열은 수로 바꿀 수 없다(비교 계산에서 트레이스백이 되지 않게 여기서 비교 불가)
            raise Incomparable(f"{side} {k}: 수가 아닌 raw 배열 {bad} ({[str(g(key).dtype) for key in bad]})")
        hw = g("depth_z").shape
        try:
            T = np.asarray(cam["cam2world"], float)
        except (TypeError, ValueError) as e:
            raise Incomparable(f"{side} {k}: cam2world 를 읽지 못함({e})") from e
        if (len(hw) != 2 or g("conf").shape != hw or g("mask").shape != hw or g("intrinsics").shape != (3, 3)
                or g("camera_pose").shape != (4, 4) or T.shape != (4, 4)):
            raise Incomparable(f"{side} {k}: 배열 모양이 맞지 않는다(depth {hw}, conf {g('conf').shape}, mask {g('mask').shape})")


def check_comparable(b: dict, n: dict) -> dict:
    """같은 입력·가중치·인자이고 결과 모양이 맞는지. 아니면 Incomparable. 두 환경 정보(torch·GPU)를 돌려준다."""
    mb, mn = b["metrics"], n["metrics"]
    for side, m in (("base", mb), ("new", mn)):
        if m.get("result") != "SUCCESS":
            raise Incomparable(f"{side} Run 결과가 {m.get('result')!r} (SUCCESS 만 비교한다)")
    for key in ("input_hash", "weights_hash", "images", "prepared_sha256"):
        if mb.get(key) != mn.get(key):
            raise Incomparable(f"{key} 가 다르다: {mb.get(key)!r} ≠ {mn.get(key)!r}")
    if mb.get("input_hash") is None or mb.get("weights_hash") is None:
        raise Incomparable("input_hash·weights_hash 가 없어 같은 입력·가중치인지 모른다")
    for key in SAME_ARGS:
        if mb["args"].get(key) != mn["args"].get(key):
            raise Incomparable(f"인자 {key} 가 다르다: {mb['args'].get(key)!r} ≠ {mn['args'].get(key)!r}")
    if b["names"] != n["names"] or not b["names"]:
        raise Incomparable(f"시점 목록이 다르다: {b['names']} ≠ {n['names']}")
    _check_shapes(b, "base")
    _check_shapes(n, "new")
    for name in b["names"]:
        if b["cams"][name]["model_input_hw"] != n["cams"][name]["model_input_hw"]:
            raise Incomparable(f"{name}: 모델 입력 크기가 다르다")
        if b["raw"][f"{name}__depth_z"].shape != n["raw"][f"{name}__depth_z"].shape:
            raise Incomparable(f"{name}: 깊이 배열 크기가 다르다")
    env = lambda m: {k: (m.get("gpu") or {}).get(k) for k in ("model", "torch", "cuda")}  # noqa: E731
    return {"base": env(mb), "new": env(mn), "same_gpu_model": env(mb)["model"] == env(mn)["model"]}


def _num(x) -> float:
    """유한 실수면 그 값, 아니면 NaN(None·bool·문자열·NaN·Inf). bool 은 int 의 하위형이라 따로 뺀다."""
    if isinstance(x, (bool, np.bool_)) or not isinstance(x, (int, float, np.integer, np.floating)):
        return NAN
    return float(x) if np.isfinite(x) else NAN


def worst(xs, fn=max) -> float:
    """시점별 값의 최악. 하나라도 잴 수 없으면(None·NaN·Inf) NaN: 내장 max/min 은 NaN 을 비교에서 버린다."""
    vals = [_num(x) for x in xs]
    if not vals or any(not np.isfinite(v) for v in vals):
        return NAN
    return fn(vals)


def rel_diff(a, b) -> float:
    """|b - a| / a. a·b 가 양의 유한 실수가 아니면 NaN(잴 수 없음)."""
    a, b = _num(a), _num(b)
    return abs(b - a) / a if np.isfinite(a) and np.isfinite(b) and a > 0 and b > 0 else NAN


def rot_deg(a: np.ndarray, b: np.ndarray) -> float:
    """두 회전의 차이(도). 비유한 입력은 NaN(잴 수 없음): clip 이 +inf 를 1 로 바꿔 0도로 보이게 하기 전에 거른다."""
    if not (np.all(np.isfinite(a)) and np.all(np.isfinite(b))):
        return NAN
    return float(np.degrees(np.arccos(np.clip((np.trace(a.T @ b) - 1) / 2, -1, 1))))


def unproject(depth: np.ndarray, K: np.ndarray, T: np.ndarray, mask: np.ndarray) -> np.ndarray:
    """깊이(z) → MapAnything world 점(OpenCV 카메라, cam2world). 두 Run 에 같은 식이라 픽셀 중심 규약은 판정에 영향이 없다."""
    v, u = np.nonzero(mask)
    z = depth[v, u]
    x = (u + 0.5 - K[0, 2]) / K[0, 0] * z
    y = (v + 0.5 - K[1, 2]) / K[1, 1] * z
    return np.stack([x, y, z], 1) @ T[:3, :3].T + T[:3, 3]


def view_cloud(r: dict, name: str, conf_pct: float) -> np.ndarray:
    g = lambda k: r["raw"][f"{name}__{k}"]  # noqa: E731
    depth, conf, mask = g("depth_z").astype(float), g("conf").astype(float), g("mask").astype(bool)
    mask &= np.isfinite(depth) & (depth > 0) & np.isfinite(conf)
    if not mask.any():
        return np.zeros((0, 3))
    keep = mask & (conf >= np.percentile(conf[mask], conf_pct))  # run_mapanything.py 의 fused.ply 와 같은 신뢰도 필터
    return unproject(depth, g("intrinsics").astype(float), g("camera_pose").astype(float), keep)


def chamfer(a: np.ndarray, b: np.ndarray) -> tuple[float, float]:
    from scipy.spatial import cKDTree

    rng = np.random.default_rng(SEED)
    pick = lambda x: x[rng.choice(len(x), SAMPLE, replace=False)] if len(x) > SAMPLE else x  # noqa: E731
    a, b = pick(a), pick(b)
    d = np.concatenate([cKDTree(b).query(a)[0], cKDTree(a).query(b)[0]])
    return float(np.median(d)), float(np.percentile(d, 95))


def finite_or_none(x):
    """JSON(RFC 8259)에 NaN·Inf 를 쓰지 않는다: 잴 수 없는 값은 null."""
    if isinstance(x, dict):
        return {k: finite_or_none(v) for k, v in x.items()}
    if isinstance(x, list):
        return [finite_or_none(v) for v in x]
    if isinstance(x, float) and not np.isfinite(x):
        return None
    return x


def compare(b: dict, n: dict) -> dict:
    env = check_comparable(b, n)
    names = b["names"]
    out: dict = {"base": b["dir"], "new": n["dir"], "env": env, "views": [], "values": {}, "checks": []}
    vals = out["values"]

    # 카메라: MapAnything world 는 두 Run 모두 첫 카메라가 원점이라 자세를 그대로 비교한다
    Tb = np.array([b["cams"][k]["cam2world"] for k in names], float)
    Tn = np.array([n["cams"][k]["cam2world"] for k in names], float)
    iu = np.triu_indices(len(names), 1)
    db = np.linalg.norm(Tb[:, None, :3, 3] - Tb[None, :, :3, 3], axis=-1)[iu]
    dn = np.linalg.norm(Tn[:, None, :3, 3] - Tn[None, :, :3, 3], axis=-1)[iu]
    # 기준 카메라 두 대가 한 점이면(거리 ≤ 1e-9) 비를 잴 수 없다(작은 수로 나누어 꾸미지 않는다). 쌍이 없어도(시점 1개) 잴 수 없음
    ratio = dn / db if len(db) and np.all(np.isfinite(db)) and np.all(db > 1e-9) else np.array([NAN])
    vals["cam_dist_ratio"] = [worst(ratio, min), worst(ratio, max)]
    vals["rot_diff_max_deg"] = worst(rot_deg(x, y) for x, y in zip(Tb[:, :3, :3], Tn[:, :3, :3]))
    vals["center_diff_max_m"] = worst(np.linalg.norm(Tb[:, :3, 3] - Tn[:, :3, 3], axis=-1))
    vals["fov_x_diff_max_deg"] = worst(abs(_num(b["cams"][k]["fov_x_deg"]) - _num(n["cams"][k]["fov_x_deg"])) for k in names)
    # 척도 계수는 시점마다 기록된다: 모든 시점이 양의 유한 실수여야 하고 최대 상대 차이로 판정
    vals["scale_rel_diff"] = worst(rel_diff(b["cams"][k].get("metric_scaling_factor"), n["cams"][k].get("metric_scaling_factor"))
                                   for k in names)

    # 깊이: 시점별 중앙값(metrics per_view)과 픽셀 대응(두 Run 모두 유효한 픽셀)
    pv = lambda r: {v.get("name"): v for v in r["metrics"].get("per_view") or [] if isinstance(v, dict)}  # noqa: E731
    pb, pn = pv(b), pv(n)
    p50, pix_med, pix_p95, ious, point_rel = [], [], [], [], []
    for k in names:
        gb = lambda key: b["raw"][f"{k}__{key}"]  # noqa: E731
        gn = lambda key: n["raw"][f"{k}__{key}"]  # noqa: E731
        d_b, d_n = gb("depth_z").astype(float), gn("depth_z").astype(float)
        m_b = gb("mask").astype(bool) & np.isfinite(d_b) & (d_b > 0)
        m_n = gn("mask").astype(bool) & np.isfinite(d_n) & (d_n > 0)
        both = m_b & m_n
        union = (m_b | m_n).sum()
        iou = float(both.sum() / union) if union else NAN
        rel = np.abs(d_n[both] - d_b[both]) / d_b[both]
        if both.any():  # 같은 픽셀의 점: 깊이·내부 파라미터·자세 차이를 한 번에 본다
            P_b = unproject(d_b, gb("intrinsics").astype(float), gb("camera_pose").astype(float), both)
            P_n = unproject(d_n, gn("intrinsics").astype(float), gn("camera_pose").astype(float), both)
            point_rel.append(np.linalg.norm(P_n - P_b, axis=1) / d_b[both])
        else:
            point_rel.append(np.array([NAN]))  # 공통 유효 픽셀이 없는 시점은 잴 수 없음(빼고 넘어가지 않는다)
        q = lambda p: ((p.get(k) or {}).get("depth_m") or {}).get("p50")  # noqa: E731
        view = {"name": k, "mask_iou": iou, "pixels_common": int(both.sum()),
                "pixel_depth_rel_median": float(np.median(rel)) if rel.size else NAN,
                "pixel_depth_rel_p95": float(np.percentile(rel, 95)) if rel.size else NAN,
                "depth_p50_rel_diff": rel_diff(q(pb), q(pn))}
        out["views"].append(view)
        p50.append(view["depth_p50_rel_diff"])
        pix_med.append(view["pixel_depth_rel_median"])
        pix_p95.append(view["pixel_depth_rel_p95"])
        ious.append(iou)
    vals["depth_p50_rel_diff"] = worst(p50)
    vals["pixel_depth_rel_median"] = worst(pix_med)
    vals["pixel_depth_rel_p95"] = worst(pix_p95)
    vals["mask_iou_min"] = worst(ious, min)
    allp = np.concatenate(point_rel)
    vals["point_rel_median"] = float(np.median(allp)) if allp.size and np.all(np.isfinite(allp)) else NAN

    # 점구름 모양(기록만): 시점별 신뢰도 필터를 거친 점을 합쳐 대칭 최근접 거리. 기준 점구름 대각선(p1~p99)으로 나눈다
    pct = _num(b["metrics"]["args"].get("conf_percentile", 10))
    cb = np.concatenate([view_cloud(b, k, pct) for k in names]) if np.isfinite(pct) else np.zeros((0, 3))
    cn = np.concatenate([view_cloud(n, k, pct) for k in names]) if np.isfinite(pct) else np.zeros((0, 3))
    vals["chamfer_median_rel"] = vals["chamfer_p95_rel"] = NAN
    if len(cb) and len(cn) and np.all(np.isfinite(cb)) and np.all(np.isfinite(cn)):
        diag = float(np.linalg.norm(np.percentile(cb, 99, 0) - np.percentile(cb, 1, 0)))
        med, p95 = chamfer(cb, cn)
        if diag > 0:
            vals["chamfer_median_rel"], vals["chamfer_p95_rel"] = med / diag, p95 / diag
        vals["cloud_points"] = [int(len(cb)), int(len(cn))]

    for key, lim in LIMITS.items():
        v = vals[key]
        if not np.all(np.isfinite(v)):
            ok, why = False, "잴 수 없음(모르면 통과로 두지 않는다)"
        elif isinstance(lim, tuple):
            ok, why = lim[0] <= v[0] and v[1] <= lim[1], f"[{lim[0]}, {lim[1]}]"
        else:
            ok, why = v <= lim, f"≤ {lim}"
        out["checks"].append({"name": key, "value": v, "limit": why, "ok": bool(ok)})
    out["record_only"] = {k: vals.get(k) for k in RECORD_ONLY}
    out["verdict"] = "PASS" if all(c["ok"] for c in out["checks"]) else "FAIL"
    return finite_or_none(out)


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--base", type=Path, required=True, help="torch 2.7.1 이미지 Run 폴더(metrics.json·cameras.json·raw.npz)")
    ap.add_argument("--new", type=Path, required=True, help="통합 환경 Run 폴더")
    ap.add_argument("-o", "--out", type=Path, help="결과 JSON 을 쓸 파일(있으면 덮어쓰지 않는다)")
    a = ap.parse_args(argv)
    try:
        res = compare(load_run(a.base), load_run(a.new))
    except Incomparable as e:
        res = {"base": str(a.base), "new": str(a.new), "verdict": "INCOMPARABLE", "reason": str(e)}
    text = json.dumps(res, ensure_ascii=False, indent=1, allow_nan=False)
    if a.out is not None:
        if a.out.exists():
            print(f"{a.out} 가 이미 있다(덮어쓰지 않는다)", file=sys.stderr)
            return 2
        a.out.write_text(text + "\n", encoding="utf-8")
    print(text)
    return {"PASS": 0, "FAIL": 1}.get(res["verdict"], 2)


if __name__ == "__main__":
    raise SystemExit(main())
