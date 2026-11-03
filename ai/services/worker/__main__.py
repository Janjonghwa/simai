"""분석 워커 진입점: `python -m services.worker [--config <설정 파일>] [--once]`.

접속 정보는 환경 변수(.env)로 받는다. 토큰 값은 저장소에 두지 않는다.
- SIMAI_EC2_BASE_URL: EC2 주소 (https)
- SIMAI_WORKER_TOKEN: 워커 토큰 (Authorization: Bearer)
- SIMAI_WORKER_ID: 워커 ID, 봉투 source_id (기본 worker-01). EC2 에 등록한 ID 와 토큰이 짝이 맞아야 한다(다르면 대여가 401·403).
  simai-bootstrap 은 --worker-id → SIMAI_WORKER_ID → 토큰 파일 이름(worker-s10.token → worker-s10)으로 정해 넘긴다(S15P31A101-414)
- GPU 공유(Tier 0 우선, 명세 3.2): simai_ai.gpu.budget.WorkerGpuSettings.from_env 의 SIMAI_TIER0_MARKER·SIMAI_WORKER_* 값.
  시작할 때 이 프로세스의 torch 할당 상한(SIMAI_WORKER_GPU_MEMORY_FRACTION)을 두고, 대여마다 Tier 0 표시 파일로 접수를 정한다.
  상한을 두지 못하면(FAILED) 시작하지 않는다. torch CUDA 가 없으면 상한 없이 돈다(노트북).

작업 유형별 처리기는 build_handlers 가 조립한다. 처리기가 없으면 시작하지 않는다.
- bev_floorplan(S15P31A101-239): 객체 저장소 주입점(services.worker.objects.ObjectStore)이 있을 때만 등록한다. 진입점은
  워커 전용 서명 API(S15P31A101-248)를 쓰는 HttpObjectStore 를 넘긴다(같은 EC2 주소·워커 토큰).
- tbm_report(S15P31A101-225): 늘 등록한다. 외부 LLM 설정(SIMAI_LLM_BASE_URL·SIMAI_LLM_MODEL·SIMAI_LLM_API_KEY·SIMAI_LLM_TIMEOUT_S,
  simai_ai.report.llm)은 작업마다 읽고, 없으면 그 작업을 재시도 불가 LLM_CONFIG 로 끝낸다(설정 오류를 EC2 에서 보이게).
- vlm_rationale(S15P31A101-25)·incident_summary(S15P31A101-271): 객체 저장소, VLM 설정(SIMAI_VLLM_*·SIMAI_VLM_*,
  simai_ai.vlm.client), 얼굴 처리용 pose 검출기(SIMAI_TIER0_WEIGHTS 의 YOLO26x-pose .pt, Tier 0 와 같은 가중치)가 다 있을 때만
  등록한다. 빠지면 시작 로그에 이유 한 줄. vlm_rationale 은 같은 Pod 의 Tier 0 상태 폴더(SIMAI_STATE_DIR, 없으면
  SIMAI_TIER0_MARKER 의 폴더)에 있는 이벤트 사진 사본을 먼저 읽는다(S15P31A101-408).

사람 지운 배경 묶음 자동 제출(S15P31A101-403, services.worker.auto_static): --once 가 아니고 SIMAI_AUTO_STATIC_RECONSTRUCT 가 0 이
아니면 보조 스레드가 LIVE 가 아닐 때 Tier 0 의 완성 묶음(SIMAI_STATE_DIR/clean-background)을 올리고 static_reconstruct 를 등록한다.
LIVE 판단은 대여와 같은 표시 파일 규칙을 이 스레드 전용 AdmissionGate 로 다시 본다.
"""

from __future__ import annotations

import argparse
from dataclasses import replace
import logging
import os
import signal
import sys
import threading
import time
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

from simai_ai.gpu.admission import AdmissionGate
from simai_ai.gpu.budget import BudgetError, WorkerGpuSettings
from simai_ai.gpu.reserve import FAILED, cap_process_memory
from simai_ai.vlm.client import VlmError, VlmSettings

from .auto_static import AutoStaticSettings, AutoStaticSubmitter, clean_bg_uploader
from .bev_floorplan import BevFloorplanHandler
from .client import AuthError, LeaseClient, RequestRejected
from .handlers import JobHandler
from .incident_summary import IncidentSummaryHandler
from .objects import HttpObjectStore, ObjectStore
from .runner import HandlerStuck, Worker
from .scene_regenerate import SceneRegenerateHandler
from .static_reconstruct import StaticReconstructHandler
from .tbm_report import TbmReportHandler
from .transport import HttpTransport, Transport
from .vlm_frames import LocalFrames
from .vlm_rationale import VlmRationaleHandler

log = logging.getLogger("services.worker")


def open_pose(weights: str) -> Any:
    """YOLO26x-pose .pt → 얼굴 처리용 KeypointProvider. ultralytics(extras detection)는 여기서 처음 import 한다."""
    from simai_ai.calibration.extrinsics.extract import UltralyticsPose
    from simai_ai.vlm.face_blur import PoseDetectorProvider

    return PoseDetectorProvider(UltralyticsPose(weights))


