"""Fast SAM 3D Body body 모드 어댑터. 보정 K·Tier 0 bbox와 일치한 seg 마스크를 쓴다."""

import os

import numpy as np

from simai_ai.hmr.loader import load_model
from simai_ai.hmr.multiview import HmrCandidate
from simai_ai.hmr.pose import pose_from_model_params
from simai_ai.hmr.prompt import HmrImageMap
from simai_ai.hmr.rig import bake_person_rig_from_model_params, load_mhr_rig, scales_from_scale_params


class FastBodyModel:
    model_version = "Fast-SAM-3D-Body@d72aa36 + sam-3d-body-dinov3@11aaa34 (body)"

    def __init__(self, checkpoint):
        # 검증된 opt 프리셋의 body 경로. glTF로 표현하지 못하는 correctives는 끈다.
        for key, value in {"USE_COMPILE": "0", "GPU_HAND_PREP": "1", "LAYER_DTYPE": "fp32",
                           "SKIP_KEYPOINT_PROMPT": "1", "IMG_SIZE": "512", "BODY_INTERM_PRED_LAYERS": "0,1,2",
                           "KEYPOINT_PROMPT_INTERM_INTERVAL": "999", "MHR_NO_CORRECTIVES": "1",
                           "MHR_USE_CUDA_GRAPH": "0"}.items():
            os.environ[key] = value
        model, cfg = load_model(checkpoint)
        self.device = next(model.parameters()).device
        from sam_3d_body import SAM3DBodyEstimator
        self.estimator = SAM3DBodyEstimator(sam_3d_body_model=model, model_cfg=cfg, human_detector=None,
                                          human_segmentor=None, fov_estimator=None)
        self.rig = load_mhr_rig(checkpoint / "assets/mhr_model.pt")
        self._image_maps = {}

    def _image_map(self, camera):
        maps = getattr(self, "_image_maps", None)
        if maps is None:
            maps = self._image_maps = {}
        key = (
            camera.camera_id,
            np.asarray(camera.K, dtype=np.float64).tobytes(),
            tuple(camera.image_size),
            camera.distortion_model,
            tuple(camera.distortion_coeffs),
        )
        imap = maps.get(key)
        if imap is None:
            imap = HmrImageMap(camera, camera.K)
            maps[key] = imap
        return imap

    def infer(self, image, observation, track, camera, *, prepared=None):
        import cv2
        import torch

        if tuple(image.shape[1::-1]) != tuple(camera.image_size):
            raise ValueError("HMR 그림과 보정 해상도가 다르다")
        # 원본 bbox를 같은 K의 왜곡 제거 그림으로 옮긴다. 알 수 없으면 실패한다.
        imap = self._image_map(camera)
        box = imap.bbox(observation["bbox"]) if prepared is None else prepared.prompt.bbox
        if box is None:
            raise ValueError("HMR bbox 왜곡 제거 실패")
        undistorted = imap.prepare_image(image, roi_box=box) if prepared is None else prepared.image
        mask_args = {}
        if prepared is not None and prepared.prompt.mask is not None:
            mask_args["masks"] = np.asarray(prepared.prompt.mask, dtype=np.uint8)[None]
        with torch.inference_mode():
            out = self.estimator.process_one_image(cv2.cvtColor(undistorted, cv2.COLOR_BGR2RGB),
                    bboxes=np.asarray([box], np.float32), cam_int=torch.as_tensor(camera.K, dtype=torch.float32)[None],
                    inference_type="body", hand_box_source="body_decoder", **mask_args)
        if len(out) != 1:
            raise ValueError("HMR 프롬프트 한 명과 결과 수가 다르다")
        p = out[0]
        def array(value):
            return value.detach().cpu().numpy() if hasattr(value, "detach") else np.asarray(value)
        if "mhr_model_params" in p:
            params = array(p["mhr_model_params"]).reshape(-1).copy()
        else:
            # 기존 verify_mhr_gltf와 같은 body 조립. body 모드 손 회전은 null이다.
            head = self.estimator.model.head_pose
            scales = scales_from_scale_params(array(p["scale_params"]), array(head.scale_mean), array(head.scale_comps))
            params = np.concatenate([np.zeros(3), array(p["global_rot"]).reshape(-1),
                                     array(p["body_pose_params"]).reshape(-1)[:130], scales.reshape(-1)])
        params[:3] = 0  # HMR translation은 버린다
        pose = pose_from_model_params(self.rig, params, hands_estimated=False)
        candidate = HmrCandidate(camera.camera_id, track["world_id"], tuple(observation["bbox"]),
                    array(p["pred_keypoints_3d"]) + array(p["pred_cam_t"]).reshape(1, 3),
                    pose.camera_from_rig, tuple(pose.joint_rotations),
                    prompt_world_id=track["world_id"], prompt_identity_revision=track["identity_revision"])
        return candidate, (array(p["shape_params"]).reshape(-1), params)

    def bake(self, samples):
        shape = np.median(np.stack([s[0] for s in samples]), axis=0)
        params = np.median(np.stack([s[1] for s in samples]), axis=0)
        return bake_person_rig_from_model_params(self.rig, shape, params)
