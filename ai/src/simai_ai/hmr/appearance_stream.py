"""외형 누적과 상시 루프의 연결. 자산 송신 성공 뒤에만 누적 결과를 확정한다."""

import copy
from dataclasses import replace
import json
import logging
from pathlib import Path
import re
import threading

import numpy as np

from simai_ai.hmr.appearance import AppearanceAccumulator, AppearanceConfig, AppearanceView
from simai_ai.hmr.appearance_quality import CameraColors
from simai_ai.hmr.appearance_report import save_result
from simai_ai.hmr.diagnostics import is_upload_conflict
from simai_ai.hmr.lbs import global_matrices, node_matrices, posed_rotations, skin_vertices
from simai_ai.hmr.multiview import COCO17_INDEX
from simai_ai.hmr.rotation import rotvec_to_matrix

log = logging.getLogger(__name__)


def face_faces_from_rig(rig, region=None):
    """명시한 MHR 얼굴 정점·면 또는 head 하위 스킨 영향의 보수적 얼굴 영역.

    영역 파일은 라이선스 자산을 포함하지 않는 정점/면 번호 JSON이며 자산 해시를 확인한다.
    자동 영역은 머리 전체를 포함한다. 얼굴 미지정 상태에서 보간하는 것보다 보수적이다.
    """
    if region is not None:
        if region["asset_sha256"] != rig.asset_sha256:
            raise ValueError("얼굴 영역과 MHR 자산 해시 불일치")
        vertex_ids = np.asarray(region.get("vertex_indices", []), dtype=np.int64)
        face_ids = np.asarray(region.get("face_indices", []), dtype=np.int64)
        if ((vertex_ids < 0).any() or (vertex_ids >= rig.vertex_count).any()
                or (face_ids < 0).any() or (face_ids >= len(rig.faces)).any()):
            raise ValueError("MHR 얼굴 정점·면 번호 범위 오류")
        vertices = np.zeros(rig.vertex_count, bool)
        vertices[vertex_ids] = True
        selected = vertices[rig.faces].any(axis=1)
        selected[face_ids] = True
    else:
        joints = {i for i, name in enumerate(rig.joint_names)
                  if re.search(r"(^|_)(head|face|jaw|eye)(_|$)", name.lower())}
        for _ in range(rig.joint_count):
            joints |= {i for i, parent in enumerate(rig.parents) if int(parent) in joints}
        vertices = (np.isin(rig.skin_joints, list(joints)) & (rig.skin_weights >= .1)).any(axis=1)
        selected = vertices[rig.faces].any(axis=1)
    if not selected.any():
        raise ValueError("MHR 얼굴 영역 없음: 검증된 얼굴 정점·면 JSON을 지정해야 한다")
    return selected


def posed_world_vertices(baked, person):
    """뷰어와 같은 LBS. root는 집계 회전, 이동은 Tier 0 pelvis(모르면 None)."""
    if person.pelvis_xyz is None or person.root_rotation is None or person.joint_rotations is None:
        return None
    rotations = posed_rotations(baked.local_rotations, person.joint_rotations)
    matrices = global_matrices(baked.rig.parents,
        node_matrices(baked.local_translations_m, rotations, baked.local_scales),
        root=rotvec_to_matrix(np.asarray(person.root_rotation)))
    vertices = skin_vertices(baked.rest_vertices_m, baked.rig.skin_joints, baked.rig.skin_weights,
                             matrices, baked.inverse_bind_matrices)
    return vertices + np.asarray(person.pelvis_xyz) - matrices[1, :3, 3]


def _cuda_available():
    import torch

    return torch.cuda.is_available()


