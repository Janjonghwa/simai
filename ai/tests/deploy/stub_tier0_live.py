"""시험용 Tier 0 LIVE 프로세스(S15P31A101-183): 실제 Tier0Service 에 검출기·추적 백엔드 대역만 끼운다(노트북에 ultralytics·엔진 없음).

session1_live.LiveHarness 의 송신·노트북 흉내·표시 파일 판정을 노트북에서 시험할 때 진입점 대신 띄운다.
    python stub_tier0_live.py --config <설정> --run-seconds N
"""

import argparse
import sys
import time
from pathlib import Path

AI = Path(__file__).resolve().parents[2]
for p in (AI, AI / "tests" / "services" / "tier0"):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

from test_live_input import OneTrackBackend  # noqa: E402

from services.tier0.service import Tier0Service, Tier0StartError  # noqa: E402
from services.tier0.settings import load_settings  # noqa: E402
from simai_ai.tracking.local_tracker import CameraDetections  # noqa: E402


class NoPeople:
    """사람 없는 영상의 검출 결과(빈 목록). 식별자·촬영 시각은 넣은 프레임 그대로(실제 검출기와 같다)."""

    def detect(self, batch):
        return [CameraDetections(f.camera_id, f.frame_ref["stream_epoch"], f.frame_ref["frame_sequence"],
                                 f.estimated_capture_time, f.timestamp_source, f.timestamp_uncertainty_ms, ())
                for f in batch]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", type=Path, required=True)
    ap.add_argument("--run-seconds", type=float, required=True)
    args = ap.parse_args()
    service = Tier0Service(load_settings(args.config), detector_factory=NoPeople, tracker_backend_factory=OneTrackBackend)
    try:
        service.start()
    except Tier0StartError as e:
        print(f"시작하지 않았다: {e}", file=sys.stderr)
        return e.exit_code
    time.sleep(args.run_seconds)
    return 0 if service.stop(timeout_s=30) else 1


if __name__ == "__main__":
    sys.exit(main())
