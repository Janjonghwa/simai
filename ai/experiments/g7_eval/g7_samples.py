"""G7 표본 묶음 생성 (노트북 CPU, S15P31A101-35, 설계 docs/design/g7-eval.md).

Tier 0 재생 출력(risk_event.jsonl·world_state.jsonl, 파일 모드 또는 시나리오 골든 `--out`)에서 이벤트를 골라
표본마다 "이벤트 시점 프레임 + 직전 프레임 + 구조화 상태 + 정답 상태 요약"을 묶는다(명세 5.4 빠른 경로 입력과 같은 모양).

- 고르기: 층(규칙·확인 필요 여부·사람 수 등) 칸마다 섞은 뒤 칸을 돌아가며 하나씩 뽑는다(칸 사이 개수 차이 최대 1, 모자란 칸은 건너뜀).
- 프레임: 녹화 세션(gateway/recorder)에서 카메라마다 요청 시각에 가장 가까운 프레임(기준 --tolerance-ms 안)만 디코딩해 쓴다.
  기준 밖이면 비운다(다른 시각으로 채우지 않음). 흐림은 여기서 하지 않는다: 답 생성기가 VLM 직전에 270 관문으로 한다(제품과 같은 자리).
- 정답 요약: 사람이 재생을 보고 쓴 truth.jsonl({"event_id", "summary", ...}). 없으면 표본에 truth null 로 남기고 manifest 에 센다.
- 사람이 찍힌 그림을 쓰므로 출력은 저장소 밖(또는 .gitignore 의 runs/·recordings/ 아래)에 둔다. 커밋하는 것은 manifest 사본뿐이다.
- 녹화가 시험 입력(is_test_input)이거나 SIM 이면 manifest 의 product_metric 은 false 다(AGENTS 규칙 7).

    uv run --extra io python experiments/g7_eval/g7_samples.py --replay <재생 출력 폴더> --session <녹화 세션> \\
        --truth <truth.jsonl> --out <묶음 폴더> [--total 100] [--by rule_id,check,people] [--seed 0]
"""

from __future__ import annotations

import argparse
import hashlib
import json
import random
import sys
from collections.abc import Callable, Iterable, Mapping, Sequence
from pathlib import Path
from typing import Any

from simai_ai.io import I0CodecInfo, I0Frame, PyAvDecoder, RecordingReplay
from simai_ai.io.decoder import DecodeError, Decoder
from simai_ai.io.sources import load_recording_manifest

FORMAT = "simai.g7_samples"
FORMAT_VERSION = "0.1.0"
MANIFEST_FILE = "manifest.json"
DEFAULT_OFFSETS_MS = (-4000, 0)  # 직전 3~5초 안 한 장 + 이벤트 시점(5.4). 프레임 수는 지연(G7 p95)과 맞바꾼다
DEFAULT_TOLERANCE_MS = 40  # 25 fps 한 프레임 간격
DIMENSIONS = ("rule_id", "severity", "check", "cameras", "people")
DEFAULT_BY = ("rule_id", "check", "people")
FAST_PATH_RULES = ("R1", "R2", "R3", "R4")  # 5.4: R1~R4 근거 설명, MVP 는 CRITICAL 만
FAST_PATH_SEVERITIES = ("CRITICAL",)

Writer = Callable[[Path, Any], None]


# ---------------------------------------------------------------- 후보


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    with path.open(encoding="utf-8") as f:
        return [json.loads(line) for line in f if line.strip()]


def _nearest_world_state(states: Sequence[Mapping[str, Any]], t_ms: int) -> Mapping[str, Any] | None:
    best = None
    for s in states:
        t = s["payload"].get("t_latest_global")
        if isinstance(t, int) and t <= t_ms and (best is None or t > best["payload"]["t_latest_global"]):
            best = s
    return best