def appearance_from_env(env, *, cuda_available=None):
    """기본 auto: CUDA 가 있으면 켜고(GPU 누적), 없으면 끈다(CPU 는 1인당 3.5초라 루프를 늦춘다). 0·1 로 강제한다."""
    enabled = env.get("SIMAI_AVATAR_APPEARANCE", "auto")
    if enabled not in {"auto", "0", "1"}:
        raise ValueError("SIMAI_AVATAR_APPEARANCE는 auto, 0, 1")
    if enabled == "0":
        return None
    cuda = _cuda_available() if cuda_available is None else cuda_available
    if enabled == "auto" and not cuda:
        return None
    def quality_enabled(name):
        value = env.get(f"SIMAI_AVATAR_TEXTURE_{name}", "1")
        if value not in {"0", "1"}:
            raise ValueError(f"SIMAI_AVATAR_TEXTURE_{name}는 0 또는 1")
        return value == "1"
    cfg = AppearanceConfig(
        resolution=int(env.get("SIMAI_AVATAR_TEXTURE_SIZE", "1024")),
        device=env.get("SIMAI_AVATAR_TEXTURE_DEVICE", "cuda" if cuda else "cpu"),
        face_policy=env.get("SIMAI_AVATAR_FACE_POLICY", "OBSERVED"),
        freeze_frames=int(env.get("SIMAI_AVATAR_APPEARANCE_FREEZE_FRAMES", "30")),
        sample_interval_ms=int(env.get("SIMAI_AVATAR_TEXTURE_INTERVAL_MS", "2000")),
        publish_interval_ms=int(env.get("SIMAI_AVATAR_TEXTURE_PUBLISH_MS", "30000")),
        silhouette_enabled=quality_enabled("SILHOUETTE"),
        color_enabled=quality_enabled("COLOR"),
        blur_enabled=quality_enabled("BLUR"),
        fit_enabled=quality_enabled("FIT"))
    region_path = env.get("SIMAI_AVATAR_FACE_REGION")
    region = json.loads(Path(region_path).read_text(encoding="utf-8")) if region_path else None
    return AppearancePipeline(cfg, face_region=region, output_dir=env.get("SIMAI_AVATAR_TEXTURE_OUTPUT_DIR"))