def build_handlers(config: Path | None, store: ObjectStore | None = None, *, env: Mapping[str, str] = os.environ,
                   pose: Callable[[str], Any] = open_pose) -> Mapping[str, JobHandler]:
    """작업 유형 → 처리기. VLM·정적 3D·HMR 처리기는 각 작업 티켓에서 기능 패키지를 조립해 넣는다.

    VLM 처리기는 vLLM 을 WorkerGpuSettings.budget 의 vllm_gpu_memory_utilization·vllm_max_num_seqs 로 띄운다(명세 3.2 상한).
    객체 저장소를 쓰는 처리기는 store 가 있을 때만 넣는다.
    """
    handlers: dict[str, JobHandler] = {"tbm_report": TbmReportHandler(clock_ms=lambda: time.time_ns() // 1_000_000)}
    if store is not None:
        handlers["bev_floorplan"] = BevFloorplanHandler(store, clock_ms=lambda: time.time_ns() // 1_000_000)
        handlers["static_reconstruct"] = StaticReconstructHandler(store, clock_ms=lambda: time.time_ns() // 1_000_000, env=env)
    if (why := _vlm_rationale_missing(store, env)) is None:
        try:
            provider = pose(env["SIMAI_TIER0_WEIGHTS"])
            # 같은 Pod 의 Tier 0 상태 폴더에 있는 이벤트 사진 사본을 먼저 읽는다(-408). 없으면 S3
            handlers["vlm_rationale"] = VlmRationaleHandler(store, provider,  # type: ignore[arg-type]
                                                            local_frames=LocalFrames.from_env(env), env=env)
            handlers["incident_summary"] = IncidentSummaryHandler(store, provider)  # type: ignore[arg-type]
        except Exception as e:  # ultralytics 없음·가중치 손상: 다른 처리기는 돈다
            why = f"pose 검출기를 열지 못했다: {type(e).__name__}: {e}"
    if why:
        log.warning("vlm_rationale·incident_summary 처리기를 등록하지 않는다: %s", why)
    return handlers


def _vlm_rationale_missing(store: ObjectStore | None, env: Mapping[str, str]) -> str | None:
    if store is None:
        return "객체 저장소가 없다"
    try:
        VlmSettings.from_env(env)
    except VlmError as e:
        return f"VLM 설정 오류: {e}"
    weights = env.get("SIMAI_TIER0_WEIGHTS", "").strip()
    if not weights or not Path(weights).is_file():
        return f"pose 가중치(SIMAI_TIER0_WEIGHTS)가 없다: {weights or '설정 안 됨'}"
    return None


def start_auto_static(settings: AutoStaticSettings, gpu: WorkerGpuSettings, transport: Transport, worker_id: str,
                      base_url: str, token: str, stop: threading.Event) -> threading.Thread | None:
    """배경 묶음 자동 제출 보조 스레드. 꺼져 있으면 None. 대여 루프의 접수 문과 상태를 섞지 않게 문을 따로 만든다."""
    if not settings.enabled:
        log.info("배경 묶음 자동 제출 꺼짐 (SIMAI_AUTO_STATIC_RECONSTRUCT=0)")
        return None
    submitter = AutoStaticSubmitter(
        settings, AdmissionGate(gpu.marker, gpu.policy, wall_s=time.time, monotonic=time.monotonic), transport, worker_id,
        prepare=clean_bg_uploader(base_url, token), clock_ms=lambda: time.time_ns() // 1_000_000, monotonic=time.monotonic,
    )
    thread = threading.Thread(target=submitter.run, args=(stop,), name="auto-static-reconstruct", daemon=True)
    thread.start()
    return thread


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="services.worker", description="I5 분석 워커 (EC2 에서 작업을 가져간다)")
    parser.add_argument("--config", type=Path, help="분석 워커 설정 파일 경로")
    parser.add_argument("--once", action="store_true", help="대여 한 번만 처리하고 끝낸다")
    parser.add_argument("--wait-ms", type=int, default=25_000, help="long-poll 대기 (0~30000)")
    args = parser.parse_args(argv)
    if not 0 <= args.wait_ms <= 30_000:
        parser.error("--wait-ms 는 0~30000 이다 (계약 job_lease_request)")
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")

    base_url = os.environ.get("SIMAI_EC2_BASE_URL", "")
    token = os.environ.get("SIMAI_WORKER_TOKEN", "")
    worker_id_env = os.environ.get("SIMAI_WORKER_ID") or ""
    worker_id = worker_id_env or "worker-01"
    if not base_url or not token:
        print("services.worker: SIMAI_EC2_BASE_URL·SIMAI_WORKER_TOKEN 이 필요하다 (.env)", file=sys.stderr)
        return 2

    try:
        gpu = WorkerGpuSettings.from_env(os.environ)
    except BudgetError as e:
        print(f"services.worker: GPU 공유 설정 오류: {e}", file=sys.stderr)
        return 2
    try:
        auto_static = AutoStaticSettings.from_env(os.environ)
    except ValueError as e:
        print(f"services.worker: 배경 묶음 자동 제출 설정 오류: {e}", file=sys.stderr)
        return 2
    try:
        torch_fraction = float(os.environ.get("SIMAI_WORKER_TORCH_MEMORY_FRACTION") or gpu.budget.worker_fraction)
        if not 0 < torch_fraction <= gpu.budget.worker_fraction:
            raise ValueError("torch 상한은 분석 워커 예산 이하여야 한다")
    except ValueError:
        print("services.worker: torch GPU 상한 설정 오류", file=sys.stderr)
        return 2
    cap = cap_process_memory(torch_fraction)
    if cap.state == FAILED:
        print(f"services.worker: GPU 메모리 상한을 두지 못했다: {cap.reason}", file=sys.stderr)
        return 2
    logging.getLogger("services.worker").info("GPU 메모리 상한 %s (%s), Tier 0 표시 파일 %s", cap.state, cap.fraction, gpu.marker)
    log.info("워커 ID %s (%s)", worker_id, "SIMAI_WORKER_ID" if worker_id_env else "SIMAI_WORKER_ID 없음 → 기본값")

    # 맵 재생성의 관제 중 실행(S15P31A101-427). 명세 3.2 는 관제 중 일괄 작업을 막으므로 기본 0 = 관제가 끝난 뒤 처리.
    # 1 = 배경 묶음 만들기와 재구성(static_reconstruct)을 관제 중에도 받는다(허가 게이트의 지연 규칙은 그대로 걸림).
    # 11차 GPU 세션에서 1 로 Tier 0 영향을 재고, 명세 예외 채택 여부를 정한다.
    during_live = os.environ.get("SIMAI_REGENERATE_DURING_LIVE", "0")
    if during_live not in ("0", "1"):
        print("services.worker: SIMAI_REGENERATE_DURING_LIVE 는 0 또는 1", file=sys.stderr)
        return 2
    blocked = gpu.policy.live_blocked_types
    blocked = (blocked - {"static_reconstruct"}) if during_live == "1" else (blocked | {"scene_regenerate"})
    gpu = replace(gpu, policy=replace(gpu.policy, live_blocked_types=frozenset(blocked)))
    transport = HttpTransport(base_url, token)
    handlers = dict(build_handlers(args.config, HttpObjectStore(transport)))
    submitter = AutoStaticSubmitter(auto_static, AdmissionGate(gpu.marker, gpu.policy, wall_s=time.time, monotonic=time.monotonic),
        transport, worker_id, prepare=clean_bg_uploader(base_url, token),
        clock_ms=lambda: time.time_ns() // 1_000_000, monotonic=time.monotonic)
    handlers["scene_regenerate"] = SceneRegenerateHandler(submitter, gpu.marker)
    if not handlers:
        print("services.worker: 등록된 작업 처리기가 없다 (처리기 조립은 후속 티켓)", file=sys.stderr)
        return 1

    stop = threading.Event()
    client = LeaseClient(
        transport,
        worker_id,
        clock_ms=lambda: time.time_ns() // 1_000_000,
        monotonic=time.monotonic,
        sleep=stop.wait,
    )
    gate = AdmissionGate(gpu.marker, gpu.policy, wall_s=time.time, monotonic=time.monotonic)
    worker = Worker(
        client, handlers, clock_ms=lambda: time.time_ns() // 1_000_000, monotonic=time.monotonic, wait_ms=args.wait_ms,
        admission=gate, live_wait_ms=gpu.live_wait_ms, hold_max_s=gpu.hold_max_s, pause_s=gpu.pause_s, sleep=stop.wait,
    )
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_: stop.set())
    if not args.once:  # 한 번만 처리하고 끝나는 실행(simai-bootstrap scene)에서는 관제 뒤 대기 시간을 채울 수 없다
        start_auto_static(auto_static, gpu, transport, worker_id, base_url, token, stop)
    try:
        if args.once:
            worker.run_once(should_stop=stop.is_set)
        else:
            worker.run(stop)
    except AuthError as e:  # 10차 세션: ID 없이 떠 기본 worker-01 로 401 → 감시 프로세스가 빠른 실패 5번 뒤 포기(S15P31A101-414)
        src = "SIMAI_WORKER_ID" if worker_id_env else "SIMAI_WORKER_ID 없음 → 기본값"
        print(f"services.worker: {e} — 워커 ID 와 토큰이 맞지 않음(현재 ID={worker_id}, {src}). EC2 에 등록한 워커 ID 를 SIMAI_WORKER_ID 로 준다"
              "(simai-bootstrap 은 --worker-id 또는 토큰 파일 이름 <워커 ID>.token)", file=sys.stderr)
        return 2
    except RequestRejected as e:
        print(f"services.worker: {e}", file=sys.stderr)
        return 2
    except HandlerStuck as e:  # 멈추지 않는 처리기 스레드는 프로세스를 다시 띄워야 정리된다
        print(f"services.worker: {e}", file=sys.stderr)
        return 3
    return 0


if __name__ == "__main__":
    sys.exit(main())
