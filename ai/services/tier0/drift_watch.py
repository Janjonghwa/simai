"""-346: 연관 결과를 솎아 별도 스레드의 기존 보정 감시기에 공급한다."""

from __future__ import annotations

import logging
import threading
from dataclasses import replace
from collections.abc import Callable, Mapping
from typing import Any

from simai_ai.calibration.bundle import PersonObservation
from simai_ai.calibration.drift import CameraObservation
from simai_ai.calibration.monitor import BaselineRecord, MonitorConfig, MonitorReport, ReprojectionMonitor
from simai_ai.calibration.live_safety import FusionWindow
from simai_ai.tracking.cross_view import AssociationFrame

log = logging.getLogger("services.tier0.drift_watch")


def person_observations(association: AssociationFrame, limit: int = 8) -> tuple[PersonObservation, ...]:
    """확실하게 연결된 3대 이상 관측만 복사한다. 원본 픽셀과 null을 보존한다."""
    people = []
    for cluster in association.clusters:
        if cluster.identity_uncertain or len(cluster.views) < 3:
            continue
        views = {v.camera_id: tuple(None if k is None else tuple(k) for k in v.view.keypoints)
                 for v in cluster.views}
        people.append(PersonObservation(cluster.association_id, association.t, views))
        if len(people) >= limit:
            break
    return tuple(people)


class BackgroundDriftWatch:
    """대기 표본은 최신 하나. 잠금 안에서는 참조 교환만 하고 기하 계산은 밖에서 한다.

    offer는 판정 스레드, run은 보조 스레드 하나가 호출한다. 문서와 AssociationFrame은 이후 변경하지 않는다.
    """

    def __init__(self, on_report: Callable[[MonitorReport, tuple[str, ...]], None], *,
                 baselines: Mapping[str, BaselineRecord], config: MonitorConfig = MonitorConfig(),
                 sample_period_ms: int = 1_000,
                 baseline_lookup: Callable[[str], BaselineRecord | None] | None = None) -> None:
        """baseline_lookup: 저장한 기준선에 검증된 카메라가 없는 보정 버전의 감시기를 만들 때 부른다. 세팅 「카메라 확인」이
        같은 Pod 에 남긴 그 보정의 검증 기준선(SETUP_CAMERA_CHECK)을 돌려주면 관제 첫 10초 즉석 학습 대신 쓴다(S15P31A101-646)."""
        self._on_report = on_report
        self._baselines = dict(baselines)
        self._baseline_lookup = baseline_lookup
        self._reset_cameras: set[tuple[str, str]] = set()
        self._config = config
        self._period = sample_period_ms
        self._lock = threading.Lock()
        self._pending = None
        self._last_offer: tuple[str, int] | None = None
        self._last_streams: tuple[CameraObservation, ...] = ()
        self._monitor: ReprojectionMonitor | None = None
        self._fusion = FusionWindow(config.window_ms, config.min_window_samples)
        self._wake = threading.Event()
        self.errors = 0
        self.dropped = 0

    def offer(self, document: Mapping[str, Any], association: AssociationFrame,
              streams: tuple[CameraObservation, ...] = ()) -> None:
        version, t = association.calibration_version, association.t
        with self._lock:
            if self._last_offer is not None and self._last_offer[0] == version:
                if t - self._last_offer[1] < self._period and streams == self._last_streams:
                    return
            self._last_offer = version, t
            self._last_streams = streams
            self.dropped += self._pending is not None
            self._pending = document, association, streams
        self._wake.set()

    def baselines(self) -> dict[str, BaselineRecord]:
        with self._lock:
            return dict(self._baselines)

    def reset_camera(self, calibration_version: str, camera_id: str) -> None:
        """재검증으로 융합에 되돌린 카메라의 확정 틀어짐을 다음 측정 전에 푼다(감시기는 보조 스레드만 만진다)."""
        with self._lock:
            self._reset_cameras.add((calibration_version, camera_id))

    def _baseline_for(self, version: str) -> BaselineRecord | None:
        known = self.baselines().get(version)
        if known is not None and known.verified_camera_ids:
            return known
        if self._baseline_lookup is not None:
            try:
                found = self._baseline_lookup(version)
            except Exception:  # noqa: BLE001 - 기준선 파일 문제로 감시를 멈추지 않는다(즉석 학습으로 간다)
                log.exception("카메라 확인 기준선 읽기 실패(즉석 학습으로 감시한다)")
                found = None
            if found is not None and found.calibration_version == version:
                log.info("세팅 카메라 확인 기준선으로 감시: %s %s", version,
                         {cid: round(v, 2) for cid, v in sorted(found.values_px.items())})
                return found
        return known

    def run(self, stop: threading.Event) -> None:
        while not stop.is_set():
            self._wake.wait(0.1)
            self._wake.clear()
            with self._lock:
                item, self._pending = self._pending, None
            if item is None or stop.is_set():
                continue
            try:
                self._measure(*item)
            except Exception:
                self.errors += 1
                log.exception("보정 감시 실패(판정은 계속한다)")

    def _measure(self, document, association, streams) -> None:
        version = association.calibration_version
        if self._monitor is None or self._monitor.calibration_version != version:
            self._monitor = ReprojectionMonitor(document, self._config, baseline=self._baseline_for(version))
            self._fusion = FusionWindow(self._config.window_ms, self._config.min_window_samples)
        with self._lock:
            resets = {cid for v, cid in self._reset_cameras if v == version}
            self._reset_cameras = set()  # 다른(지난) 보정 버전 요청은 버린다
        for cid in sorted(resets):
            self._monitor.reset_camera(cid)
        people = person_observations(association)
        report = self._monitor.observe(association.t, [p.views for p in people], streams)
        report = replace(report, fusion_3plus_ratio=self._fusion.observe(association))
        baseline = self._monitor.baseline_record()
        with self._lock:
            self._baselines[version] = baseline
        self._on_report(report, self._monitor.stream_changed_camera_ids)
