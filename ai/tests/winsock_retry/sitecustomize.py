"""시험 환경 보정: Windows 의 WinError 10014(WSAEFAULT) 일시 오류만 다시 시도한다 (S15P31A101-198).

이 노트북의 Winsock 은 포인터 인수가 없는 listen() 까지 가끔 WSAEFAULT 로 실패시킨다. 시험·제품 코드 없이 순수 소켓 반복만으로
재현된다(유휴 때 listen 0.1~1%, 몰릴 때 수십 %, 같은 소켓으로 바로 다시 하면 된다). 그래서 TLS·소켓·e2e 시험이 묶음마다 다른
곳(시험 서버 listen, asyncio 이벤트 루프의 socketpair, Tier 0 자식 프로세스의 I0 listen, I1 connect)에서 1~2개씩 실패했다.

tests/conftest.py 가 시험 프로세스에서 install() 하고, 이 디렉터리를 PYTHONPATH 앞에 넣어 시험이 띄우는 자식 Python 프로세스도
시작할 때(sitecustomize) 같은 보정을 건다. 자식에서는 이 파일이 PYTHONPATH 뒤쪽·site-packages 의 원래 sitecustomize 를 가리므로,
보정을 건 뒤 이 폴더를 뺀 sys.path 에서 원래 sitecustomize 를 찾아 그 이름으로 실행한다(부작용·예외 그대로). Windows 가 아니거나
다른 오류는 그대로 올린다. 제품 코드는 바꾸지 않는다.
"""

from __future__ import annotations

import os
import socket
import sys
import time

WSAEFAULT = 10014
TRIES = 20  # 오류가 몰려 이어지기도 한다(소켓을 빠르게 여닫는 탐침에서 5회·10회로도 2000회 중 수 회 남음). 모두 합쳐 약 1.5 s


def _retrying(fn):
    def call(*args, **kwargs):
        for attempt in range(TRIES):
            try:
                return fn(*args, **kwargs)
            except OSError as e:
                if getattr(e, "winerror", None) != WSAEFAULT or attempt == TRIES - 1:
                    raise
                time.sleep(min(0.01 * (attempt + 1), 0.1))
        raise AssertionError("도달하지 않는다")

    call.__wrapped__ = fn
    call._simai_winsock_retry = True
    return call


def install() -> None:
    if sys.platform != "win32" or getattr(socket.socket.listen, "_simai_winsock_retry", False):
        return
    socket.socket.listen = _retrying(socket.socket.listen)
    socket.socket.connect = _retrying(socket.socket.connect)
    socket.socketpair = _retrying(socket.socketpair)  # 새 소켓 쌍으로 다시(창 안의 비차단 connect 를 되풀이하지 않게)


def _run_shadowed_sitecustomize() -> None:
    """자식에서 'sitecustomize' 로 불렸을 때만: 이 폴더 밖의 원래 sitecustomize 를 찾아 실행하고 sys.modules 에 둔다."""
    if __name__ != "sitecustomize":
        return
    import importlib.machinery
    import importlib.util

    here = os.path.normcase(os.path.dirname(os.path.abspath(__file__)))
    path = [p for p in sys.path if os.path.normcase(os.path.abspath(p or os.curdir)) != here]
    spec = importlib.machinery.PathFinder.find_spec("sitecustomize", path)
    if spec is None or spec.loader is None:
        return
    module = importlib.util.module_from_spec(spec)
    sys.modules["sitecustomize"] = module
    spec.loader.exec_module(module)  # 예외는 그대로 site 로 간다(원래와 같이 'Error in sitecustomize' 로 알림)


install()
_run_shadowed_sitecustomize()
