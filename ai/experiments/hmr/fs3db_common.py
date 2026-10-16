# EXP-02 E-GATE-0 (S15P31A101-86): 선행 PoC Moge-3-test/pocs/human_fast_sam3d_g3_lite/scripts 에서 옮김. 경로·입력 준비만 바꿨다.
"""Fast SAM 3D Body 공통 유틸 — 공식 API(load_sam_3d_body / SAM3DBodyEstimator.process_one_image)만 사용. 공식 코드 수정 없음.

Fast 의 가속 설정은 대부분 환경변수이고 일부는 import 시점에 읽힌다 → 프리셋은 **프로세스 시작 시 import 전에** apply_preset() 으로 적용.
  off      : Fast 가속 끔 (원본 SAM 3D Body 경로에 가장 가까움: 모든 layer interm 예측, keypoint prompt, correctives, CPU hand prep, 순차 decoder)
  opt      : 공식 run_demo.sh 의 비-compile 설정 (keypoint prompt 생략, interm 예측 body 0,1,2 / hand 0,1, correctives 끔, GPU hand prep, 병렬 decoder 허용)
  opt_compile : opt + torch.compile (공식 run_demo.sh: backbone·decoder compile, reduce-overhead)
병렬(merged backbone) decoder 는 공식 코드상 hand_box_source=yolo_pose + YOLO-Pose keypoints 가 있을 때만 동작 (sam3d_body.py run_inference).
→ 외부 bbox 로 이 경로를 쓰려면 PrecomputedPoseDetector 로 같은 bbox 에 IoU 매칭된 YOLO26-Pose keypoints 를 함께 전달.
"""
import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

ROOT = Path(os.environ.get("SIMAI_ROOT", "/workspace/simai"))
IMPL = "fast"                                       # 원본 구현 비교는 off 프리셋(원본 경로에 가장 가까움, PoC 에서 2D 0.65 px 일치)으로 대신
REPO = ROOT / "third_party" / "Fast-SAM-3D-Body"
CKPT = ROOT / "weights"
OUT = ROOT / "runs" / "exp02"
INPUTS = ROOT / "datasets" / "hmr_poc14"
YOLO = CKPT / "yolo" / "yolo26x-pose.pt"

PRESETS = {
    "off": {"USE_COMPILE": "0", "SKIP_KEYPOINT_PROMPT": "0", "MHR_NO_CORRECTIVES": "0", "GPU_HAND_PREP": "0",
            "PARALLEL_DECODERS": "0", "IMG_SIZE": "0", "MHR_USE_CUDA_GRAPH": "0", "DEBUG_NAN": "0"},
    "opt": {"USE_COMPILE": "0", "GPU_HAND_PREP": "1", "LAYER_DTYPE": "fp32", "SKIP_KEYPOINT_PROMPT": "1", "IMG_SIZE": "512",
            "KEYPOINT_PROMPT_INTERM_INTERVAL": "999", "BODY_INTERM_PRED_LAYERS": "0,1,2", "HAND_INTERM_PRED_LAYERS": "0,1",
            "MHR_NO_CORRECTIVES": "1", "PARALLEL_DECODERS": "1", "MHR_USE_CUDA_GRAPH": "0", "DEBUG_NAN": "0",
            # 공식 run_demo.sh 의 FOV 설정 (MoGe2-S, normal head 생략, level 0). TRT 엔진은 별도 빌드 시에만 FOV_TRT=1
            "FOV_MODEL": "s", "FOV_FAST": "1", "FOV_LEVEL": "0", "FOV_TRT": "0"},
}
# off_kp: 가속 전부 끄고 keypoint prompt 재추론까지 켬 → 원본 대비 2D 20 px / cam_t 0.87 m 차이 (logs/bisect, compare_bisect_*.json).
# Fast 경로의 keypoint prompt 단계가 원본과 다른 결과를 냄 → 기준선 off 는 SKIP_KEYPOINT_PROMPT=1 (원본과 2D 0.65 px 일치).
PRESETS["off_kp"] = dict(PRESETS["off"])
PRESETS["off"]["SKIP_KEYPOINT_PROMPT"] = "1"
# off 는 FOV 도 원본 기본값 (MoGe2-L, level 9) = 기존 PoC 와 같은 FOV 모델
PRESETS["off"].update({"FOV_MODEL": "l", "FOV_FAST": "0", "FOV_TRT": "0"})
PRESETS["off_kp"].update({"FOV_MODEL": "l", "FOV_FAST": "0", "FOV_TRT": "0"})
PRESETS["opt_compile"] = {**PRESETS["opt"], "USE_COMPILE": "1", "USE_COMPILE_BACKBONE": "1", "DECODER_COMPILE": "1",
                          "COMPILE_MODE": "reduce-overhead", "COMPILE_WARMUP_BATCH_SIZES": "1"}


