"""kanana-2-v-decide-3b-2611 및 T1-v4-ALL YOLO26x(PpeDetector) 추론 실행 스크립트 (S15P31A101-538).

입력 묶음(`manifest.json` + 크롭 이미지)을 읽어:
1. `model.system_one(state=..., questions=..., images=..., image_root=...)` 로 `decide-3b` 확률·판정·지연·메모리를 측정하고,
2. (B) PPE 표본에서는 운영 `simai_ai.ppe.detector.PpeDetector`(T1-v4-ALL YOLO26x)와 `simai_ai.ppe.state.PpeJudge.combine_bin`
   을 그대로 import 해 같은 사람 크롭을 추론하고, **[YOLO26x(기준) | decide-3b | 둘 결합]** 결과와 지연·GPU 메모리를 나란히 기록한다.

- GPU Pod 실행: `--model <decide 경로> --ppe-weights <T1-v4-ALL pt 경로> --device cuda --dtype bfloat16`
- 노트북 CPU 검증: `--fake` (`FakeKananaDecideModel` + `FakePpeDetector` 사용)
- LIVE 관제 보호(AGENTS.md): `--tier0-marker` 경로에 LIVE 표시 파일이 있고 `phase=running` 이면 실행을 거부한다.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import time
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any, Protocol

import cv2
import numpy as np

from simai_ai.ppe.association import (
    R_CONFLICTING_CLASSES,
    R_NO_DETECTION,
    R_NOT_WORN_MATCHED,
    R_WORN_MATCHED,
    S_NONE,
    S_NOT_WORN,
    S_UNKNOWN,
    S_WORN,
    Sample,
)
from simai_ai.ppe.classes import Candidate
from simai_ai.ppe.detector import PpeDetector
from simai_ai.ppe.state import B_NOT_WORN, B_WORN, PpeJudge

from .questions import (
    T1_V4_ALL_YOLO_SPEC,
    combine_yolo_and_decide_ppe,
    noul_to_wear_state,
    score_to_proposed_severity,
    validate_questions_bundle,
    validate_system_one_payload,
)

FORMAT = "simai.decide_benchmark_results"
FORMAT_VERSION = "0.2.0"
DEFAULT_MODEL_ID = "kakaocorp/kanana-2-v-decide-3b-2611"
DEFAULT_BUNDLE_DIR = Path(__file__).resolve().parent / "runs" / "input_bundle"
DEFAULT_OUT_PATH = Path(__file__).resolve().parent / "runs" / "results_fake.json"


class DecideModelProtocol(Protocol):
    def system_one(
        self,
        state: Any = None,
        questions: Mapping[str, Any] | None = None,
        images: Sequence[Any] | None = None,
        *,
        state_format: str | None = None,
        temperature: float = 1.0,
        query_schemas: Any = None,
        image_root: str | os.PathLike[str] = "",
    ) -> dict[str, Any]: ...


class PpeDetectorProtocol(Protocol):
    def infer(self, frame_bgr: Any) -> list[Candidate]: ...


def _softmax(logits: Sequence[float], temperature: float = 1.0) -> list[float]:
    t = max(float(temperature), 1e-5)
    scaled = [float(x) / t for x in logits]
    m = max(scaled)
    exps = [math.exp(x - m) for x in scaled]
    total = sum(exps)
    return [e / total for e in exps]


def _margin_confidence(probs: Sequence[float]) -> float:
    if len(probs) < 2:
        return 1.0
    ordered = sorted((float(p) for p in probs), reverse=True)
    return round(ordered[0] - ordered[1], 6)


class FakePpeDetector:
    """노트북 CPU 검증용 `PpeDetector.infer` 모사체 (운영 `Candidate` 객체를 그대로 반환)."""

    def __init__(self) -> None:
        self.checkpoint_sha256 = T1_V4_ALL_YOLO_SPEC["sha256"]
        self._call_hint: str = ""

    def set_sample_hint(self, sample_id: str) -> None:
        self._call_hint = sample_id

    def infer(self, frame_bgr: Any) -> list[Candidate]:
        if getattr(frame_bgr, "ndim", None) != 3 or frame_bgr.shape[2] != 3 or str(frame_bgr.dtype) != "uint8":
            raise ValueError("원본 HWC uint8 BGR 프레임이어야 한다")
        h, w = int(frame_bgr.shape[0]), int(frame_bgr.shape[1])
        sid = self._call_hint
        head_box = (w * 0.25, h * 0.05, w * 0.75, h * 0.32)
        torso_box = (w * 0.20, h * 0.30, w * 0.80, h * 0.78)

        out: list[Candidate] = []
        # helmet 후보
        if "re_putting" in sid or "crossing" in sid:
            # 전환·교차 가림 구간에서는 YOLO 가 검출을 못 내거나(UNKNOWN 유도) 저신뢰로 동작하는 케이스 모사
            pass
        elif "both_worn" in sid or "helmet_only" in sid or "near_front" in sid or "far_back" in sid or "crouch" in sid or "s1_" in sid or "s5_" in sid or "s6_i00005_personA" in sid:
            conf = 0.58 if "crouch" in sid else 0.88
            out.append(Candidate(0, "helmet_head", "helmet", "WORN", conf, head_box))
        else:
            out.append(Candidate(1, "no_helmet_head", "helmet", "NOT_WORN", 0.86, head_box))

        # vest 후보
        if "none" in sid or "helmet_only" in sid:
            out.append(Candidate(3, "no_vest_torso", "vest", "NOT_WORN", 0.85, torso_box))
        else:
            out.append(Candidate(2, "vest_torso", "vest", "WORN", 0.90, torso_box))
        return out


def _candidates_to_sample(
    candidates: Sequence[Candidate],
    *,
    item: str,
    world_id: str,
    camera_id: str,
    capture_time_ms: int,
) -> Sample:
    """한 카메라 인물 크롭에서 나온 `PpeDetector.infer` 후보(`Candidate`)를 운영 `Sample` 로 변환한다."""
    hits = [c for c in candidates if c.item == item]
    worn = [c for c in hits if c.candidate_state == S_WORN]
    not_worn = [c for c in hits if c.candidate_state == S_NOT_WORN]
    if worn and not_worn:
        return Sample(
            world_id=world_id,
            identity_revision=0,
            item=item,
            kind=S_UNKNOWN,
            reason=R_CONFLICTING_CLASSES,
            camera_id=camera_id,
            stream_epoch=0,
            frame_sequence=0,
            capture_time_ms=capture_time_ms,
            local_track_id="crop-0",
            confidence=None,
            region_quality=1.0,
        )
    if worn:
        best = max(worn, key=lambda c: c.confidence)
        return Sample(
            world_id=world_id,
            identity_revision=0,
            item=item,
            kind=S_WORN,
            reason=R_WORN_MATCHED,
            camera_id=camera_id,
            stream_epoch=0,
            frame_sequence=0,
            capture_time_ms=capture_time_ms,
            local_track_id="crop-0",
            confidence=round(float(best.confidence), 4),
            region_quality=1.0,
        )
    if not_worn:
        best = max(not_worn, key=lambda c: c.confidence)
        return Sample(
            world_id=world_id,
            identity_revision=0,
            item=item,
            kind=S_NOT_WORN,
            reason=R_NOT_WORN_MATCHED,
            camera_id=camera_id,
            stream_epoch=0,
            frame_sequence=0,
            capture_time_ms=capture_time_ms,
            local_track_id="crop-0",
            confidence=round(float(best.confidence), 4),
            region_quality=1.0,
        )
    return Sample(
        world_id=world_id,
        identity_revision=0,
        item=item,
        kind=S_NONE,
        reason=R_NO_DETECTION,
        camera_id=camera_id,
        stream_epoch=0,
        frame_sequence=0,
        capture_time_ms=capture_time_ms,
        local_track_id="crop-0",
        confidence=None,
        region_quality=1.0,
    )


def evaluate_person_crops_with_yolo(
    detector: PpeDetectorProtocol,
    crop_paths: Sequence[Path],
    *,
    sample_id: str,
    world_id: str = "w-1",
) -> dict[str, Any]:
    """같은 사람 크롭(1~4장)에 운영 `PpeDetector.infer` 와 `PpeJudge.combine_bin` 을 적용해 사람별 helmet/vest 상태를 산출한다."""
    if hasattr(detector, "set_sample_hint"):
        detector.set_sample_hint(sample_id)

    judge = PpeJudge()
    helmet_samples: list[Sample] = []
    vest_samples: list[Sample] = []
    raw_detections: dict[str, list[dict[str, Any]]] = {}

    t0 = time.perf_counter()
    for idx, p in enumerate(crop_paths):
        cid = p.stem or f"cam_{idx}"
        img = cv2.imread(str(p))
        if img is None:
            img = np.zeros((128, 128, 3), dtype=np.uint8)
        cands = detector.infer(img)
        raw_detections[cid] = [
            {
                "class_id": c.class_id,
                "class_name": c.class_name,
                "item": c.item,
                "candidate_state": c.candidate_state,
                "confidence": round(float(c.confidence), 4),
                "xyxy": [round(float(v), 1) for v in c.xyxy],
            }
            for c in cands
        ]
        helmet_samples.append(
            _candidates_to_sample(cands, item="helmet", world_id=world_id, camera_id=cid, capture_time_ms=1000)
        )
        vest_samples.append(
            _candidates_to_sample(cands, item="vest", world_id=world_id, camera_id=cid, capture_time_ms=1000)
        )
    latency_ms = round((time.perf_counter() - t0) * 1000.0, 2)

    h_bin, h_ev = judge.combine_bin(helmet_samples)
    v_bin, v_ev = judge.combine_bin(vest_samples)

    h_state = "WORN" if h_bin == B_WORN else ("NOT_WORN" if h_bin == B_NOT_WORN else "UNKNOWN")
    v_state = "WORN" if v_bin == B_WORN else ("NOT_WORN" if v_bin == B_NOT_WORN else "UNKNOWN")
    h_conf = max((s.confidence for s in h_ev if s.confidence is not None), default=None)
    v_conf = max((s.confidence for s in v_ev if s.confidence is not None), default=None)

    return {
        "model_version": T1_V4_ALL_YOLO_SPEC["model_version"],
        "latency_ms": latency_ms,
        "helmet_state": h_state,
        "helmet_confidence": round(float(h_conf), 4) if h_conf is not None else None,
        "vest_state": v_state,
        "vest_confidence": round(float(v_conf), 4) if v_conf is not None else None,
        "r9_finding_raised": bool(h_state == "NOT_WORN"),
        "per_camera_detections": raw_detections,
    }


class FakeKananaDecideModel:
    """노트북 CPU 드라이런 및 단위 시험용 결정론적 `kanana-2-v-decide-3b-2611` 모사 모델."""

    def __init__(self, model_name: str = f"{DEFAULT_MODEL_ID}-fake-cpu") -> None:
        self.model_name = model_name

    def system_one(
        self,
        state: Any = None,
        questions: Mapping[str, Any] | None = None,
        images: Sequence[Any] | None = None,
        *,
        state_format: str | None = None,
        temperature: float = 1.0,
        query_schemas: Any = None,
        image_root: str | os.PathLike[str] = "",
    ) -> dict[str, Any]:
        if questions is None:
            raise ValueError("questions 는 필수다.")
        validate_system_one_payload(state, questions, images=images, state_format=state_format)

        root_path = Path(image_root) if image_root else Path(".")
        img_sharpness: list[float] = []
        for img_item in images or []:
            p = Path(img_item) if not isinstance(img_item, Path) else img_item
            full_p = p if p.is_absolute() else root_path / p
            if full_p.is_file():
                arr = cv2.imread(str(full_p), cv2.IMREAD_GRAYSCALE)
                if arr is not None:
                    img_sharpness.append(float(cv2.Laplacian(arr, cv2.CV_64F).var()))
                    continue
            img_sharpness.append(100.0)

        state_dict: Mapping[str, Any] = state if isinstance(state, Mapping) else {"raw_text": str(state)}
        seed_bytes = json.dumps(state_dict, ensure_ascii=False, sort_keys=True).encode("utf-8")
        digest = hashlib.sha256(seed_bytes).digest()
        jitter = (digest[0] / 255.0 - 0.5) * 0.08

        answers: dict[str, dict[str, Any]] = {}
        for qid, q in questions.items():
            qtype = q["type"]
            criteria = q.get("criteria")

            if qtype == "choice":
                keys = list(criteria.keys())
                logits = self._choice_logits(qid, keys, state_dict, img_sharpness, digest)
                probs = _softmax(logits, temperature=temperature)
                prob_map = {k: round(float(p), 6) for k, p in zip(keys, probs)}
                best_idx = int(np.argmax(probs))
                answers[qid] = {
                    "type": "choice",
                    "choice": keys[best_idx],
                    "confidence": _margin_confidence(probs),
                    "probabilities": prob_map,
                }
            elif qtype == "noul":
                prob_true = self._noul_prob(qid, state_dict, jitter, temperature=temperature)
                answers[qid] = {
                    "type": "noul",
                    "noul": round(float(prob_true), 6),
                }
            elif qtype == "score":
                levels = list(criteria)
                logits = self._score_logits(qid, len(levels), state_dict, jitter)
                probs = _softmax(logits, temperature=temperature)
                expected = sum(idx * p for idx, p in enumerate(probs))
                answers[qid] = {
                    "type": "score",
                    "score": round(float(expected), 6),
                    "confidence": _margin_confidence(probs),
                    "legend": {str(idx): str(desc) for idx, desc in enumerate(levels)},
                    "probabilities": {str(idx): round(float(p), 6) for idx, p in enumerate(probs)},
                }

        input_tokens = 256 + 180 * len(images or []) + 24 * len(questions)
        return {
            "model": self.model_name,
            "answers": answers,
            "usage": {
                "billing_units": len(questions),
                "input_tokens": int(input_tokens),
                "output_tokens": 0,
            },
        }

    def _choice_logits(
        self,
        qid: str,
        keys: Sequence[str],
        state_dict: Mapping[str, Any],
        img_sharpness: Sequence[float],
        digest: bytes,
    ) -> list[float]:
        if qid == "risk_type":
            target_rule = str(state_dict.get("rule_id", "R1"))
            return [2.4 if k == target_rule else -0.4 + ((digest[idx % len(digest)] % 17) * 0.02) for idx, k in enumerate(keys)]
        if qid == "cleanest" and len(img_sharpness) == len(keys):
            vals = [math.log1p(max(1.0, s)) for s in img_sharpness]
            m = sum(vals) / max(len(vals), 1)
            return [(v - m) * 1.8 for v in vals]
        return [1.2 if idx == 0 else 0.0 for idx in range(len(keys))]

    def _noul_prob(
        self,
        qid: str,
        state_dict: Mapping[str, Any],
        jitter: float,
        *,
        temperature: float,
    ) -> float:
        if qid == "person_in_zone":
            rule_id = str(state_dict.get("rule_id", "R1"))
            metrics = state_dict.get("metrics") or {}
            dist = float(metrics.get("zone_distance_m", 0.0))
            in_zone = rule_id in ("R1", "R2", "R3", "R4", "R6", "R9") and dist <= 1.0
            base_logit = 1.9 if in_zone else -1.8
            return _softmax([0.0, base_logit + jitter], temperature=temperature)[1]

        scene_id = str(state_dict.get("scene_id", ""))
        sample_id = str(state_dict.get("sample_id", ""))
        if qid == "helmet":
            if "both_worn" in scene_id or "helmet_only" in sample_id or "near_front" in sample_id or "far_back" in sample_id or "crouch" in sample_id:
                logit = 2.1
            elif "re_putting" in sample_id:
                logit = 0.35
            elif "crossing" in sample_id or "in_hand" in sample_id:
                logit = -0.95
            else:
                logit = -2.1
            return _softmax([0.0, logit + jitter], temperature=temperature)[1]

        if qid == "vest":
            if "both_unworn" in scene_id or "helmet_only" in sample_id:
                logit = -2.0
            else:
                logit = 2.0
            return _softmax([0.0, logit + jitter], temperature=temperature)[1]

        return _softmax([0.0, 0.5 + jitter], temperature=temperature)[1]

    def _score_logits(
        self,
        qid: str,
        n_levels: int,
        state_dict: Mapping[str, Any],
        jitter: float,
    ) -> list[float]:
        if qid == "upgrade" and n_levels == 3:
            sev = str(state_dict.get("current_severity", "CAUTION"))
            rule_id = str(state_dict.get("rule_id", "R1"))
            if sev == "CRITICAL":
                return [1.3, 0.2, 0.9 + jitter]
            if rule_id in ("R2", "R5", "R9"):
                return [-0.3, 1.6 + jitter, 0.6]
            return [1.8, -0.4 + jitter, -1.2]
        return [1.0 if i == 0 else 0.0 for i in range(n_levels)]


def validate_system_one_response(
    response: Mapping[str, Any],
    questions: Mapping[str, Mapping[str, Any]],
) -> None:
    """`model.system_one(...)` 반환 객체가 `decision_kanana_v_decide.py` 응답 규약을 만족하는지 검증한다."""
    if not isinstance(response, Mapping):
        raise ValueError("system_one 응답은 사전이어야 한다.")
    for req_key in ("model", "answers", "usage"):
        if req_key not in response:
            raise ValueError(f"system_one 응답에 '{req_key}' 키가 없다.")

    answers = response["answers"]
    if not isinstance(answers, Mapping) or set(answers) != set(questions):
        raise ValueError(f"응답 answers 키({set(answers) if isinstance(answers, Mapping) else None})가 질문 키({set(questions)})와 일치하지 않는다.")

    for qid, q in questions.items():
        ans = answers[qid]
        qtype = q["type"]
        if not isinstance(ans, Mapping) or ans.get("type") != qtype:
            raise ValueError(f"질문 '{qid}' 응답 type 불일치: 기대={qtype!r}, 실제={ans.get('type') if isinstance(ans, Mapping) else ans!r}")

        if qtype == "choice":
            expected_keys = set(q["criteria"].keys())
            choice = ans.get("choice")
            probs = ans.get("probabilities")
            conf = ans.get("confidence")
            if choice not in expected_keys:
                raise ValueError(f"choice 질문 '{qid}' 의 선택값 {choice!r} 가 criteria 에 없다.")
            if not isinstance(probs, Mapping) or set(probs) != expected_keys:
                raise ValueError(f"choice 질문 '{qid}' 의 probabilities 키가 criteria 와 일치하지 않는다.")
            prob_sum = sum(float(v) for v in probs.values())
            if not (0.98 <= prob_sum <= 1.02):
                raise ValueError(f"choice 질문 '{qid}' 의 확률 합이 1.0 이 아니다: {prob_sum:.4f}")
            if not isinstance(conf, (int, float)) or not (0.0 <= float(conf) <= 1.0):
                raise ValueError(f"choice 질문 '{qid}' 의 confidence 값이 유효하지 않다: {conf!r}")

        elif qtype == "noul":
            prob = ans.get("noul")
            if not isinstance(prob, (int, float)) or not (0.0 <= float(prob) <= 1.0):
                raise ValueError(f"noul 질문 '{qid}' 의 확률값이 0~1 범위가 아니다: {prob!r}")

        elif qtype == "score":
            n_levels = len(q["criteria"])
            score_val = ans.get("score")
            probs = ans.get("probabilities")
            legend = ans.get("legend")
            expected_idx_keys = {str(i) for i in range(n_levels)}
            if not isinstance(score_val, (int, float)) or not (-1e-4 <= float(score_val) <= (n_levels - 1) + 1e-4):
                raise ValueError(f"score 질문 '{qid}' 의 점수가 0..{n_levels - 1} 범위를 벗어났다: {score_val!r}")
            if not isinstance(probs, Mapping) or set(probs) != expected_idx_keys:
                raise ValueError(f"score 질문 '{qid}' 의 probabilities 키가 0..{n_levels - 1} 와 다르다.")
            if not isinstance(legend, Mapping) or set(legend) != expected_idx_keys:
                raise ValueError(f"score 질문 '{qid}' 의 legend 키가 0..{n_levels - 1} 와 다르다.")
            prob_sum = sum(float(v) for v in probs.values())
            if not (0.98 <= prob_sum <= 1.02):
                raise ValueError(f"score 질문 '{qid}' 의 확률 합이 1.0 이 아니다: {prob_sum:.4f}")

    usage = response["usage"]
    if not isinstance(usage, Mapping) or int(usage.get("billing_units", -1)) != len(questions):
        raise ValueError("usage.billing_units 가 질문 개수와 일치하지 않는다.")


def check_tier0_not_live(marker_path: Path | None) -> None:
    """LIVE 관제 중에는 오프라인 벤치마크 실행을 차단한다 (AGENTS.md 작업 방식)."""
    if marker_path is None or not marker_path.is_file():
        return
    try:
        doc = json.loads(marker_path.read_text(encoding="utf-8"))
        phase = str(doc.get("phase", "")).lower()
    except Exception:
        phase = "running"
    if phase in ("running", "starting", "live"):
        raise RuntimeError(
            f"Tier 0 LIVE 표시 파일({marker_path})이 활성 상태(phase={phase})이므로 오프라인 벤치마크를 실행하지 않는다."
        )


def load_real_ppe_detector(
    weights_path: Path,
    *,
    sha256: str = T1_V4_ALL_YOLO_SPEC["sha256"],
    size_bytes: int | None = T1_V4_ALL_YOLO_SPEC["size_bytes"],
    device: str = "cuda",
) -> tuple[PpeDetector, dict[str, Any]]:
    """운영 `simai_ai.ppe.detector.PpeDetector`(T1-v4-ALL YOLO26x)를 그대로 적재하고 GPU 메모리를 기록한다."""
    import torch

    before_mb = (
        torch.cuda.memory_allocated() / (1024.0 * 1024.0)
        if device.startswith("cuda") and torch.cuda.is_available()
        else 0.0
    )
    t0 = time.perf_counter()
    yolo_dev: str | int = 0 if device.startswith("cuda") else "cpu"
    detector = PpeDetector(weights_path, sha256=sha256, size_bytes=size_bytes, device=yolo_dev)
    load_s = round(time.perf_counter() - t0, 3)
    after_mb = (
        torch.cuda.memory_allocated() / (1024.0 * 1024.0)
        if device.startswith("cuda") and torch.cuda.is_available()
        else 0.0
    )
    return detector, {
        "model_version": T1_V4_ALL_YOLO_SPEC["model_version"],
        "weights_path": str(weights_path),
        "sha256": detector.checkpoint_sha256,
        "load_seconds": load_s,
        "weight_allocated_mb": round(max(0.0, after_mb - before_mb), 2),
    }


def load_real_decide_model(
    model_id_or_path: str,
    *,
    device: str = "cuda",
    dtype_name: str = "bfloat16",
) -> tuple[DecideModelProtocol, dict[str, Any]]:
    """HuggingFace `AutoModel.from_pretrained(..., trust_remote_code=True)` 로 실제 모델을 적재한다."""
    import torch
    from transformers import AutoModel

    dtype_map = {
        "bfloat16": torch.bfloat16,
        "float16": torch.float16,
        "float32": torch.float32,
    }
    if dtype_name not in dtype_map:
        raise ValueError(f"지원하지 않는 dtype: {dtype_name!r}")

    before_mb = (
        torch.cuda.memory_allocated() / (1024.0 * 1024.0)
        if device.startswith("cuda") and torch.cuda.is_available()
        else 0.0
    )
    t0 = time.perf_counter()
    hf_token = os.environ.get("HF_TOKEN") or None
    model = AutoModel.from_pretrained(
        model_id_or_path,
        trust_remote_code=True,
        dtype=dtype_map[dtype_name],
        token=hf_token,
    )
    model = model.to(device)
    model.eval()
    load_s = round(time.perf_counter() - t0, 3)
    after_mb = (
        torch.cuda.memory_allocated() / (1024.0 * 1024.0)
        if device.startswith("cuda") and torch.cuda.is_available()
        else 0.0
    )

    gpu_info: dict[str, Any] = {
        "device": device,
        "dtype": dtype_name,
        "load_seconds": load_s,
        "cuda_available": bool(torch.cuda.is_available()),
        "device_name": torch.cuda.get_device_name(0) if device.startswith("cuda") and torch.cuda.is_available() else "CPU",
        "weight_allocated_mb": round(max(0.0, after_mb - before_mb), 2),
    }
    return model, gpu_info


def _interpret_sample_answers(
    sample: Mapping[str, Any],
    answers: Mapping[str, Any],
    *,
    yolo_ppe: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """문항별 원시 확률을 simai 도메인 판정값(`proposed_severity`, `WearState`, `[YOLO|decide|결합]`)으로 변환한다."""
    cat = sample["category"]
    if cat == "risk_event":
        cur_sev = str(sample["state"]["current_severity"])
        proposed, discard_reason = score_to_proposed_severity(cur_sev, answers["upgrade"])
        return {
            "predicted_rule_id": answers["risk_type"]["choice"],
            "predicted_rule_prob": float(answers["risk_type"]["probabilities"][answers["risk_type"]["choice"]]),
            "upgrade_score": float(answers["upgrade"]["score"]),
            "proposed_severity": proposed,
            "proposal_discard_reason": discard_reason,
            "person_in_zone_prob": float(answers["person_in_zone"]["noul"]),
            "person_in_zone_pred": bool(float(answers["person_in_zone"]["noul"]) >= 0.5),
        }
    if cat == "ppe":
        h_prob = float(answers["helmet"]["noul"])
        v_prob = float(answers["vest"]["noul"])
        h_state = noul_to_wear_state(h_prob)
        v_state = noul_to_wear_state(v_prob)

        yolo_h_state = str((yolo_ppe or {}).get("helmet_state", "UNKNOWN"))
        yolo_h_conf = (yolo_ppe or {}).get("helmet_confidence")
        yolo_v_state = str((yolo_ppe or {}).get("vest_state", "UNKNOWN"))
        yolo_v_conf = (yolo_ppe or {}).get("vest_confidence")

        comb_h_state, comb_h_reason = combine_yolo_and_decide_ppe(yolo_h_state, yolo_h_conf, h_prob)
        comb_v_state, comb_v_reason = combine_yolo_and_decide_ppe(yolo_v_state, yolo_v_conf, v_prob)

        return {
            "helmet_prob": h_prob,
            "vest_prob": v_prob,
            "helmet_pred": bool(h_prob >= 0.5),
            "vest_pred": bool(v_prob >= 0.5),
            "helmet_wear_state": h_state,
            "vest_wear_state": v_state,
            "r9_finding_pred": bool(h_state == "NOT_WORN"),
            "combined_helmet_state": comb_h_state,
            "combined_helmet_reason": comb_h_reason,
            "combined_vest_state": comb_v_state,
            "combined_vest_reason": comb_v_reason,
            "combined_r9_finding_pred": bool(comb_h_state == "NOT_WORN"),
        }
    if cat == "map_crop":
        choice = answers["cleanest"]["choice"]
        probs = answers["cleanest"]["probabilities"]
        return {
            "selected_camera": choice,
            "selected_prob": float(probs[choice]),
            "confidence": float(answers["cleanest"]["confidence"]),
            "ranking": sorted(probs.keys(), key=lambda k: float(probs[k]), reverse=True),
        }
    return {}


def _latency_stats(values_ms: Sequence[float]) -> dict[str, float]:
    if not values_ms:
        return {"count": 0, "mean_ms": 0.0, "p50_ms": 0.0, "p95_ms": 0.0, "max_ms": 0.0}
    arr = np.asarray(values_ms, dtype=float)
    return {
        "count": int(len(arr)),
        "mean_ms": round(float(np.mean(arr)), 2),
        "p50_ms": round(float(np.percentile(arr, 50)), 2),
        "p95_ms": round(float(np.percentile(arr, 95)), 2),
        "max_ms": round(float(np.max(arr)), 2),
    }


def run_benchmark(
    bundle_dir: Path,
    out_path: Path,
    *,
    model: DecideModelProtocol | None = None,
    ppe_detector: PpeDetectorProtocol | None = None,
    model_id_or_path: str = DEFAULT_MODEL_ID,
    ppe_weights_path: Path | None = None,
    ppe_sha256: str = T1_V4_ALL_YOLO_SPEC["sha256"],
    device: str = "cuda",
    dtype_name: str = "bfloat16",
    fake: bool = False,
    temperature: float = 1.0,
    tier0_marker: Path | None = None,
) -> dict[str, Any]:
    """입력 묶음 전체를 실행하고 결과 사전을 저장·반환한다."""
    check_tier0_not_live(tier0_marker)
    manifest_path = bundle_dir / "manifest.json"
    if not manifest_path.is_file():
        raise FileNotFoundError(f"입력 묶음 manifest 가 없다: {manifest_path}")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))

    env_ppe = os.environ.get("SIMAI_PPE_WEIGHTS")
    resolved_ppe_weights = ppe_weights_path or (Path(env_ppe) if env_ppe else None)

    if ppe_detector is not None:
        active_ppe_detector: PpeDetectorProtocol = ppe_detector
        yolo_gpu_meta: dict[str, Any] = {
            "model_version": T1_V4_ALL_YOLO_SPEC["model_version"],
            "is_fake": True,
            "weight_allocated_mb": 0.0,
            "peak_allocated_mb": 0.0,
        }
    elif fake or resolved_ppe_weights is None:
        active_ppe_detector = FakePpeDetector()
        yolo_gpu_meta = {
            "model_version": f"{T1_V4_ALL_YOLO_SPEC['model_version']}-fake",
            "is_fake": True,
            "weight_allocated_mb": 0.0,
            "peak_allocated_mb": 0.0,
        }
    else:
        active_ppe_detector, yolo_gpu_meta = load_real_ppe_detector(
            resolved_ppe_weights,
            sha256=ppe_sha256,
            device=device,
        )
        yolo_gpu_meta["is_fake"] = False

    if model is not None:
        active_model = model
        gpu_meta: dict[str, Any] = {
            "device": "custom",
            "dtype": "custom",
            "load_seconds": 0.0,
            "cuda_available": False,
            "device_name": "injected-model",
            "weight_allocated_mb": 0.0,
        }
    elif fake:
        active_model = FakeKananaDecideModel()
        gpu_meta = {
            "device": "cpu",
            "dtype": "float32",
            "load_seconds": 0.0,
            "cuda_available": False,
            "device_name": "CPU (FakeKananaDecideModel)",
            "weight_allocated_mb": 0.0,
        }
    else:
        active_model, gpu_meta = load_real_decide_model(
            model_id_or_path,
            device=device,
            dtype_name=dtype_name,
        )

    sample_results: list[dict[str, Any]] = []
    latencies_all: list[float] = []
    latencies_by_cat: dict[str, list[float]] = {"risk_event": [], "ppe": [], "map_crop": []}
    yolo_ppe_latencies: list[float] = []

    for entry in manifest["samples"]:
        sid = entry["sample_id"]
        cat = entry["category"]
        state = entry["state"]
        questions = entry["questions"]
        images = entry["images"]
        state_format = entry.get("state_format", "json")

        validate_questions_bundle(questions)
        validate_system_one_payload(state, questions, images=images, state_format=state_format)

        yolo_ppe_res: dict[str, Any] | None = None
        if cat == "ppe":
            crop_paths = [bundle_dir / rel for rel in images]
            yolo_ppe_res = evaluate_person_crops_with_yolo(
                active_ppe_detector,
                crop_paths,
                sample_id=sid,
                world_id=str(state.get("world_id", "w-1")),
            )
            yolo_ppe_latencies.append(float(yolo_ppe_res["latency_ms"]))

        t0 = time.perf_counter()
        resp = active_model.system_one(
            state=state,
            questions=questions,
            images=images,
            state_format=state_format,
            temperature=temperature,
            image_root=str(bundle_dir),
        )
        elapsed_ms = round((time.perf_counter() - t0) * 1000.0, 2)
        validate_system_one_response(resp, questions)

        interpreted = _interpret_sample_answers(entry, resp["answers"], yolo_ppe=yolo_ppe_res)
        latencies_all.append(elapsed_ms)
        latencies_by_cat.setdefault(cat, []).append(elapsed_ms)

        sample_results.append(
            {
                "sample_id": sid,
                "category": cat,
                "source_session": entry.get("source_session", ""),
                "split_status": entry.get("split_status"),
                "is_upper_bound": entry.get("is_upper_bound", False),
                "state": state,
                "latency_ms": elapsed_ms,
                "model": resp["model"],
                "usage": resp["usage"],
                "answers": resp["answers"],
                "interpreted": interpreted,
                "yolo26x_ppe": yolo_ppe_res,
                "ground_truth": entry.get("ground_truth", {}),
                "baseline_vlm": entry.get("baseline_vlm"),
                "baseline_ppe_r9": entry.get("baseline_ppe_r9"),
                "per_camera_quality": entry.get("per_camera_quality"),
            }
        )

    peak_alloc_mb = float(gpu_meta.get("weight_allocated_mb", 0.0))
    peak_reserved_mb = 0.0
    if not fake and model is None and device.startswith("cuda"):
        try:
            import torch

            if torch.cuda.is_available():
                peak_alloc_mb = round(torch.cuda.max_memory_allocated() / (1024.0 * 1024.0), 2)
                peak_reserved_mb = round(torch.cuda.max_memory_reserved() / (1024.0 * 1024.0), 2)
        except Exception:
            pass

    gpu_meta["peak_allocated_mb"] = peak_alloc_mb
    gpu_meta["peak_reserved_mb"] = peak_reserved_mb
    yolo_gpu_meta["latency_ppe"] = _latency_stats(yolo_ppe_latencies)

    output_doc: dict[str, Any] = {
        "format": FORMAT,
        "format_version": FORMAT_VERSION,
        "jira_key": "S15P31A101-538",
        "target_model": model_id_or_path,
        "is_fake_run": bool(fake or model is not None),
        "temperature": float(temperature),
        "gpu": gpu_meta,
        "yolo26x_ppe_summary": yolo_gpu_meta,
        "latency_summary": {
            "overall": _latency_stats(latencies_all),
            "by_category": {k: _latency_stats(v) for k, v in latencies_by_cat.items()},
            "yolo26x_ppe": _latency_stats(yolo_ppe_latencies),
        },
        "sample_count": len(sample_results),
        "samples": sample_results,
    }

    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(output_doc, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return output_doc


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="kanana-2-v-decide-3b-2611 + T1-v4-ALL YOLO26x 벤치마크 추론 실행")
    parser.add_argument("--bundle", type=Path, default=DEFAULT_BUNDLE_DIR, help="입력 묶음 폴더 경로(manifest.json 포함)")
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT_PATH, help="결과 JSON 저장 경로")
    parser.add_argument("--model", type=str, default=DEFAULT_MODEL_ID, help="HuggingFace 모델 ID 또는 로컬 가중치 폴더")
    parser.add_argument(
        "--ppe-weights",
        type=Path,
        default=None,
        help="T1-v4-ALL YOLO26x 체크포인트 경로 (미지정 시 SIMAI_PPE_WEIGHTS 환경변수 참조)",
    )
    parser.add_argument(
        "--ppe-sha256",
        type=str,
        default=T1_V4_ALL_YOLO_SPEC["sha256"],
        help="T1-v4-ALL YOLO26x 체크포인트 sha256",
    )
    parser.add_argument("--device", type=str, default="cuda", help="실행 디바이스 (cuda 또는 cpu)")
    parser.add_argument("--dtype", type=str, default="bfloat16", choices=("bfloat16", "float16", "float32"))
    parser.add_argument("--temperature", type=float, default=1.0, help="system_one 온도 계수(기본 1.0)")
    parser.add_argument("--fake", action="store_true", help="노트북 CPU 검증용 FakeKananaDecideModel + FakePpeDetector 사용")
    parser.add_argument(
        "--tier0-marker",
        type=Path,
        default=None,
        help="Tier 0 LIVE 표시 파일 경로(활성 시 실행 거부)",
    )
    args = parser.parse_args(argv)

    doc = run_benchmark(
        args.bundle,
        args.out,
        model_id_or_path=args.model,
        ppe_weights_path=args.ppe_weights,
        ppe_sha256=args.ppe_sha256,
        device=args.device,
        dtype_name=args.dtype,
        fake=args.fake,
        temperature=args.temperature,
        tier0_marker=args.tier0_marker,
    )
    print(
        json.dumps(
            {
                "out": str(args.out),
                "is_fake_run": doc["is_fake_run"],
                "sample_count": doc["sample_count"],
                "latency_decide_overall": doc["latency_summary"]["overall"],
                "latency_yolo26x_ppe": doc["latency_summary"]["yolo26x_ppe"],
                "gpu_decide": doc["gpu"],
                "gpu_yolo26x": doc["yolo26x_ppe_summary"],
            },
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