def candidates_from_replay(risk_events: Sequence[Mapping[str, Any]], world_states: Sequence[Mapping[str, Any]],
                           truth: Mapping[str, Mapping[str, Any]] | None = None) -> list[dict[str, Any]]:
    """이벤트마다 처음 줄(발생 상태) → 후보. 구조화 상태는 발생 시각 이전 가장 가까운 world_state 의 관련 트랙·트랙 수다."""
    seen: set[str] = set()
    out: list[dict[str, Any]] = []
    for line in risk_events:
        e = line["payload"]
        if e["event_id"] in seen:
            continue
        seen.add(e["event_id"])
        t = e["raised_at"]
        ws = _nearest_world_state(world_states, t)
        tracks = list(ws["payload"]["tracks"]) if ws else []
        related = [tr for tr in tracks if tr.get("world_id") in e["world_ids"]]
        cams = {c.get("camera_id") if isinstance(c, Mapping) else c for tr in related for c in tr.get("cameras") or ()}
        out.append({
            "event_id": e["event_id"],
            "input": {
                "event_id": e["event_id"], "event_sequence": e["event_sequence"], "rule_id": e["rule_id"],
                "rule_version": e["rule_version"], "severity": e["severity"], "confidence": e["confidence"],
                "needs_check": e["needs_check"], "confidence_reasons": list(e.get("confidence_reasons") or ()),
                "world_ids": list(e["world_ids"]), "zone_id": e["zone_id"], "metrics": dict(e["metrics"]),
                "observation_time": t,
                "structured_state": {"tracks": related, "track_count": len(tracks),
                                     "calibration_version": e.get("calibration_version"), "scene_version": e.get("scene_version")},
                "world_state_ref": ws.get("msg_id") if ws else None,
            },
            "strata": {
                "rule_id": e["rule_id"], "severity": e["severity"],
                "check": "확인 필요" if e.get("confidence_reasons") or e["needs_check"] else "확정",
                "cameras": "모름" if not related else ("2대 이하" if len(cams) <= 2 else "3대 이상"),
                "people": "0" if not tracks else ("1" if len(tracks) == 1 else "2+"),
            },
            "truth": (truth or {}).get(e["event_id"]),
        })
    return out


def filter_candidates(cands: Iterable[Mapping[str, Any]], rules: Sequence[str] | None,
                      severities: Sequence[str] | None) -> list[Mapping[str, Any]]:
    return [c for c in cands if (not rules or c["strata"]["rule_id"] in rules)
            and (not severities or c["strata"]["severity"] in severities)]


def stratify(cands: Sequence[Mapping[str, Any]], total: int, by: Sequence[str] = DEFAULT_BY,
             seed: int = 0) -> list[Mapping[str, Any]]:
    """by 차원의 칸마다 섞고 칸을 돌아가며 하나씩 뽑는다. 결과는 같은 seed 면 같다."""
    rng = random.Random(seed)
    cells: dict[tuple, list[Mapping[str, Any]]] = {}
    for c in sorted(cands, key=lambda c: c["event_id"]):
        cells.setdefault(tuple(c["strata"][d] for d in by), []).append(c)
    for v in cells.values():
        rng.shuffle(v)
    picked: list[Mapping[str, Any]] = []
    queues = [cells[k] for k in sorted(cells)]
    while len(picked) < total and any(queues):
        for q in queues:
            if q and len(picked) < total:
                picked.append(q.pop())
    return picked


def distribution(samples: Iterable[Mapping[str, Any]]) -> dict[str, dict[str, int]]:
    out: dict[str, dict[str, int]] = {d: {} for d in DIMENSIONS}
    for s in samples:
        for d in DIMENSIONS:
            v = s["strata"][d]
            out[d][v] = out[d].get(v, 0) + 1
    return {d: dict(sorted(v.items())) for d, v in out.items()}


# ---------------------------------------------------------------- 프레임


def read_headers(session: Path, camera_ids: Sequence[str] | None = None) -> dict[str, list[dict[str, Any]]]:
    replay = RecordingReplay(session, camera_ids)
    out: dict[str, list[dict[str, Any]]] = {c: [] for c in replay.camera_ids}
    try:
        for msg in replay.pop_due(2**62):
            if isinstance(msg.message, I0Frame):
                h = msg.message.header
                out[msg.camera_id].append({"key": (h.stream_epoch, h.frame_sequence), "capture_time_ms": h.estimated_capture_time_ms,
                                           "timestamp_source": h.timestamp_source,
                                           "timestamp_uncertainty_ms": h.timestamp_uncertainty_ms})
    finally:
        replay.close()
    return out