def apply_preset(name, overrides=()):
    """name 프리셋 + 'K=V' overrides 를 os.environ 에 적용 (sam_3d_body import 전에 호출)."""
    env = dict(PRESETS[name])
    for kv in overrides:
        k, v = kv.split("=", 1); env[k] = v
    for k in ("USE_TRT_BACKBONE", "TRT_BACKBONE_PATH", "LAYER_DTYPE", "COMPILE_WARMUP_BATCH_SIZES") + tuple(env):
        os.environ.pop(k, None)
    os.environ.update(env)
    os.environ.setdefault("PYOPENGL_PLATFORM", "egl")
    return env


sys.path.insert(0, str(REPO))
import numpy as np  # noqa: E402


def torch_():
    import torch
    return torch


def mb(x):
    return round(x / 2**20, 1)


def sync():
    t = torch_()
    if t.cuda.is_available():
        t.cuda.synchronize()


def gpu_used_mb():
    try:
        out = subprocess.run(["nvidia-smi", "--query-gpu=memory.used", "--format=csv,noheader,nounits"], capture_output=True, text=True, timeout=5).stdout
        return int(out.strip().splitlines()[0])
    except Exception:
        return -1


class GpuSampler:
    def __init__(self, period=0.2):
        self.base = gpu_used_mb(); self.peak = self.base; self.period = period
        self._stop = threading.Event(); self._t = threading.Thread(target=self._run, daemon=True); self._t.start()

    def _run(self):
        while not self._stop.is_set():
            self.peak = max(self.peak, gpu_used_mb()); time.sleep(self.period)

    def stop(self):
        self._stop.set(); self._t.join(timeout=2)
        return {"gpu_used_base_mb": self.base, "gpu_used_peak_mb": self.peak}


def ckpt_dir(variant):
    """dinov3 = Fast 공식 config(float16, setup 이 만든 링크 폴더) / dinov3-origcfg = HF 원본 config(bfloat16)."""
    return CKPT / ("sam-3d-body-dinov3-fastcfg" if variant == "dinov3" else "sam-3d-body-dinov3")


class Timed:
    def __init__(self):
        self.records = []

    def wrap(self, obj, meth, stage):
        fn = getattr(obj, meth); t = torch_()

        def w(*a, **k):
            sync(); t.cuda.reset_peak_memory_stats(); t0 = time.perf_counter()
            r = fn(*a, **k)
            sync()
            self.records.append({"stage": stage, "ms": (time.perf_counter() - t0) * 1000, "peak_alloc_mb": mb(t.cuda.max_memory_allocated())})
            return r
        setattr(obj, meth, w)

    def pop(self):
        r, self.records = self.records, []
        return r


