"""세팅 「카메라 확인」: 관제(LIVE) 검사와 같은 계산으로 새 보정 후보를 사람 걷기로 확인한다(S15P31A101-646).

14차 세션에서 세팅 「보정 검증 결과」는 카메라별 높이만 보고 통과였는데, 관제 중 B–C 쌍 에피폴라 실패와 -540 틀어짐 감시 오탐이
났다. 관제에서 뜨는 카메라 경고는 세팅에서 같은 검사로 먼저 통과해야 한다(사용자 결정). 그래서 이 모듈은 관제와 **같은 입력과
같은 함수**를 쓴다.

- 입력: Tier 0 가 관제 경로에서 만든 공통 시각 view(`LocalTrackletBuffer.views_at`, YOLO 관절·로컬 트랙·시각 보간 그대로)를
  `views_to_json` 으로 받아 `views_from_json` 으로 되살린 것. 보정과 무관한 2D 원본 픽셀이라 새 보정 후보로 다시 계산할 수 있다.
- 쌍 검사: `tracking.cross_view.CrossViewAssociator` 를 새 보정 후보로 돌린 쌍 근거(`PairEvidence`)의 통과율·재투영 잔차.
  관제의 추적 계측(`tracking.diagnostics`)이 세는 것과 같다. 다른 사람끼리 짝지은 근거가 섞이지 않게, 그 시각 두 카메라에
  view 가 하나씩일 때(한 사람)만 센다. 공통 관절이 모자란 근거(FEW_COMMON_JOINTS·NO_FOV_OVERLAP)는 기하 근거가 아니라 뺀다.
- 카메라 검사: `calibration.monitor.leave_one_out_samples`(관제 틀어짐 감시의 한 카메라 제외 재투영)를 같은 설정
  (`MonitorConfig`)으로 잰 카메라별 중앙값. 통과한 값은 관제 감시의 검증된 기준선(`BaselineRecord`, 출처 SETUP_CAMERA_CHECK)이 된다.
- 4대 관측: 카메라마다 사람이 보인 시각 수.

판정(초기값, 근거는 docs/design/S15P31A101-646-setup-camera-check.md):
- 쌍: 근거가 `min_pair_samples` 이상이면 통과율 ≥ `min_pair_pass_rate` 이고 잔차 중앙값 ≤ `max_pair_residual_median_px` 일 때 PASS,
  아니면 FAIL. 근거가 모자라면 INSUFFICIENT. 새 보정 기하로 시야가 겹치지 않는 쌍(관제 연결도 건너뛰는 쌍)은 NOT_OVERLAPPING.
- 카메라: 사람이 `min_camera_views` 시각 넘게 보이지 않으면 NOT_OBSERVED. 한 카메라 제외 표본이 `min_loo_samples` 이상이면
  중앙값 ≤ `max_loo_median_px` 일 때 PASS, 아니면 FAIL. 표본이 모자라면 INSUFFICIENT(기준선 없음). 이 상한 + 관제 감시
  상대 여유 ≤ 관제 감시 절대 상한이다(S15P31A101-694, docs/design/S15P31A101-694-camera-check-criteria.md).
- 끝: 사람이 보인 시간이 `min_duration_ms` 이상이고, 필수 쌍(겹치는 쌍과 세팅 BA 의 '확인 필요' 쌍)이 모두 근거를 채우고, 모든
  카메라가 관측되면 판정한다. 필수 쌍이 끝내 근거를 못 채우면 `max_duration_ms` 뒤에 판정하되 이때 '확인 필요' 쌍과 관측되지 않은
  카메라는 실패다(모르면 통과로 두지 않는다). 겹치는 쌍 중 근거를 못 채운 쌍은 INSUFFICIENT 로 남기고 막지 않는다(관제도 같은
  사람을 그 쌍으로 묶을 일이 드물다). 단 판정한 쌍이 `min_evaluated_pairs` 개 미만이거나 통과 쌍이 하나도 없는 카메라가 있으면 실패다.

시각은 UTC int64 ms 이고 view 시각(t)만 쓴다(현재 시각을 읽지 않는다). Tier 0 위치·판정을 바꾸지 않는 관찰자다.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import numpy as np

from simai_ai.calibration.monitor import (
    BASELINE_SOURCE_SETUP_CAMERA_CHECK,
    BaselineRecord,
    MonitorConfig,
    leave_one_out_samples,
)
from simai_ai.geometry.interpolation import InterpolatedView
from simai_ai.geometry.rig import CameraRig, RigCamera
from simai_ai.providers.base import CalibrationProvider
from simai_ai.providers.contract import is_finite_number
from simai_ai.tracking.cross_view import CrossViewAssociator, LocalView, RejectReason

# 계약 site_setup#/$defs/camera_check 의 상태. AWAITING_START 는 워커 처리기가 시도 전에 싣는다(평가기는 시도 하나만 본다)
AWAITING_START = "AWAITING_START"
WAITING = "WAITING"
MEASURING = "MEASURING"
PASSED = "PASSED"
FAILED = "FAILED"
INCOMPLETE = "INCOMPLETE"
FINAL_STATUSES = frozenset({PASSED, FAILED, INCOMPLETE})

# 쌍·카메라 판정
PASS = "PASS"
FAIL = "FAIL"
INSUFFICIENT = "INSUFFICIENT"
NOT_OVERLAPPING = "NOT_OVERLAPPING"
NOT_OBSERVED = "NOT_OBSERVED"

# 기하 근거가 아닌 탈락(공통 관절 부족·시야 밖)은 통과율에 넣지 않는다
_NON_GEOMETRIC = frozenset({RejectReason.FEW_COMMON_JOINTS, RejectReason.NO_FOV_OVERLAP})

# 표본 부족(사람이 덜 걸었다) 사유. 기하 실패(PASS_RATE_LOW·RESIDUAL_HIGH·REPROJECTION_HIGH·NO_PASSING_PAIR)와 화면 안내가 다르다
# (S15P31A101-687: "카메라 4에서 사람이 충분히 보이지 않았습니다 / 표시된 구간을 지나며 30초만 다시 걸어 주세요")
SAMPLE_SHORTAGE_REASONS = frozenset({"FEW_SAMPLES", "NO_PERSON", "NOT_FINISHED", "NOT_OBSERVED", "NOT_CONFIRMED", "FEW_PAIRS"})
PERSON_VISIBLE_WINDOW_MS = 1_500
"""걷기 창의 카메라별 「사람 보임」: 마지막 view 시각 기준 이 시간 안에 그 카메라에 사람이 있었다(Tier 0 가 최대 5 Hz 로 넘기므로
약 7표본, 걸음 중 한두 프레임 검출이 빠져도 깜빡이지 않는다). 표시용이다"""


@dataclass(frozen=True)
class CameraCheckConfig:
    """카메라 확인 기준. 근거는 docs/design/S15P31A101-646-setup-camera-check.md, LOO 상한은 S15P31A101-694 문서."""

    min_duration_ms: int = 30_000
    """사람이 보인 시간(첫 관측 ~ 마지막 관측) 하한. 안내는 30~60초 걷기"""
    max_duration_ms: int = 120_000
    """필수 쌍 근거를 다 못 채워도 이 시간 뒤에는 판정한다(사람이 보인 시간 기준)"""
    min_pair_samples: int = 30
    """쌍 판정에 필요한 한 사람 근거 수(Tier 0 가 5 Hz 로 넘기면 두 카메라 동시 관측 약 6초)"""
    min_pair_pass_rate: float = 0.75
    max_pair_residual_median_px: float = 8.0
    """관제 연결의 경계 잔차(CrossViewConfig.residual_marginal_px). 넘으면 관제가 합치지 않고 IDENTITY_UNCERTAIN 으로 둔다"""
    min_camera_views: int = 30
    min_loo_samples: int = 20
    """관제 감시 판정 창 표본 하한(MonitorConfig.min_window_samples)과 같다"""
    max_loo_median_px: float = 12.0
    """S15P31A101-694: 15차 로비 실측에서 사람 관절로 직접 맞춘 보정(잡음 바닥)도 LOO 중앙값이 카메라별 5.9~8.7px 였고, 그
    보정에서 카메라 하나를 0.5° 돌리면 9.0~13.7px, 1° 면 15~22px 다. 관제 감시 절대 상한(MonitorConfig.absolute_max_median_px
    15px)은 이 값 + increase_margin_px 이상이라, 통과한 기준선의 관제 문턱이 기준선 + 여유 아래로 눌리지 않는다"""
    min_evaluated_pairs: int = 3
    """판정까지 간(PASS·FAIL) 쌍 수 하한. 카메라 4대 고리면 인접 4쌍 중 3쌍"""

    def __post_init__(self) -> None:
        for name in ("min_duration_ms", "max_duration_ms", "min_pair_samples", "min_camera_views", "min_loo_samples",
                     "min_evaluated_pairs"):
            value = getattr(self, name)
            if not isinstance(value, int) or isinstance(value, bool) or value < 1:
                raise ValueError(f"CameraCheckConfig.{name} 는 1 이상의 정수여야 한다: {value!r}")
        if self.max_duration_ms < self.min_duration_ms:
            raise ValueError("max_duration_ms 는 min_duration_ms 이상이어야 한다")
        if not 0 < self.min_pair_pass_rate <= 1:
            raise ValueError("min_pair_pass_rate 는 (0, 1] 이다")
        for name in ("max_pair_residual_median_px", "max_loo_median_px"):
            if not is_finite_number(getattr(self, name)) or getattr(self, name) <= 0:
                raise ValueError(f"CameraCheckConfig.{name} 는 양수다")

    def thresholds(self) -> dict[str, Any]:
        """계약 camera_check.thresholds."""
        return {
            "min_duration_s": self.min_duration_ms / 1000,
            "max_duration_s": self.max_duration_ms / 1000,
            "min_pair_samples": self.min_pair_samples,
            "min_pair_pass_rate": self.min_pair_pass_rate,
            "max_pair_residual_median_px": self.max_pair_residual_median_px,
            "min_camera_views": self.min_camera_views,
            "min_loo_samples": self.min_loo_samples,
            "max_loo_median_px": self.max_loo_median_px,
            "min_evaluated_pairs": self.min_evaluated_pairs,
        }


DEFAULT_CONFIG = CameraCheckConfig()
_DEFAULT_MONITOR = MonitorConfig()


class _DocProvider(CalibrationProvider):
    def __init__(self, doc: Mapping[str, Any]) -> None:
        self._doc = json.loads(json.dumps(doc))

    def current(self) -> dict[str, Any]:
        return json.loads(json.dumps(self._doc))


def pair_key(a: str, b: str) -> str:
    """쌍 키(정렬한 카메라 ID 를 '-' 로 잇는다). 카메라 ID 에 '-' 가 있을 수 있어 항목에 camera_ids 를 따로 싣는다."""
    first, second = sorted((a, b))
    return f"{first}-{second}"


@dataclass
class _PairStats:
    camera_ids: tuple[str, str]
    overlapping: bool
    samples: int = 0
    passed: int = 0
    marginal: int = 0
    residuals: list[float] = field(default_factory=list)
    rejects: dict[str, int] = field(default_factory=dict)


class CameraCheckEvaluator:
    """새 보정 후보 하나의 카메라 확인. observe 를 view 시각 순서로 부른다(같거나 이른 시각은 버린다)."""

    def __init__(
        self,
        calibration: Mapping[str, Any],
        config: CameraCheckConfig = DEFAULT_CONFIG,
        *,
        required_pairs: Iterable[tuple[str, str]] = (),
        monitor_config: MonitorConfig = _DEFAULT_MONITOR,
        joint_layout: str = "COCO17",
    ) -> None:
        self.config = config
        self.monitor_config = monitor_config
        self.calibration_version = str(calibration["calibration_version"])
        self.rig = CameraRig.from_provider(_DocProvider(calibration))
        self._cameras: dict[str, RigCamera] = dict(self.rig.cameras)
        self.associator = CrossViewAssociator(self.rig, joint_layout=joint_layout)
        ids = sorted(self._cameras)
        skipped = set(self.associator.skipped_camera_pairs)
        self._pairs: dict[str, _PairStats] = {}
        for i, a in enumerate(ids):
            for b in ids[i + 1:]:
                self._pairs[pair_key(a, b)] = _PairStats((a, b), (a, b) not in skipped)
        self._required_extra = {pair_key(a, b) for a, b in required_pairs if a in self._cameras and b in self._cameras}
        self._views: dict[str, int] = {cid: 0 for cid in ids}
        self._seen_t: dict[str, int] = {}
        self._loo: dict[str, list[float]] = {cid: [] for cid in ids}
        self._loo_ambiguous: dict[str, int] = {cid: 0 for cid in ids}
        self._first_t: int | None = None
        self._last_t: int | None = None
        self._last_seen_t: int | None = None
        self._ticks = 0
        self._final: dict[str, Any] | None = None

    # ------------------------------------------------------------------ 입력

    @property
    def camera_ids(self) -> tuple[str, ...]:
        return tuple(sorted(self._cameras))

    @property
    def final(self) -> bool:
        return self._final is not None

    def observe(self, t: int, views: Sequence[LocalView]) -> None:
        """공통 시각 t 의 view 묶음 하나. 판정이 끝났으면 무시한다."""
        if self._final is not None:
            return
        if self._last_t is not None and t <= self._last_t:
            return
        own = [v for v in views if v.camera_id in self._cameras and v.t == t]
        self._last_t = t
        self._ticks += 1
        if not own:
            return
        try:
            assoc = self.associator.associate(t, own)
        except (ValueError, KeyError):
            return  # 관절 수가 다른 입력 등. 이 시각은 근거로 쓰지 않는다
        if self._first_t is None:
            self._first_t = t
        self._last_seen_t = t
        per_camera: dict[str, list[LocalView]] = {}
        for v in own:
            per_camera.setdefault(v.camera_id, []).append(v)
        for cid in per_camera:
            self._views[cid] += 1
            self._seen_t[cid] = t
        evidence = {}
        for p in assoc.pairs:
            evidence[(p.a, p.b)] = p
            evidence[(p.b, p.a)] = p
        for stats in self._pairs.values():
            a, b = stats.camera_ids
            if len(per_camera.get(a, ())) != 1 or len(per_camera.get(b, ())) != 1:
                continue  # 한 사람일 때만(다른 사람끼리 짝지은 근거는 보정과 무관한 탈락이다)
            p = evidence.get((per_camera[a][0].key, per_camera[b][0].key))
            if p is None or p.reject in _NON_GEOMETRIC:
                continue
            stats.samples += 1
            if p.reject is None:
                stats.passed += 1
                stats.marginal += int(p.marginal)
            else:
                stats.rejects[str(p.reject)] = stats.rejects.get(str(p.reject), 0) + 1
            if p.residual_px is not None and math.isfinite(p.residual_px):
                stats.residuals.append(float(p.residual_px))
        people = []
        for cluster in assoc.clusters:
            if cluster.identity_uncertain or len(cluster.views) < 3:
                continue
            people.append({v.camera_id: v.view.keypoints for v in cluster.views})
        if people:
            for cid, error, attributable in leave_one_out_samples(self._cameras, people, self.monitor_config):
                if attributable:
                    self._loo[cid].append(float(error))
                else:
                    self._loo_ambiguous[cid] += 1

    # ------------------------------------------------------------------ 판정

    def _span_ms(self) -> int:
        if self._first_t is None or self._last_seen_t is None:
            return 0
        return self._last_seen_t - self._first_t

    def _required(self, key: str) -> bool:
        return self._pairs[key].overlapping or key in self._required_extra

    def _pair_view(self, key: str, *, closing: bool) -> dict[str, Any]:
        cfg = self.config
        s = self._pairs[key]
        rate = s.passed / s.samples if s.samples else None
        residual = float(np.median(s.residuals)) if s.residuals else None
        if s.samples >= cfg.min_pair_samples:
            ok = rate is not None and rate >= cfg.min_pair_pass_rate and (
                residual is None or residual <= cfg.max_pair_residual_median_px)
            verdict = PASS if ok else FAIL
        elif not s.overlapping and key not in self._required_extra:
            verdict = NOT_OVERLAPPING
        else:
            verdict = INSUFFICIENT
        if closing and verdict == INSUFFICIENT and key in self._required_extra:
            verdict = FAIL  # 세팅 BA 에서 '확인 필요'였던 쌍은 사람으로도 확인하지 못하면 통과가 아니다
        out: dict[str, Any] = {
            "camera_ids": list(s.camera_ids),
            "required": self._required(key),
            "samples": s.samples,
            "pass_rate": None if rate is None else round(rate, 4),
            "residual_median_px": None if residual is None else round(residual, 3),
            "verdict": verdict,
        }
        if s.rejects:
            out["rejects"] = dict(sorted(s.rejects.items()))
        return out

    def _camera_view(self, cid: str, *, closing: bool) -> dict[str, Any]:
        cfg = self.config
        values = self._loo[cid]
        median = float(np.median(values)) if values else None
        p95 = float(np.percentile(values, 95)) if values else None
        if self._views[cid] < cfg.min_camera_views:
            verdict = NOT_OBSERVED
        elif len(values) >= cfg.min_loo_samples:
            verdict = PASS if median is not None and median <= cfg.max_loo_median_px else FAIL
        else:
            verdict = INSUFFICIENT
        if closing and verdict == NOT_OBSERVED:
            verdict = FAIL
        seen = self._seen_t.get(cid)
        return {
            "observed_views": self._views[cid],
            "loo_samples": len(values),
            "loo_median_px": None if median is None else round(median, 3),
            "loo_p95_px": None if p95 is None else round(p95, 3),
            "verdict": verdict,
            "person_visible": bool(seen is not None and self._last_t is not None
                                   and self._last_t - seen <= PERSON_VISIBLE_WINDOW_MS),
        }

    def _ready_to_close(self) -> bool:
        cfg = self.config
        span = self._span_ms()
        if span >= cfg.max_duration_ms:
            return True
        if span < cfg.min_duration_ms:
            return False
        if any(self._views[c] < cfg.min_camera_views for c in self._views):
            return False
        return all(self._pairs[k].samples >= cfg.min_pair_samples for k in self._pairs if self._required(k))

    def _progress_pct(self) -> float:
        cfg = self.config
        fractions = [min(1.0, self._span_ms() / cfg.min_duration_ms)]
        fractions += [min(1.0, n / cfg.min_camera_views) for n in self._views.values()]
        fractions += [min(1.0, s.samples / cfg.min_pair_samples) for k, s in self._pairs.items() if self._required(k)]
        return round(100.0 * sum(fractions) / len(fractions), 1)

    def summary(self) -> dict[str, Any]:
        """계약 site_setup#/$defs/camera_check 모양. 끝나면(PASSED·FAILED) 같은 값을 계속 돌려준다."""
        if self._final is not None:
            return json.loads(json.dumps(self._final))
        closing = self._first_t is not None and self._ready_to_close()
        pairs = {k: self._pair_view(k, closing=closing) for k in sorted(self._pairs)}
        cameras = {c: self._camera_view(c, closing=closing) for c in sorted(self._views)}
        out: dict[str, Any] = {
            "calibration_version": self.calibration_version,
            "elapsed_s": round(self._span_ms() / 1000, 1),
            "thresholds": self.config.thresholds(),
            "pairs": pairs,
            "cameras": cameras,
        }
        if self._first_t is None:
            out.update(status=WAITING, progress_pct=0.0, problems=[])
            return out
        if not closing:
            out.update(status=MEASURING, progress_pct=min(99.0, self._progress_pct()), problems=[])
            return out
        problems = _problems(pairs, cameras, self.config)
        out.update(status=FAILED if problems else PASSED, progress_pct=100.0, problems=problems)
        if not problems:
            out["baseline"] = _baseline_json(self.baseline_record())
        self._final = json.loads(json.dumps(out))
        return out

    def incomplete(self) -> dict[str, Any]:
        """판정 전에 시간이 다했다(시도 상한·작업 마감). 지금까지의 값으로 INCOMPLETE 를 고정한다.

        문제는 표본 부족 사유만 싣는다(S15P31A101-687): 사람이 하나도 안 보였으면 NO_PERSON, 관측이 모자란 카메라마다 FEW_SAMPLES,
        카메라는 채웠는데 필수 쌍의 동시 관측이 모자라면 그 쌍 FEW_SAMPLES, 그래도 없으면(걸은 시간 부족) NOT_FINISHED."""
        if self._final is not None:
            return self.summary()
        out = self.summary()
        if out["status"] in FINAL_STATUSES:
            return out
        out["status"] = INCOMPLETE
        out["problems"] = self._shortage_problems(out)
        self._final = json.loads(json.dumps(out))
        return out

    def _shortage_problems(self, out: Mapping[str, Any]) -> list[dict[str, Any]]:
        cfg = self.config
        retry = "표시된 구간을 지나며 30초만 다시 걸어 주세요"
        if self._first_t is None:
            return [{"kind": "CHECK", "id": "camera_check", "camera_ids": [], "reason": "NO_PERSON",
                     "message": "어느 카메라에서도 사람이 보이지 않았습니다. 한 사람이 카메라 4대에 모두 보이도록 "
                                "30초만 다시 걸어 주세요"}]
        problems: list[dict[str, Any]] = []
        for cid, cam in out["cameras"].items():
            if cam["observed_views"] < cfg.min_camera_views:
                problems.append({"kind": "CAMERA", "id": cid, "camera_ids": [cid], "reason": "FEW_SAMPLES",
                                 "message": f"{cid} 에서 사람이 충분히 보이지 않았습니다"
                                            f"({cam['observed_views']}/{cfg.min_camera_views}). {retry}"})
        if not problems:
            for key, pair in out["pairs"].items():
                if pair["required"] and pair["samples"] < cfg.min_pair_samples:
                    a, b = pair["camera_ids"]
                    problems.append({"kind": "PAIR", "id": key, "camera_ids": [a, b], "reason": "FEW_SAMPLES",
                                     "message": f"{a}–{b}: 두 카메라에 한 사람이 함께 보인 시간이 모자랍니다"
                                                f"({pair['samples']}/{cfg.min_pair_samples}). 두 카메라가 함께 보는 곳을 지나며 "
                                                "30초만 다시 걸어 주세요"})
        if not problems:
            problems.append({"kind": "CHECK", "id": "camera_check", "camera_ids": [], "reason": "NOT_FINISHED",
                             "message": f"사람이 보인 시간이 {self._span_ms() / 1000:.0f}초로 모자랍니다"
                                        f"(최소 {cfg.min_duration_ms / 1000:.0f}초). 30초 동안 계속 걸어 주세요"})
        return problems[:64]

    def baseline_record(self) -> BaselineRecord:
        """통과한 카메라의 한 카메라 제외 재투영 중앙값(관제 감시와 같은 통계). 표본이 모자란 카메라는 넣지 않는다(모르면 비움)."""
        values = {
            cid: float(np.median(v))
            for cid, v in self._loo.items()
            if len(v) >= max(self.config.min_loo_samples, self.monitor_config.min_window_samples)
            and float(np.median(v)) <= self.config.max_loo_median_px
        }
        ids = tuple(sorted(values))
        return BaselineRecord(self.calibration_version, values, ids,
                              {cid: BASELINE_SOURCE_SETUP_CAMERA_CHECK for cid in ids})


