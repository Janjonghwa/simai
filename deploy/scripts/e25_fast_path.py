#!/usr/bin/env python3
"""빠른 경로 종단 확인 (GPU 세션 큐 e25, S15P31A101-25). 이벤트 관측 → EC2 근거 저장 p95 를 실제 EC2 DB 시각으로 낸다.

LIVE 하네스(무늬 영상, EC2 없음)와 file 입력(service.py: 이벤트 프레임을 조립하지 않음)으로는 event_frame_ready 가 나오지 않는다.
그래서 이 항목은 스스로 띄우지 않고, 이미 도는 lobby LIVE(tier0·vllm·worker, EC2 dev 연결) + 입력(relay 녹화 재생 또는 실제 카메라)을 지켜본다.

    # Pod: lobby LIVE 와 relay 를 켠 뒤 (LIVE 중이라 --allow-live)
    simai-bootstrap queue --image unified --only e25 --allow-live --session <이름>
    # 노트북: 결과 묶음을 받은 뒤
    python deploy/scripts/e25_fast_path.py sql --summary <묶음>/runs/e25/<run>/pod_summary.json > e25.sql
    ssh simai-ec2 'cd /opt/simai && docker compose --env-file .env -f repo/deploy/ec2/compose.yaml exec -T db \
        psql -U postgres -d simai -v ON_ERROR_STOP=1 --csv' < e25.sql > e25_db.csv
    python deploy/scripts/e25_fast_path.py report --summary <…>/pod_summary.json --db-csv e25_db.csv --input relay --out e25_result.json

판정(report, 대상은 창 안에서 RAISED 된 LIVE R1~R4 CRITICAL 전부):
  PASS: 표본 ≥ 30, p95 ≤ 5000 ms, 하향·직접 변경·상향 의견·조항·사후 프레임·클립 입력·이벤트당 2작업 모두 0. 종료 0
  FAIL: 위반이 있거나 표본 ≥ 30 에서 p95 초과. 종료 1
  INSUFFICIENT: 위반은 없지만 표본 < 30. 종료 3 (2 = 사용·입력 오류)
p95 분모에는 근거가 없는 이벤트(작업 없음·실패·마감 초과)를 무한대로 넣는다. 성공 건만 세면 좋아 보이기 때문이다(성공 건 p95 는 따로 적는다).
결과에는 지연·개수·이벤트 ID 만 남긴다. 근거 문장·그림·토큰은 다루지 않는다.
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import os
import re
import sys
import time
from pathlib import Path

G7_P95_MS = 5000
MIN_SAMPLES = 30
MARKER_MAX_AGE_S = 30.0
EXIT_PASS, EXIT_FAIL, EXIT_USAGE, EXIT_INSUFFICIENT = 0, 1, 2, 3
SEVERITY_RANK = {"INFO": 0, "CAUTION": 1, "HIGH": 2, "CRITICAL": 3}
BASELINE = {"p95_s": 2.12, "scope": "3차 세션 vlm_int: 그림 1장, VLM 호출 단독(얼굴 처리·덧그림·프레임 읽기·대여·저장 없음)"}
SCOPE = "이벤트 관측 → EC2 저장: 프레임 4장 JPEG·로컬 사본·서명 + I1 + 작업 등록·대여 + 얼굴 처리 + 덧그림 + VLM + 콜백 저장"

READY = re.compile(r"event_frames_ready event_id=(\S+) frames=(\d+) local=(\d+) event_to_ready_ms=(-?\d+)")
FRAME_FAILED = re.compile(r"event_frames_failed event_id=(\S+) reason=(\S+)")
DONE = re.compile(r"이벤트 (\S+): 근거 완료 frames=(\d+) gate_ms=(\d+) vlm_ms=(\d+) total_ms=(\d+)")
COUNTED = {"local_rejected": "로컬 사본을 쓰지 않는다", "late_frames": "observation_time 뒤 프레임",
           "face_dropped": "얼굴 처리 못 한 프레임", "gate_closed": "Tier 0 우선 접수 문: 받지 않음",
           "upload_failed": "event_frames_upload_failed"}


def parse_logs(tier0: str, worker: str) -> dict:
    """로그 조각 → 이벤트별 구간과 경고 수."""
    ready = {m[1]: {"frames": int(m[2]), "local": int(m[3]), "event_to_ready_ms": int(m[4])} for m in READY.finditer(tier0)}
    done = {m[1]: {"frames": int(m[2]), "gate_ms": int(m[3]), "vlm_ms": int(m[4]), "total_ms": int(m[5])} for m in DONE.finditer(worker)}
    both = tier0 + "\n" + worker
    return {"ready": ready, "done": done, "frame_failed": [{"event_id": m[1], "reason": m[2]} for m in FRAME_FAILED.finditer(tier0)],
            "counts": {k: both.count(text) for k, text in COUNTED.items()}}


def _tail(path: Path, offset: int) -> str:
    try:
        with open(path, "rb") as f:
            f.seek(offset)
            return f.read().decode("utf-8", "replace")
    except OSError:
        return ""


def _size(path: Path) -> int:
    try:
        return path.stat().st_size
    except OSError:
        return 0


def cmd_watch(args) -> int:
    """lobby 가 도는지 보고, 이벤트가 찰 때까지 로그를 읽어 pod_summary.json 을 쓴다. 표본 부족도 파일은 남긴다."""
    try:
        age = time.time() - args.marker.stat().st_mtime
    except OSError:
        age = math.inf
    if age > MARKER_MAX_AGE_S:
        print(f"e25: LIVE 가 아니다({args.marker} 갱신 {age:.0f} s 전). lobby 와 relay 를 켠 뒤 --only e25 --allow-live 로 돈다", file=sys.stderr)
        return EXIT_USAGE
    args.out.mkdir(parents=True, exist_ok=True)
    tier0_log, worker_log = args.logs / "tier0.log", args.logs / "worker.log"
    start = (_size(tier0_log), _size(worker_log))  # 로그는 이어 쓰므로 지난 실행은 뺀다
    since = time.time_ns() // 1_000_000
    end = time.monotonic() + args.max_s
    parsed = parse_logs("", "")
    while time.monotonic() < end and len(parsed["ready"]) < args.min_events:
        time.sleep(args.poll_s)
        parsed = parse_logs(_tail(tier0_log, start[0]), _tail(worker_log, start[1]))
        print(f"e25: 프레임 알림 {len(parsed['ready'])}/{args.min_events}, 근거 완료 {len(parsed['done'])}", flush=True)
    time.sleep(args.settle_s)  # 마지막 이벤트의 근거(마감 5 s)와 콜백을 기다린다
    parsed = parse_logs(_tail(tier0_log, start[0]), _tail(worker_log, start[1]))
    until = time.time_ns() // 1_000_000
    (args.out / "pod_summary.json").write_text(
        json.dumps({"since_ms": since, "until_ms": until, **parsed}, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    (args.out / "metrics.json").write_text(
        json.dumps({"ready": len(parsed["ready"]), "done": len(parsed["done"]), **parsed["counts"]}) + "\n", encoding="utf-8")
    enough = len(parsed["ready"]) >= args.min_events
    print(f"e25: 창 {since}~{until}, 프레임 알림 {len(parsed['ready'])}, 근거 완료 {len(parsed['done'])}. "
          + ("노트북에서 sql → report 로 판정한다" if enough else f"표본 부족(목표 {args.min_events})"), flush=True)
    return EXIT_PASS if enough else EXIT_FAIL


SQL = """-- e25 읽기 전용. 창 {since_ms}~{until_ms} 에 RAISED 된 LIVE R1~R4 CRITICAL 이벤트마다 한 줄(작업·근거가 없어도 나온다).
SELECT e.event_id, e.rule_id, e.severity, u.severity AS tier0_severity,
    j.job_id, j.status AS job_status, j.attempt, j.error->>'code' AS error_code,
    a.rule_severity, a.proposed_severity, cardinality(a.clause_ids) AS clause_count,
    a.event_to_rationale_ms, a.within_deadline, a.latency_ms,
    (SELECT count(*) FROM jsonb_array_elements(j.input->'frames')) AS frames,
    (SELECT count(*) FROM jsonb_array_elements(j.input->'frames') f WHERE f ? 'local_copy') AS frames_local,
    (SELECT count(*) FROM jsonb_array_elements(j.input->'frames') f
        WHERE (f->>'estimated_capture_time_ms')::bigint > (j.input->>'observation_time')::bigint) AS frames_after_observation,
    (SELECT count(*) FROM jsonb_object_keys(j.input) k WHERE k ILIKE '%clip%') AS clip_keys,
    jsonb_array_length(j.input->'structured_state'->'tracks') AS tracks,
    (SELECT count(*) FROM analysis_job x WHERE x.event_id = e.event_id AND x.job_type = 'vlm_rationale') AS jobs
