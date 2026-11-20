"""vLLM 이 준비되기 전에는 VLM 작업 유형을 받지 않는다 (S15P31A101-577).

통합 이미지의 감시 프로세스(simai-bootstrap)는 분석 워커를 Tier 0 준비 직후 바로 띄우고, VLM(Cosmos3-Nano 31.5GB)은 뒤에서 받아 vLLM 을 띄운다.
그 사이에 vLLM 이 필요한 작업을 받으면 VLM_UNAVAILABLE 로 실패해 시도 횟수를 쓴다. 그래서 감시 프로세스가 SIMAI_WORKER_VLM_GATE=health 를 주면
대여 요청 직전마다 vLLM /health 를 보고(ttl_s 동안 결과를 다시 쓴다) 200 이 아니면 그 유형을 대여 요청 목록에서 뺀다. EC2 는 작업을 대기열에 둔다.
vlm_zone_draft 는 VLM 없이도 기하 후보를 내므로 미루지 않는다. 준비/미준비가 바뀔 때만 이유를 로그에 남긴다.
"""

from __future__ import annotations

import logging
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Mapping

log = logging.getLogger(__name__)

VLM_TYPES = frozenset({"vlm_rationale", "incident_summary"})  # vLLM 이 없으면 실패하는 유형
ENV = "SIMAI_WORKER_VLM_GATE"


def _probe(url: str) -> tuple[bool, str]:
    try:
        with urllib.request.urlopen(url, timeout=2) as r:  # 127.0.0.1 의 vLLM 만
            return (r.status == 200), f"HTTP {r.status}"
    except urllib.error.HTTPError as e:
        return False, f"HTTP {e.code}"
    except (urllib.error.URLError, OSError) as e:
        return False, f"{type(e).__name__}: {getattr(e, 'reason', e)}"


class VlmHealthGate:
    def __init__(self, url: str, *, probe: Callable[[str], tuple[bool, str]] = _probe,
                 monotonic: Callable[[], float] = time.monotonic, ttl_s: float = 5.0) -> None:
        self.url, self._probe, self._monotonic, self.ttl_s = url, probe, monotonic, ttl_s
        self._checked: float | None = None
        self.ready = False
        self.reason = "아직 확인하지 않음"
        self._logged: bool | None = None

    def _check(self) -> bool:
        now = self._monotonic()
        if self._checked is None or now - self._checked >= self.ttl_s:
            self._checked = now
            self.ready, why = self._probe(self.url)
            self.reason = f"vLLM {self.url} {why}"
            if self.ready != self._logged:
                self._logged = self.ready
                if self.ready:
                    log.info("VLM 준비됨(%s) → %s 접수", self.reason, ", ".join(sorted(VLM_TYPES)))
                else:
                    log.warning("VLM 미준비(%s) → %s 접수 보류(다른 유형은 그대로 받는다)", self.reason, ", ".join(sorted(VLM_TYPES)))
        return self.ready

    def __call__(self, types: list[str]) -> list[str]:
        if not VLM_TYPES & set(types) or self._check():
            return types
        return [t for t in types if t not in VLM_TYPES]


class VlmDisabledGate:
    """의도적으로 끈 VLM 작업은 대여하지 않는다."""

    def __call__(self, types: list[str]) -> list[str]:
        return [t for t in types if t not in VLM_TYPES]


def from_env(env: Mapping[str, str]) -> VlmHealthGate | VlmDisabledGate | None:
    """SIMAI_WORKER_VLM_GATE: 비었거나 0 이면 없음(지금까지와 같다), health 면 127.0.0.1:SIMAI_VLLM_PORT/health 를 본다."""
    mode = (env.get(ENV) or "").strip()
    if mode in ("", "0"):
        return None
    if mode == "off":
        return VlmDisabledGate()
    if mode != "health":
        raise ValueError(f"{ENV} 는 health, off 또는 0 이다: {mode!r}")
    port = int(env.get("SIMAI_VLLM_PORT") or 8000)
    if not 0 < port < 65536:
        raise ValueError(f"SIMAI_VLLM_PORT 범위 밖: {port}")
    return VlmHealthGate(f"http://127.0.0.1:{port}/health")
