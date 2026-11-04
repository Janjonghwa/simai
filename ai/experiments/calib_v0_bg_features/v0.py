"""무보행 보정 V0 (S15P31A101-437): 배경 특징점으로 현재 보정을 잰다. 보정을 바꾸거나 올리지 않는다.

순서: 녹화 → 카메라별 중앙값 배경(사람 제거) → 왜곡 펴기(같은 K) → ALIKED/SuperPoint + LightGlue 6쌍
→ Essential RANSAC(초기 자세 게이트 없음) → 쌍별 상대 자세와 현재 보정 비교 → 다시점 트랙 → 현재 보정으로 삼각측량
→ 카메라별 재투영 오차 → 한 카메라씩 빼고(나머지로 삼각측량) PnP 로 그 카메라 자세를 다시 구해 현재 보정과 차이.

보정 규약: OPENCV_WORLD_TO_CAMERA (x_cam = R X + t), World 미터·+Z 상방.
실행: <venv python> v0.py --rec <rec 폴더> --cal <calibration.json> --out <출력 폴더> [--features aliked superpoint]
"""
from __future__ import annotations

import argparse
import itertools
import json
from pathlib import Path

import av
import cv2
import numpy as np
import torch
from lightglue import ALIKED, LightGlue, SuperPoint
from lightglue.utils import rbd

CAMS = ("cam_A", "cam_B", "cam_C", "cam_D")
MIN_EDGE_INLIERS = 50
MIN_TRI_ANGLE_DEG = 2.0


def load_cal(path: Path) -> dict:
    doc = json.loads(path.read_text(encoding="utf-8"))
    assert doc["extrinsic_convention"] == "OPENCV_WORLD_TO_CAMERA"
    out = {}
    for c in doc["cameras"]:
        out[c["camera_id"]] = {
            "K": np.array(c["K"], float), "dist": np.array(c["distortion"]["coeffs"], float),
            "R": np.array(c["R"], float), "t": np.array(c["t"], float), "size": tuple(c["image_size"]),
        }
    return doc["calibration_version"], out


def median_background(video: Path, n: int = 48) -> np.ndarray:
    # 프레임을 전부 메모리에 올리지 않는다(150 s 원본이면 카메라당 ~20 GB → 11-03 노트북 강제 종료).
    # 1차: 개수만 센다(변환 없음). 2차: 고른 n 장만 BGR 로 남긴다.
    with av.open(str(video), format="h264") as cont:
        total = sum(1 for _ in cont.decode(video=0))
    keep = set(np.linspace(0, total - 1, n).astype(int).tolist())
    picked = []
    with av.open(str(video), format="h264") as cont:
        for i, f in enumerate(cont.decode(video=0)):
            if i in keep:
                picked.append(f.to_ndarray(format="bgr24"))
    return np.median(np.stack(picked), axis=0).astype(np.uint8)


def to_tensor(img_bgr: np.ndarray, dev: str) -> torch.Tensor:
    rgb = cv2.cvtColor(img_bgr, cv2.COLOR_BGR2RGB)
    return torch.from_numpy(rgb).permute(2, 0, 1).float().div(255).to(dev)


def rot_deg(Ra: np.ndarray, Rb: np.ndarray) -> float:
    c = (np.trace(Ra.T @ Rb) - 1) / 2
    return float(np.degrees(np.arccos(np.clip(c, -1, 1))))


def center(c: dict) -> np.ndarray:
    return -c["R"].T @ c["t"]


def triangulate(obs: list[tuple[str, np.ndarray]], cal: dict) -> np.ndarray:
    A = []
    for cid, uv in obs:
        P = cal[cid]["K"] @ np.hstack([cal[cid]["R"], cal[cid]["t"][:, None]])
        A.append(uv[0] * P[2] - P[0])
        A.append(uv[1] * P[2] - P[1])
    X = np.linalg.svd(np.array(A))[2][-1]
    return X[:3] / X[3]


def project(X: np.ndarray, c: dict) -> tuple[np.ndarray, float]:
    xc = c["R"] @ X + c["t"]
    uv = c["K"] @ xc
    return uv[:2] / uv[2], xc[2]


def max_angle(X: np.ndarray, cids: list[str], cal: dict) -> float:
    rays = [(X - center(cal[c])) / np.linalg.norm(X - center(cal[c])) for c in cids]
    return max(float(np.degrees(np.arccos(np.clip(a @ b, -1, 1)))) for a, b in itertools.combinations(rays, 2))