FROM risk_event e
LEFT JOIN risk_event_update u ON u.event_id = e.event_id AND u.event_sequence = e.event_sequence
LEFT JOIN analysis_job j ON j.event_id = e.event_id AND j.job_type = 'vlm_rationale'
LEFT JOIN vlm_assessment a ON a.job_id = j.job_id
WHERE e.source = 'LIVE' AND e.rule_id IN ('R1', 'R2', 'R3', 'R4') AND COALESCE(e.delivery, '') <> 'AUDIT_ONLY'
  AND e.raised_at >= to_timestamp({since_ms} / 1000.0) AND e.raised_at <= to_timestamp({until_ms} / 1000.0)
  AND EXISTS (SELECT 1 FROM risk_event_update r WHERE r.event_id = e.event_id AND r.transition = 'RAISED' AND r.severity = 'CRITICAL')
ORDER BY e.raised_at;
"""


def cmd_sql(args) -> int:
    summary = json.loads(args.summary.read_text(encoding="utf-8"))
    sys.stdout.write(SQL.format(since_ms=int(summary["since_ms"]), until_ms=int(summary["until_ms"])))
    return EXIT_PASS


def p95(values: list[float]) -> float | None:
    """최근접 순위(g7_score.p95_ms 와 같은 정의)."""
    v = sorted(values)
    return v[math.ceil(0.95 * len(v)) - 1] if v else None


def _number(text: str | None) -> int:
    return int(text) if text else 0


def judge(rows: list[dict], summary: dict, input_kind: str) -> dict:
    latencies = [float(r["event_to_rationale_ms"]) for r in rows if r["event_to_rationale_ms"]]
    all_events = latencies + [math.inf] * (len(rows) - len(latencies))
    rank = SEVERITY_RANK
    violations = {
        "downgrades": sum(bool(r["rule_severity"]) and rank[r["severity"]] < rank[r["rule_severity"]] for r in rows),
        "direct_severity_changes": sum(r["severity"] != r["tier0_severity"] for r in rows),  # 현재 등급이 마지막 Tier 0 전이와 다르다
        "proposed_severity_not_null": sum(bool(r["proposed_severity"]) for r in rows),
        "clause_ids_not_empty": sum(_number(r["clause_count"]) > 0 for r in rows),
        "frames_after_observation": sum(_number(r["frames_after_observation"]) for r in rows),
        "clip_input_keys": sum(_number(r["clip_keys"]) for r in rows),
        "events_with_multiple_jobs": sum(_number(r["jobs"]) > 1 for r in rows),
    }
    worst = p95(all_events)
    if any(violations.values()) or (len(rows) >= MIN_SAMPLES and worst > G7_P95_MS):
        verdict = "FAIL"
    elif len(rows) < MIN_SAMPLES:
        verdict = "INSUFFICIENT"
    else:
        verdict = "PASS"
    statuses: dict[str, int] = {}
    for r in rows:
        key = r["job_status"] or "NO_JOB"
        statuses[key] = statuses.get(key, 0) + 1
    ready, done = summary.get("ready", {}), summary.get("done", {})
    segment = {name: p95([s[name] for s in source.values()]) for name, source in
               (("event_to_ready_ms", ready), ("gate_ms", done), ("vlm_ms", done), ("total_ms", done))}
    return {
        "verdict": verdict, "input": input_kind, "samples": len(rows), "min_samples": MIN_SAMPLES, "threshold_ms": G7_P95_MS,
        "p95_all_events_ms": None if worst is None or math.isinf(worst) else worst,
        "p95_all_events_unbounded": worst is not None and math.isinf(worst),  # 근거 없는 이벤트가 5% 를 넘는다
        "p95_succeeded_ms": p95(latencies), "succeeded": len(latencies),
        "within_deadline": sum(r["within_deadline"] == "t" for r in rows),
        "job_status": statuses, "violations": violations,
        # 프레임 알림이 전이보다 먼저 EC2 에 닿으면 작업 없이 끝난다(VlmRationaleIngestor). 0 이 아니면 박종화·곽나혁에게 알린다
        "events_without_job": statuses.get("NO_JOB", 0),
        "jobs_with_empty_tracks": sum(bool(r["job_id"]) and _number(r["tracks"]) == 0 for r in rows),
        "frames_not_local": sum(_number(r["frames"]) - _number(r["frames_local"]) for r in rows),
        # 저장 지연이 워커 처리보다 짧으면 Tier 0(노트북 촬영 기준)과 EC2 시계가 어긋난 것이다
        "clock_suspect": sum(bool(r["event_to_rationale_ms"]) and bool(r["latency_ms"])
                             and int(r["event_to_rationale_ms"]) < int(r["latency_ms"]) for r in rows),
        "segments_p95_ms": segment, "pod_counts": summary.get("counts", {}), "frame_failed": len(summary.get("frame_failed", [])),
        "scope": SCOPE, "baseline_not_comparable": BASELINE,
        "note": "관측 시각은 노트북 기준 촬영 시각, 저장 시각은 EC2 시계다. 시계 차이가 그대로 들어간다(clock_suspect 로 본다)",
    }


def cmd_report(args) -> int:
    summary = json.loads(args.summary.read_text(encoding="utf-8"))
    with open(args.db_csv, newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    result = judge(rows, summary, args.input)
    text = json.dumps(result, ensure_ascii=False, indent=2) + "\n"
    if args.out:
        args.out.write_text(text, encoding="utf-8")
    sys.stdout.write(text)
    return {"PASS": EXIT_PASS, "FAIL": EXIT_FAIL, "INSUFFICIENT": EXIT_INSUFFICIENT}[result["verdict"]]


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    root = Path(os.environ.get("SIMAI_ROOT") or "/workspace/simai")
    ap = argparse.ArgumentParser(description="빠른 경로 종단 확인 e25 (S15P31A101-25)")
    sub = ap.add_subparsers(dest="cmd", required=True)
    w = sub.add_parser("watch", help="Pod: lobby LIVE 로그에서 이벤트를 모은다")
    w.add_argument("--out", type=Path, required=True)
    w.add_argument("--logs", type=Path, default=root / "logs")
    w.add_argument("--marker", type=Path, default=root / "runs" / "lobby-live" / "state" / "tier0.live")
    w.add_argument("--min-events", type=int, default=40, help="작업이 없거나 대상 밖 이벤트가 있어도 표본 30 이 남게 넉넉히")
    w.add_argument("--max-s", type=float, default=2400)
    w.add_argument("--poll-s", type=float, default=10)
    w.add_argument("--settle-s", type=float, default=15)
    w.set_defaults(fn=cmd_watch)
    s = sub.add_parser("sql", help="노트북: 창에 맞는 읽기 전용 SQL 을 출력한다")
    s.add_argument("--summary", type=Path, required=True)
    s.set_defaults(fn=cmd_sql)
    r = sub.add_parser("report", help="노트북: DB CSV 와 Pod 요약으로 판정한다")
    r.add_argument("--summary", type=Path, required=True)
    r.add_argument("--db-csv", type=Path, required=True)
    r.add_argument("--input", choices=("relay", "live"), required=True, help="입력 출처(녹화 재생은 실시간 도착 흔들림이 없다)")
    r.add_argument("--out", type=Path)
    r.set_defaults(fn=cmd_report)
    args = ap.parse_args(argv)
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
