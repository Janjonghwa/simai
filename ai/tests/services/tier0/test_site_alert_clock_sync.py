"""운영 현장 경보 단말(gateway/site_alert)과 실제 Tier 0 서비스의 시계 차이 교환 왕복 (명세 3.9, S15P31A101-289).

노트북 흉내(tier0_site.Laptop) 대신 제품 단말(SiteAlertApp + WebSocketControlChannel + SiteLinkMonitor)을 실제 Tier0Service
I0 제어 서버(wss, 기기 토큰)에 붙인다. 단말이 clock_sync_request 에 답하면 Tier 0 가 노트북–Pod 시계 차이를 알게 되어
health 의 laptop_clock_offset·pipeline_lag_ms 가 null 이 아니게 된다. 답하지 않을 때(null)는 test_tier0_service 의
test_unknown_clock_offset_keeps_health_unsafe_and_blocks_resolution 이 본다.

site_alert 는 ai/ 와 따로인 uv 프로젝트라 설치하지 않고 소스 경로(gateway/site_alert/src)를 import 경로에 넣는다(실행 의존성은
websockets 하나로 ai tier0 과 같다).
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
AI_DIR = HERE.parents[2]
SITE_ALERT_SRC = AI_DIR.parent / "gateway" / "site_alert" / "src"
for p in (AI_DIR, HERE, SITE_ALERT_SRC):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

import tier0_site as ts  # noqa: E402
from test_tier0_service import _now, _settings, control, sim_run  # noqa: E402,F401 - 픽스처 재사용

from services.tier0.service import Tier0Service  # noqa: E402
from simai_ai.providers.contract import schema_errors  # noqa: E402
from simai_site_alert.app import SiteAlertApp  # noqa: E402
from simai_site_alert.core import SiteAlertCore  # noqa: E402
from simai_site_alert.link_status import SiteLinkMonitor  # noqa: E402
from simai_site_alert.sound import FakeSoundBackend  # noqa: E402
from simai_site_alert.transport import WebSocketControlChannel  # noqa: E402


def test_site_alert_answers_clock_sync_so_health_lag_is_known(tmp_path, sim_run, control) -> None:  # noqa: F811
    settings = _settings(tmp_path, sim_run, anchor_ms=_now() + 300, env=control["env"])
    service = Tier0Service(settings)
    core = SiteAlertCore(FakeSoundBackend(), laptop_id="laptop-01")
    channel = WebSocketControlChannel(f"wss://127.0.0.1:{control['port']}/control", device_token=ts.LAPTOP_TOKEN,
                                      ca_file=str(control["cert"]), reconnect_min_s=0.1, reconnect_max_s=0.5)
    app = SiteAlertApp(core, channel, monitor=SiteLinkMonitor())
    healths: list[dict] = []
    product_on_message = app._on_message  # noqa: SLF001 - 시험: 단말이 받은 health 를 제품 처리 앞에서 기록만 한다

    def record(raw, received_mono):
        msg = json.loads(raw)
        if msg.get("msg_type") == "health":
            healths.append(msg)
        product_on_message(raw, received_mono)

    app._on_message = record  # noqa: SLF001
    service.start()
    app.start(test_alert_s=None)
    try:
        # 단말 응답이 표본이 되어 Tier 0 가 시계 차이를 안다
        assert ts.wait_until(lambda: service.clock_sync.capture_clock(True) is not None, timeout=15)
        assert ts.wait_until(lambda: any(h["payload"]["pipeline_lag_ms"] is not None for h in healths), timeout=15)
        known = [h for h in healths if h["payload"]["pipeline_lag_ms"] is not None]
        for h in known:
            p = h["payload"]
            assert p["laptop_clock_offset"] is not None and p["gateway_link"] == "CONNECTED"
            assert schema_errors(h, "health") == []
        assert core.rejected == {}  # 단말이 거부한 Tier 0 메시지가 없다(clock_sync_request 포함)
    finally:
        app.close()
        service.stop()
