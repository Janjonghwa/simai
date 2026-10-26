"""E-GATE-1 GPU Job: 같은 시각 카메라별 그림 → 카메라별 HMR 결과 묶음 (S15P31A101-87, 통합 이미지 HMR 경로).

    simai-exp02 <py> ai/experiments/hmr/run_multiview_hmr.py --run-id <id> --frames <egate1_frames 출력> \\
        --calibration <교실 내부 파라미터 기록> --seg-model <yolo26x-seg.pt> --seg-sha256 sha256:… [--prompt mask|bbox|mask_kp]

그림마다(카메라별 그림 하나):
1. 보정 K 로 왜곡을 편다(`fs3db_common.resolve_k`·`prepare_image`, -238 과 같은 경로. K 가 없으면 그 그림을 실패로 둔다, MoGe 를 띄우지 않음).
2. 원본 그림에서 YOLO26x-pose(Tier 0 와 같은 검출기)로 사람 bbox·COCO17 keypoint 를 잡는다. Tier 0 per_camera 관측 대신이다.
3. 편 그림에서 YOLO26-seg 로 사람 마스크를 잡고 `simai_ai.hmr.prompt.build_prompts` 로 프롬프트(bbox·마스크·몸통·고관절 keypoint)를 만든다.
4. Fast SAM 3D Body(공식 `process_one_image`)에 bbox(+ 마스크)를 넣는다. 마스크가 있는 사람과 없는 사람은 따로 부른다
   (공식 API 는 한 번에 모두 마스크가 있거나 모두 없어야 한다. 없는 사람에게 사각형 마스크를 지어 주지 않는다).
   `--prompt mask_kp` 는 body 모드에서 몸통·고관절 keypoint 를 모델의 keypoint 프롬프트(run_keypoint_prompt)로 한 번 더 넣는다(GPU 미검증 변형).
5. 사람마다 카메라 좌표 3D 관절·메시, 몸 방향·관절 회전(`simai_ai.hmr.pose.pose_from_hmr`), 마스크를 `egate1_bundle` 형식으로 쓴다.

World ID 연결·다시점 집계·지표는 노트북의 `egate1_eval.py` 가 Tier 0 world_tracks 와 함께 한다(이 Job 은 Tier 0 결과를 읽지 않는다).
결과: $SIMAI_ROOT/runs/exp02/<run id>/ (exp_job.py 가 metrics.json 을 Run 기록 지표로 줄인다). 사람이 찍힌 입력이라 결과는 커밋하지 않는다.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import traceback
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))
import egate1_bundle as B  # noqa: E402

PROMPT_MODES = ("bbox", "mask", "mask_kp")
PERSON_CLASS = 0


# ------------------------------------------------------------------ 순수 보조(시험 대상, torch·cv2 없음)


def hmr_groups(prompts, mode: str) -> list[tuple[list[int], bool]]:
    """HMR 호출 묶음: [(prompts 의 번호 목록, 마스크를 넣는지)]. 공식 API 는 한 호출 안에서 모두 마스크가 있거나 모두 없어야 한다."""
    if mode not in PROMPT_MODES:
        raise ValueError(f"prompt 는 {PROMPT_MODES} 중 하나: {mode}")
    if mode == "bbox":
        return [(list(range(len(prompts))), False)] if prompts else []
    with_mask = [i for i, p in enumerate(prompts) if p.mask is not None]
    without = [i for i, p in enumerate(prompts) if p.mask is None]
    return [(g, m) for g, m in ((with_mask, True), (without, False)) if g]


def crop_prompt(xy_crop: np.ndarray, labels: np.ndarray) -> np.ndarray:
    """crop 정규화 좌표([-0.5, 0.5], 모델 `_full_to_crop` 결과) + MHR70 번호 → 모델 keypoint 프롬프트 (N, S, 3).

    공식 손목 프롬프트(sam3d_body.py)와 같은 규칙: [0, 1] 로 옮기고, crop 밖이거나 빈 칸(번호 −2)은 [0, 0, −2] 다.
    """
    xy = np.asarray(xy_crop, dtype=np.float32)
    lab = np.asarray(labels, dtype=np.float32)
    invalid = (lab < 0) | (xy < -0.5).any(-1) | (xy > 0.5).any(-1) | ~np.isfinite(xy).all(-1)
    out = np.concatenate([np.clip(xy + 0.5, 0.0, 1.0), lab[..., None]], axis=-1)
    out[invalid] = (0.0, 0.0, -2.0)
    return out


def all_dummy(prompt: np.ndarray) -> bool:
    """keypoint 프롬프트 묶음 (N, S, 3) 이 모두 빈 칸(번호 < 0)인지. 그러면 재추론을 하지 않는다(넣을 정보가 없다)."""
    p = np.asarray(prompt)
    return p.size == 0 or bool((p[..., 2] < 0).all())


def person_arrays(out: dict, prompt, parents: np.ndarray | None, hands_estimated: bool) -> dict[str, np.ndarray]:
    """HMR 출력 사람 하나 → 묶음 배열. 카메라 좌표 = pred_* + pred_cam_t. 몸 방향·관절 회전은 리그 부모가 있을 때만."""
    from simai_ai.hmr.pose import pose_from_hmr

    cam_t = np.asarray(out["pred_cam_t"], dtype=np.float64).reshape(3)
    arrays = {"keypoints_cam": (np.asarray(out["pred_keypoints_3d"], dtype=np.float64)[:70] + cam_t).astype(np.float32),
              "vertices_cam": (np.asarray(out["pred_vertices"], dtype=np.float64) + cam_t).astype(np.float16)}
    if parents is not None and "pred_global_rots" in out:
        pose = pose_from_hmr(np.asarray(out["pred_global_rots"]), parents, hands_estimated=hands_estimated)
        arrays["camera_from_rig"] = pose.camera_from_rig
        arrays["joint_rotations"] = B.joint_rotations_array(pose.joint_rotations)
    if prompt.mask is not None:
        packed = B.pack_mask(prompt.mask)
        if packed:
            arrays.update(packed)
    return arrays


# ------------------------------------------------------------------ GPU 쪽


class KeypointPromptHook:
    """body 모드 추론 뒤 몸통·고관절 keypoint 를 모델의 keypoint 프롬프트로 한 번 더 넣는다(공식 손목 프롬프트와 같은 호출).

    shortcut: 모델 내부(run_keypoint_prompt, _full_to_crop)를 부른다. 판이 바뀌면 깨질 수 있어 출력 키를 확인하고 없으면 오류로 둔다.
    """

    def __init__(self, model) -> None:
        import torch

        self.model, self.torch, self.prompts, self.applied, self.skipped_all_dummy = model, torch, None, 0, 0
        self.last = None  # 마지막 호출: APPLIED | SKIPPED_ALL_DUMMY
        orig = model.run_inference

        def wrapped(img, batch, *a, inference_type="full", **kw):
            out = orig(img, batch, *a, inference_type=inference_type, **kw)
            if self.prompts is None:
                return out
            if inference_type != "body":
                raise RuntimeError("keypoint 프롬프트 주입은 body 모드만 지원한다")
            missing = [k for k in ("image_embeddings", "condition_info", "mhr") if k not in out]
            if missing:
                raise RuntimeError(f"모델 출력에 {missing} 가 없어 keypoint 프롬프트를 넣을 수 없다")
            if all_dummy(self.prompts):
                self.skipped_all_dummy += 1
                self.last = "SKIPPED_ALL_DUMMY"
                return out
            kp = torch.as_tensor(self.prompts, dtype=torch.float32, device=batch["img"].device)
            xy = model._full_to_crop(batch, kp[..., :2].clone())
            crop = crop_prompt(xy.detach().cpu().numpy(), kp[..., 2].cpu().numpy())
            if all_dummy(crop):  # 모두 crop 밖이면 빈 프롬프트만 남는다
                self.skipped_all_dummy += 1
                self.last = "SKIPPED_ALL_DUMMY"
                return out
            out, _ = model.run_keypoint_prompt(batch, out, torch.as_tensor(crop, device=kp.device))
            self.applied += 1
            self.last = "APPLIED"
            return out

        model.run_inference = wrapped


def yolo_people(model, img, conf: float, imgsz: int):
    r = model.predict(img, classes=[PERSON_CLASS], conf=conf, imgsz=imgsz, verbose=False)[0]
    boxes = r.boxes.xyxy.cpu().numpy() if r.boxes is not None else np.zeros((0, 4))
    scores = r.boxes.conf.cpu().numpy() if r.boxes is not None else np.zeros(0)
    kps = r.keypoints.data.cpu().numpy() if getattr(r, "keypoints", None) is not None else None
    return boxes, scores, kps, r


def seg_instances(model, img, conf: float, imgsz: int):
    import cv2

    from simai_ai.hmr.prompt import SegInstance

    h, w = img.shape[:2]
    boxes, scores, _k, r = yolo_people(model, img, conf, imgsz)
    if r.masks is None:
        return []
    out = []
    for b, s, m in zip(boxes, scores, r.masks.data.cpu().numpy() > 0.5):
        if m.shape != (h, w):
            m = cv2.resize(m.astype(np.uint8), (w, h), interpolation=cv2.INTER_NEAREST) > 0
        out.append(SegInstance(tuple(float(v) for v in b), float(s), m))
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--run-id", required=True)
    ap.add_argument("--frames", required=True, type=Path, help="egate1_frames 출력(index.json)")
    ap.add_argument("--calibration", required=True, help="보정 K: simai.intrinsics_estimate 기록 또는 calibration 계약 문서")
    ap.add_argument("--variant", default="dinov3")
    ap.add_argument("--preset", default="opt")
    ap.add_argument("--inference-type", default="body", choices=["body", "full"])
    ap.add_argument("--prompt", default="mask", choices=PROMPT_MODES)
    ap.add_argument("--pose-model", default=None, help="YOLO26x-pose .pt (기본 $SIMAI_ROOT/weights/yolo/yolo26x-pose.pt)")
    ap.add_argument("--seg-model", default=None, help="YOLO26-seg .pt (bbox 모드면 생략 가능)")
    ap.add_argument("--seg-sha256", default=None)
    ap.add_argument("--det-conf", type=float, default=0.5)
    ap.add_argument("--seg-conf", type=float, default=0.25)
    ap.add_argument("--imgsz", type=int, default=1280)
    ap.add_argument("--max-instants", type=int, default=0)
    a = ap.parse_args(argv)
    if a.prompt != "bbox" and not (a.seg_model and a.seg_sha256):
        ap.error("mask·mask_kp 는 --seg-model·--seg-sha256 이 필요하다")
    if a.prompt == "mask_kp" and a.inference_type != "body":
        ap.error("mask_kp 는 --inference-type body 만")

    import fs3db_common as C

    env = C.apply_preset(a.preset)
    out_dir = C.OUT / a.run_id
    if (out_dir / B.FRAMES_FILE).exists():
        print(f"run_multiview_hmr: {out_dir} 에 결과가 이미 있다(덮어쓰지 않는다)", file=sys.stderr)
        return 4
    index = json.loads((a.frames / "index.json").read_text(encoding="utf-8"))
    instants = index["instants"][: a.max_instants or None]
    res = {"run_id": a.run_id, "args": {k: str(v) for k, v in vars(a).items()}, "preset_env": env, "env": C.env_summary(),
           "bundle_format": B.BUNDLE_FORMAT, "bundle_version": B.BUNDLE_VERSION,
           "input_provenance": {"frames": {"source": "FILE", "dataset_id": index.get("dataset_id", "unknown")}, "is_upper_bound": False},
           "model_version": f"Fast-SAM-3D-Body@d72aa36 {a.variant} ({a.preset}, {a.inference_type}) + YOLO26x-pose"
                            + ("" if a.prompt == "bbox" else " + YOLO26x-seg") + f", prompt {a.prompt}",
           "images": []}
    gs = C.GpuSampler()
    try:
        import cv2
        from ultralytics import YOLO

        from simai_ai.calibration.intrinsics.estimate import WeightsRef
        from simai_ai.hmr.prompt import HmrImageMap, PersonObservation, build_prompts, keypoint_prompt_array
        from simai_ai.hmr.rig import load_mhr_rig

        pose_model = YOLO(a.pose_model or str(C.YOLO))
        seg_model = YOLO(str(WeightsRef(a.seg_model, a.seg_sha256).verify()), task="segment") if a.prompt != "bbox" else None
        rig = load_mhr_rig(C.ckpt_dir(a.variant) / "assets" / "mhr_model.pt")
        est, _comps, timed, info = C.build(a.variant)
        res["load"] = info
        hook = KeypointPromptHook(est.model) if a.prompt == "mask_kp" else None
        out_dir.mkdir(parents=True, exist_ok=True)
        np.save(out_dir / B.FACES_FILE, np.asarray(est.faces, dtype=np.int32))
        kins: dict[str, dict] = {}
        for inst in instants:
            for cid, f in sorted(inst["frames"].items()):
                row = {"instant": inst["instant"], "camera_id": cid, "image": f["file"], "capture_time_ms": f["capture_time_ms"],
                       "frame_ref": {"stream_epoch": f["stream_epoch"], "frame_sequence": f["frame_sequence"]},
                       "timestamp_source": f.get("timestamp_source"), "people": []}
                arrays: list[dict] = []
                try:
                    img = cv2.imread(str(a.frames / f["file"]))
                    if img is None:
                        raise OSError(f"그림을 읽지 못했다: {f['file']}")
                    size = [img.shape[1], img.shape[0]]
                    if cid not in kins:
                        kins[cid] = C.resolve_k(size, [(C.K_CALIBRATION, a.calibration)], camera_id=cid)
                    kin = dict(kins[cid])
                    if kin["source"] == C.K_MOGE:
                        raise RuntimeError(f"{cid}: 보정 K 를 쓸 수 없다(MoGe 추정으로 돌지 않는다): {kin.get('skipped')}")
                    img_u, K_used = C.prepare_image(img, kin)
                    row.update(image_size=size, K_used=np.asarray(K_used).tolist(), undistorted=bool(kin.get("undistorted")),
                               k_input={k: kin.get(k) for k in ("source", "calibration_source", "file", "file_sha256", "undistorted")})
                    boxes, scores, kps, _r = yolo_people(pose_model, img, a.det_conf, a.imgsz)
                    obs = [PersonObservation(cid, tuple(float(v) for v in b), tuple(map(tuple, k.tolist())) if kps is not None else (), score=float(s))
                           for b, s, k in zip(boxes, scores, kps if kps is not None else [None] * len(boxes))]
                    inst_seg = seg_instances(seg_model, img_u, a.seg_conf, a.imgsz) if seg_model is not None else []
                    imap = HmrImageMap.from_intrinsics(cid, kin["K_given"], size, kin["distortion"]["model"], kin["distortion"]["coeffs"], K_used)
                    prompts, skipped = build_prompts(obs, inst_seg, imap)
                    row["skipped"] = [s.reason for s in skipped]
                    rgb = cv2.cvtColor(img_u, cv2.COLOR_BGR2RGB)
                    t_hmr = 0.0
                    outs: dict[int, dict] = {}
                    for idx, use_mask in hmr_groups(prompts, a.prompt):
                        kw = {"bboxes": np.asarray([prompts[i].bbox for i in idx], np.float32),
                              "cam_int": C.torch_().as_tensor(np.asarray(K_used, np.float32).reshape(1, 3, 3))}
                        if use_mask:
                            kw["masks"] = np.stack([prompts[i].mask for i in idx]).astype(np.uint8)
                        if hook is not None:
                            hook.prompts = np.stack([keypoint_prompt_array(prompts[i]) for i in idx])
                        C.sync()
                        t0 = time.perf_counter()
                        res_g = est.process_one_image(rgb, inference_type=a.inference_type, hand_box_source="body_decoder", **kw)
                        C.sync()
                        t_hmr += (time.perf_counter() - t0) * 1000
                        timed.pop()
                        if len(res_g) != len(idx):
                            raise RuntimeError(f"HMR 출력 {len(res_g)}명 ≠ 프롬프트 {len(idx)}명")
                        outs.update(zip(idx, res_g))
                        if hook is not None:
                            row.setdefault("kp_prompt", []).append(hook.last)
                    for n, i in enumerate(sorted(outs)):
                        p = prompts[i]
                        arrays.append(person_arrays(outs[i], p, rig.parents, a.inference_type == "full"))
                        row["people"].append({
                            "candidate_id": f"{B.image_key(inst['instant'], cid)}_p{n}", "bbox": list(p.observation.bbox),
                            "score": p.observation.score, "keypoints_2d": [list(k) for k in p.observation.keypoints],
                            "prompt": {"mode": a.prompt, "mask_iou": p.mask_iou, "mask_used": p.mask is not None and a.prompt != "bbox",
                                       "keypoints": [list(k) for k in p.keypoints], "hmr_bbox": list(p.bbox)},
                        })
                    finite = all(np.isfinite(x["keypoints_cam"]).all() for x in arrays)
                    row.update(hmr_ms=round(t_hmr, 1), n_people=len(arrays), result="SUCCESS" if finite else "NAN_OUTPUT")
                    if not finite:
                        arrays, row["people"] = [], []
                except Exception as e:  # noqa: BLE001
                    row.update(result="ERROR", error=f"{type(e).__name__}: {e}"[:400], traceback=traceback.format_exc()[-1500:])
                    arrays, row["people"] = [], []
                    row.setdefault("image_size", [0, 0])
                B.write_image(out_dir, row, arrays)
                res["images"].append({k: v for k, v in row.items() if k not in ("people", "traceback")})
                print(f"[{a.run_id}] i{inst['instant']} {cid} {row['result']} n={row.get('n_people')} hmr={row.get('hmr_ms')} ms", flush=True)
        if hook is not None:
            res["kp_prompt_applied"] = hook.applied
            res["kp_prompt_skipped_all_dummy"] = hook.skipped_all_dummy
        ok = [r for r in res["images"] if r.get("result") == "SUCCESS"]
        res["result"] = "SUCCESS" if ok and len(ok) == len(res["images"]) else ("PARTIAL" if ok else "FAIL")
        res["counts"] = {"images": len(res["images"]), "success": len(ok), "people": sum(r.get("n_people") or 0 for r in ok)}
        res["hmr_ms"] = C.pct([r["hmr_ms"] for r in ok if r.get("hmr_ms")])
    except Exception as e:  # noqa: BLE001
        res.update(result="ERROR", error=f"{type(e).__name__}: {e}"[:600], traceback=traceback.format_exc()[-3000:])
    res.update(gs.stop())
    res["counts"] = {**(res.get("counts") or {}), **_prompt_counts(out_dir)}
    C.write_json(out_dir / "metrics.json", res)
    print(f"[{a.run_id}] RESULT {res['result']} {res.get('error', '')[:300]}")
    return 0 if res["result"] in ("SUCCESS", "PARTIAL") else 1


def _prompt_counts(out_dir: Path) -> dict[str, int]:
    """묶음 줄에서 프롬프트 집계(마스크 쓴 사람, keypoint 프롬프트 관절 수)."""
    path = out_dir / B.FRAMES_FILE
    if not path.is_file():
        return {}
    people = [p for line in path.read_text(encoding="utf-8").splitlines() if line.strip() for p in json.loads(line).get("people", [])]
    return {"people_total": len(people), "mask_used": sum(1 for p in people if p["prompt"].get("mask_used")),
            "kp_prompt_joints": sum(len(p["prompt"].get("keypoints") or []) for p in people)}


if __name__ == "__main__":
    raise SystemExit(main())
