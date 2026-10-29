"""기하 게이트 밖의 카메라 회전 초기값: 3D/2D 궤적 후보별 PnP RANSAC.

다른 두 대 이상으로 관측된 관절만 쓴다. PnP로 후보를 검증한 뒤 기존
realign_rotation으로 중심을 고정한 3자유도 회전만 초기값에 반영한다.
"""

from __future__ import annotations

import copy
from collections import defaultdict

import numpy as np
from scipy.spatial.transform import Rotation

from simai_ai.calibration.bundle import PersonObservation
from simai_ai.calibration.recalibration import realign_rotation
from simai_ai.geometry.rig import RigCamera


def rotation_candidates(tracks, reference_frames, calibration, camera_id):
    """로컬 트랙 × 다른 카메라의 연관 궤적을 각각 시험한다. 오대응도 후보에서 검증한다."""
    import cv2

    camera = next(c for c in calibration["cameras"] if c["camera_id"] == camera_id)
    rig = RigCamera.from_calibration(camera)
    by_t = {f.t: f for f in reference_frames}
    groups = defaultdict(lambda: {"xyz": [], "uv": [], "observations": [], "times": [], "links": []})
    for t, views in tracks.ticks:
        if t not in by_t:
            continue
        for v in views:
            if v.camera_id != camera_id:
                continue
            for c in by_t[t].clusters:
                # 3대 기준 리그에서는 2대 클러스터가 TWO_VIEW_COMPETITOR로 표시될 수 있으므로
                # 관절 재투영/교차각과 PnP RANSAC + 4대 연관 점수로 오대응을 거른다.
                if len(c.views) < 2 or c.skeleton is None:
                    continue
                xyz, uv, joints = [], [], []
                # 머리의 방향별 검출 편향은 회전 초기값에 넣지 않는다.
                for j in range(5, 17):
                    kp, joint = v.view.keypoints[j], c.skeleton.joints[j]
                    if (kp is not None and kp[2] >= 0.3 and joint.xyz is not None
                            and joint.ray_angle_rad is not None and joint.ray_angle_rad >= np.radians(5)
                            and joint.reprojection_error_px is not None and joint.reprojection_error_px <= 12):
                        xyz.append(joint.xyz)
                        uv.append(kp[:2])
                        joints.append(j)
                if len(xyz) < 4:
                    continue
                group = groups[(v.local_track_id, c.association_id)]
                group["links"].extend((len(group["observations"]), j) for j in joints)
                group["xyz"].extend(xyz)
                group["uv"].extend(uv)
                group["times"].append(t)
                group["observations"].append(PersonObservation(
                    c.association_id, t, {**{w.camera_id: w.view.keypoints for w in c.views},
                                            camera_id: v.view.keypoints},
                ))
    records, poses = [], []
    for key, group in sorted(groups.items()):
        if len(group["xyz"]) < 30 or len(group["times"]) < 3:
            continue
        X = np.asarray(group["xyz"], dtype=np.float64)
        uv = np.asarray(group["uv"], dtype=np.float64)
        norm, valid = rig.normalize_with_mask(uv)
        X, norm = X[valid], norm[valid]
        if len(X) < 30:
            continue
        best_pnp = (False, None, None, None)
        for flag in (cv2.SOLVEPNP_EPNP, cv2.SOLVEPNP_SQPNP):
            cv2.setRNGSeed(331)
            ok, rvec, tvec, inliers = cv2.solvePnPRansac(
                X, norm, np.eye(3), None, iterationsCount=300,
                reprojectionError=12 / float(rig.K[0, 0]), confidence=0.999, flags=flag,
            )
            n_in = 0 if not ok or inliers is None else len(inliers)
            if n_in > (0 if best_pnp[3] is None else len(best_pnp[3])):
                best_pnp = (ok, rvec, tvec, inliers)
        ok, rvec, tvec, inliers = best_pnp
        rec = {"target_track": key[0], "reference_track": key[1], "points": len(X),
               "ticks": len(group["times"]), "inliers": 0 if inliers is None else len(inliers), "accepted": False}
        records.append(rec)
        if not ok or inliers is None or len(inliers) < 30 or len(inliers) / len(X) < 0.45:
            rec["reason"] = "PNP_SUPPORT"
            continue
        R = cv2.Rodrigues(rvec)[0]
        C = -R.T @ tvec.reshape(3)
        angle = float(np.degrees(Rotation.from_matrix(R @ rig.R.T).magnitude()))
        rec.update(pnp_rotation_deg=angle, pnp_center_change_m=float(np.linalg.norm(C - rig.center)))
        if angle > 20 or np.linalg.norm(C - rig.center) > 0.75:
            rec["reason"] = "PNP_IMPLAUSIBLE_MOTION"
            continue
        # PnP가 버린 관절/시각을 중심 고정 Kabsch에 다시 넣으면 오대응이 회전을 편향시킨다.
        links = np.asarray(group["links"])[valid][inliers.reshape(-1)]
        accepted = defaultdict(set)
        for obs_idx, joint_idx in links:
            accepted[int(obs_idx)].add(int(joint_idx))
        if len(accepted) < 3:
            rec["reason"] = "PNP_FEW_INLIER_TICKS"
            continue
        filtered = []
        for obs_idx, obs in enumerate(group["observations"]):
            if obs_idx not in accepted:
                continue
            own = tuple(kp if j in accepted[obs_idx] else None for j, kp in enumerate(obs.views[camera_id]))
            filtered.append(PersonObservation(obs.person_id, obs.t, {**obs.views, camera_id: own}))
        pose = realign_rotation(calibration, camera_id, filtered)
        if pose is None:
            rec["reason"] = "ROTATION_SUPPORT"
            continue
        fixed_R = np.asarray(pose[0])
        cam_xyz = (X - rig.center) @ fixed_R.T
        with np.errstate(divide="ignore", invalid="ignore"):
            residual = np.linalg.norm(cam_xyz[:, :2] / cam_xyz[:, 2:] - norm, axis=1) * rig.K[0, 0]
        good = (cam_xyz[:, 2] > 0) & np.isfinite(residual) & (residual <= 12)
        rec["fixed_center_inlier_ratio"] = float(good.mean())
        rec["fixed_center_median_px"] = float(np.median(residual))
        if good.mean() < 0.5:
            rec["reason"] = "CENTER_FIXED_RESIDUAL"
            continue
        candidate = copy.deepcopy(calibration)
        own = next(c for c in candidate["cameras"] if c["camera_id"] == camera_id)
        own["R"], own["t"] = pose
        poses.append((candidate, rec))
    return poses, records
