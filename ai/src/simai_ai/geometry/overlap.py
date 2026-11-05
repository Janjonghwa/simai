"""보정 버전별 보수적 시야 분리 증명. 증명할 수 없으면 후보를 유지한다.

유한한 바닥 영역 샘플은 높은 관절·원거리 겹침을 놓치므로 쓰지 않는다.
왜곡 없는 카메라의 무한 절두체를 64 px 넓힌 뒤, 한 쪽 측면 평면 바깥에
상대 카메라 중심(0.25 m 여유)과 모든 시선이 있는 경우에만 분리로 본다.
왜곡·비정상 보정은 unknown(제외 없음). 영상 특징/기하 통과율로 제외하지 않는다.
"""

import itertools

import numpy as np

from simai_ai.geometry.rig import CameraRig, RigCamera


def _cone(camera: RigCamera):
    if camera.distortion_model != "NONE" and any(camera.distortion_coeffs):
        return None
    w, h = camera.image_size
    if w <= 0 or h <= 0 or not np.all(np.isfinite(camera.K)):
        return None
    # 표준 핀홀 외 K/회전이면 판단하지 않는다.
    if (not np.allclose(camera.K[2], [0, 0, 1]) or camera.K[0, 0] <= 0 or camera.K[1, 1] <= 0
            or not np.allclose(camera.R @ camera.R.T, np.eye(3), atol=1e-6)):
        return None
    corners = np.array([[-64, -64, 1], [w + 64, -64, 1],
                        [w + 64, h + 64, 1], [-64, h + 64, 1]], dtype=float)
    rays = corners @ np.linalg.inv(camera.K).T @ camera.R
    axis = camera.R[2]
    planes = [axis]  # 카메라 뒤 반공간도 절두체 밖이다.
    for i in range(4):
        normal = np.cross(rays[i], rays[(i + 1) % 4])
        norm = np.linalg.norm(normal)
        if not np.isfinite(norm) or norm < 1e-12:
            return None
        normal /= norm
        if normal @ axis < 0:
            normal *= -1
        planes.append(normal)
    return rays, np.array(planes)


def disjoint_camera_pairs(rig: CameraRig) -> frozenset[tuple[str, str]]:
    cones = {cid: _cone(cam) for cid, cam in rig.cameras.items()}
    skipped = set()
    for a, b in itertools.combinations(sorted(rig.cameras), 2):
        if cones[a] is None or cones[b] is None:
            continue
        for first, second in ((a, b), (b, a)):
            _, planes = cones[first]
            rays, _ = cones[second]
            delta = rig.cameras[second].center - rig.cameras[first].center
            if any(normal @ delta < -0.25 and np.all(rays @ normal < -1e-9) for normal in planes):
                skipped.add((a, b))
                break
    return frozenset(skipped)
