"""녹화 세션 시험 도구 (S15P31A101-215): gateway/recorder 녹화 형식(세그먼트 파일 + 계약을 통과하는 manifest)을 만든다.

영상 바이트는 호출자가 준다(시험 안에서 만든 H.264 이거나 임의 바이트). 시험 임시 디렉터리에만 쓰고 커밋하지 않는다.
manifest 는 contracts/examples/recording_manifest.test_input.json 의 합성 카메라 항목을 틀로 쓴다(합성 입력, is_test_input).
"""

from __future__ import annotations

import copy
import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from simai_ai.providers.contract import contracts_dir


@dataclass
class Segment:
    epoch: int
    codec: dict[str, Any]  # codec_info 계약
    frames: list[tuple[dict[str, Any], bytes]] = field(default_factory=list)  # (frame_header 계약, 압축 바이트)


def _entry(root: Path, rel: str) -> dict[str, Any]:
    data = (root / rel).read_bytes()
    return {"path": rel, "bytes": len(data), "sha256": "sha256:" + hashlib.sha256(data).hexdigest()}


def _received(header: dict[str, Any]) -> int:
    return header.get("received_at_ms", header["estimated_capture_time_ms"])


def write_session(root: Path, session_id: str, cameras: dict[str, list[Segment]], *,
                  consent: dict[str, Any] | None = None) -> Path:
    """root/<session_id>/ 에 세션을 쓰고 그 경로를 돌려준다. 계약(recording_manifest_errors)을 통과하는 manifest 를 만든다."""
    template = json.loads((contracts_dir() / "examples" / "recording_manifest.test_input.json").read_text(encoding="utf-8"))
    cam_template = template["cameras"][0]
    session = root / session_id
    cams_doc = []
    for cid, segments in cameras.items():
        (session / cid).mkdir(parents=True, exist_ok=True)
        (session / cid / "events.jsonl").write_bytes(b"")
        segs_doc = []
        for seg in segments:
            stem = f"{cid}/epoch_{seg.epoch:04d}"
            video, lines, offset = bytearray(), [], 0
            for header, data in seg.frames:
                lines.append(json.dumps({"header": header, "offset": offset, "size": len(data)}) + "\n")
                video += data
                offset += len(data)
            (session / f"{stem}.h264").write_bytes(bytes(video))
            (session / f"{stem}.frames.jsonl").write_text("".join(lines), encoding="utf-8", newline="\n")
            (session / f"{stem}.codec_info.json").write_text(json.dumps(seg.codec), encoding="utf-8")
            headers = [h for h, _ in seg.frames]
            doc = copy.deepcopy(cam_template["segments"][0])
            doc.update({
                "stream_epoch": seg.epoch,
                "video": _entry(session, f"{stem}.h264"),
                "frames": _entry(session, f"{stem}.frames.jsonl"),
                "codec_info": _entry(session, f"{stem}.codec_info.json"),
                "frame_count": len(headers),
                "keyframe_count": sum(1 for h in headers if h["keyframe"]),
                "frame_sequence_range": [headers[0]["frame_sequence"], headers[-1]["frame_sequence"]] if headers else None,
                "capture_time_range_ms": ([headers[0]["estimated_capture_time_ms"], headers[-1]["estimated_capture_time_ms"]]
                                          if headers else None),
                "received_time_range_ms": [_received(headers[0]), _received(headers[-1])] if headers else None,
                "timestamp_sources": sorted({h["timestamp_source"] for h in headers}),
                "timestamp_uncertainty_ms_max": max((h.get("timestamp_uncertainty_ms") or 0.0 for h in headers),
                                                    default=None),
            })
            segs_doc.append(doc)
        cam = copy.deepcopy(cam_template)
        ranges = [s["capture_time_range_ms"] for s in segs_doc if s["capture_time_range_ms"] is not None]
        cam.update({
            "camera_id": cid,
            "frame_count": sum(s["frame_count"] for s in segs_doc),
            "capture_time_range_ms": [min(r[0] for r in ranges), max(r[1] for r in ranges)] if ranges else None,
            "reconnects": max(0, len(segs_doc) - 1),
            "events": _entry(session, f"{cid}/events.jsonl"),
            "segments": segs_doc,
        })
        cams_doc.append(cam)
    ranges = [c["capture_time_range_ms"] for c in cams_doc if c["capture_time_range_ms"] is not None]
    manifest = copy.deepcopy(template)
    manifest.update({
        "manifest_version": "1.2.0",
        "session_id": session_id,
        "input_kinds": ["SYNTHETIC"],
        "is_test_input": True,
        "started_at_ms": min(r[0] for r in ranges),
        "ended_at_ms": max(r[1] for r in ranges),
        "common_capture_window_ms": [max(r[0] for r in ranges), min(r[1] for r in ranges)],
        "consent": consent,
        "storage": {"uri": f"volume://AP-JP-1/simai/recordings/{session_id}/",
                    "layout": "<camera_id>/epoch_NNNN.{h264,frames.jsonl,codec_info.json}"},
        "cameras": cams_doc,
    })
    (session / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=1), encoding="utf-8")
    return session


def header(cam: str, epoch: int, seq: int, capture_ms: int, keyframe: bool, *, received_ms: int | None = None,
           source: str = "RECEIVE_ESTIMATED", uncertainty: float = 20.0) -> dict[str, Any]:
    """frame_header 계약 한 건. received_ms 를 주지 않으면 received_at_ms 를 싣지 않는다."""
    doc = {"camera_id": cam, "stream_epoch": epoch, "frame_sequence": seq, "estimated_capture_time_ms": capture_ms,
           "timestamp_source": source, "timestamp_uncertainty_ms": uncertainty, "source_pts": seq * 3600,
           "pts_timebase": "1/90000", "keyframe": keyframe}
    if received_ms is not None:
        doc["received_at_ms"] = received_ms
    return doc


def codec_doc(cam: str, epoch: int, *, sps: str = "Z0LAHg==", pps: str = "aM4G4g==", width: int = 256,
              height: int = 144) -> dict[str, Any]:
    return {"camera_id": cam, "stream_epoch": epoch, "codec": "H264", "profile": "High", "sps": sps, "pps": pps,
            "pts_timebase": "1/90000", "width": width, "height": height, "nominal_fps": 25}
