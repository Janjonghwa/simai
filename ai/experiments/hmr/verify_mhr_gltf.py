"""EXP-02 MHR → 스키닝 glTF 검증 (S15P31A101-88, docs/design/mhr-skinned-gltf.md).

내보낸 .glb(노드 TRS·역바인드·가중치를 파일에서 다시 읽음) + 계약 포즈(소수 넷째 자리)로 LBS 를 돌린 정점을
HMR `pred_vertices` 와 맞춰(pelvis 관절 위치만 맞춤, 뷰어가 Tier 0 pelvis 에 맞추는 것과 같음) 사람별 오차(mm)를 잰다.
샘플 산출물(.glb 한 명, avatar_pose_batch 포즈 시퀀스)도 쓴다. 산출물·자산은 커밋하지 않는다(SAM License).

실행(노트북 CPU, torch 가 있는 환경. 저장소 ai/ 에서):
    PYTHONPATH=src python experiments/hmr/verify_mhr_gltf.py --asset <mhr_model.pt> --hmr-basis <hmr_basis.npz> \\
        --npz-dir <runs/exp02/<run>/native> --out <출력 폴더>

hmr_basis.npz: SAM 3D Body 체크포인트의 head_pose.{scale_mean, scale_comps, hand_pose_mean, hand_pose_comps,
hand_joint_idxs_left, hand_joint_idxs_right}. npz 에 mhr_model_params 가 없어(Fast-SAM-3D-Body 추정기가 내보내지 않음)
mhr_head 의 조립(전역 0·global_rot·몸 자세·손 PCA→오일러·scale)을 여기서 다시 한다. 워커(-227)는 모델 출력
mhr_model_params 를 바로 쓰면 된다.
"""

from __future__ import annotations

import argparse
import json
import math
import time
from pathlib import Path

import numpy as np

from simai_ai.hmr.gltf_export import accessor_array, build_skinned_glb, read_glb
from simai_ai.hmr.lbs import (
    global_matrices,
    node_matrices,
    posed_rotations,
    skin_directions,
    skin_vertices,
)
from simai_ai.hmr.pose import (
    mhr_pose,
    pose_from_hmr,
    pose_from_model_params,
    world_root_rotation,
)
from simai_ai.hmr.rig import (
    PELVIS_JOINT_INDEX,
    bake_person_rig,
    bake_person_rig_from_model_params,
    load_mhr_rig,
    scales_from_scale_params,
)

# 합격선(설계 문서 '검증'): 제품 경로(A) 사람별 정점 오차
PASS_MEDIAN_MM = 5.0
PASS_P95_MM = 15.0
_HAND_DOFS_IN_ORDER = [3, 1, 1, 3, 1, 1, 3, 1, 1, 3, 1, 1, 2, 3, 1, 1]
# 샘플 root_rotation 용 가정: 수평 카메라(광축 = World +Y). 실제 보정 R 이 아니다
LEVEL_CAMERA_R = np.array([[1.0, 0.0, 0.0], [0.0, 0.0, -1.0], [0.0, 1.0, 0.0]])


def _xyz_from_6d(six: np.ndarray) -> np.ndarray:
    """Fast-SAM-3D-Body _batchXYZfrom6D_jit 과 같은 계산(그람-슈미트 뒤 XYZ 오일러)."""
    x_raw, y_raw = six[..., :3], six[..., 3:]
    c0 = x_raw / (np.linalg.norm(x_raw, axis=-1, keepdims=True) + 1e-8)
    z = np.cross(c0, y_raw)
    c2 = z / (np.linalg.norm(z, axis=-1, keepdims=True) + 1e-8)
    c1 = np.cross(c2, c0)
    sy = np.sqrt(c0[..., 0] ** 2 + c0[..., 1] ** 2)
    return np.stack([np.arctan2(c1[..., 2], c2[..., 2]), np.arctan2(-c0[..., 2], sy), np.arctan2(c0[..., 1], c0[..., 0])], -1)


def _hand_cont_to_model_params(hand_cont: np.ndarray) -> np.ndarray:
    three_c = np.concatenate([np.full(2 * k, k == 3) for k in _HAND_DOFS_IN_ORDER])
    one_c = np.concatenate([np.full(2 * k, k in (1, 2)) for k in _HAND_DOFS_IN_ORDER])
    three_p = np.concatenate([np.full(k, k == 3) for k in _HAND_DOFS_IN_ORDER])
    one_p = np.concatenate([np.full(k, k in (1, 2)) for k in _HAND_DOFS_IN_ORDER])
    out = np.zeros(27)
    out[three_p] = _xyz_from_6d(hand_cont[three_c].reshape(-1, 6)).reshape(-1)
    pairs = hand_cont[one_c].reshape(-1, 2)
    out[one_p] = np.arctan2(pairs[:, 0], pairs[:, 1])
    return out