def stats(v: list[float]) -> dict:
    if not v:
        return {"n": 0, "median": None, "p90": None}
    a = np.array(v)
    return {"n": int(a.size), "median": round(float(np.median(a)), 2), "p90": round(float(np.percentile(a, 90)), 2)}


def run(feature: str, imgs: dict, cal: dict, scale: float, out: Path, dev: str) -> dict:
    ext = (ALIKED if feature == "aliked" else SuperPoint)(max_num_keypoints=4096).eval().to(dev)
    matcher = LightGlue(features=feature).eval().to(dev)
    feats, kps = {}, {}
    with torch.inference_mode():
        for cid in CAMS:
            small = cv2.resize(imgs[cid], None, fx=scale, fy=scale, interpolation=cv2.INTER_AREA)
            f = ext.extract(to_tensor(small, dev), resize=None)
            feats[cid] = f
            kps[cid] = rbd(f)["keypoints"].cpu().numpy() / scale  # 원본 해상도 좌표
    pairs, edges = {}, []
    inlier_matches = {}
    for a, b in itertools.combinations(CAMS, 2):
        with torch.inference_mode():
            m = rbd(matcher({"image0": feats[a], "image1": feats[b]}))["matches"].cpu().numpy()
        pa, pb = kps[a][m[:, 0]], kps[b][m[:, 1]]
        rec = {"raw": len(m), "inliers": 0}
        if len(m) >= 8:
            Ka, Kb = cal[a]["K"], cal[b]["K"]
            na = cv2.undistortPoints(pa[:, None], Ka, None)[:, 0]
            nb = cv2.undistortPoints(pb[:, None], Kb, None)[:, 0]
            thr = 1.0 / float(np.mean([Ka[0, 0], Kb[0, 0]]))
            E, mask = cv2.findEssentialMat(na, nb, np.eye(3), cv2.RANSAC, 0.999, thr)
            if E is not None and E.shape == (3, 3):
                _, Rr, tr, mask2 = cv2.recoverPose(E, na, nb, np.eye(3), mask=mask)
                inl = mask2.ravel() > 0
                rec["inliers"] = int(inl.sum())
                ia, ib = pa[inl], pb[inl]
                w, h = cal[a]["size"]
                rec["spread_a"] = round(float(np.ptp(ia[:, 0]) / w * np.ptp(ia[:, 1]) / h), 3) if len(ia) else 0
                rec["spread_b"] = round(float(np.ptp(ib[:, 0]) / w * np.ptp(ib[:, 1]) / h), 3) if len(ib) else 0
                R_cal = cal[b]["R"] @ cal[a]["R"].T
                t_cal = cal[b]["t"] - R_cal @ cal[a]["t"]
                rec["rel_rot_diff_deg"] = round(rot_deg(R_cal, Rr), 2)
                cosang = float(tr.ravel() @ t_cal / np.linalg.norm(t_cal))
                rec["rel_dir_diff_deg"] = round(float(np.degrees(np.arccos(np.clip(cosang, -1, 1)))), 2)
                inlier_matches[(a, b)] = (m[inl, 0], m[inl, 1])
                vis = np.hstack([imgs[a], imgs[b]]).copy()
                for (x0, y0), (x1, y1) in zip(ia[::4], ib[::4]):
                    cv2.line(vis, (int(x0), int(y0)), (int(x1 + w), int(y1)), (0, 255, 0), 1)
                cv2.imwrite(str(out / f"{feature}_{a}_{b}.jpg"), cv2.resize(vis, None, fx=0.4, fy=0.4))
        pairs[f"{a}-{b}"] = rec
        if rec["inliers"] >= MIN_EDGE_INLIERS:
            edges.append((a, b))
    # 연결 그래프
    comp = {c: c for c in CAMS}
    def find(x):
        while comp[x] != x:
            x = comp[x]
        return x
    for a, b in edges:
        comp[find(a)] = find(b)
    connected = len({find(c) for c in CAMS}) == 1
    # 다시점 트랙(union-find over (cam, kp))
    parent: dict = {}
    def fnd(x):
        parent.setdefault(x, x)
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x
    for (a, b), (ia, ib) in inlier_matches.items():
        for i, j in zip(ia, ib):
            parent[fnd((a, int(i)))] = fnd((b, int(j)))
    groups: dict = {}
    for node in list(parent):
        groups.setdefault(fnd(node), []).append(node)
    tracks = []
    for nodes in groups.values():
        cams = [c for c, _ in nodes]
        if len(nodes) >= 2 and len(set(cams)) == len(cams):
            tracks.append([(c, kps[c][i]) for c, i in nodes])
    # 현재 보정으로 삼각측량 → 카메라별 재투영 오차
    err = {c: {"2view": [], "3plus": []} for c in CAMS}
    kept = 0
    for tr in tracks:
        X = triangulate(tr, cal)
        cids = [c for c, _ in tr]
        if any(project(X, cal[c])[1] <= 0 for c in cids) or max_angle(X, cids, cal) < MIN_TRI_ANGLE_DEG:
            continue
        kept += 1
        key = "2view" if len(tr) == 2 else "3plus"
        for c, uv in tr:
            err[c][key].append(float(np.linalg.norm(project(X, cal[c])[0] - uv)))
    reproj = {c: {k: stats(v) for k, v in d.items()} for c, d in err.items()}
    # 한 카메라씩 빼고 PnP: 나머지 카메라로 삼각측량한 점 → 그 카메라 자세 재추정
    loo = {}
    for x in CAMS:
        obj, img = [], []
        for tr in tracks:
            others = [(c, uv) for c, uv in tr if c != x]
            mine = [uv for c, uv in tr if c == x]
            if len(others) < 2 or not mine:
                continue
            X = triangulate(others, cal)
            oc = [c for c, _ in others]
            if any(project(X, cal[c])[1] <= 0 for c in oc) or max_angle(X, oc, cal) < MIN_TRI_ANGLE_DEG:
                continue
            obj.append(X)
            img.append(mine[0])
        rec = {"points": len(obj)}
        if len(obj) >= 12:
            obj_a, img_a = np.array(obj), np.array(img)
            ok, rvec, tvec, inl = cv2.solvePnPRansac(obj_a, img_a, cal[x]["K"], None, reprojectionError=4.0,
                                                    iterationsCount=5000, flags=cv2.SOLVEPNP_SQPNP)
            if ok and inl is not None and len(inl) >= 8:
                R_new = cv2.Rodrigues(rvec)[0]
                C_new = -R_new.T @ tvec.ravel()
                rec.update(inliers=len(inl), rot_diff_deg=round(rot_deg(cal[x]["R"], R_new), 3),
                           center_diff_m=round(float(np.linalg.norm(C_new - center(cal[x]))), 3))
                ii = inl.ravel()
                before = [float(np.linalg.norm(project(obj_a[i], cal[x])[0] - img_a[i])) for i in ii]
                newc = {"K": cal[x]["K"], "R": R_new, "t": tvec.ravel()}
                after = [float(np.linalg.norm(project(obj_a[i], newc)[0] - img_a[i])) for i in ii]
                rec["reproj_before"], rec["reproj_after"] = stats(before), stats(after)
        loo[x] = rec
    return {"feature": feature, "pairs": pairs, "edges": [f"{a}-{b}" for a, b in edges], "connected": connected,
            "tracks_total": len(tracks), "tracks_kept": kept, "reproj_current_cal": reproj, "leave_one_out_pnp": loo}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--rec", type=Path, required=True)
    ap.add_argument("--cal", type=Path, required=True)
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--features", nargs="+", default=["aliked", "superpoint"])
    ap.add_argument("--scale", type=float, default=0.5)
    args = ap.parse_args()
    args.out.mkdir(parents=True, exist_ok=True)
    version, cal = load_cal(args.cal)
    imgs = {}
    for cid in CAMS:
        cache = args.out / f"bg_{cid}.png"
        if cache.exists():
            imgs[cid] = cv2.imread(str(cache))
            continue
        bg = median_background(args.rec / cid / "epoch_0000.h264")
        bg = cv2.undistort(bg, cal[cid]["K"], cal[cid]["dist"])
        cv2.imwrite(str(cache), bg)
        imgs[cid] = bg
    dev = "cuda" if torch.cuda.is_available() else "cpu"
    results = {"calibration_version": version, "scale": args.scale, "device": dev,
               "runs": [run(f, imgs, cal, args.scale, args.out, dev) for f in args.features]}
    (args.out / "v0_results.json").write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(results, ensure_ascii=False, indent=1))


if __name__ == "__main__":
    main()