def plan_frames(headers: Mapping[str, Sequence[Mapping[str, Any]]], t_ms: int, offsets: Sequence[int],
                tolerance_ms: int = DEFAULT_TOLERANCE_MS) -> list[dict[str, Any]]:
    """(카메라, 시각) 마다 가장 가까운 프레임(같으면 이른 것). 기준 밖이면 넣지 않는다."""
    out = []
    for cid in sorted(headers):
        for off in offsets:
            want = t_ms + off
            best = min(headers[cid], key=lambda h: (abs(h["capture_time_ms"] - want), h["capture_time_ms"]), default=None)
            if best is not None and abs(best["capture_time_ms"] - want) <= tolerance_ms:
                out.append({"camera_id": cid, "offset_ms": off, "key": best["key"],
                            "estimated_capture_time_ms": best["capture_time_ms"], "timestamp_source": best["timestamp_source"],
                            "timestamp_uncertainty_ms": best["timestamp_uncertainty_ms"]})
    return out


def decode_frames(session: Path, wanted: Mapping[tuple[str, tuple[int, int]], list[Path]], writer: Writer,
                  decoder_factory: Callable[[], Decoder] = PyAvDecoder) -> int:
    """고른 프레임만 쓴다. H.264 는 앞 프레임에 기대므로 카메라마다 처음부터 디코딩한다. 쓴 개수를 돌려준다."""
    written = 0
    for cid in sorted({k[0] for k in wanted}):
        replay = RecordingReplay(session, [cid])
        dec = decoder_factory()

        def emit(pictures, cid=cid) -> None:
            nonlocal written
            for pic in pictures:
                for path in wanted.get((cid, tuple(pic.key)), ()):
                    path.parent.mkdir(parents=True, exist_ok=True)
                    writer(path, pic.image)
                    written += 1

        try:
            for msg in replay.pop_due(2**62):
                m = msg.message
                if isinstance(m, I0CodecInfo):
                    dec.configure(m)
                elif isinstance(m, I0Frame):
                    try:
                        emit(dec.decode(m.payload, m.header.key, m.header.keyframe))
                    except DecodeError:
                        pass  # 그 프레임은 비운다(다른 프레임으로 채우지 않음)
                elif m is None:
                    emit(dec.flush())
            emit(dec.flush())
        finally:
            replay.close()
    return written


def jpeg_writer(quality: int = 95) -> Writer:
    import cv2  # extras io

    def write(path: Path, bgr) -> None:
        if not cv2.imwrite(str(path), bgr, [cv2.IMWRITE_JPEG_QUALITY, quality]):
            raise OSError(f"{path}: JPEG 를 쓰지 못했다")

    return write


# ---------------------------------------------------------------- 묶음


def _sha256(path: Path) -> str:
    return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()


def build_bundle(picked: Sequence[Mapping[str, Any]], out: Path, *, source: Mapping[str, Any],
                 headers: Mapping[str, Sequence[Mapping[str, Any]]] | None, session: Path | None,
                 writer: Writer | None, offsets: Sequence[int] = DEFAULT_OFFSETS_MS,
                 tolerance_ms: int = DEFAULT_TOLERANCE_MS, face_policy: str = "BLURRED",
                 selection: Mapping[str, Any] | None = None, considered: int | None = None) -> dict[str, Any]:
    """samples/<id>/{input.json, truth.json, frames/*.jpg} 와 manifest.json. out 이 있으면 덮어쓰지 않는다."""
    out.mkdir(parents=True, exist_ok=False)
    wanted: dict[tuple[str, tuple[int, int]], list[Path]] = {}
    samples = []
    for i, c in enumerate(picked):
        sid = f"g7-{i + 1:03d}"
        sdir = out / "samples" / sid
        sdir.mkdir(parents=True)
        frames = []
        for f in plan_frames(headers or {}, c["input"]["observation_time"], offsets, tolerance_ms):
            name = f"frames/{f['camera_id']}_{'m' if f['offset_ms'] < 0 else 'p'}{abs(f['offset_ms'])}.jpg"
            wanted.setdefault((f["camera_id"], tuple(f["key"])), []).append(sdir / name)
            frames.append({"camera_id": f["camera_id"], "file": name, "offset_ms": f["offset_ms"],
                           "estimated_capture_time_ms": f["estimated_capture_time_ms"], "timestamp_source": f["timestamp_source"],
                           "timestamp_uncertainty_ms": f["timestamp_uncertainty_ms"], "face_policy": face_policy})
        samples.append((sid, sdir, c, frames))
    if wanted:
        if session is None or writer is None:
            raise ValueError("프레임을 쓰려면 녹화 세션과 writer 가 필요하다")
        decode_frames(session, wanted, writer)
    entries = []
    for sid, sdir, c, frames in samples:
        frames = [f for f in frames if (sdir / f["file"]).is_file()]  # 디코딩 못 한 프레임은 뺀다(채우지 않음)
        for f in frames:
            f["sha256"] = _sha256(sdir / f["file"])
        inp = {**c["input"], "frames": frames}
        (sdir / "input.json").write_text(json.dumps(inp, ensure_ascii=False, indent=1), encoding="utf-8")
        (sdir / "truth.json").write_text(json.dumps(c["truth"], ensure_ascii=False, indent=1), encoding="utf-8")
        entries.append({"sample_id": sid, "event_id": c["event_id"], "strata": c["strata"], "frames": len(frames),
                        "truth": c["truth"] is not None,
                        "input_sha256": _sha256(sdir / "input.json")})
    manifest = {
        "format": FORMAT, "format_version": FORMAT_VERSION, "source": dict(source),
        "product_metric": source.get("kind") == "RECORDING" and source.get("is_test_input") is False,
        "selection": dict(selection or {}), "considered": considered, "count": len(entries),
        "missing_truth": sum(not e["truth"] for e in entries), "no_frames": sum(e["frames"] == 0 for e in entries),
        "distribution": distribution(picked), "samples": entries,
    }
    (out / MANIFEST_FILE).write_text(json.dumps(manifest, ensure_ascii=False, indent=1), encoding="utf-8")
    return manifest


