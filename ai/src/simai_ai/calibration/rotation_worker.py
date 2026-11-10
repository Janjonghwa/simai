"""회전 복구 전용 프로세스: 캐시·합성·특징·기하 계산과 작업자 CPU 예산."""

from __future__ import annotations

import json
import math
import os
import pickle
import sys
import time
from contextlib import contextmanager
from dataclasses import replace

import cv2
import numpy as np

from simai_ai.calibration.rotation_recovery import extract_features, make_candidate


def resize_samples(samples, side):
    result = []
    for frame, mask in samples:
        h, w = mask.shape
        scale = min(1., side / max(h, w))
        size = round(w * scale), round(h * scale)
        if size == (w, h):
            result.append((frame, mask))
        else:
            result.append((cv2.resize(frame, size, interpolation=cv2.INTER_LINEAR),
                           cv2.resize(mask.astype(np.float32), size, interpolation=cv2.INTER_AREA) >= .999999))
    return result


def reference_features(image, mask, camera, config):
    if tuple(image.shape[1::-1]) != tuple(camera["image_size"]):
        raise ValueError("STREAM_CHANGED: 배경과 내부 파라미터의 해상도가 다름")
    # 특징이 충분한 장면은 320px에서 시작한다. 희박하면 기존 상한(960px)으로 복귀한다.
    # 위치·잔차는 항상 원본 픽셀 단위이며 기존 모든 기하 게이트를 통과해야 한다.
    base = resize_samples([(image, mask != 0)], config.max_image_side)[0]
    coarse = replace(config, max_image_side=min(320, config.max_image_side))
    features = extract_features(base[0], base[1].astype(np.uint8) * 255, camera, coarse,
                                original_size=camera["image_size"])
    if len(features.points) >= max(80, config.min_matches * 2):
        return features, coarse
    return extract_features(image, mask, camera, config), config


