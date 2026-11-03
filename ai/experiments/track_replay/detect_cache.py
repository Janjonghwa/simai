"""녹화 세션의 프레임마다 YOLO26-pose 검출을 한 번 돌려 2D 관측 캐시를 만든다 (S15P31A101-413).

추적·융합 실험(`replay_tracking.py`)을 검출 없이 반복하려고 만든다. 녹화의 H.264 를 PyAV 로 디코딩해 운영과 같은
검출기 어댑터(`services.tier0.yolo_detector` 와 같은 `predict` 인자: imgsz·half, 나머지 ultralytics 기본값)로 검출하고,
프레임 헤더(촬영 시각·수신 시각·불확실성·frame_sequence)와 검출(박스·점수·관절)만 카메라별 gzip JSONL 로 남긴다.

- 영상·프레임 그림은 저장하지 않는다(사람이 찍힌 dev_test 녹화, 명세 11.5). 캐시도 저장소 밖에 둔다(`--out`).
- 검출 값은 고치지 않는다. 숫자만 소수 2자리(픽셀)·4자리(점수)로 줄인다(검출 잡음보다 훨씬 작다).
- 카메라마다 `<camera_id>.jsonl.gz.partial` 에 쓰고 끝나면 이름을 바꾼다. 이미 끝난 카메라는 건너뛴다(이어 하기).
- 손상 표시가 붙은 그림·디코딩 실패 프레임은 검출하지 않고 `"decode": "corrupt"|"error"` 로 남긴다(지어내지 않음).

실행(노트북 GPU, CUDA torch 가 든 별도 venv 에 `uv pip install -e ./ai --no-deps`):

    python experiments/track_replay/detect_cache.py --session <녹화 폴더> --weights <yolo26x-pose.pt> \
        --out <캐시 폴더> [--imgsz 1280] [--batch 4] [--cameras cam_A cam_B ...]
"""

from __future__ import annotations

import argparse
import base64
import gzip
import json
import os
import sys
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

ANNEXB = b"\x00\x00\x00\x01"


def _frames(session: Path, camera_id: str) -> Iterator[tuple[dict[str, Any], bytes, dict[str, Any]]]:
    """(헤더, 압축 프레임, 코덱 정보)를 frame_sequence 순서로. 세그먼트(epoch)가 여럿이면 차례로."""
    manifest = json.loads((session / "manifest.json").read_text(encoding="utf-8"))
    cam = next(c for c in manifest["cameras"] if c["camera_id"] == camera_id)
    for seg in cam["segments"]:
        codec = json.loads((session / seg["codec_info"]["path"]).read_text(encoding="utf-8"))
        with open(session / seg["video"]["path"], "rb") as video, open(
            session / seg["frames"]["path"], encoding="utf-8"
        ) as lines:
            for line in lines:
                row = json.loads(line)
                video.seek(int(row["offset"]))
                yield row["header"], video.read(int(row["size"])), codec


def _decoder(av: Any, codec: dict[str, Any]) -> tuple[Any, bytes]:
    ctx = av.CodecContext.create("h264", "r")
    ctx.thread_type = "SLICE"  # 운영(PyAvDecoder)과 같다. AUTO(프레임 스레드)는 Windows PyAV 18 에서 BGR 변환 중 멈췄다
    prefix = b"".join(ANNEXB + base64.b64decode(codec[k]) for k in ("sps", "pps") if codec.get(k))
    return ctx, prefix


def _round_det(box: list[float], score: float, kps: list[list[float]] | None) -> list[Any]:
    joints = [[round(k[0], 2), round(k[1], 2), round(k[2], 4)] if len(k) >= 3 else None for k in (kps or ())]
    return [[round(v, 2) for v in box], round(min(1.0, max(0.0, score)), 4), joints]