class AppearancePipeline:
    def __init__(self, config=None, *, face_region=None, output_dir=None):
        self.config = config or AppearanceConfig()
        self.face_region = face_region
        self.people = {}
        self.pending = {}
        self.output_dir = output_dir
        self.camera_colors = CameraColors()
        self.calibration_version = None
        # 외형 승계(S15P31A101-743): 포즈 루프가 넣고 외형 작업자가 그 사람 누적에 합친다. world_id → [(사본, 이어받기)]
        self._carry_lock = threading.Lock()
        self._carry = {}
        self.carry_events = {"merged": 0, "inherited": 0}

    def retain(self, active):
        self.people = {k: v for k, v in self.people.items() if k in active}
        self.pending = {k: v for k, v in self.pending.items() if k in active}
        with self._carry_lock:
            self._carry = {k: v for k, v in self._carry.items() if k in active}

    def snapshot(self, wid):
        """그 사람의 지금 누적 텍셀 사본(소실·흡수 때 포즈 루프가 들고 있는다). 누적이 없으면 합치기 대기분, 그것도 없으면 None."""
        acc = self.people.get(wid)
        if acc is not None:
            return acc.snapshot()
        with self._carry_lock:
            queued = self._carry.get(wid)
            return queued[0][0] if queued else None

    def carry_in(self, wid, snapshot, *, inherit):
        """wid 누적에 합칠 사본을 넣는다. inherit=True 는 다른 World ID 에서 이어받기, False 는 병합·같은 ID 재연결."""
        if snapshot is None:
            return
        with self._carry_lock:
            self._carry.setdefault(wid, []).append((snapshot, inherit))

    def apply_carry(self, wid):
        """누적이 있으면 대기 사본을 합친다(외형 작업자 스레드). 누적이 아직 없으면(체형 굽기 전) 기다린다."""
        acc = self.people.get(wid)
        if acc is None or acc.revision is None:
            return
        with self._carry_lock:
            queued = self._carry.pop(wid, [])
        for snapshot, inherit in queued:
            if acc.absorb(snapshot, inherit=inherit):
                self.carry_events["inherited" if inherit else "merged"] += 1
                log.info("외형 승계 world_id=%s kind=%s inherited=%.4f", wid,
                         "inherit" if inherit else "merge", acc.inherited_ratio())

    def track(self, track, t, *, identity_key=None):
        """identity_key: 같은 사람으로 누적을 잇는 기준 차수(포즈 루프의 revision_base, 순수 병합이면 그대로). 없으면 트랙 차수."""
        wid = track["world_id"]
        acc = self.people.get(wid)
        key = track["identity_revision"] if identity_key is None else identity_key
        if acc is not None:
            hold = bool(
                track.get("avatar_update_hold", False)
                or track.get("hold_appearance_update", False)
            )
            uncertain = track["identity_confidence"] == "IDENTITY_UNCERTAIN" or hold
            if uncertain or acc.revision != key:
                self.pending.pop(wid, None)
            confidence = "IDENTITY_UNCERTAIN" if uncertain else track["identity_confidence"]
            acc.track(key, confidence, t)

    def register(self, wid, state, t):
        rig = state.baked.rig
        if rig.texcoords is None or rig.texcoord_faces is None:
            raise ValueError("관측 텍스처에는 MHR TEXCOORD_0가 필요하다")
        acc = AppearanceAccumulator(rig.faces, rig.texcoords, rig.texcoord_faces,
                                   face_faces_from_rig(rig, self.face_region), config=self.config,
                                   camera_colors=self.camera_colors)
        old = self.people.get(wid)
        acc.last_confirmed = old.last_confirmed if old else None
        acc.track(getattr(state, "revision_base", state.revision), "CONFIRMED", t)
        # 체형만 다시 구워 등록이 바뀐 같은 사람은 누적 텍셀을 잇는다(MHR UV 는 체형과 무관, S15P31A101-743)
        acc.adopt(old)
        acc.stable = state.stable
        acc.ready = acc.stable >= self.config.freeze_frames
        self.people[wid] = acc
        self.pending.pop(wid, None)

    def _prepare(self, wid, acc, state, frame, doc, clock_ms, reserve_version, t):
        result = acc.result()
        version = (reserve_version(wid) if reserve_version is not None
                   else max(state.version, state.high_version) + 1)
        state.high_version = version
        asset = copy.deepcopy(state.asset)
        prev_version = max(state.history) if state.history else (version - 1 if version > 0 else None)
        # 새 버전은 지금 트랙 차수로 적는다(순수 병합 뒤면 남는 ID 의 새 차수, S15P31A101-661)
        asset.update(identity_revision=state.revision,
                     appearance_version=version, previous_appearance_version=prev_version,
                     created_at_ms=clock_ms(), calibration_version=frame.calibration_version,
                     input_provenance=copy.deepcopy(doc["input_provenance"]), face_policy=result.face_policy)
        pending = (asset, result, t)
        self.pending[wid] = pending
        return pending

    def update(self, frame, doc, snapshot, images, rig, states, upload, sender, clock_ms, admit, masks=None,
               *, reserve_version=None):
        if self.calibration_version != frame.calibration_version:
            self.camera_colors.gains.clear()
            for acc in self.people.values():
                acc.sharpness.clear()
            self.calibration_version = frame.calibration_version
        tracks = {tr["world_id"]: tr for tr in doc["tracks"]}
        meshes = {}
        for person in frame.people:
            state = states[person.world_id]
            if state.baked is not None and state.asset is not None and state.asset["identity_revision"] >= getattr(
                    state, "revision_base", state.revision):
                ref = snapshot["frames"].get(person.representative_camera)
                if ref is None or abs(ref["capture_time_ms"] - doc["t"]) > self.config.sync_tolerance_ms:
                    continue
                vertices = posed_world_vertices(state.baked, person)
                if vertices is not None:
                    meshes[person.world_id] = (vertices, state.baked.rig.faces)
        for person in frame.people:
            wid, t = person.world_id, doc["t"]
            state, acc = states[wid], self.people.get(wid)
            if acc is None or not (acc.ready or acc.carry_publish) or wid not in meshes or not admit():
                continue
            pending = self.pending.get(wid)
            if pending is None and not acc.ready:
                # 승계로 받은 텍셀만 있다: 새 관측 누적은 안정 프레임 뒤에, 받은 외형은 지금 한 번 낸다(S15P31A101-743)
                if not acc.due(t) or not admit():
                    continue
                pending = self._prepare(wid, acc, state, frame, doc, clock_ms, reserve_version, t)
            if pending is None:
                views = []
                for obs in tracks[wid].get("per_camera", []):
                    cid, ref = obs["camera_id"], snapshot["frames"].get(obs["camera_id"])
                    if (cid not in rig.cameras or cid not in images or ref is None
                            or ref["frame_ref"] != obs["frame_ref"]):
                        continue
                    # 메시가 없는 다른 사람(불확실/아직 체형 고정 전)은 bbox 전체를 가림막으로 뺀다.
                    view = AppearanceView(rig.cameras[cid], images[cid], ref["capture_time_ms"],
                                          bbox=obs["bbox"], mask=(masks.get((wid, cid)) if masks is not None
                                                                 else obs.get("mask")),
                                          keypoints_2d=obs.get("keypoints_2d")
                                          if obs.get("joint_layout", "COCO17") == "COCO17" else None)
                    boxes = [o["bbox"] for other, tr in tracks.items() if other != wid and other not in meshes
                             for o in tr.get("per_camera", []) if o["camera_id"] == cid]
                    if boxes:
                        mask = np.zeros(images[cid].shape[:2], np.uint8)
                        height, width = mask.shape
                        x0, y0, x1, y1 = np.clip(np.asarray(obs["bbox"], int), [0, 0, 0, 0],
                                                 [width, height, width, height])
                        mask[y0:y1, x0:x1] = 1
                        if view.mask is not None:
                            mask &= (view.mask > 0).astype(np.uint8)
                        for box in boxes:
                            x0, y0, x1, y1 = np.clip(np.asarray(box, int), [0, 0, 0, 0], [width, height, width, height])
                            mask[y0:y1, x0:x1] = 0
                        view = replace(view, mask=mask)
                    views.append(view)
                joints = getattr(person, "world_keypoints", None)
                acc.accumulate(wid, meshes[wid][0], views, t,
                    joints_world=None if joints is None else joints[COCO17_INDEX],
                    occluders=[m for other, m in meshes.items() if other != wid], admit=admit)
                if acc.last_sample == t:
                    # 느린 CPU 회차가 간격보다 길어도 다음 회차는 포즈를 보낼 여유를 둔다.
                    acc.last_sample = max(t, clock_ms())
                if not acc.due(t) or not admit():
                    continue
                pending = self._prepare(wid, acc, state, frame, doc, clock_ms, reserve_version, t)
            asset, result, sampled_t = pending
            if not admit():
                continue
            version = asset["appearance_version"]
            try:
                texture_uri = upload.png(wid, sender.epoch, version, "albedo.png", result.albedo)
                if not admit():
                    continue
                mask_uri = upload.png(wid, sender.epoch, version, "surface_source.png", result.surface)
            except Exception as error:
                if is_upload_conflict(error):
                    next_version = (reserve_version(wid) if reserve_version is not None
                                    else max(state.version, state.high_version, version) + 1)
                    state.high_version = max(state.high_version, next_version)
                    asset["appearance_version"] = next_version
                    asset["previous_appearance_version"] = (
                        max(state.history) if state.history else (next_version - 1 if next_version > 0 else None)
                    )
                raise
            if not admit():
                continue
            asset["texture"] = {"object_uri": texture_uri, "sha256": result.albedo.sha256,
                                "resolution": [result.resolution, result.resolution]}
            asset["surface_source"] = {"mask_uri": mask_uri, "ratios": result.ratios, "face_ratios": result.face_ratios}
            sender.asset(asset)
            state.asset, state.version = asset, version
            state.history[version] = copy.deepcopy(asset)
            acc.confirm(result, max(sampled_t, clock_ms()))
            del self.pending[wid]
            log.info("외형 버전 확정 world_id=%s version=%s observed=%.4f inherited=%.4f", wid, version,
                     result.ratios["OBSERVED"], acc.inherited_ratio())
            if self.output_dir is not None:
                try:
                    directory = save_result(self.output_dir, asset, result)
                    log.info("외형 진단 PNG world_id=%s version=%s directory=%s", wid, version, directory)
                except Exception as error:
                    log.warning("외형 진단 저장 실패: %s", type(error).__name__)
