"""E-GATE-1 입력 준비: 녹화 세션 → 같은 시각 카메라별 그림 (노트북 CPU, S15P31A101-87).

gateway/recorder 녹화(rec-20261021-031045z-classroom-dev-test 등)를 `simai_ai.io.RecordingReplay` 로 읽고 PyAV CPU 로 디코딩해,
공통 촬영 구간에서 일정 간격 시각마다 카메라별로 촬영 시각이 가장 가까운 프레임 하나를 JPEG 로 쓴다. 기준(--tolerance-ms)보다 먼
프레임밖에 없으면 그 카메라는 그 시각에 비운다(다른 시각 그림으로 채우지 않는다). GPU Job(`run_multiview_hmr.py`)의 입력이다.

    uv run --extra io python experiments/hmr/egate1_frames.py --session experiments/recordings/rec-20261021-031045z-classroom-dev-test \\
        --out <저장소 밖 또는 experiments/recordings/ 아래 새 폴더> --step-ms 2000 --max-instants 60

출력: <out>/index.json(`simai.egate1_frames`), <out>/i<시각 번호>/<camera_id>.jpg. 사람이 찍힌 자료라 커밋하지 않고(.gitignore recordings/),
Pod 에는 묶음(tar)으로 올린다. 촬영 시각·timestamp_source 는 녹화 헤더 그대로다(지어내지 않음).
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any

from simai_ai.io import I0CodecInfo, I0Frame, PyAvDecoder, RecordingReplay
from simai_ai.io.decoder import DecodeError, Decoder
from simai_ai.io.sources import recording_dataset_id

FORMAT = "simai.egate1_frames"
FORMAT_VERSION = "0.1.0"
INDEX_FILE = "index.json"
DEFAULT_TOLERANCE_MS = 20  # 25 fps 프레임 간격(40 ms)의 절반
DEFAULT_JPEG_QUALITY = 95

Header = dict[str, Any]


def read_headers(session: Path, camera_ids: Sequence[str] | None = None) -> dict[str, list[Header]]:
    """카메라별 프레임 헤더(디코딩 없이). RecordingReplay 가 manifest·세그먼트 무결성을 먼저 확인한다."""
    replay = RecordingReplay(session, camera_ids)
    out: dict[str, list[Header]] = {c: [] for c in replay.camera_ids}
    try:
        for msg in replay.pop_due(2**62):
            if isinstance(msg.message, I0Frame):
                h = msg.message.header
                out[msg.camera_id].append({
                    "stream_epoch": h.stream_epoch, "frame_sequence": h.frame_sequence,
                    "capture_time_ms": h.estimated_capture_time_ms, "timestamp_source": h.timestamp_source,
                    "timestamp_uncertainty_ms": h.timestamp_uncertainty_ms,
                })
    finally:
        replay.close()
    return out


def select_instants(headers: Mapping[str, Sequence[Header]], *, step_ms: int, start_offset_ms: int = 0,
                    max_instants: int = 0, tolerance_ms: int = DEFAULT_TOLERANCE_MS) -> list[dict[str, Any]]:
    """공통 촬영 구간 [모든 카메라 첫 프레임 중 가장 늦은 것, 마지막 프레임 중 가장 이른 것] 에서 step_ms 마다 한 시각.
    카메라마다 촬영 시각이 가장 가까운 프레임(같으면 이른 것). 차이가 tolerance_ms 를 넘으면 그 카메라는 비운다."""
    if step_ms <= 0:
        raise ValueError("step_ms 는 양수여야 한다")
    usable = {c: sorted(hs, key=lambda h: (h["capture_time_ms"], h["stream_epoch"], h["frame_sequence"])) for c, hs in headers.items() if hs}
    if not usable:
        return []
    start = max(hs[0]["capture_time_ms"] for hs in usable.values()) + start_offset_ms
    end = min(hs[-1]["capture_time_ms"] for hs in usable.values())
    out = []
    t = start
    while t <= end and (not max_instants or len(out) < max_instants):
        frames, missing = {}, []
        for cid in sorted(headers):
            hs = usable.get(cid, [])
            best = min(hs, key=lambda h: (abs(h["capture_time_ms"] - t), h["capture_time_ms"]), default=None)
            if best is None or abs(best["capture_time_ms"] - t) > tolerance_ms:
                missing.append(cid)
            else:
                frames[cid] = dict(best)
        out.append({"instant": len(out), "t_ms": t, "frames": frames, "missing": missing})
        t += step_ms
    return out


def default_writer(quality: int = DEFAULT_JPEG_QUALITY) -> Callable[[Path, Any], None]:
    import cv2  # extras io

    def write(path: Path, bgr) -> None:
        if not cv2.imwrite(str(path), bgr, [cv2.IMWRITE_JPEG_QUALITY, quality]):
            raise OSError(f"{path}: JPEG 를 쓰지 못했다")

    return write


def extract(session: Path, instants: list[dict[str, Any]], out: Path, writer: Callable[[Path, Any], None],
            decoder_factory: Callable[[], Decoder] = PyAvDecoder) -> dict[str, int]:
    """고른 프레임만 디코딩해 쓴다. 카메라마다 처음부터 디코딩한다(H.264 는 앞 프레임에 기댄다). 못 낸 프레임은 지운다(채우지 않음)."""
    wanted: dict[tuple[str, int, int], list[dict]] = {}
    for inst in instants:
        for cid, f in inst["frames"].items():
            wanted.setdefault((cid, f["stream_epoch"], f["frame_sequence"]), []).append((inst, f))
    stats = {"written": 0, "decode_errors": 0, "not_decoded": 0}
    cams = sorted({k[0] for k in wanted})
    for cid in cams:
        replay = RecordingReplay(session, [cid])
        dec = decoder_factory()

        def emit(pictures) -> None:
            for pic in pictures:
                for inst, f in wanted.get((cid, *pic.key), []):
                    path = out / f"i{inst['instant']:05d}" / f"{cid}.jpg"
                    path.parent.mkdir(parents=True, exist_ok=True)
                    writer(path, pic.image)
                    f["file"] = path.relative_to(out).as_posix()
                    f["sha256"] = "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()
                    stats["written"] += 1

        try:
            for msg in replay.pop_due(2**62):
                m = msg.message
                if isinstance(m, I0CodecInfo):
                    dec.configure(m)
                elif isinstance(m, I0Frame):
                    try:
                        emit(dec.decode(m.payload, m.header.key, m.header.keyframe))
                    except DecodeError:
                        stats["decode_errors"] += 1
                elif m is None:
                    emit(dec.flush())
            emit(dec.flush())
        finally:
            replay.close()
    for inst in instants:
        for cid in [c for c, f in inst["frames"].items() if "file" not in f]:
            del inst["frames"][cid]
            inst["missing"] = sorted({*inst["missing"], cid})
            stats["not_decoded"] += 1
    return stats


def main(argv: list[str] | None = None, writer: Callable[[Path, Any], None] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--session", required=True, type=Path)
    ap.add_argument("--out", required=True, type=Path)
    ap.add_argument("--cameras", nargs="*", default=None)
    ap.add_argument("--step-ms", type=int, default=2000)
    ap.add_argument("--start-offset-ms", type=int, default=0)
    ap.add_argument("--max-instants", type=int, default=0)
    ap.add_argument("--tolerance-ms", type=int, default=DEFAULT_TOLERANCE_MS)
    a = ap.parse_args(argv)
    if a.out.exists():
        print(f"egate1_frames: {a.out} 가 이미 있다(덮어쓰지 않는다)", file=sys.stderr)
        return 2
    headers = read_headers(a.session, a.cameras)
    instants = select_instants(headers, step_ms=a.step_ms, start_offset_ms=a.start_offset_ms, max_instants=a.max_instants,
                               tolerance_ms=a.tolerance_ms)
    if not instants:
        print("egate1_frames: 공통 촬영 구간에 고를 시각이 없다", file=sys.stderr)
        return 3
    a.out.mkdir(parents=True)
    stats = extract(a.session, instants, a.out, writer or default_writer())
    replay = RecordingReplay(a.session, a.cameras)
    manifest, prov = replay.manifest, replay.provenance()
    replay.close()
    index = {"format": FORMAT, "format_version": FORMAT_VERSION, "session_id": manifest["session_id"],
             "dataset_id": recording_dataset_id(manifest), "frames_provenance": prov,
             "selection": {"step_ms": a.step_ms, "start_offset_ms": a.start_offset_ms, "tolerance_ms": a.tolerance_ms,
                           "max_instants": a.max_instants},
             "stats": stats, "instants": instants}
    (a.out / INDEX_FILE).write_text(json.dumps(index, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"egate1_frames: 시각 {len(instants)}개, 그림 {stats['written']}장 → {a.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
