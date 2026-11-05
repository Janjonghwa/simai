"""python -m services.avatar: 기본 서비스 목록에는 없고 avatar를 명시할 때만 돈다."""

import logging
import os
import signal
import threading
import time
from dataclasses import replace
from pathlib import Path

from simai_ai.gpu.admission import AvatarAdmissionGate
from simai_ai.gpu.budget import WorkerGpuSettings
from simai_ai.gpu.reserve import FAILED, cap_process_memory
from simai_ai.hmr.runtime import FastBodyModel
from simai_ai.hmr.diagnostics import exception_summary
from simai_ai.hmr.loader import prepare_checkpoint
from simai_ai.hmr.stream import AvatarLoop
from simai_ai.hmr.segmentation import segmentation_from_env
from simai_ai.hmr.appearance_stream import appearance_from_env, face_faces_from_rig
from simai_ai.io.avatar_channel import LatestChannelReader, avatar_channel_path

from .transport import AvatarSender, AvatarUpload, websocket_connector


def main():
    logging.basicConfig(level=logging.INFO)
    env = os.environ
    stream_epoch = time.time_ns() // 1_000_000
    root = Path(env.get("SIMAI_ROOT") or "/workspace/simai")
    stop = threading.Event()
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda *_: stop.set())
    try:
        base, token = env.get("SIMAI_EC2_BASE_URL", ""), env.get("SIMAI_WORKER_TOKEN", "")
        if not base or not token:
            raise ValueError("EC2 주소·워커 토큰 필요")
        settings = WorkerGpuSettings.from_env(env)
        # I5 hmr_* 차단은 그대로. 이 서비스는 I5 작업을 대여하지 않는다.
        policy = replace(settings.policy, allow_without_marker=False)
        gate = AvatarAdmissionGate(settings.marker, policy, wall_s=time.time, monotonic=time.monotonic)
        # vLLM 몫을 제외한 분석 워커 잔여 예산. 프로세스별 0.70을 중복 허용하지 않는다.
        fraction = float(env.get("SIMAI_AVATAR_GPU_MEMORY_FRACTION") or "0.10")
        if not 0 < fraction <= settings.budget.worker_fraction:
            raise ValueError("아바타 GPU 상한은 분석 워커 잔여 예산 이하여야 한다")
        cap = cap_process_memory(fraction)
        if cap.state == FAILED:
            raise ValueError("아바타 GPU 상한 설정 실패")
        report_at = 0.
        while not stop.is_set() and not (admission := gate.check()).admit:
            if time.monotonic() >= report_at:
                logging.info("아바타 시작 대기: AdmissionGate=%s", admission.reason)
                report_at = time.monotonic() + 60
            stop.wait(0.2)
        if stop.is_set():
            return 0
        checkpoint = env.get("SIMAI_AVATAR_CHECKPOINT")
        path = Path(checkpoint) if checkpoint else prepare_checkpoint(root, Path(env["SIMAI_FS3DB_REPO"]))
        model = FastBodyModel(path)
        segmentation = segmentation_from_env(env, device=model.device)
        appearance = appearance_from_env(env)
        if appearance is not None:
            try:
                face_faces_from_rig(model.rig, appearance.face_region)
            except ValueError as e:
                # auto 로 켜진 누적은 얼굴 영역을 못 정하면 끄고 메시·대표색으로 계속한다. 1 로 강제했으면 멈춘다
                if env.get("SIMAI_AVATAR_APPEARANCE", "auto") == "1":
                    raise
                logging.warning("텍스처 누적 끔: 얼굴 영역 확인 실패 (%s)", e)
                appearance = None
        clock_ms = lambda: time.time_ns() // 1_000_000
        sender = AvatarSender(websocket_connector(base, token), clock_ms=clock_ms, monotonic=time.monotonic,
                              source_id=env.get("SIMAI_WORKER_ID") or "worker-01", stream_epoch=stream_epoch,
                              log_secrets=(token,))
        source = LatestChannelReader(avatar_channel_path(env, root))
        loop = AvatarLoop(source, gate, model, AvatarUpload(base, token), sender, clock_ms=clock_ms,
                          appearance=appearance, segmentation=segmentation)
        def send_loop():
            while not stop.is_set():
                sender.tick()
                stop.wait(0.02)
            sender.close()
        thread = threading.Thread(target=send_loop, name="avatar-send", daemon=True)
        thread.start()
        try:
            while not stop.is_set():
                started = time.monotonic()
                try:
                    loop.step()
                except Exception as error:
                    logging.warning("아바타 회차 실패: %s; 다음 최신 입력으로 재시도",
                                    exception_summary(error, secrets=(token,)))
                stop.wait(max(0., .2 - (time.monotonic() - started)))
        finally:
            stop.set()
            loop.close()
            thread.join(timeout=2)
        return 0
    except Exception as error:
        logging.error("아바타 서비스 시작 실패: %s", exception_summary(error, secrets=(env.get("SIMAI_WORKER_TOKEN", ""),)))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