def model_params_from_npz(get, basis) -> np.ndarray:
    """mhr_head._mhr_forward_core + replace_hands_in_pose 재현: [전역 이동 0, global_rot, 몸 자세 130(손 교체), scale 68]."""
    full = np.zeros(136)
    full[3:6] = get("global_rot")
    full[6:] = get("body_pose_params")[:130]
    hands = get("hand_pose_params").astype(np.float64)
    for half, idx in ((hands[:54], basis["hand_joint_idxs_left"]), (hands[54:], basis["hand_joint_idxs_right"])):
        decoded = basis["hand_pose_mean"] + half @ basis["hand_pose_comps"]
        full[idx] = _hand_cont_to_model_params(decoded)
    scales = scales_from_scale_params(get("scale_params"), basis["scale_mean"], basis["scale_comps"])
    return np.concatenate([full, scales])


def glb_rig(data: bytes) -> dict:
    """내보낸 .glb 를 다시 읽어 LBS 에 필요한 값을 꺼낸다(파일 자체를 검증)."""
    gltf, binary = read_glb(data)
    skin = gltf["skins"][0]
    prim = gltf["meshes"][0]["primitives"][0]["attributes"]
    joints = skin["joints"]
    nodes = [gltf["nodes"][j] for j in joints]
    parents = np.full(len(joints), -1)
    for j, n in enumerate(nodes):
        for c in n.get("children", []):
            parents[joints.index(c)] = j
    return {
        "parents": parents,
        "t": np.array([n["translation"] for n in nodes]),
        "q": np.array([n["rotation"] for n in nodes]),
        "s": np.array([n.get("scale", [1.0, 1.0, 1.0])[0] for n in nodes]),
        "ibm": np.transpose(accessor_array(gltf, binary, skin["inverseBindMatrices"]).reshape(-1, 4, 4), (0, 2, 1)).astype(np.float64),
        "pos": accessor_array(gltf, binary, prim["POSITION"]).astype(np.float64),
        "jnt": accessor_array(gltf, binary, prim["JOINTS_0"]).astype(np.int64),
        "wgt": accessor_array(gltf, binary, prim["WEIGHTS_0"]).astype(np.float64),
        "src": accessor_array(gltf, binary, prim["_MHR_VERTEX_INDEX"]).astype(np.int64),
    }


def lbs_camera(g: dict, joint_rotations, camera_from_rig) -> tuple[np.ndarray, np.ndarray]:
    """glTF 값 + 계약 포즈 → 카메라 축 정점(분할 정점)과 관절 global 행렬. 이동은 아직 안 맞춤."""
    R = posed_rotations(g["q"], joint_rotations)
    G = global_matrices(g["parents"], node_matrices(g["t"], R, g["s"]), camera_from_rig)
    return skin_vertices(g["pos"], g["jnt"], g["wgt"], G, g["ibm"]), G


def vertex_errors_mm(g, joint_rotations, camera_from_rig, pred_vertices, pred_joints) -> tuple[np.ndarray, np.ndarray]:
    V_split, G = lbs_camera(g, joint_rotations, camera_from_rig)
    V = np.zeros((len(pred_vertices), 3))
    V[g["src"]] = V_split  # 같은 MHR 정점에서 나온 분할 정점은 값이 같다
    shift = pred_joints[PELVIS_JOINT_INDEX] - G[PELVIS_JOINT_INDEX, :3, 3]
    ev = np.linalg.norm(V + shift - pred_vertices, axis=1) * 1000
    ej = np.linalg.norm(G[1:, :3, 3] + shift - pred_joints[1:], axis=1) * 1000  # 관절 0(바닥 원점)은 회전 중심이 달라 뺀다
    return ev, ej