def synthesize_samples(samples):
    """축소된 최근 표본의 관측 중앙값. 과반 관측·채널별 분산 게이트를 유지한다."""
    frames = [s[0] for s in samples]
    masks = np.stack([s[1] for s in samples], axis=-1)
    # 작은 표본 축을 연속 배치해 픽셀마다 관측된 중앙값만 선택한다.
    gray = [cv2.cvtColor(f, cv2.COLOR_BGR2GRAY) if f.ndim == 3 else f for f in frames]
    pixels = np.stack(gray, axis=-1)
    count = masks.sum(axis=-1)
    minimum = max(2, math.ceil(len(samples) * .5))
    if masks.all():
        lo, hi = (len(samples) - 1) // 2, len(samples) // 2
        ordered = np.partition(pixels, (lo, hi), axis=-1)
        a, b = ordered[..., lo], ordered[..., hi]
    else:
        ordered = np.where(masks, pixels.astype(np.uint16), 256)
        ordered.sort(axis=-1)
        lo = np.maximum(count.astype(np.intp) - 1, 0) // 2
        hi = count.astype(np.intp) // 2
        a = np.take_along_axis(ordered, lo[..., None], axis=-1)[..., 0]
        b = np.take_along_axis(ordered, hi[..., None], axis=-1)[..., 0]
    bg = np.rint((a.astype(np.float32) + b) * .5).clip(0, 255).astype(np.uint8)
    # 채널별 분산을 보존한다. 회색조만 검사하면 색만 변하는 물체가 통과한다.
    shape = frames[0].shape
    total = np.zeros(shape, np.float32)
    squares = np.zeros(shape, np.float32)
    for frame, mask in zip(frames, np.moveaxis(masks, -1, 0)):
        valid_mask = np.ascontiguousarray(mask, dtype=np.uint8)
        cv2.accumulate(frame, total, mask=valid_mask)
        cv2.accumulateSquare(frame, squares, mask=valid_mask)
    denom = np.maximum(count, 1).astype(np.float32)
    if total.ndim == 3:
        denom = denom[..., None]
    variance = np.maximum(squares / denom - (total / denom) ** 2, 0)
    if variance.ndim == 3:
        variance = variance.max(axis=2)
    # 전역 무채색 조명 밝기(노출/조도) 변화만 있는 창은 스칼라 밝기 오프셋을 보정해 오탐을 막는다.
    # 색 변화(채널별 편차)나 국소 움직임(공간 잔차 표준편차)은 기존 분산 게이트대로 기각한다.
    common = masks.all(axis=-1)
    if (variance > 225).any() and int(common.sum()) >= max(64, int(common.size * 0.25)):
        g0 = pixels[..., 0].astype(np.float32)
        f0 = frames[0].astype(np.float32)
        shifts: list[float] = []
        achromatic_global = True
        for k, frame in enumerate(frames):
            gk = pixels[..., k].astype(np.float32)
            diff_g = gk[common] - g0[common]
            shift_k = float(np.median(diff_g))
            if float(np.std(diff_g - shift_k)) > 15.0:
                achromatic_global = False
                break
            if frame.ndim == 3:
                fk = frame.astype(np.float32)
                for ch in range(3):
                    diff_ch = fk[..., ch][common] - f0[..., ch][common]
                    if abs(float(np.median(diff_ch)) - shift_k) > 15.0 or float(np.std(diff_ch - shift_k)) > 15.0:
                        achromatic_global = False
                        break
                if not achromatic_global:
                    break
            shifts.append(shift_k)
        if achromatic_global and max(shifts, default=0.0) - min(shifts, default=0.0) > 5.0:
            anchor_shift = float(np.median(shifts))
            norm_total = np.zeros(shape, np.float32)
            norm_squares = np.zeros(shape, np.float32)
            for frame, mask, shift_k in zip(frames, np.moveaxis(masks, -1, 0), shifts):
                valid_mask = np.ascontiguousarray(mask, dtype=np.uint8)
                aligned = np.clip(frame.astype(np.float32) - (shift_k - anchor_shift), 0.0, 255.0)
                cv2.accumulate(aligned, norm_total, mask=valid_mask)
                cv2.accumulateSquare(aligned, norm_squares, mask=valid_mask)
            norm_var = np.maximum(norm_squares / denom - (norm_total / denom) ** 2, 0)
            if norm_var.ndim == 3:
                norm_var = norm_var.max(axis=2)
            variance = np.minimum(variance, norm_var)
    valid = ((count >= minimum) & (variance <= 225)).astype(np.uint8) * 255
    bg[valid == 0] = 0
    return bg, valid


