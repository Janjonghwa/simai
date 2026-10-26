"""막힌 망 I0 예비 경로 측정 도구(deploy/scripts/i0_link_measure.py, S15P31A101-288) 시험. GPU·RunPod·wstunnel 을 쓰지 않는다.

- pod 계획: tunnel 이면 Tier 0 가 127.0.0.1 의 고정 포트에서만 듣고, 같은 값이 엔드포인트 등록에도 간다(노트북 로컬 포트 = Pod 포트).
  direct 면 RunPod 대칭 매핑 값을 쓰고, 모르면 지어내지 않고 이름을 모두 적어 실패한다.
- 노트북 대상: endpoint.json 의 지문이 --pin 에 없으면 붙지 않는다.
- 요약: 처리량·재연결·100초 끊김·지연 분위수, 끝낼 때의 끊김은 세지 않는다.
노트북 relay·제어 채널 조립(laptop)은 gateway/relay 환경에서 도는 수동 확인이다(로컬 대역 Tier 0 왕복은 MR 에 적었다).
"""

from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

import pytest

from .conftest import load_script

AI = Path(__file__).resolve().parents[2]
for p in (AI, AI / "tests" / "services" / "tier0"):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

CAMS = ("cam1", "cam2", "cam3", "cam4")
FP = "sha256:" + "ab" * 32


@pytest.fixture
def lm():
    return load_script("i0_link_measure")


def test_tunnel_plan_listens_on_loopback_and_registers_the_same_ports(lm, tmp_path):
    from test_i1_tls import make_cert

    from services.tier0.endpoint_registration import build_payload
    from services.tier0.settings import _control
    from services.tier0.video_ingest import listen_specs_from_env

    endpoint, env = lm.pod_plan("tunnel", {"RUNPOD_PUBLIC_IP": "203.0.113.7", "RUNPOD_TCP_PORT_70000": "40000"}, CAMS,
                                control_port=17000, video_port=17001)
    assert endpoint == {"link": "tunnel", "host": "127.0.0.1", "control_port": 17000,
                        "video_ports": dict.fromkeys(CAMS, 17001), "control_path": "/control"}
    # RunPod 가 넣은 값이 있어도 사이트 무관 이름(SIMAI_PUBLIC_*)이 먼저다: Tier 0 가 듣는 곳·등록하는 곳이 모두 터널 쪽이다
    merged = {"RUNPOD_PUBLIC_IP": "203.0.113.7", "RUNPOD_TCP_PORT_70000": "40000", "RUNPOD_TCP_PORT_70001": "40001", **env}
    [spec] = listen_specs_from_env(merged)
    assert (spec.host, spec.port, spec.cameras) == ("127.0.0.1", 17001, frozenset(CAMS))
    cert, key = make_cert(tmp_path)
    control = _control({**merged, "SIMAI_TIER0_TLS_CERT_FILE": str(cert), "SIMAI_TIER0_TLS_KEY_FILE": str(key),
                        "SIMAI_LAPTOP_TOKEN_SHA256": FP})
    assert (control.host, control.port) == ("127.0.0.1", 17000)
    payload = build_payload(merged, cert_pem=cert.read_bytes(), registered_at_ms=1)
    assert payload["public_ip"] == "127.0.0.1"
    assert {(p["role"], p["public_port"]) for p in payload["ports"]} == {("CONTROL", 17000), ("VIDEO", 17001)}


@pytest.mark.parametrize("control, video", [(0, 17001), (17000, 70000), (17000, 17000)])
def test_tunnel_plan_rejects_bad_ports(lm, control, video):
    with pytest.raises(ValueError):
        lm.pod_plan("tunnel", {}, CAMS, control_port=control, video_port=video)