def run_camera(
    session: Path, camera_id: str, model: Any, out_dir: Path, imgsz: int, batch: int, half: bool,
    max_frames: int | None = None, stride: int = 1,
) -> None:
    import av

    final = out_dir / f"{camera_id}.jsonl.gz"
    if final.exists():
        print(f"{camera_id}: 이미 있다, 건너뛴다 ({final})", flush=True)
        return
    partial = out_dir / f"{camera_id}.jsonl.gz.partial"
    ctx, prefix, codec_key = None, b"", None
    pending: dict[int, dict[str, Any]] = {}  # 디코더 입력 번호 → 헤더
    queue: list[tuple[dict[str, Any], Any]] = []  # (헤더, BGR 그림)
    n_in = n_det = n_bad = 0
    t0 = time.monotonic()

    def flush_queue(fh: Any) -> None:
        nonlocal n_det
        if not queue:
            return
        results = model.predict([img for _, img in queue], imgsz=imgsz, half=half, verbose=False)
        for (header, _), r in zip(queue, results, strict=True):
            dets = []
            boxes = getattr(r, "boxes", None)
            if boxes is not None and len(boxes):
                kp = r.keypoints.data.tolist() if r.keypoints is not None else [None] * len(boxes)
                for box, score, kps in zip(boxes.xyxy.tolist(), boxes.conf.tolist(), kp, strict=True):
                    dets.append(_round_det(box, float(score), kps))
            fh.write(json.dumps({"h": header, "dets": dets}, separators=(",", ":")) + "\n")
            n_det += 1
        queue.clear()

    with gzip.open(partial, "wt", encoding="utf-8", compresslevel=5) as fh:
        for header, payload, codec in _frames(session, camera_id):
            if max_frames is not None and n_in >= max_frames:
                break
            key = (codec.get("sps"), codec.get("pps"), header["stream_epoch"])
            if key != codec_key:
                ctx, prefix = _decoder(av, codec)
                codec_key = key
            pts = n_in
            n_in += 1
            pending[pts] = header
            data = prefix + payload if header.get("keyframe") else payload
            try:
                packet = av.Packet(data)
                packet.pts = pts
                pictures = ctx.decode(packet)
            except Exception:  # noqa: BLE001 - 디코딩 실패 프레임은 검출하지 않고 표시만 남긴다
                pending.pop(pts, None)
                fh.write(json.dumps({"h": header, "dets": None, "decode": "error"}, separators=(",", ":")) + "\n")
                n_bad += 1
                continue
            for pic in pictures:
                h = pending.pop(pic.pts, None) if pic.pts is not None else None
                if h is None:
                    continue
                if pic.is_corrupt:
                    fh.write(json.dumps({"h": h, "dets": None, "decode": "corrupt"}, separators=(",", ":")) + "\n")
                    n_bad += 1
                    continue
                if int(h["frame_sequence"]) % stride:
                    continue  # 검출하지 않는 프레임(운영 Tier 0 도 latest-frame-wins 로 카메라당 약 12 fps 만 검출한다)
                queue.append((h, pic.to_ndarray(format="bgr24")))
                if len(queue) >= batch:
                    flush_queue(fh)
            if n_in % 1000 == 0:
                rate = n_in / max(time.monotonic() - t0, 1e-6)
                print(f"{camera_id}: {n_in} 프레임 ({rate:.1f} fps)", flush=True)
        if ctx is not None:
            for pic in ctx.decode(None):
                h = pending.pop(pic.pts, None) if pic.pts is not None else None
                if h is not None and not pic.is_corrupt and not int(h["frame_sequence"]) % stride:
                    queue.append((h, pic.to_ndarray(format="bgr24")))
        flush_queue(fh)
    os.replace(partial, final)
    print(f"{camera_id}: 끝 ({n_det} 검출 프레임, 디코딩 실패·손상 {n_bad}, {time.monotonic() - t0:.0f} s)", flush=True)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--session", required=True, type=Path)
    ap.add_argument("--weights", required=True)
    ap.add_argument("--out", required=True, type=Path)
    ap.add_argument("--imgsz", type=int, default=1280)
    ap.add_argument("--batch", type=int, default=4)
    ap.add_argument("--no-half", action="store_true")
    ap.add_argument("--cameras", nargs="*")
    ap.add_argument("--stride", type=int, default=1, help="frame_sequence 가 이 값의 배수인 프레임만 검출(2 = 12.5 fps)")
    ap.add_argument("--max-frames", type=int, default=None, help="시험용: 카메라당 앞 N 프레임만")
    args = ap.parse_args(argv)
    manifest_path = args.session / "manifest.json"
    if not manifest_path.is_file():
        ap.error(f"{manifest_path}: 원본 manifest·codec_info·frames sidecar가 필요하다. H264의 명목 FPS로 촬영 시각을 만들지 않는다")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    cams = args.cameras or [c["camera_id"] for c in manifest["cameras"]]
    # GPU 가중치를 올리기 전에 원본 시각·오프셋·코덱 메타데이터가 있는지 검사한다.
    for cam in cams:
        entry = next(c for c in manifest["cameras"] if c["camera_id"] == cam)
        for segment in entry["segments"]:
            for kind in ("video", "frames", "codec_info"):
                path = args.session / segment[kind]["path"]
                if not path.is_file():
                    ap.error(f"원본 녹화 파일이 없다: {path}")
    from ultralytics import YOLO
    args.out.mkdir(parents=True, exist_ok=True)
    meta = {
        "session_id": manifest["session_id"],
        "weights": os.path.basename(args.weights),
        "imgsz": args.imgsz,
        "half": not args.no_half,
        "predict": "ultralytics defaults (conf 0.25, iou 0.7, max_det 300)",
        "cameras": cams,
        "stride": args.stride,
    }
    (args.out / "meta.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
    model = YOLO(args.weights, task="pose")
    for cam in cams:
        run_camera(args.session, cam, model, args.out, args.imgsz, args.batch, not args.no_half, args.max_frames, args.stride)
    return 0


if __name__ == "__main__":
    sys.exit(main())
