"""PPE 생존 파일(S15P31A101-634).

PPE 메인 루프는 한 주기 실패로 프로세스를 내리지 않으므로(다음 프레임으로 계속), 살아 있는 채 매 주기 실패하거나 한 주기에서 멈추면
상태를 보내지 않는데도 감시 프로세스(simai-bootstrap)는 프로세스가 끝나지 않아 알 수 없었다. 그러면 R9·R11 이 조용히 나지 않는다.
그래서 주기를 **정상으로 끝낼 때만** 이 파일에 지금 시각(UTC ms)을 적고, 감시 프로세스가 이 값이 오래되면 다시 띄운다.

파일 내용은 정수 하나(UTC ms)다. 같은 폴더의 임시 파일에 쓴 뒤 바꿔 넣어(os.replace) 읽는 쪽이 반쯤 쓴 값을 보지 않게 한다.
매 주기(0.05 s)마다 쓰지 않고 [period_ms] 마다 한 번 쓴다.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

ALIVE_ENV = "SIMAI_PPE_ALIVE"
DEFAULT_PERIOD_MS = 1_000

log = logging.getLogger(__name__)


def alive_path(env: os._Environ[str] | dict[str, str], root: Path) -> Path:
    """감시 프로세스가 넣은 경로. 없으면 $SIMAI_ROOT/state/ppe.alive(감시 프로세스의 기본값과 같다)"""
    value = (env.get(ALIVE_ENV) or "").strip()
    return Path(value) if value else root / "state" / "ppe.alive"


class AliveWriter:
    """정상 주기 뒤 [beat] 를 부른다. 쓰기 실패는 PPE 를 멈추지 않는다(감시 프로세스가 오래된 값으로 보고 다시 띄운다)."""

    def __init__(self, path: Path, *, period_ms: int = DEFAULT_PERIOD_MS) -> None:
        self.path = path
        self.period_ms = period_ms
        self._last_ms: int | None = None
        self.failed = 0

    def beat(self, now_ms: int) -> bool:
        """[period_ms] 가 지났으면 [now_ms] 를 쓴다. 썼으면 True"""
        if self._last_ms is not None and now_ms - self._last_ms < self.period_ms:
            return False
        tmp = self.path.with_name(self.path.name + ".tmp")
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp.write_text(str(now_ms), encoding="ascii")
            os.replace(tmp, self.path)
        except OSError as e:
            self.failed += 1
            log.warning("PPE 생존 파일을 쓰지 못했다(%s): %s", self.path, e)
            return False
        self._last_ms = now_ms
        return True


def read_alive_ms(path: Path) -> int | None:
    """생존 파일의 시각(UTC ms). 없거나 읽지 못하거나 정수가 아니면 None"""
    try:
        return int(path.read_text(encoding="ascii").strip())
    except (OSError, ValueError):
        return None