_PAIR_REASON_TEXT = {
    "PASS_RATE_LOW": "두 카메라가 같은 사람을 같은 위치로 보지 못했습니다(통과율 {rate:.0%} < {need:.0%})",
    "RESIDUAL_HIGH": "두 카메라 삼각측량 잔차가 큽니다(중앙값 {residual:.1f}px > {limit:.1f}px)",
    "NOT_CONFIRMED": "보정에서 확인 필요였던 쌍인데 두 카메라에 한 사람이 동시에 충분히 보이지 않았습니다",
}


def _problems(pairs: Mapping[str, Mapping[str, Any]], cameras: Mapping[str, Mapping[str, Any]],
              cfg: CameraCheckConfig) -> list[dict[str, Any]]:
    problems: list[dict[str, Any]] = []
    for key, p in pairs.items():
        if p["verdict"] != FAIL:
            continue
        a, b = p["camera_ids"]
        rate, residual = p["pass_rate"], p["residual_median_px"]
        if p["samples"] < cfg.min_pair_samples:
            reason = "NOT_CONFIRMED"
        elif rate is not None and rate < cfg.min_pair_pass_rate:
            reason = "PASS_RATE_LOW"
        else:
            reason = "RESIDUAL_HIGH"
        text = _PAIR_REASON_TEXT[reason].format(rate=rate or 0.0, need=cfg.min_pair_pass_rate,
                                                residual=residual or 0.0, limit=cfg.max_pair_residual_median_px)
        problems.append({"kind": "PAIR", "id": key, "camera_ids": [a, b], "reason": reason,
                         "message": f"{a}–{b}: {text}"})
    for cid, c in cameras.items():
        if c["verdict"] != FAIL:
            continue
        if c["observed_views"] < cfg.min_camera_views:
            problems.append({"kind": "CAMERA", "id": cid, "camera_ids": [cid], "reason": "NOT_OBSERVED",
                             "message": f"{cid}: 사람이 충분히 보이지 않았습니다(카메라 방향·연결을 확인하세요)"})
        else:
            problems.append({"kind": "CAMERA", "id": cid, "camera_ids": [cid], "reason": "REPROJECTION_HIGH",
                             "message": f"{cid}: 다른 카메라로 잰 사람 관절이 이 카메라 영상과 어긋납니다"
                                        f"(중앙값 {c['loo_median_px']:.1f}px > {cfg.max_loo_median_px:.1f}px)"})
    evaluated = [p for p in pairs.values() if p["verdict"] in (PASS, FAIL)]
    if len(evaluated) < cfg.min_evaluated_pairs:
        problems.append({"kind": "CHECK", "id": "pairs", "camera_ids": [], "reason": "FEW_PAIRS",
                         "message": f"판정한 카메라 쌍이 {len(evaluated)}개뿐입니다(최소 {cfg.min_evaluated_pairs}개). "
                                    "한 사람이 카메라 사이 겹치는 곳을 고루 걸어야 합니다"})
    for cid in cameras:
        if cameras[cid]["verdict"] == FAIL:
            continue
        if not any(p["verdict"] == PASS and cid in p["camera_ids"] for p in pairs.values()):
            problems.append({"kind": "CAMERA", "id": cid, "camera_ids": [cid], "reason": "NO_PASSING_PAIR",
                             "message": f"{cid}: 이 카메라가 낀 쌍 중 통과한 쌍이 없습니다"})
    return problems


