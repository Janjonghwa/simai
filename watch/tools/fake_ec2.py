# /// script
# requires-python = ">=3.11"
# dependencies = ["websockets>=15,<18", "jsonschema>=4.23", "referencing>=0.35"]
# ///
"""가짜 EC2: 워치 앱의 I4 연결을 EC2 없이 시험한다(S15P31A101-337). 서버 쪽 구현(-336·-338)이 아니다.

- ws://<PC>:8765/ws/watch 에서 듣고 Authorization: Bearer <토큰> 이 --token 과 같을 때만 받는다(아니면 401)
- 워치가 보내는 메시지(watch_heartbeat·watch_ack·gait_features)를 contracts/ 스키마로 검사하고 찍는다
- 워치 하트비트에는 EC2 하트비트(echo·interval_ms)로 바로 답한다
- 표준 입력으로 알림을 보낸다:  alert CRITICAL 추락 FRONT   /  alert HIGH 지게차  /  alert CAUTION 접근 - 10000
  (등급, 한 단어, 방향(FRONT·BACK·LEFT·RIGHT, 없으면 -), 확인 기한 ms). resend 는 마지막 알림을 같은 msg_id 로 다시 보낸다
- watch_ack 가 오면 VIBRATED 까지 걸린 시간(워치 received_at − 보낸 sent_at, 시계 차이 포함)을 찍는다
- gait_features 는 창마다 걸음 수·RMS·최대 주파수·전환을 찍고, --gait-log <파일> 을 주면 CSV 로도 남긴다(걷기·멈춤 기록, -339)

실행(저장소 루트에서):  uv run watch/tools/fake_ec2.py --token dev-token
워치 설정(debug 빌드만 http 를 받는다): adb shell "am start -n com.ssafy.simai.watch/.ConfigActivity \
  --es ec2_base_url 'http://<PC IP>:8765' --es device_id watch-01 --es device_token 'dev-token'"
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
import uuid
from http import HTTPStatus
from pathlib import Path

from jsonschema import Draft202012Validator
from referencing import Registry, Resource
from websockets.asyncio.server import serve
from websockets.exceptions import ConnectionClosed

CONTRACTS = Path(__file__).resolve().parents[2] / "contracts"
PATH = "/ws/watch"


def load_validators() -> dict[str, Draft202012Validator]:
    registry = Registry()
    schemas: dict[str, dict] = {}
    for path in sorted(CONTRACTS.rglob("*.schema.json")):
        schema = json.loads(path.read_text(encoding="utf-8"))
        registry = registry.with_resource(schema["$id"], Resource.from_contents(schema))
        schemas[path.name.removesuffix(".schema.json")] = schema
    names = ("watch_alert", "watch_heartbeat", "watch_ack", "gait_features")
    return {name: Draft202012Validator(schemas[name], registry=registry) for name in names}


def now_ms() -> int:
    return int(time.time() * 1000)


def log(text: str) -> None:
    """시각을 붙여 찍는다(절전 중 하트비트 간격을 재는 데 쓴다)"""
    print(f"{time.strftime('%H:%M:%S')} {text}", flush=True)


def envelope(msg_type: str, payload: dict) -> dict:
    return {
        "schema_version": "1.0.0",
        "msg_type": msg_type,
        "msg_id": str(uuid.uuid4()),
        "sent_at": now_ms(),
        "source_id": "ec2",
        "payload": payload,
    }


def event_id() -> str:
    """uuid_v7 꼴(시각 기반). uuid 모듈에 v7 이 없는 판도 있어 직접 만든다."""
    ts = now_ms()
    rand = uuid.uuid4().hex
    variant = (int(rand[3], 16) & 0x3) | 0x8
    return f"{ts >> 16:08x}-{ts & 0xFFFF:04x}-7{rand[:3]}-{variant:x}{rand[4:7]}-{rand[7:19]}"


class FakeEc2:
    def __init__(self, token: str, interval_ms: int, gait_log: Path | None = None) -> None:
        self.token = token
        self.interval_ms = interval_ms
        self.gait_log = gait_log
        if gait_log is not None and not gait_log.exists():
            gait_log.write_text("received_at,device,window_start_ms,window_end_ms,clock_offset_ms,steps,rms,peak_freq_hz,transitions\n", encoding="utf-8")
        self.validators = load_validators()
        self.watches: dict[object, str] = {}
        self.sent_alerts: dict[str, dict] = {}
        self.last_alert: dict | None = None

    async def process_request(self, connection, request):
        if request.path != PATH:
            return connection.respond(HTTPStatus.NOT_FOUND, "not found\n")
        auth = request.headers.get("Authorization", "")
        if auth != f"Bearer {self.token}":
            log("연결 거부: 토큰이 다르다")
            return connection.respond(HTTPStatus.UNAUTHORIZED, "bad token\n")
        return None

    async def handler(self, connection):
        peer = connection.remote_address
        log(f"워치 연결 {peer}")
        try:
            async for text in connection:
                await self.on_text(connection, text)
        finally:
            device = self.watches.pop(connection, "?")
            log(f"워치 끊김 {device} {peer}")

    async def on_text(self, connection, text: str) -> None:
        try:
            doc = json.loads(text)
        except json.JSONDecodeError:
            log(f"JSON 아님: {text[:80]}")
            return
        msg_type = doc.get("msg_type")
        validator = self.validators.get(msg_type)
        if validator is None:
            log(f"모르는 msg_type: {msg_type}")
            return
        errors = [f"{'/'.join(map(str, e.absolute_path)) or '<root>'}: {e.message}" for e in validator.iter_errors(doc)]
        if errors:
            log(f"계약 위반 {msg_type}: {errors}")
            return
        self.watches[connection] = doc["source_id"]
        payload = doc["payload"]
        if msg_type == "watch_heartbeat":
            await self.reply_heartbeat(connection, doc)
        elif msg_type == "watch_ack":
            self.on_ack(doc["source_id"], payload)
        elif msg_type == "gait_features":
            self.on_gait(doc["source_id"], payload)

    def on_gait(self, device: str, payload: dict) -> None:
        steps = len(payload["step_times_ms"])
        band = payload["band_magnitude"]
        transitions = ";".join(f"{t['kind']}@{t['at_ms']}" for t in payload.get("motion_transitions", []))
        peak = band["peak_freq_hz"]
        log(
            f"gait {device} [{payload['window_start_ms']}~{payload['window_end_ms']}) 걸음 {steps} rms {band['rms']:.2f} "
            f"peak {peak if peak is not None else '-'} offset {payload['clock_offset_ms']} 지연 {now_ms() - payload['window_end_ms']}ms {transitions}"
        )
        if self.gait_log is not None:
            with self.gait_log.open("a", encoding="utf-8") as f:
                f.write(
                    f"{now_ms()},{device},{payload['window_start_ms']},{payload['window_end_ms']},{payload['clock_offset_ms']},"
                    f"{steps},{band['rms']},{'' if peak is None else peak},{transitions}\n"
                )

    async def reply_heartbeat(self, connection, doc: dict) -> None:
        payload = doc["payload"]
        received = now_ms()
        watch = payload["watch"]
        log(
            f"heartbeat {doc['source_id']} seq={payload['heartbeat_seq']} 배터리 {watch['battery_pct']}% {watch['transport']} 지연 {received - doc['sent_at']}ms",
            )
        reply = envelope(
            "watch_heartbeat",
            {
                "heartbeat_seq": payload["heartbeat_seq"],
                "echo": {"seq": payload["heartbeat_seq"], "received_at": received},
                "interval_ms": self.interval_ms,
            },
        )
        await connection.send(json.dumps(reply, ensure_ascii=False))

    def on_ack(self, device: str, payload: dict) -> None:
        alert = self.sent_alerts.get(payload["alert_msg_id"])
        latency = ""
        if alert is not None and payload.get("received_at") is not None:
            latency = f" 전달 {payload['received_at'] - alert['sent_at']}ms(시계 차이 포함)"
        known = "" if alert is not None else " (모르는 알림)"
        log(
            f"ack {device} {payload['kind']} event={payload['event_id'][-12:]} acked_at={payload['acked_at']}{latency}{known}",
            )

    async def send_alert(self, severity: str, word: str, direction: str | None, ack_within_ms: int, resend: bool = False) -> None:
        if resend:
            if self.last_alert is None:
                log("다시 보낼 알림이 없다")
                return
            msg = self.last_alert
        else:
            payload = {
                "event_id": event_id(),
                "event_sequence": 1,
                "severity": severity,
                "word": word,
                "ack_within_ms": ack_within_ms,
                "needs_check": False,
            }
            if direction:
                payload["direction"] = direction
            msg = envelope("watch_alert", payload)
            errors = list(self.validators["watch_alert"].iter_errors(msg))
            if errors:
                log(f"보낼 알림이 계약에 어긋난다: {[e.message for e in errors]}")
                return
            self.sent_alerts[msg["msg_id"]] = msg
            self.last_alert = msg
        if not self.watches:
            log("연결된 워치가 없다")
            return
        text = json.dumps(msg, ensure_ascii=False)
        for connection, device in list(self.watches.items()):
            try:
                await connection.send(text)
            except ConnectionClosed:
                # 끊긴 워치는 빼고 서버는 계속 돈다
                self.watches.pop(connection, None)
                log(f"alert → {device} 실패: 연결이 닫혔다")
                continue
            log(f"alert → {device} {msg['payload']['severity']} {msg['payload']['word']} msg_id={msg['msg_id'][-12:]}")

    async def read_commands(self) -> None:
        loop = asyncio.get_running_loop()
        log("명령: alert <CRITICAL|HIGH|CAUTION> <한 단어> [FRONT|BACK|LEFT|RIGHT|-] [확인 기한 ms] | resend | quit")
        while True:
            line = await loop.run_in_executor(None, sys.stdin.readline)
            if not line:
                return
            parts = line.split()
            if not parts:
                continue
            if parts[0] == "quit":
                return
            if parts[0] == "resend":
                await self.send_alert("", "", None, 0, resend=True)
            elif parts[0] == "alert" and len(parts) >= 3:
                direction = parts[3] if len(parts) > 3 and parts[3] != "-" else None
                ack_within = int(parts[4]) if len(parts) > 4 else 10000
                await self.send_alert(parts[1], parts[2], direction, ack_within)
            else:
                log("모르는 명령")


async def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--token", required=True, help="워치에 넣은 기기 토큰과 같은 값")
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--interval-ms", type=int, default=10000, help="워치에 알려 줄 하트비트 주기")
    ap.add_argument("--gait-log", type=Path, default=None, help="걸음 특징을 CSV 로 남길 파일")
    args = ap.parse_args()
    fake = FakeEc2(args.token, args.interval_ms, args.gait_log)
    # ping 으로 죽은 TCP 연결을 걸러 낸다(워치 쪽 OkHttp 는 pong 을 자동으로 보낸다)
    async with serve(fake.handler, "0.0.0.0", args.port, process_request=fake.process_request, ping_interval=20, ping_timeout=20):
        log(f"가짜 EC2 ws://0.0.0.0:{args.port}{PATH} (토큰 검사, 계약 검사)")
        await fake.read_commands()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
