"""python -m services.ppe: PPE 프로세스(명세 5.5, S15P31A101-507). 기본 서비스 목록에는 없고 ppe 를 명시할 때만 돈다.

simai-bootstrap 이 띄울 때 넣는 환경:
  SIMAI_PPE_WEIGHTS                 체크포인트 경로(가중치 목록 ppe 묶음, bootstrap 이 확인한 파일)
  SIMAI_PPE_WEIGHTS_SHA256          그 sha256(불러오기 전에 다시 확인한다)
  SIMAI_PPE_GPU_MEMORY_FRACTION     이 프로세스의 torch 할당 상한(기본 0.05, 3.2)
  SIMAI_TIER0_MARKER                Tier 0 표시 파일(접수 문, 3.2)
  SIMAI_AVATAR_CHANNEL              입력 공유 파일(없으면 /dev/shm/simai-avatar-input.npz)
  SIMAI_PPE_OUTBOX                  Tier 0 가 대신 보낼 메시지 폴더(없으면 /dev/shm/simai-ppe-outbox)
  SIMAI_TIER0_ID                    봉투 source_id(없으면 tier0-01, Tier 0 와 같다)
  SIMAI_PPE_MODEL_VERSION           ppe_state.model_version(없으면 아래 기본값)
  SIMAI_PPE_ALIVE                   생존 파일(없으면 $SIMAI_ROOT/state/ppe.alive). 주기를 정상으로 끝낼 때 1초마다 시각을 적고
                                    감시 프로세스가 오래되면 다시 띄운다(S15P31A101-634, alive.py)
PPE 가 죽거나 멈춰도 Tier 0 는 계속 돈다(5.5). 종료 코드: 0 정상(SIGTERM), 2 설정·가중치 오류.
"""

from __future__ import annotations

import logging
import os
import signal
import threading
import time
from dataclasses import replace
from pathlib import Path

from simai_ai.gpu.admission import AdmissionGate
from simai_ai.gpu.budget import WorkerGpuSettings
from simai_ai.gpu.reserve import FAILED, cap_process_memory
from simai_ai.io.avatar_channel import LatestChannelReader, avatar_channel_path
from simai_ai.io.ppe_outbox import PpeOutboxWriter, ppe_outbox_path
from simai_ai.ppe.detector import PpeDetector

from .alive import AliveWriter, alive_path
from .loop import PpeLoop

DEFAULT_MODEL_VERSION = "T1-v4-ALL-seed20261021-e08-e373a1048057"
DEFAULT_TIER0_ID = "tier0-01"
DEFAULT_GPU_FRACTION = 0.05
STEP_S = 0.05
REPORT_S = 60.0


def main() -> int:
    logging.basicConfig(level=logging.INFO)
    env = os.environ
    stream_epoch = time.time_ns() // 1_000_000  # 프로세스 시작 시각. 재시작하면 더 크다(ppe_state.stream_epoch)
    root = Path(env.get("SIMAI_ROOT") or "/workspace/simai")
    stop = threading.Event()
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda *_: stop.set())
    try:
        weights = env.get("SIMAI_PPE_WEIGHTS", "").strip()
        sha256 = env.get("SIMAI_PPE_WEIGHTS_SHA256", "").strip()
        if not weights or not sha256:
            raise ValueError("SIMAI_PPE_WEIGHTS·SIMAI_PPE_WEIGHTS_SHA256 필요(simai-bootstrap 이 ppe 묶음을 확인한 뒤 넣는다)")
        fraction = float(env.get("SIMAI_PPE_GPU_MEMORY_FRACTION") or DEFAULT_GPU_FRACTION)
        if not 0 < fraction <= 1:
            raise ValueError("SIMAI_PPE_GPU_MEMORY_FRACTION 은 0 초과 1 이하다")
        settings = WorkerGpuSettings.from_env(env)
        # Tier 0 지연 접수 규칙(3.2): 지연을 모르거나 넘으면 새 추론을 멈추고 안정 뒤 재개. 표시 파일이 없으면 모름으로 본다
        policy = replace(settings.policy, allow_without_marker=False)
        gate = AdmissionGate(settings.marker, policy, wall_s=time.time, monotonic=time.monotonic)
        if cap_process_memory(fraction).state == FAILED:
            raise ValueError("PPE GPU 상한 설정 실패")
        detector = PpeDetector(Path(weights), sha256=sha256)
    except (ValueError, OSError) as e:
        logging.error("PPE 시작 실패: %s", e)
        return 2
    loop = PpeLoop(
        LatestChannelReader(avatar_channel_path(env, root), require_publication=True),
        gate,
        detector,
        PpeOutboxWriter(ppe_outbox_path(env, root)),
        source_id=(env.get("SIMAI_TIER0_ID") or DEFAULT_TIER0_ID).strip(),
        stream_epoch=stream_epoch,
        model_version=env.get("SIMAI_PPE_MODEL_VERSION") or DEFAULT_MODEL_VERSION,
        checkpoint_sha256=detector.checkpoint_sha256,
        clock_ms=lambda: time.time_ns() // 1_000_000,
        monotonic_ms=lambda: time.monotonic_ns() // 1_000_000,
    )
    logging.info("PPE 시작: stream_epoch=%d model=%s gpu_fraction=%.2f", stream_epoch, loop.model_version, fraction)
    report_at = time.monotonic() + REPORT_S
    alive = AliveWriter(alive_path(env, root))
    while not stop.is_set():
        try:
            loop.step()
        except Exception:  # noqa: BLE001 - 한 주기 실패로 프로세스를 내리지 않는다(다음 프레임으로 계속)
            loop.counts["step_failed"] += 1
            logging.exception("PPE 주기 실패")
        else:
            # 정상 주기만 생존으로 친다. 실패가 이어지거나 step 이 멈추면 값이 낡아 감시 프로세스가 다시 띄운다(-634)
            alive.beat(time.time_ns() // 1_000_000)
        if time.monotonic() >= report_at:
            logging.info("PPE 상태: seq=%d counts=%s gate=%s timing=%s", loop.state_sequence,
                         dict(loop.counts), gate.stats(), loop.diagnostics())
            report_at = time.monotonic() + REPORT_S
        stop.wait(STEP_S)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