# --------------------------------------------------------------------------- 직렬화(Tier 0 → 워커 파일)


def views_to_json(t: int, views: Sequence[LocalView]) -> dict[str, Any]:
    """공통 시각 t 의 view 묶음 한 줄. 원본 픽셀·신뢰도·null 을 그대로 둔다(보정과 무관한 2D 관측)."""
    return {
        "t": int(t),
        "views": [
            {
                "c": v.camera_id,
                "k": v.local_track_id,
                "kp": [None if kp is None else [float(x) for x in kp] for kp in v.view.keypoints],
                "src": v.view.timestamp_source,
                "unc": float(v.view.timestamp_uncertainty_ms),
                "st": [int(s) for s in v.view.source_times],
                "bb": None if v.view.bbox is None else [float(x) for x in v.view.bbox],
            }
            for v in views
        ],
    }


def views_from_json(doc: Mapping[str, Any]) -> tuple[int, list[LocalView]]:
    """`views_to_json` 의 역. 형식이 틀리면 ValueError."""
    try:
        t = int(doc["t"])
        out = []
        for item in doc["views"]:
            kps = tuple(None if kp is None else tuple(float(x) for x in kp) for kp in item["kp"])
            bbox = item.get("bb")
            view = InterpolatedView(
                camera_id=str(item["c"]),
                t=t,
                keypoints=kps,
                timestamp_source=str(item.get("src") or "UNKNOWN"),
                timestamp_uncertainty_ms=float(item.get("unc") or 0.0),
                source_times=tuple(int(s) for s in item.get("st") or (t,)),
                bbox=None if bbox is None else tuple(float(x) for x in bbox),  # type: ignore[arg-type]
            )
            out.append(LocalView(str(item["c"]), str(item["k"]), view))
        return t, out
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError(f"카메라 확인 view 줄 형식 오류: {exc}") from None


