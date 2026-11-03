"""실행 중 보정의 연속 확인·누적 편차·교체 후 감시. 시각과 지표는 호출자가 공급한다."""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from typing import Any

from simai_ai.calibration.monitor import MonitorReport, MonitorStatus


class FusionWindow:
    """후보 holdout과 같은 분모(2대 이상 클러스터)의 3+대 융합률. 관측 없으면 비운다."""

    def __init__(self, window_ms: int, min_samples: int):
        self.window_ms, self.min_samples = window_ms, min_samples
        self.version = None
        self.samples = deque()

    def observe(self, association) -> float | None:
        if association.calibration_version != self.version:
            self.samples.clear()
            self.version = association.calibration_version
        counts = [len(c.views) for c in association.clusters if not c.identity_uncertain and len(c.views) >= 2]
        self.samples.append((association.t, len(counts), sum(n >= 3 for n in counts)))
        while self.samples and self.samples[0][0] <= association.t - self.window_ms:
            self.samples.popleft()
        total = sum(n for _, n, _ in self.samples)
        if not counts or total < self.min_samples:
            return None
        return sum(n for _, _, n in self.samples) / total


@dataclass
class CandidateConfirmation:
    previous_version: str | None = None
    last_end_ms: int | None = None
    count: int = 0

    def reset(self) -> None:
        self.previous_version, self.last_end_ms, self.count = None, None, 0

    def observe(self, report: dict[str, Any]) -> bool:
        """동일 활성 버전에서 새 표본을 포함한 두 번의 연속 개선만 채택한다."""
        version = report.get("previous_version")
        end = report.get("validation_end_ms")
        if not report.get("improvement_decision", {}).get("accepted") or not isinstance(end, int):
            self.reset()
            return False
        if version != self.previous_version:
            self.reset()
        if self.last_end_ms is not None and end <= self.last_end_ms:
            return False
        self.count += 1
        self.previous_version, self.last_end_ms = version, end
        report["consecutive_confirmation"] = {"count": self.count, "required": 2, "validation_end_ms": end}
        return self.count >= 2


@dataclass
class SwapProbation:
    previous_version: str
    active_version: str
    started_ms: int
    fusion_before: float | None = None
    bad_since_ms: int | None = None
    last_sample_ms: int | None = None
    window_ms: int = 60_000
    sustain_ms: int = 10_000
    last_bad_ms: int | None = None

    def observe(self, report: MonitorReport, fusion_ratio: float | None = None) -> bool:
        if report.calibration_version != self.active_version or not self.started_ms <= report.t_ms <= self.started_ms + self.window_ms:
            return False
        fresh = [c for c in report.cameras if c.window_samples >= report.min_window_samples
                 and c.window_last_sample_ms is not None
                 and 0 <= report.t_ms - c.window_last_sample_ms <= (5_000 if report.max_sample_age_ms is None else report.max_sample_age_ms)]
        latest = max((c.window_last_sample_ms for c in fresh), default=None)
        if fusion_ratio is not None:
            latest = report.t_ms
        if latest is None or (self.last_sample_ms is not None and latest <= self.last_sample_ms):
            return False
        self.last_sample_ms = latest
        bad = any(c.status in (MonitorStatus.SUSPECT, MonitorStatus.DRIFTED) for c in fresh)
        bad |= (fusion_ratio is not None and self.fusion_before is not None
                and fusion_ratio < self.fusion_before - 0.05)
        if not bad:
            self.bad_since_ms = self.last_bad_ms = None
            return False
        gap = 5_000 if report.max_sample_age_ms is None else max(5_000, report.max_sample_age_ms)
        if self.last_bad_ms is None or report.t_ms - self.last_bad_ms > gap:
            self.bad_since_ms = report.t_ms
        self.last_bad_ms = report.t_ms
        return self.bad_since_ms is not None and report.t_ms - self.bad_since_ms >= self.sustain_ms