class PrecomputedPoseDetector:
    """공식 HumanDetector(yolo_pose) 와 같은 반환 형식 {"boxes", "keypoints"} 를 미리 계산된 값으로 돌려준다.
    process_one_image 의 공식 경로(병렬 decoder 조건 포함)를 그대로 타기 위한 어댑터. 다음 호출에 쓸 값을 set() 으로 지정."""
    name = "yolo_pose(precomputed)"

    def __init__(self):
        self.boxes = np.zeros((0, 4), np.float32); self.kps = np.zeros((0, 17, 3), np.float32)

    def set(self, boxes, kps):
        self.boxes = np.asarray(boxes, np.float32).reshape(-1, 4); self.kps = np.asarray(kps, np.float32).reshape(-1, 17, 3)

    def run_human_detection(self, img, **kw):
        return {"boxes": self.boxes.copy(), "keypoints": self.kps.copy()}


def match_kps(ext_boxes, yolo_boxes, yolo_kps, thr=0.3):
    """외부 bbox 마다 IoU 최대 YOLO-Pose 검출의 keypoints. 매칭 실패 시 conf 0 keypoints (공식 코드의 '없음' 표현과 같음)."""
    out = np.zeros((len(ext_boxes), 17, 3), np.float32); ious = []
    for i, b in enumerate(ext_boxes):
        best, bj = 0.0, -1
        for j, y in enumerate(yolo_boxes):
            x1, y1 = max(b[0], y[0]), max(b[1], y[1]); x2, y2 = min(b[2], y[2]), min(b[3], y[3])
            inter = max(0, x2 - x1) * max(0, y2 - y1)
            u = (b[2] - b[0]) * (b[3] - b[1]) + (y[2] - y[0]) * (y[3] - y[1]) - inter
            v = inter / max(u, 1e-9)
            if v > best:
                best, bj = v, j
        if bj >= 0 and best >= thr:
            out[i] = yolo_kps[bj]
        ious.append(round(best, 3))
    return out, ious


def build(variant="dinov3", detector=None, detector_model=None, fov=None, device="cuda", log=print, precomputed_pose=False):
    """구성요소별 로드 시간·VRAM 증가 기록. detector: None | 'yolo_pose' | 'vitdet'. fov: None | 'moge2'."""
    t = torch_()
    from sam_3d_body import SAM3DBodyEstimator, load_sam_3d_body
    info = {"variant": variant, "detector": detector, "fov": fov, "load": [], "env": {k: os.environ.get(k) for k in sorted(set(sum([list(v) for v in PRESETS.values()], [])) | {"USE_TRT_BACKBONE", "TRT_BACKBONE_PATH", "FOV_MODEL", "FOV_FAST", "FOV_TRT", "FOV_LEVEL"})}}
    timed = Timed(); comps = {}

    def step(name, fn):
        sync(); a0 = t.cuda.memory_allocated(); t.cuda.reset_peak_memory_stats(); t0 = time.perf_counter()
        try:
            obj = fn()
        except (t.cuda.OutOfMemoryError, RuntimeError) as e:
            oom = isinstance(e, t.cuda.OutOfMemoryError) or "out of memory" in str(e)
            info["load"].append({"component": name, "result": "OOM_AT_LOAD" if oom else "ERROR", "error": str(e)[:300],
                                 "alloc_before_mb": mb(a0), "peak_alloc_mb": mb(t.cuda.max_memory_allocated()), "gpu_used_mb": gpu_used_mb()})
            log(f"[load] FAIL {info['load'][-1]}")
            if oom:
                err = t.cuda.OutOfMemoryError(f"load {name}: {e}"); err.load_info = info
                raise err from e
            raise
        sync()
        rec = {"component": name, "result": "OK", "load_s": round(time.perf_counter() - t0, 2), "alloc_delta_mb": mb(t.cuda.memory_allocated() - a0),
               "alloc_total_mb": mb(t.cuda.memory_allocated()), "reserved_total_mb": mb(t.cuda.memory_reserved()), "peak_alloc_mb": mb(t.cuda.max_memory_allocated())}
        info["load"].append(rec); log(f"[load] {rec}")
        return obj

    d = ckpt_dir(variant)
    model, cfg = step(f"hmr_{variant}", lambda: load_sam_3d_body(str(d / "model.ckpt"), device=device, mhr_path=str(d / "assets" / "mhr_model.pt")))
    info["hmr_params_M"] = round(sum(p.numel() for p in model.parameters()) / 1e6, 1)
    info["hmr_param_dtypes"] = sorted({str(p.dtype) for p in model.parameters()})
    info["fp16_type_cfg"] = f"USE_FP16={cfg.TRAIN.USE_FP16} {cfg.TRAIN.FP16_TYPE}"
    if precomputed_pose:
        comps["det"] = PrecomputedPoseDetector()
    elif detector:
        from tools.build_detector import HumanDetector
        kw = {"model": detector_model} if detector == "yolo_pose" and detector_model else {}
        comps["det"] = step(f"detector_{detector}", lambda: HumanDetector(name=detector, device=device, **kw))
        timed.wrap(comps["det"], "run_human_detection", "detect")
    if fov:
        from tools.build_fov_estimator import FOVEstimator
        comps["fov"] = step(f"fov_{fov}", lambda: FOVEstimator(name=fov, device=device))
        timed.wrap(comps["fov"], "get_cam_intrinsics", "fov")
    est = step("estimator_init(warmup)", lambda: SAM3DBodyEstimator(sam_3d_body_model=model, model_cfg=cfg, human_detector=comps.get("det"),
                                                                    human_segmentor=None, fov_estimator=comps.get("fov")))
    timed.wrap(est.model, "run_inference", "hmr")
    return est, comps, timed, info