# --------------------------------------------------------------------------- 기준선 파일(워커 → Tier 0)


def _baseline_json(record: BaselineRecord) -> dict[str, Any]:
    return {
        "calibration_version": record.calibration_version,
        "values_px": {cid: round(float(v), 4) for cid, v in sorted(record.values_px.items())},
        "source": BASELINE_SOURCE_SETUP_CAMERA_CHECK,
    }


def baseline_path(root: Path, calibration_version: str) -> Path:
    """보정 버전 하나의 기준선 파일. 버전 문자열을 파일 이름에 그대로 쓰지 않는다."""
    digest = hashlib.sha256(calibration_version.encode("utf-8")).hexdigest()[:32]
    return Path(root) / "baselines" / f"{digest}.json"


def save_baseline(root: Path, record: BaselineRecord) -> Path:
    """검증된 기준선을 원자적으로 쓴다(같은 Pod 의 Tier 0 가 그 보정을 채택할 때 읽는다)."""
    path = baseline_path(root, record.calibration_version)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(f".{os.getpid()}.tmp")
    tmp.write_text(json.dumps(_baseline_json(record), ensure_ascii=False, sort_keys=True), encoding="utf-8")
    os.replace(tmp, path)
    return path


def baseline_from_json(doc: Any) -> BaselineRecord | None:
    """기준선 문서(계약 camera_check.baseline 과 같은 모양) → BaselineRecord. 틀리면 None(지어내지 않는다)."""
    if not isinstance(doc, Mapping) or doc.get("source") != BASELINE_SOURCE_SETUP_CAMERA_CHECK:
        return None
    version = doc.get("calibration_version")
    values = doc.get("values_px")
    if not isinstance(version, str) or not version or not isinstance(values, Mapping) or not values:
        return None
    clean = {}
    for cid, value in values.items():
        if not isinstance(cid, str) or not is_finite_number(value) or value < 0:
            return None
        clean[cid] = float(value)
    ids = tuple(sorted(clean))
    return BaselineRecord(version, clean, ids, {cid: BASELINE_SOURCE_SETUP_CAMERA_CHECK for cid in ids})


def load_baseline(root: Path | None, calibration_version: str) -> BaselineRecord | None:
    """그 보정 버전의 카메라 확인 기준선. 없거나 형식이 틀리거나 버전이 다르면 None."""
    if root is None:
        return None
    try:
        doc = json.loads(baseline_path(root, calibration_version).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    record = baseline_from_json(doc)
    if record is None or record.calibration_version != calibration_version:
        return None
    return record