def test_direct_plan_uses_runpod_mapping_and_never_invents_it(lm):
    env = {"RUNPOD_PUBLIC_IP": "203.0.113.7", "RUNPOD_TCP_PORT_70000": "40000", "RUNPOD_TCP_PORT_70001": "40001"}
    endpoint, extra = lm.pod_plan("direct", env, CAMS)
    assert (endpoint["host"], endpoint["control_port"], set(endpoint["video_ports"].values())) == ("203.0.113.7", 40000, {40001})
    assert extra["SIMAI_TIER0_CONTROL_LISTEN_HOST"] == extra["SIMAI_TIER0_VIDEO_LISTEN_HOST"] == "0.0.0.0"
    assert not any(k.startswith("SIMAI_PUBLIC") for k in extra)
    with pytest.raises(ValueError) as e:
        lm.pod_plan("direct", {"RUNPOD_TCP_PORT_70000": "40000"}, CAMS)
    assert "RUNPOD_PUBLIC_IP" in str(e.value) and "70001" in str(e.value) and "70000 " not in str(e.value)


def test_token_hash_is_required_and_checked(lm):
    with pytest.raises(ValueError):
        lm.token_hashes({})
    with pytest.raises(ValueError):
        lm.token_hashes({"SIMAI_LAPTOP_TOKEN_SHA256": "laptop-device-token"})  # 토큰 그대로는 받지 않는다
    assert lm.token_hashes({"SIMAI_LAPTOP_TOKEN_SHA256": f" {FP} ,"}) == FP


def test_token_hash_command_prints_only_the_hash(lm, monkeypatch, capsys):
    monkeypatch.setenv("SIMAI_DEVICE_TOKEN", "device-token-value-288")
    assert lm.main(["token-hash"]) == 0
    out = capsys.readouterr()
    assert out.out.strip() == "sha256:" + hashlib.sha256(b"device-token-value-288").hexdigest()
    assert "device-token-value-288" not in out.out + out.err


def test_endpoint_with_unpinned_fingerprint_is_refused(lm, tmp_path):
    path = tmp_path / "endpoint.json"
    path.write_text(json.dumps({"host": "127.0.0.1", "control_port": 17000, "video_ports": {"cam1": 17001},
                                "tls_public_key_sha256": FP, "written_at": 1}), encoding="utf-8")
    assert lm.read_endpoint(path, frozenset({FP}))["host"] == "127.0.0.1"
    with pytest.raises(ValueError, match="--pin"):
        lm.read_endpoint(path, frozenset({"sha256:" + "cd" * 32}))


def test_marker_row_keeps_latency_and_link_fields(lm):
    doc = {"started": True, "gpu_priority": {"written_at": 5, "load": {"samples": 3, "unknown": 1, "lag_ms": [70.0, 80.0],
                                                                         "lag_ms_p95": 80.0, "lag_ms_max": 80.0}},
           "status": {"laptop_connected": True, "system_health": "DEGRADED",
                      "live_input": {"cameras": {"cam1": {"video_connected": True, "frame_age_ms_p95": 61, "received": 9,
                                                          "disconnects": 0, "bridge_errors": {}}}}}}
    row = lm.marker_row(doc, 10)
    assert row["written_at"] == 5 and row["laptop_connected"] is True and row["lag"]["lag_ms"] == [70.0, 80.0]
    assert row["cams"]["cam1"]["frame_age_ms_p95"] == 61 and "bridge_errors" not in row["cams"]["cam1"]
    assert lm.marker_row({"started": False}, 10) is None


