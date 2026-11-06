#!/usr/bin/env python3
"""Risk Engine 컨테이너 왕복 점검 (S15P31A101-99, 명세 12.2, 부록 D-G3-04).

계약 예시 요청(정상·생성 위치·미등록 규칙·읽지 못하는 본문)을 보내 계약 예시 결과와 같은지 본다(봉투 msg_id·sent_at 과
computed_at 은 비교하지 않는다). 두 방식이 같은 사례·같은 비교를 쓴다.

    python risk_plan_roundtrip.py --url http://127.0.0.1:8080 --examples contracts/examples     # 컨테이너 API(HTTP)
    python risk_plan_roundtrip.py --direct --examples contracts/examples                        # 라이브러리 직접(ai/ 에서, Tier 0 이미지)

`--dump <파일>` 은 비교에 쓴 응답(휘발 필드 뺀 것)을 JSON 으로 남긴다. 두 이미지의 덤프가 같으면 D-G3-04 의 PLANNED 판정이 같다.
HTTP 방식은 표준 라이브러리만 쓴다(컨테이너 안에서 docker exec 로 돈다). 모두 같으면 종료 코드 0, 하나라도 다르면 1.
"""

from __future__ import annotations

import argparse
import copy
import json
import sys
import urllib.error
import urllib.request
from collections.abc import Callable
from pathlib import Path

PLAN_PATH = "/v1/plan/evaluate"


def _load(examples: Path, name: str) -> dict:
    return json.loads((examples / name).read_text(encoding="utf-8"))


def cases(examples: Path) -> list[tuple[str, dict, dict, int]]:
    """(이름, 요청, 기대 결과, HTTP 상태)."""
    unknown = _load(examples, "risk_plan_request.json")
    unknown["payload"]["rules"]["rule_versions"] = [{"rule_id": "R8", "rule_version": 1}]
    unreadable = _load(examples, "risk_plan_request.json")
    unreadable["payload"].pop("request_id")
    empty = _load(examples, "risk_plan_request.generated_positions.json")
    empty["payload"]["layout"]["work_positions"] = []
    return [
        ("requested", _load(examples, "risk_plan_request.json"), _load(examples, "risk_plan_result.json"), 200),
        (
            "generated",
            _load(examples, "risk_plan_request.generated_positions.json"),
            _load(examples, "risk_plan_result.generated_positions.json"),
            200,
        ),
        ("generated_empty_array", empty, _load(examples, "risk_plan_result.generated_positions.json"), 200),
        ("unknown_rule", unknown, _load(examples, "risk_plan_result.unknown_rule.json"), 422),
        ("unreadable_request", unreadable, _load(examples, "risk_plan_result.unreadable_request.json"), 400),
    ]


def strip(doc: dict) -> dict:
    doc = copy.deepcopy(doc)
    doc.pop("msg_id", None)
    doc.pop("sent_at", None)
    if isinstance(doc.get("payload"), dict):
        doc["payload"].pop("computed_at", None)
    return doc


def http_sender(base: str, timeout_s: float = 10.0) -> Callable[[bytes], tuple[int, dict]]:
    def send(body: bytes) -> tuple[int, dict]:
        req = urllib.request.Request(
            base.rstrip("/") + PLAN_PATH, data=body, method="POST", headers={"Content-Type": "application/json"}
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout_s) as r:  # noqa: S310 - 점검 대상 주소(내부)
                return r.status, json.loads(r.read())
        except urllib.error.HTTPError as e:  # 4xx·5xx 도 계약 본문이 있다
            return e.code, json.loads(e.read())

    return send


def direct_sender() -> Callable[[bytes], tuple[int, dict]]:
    # services 는 설치하지 않으므로 ai/ 작업 디렉터리(또는 이미지 /opt/simai/ai)에서 실행하고 그 위치를 import 경로에 넣는다
    if str(Path.cwd()) not in sys.path:
        sys.path.insert(0, str(Path.cwd()))
    from services.risk_engine.handler import handle

    def send(body: bytes) -> tuple[int, dict]:
        status, content = handle(body)
        return status, json.loads(content)

    return send


def run(send: Callable[[bytes], tuple[int, dict]], examples: Path) -> tuple[list[str], dict]:
    failures: list[str] = []
    dump: dict = {}
    for name, request, expected, status in cases(examples):
        code, doc = send(json.dumps(request).encode("utf-8"))
        got = strip(doc)
        dump[name] = {"status": code, "body": got}
        if code != status:
            failures.append(f"{name}: HTTP {code} (기대 {status})")
        if got != strip(expected):
            failures.append(f"{name}: 본문이 계약 예시와 다르다: {json.dumps(got, ensure_ascii=False)[:600]}")
    return failures, dump


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Risk Engine 컨테이너 계약 예시 왕복 점검")
    how = parser.add_mutually_exclusive_group(required=True)
    how.add_argument("--url", help="컨테이너 API 주소 (예: http://127.0.0.1:8080)")
    how.add_argument("--direct", action="store_true", help="HTTP 없이 services.risk_engine.handler 를 직접 부른다")
    parser.add_argument("--examples", type=Path, required=True, help="contracts/examples 폴더")
    parser.add_argument("--dump", type=Path, help="비교에 쓴 응답을 JSON 으로 쓸 파일")
    args = parser.parse_args(argv)

    send = direct_sender() if args.direct else http_sender(args.url)
    failures, dump = run(send, args.examples)
    if args.dump:
        args.dump.write_text(json.dumps(dump, ensure_ascii=False, indent=1, sort_keys=True), encoding="utf-8")
    for name in dump:
        bad = [f for f in failures if f.startswith(name + ":")]
        print(f"{'FAIL' if bad else 'PASS'}  {name} (HTTP {dump[name]['status']})")
        for f in bad:
            print(f"      {f}")
    print(f"risk_plan_roundtrip: {len(dump) - len({f.split(':')[0] for f in failures})}/{len(dump)} 같음")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
