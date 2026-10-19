"""Windows WinError 10014 일시 오류 보정 시험 (S15P31A101-198, tests/winsock_retry/sitecustomize.py).

가짜 오류 주입으로 본다: 10014 만 다시 시도하고(정해진 횟수까지, 넘으면 그대로 올림) 다른 오류는 바로 올린다. 시험이 띄우는 자식
Python 프로세스에도 걸린다. 실제 Winsock 의 일시 오류는 재현 시각을 고를 수 없어 여기서 기다리지 않는다.
"""

from __future__ import annotations

import importlib.util
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

SHIM = Path(__file__).with_name("winsock_retry") / "sitecustomize.py"


@pytest.fixture
def shim(monkeypatch):
    spec = importlib.util.spec_from_file_location("winsock_retry_under_test", SHIM)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module.sleeps = []
    monkeypatch.setattr(module, "time", SimpleNamespace(sleep=module.sleeps.append))  # 가짜 시계: 기다리지 않고 센다
    return module


def flaky(errors):
    """errors 를 차례로 올리고, 다 쓰면 'ok' 를 돌려주는 함수와 호출 횟수."""
    calls = []

    def fn():
        calls.append(1)
        if len(calls) <= len(errors):
            raise errors[len(calls) - 1]
        return "ok"

    return fn, calls


def wsa(code: int) -> OSError:
    e = OSError(f"WinError {code}")
    e.winerror = code
    return e


def test_transient_wsaefault_is_retried_until_it_succeeds(shim):
    fn, calls = flaky([wsa(10014)] * (shim.TRIES - 1))
    assert shim._retrying(fn)() == "ok" and len(calls) == shim.TRIES


def test_wsaefault_that_persists_is_raised_not_hidden(shim):
    """반례(경계): TRIES 번 모두 10014 면 마지막 오류를 그대로 올린다(무한히 기다리거나 성공으로 바꾸지 않는다)."""
    fn, calls = flaky([wsa(10014)] * shim.TRIES)
    with pytest.raises(OSError) as info:
        shim._retrying(fn)()
    assert info.value.winerror == 10014 and len(calls) == shim.TRIES
    assert len(shim.sleeps) == shim.TRIES - 1 and sum(shim.sleeps) < 2.0  # 기다림은 2 초 안으로 묶인다


@pytest.mark.parametrize("error", [wsa(10048), wsa(10061), ConnectionRefusedError("refused"), TimeoutError("t")])
def test_other_errors_are_raised_at_once(shim, error):
    """반례: 주소 사용 중(10048)·연결 거부(10061)·시간 초과 등 실제 상태를 알리는 오류는 다시 시도하지 않는다."""
    fn, calls = flaky([error])
    with pytest.raises(type(error)):
        shim._retrying(fn)()
    assert len(calls) == 1


def test_install_is_a_no_op_off_windows(shim, monkeypatch):
    import socket

    raw = getattr(socket.socket.listen, "__wrapped__", socket.socket.listen)
    monkeypatch.setattr(socket.socket, "listen", raw)
    monkeypatch.setattr(sys, "platform", "linux")
    shim.install()
    assert socket.socket.listen is raw


@pytest.mark.skipif(sys.platform != "win32", reason="Windows 에서만 건다")
def test_test_process_and_child_python_processes_have_the_shim():
    import socket

    assert socket.socket.listen._simai_winsock_retry and socket.socketpair._simai_winsock_retry
    code = "import socket; print(bool(getattr(socket.socket.connect, '_simai_winsock_retry', False)))"
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, env=dict(os.environ), timeout=60)
    assert out.stdout.strip() == "True", out.stderr


@pytest.mark.skipif(sys.platform != "win32", reason="Windows 에서만 건다")
@pytest.mark.parametrize("body", ["import os; os.environ['R198_EXISTING_SITE'] = 'loaded'",
                                  "import os; os.environ['R198_EXISTING_SITE'] = 'loaded'; raise RuntimeError('orig-boom')"])
def test_child_still_runs_the_existing_sitecustomize_behind_the_shim(tmp_path, body):
    """반례(리뷰 필수 2): PYTHONPATH 뒤쪽의 원래 sitecustomize 도 실행되고(부작용·예외 알림 그대로) 보정도 걸린다."""
    (tmp_path / "sitecustomize.py").write_text(body + "\n", encoding="utf-8")
    code = ("import os, socket, sys; print(os.environ.get('R198_EXISTING_SITE', 'MISSING'), "
            "bool(getattr(socket.socket.listen, '_simai_winsock_retry', False)), "
            "getattr(sys.modules.get('sitecustomize'), '__file__', 'NONE'))")
    env = dict(os.environ, PYTHONPATH=os.pathsep.join([str(SHIM.parent), str(tmp_path)]))
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, env=env, timeout=60)
    loaded, shimmed, origin = out.stdout.split()
    assert (loaded, shimmed) == ("loaded", "True"), out
    if "raise" in body:  # 원래처럼 실패한 import 는 남지 않고 site 가 오류를 알린다
        assert origin == "NONE" and "Error in sitecustomize" in out.stderr and "orig-boom" in out.stderr, out
    else:
        assert Path(origin).parent == tmp_path and out.stderr == "", out


def test_retry_wait_budget_is_bounded_even_with_a_short_socket_timeout(shim):
    """권장 1: 재시도 기다림은 소켓 시간 제한을 보지 않는다(별도 예산). 그 예산의 상한을 고정해 둔다: 10014 가 끝까지 이어져도
    가짜 시계 기준 합계 2 s 안, 한 번 기다림 0.1 s 이하."""
    fn, _ = flaky([wsa(10014)] * shim.TRIES)
    with pytest.raises(OSError):
        shim._retrying(fn)()
    assert max(shim.sleeps) <= 0.1 and sum(shim.sleeps) < 2.0
