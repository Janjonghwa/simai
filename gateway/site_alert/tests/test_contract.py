"""계약과 맞는지: 저장소 contracts/ 의 예시를 해석기·코어에 넣고, 만든 site_ack·alarm 상태를 스키마로 검증한다."""

from __future__ import annotations

import json

import pytest
from conftest import contracts_dir, load_example

from simai_site_alert.bench import local_alert, uuid7
from simai_site_alert.core import Outcome, SiteAlertCore
from simai_site_alert.messages import SITE_ACK_SCHEMA_VERSION, build_site_ack


def test_local_alert_example_alarms(core: SiteAlertCore, sound) -> None:
    raised = load_example("local_alert")
    assert core.handle(raised).outcome is Outcome.APPLIED
    assert sound.playing.name == "CRITICAL"
    alert = core.active_alerts()[raised["payload"]["event_id"]]
    assert alert.summary == raised["payload"]["summary"] and alert.rule_id == "R2" and alert.zone_id == "z-04"


def test_resolved_example_clears(core: SiteAlertCore, sound) -> None:
    raised, resolved = load_example("local_alert"), load_example("local_alert.resolved")
    core.handle(raised)
    resolved = {**resolved, "msg_id": "0192a6f0-7c1e-7b3a-9f2d-0000000001ff"}  # 예시 둘의 msg_id 가 같다
    assert core.handle(resolved).outcome is Outcome.APPLIED
    assert core.screen_state().alerts == () and sound.playing is None


def test_alarm_stop_example(core: SiteAlertCore, sound) -> None:
    core.handle(load_example("local_alert"))
    assert core.handle(load_example("alarm_stop")).outcome is Outcome.APPLIED
    assert sound.playing is None


def test_health_example_resyncs_active_alerts(core: SiteAlertCore, sound) -> None:
    """활성 현장 경보 목록 예시: 확인된 CRITICAL 은 표시만, 확인 전 HIGH 는 표시 + 소리(명세 3.10)."""
    health = load_example("health")
    assert core.handle(health).outcome is Outcome.APPLIED
    items = health["payload"]["active_site_alerts"]
    alerts = core.active_alerts()
    assert set(alerts) == {i["event_id"] for i in items}
    for item in items:
        alert = alerts[item["event_id"]]
        assert (alert.event_sequence, alert.severity, alert.acknowledged, alert.summary) == (
            item["event_sequence"], item["severity"], item["acknowledged"], item["summary"])
    assert sound.playing.name == "HIGH"


@pytest.mark.parametrize(
    "name", ["health.site_alert_caution", "health.site_alert_replay_source", "health.site_alert_missing_sequence"]
)
def test_invalid_site_alert_items_never_alarm(core: SiteAlertCore, sound, name: str) -> None:
    """계약 위반 항목(CAUTION·REPLAY·sequence 없음)은 표시·소리로 내지 않는다(AGENTS.md 규칙 5)."""
    path = contracts_dir() / "examples" / "invalid" / f"{name}.json"
    msg = {k: v for k, v in json.loads(path.read_text(encoding="utf-8")).items() if not k.startswith("_")}
    core.handle(msg)
    bad = {i.get("event_id") for i in msg["payload"]["active_site_alerts"]
           if i.get("source") != "LIVE" or i.get("severity") not in ("CRITICAL", "HIGH") or "event_sequence" not in i}
    assert bad and not (bad & set(core.active_alerts()))
    assert all(a.severity in ("CRITICAL", "HIGH") for a in core.active_alerts().values())


@pytest.mark.parametrize("name", ["local_alert.replay_source", "local_alert.caution_severity"])
def test_invalid_local_alert_examples_rejected(core: SiteAlertCore, sound, name: str) -> None:
    path = contracts_dir() / "examples" / "invalid" / f"{name}.json"
    msg = {k: v for k, v in json.loads(path.read_text(encoding="utf-8")).items() if not k.startswith("_")}
    assert core.handle(msg).outcome is Outcome.REJECTED
    assert sound.calls == []


def test_built_site_ack_matches_contract(core: SiteAlertCore, contract_validator) -> None:
    eid = uuid7()
    core.handle(local_alert(eid, 1, "RAISED"))
    envelope = core.site_ack(eid)
    contract_validator("site_ack", envelope)
    assert envelope["payload"]["event_sequence"] == 1  # 누를 때 표시하던 상태(확인 대상)
    assert envelope["schema_version"] == SITE_ACK_SCHEMA_VERSION == load_example("site_ack")["schema_version"]


def test_build_site_ack_rejects_non_uuid7() -> None:
    with pytest.raises(ValueError):
        build_site_ack(event_id="0192a6f0-7c1e-4b3a-9f2d-3c4b5a6d7e8f", laptop_id="laptop-01", now_ms=1)


def test_alarm_status_fits_laptop_heartbeat(core: SiteAlertCore, contract_validator) -> None:
    a, b = uuid7(), uuid7()
    core.handle(local_alert(a, 1, "RAISED"))
    core.handle(local_alert(b, 1, "RAISED", severity="HIGH", acknowledged=True))
    heartbeat = load_example("laptop_heartbeat")
    heartbeat["payload"]["alarm"] = core.alarm_status()
    contract_validator("laptop_heartbeat", heartbeat)
    assert heartbeat["payload"]["alarm"] == {"sounding_event_ids": [a], "displayed_event_ids": [a, b]}


def test_bench_messages_match_contract(contract_validator) -> None:
    contract_validator("local_alert", local_alert(uuid7(), 1, "RAISED"))
    contract_validator("local_alert", local_alert(uuid7(), 2, "RESOLVED", severity="HIGH"))
