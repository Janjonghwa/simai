# /// script
# requires-python = ">=3.11"
# dependencies = ["opencv-python>=4.9"]
# ///
"""EXP-01 E-GATE-0 품질 확인용: 노트북 웹캠으로 교실을 여러 위치에서 찍는다.

미리보기 창에 카운트다운이 보이고 --interval 초마다 한 장씩 --count 장을 저장한다.
찍는 사이 노트북을 1~2m씩 옮기고 방향을 조금씩 돌린다. 사람이 화면에 없을수록 좋다.
저장 위치: ai/experiments/recordings/webcam_<시각>/ (커밋하지 않음). q 키로 중단.

  uv run ai/experiments/static_scene/capture_webcam.py --count 6 --interval 4
"""
import argparse
import time
from datetime import datetime
from pathlib import Path

import cv2

REPO = Path(__file__).resolve().parents[3]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--count", type=int, default=6)
    ap.add_argument("--interval", type=float, default=4.0)
    ap.add_argument("--camera", type=int, default=0)
    ap.add_argument("--width", type=int, default=1920)
    ap.add_argument("--height", type=int, default=1080)
    a = ap.parse_args()

    cap = cv2.VideoCapture(a.camera, cv2.CAP_DSHOW)
    cap.set(cv2.CAP_PROP_FRAME_WIDTH, a.width)
    cap.set(cv2.CAP_PROP_FRAME_HEIGHT, a.height)
    if not cap.isOpened():
        print("웹캠을 열 수 없습니다")
        return 1
    out = REPO / "ai" / "experiments" / "recordings" / f"webcam_{datetime.now():%Y%m%d-%H%M%S}"
    out.mkdir(parents=True)
    saved, next_t = 0, time.monotonic() + a.interval
    while saved < a.count:
        ok, frame = cap.read()
        if not ok:
            continue
        left = next_t - time.monotonic()
        view = frame.copy()
        cv2.putText(view, f"{saved + 1}/{a.count}  {max(left, 0):.1f}s", (30, 60), cv2.FONT_HERSHEY_SIMPLEX, 1.6, (0, 255, 255), 3)
        cv2.imshow("capture (q: quit)", cv2.resize(view, (960, int(960 * view.shape[0] / view.shape[1]))))
        if cv2.waitKey(1) & 0xFF == ord("q"):
            break
        if left <= 0:
            saved += 1
            cv2.imwrite(str(out / f"view_{saved:02d}.jpg"), frame, [cv2.IMWRITE_JPEG_QUALITY, 95])
            print(f"저장 {saved}/{a.count} ({frame.shape[1]}x{frame.shape[0]})", flush=True)
            next_t = time.monotonic() + a.interval
    cap.release()
    cv2.destroyAllWindows()
    print(f"폴더: {out.relative_to(REPO)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
