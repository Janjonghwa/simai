"""HMR(SAM 3D Body·MHR) 출력 → 아바타 스트림 포즈(계약 `mhr_pose`) 변환 (S15P31A101-88, docs/design/mhr-skinned-gltf.md).

규약
- 리그 좌표 = MHR 자산 좌표 = glTF Y-up(m). HMR 의 정점·관절 위치는 이 좌표에 F = diag(1, −1, −1) 을 곱한 카메라 좌표
  (OpenCV: x 오른쪽, y 아래, z 앞)로 나오고, `pred_global_rots` 는 F 를 곱하지 않은 리그 좌표의 관절 global 회전이다
  (Fast-SAM-3D-Body `mhr_head.py`: 위치만 [1, 2] 부호 반전).
- MHR 의 몸 방향(global_rot)은 관절 1(pelvis, 자산 이름 "root")에 걸리고 관절 0("body_world")은 늘 단위다.
  계약은 관절 0 회전 = 0, 몸 방향 = root_rotation 이므로 pelvis 의 global 회전을 통째로 root 쪽으로 옮기고
  관절 0·1 회전을 [0, 0, 0] 으로 둔다. 회전 중심이 pelvis 에서 리그 원점으로 바뀌어 생기는 차이는 이동뿐이고,
  뷰어가 pelvis 관절을 Tier 0 pelvis_xyz 에 맞출 때 사라진다.
- camera_from_rig = F · G_pelvis: 리그 좌표 → 카메라 좌표 회전. World 로는 root_rotation = R_wc · camera_from_rig
  (R_wc = 보정 R(World → 카메라)의 전치). `world_root_rotation` 이 그 곱이고 -227 이 보정 R 을 넣어 부른다.
- 손가락(43~64·79~100)은 body 모드에서 HMR 이 추정하지 않으므로 null(뷰어는 바인드 회전). full 모드면 값을 싣는다.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from simai_ai.hmr.lbs import topological_order
from simai_ai.hmr.rig import MHR_MODEL_PARAM_COUNT, PELVIS_JOINT_INDEX, MhrRig
from simai_ai.hmr.rotation import (
    euler_xyz_to_matrix,
    matrix_to_rotvec,
    quat_xyzw_to_matrix,
)

# HMR 카메라 좌표 = F · 리그 좌표 (OpenCV 카메라 ↔ Y-up 리그)
CAMERA_FROM_MHR = np.diag([1.0, -1.0, -1.0])
FINGER_JOINT_RANGES = ((43, 64), (79, 100))  # 양 끝 포함, r_pinky0..r_thumb_null · l_pinky0..l_thumb_null
POSE_DECIMALS = 4  # 설계 문서 '단위·반올림'


def finger_joint_indices() -> list[int]:
    return [j for lo, hi in FINGER_JOINT_RANGES for j in range(lo, hi + 1)]


def local_rotations_from_global(global_rots: np.ndarray, parents: np.ndarray) -> np.ndarray:
    """관절 global 회전 (J, 3, 3) → 부모 기준 local 회전 (J, 3, 3). 루트는 global 그대로."""
    global_rots = np.asarray(global_rots, dtype=np.float64)
    out = global_rots.copy()
    for j, p in enumerate(parents):
        if p >= 0:
            out[j] = global_rots[p].T @ global_rots[j]
    return out


def global_rotations_from_local(local_rots: np.ndarray, parents: np.ndarray) -> np.ndarray:
    """`local_rotations_from_global` 의 역. 부모 인덱스 < 자기 인덱스를 가정하지 않는다."""
    out = np.empty_like(np.asarray(local_rots, dtype=np.float64))
    for j in topological_order(parents):
        p = parents[j]
        out[j] = local_rots[j] if p < 0 else out[p] @ local_rots[j]
    return out


def _rounded(v: np.ndarray, decimals: int | None) -> list[float]:
    if decimals is None:
        return [float(x) for x in v]
    return [float(round(float(x), decimals)) + 0.0 for x in v]  # + 0.0: −0.0 을 0.0 으로


@dataclass(frozen=True)
class HmrPose:
    """HMR 한 사람의 변환 결과. root 는 아직 카메라 기준이다(World 는 `world_root_rotation`)."""

    camera_from_rig: np.ndarray  # (3, 3) 리그 좌표 → HMR 카메라 좌표
    joint_rotations: list[list[float] | None]  # 계약 joint_rotations (관절 0·pelvis = [0, 0, 0])


def pose_from_hmr(
    pred_global_rots: np.ndarray,
    parents: np.ndarray,
    *,
    hands_estimated: bool,
    pelvis_joint_index: int = PELVIS_JOINT_INDEX,
    decimals: int | None = POSE_DECIMALS,
) -> HmrPose:
    """HMR `pred_global_rots` (J, 3, 3) → 계약 joint_rotations 와 카메라 기준 몸 방향.

    hands_estimated: HMR 이 손을 추정한 경우(full 모드)만 True. False 면 손가락 관절은 null 이다(지어내지 않음).
    """
    G = np.asarray(pred_global_rots, dtype=np.float64)
    parents = np.asarray(parents)
    if G.shape != (len(parents), 3, 3):
        raise ValueError(f"pred_global_rots 모양 {G.shape} != ({len(parents)}, 3, 3)")
    root_chain = {pelvis_joint_index}
    p = parents[pelvis_joint_index]
    while p >= 0:
        root_chain.add(int(p))
        p = parents[p]
    for j, pj in enumerate(parents):
        if pj in root_chain and j not in root_chain and pj != pelvis_joint_index:
            raise ValueError(f"관절 {j} 가 pelvis 위 관절 {pj} 에 직접 달려 있어 몸 방향을 root 로 옮길 수 없다")
    local = local_rotations_from_global(G, parents)
    fingers = set() if hands_estimated else set(finger_joint_indices())
    rotations: list[list[float] | None] = []
    for j in range(len(parents)):
        if j in root_chain:
            rotations.append([0.0, 0.0, 0.0])
        elif j in fingers:
            rotations.append(None)
        else:
            rotations.append(_rounded(matrix_to_rotvec(local[j]), decimals))
    return HmrPose(camera_from_rig=CAMERA_FROM_MHR @ G[pelvis_joint_index], joint_rotations=rotations)


def local_rotations_from_model_params(rig: MhrRig, model_params: np.ndarray) -> np.ndarray:
    """모델 출력 `mhr_model_params` (204) → 관절 local 회전 (J, 3, 3) = 사전 회전 · 오일러(관절 파라미터 회전). 자산 계산과 같다."""
    mp = np.zeros(rig.parameter_transform.shape[1])
    mp[:MHR_MODEL_PARAM_COUNT] = np.asarray(model_params, dtype=np.float64)
    jp = (rig.parameter_transform @ mp).reshape(rig.joint_count, 7)
    return quat_xyzw_to_matrix(rig.joint_prerotations) @ euler_xyz_to_matrix(jp[:, 3:6])


def pose_from_model_params(
    rig: MhrRig,
    model_params: np.ndarray,
    *,
    hands_estimated: bool,
    decimals: int | None = POSE_DECIMALS,
) -> HmrPose:
    """모델 출력 `mhr_model_params` (204) → 포즈(권장 경로).

    Fast-SAM-3D-Body full 모드는 손 IK 뒤 메시를 다시 계산하면서 `pred_global_rots` 는 IK 전 값을 그대로 내보낸다
    (`sam3d_body.py` 재계산 블록이 joint_global_rots 를 갱신하지 않음). 그래서 손·손목이 바뀐 사람은 `pose_from_hmr` 로
    만든 손 회전이 메시와 다르다. 재계산 뒤 `mhr_model_params` 는 메시와 같은 값이라 이 경로가 정확하다.
    """
    local = local_rotations_from_model_params(rig, model_params)
    G = global_rotations_from_local(local, rig.parents)
    return pose_from_hmr(G, rig.parents, hands_estimated=hands_estimated, decimals=decimals)


def world_root_rotation(
    camera_from_rig: np.ndarray, world_to_camera_R: np.ndarray, decimals: int | None = POSE_DECIMALS
) -> list[float]:
    """계약 root_rotation(리그 좌표 → World 축각) = R_wc · camera_from_rig, R_wc = world_to_camera_R 의 전치.

    world_to_camera_R 은 보정의 외부 파라미터 R(X_cam = R·X_world + t, 명세 4장)이다. 그 calibration_version 을 포즈 묶음에 싣는다.
    """
    R = np.asarray(world_to_camera_R, dtype=np.float64).T @ np.asarray(camera_from_rig, dtype=np.float64)
    return _rounded(matrix_to_rotvec(R), decimals)


def mhr_pose(root_rotation: list[float], joint_rotations: list[list[float] | None]) -> dict:
    """계약 `mhr_pose` 객체."""
    return {"root_rotation": list(root_rotation), "joint_rotations": list(joint_rotations)}