def stats(e: np.ndarray) -> dict:
    return {"median": float(np.median(e)), "p95": float(np.percentile(e, 95)), "max": float(np.max(e))}


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--asset", required=True)
    ap.add_argument("--hmr-basis", required=True)
    ap.add_argument("--npz-dir", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--sample", default="cam_B_01:0", help="샘플 .glb 로 쓸 사람 <이미지>:<사람>")
    ap.add_argument("--body-mode", action="store_true", help="body 모드 출력이면 손가락 회전을 null 로 둔다(기본은 full 모드: 손 추정)")
    args = ap.parse_args()
    args.hands_estimated = not args.body_mode
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)

    import torch

    t0 = time.time()
    rig = load_mhr_rig(args.asset)
    model = torch.jit.load(args.asset, map_location="cpu")
    basis = dict(np.load(args.hmr_basis))
    files = sorted(Path(args.npz_dir).glob("*.npz"))

    people = []
    for f in files:
        z = np.load(f)
        for p in range(int(z["n_people"])):
            people.append((f.stem, p, {k.split("__", 1)[1]: z[k] for k in z.files if k.startswith(f"p{p}__")}))
    mean_body_pose = np.mean([model_params_from_npz(d.get, basis)[6:136] for _, _, d in people], axis=0)

    rows, sample = [], None
    for image, p, d in people:
        mp = model_params_from_npz(d.get, basis)
        shape = d["shape_params"].astype(np.float64)
        pv, pj = d["pred_vertices"].astype(np.float64), d["pred_joint_coords"].astype(np.float64)

        # 재조립 확인: 자산 forward(보정 형상 끔, 실행 설정 MHR_NO_CORRECTIVES=1 과 같음) vs pred_vertices
        with torch.no_grad():
            verts, _ = model(torch.tensor(shape[None], dtype=torch.float32), torch.tensor(mp[None], dtype=torch.float32),
                             torch.zeros(1, 72), False)
        ref = verts[0].numpy().astype(np.float64) * 0.01 * np.array([1.0, -1.0, -1.0])
        recon_mm = float(np.max(np.linalg.norm(ref - pv, axis=1)) * 1000)

        person = bake_person_rig_from_model_params(rig, shape, mp)
        glb = build_skinned_glb(person)
        g = glb_rig(glb.data)
        pose = pose_from_model_params(rig, mp, hands_estimated=args.hands_estimated)
        ev, ej = vertex_errors_mm(g, pose.joint_rotations, pose.camera_from_rig, pv, pj)

        pose_raw = pose_from_model_params(rig, mp, hands_estimated=args.hands_estimated, decimals=None)
        ev_raw, _ = vertex_errors_mm(g, pose_raw.joint_rotations, pose_raw.camera_from_rig, pv, pj)
        pose_gr = pose_from_hmr(d["pred_global_rots"], rig.parents, hands_estimated=args.hands_estimated)
        ev_gr, _ = vertex_errors_mm(g, pose_gr.joint_rotations, pose_gr.camera_from_rig, pv, pj)

        # 뼈 길이(*_flexible) 고정: 0 으로 굽기, 전체 사람 평균으로 굽기
        scales = mp[136:]
        g0 = glb_rig(build_skinned_glb(bake_person_rig(rig, shape, scales, None)).data)
        ev_flex0, _ = vertex_errors_mm(g0, pose.joint_rotations, pose.camera_from_rig, pv, pj)
        gm = glb_rig(build_skinned_glb(bake_person_rig(rig, shape, scales, mean_body_pose)).data)
        ev_flexm, _ = vertex_errors_mm(gm, pose.joint_rotations, pose.camera_from_rig, pv, pj)

        # 자세 보정 형상(correctives)을 켰다면 glTF 로 못 싣는 몫: 스키닝한 보정 변위 크기
        jp = torch.tensor((rig.parameter_transform @ np.concatenate([mp, np.zeros(45)]))[None], dtype=torch.float32)
        with torch.no_grad():
            corr = model.pose_correctives_model(jp)[0].numpy().astype(np.float64) * 0.01
        R = posed_rotations(person.local_rotations, pose_raw.joint_rotations)
        G = global_matrices(rig.parents, node_matrices(person.local_translations_m, R, person.local_scales), pose_raw.camera_from_rig)
        ev_corr = np.linalg.norm(skin_directions(corr, rig.skin_joints, rig.skin_weights, G, person.inverse_bind_matrices), axis=1) * 1000

        up = pose.camera_from_rig @ np.array([0.0, 1.0, 0.0])
        rows.append({
            "image": image, "person": p, "recon_max_mm": recon_mm,
            "A_gltf": stats(ev), "A_joint_max_mm": float(ej.max()), "A_unrounded": stats(ev_raw),
            "global_rots_path": stats(ev_gr), "flex_zero": stats(ev_flex0), "flex_mean": stats(ev_flexm),
            "correctives_if_on": stats(ev_corr), "rig_up_in_camera": [round(float(x), 3) for x in up],
            "glb_vertex_count": glb.vertex_count, "glb_bytes": glb.size_bytes,
        })
        if f"{image}:{p}" == args.sample:
            sample = (image, p, glb, mp)
        print(f"{image} p{p}: A med {rows[-1]['A_gltf']['median']:.3f} p95 {rows[-1]['A_gltf']['p95']:.3f} "
              f"max {rows[-1]['A_gltf']['max']:.3f} mm | recon {recon_mm:.3f} | grot p95 {rows[-1]['global_rots_path']['p95']:.1f} "
              f"| flex0 p95 {rows[-1]['flex_zero']['p95']:.1f} | flexmean p95 {rows[-1]['flex_mean']['p95']:.1f} "
              f"| corr p95 {rows[-1]['correctives_if_on']['p95']:.1f}", flush=True)

    def summary(key: str, field: str) -> dict:
        vals = np.array([r[key][field] for r in rows])
        return {"min": float(vals.min()), "median": float(np.median(vals)), "max": float(vals.max())}

    verdict = all(r["A_gltf"]["median"] <= PASS_MEDIAN_MM and r["A_gltf"]["p95"] <= PASS_P95_MM for r in rows)
    report = {
        "run": str(Path(args.npz_dir).parent.name),
        "people": len(rows),
        "images": len(files),
        "skin_dropped_weight_max": rig.dropped_weight_max,
        "pass_threshold": {"median_mm": PASS_MEDIAN_MM, "p95_mm": PASS_P95_MM, "applies_to": "A_gltf (모든 사람)"},
        "verdict": "PASS" if verdict else "FAIL",
        "summary": {k: {f: summary(k, f) for f in ("median", "p95", "max")}
                    for k in ("A_gltf", "A_unrounded", "global_rots_path", "flex_zero", "flex_mean", "correctives_if_on")},
        "recon_max_mm": float(max(r["recon_max_mm"] for r in rows)),
        "rows": rows,
        "elapsed_s": round(time.time() - t0, 1),
    }
    (out / "verify_report.json").write_text(json.dumps(report, ensure_ascii=False, indent=1), encoding="utf-8")

    if sample:
        image, p, glb, mp = sample
        name = f"mhr_{image}_p{p}"
        (out / f"{name}.glb").write_bytes(glb.data)
        write_pose_sequence(out / "avatar_pose_sequence.json", rig, people, basis, args.hands_estimated, glb)
        print("sample:", out / f"{name}.glb", glb.size_bytes, "bytes", glb.sha256)
    print("verdict", report["verdict"], "elapsed", report["elapsed_s"], "s")