def run_one(est, timed, img_bgr, bboxes=None, cam_int=None, inference_type="full", bbox_thr=0.5, hand_box_source="body_decoder"):
    """공식 process_one_image 1 회 (RGB 입력, 기존 PoC 와 동일) + 구간별 시간."""
    import cv2
    t = torch_()
    rgb = cv2.cvtColor(img_bgr, cv2.COLOR_BGR2RGB)
    sync(); t.cuda.reset_peak_memory_stats(); t0 = time.perf_counter()
    kw = {}
    if bboxes is not None:
        kw["bboxes"] = np.asarray(bboxes, np.float32).reshape(-1, 4)
    if cam_int is not None:
        kw["cam_int"] = t.as_tensor(np.asarray(cam_int, np.float32).reshape(1, 3, 3))
    if IMPL != "orig":
        kw["hand_box_source"] = hand_box_source          # 원본 API 에는 없는 인자
    out = est.process_one_image(rgb, bbox_thr=bbox_thr, inference_type=inference_type, **kw)
    sync()
    total = (time.perf_counter() - t0) * 1000
    st = {}
    for r in timed.pop():
        st[r["stage"] + "_ms"] = round(st.get(r["stage"] + "_ms", 0) + r["ms"], 2)
        st[r["stage"] + "_peak_alloc_mb"] = r["peak_alloc_mb"]
    st["process_one_image_ms"] = round(total, 2)
    st["other_ms"] = round(total - sum(v for k, v in st.items() if k.endswith("_ms") and k != "process_one_image_ms"), 2)
    st["peak_alloc_mb"] = mb(t.cuda.max_memory_allocated()); st["peak_reserved_mb"] = mb(t.cuda.max_memory_reserved())
    st["n_people"] = len(out)
    return out, st


def finite(out):
    keys = ("pred_keypoints_3d", "pred_keypoints_2d", "pred_vertices", "pred_cam_t", "pred_joint_coords")
    return all(np.isfinite(np.asarray(p[k])).all() for p in out for k in keys if k in p)


def save_native(path, out):
    path.parent.mkdir(parents=True, exist_ok=True)
    d = {}
    for i, p in enumerate(out):
        for k, v in p.items():
            if v is not None:
                d[f"p{i}__{k}"] = np.asarray(v)
    np.savez_compressed(path, n_people=len(out), **d)


