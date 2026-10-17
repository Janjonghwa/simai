"""심각도 × 신뢰도 → 채널 (명세 7.3 표가 정본), 출처 분리(7.4)."""

import pytest

from simai_ai.risk import Channel, Confidence, Origin, Severity, channels_for
from simai_ai.risk.alerting import LOCAL_ALERT_SEVERITIES

ALERT = {Channel.LOCAL_ALERT, Channel.CONTROL_SCREEN_ALERT, Channel.FOREMAN_PWA, Channel.WATCH}

# 7.3 표를 그대로 옮긴 기대값.
SPEC_TABLE = {
    (Severity.CRITICAL, Confidence.HIGH): ALERT,
    (Severity.CRITICAL, Confidence.LOW): ALERT,
    (Severity.HIGH, Confidence.HIGH): ALERT,
    (Severity.HIGH, Confidence.LOW): ALERT,
    (Severity.CAUTION, Confidence.HIGH): {Channel.CONTROL_SCREEN_NOTICE, Channel.WATCH_WEAK_VIBRATION},
    (Severity.CAUTION, Confidence.LOW): {Channel.CONTROL_SCREEN_NOTICE},
    (Severity.INFO, Confidence.HIGH): set(),
    (Severity.INFO, Confidence.LOW): set(),
}


@pytest.mark.parametrize(("severity", "confidence"), sorted(SPEC_TABLE))
def test_live_channels_follow_spec_table(severity, confidence):
    plan = channels_for(severity, confidence, Origin.LIVE)
    assert plan.channels == SPEC_TABLE[(severity, confidence)]
    assert plan.needs_check is (confidence is Confidence.LOW)


def test_low_confidence_never_drops_alert_channels():
    for severity in Severity:
        high = channels_for(severity, Confidence.HIGH, Origin.LIVE).channels
        low = channels_for(severity, Confidence.LOW, Origin.LIVE).channels
        assert ALERT & high <= low  # 경보 채널은 신뢰도가 낮아도 그대로다


def test_local_alert_severities_match_table():
    from_table = {s for s in Severity if Channel.LOCAL_ALERT in channels_for(s, Confidence.HIGH, Origin.LIVE).channels}
    assert from_table == LOCAL_ALERT_SEVERITIES == {Severity.CRITICAL, Severity.HIGH}


@pytest.mark.parametrize("origin", [Origin.PLANNED, Origin.WHAT_IF, Origin.REPLAY])
@pytest.mark.parametrize(("severity", "confidence"), sorted(SPEC_TABLE))
def test_non_live_has_no_channels(origin, severity, confidence):
    assert channels_for(severity, confidence, origin).channels == frozenset()


def test_confidence_from_needs_check():
    assert Confidence.from_needs_check(True) is Confidence.LOW
    assert Confidence.from_needs_check(False) is Confidence.HIGH
