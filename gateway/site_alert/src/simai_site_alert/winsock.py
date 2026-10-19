"""Windows WinError 10014(WSAEFAULT) 일시 오류만 짧게 다시 시도한다 (S15P31A101-199).

이 노트북의 Winsock 은 bind·listen·connect·socketpair 가 제품 코드와 상관없이 가끔 10014 로 실패하고, 새로 하면 바로 된다
(S15P31A101-198 측정, 원인 후보 보안 프로그램 V3·미확인). 관제 경로(영상 중계의 Tier 0 연결, 현장 경보 단말의 제어 채널 루프·
중계 상태 포트)는 호출 지점에서 이 함수로 명시적으로 감싼다. 전역 socket 교체(몽키패치)는 하지 않는다.

- Windows 가 아니거나 10014 가 아닌 오류(10048 주소 사용 중, 10061 거부, 시간 초과 등)는 그대로 바로 올린다.
- 예산(BUDGET_S, 한 번 기다림 최대 MAX_WAIT_S)을 넘으면 마지막 10014 를 그대로 올린다. 그다음은 호출부의 기존
  끊김·재접속·상태 표시 경로다(조용히 성공으로 바꾸지 않는다).
- 예산 근거: 하트비트·중계 상태 보고 1 s 주기, 누락 판정 3 s, 단말 표시 tick 0.5 s, 영상 송신 큐 25 프레임(25 fps 1 s),
  기존 재접속 첫 대기 0.5 s. 0.3 s 는 그 어느 것보다 짧아 다시 시도하는 동안 판정·표시·큐가 바뀌지 않는다.
- fn 은 매번 새 소켓으로 처음부터 하는 함수여야 한다(실패한 소켓의 connect 를 되풀이하지 않는다).
"""

from __future__ import annotations

import logging
import sys
import time
from collections.abc import Callable
from typing import TypeVar

log = logging.getLogger(__name__)

WSAEFAULT = 10014
BUDGET_S = 0.3
MAX_WAIT_S = 0.05

T = TypeVar("T")


def retry_wsaefault(fn: Callable[[], T], *, what: str) -> T:
    """fn() 을 부른다. Windows 의 10014 면 예산 안에서 다시 부른다. what 은 로그에 쓰는 경로 이름(비밀 값을 넣지 않는다)."""
    if sys.platform != "win32":
        return fn()
    deadline = time.monotonic() + BUDGET_S
    failures = 0
    while True:
        try:
            result = fn()
        except OSError as e:
            if getattr(e, "winerror", None) != WSAEFAULT:
                raise
            failures += 1
            wait = min(0.01 * failures, MAX_WAIT_S)
            if time.monotonic() + wait > deadline:
                log.warning("%s: WinError 10014 가 %d번 이어져 다시 시도를 멈춘다(기존 재접속·상태 표시로 넘긴다)",
                            what, failures)
                raise
            time.sleep(wait)
            continue
        if failures:
            log.info("%s: WinError 10014 를 %d번 다시 시도해 성공했다", what, failures)
        return result