def save_meshes(dirpath, stem, out, faces):
    import trimesh
    dirpath.mkdir(parents=True, exist_ok=True)
    paths = []
    for i, p in enumerate(out):
        f = dirpath / f"{stem}_p{i}.ply"
        trimesh.Trimesh(p["pred_vertices"] + p["pred_cam_t"][None], faces, process=False).export(f); paths.append(str(f))
    return paths


def render(img_bgr, out, faces):
    from tools.vis_utils import visualize_sample_together
    return visualize_sample_together(img_bgr, out, faces).astype(np.uint8)


def baseline_inputs(stem):
    """bbox(YOLO26x-Pose, conf>=0.5) 와 K(MoGe2-L FOV, 중심 주점) 를 한 번 계산해 OUT/_inputs/<stem>.npz 에 캐시한다.
    PoC 는 다른 PoC 출력(ViTDet bbox, MoGe2-L K)을 읽었으나 여기에는 그 파일이 없어 같은 종류의 값을 직접 만든다."""
    import cv2
    t = torch_()
    img = cv2.imread(str(INPUTS / f"{stem}.jpg"))
    H, W = img.shape[:2]
    cache = OUT / "_inputs" / f"{stem}.npz"
    if not cache.exists():
        from tools.build_detector import HumanDetector
        from tools.build_fov_estimator import FOVEstimator
        det = HumanDetector(name="yolo_pose", device="cuda", model=str(YOLO))
        r = det.run_human_detection(img, det_cat_id=0, bbox_thr=0.5, nms_thr=0.3, default_to_full_image=False)
        del det
        os.environ["FOV_MODEL"], os.environ["FOV_FAST"] = "l", "0"
        fov = FOVEstimator(name="moge2", device="cuda")
        K = np.asarray(fov.get_cam_intrinsics(cv2.cvtColor(img, cv2.COLOR_BGR2RGB)).cpu()).reshape(3, 3).astype(np.float32)
        del fov
        t.cuda.empty_cache()
        cache.parent.mkdir(parents=True, exist_ok=True)
        np.savez(cache, boxes=np.asarray(r["boxes"], np.float32).reshape(-1, 4), K=K)
    z = np.load(cache)
    return img, z["boxes"], z["K"]


def env_summary():
    t = torch_()
    d = {"torch": t.__version__, "cuda": t.version.cuda, "cuda_available": t.cuda.is_available()}
    if t.cuda.is_available():
        p = t.cuda.get_device_properties(0); d.update(gpu=p.name, vram_total_mb=mb(p.total_memory), free_mb=mb(t.cuda.mem_get_info()[0]))
    try:
        d["repo_commit"] = subprocess.run(["git", "-C", str(REPO), "rev-parse", "HEAD"], capture_output=True, text=True).stdout.strip()
    except Exception:
        pass
    try:   # 노트북 전원 상태가 성능을 좌우함 (배터리: power limit 35 W, SW power cap) → 매 실행 기록
        d["gpu_power"] = subprocess.run(["nvidia-smi", "--query-gpu=power.limit,power.default_limit,pstate,clocks.max.sm,temperature.gpu",
                                         "--format=csv,noheader"], capture_output=True, text=True, timeout=5).stdout.strip()
    except Exception:
        pass
    return d


def write_json(path, obj):
    path = Path(path); path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, indent=1, ensure_ascii=False, default=lambda o: o.tolist() if hasattr(o, "tolist") else str(o)), encoding="utf-8")


def pct(x):
    x = np.asarray(x, float)
    if not len(x):
        return {}
    return {"n": int(len(x)), "mean": round(float(x.mean()), 2), "p50": round(float(np.median(x)), 2), "p95": round(float(np.percentile(x, 95)), 2),
            "min": round(float(x.min()), 2), "max": round(float(x.max()), 2)}