def write_pose_sequence(path: Path, rig, people, basis, hands_estimated: bool, glb) -> None:
    """교실 사진 여러 장의 사람 포즈를 5 Hz 로 이어 붙인 시퀀스(avatar_pose_batch 메시지 배열). 첫 프레임은 바인드 자세."""
    base_ms = 1761460000000  # 2026-10-26 06:26:40Z (샘플 시각)
    picks = [(img, p, d) for img, p, d in people if p == 0]
    msgs = []
    frames = [None] + picks
    for i, item in enumerate(frames):
        if item is None:
            rotations = [[0.0, 0.0, 0.0], [0.0, 0.0, 0.0], *([None] * (rig.joint_count - 2))]
            root = [round(math.pi / 2, 4), 0.0, 0.0]  # 똑바로 선 사람: Rx(+π/2)
            note = "bind"
        else:
            img, p, d = item
            pose = pose_from_model_params(rig, model_params_from_npz(d.get, basis), hands_estimated=hands_estimated)
            rotations = pose.joint_rotations
            root = world_root_rotation(pose.camera_from_rig, LEVEL_CAMERA_R)
            note = f"{img}:p{p}"
        msgs.append({
            "schema_version": "1.0.0",
            "msg_type": "avatar_pose_batch",
            "msg_id": f"00000000-0000-4000-8000-{i:012d}",
            "sent_at": base_ms + i * 200 + 50,
            "source_id": "worker-01",
            "payload": {
                "stream_epoch": base_ms - 60000,
                "batch_sequence": i,
                "calibration_version": "sample-level-camera",
                "model_version": "Fast-SAM-3D-Body@d72aa36 + sam-3d-body-dinov3 (H7u_calib_k_unified_img_20261026)",
                "input_provenance": {
                    "frames": {"source": "FILE", "dataset_id": "classroom-stills-20261021"},
                    "tracks": {"source": "TIER0_LOG", "provider_version": "none (offline stills sample)"},
                    "is_upper_bound": False,
                },
                "dropped_poses": 0,
                "poses": [{
                    "world_id": "w-sample",
                    "identity_revision": 0,
                    "capture_time_ms": base_ms + i * 200,
                    "appearance_version": 0,
                    "joint_source": "inferred",
                    "pose": mhr_pose(root, rotations),
                }],
            },
            "_frame": note,
        })
    doc = {
        "_note": ("실험 확인용 샘플(S15P31A101-88). 서로 다른 사진의 사람 포즈를 한 체형에 순서대로 입힌 것이고 실제 시간 연속이 아니다. "
                  "root_rotation 은 수평 카메라 가정(실제 보정 아님)이라 내려다보는 카메라의 사람은 기울어 보인다. tracks 출처는 형식만 채웠다."),
        "glb_sha256": glb.sha256,
        "messages": msgs,
    }
    path.write_text(json.dumps(doc, ensure_ascii=False, indent=1), encoding="utf-8")


if __name__ == "__main__":
    main()