def main(argv: list[str] | None = None, writer: Writer | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--replay", type=Path, required=True, help="risk_event.jsonl·world_state.jsonl 이 있는 재생 출력 폴더")
    p.add_argument("--session", type=Path, help="녹화 세션(프레임). 없으면 프레임 없이 묶는다(시나리오 골든 리허설)")
    p.add_argument("--source-kind", choices=("RECORDING", "SIM"), default="RECORDING")
    p.add_argument("--truth", type=Path, help="사람이 쓴 정답 요약 JSONL(event_id, summary)")
    p.add_argument("--out", type=Path, required=True)
    p.add_argument("--total", type=int, default=100)
    p.add_argument("--by", default=",".join(DEFAULT_BY))
    p.add_argument("--rules", default=",".join(FAST_PATH_RULES))
    p.add_argument("--severities", default=",".join(FAST_PATH_SEVERITIES), help="빈 문자열이면 모든 등급")
    p.add_argument("--offsets-ms", default=",".join(map(str, DEFAULT_OFFSETS_MS)))
    p.add_argument("--tolerance-ms", type=int, default=DEFAULT_TOLERANCE_MS)
    p.add_argument("--seed", type=int, default=0)
    a = p.parse_args(argv)
    by = tuple(x for x in a.by.split(",") if x)
    if bad := [d for d in by if d not in DIMENSIONS]:
        p.error(f"--by 는 {DIMENSIONS} 중에서: {bad}")
    truth = {t["event_id"]: t for t in read_jsonl(a.truth)} if a.truth else None
    cands = candidates_from_replay(read_jsonl(a.replay / "risk_event.jsonl"), read_jsonl(a.replay / "world_state.jsonl"), truth)
    rules = [x for x in a.rules.split(",") if x]
    sevs = [x for x in a.severities.split(",") if x]
    pool = filter_candidates(cands, rules, sevs)
    picked = stratify(pool, a.total, by, a.seed)
    source: dict[str, Any] = {"kind": a.source_kind, "replay_files": {
        n: _sha256(a.replay / n) for n in ("risk_event.jsonl", "world_state.jsonl")}}
    headers = None
    if a.session:
        m = load_recording_manifest(a.session)
        source.update(session_id=m["session_id"], is_test_input=m["is_test_input"], scenario=m.get("scenario"))
        headers = read_headers(a.session)
    else:
        source["is_test_input"] = None
    manifest = build_bundle(picked, a.out, source=source, headers=headers, session=a.session,
                            writer=writer or (jpeg_writer() if a.session else None),
                            offsets=tuple(int(x) for x in a.offsets_ms.split(",")), tolerance_ms=a.tolerance_ms,
                            selection={"by": by, "rules": rules, "severities": sevs, "seed": a.seed, "total": a.total},
                            considered=len(pool))
    print(json.dumps({k: manifest[k] for k in ("count", "considered", "missing_truth", "no_frames", "product_metric",
                                               "distribution")}, ensure_ascii=False))
    if manifest["count"] < a.total:
        print(f"후보가 모자라 {manifest['count']}건만 골랐다(요청 {a.total})", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