def _laptop_rows(seconds: int = 300) -> list[dict]:
    """t=0 시작, 1초마다 relay. cam1 은 100초에 끊겼다 102초에 다시 붙고, 제어 채널은 100초에 끊겼다 101초에 붙는다."""
    t0 = 1_800_000_000_000
    rows = [{"t": t0, "kind": "start", "link": "tunnel", "seconds": seconds}, {"t": t0 + 500, "kind": "control", "connected": True}]
    for s in range(1, seconds + 2):  # 마지막 줄은 끝낼 때(끊김) — 세지 않는다
        tearing = s > seconds
        cams = {}
        for c in ("cam1", "cam2"):
            down = tearing or (c == "cam1" and 100 <= s < 102)
            reconnected = c == "cam1" and s >= 102
            cams[c] = {"tier0_video": "DISCONNECTED" if down else "CONNECTED", "connects": 1 + reconnected,
                       "disconnects": int(c == "cam1" and s >= 100) + tearing, "connect_failures": 0,
                       "sent_frames": 25 * s, "sent_bytes": 125_000 * s, "skipped": 0, "dropped_link": 0}
        rows.append({"t": t0 + 1000 * s, "kind": "relay", "cams": cams})
        if 10 <= s <= seconds:
            rows.append({"t": t0 + 1000 * s + 10, "kind": "health", "pipeline_lag_ms": None if s == 10 else 80 + s % 5})
    rows += [{"t": t0 + 100_000, "kind": "control", "connected": False}, {"t": t0 + 101_000, "kind": "control", "connected": True},
             {"t": t0 + (seconds + 1) * 1000 + 5, "kind": "control", "connected": False}]
    return rows


def _pod_rows(t0: int = 1_800_000_000_000, seconds: int = 300) -> list[dict]:
    return [{"t": t0 + s * 1000 + 50, "written_at": t0 + s * 1000, "kind": "pod",
             "cams": {"cam1": {"frame_age_ms_p95": 60 + s % 7, "received": 25 * s, "disconnects": int(s >= 100),
                               "gateway_to_runpod_loss": 0},
                      "cam2": {"frame_age_ms_p95": 55, "received": 25 * s, "disconnects": 0, "gateway_to_runpod_loss": 0}},
             "lag": {"lag_ms": [90.0, 100.0], "unknown": 0}} for s in range(5, seconds + 10, 5)]


def test_summary_counts_reconnects_100s_cuts_and_ignores_teardown(lm):
    r = lm.summarize_run("tunnel", _pod_rows(), _laptop_rows())
    assert r["link"] == "tunnel" and r["window"]["seconds"] == 298.0  # 1초 ~ 299초 relay 줄(끝내기 0.5초 전까지)
    cam1, cam2 = r["cameras"]["cam1"], r["cameras"]["cam2"]
    assert (cam1["disconnects"], cam1["connects"], cam2["disconnects"]) == (1, 1, 0)
    assert r["video_reconnects"] == 1 and r["control_disconnects"] == 1
    assert [s["cut"] for s in r["control_segments"]] == [True, False]
    assert r["cut_near_100s"] == 2  # 제어 99.5초·cam1 99초 구간
    assert cam2["mbps"] == 1.0 and r["throughput_mbps"] == 2.0 and cam1["sent_fps"] == 25.0
    assert r["pipeline_lag_ms_pod"]["p50"] in (90.0, 100.0) and r["pipeline_lag_ms_health"]["null"] == 1
    assert r["frame_age_ms_p95"]["max"] == 66 and cam1["pod_disconnects"] == 1
    assert [e["event"] for e in r["timeline"] if e["link"] == "video:cam1"] == ["DOWN", "UP"]


def test_summarize_writes_json_and_table(lm, tmp_path, capsys):
    pod, lap = tmp_path / "pod.jsonl", tmp_path / "laptop.jsonl"
    pod.write_text("".join(json.dumps(r) + "\n" for r in _pod_rows()), encoding="utf-8")
    lap.write_text("".join(json.dumps(r) + "\n" for r in _laptop_rows()) + '{"t": 1, "kind"', encoding="utf-8")  # 끊긴 줄
    out = tmp_path / "s" / "summary.json"
    assert lm.main(["summarize", "--run", f"tunnel={pod},{lap}", "--run", f"lte={pod},{lap}", "--out", str(out)]) == 0
    doc = json.loads(out.read_text(encoding="utf-8"))
    assert [r["label"] for r in doc["runs"]] == ["tunnel", "lte"] and "제품 성능 지표가 아니다" in doc["note"]
    table = out.with_suffix(".md").read_text(encoding="utf-8")
    assert table.count("\n| tunnel |") == 1 and "| lte |" in capsys.readouterr().out
    assert lm.main(["summarize", "--run", f"tunnel={pod}"]) == 2