class RotationComputation:
    """1개 작업자에서만 소유한다. 이월해도 완료한 기준·최근 특징은 보관한다."""

    def __init__(self, cpu_clock=time.process_time):
        self.clock = cpu_clock
        self.references = {}
        self.recent = {}
        self.synthesized = {}
        self.scaled = {}

    def _samples(self, cid, recent, side):
        cached = self.scaled.get(cid, {})
        retained = {}
        result = []
        times = recent.get("sample_times", range(len(recent["samples"])))
        for at, sample in zip(times, recent["samples"]):
            key = (recent["signature"][0], at, recent["size"], side)
            small = cached.get(key)
            if small is None:
                small = resize_samples([sample], side)[0]
            retained[key] = small
            result.append(small)
        self.scaled[cid] = retained
        return result

    def compute(self, request, *, started=None):
        started = self.clock() if started is None else started
        timings = dict.fromkeys(("reference_read", "reference_extract", "recent_prepare",
                                 "recent_extract", "matching", "estimation"), 0.0)

        @contextmanager
        def measure(stage):
            start = self.clock()
            try:
                yield
            finally:
                timings[stage] += (self.clock() - start) * 1000

        def check_budget():
            if (self.clock() - started) * 1000 > request["budget_ms"]:
                raise TimeoutError(f"CPU 시간 예산 초과 ({request['budget_ms']}ms)")

        result = {}
        try:
            config = request["config"]
            ignored = set(request.get("ignored_cameras") or ())
            refs, features = {}, {}
            # 문서·렌즈·해상도·설정도 최근 특징의 캐시 키에 넣는다.
            for camera in request["document"]["cameras"]:
                cid = camera["camera_id"]
                if cid in ignored:
                    continue
                entry = request["references"][cid]
                key = (entry["key"], config)
                cached = self.references.get(cid)
                if cached is None or cached[0] != key:
                    check_budget()
                    with measure("reference_read"):
                        image = cv2.imread(entry["image"])
                        mask = cv2.imread(entry["mask"], cv2.IMREAD_GRAYSCALE)
                    if image is None or mask is None:
                        result = {"status": "NOT_READY"}
                        return result
                    with measure("reference_extract"):
                        try:
                            feat, feature_config = reference_features(image, mask, entry["camera"], config)
                        except ValueError as exc:
                            if str(exc).startswith("STREAM_CHANGED"):
                                raise ValueError(f"STREAM_CHANGED:{cid}:{exc}") from exc
                            raise
                    self.references[cid] = key, feat, feature_config
                refs[cid] = entry["camera"], self.references[cid][1]
            for camera in request["document"]["cameras"]:
                cid = camera["camera_id"]
                if cid in ignored:
                    continue
                recent = request["recent"][cid]
                feature_config = self.references[cid][2]
                key = (recent["signature"], json.dumps(camera, sort_keys=True), feature_config)
                cached = self.recent.get(cid)
                if cached is None or cached[0] != key:
                    check_budget()
                    synthesized = self.synthesized.get(cid)
                    if synthesized is None or synthesized[0] != key:
                        with measure("recent_prepare"):
                            samples = self._samples(cid, recent, feature_config.max_image_side)
                            bg, mask = synthesize_samples(samples)
                        self.synthesized[cid] = key, bg, mask
                    _, bg, mask = self.synthesized[cid]
                    check_budget()
                    with measure("recent_extract"):
                        try:
                            feat = extract_features(
                                bg,
                                mask,
                                camera,
                                feature_config,
                                original_size=recent["size"],
                                person_cover_ratio=float(recent.get("mean_person_cover_ratio") or 0.0),
                                never_revealed_ratio=float(recent.get("never_revealed_ratio") or 0.0),
                            )
                        except ValueError as exc:
                            if str(exc).startswith("STREAM_CHANGED"):
                                raise ValueError(f"STREAM_CHANGED:{cid}:{exc}") from exc
                            raise
                    self.recent[cid] = key, feat
                    self.synthesized.pop(cid, None)
                features[cid] = self.recent[cid][1]
            check_budget()
            candidate, report = make_candidate(
                request["document"], refs, features, request["recent_ms"],
                at_ms=request["now_ms"], version=request["version"], config=config,
                check_budget=check_budget, measure=measure, ignored_cameras=ignored,
            )
            check_budget()
            result = {"status": "COMPLETED", "candidate": candidate, "report": report}
        except TimeoutError as exc:
            result = {"status": "BUDGET_DEFERRED", "reason": str(exc)}
        except Exception as exc:  # noqa: BLE001 -- 프로세스 경계에서 오류를 부모에게 전달한다.
            result = {"status": "ERROR", "reason": str(exc)}
        finally:
            timings["total"] = (self.clock() - started) * 1000
            result["timings"] = timings
            result["feature_sides"] = {cid: entry[2].max_image_side for cid, entry in self.references.items()}
        return result


def worker_main():
    """전용 프로세스 진입점. 입력 역직렬화도 작업자 CPU 예산에 포함한다."""
    cv2.setNumThreads(1)
    if hasattr(os, "nice"):
        try:
            os.nice(10)
        except OSError:
            pass
    computation = RotationComputation()
    try:
        while True:
            started = time.process_time()
            request = pickle.load(sys.stdin.buffer)
            if request is None:
                return
            result = computation.compute(request, started=started)
            result.update(worker_pid=os.getpid(), opencv_threads=cv2.getNumThreads())
            pickle.dump(result, sys.stdout.buffer, protocol=pickle.HIGHEST_PROTOCOL)
            sys.stdout.buffer.flush()
    except (EOFError, BrokenPipeError):
        pass


if __name__ == "__main__":
    worker_main()
