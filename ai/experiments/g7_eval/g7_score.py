"""G7 채점표 내보내기·집계 (노트북 CPU, S15P31A101-35, 설계 docs/design/g7-eval.md, 명세 15.1 G7).

근거 설명(vlm_rationale):
    sheet     표본 묶음 + answers.jsonl → 평가자별 CSV·HTML(가린 번호, 평가자마다 순서 다름) + key_map.json(진행자만)
    aggregate 채점 CSV들 → g7_result.json·요약 표: 근거-상태 일치율(≥95%), 평가자 간 일치도(단순 일치·Cohen κ), 층별, 지연 p95(≤5 s)
보고서 인용(tbm_report):
    cite-sheet     job 묶음 JSONL({"input": tbm_report 입력, "result": 결과}) → 인용 존재 자동 검사 + 적절성 채점표
    cite-aggregate 적절성 채점 CSV들 → 적절률(≥80%)·존재율(100%)

채점표에는 모델 이름·프롬프트·지연·토큰을 싣지 않는다(평가자는 구현 정보를 모른다). 일치 판정은 verdict 가 "일치" 이고
지어냄·등급변경이 모두 N 일 때만 일치다. 평가자 판정이 갈리면 조정표(adjudication CSV)의 값을 쓰고, 없으면 불일치로 센다(보수).
"""

from __future__ import annotations

import argparse
import csv
import html
import json
import math
import random
from collections.abc import Iterable, Mapping, Sequence
from itertools import combinations
from pathlib import Path
from typing import Any

from simai_ai.report.tbm import check_draft

RATIONALE_PASS = 0.95
CITE_APPROPRIATE_PASS = 0.80
LATENCY_P95_MS = 5000
MIN_SAMPLES = 100
VERDICTS = ("일치", "부분", "불일치")
YES_NO = ("Y", "N")
SHEET_HIDDEN = ("model", "prompt", "latency_ms", "input_tokens", "output_tokens", "finish_reason", "gate")
RATIONALE_COLUMNS = ("key", "구조화 상태", "정답 요약", "VLM 답", "verdict(일치/부분/불일치)", "지어냄(Y/N)", "등급변경(Y/N)", "메모")
CITE_COLUMNS = ("key", "위험 항목·문단", "인용 조항", "조항 본문", "적절(Y/N)", "메모")


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    with path.open(encoding="utf-8") as f:
        return [json.loads(line) for line in f if line.strip()]


def _write_csv(path: Path, columns: Sequence[str], rows: Iterable[Sequence[Any]]) -> None:
    with path.open("x", encoding="utf-8-sig", newline="") as f:  # 엑셀이 한글을 읽게 BOM
        w = csv.writer(f)
        w.writerow(columns)
        w.writerows(rows)


def _read_csv(path: Path) -> list[dict[str, str]]:
    with path.open(encoding="utf-8-sig", newline="") as f:
        return [{k: (v or "").strip() for k, v in r.items()} for r in csv.DictReader(f)]


def _state_text(inp: Mapping[str, Any]) -> str:
    keep = ("rule_id", "severity", "confidence_reasons", "zone_id", "metrics", "structured_state")
    return json.dumps({k: inp.get(k) for k in keep}, ensure_ascii=False, sort_keys=True)


# ---------------------------------------------------------------- 근거 설명 채점표


def export_rationale_sheets(bundle: Path, answers: Path, out: Path, raters: Sequence[str], seed: int = 0) -> dict[str, Any]:
    """답이 있는 표본만 싣는다(오류 표본은 key_map 의 excluded 에 사유와 함께 남긴다)."""
    out.mkdir(parents=True, exist_ok=False)
    rows = {a["sample_id"]: a for a in read_jsonl(answers)}
    ids = sorted(rows)
    keyed = [sid for sid in ids if rows[sid].get("content")]
    order = random.Random(seed).sample(keyed, len(keyed))
    key_map = {f"s{i + 1:03d}": sid for i, sid in enumerate(order)}
    by_id = {v: k for k, v in key_map.items()}
    lines = {}
    for sid in keyed:
        sdir = bundle / "samples" / sid
        inp = json.loads((sdir / "input.json").read_text(encoding="utf-8"))
        truth = json.loads((sdir / "truth.json").read_text(encoding="utf-8"))
        lines[sid] = (by_id[sid], _state_text(inp), (truth or {}).get("summary", "정답 요약 없음"), rows[sid]["content"])
    for r in raters:
        mine = random.Random(f"{seed}:{r}").sample(keyed, len(keyed))
        _write_csv(out / f"rationale_{r}.csv", RATIONALE_COLUMNS, [(*lines[s], "", "", "", "") for s in mine])
        _write_html(out / f"rationale_{r}.html", [(lines[s], answers.parent / "blurred" / s) for s in mine], out)
    (out / "key_map.json").write_text(json.dumps({
        "keys": key_map, "excluded": {sid: (rows[sid].get("error") or {}).get("code") for sid in ids if sid not in keyed},
        "prompt": next(iter(rows.values()))["prompt"] if rows else None}, ensure_ascii=False, indent=1), encoding="utf-8")
    return {"rated": len(keyed), "excluded": len(ids) - len(keyed), "raters": list(raters)}


def _write_html(path: Path, items: Sequence[tuple[tuple[str, str, str, str], Path]], out: Path) -> None:
    """평가자가 보는 화면: 흐린 그림·구조화 상태·정답 요약·답. 채점은 같은 이름 CSV 에 적는다."""
    parts = ["<!doctype html><meta charset='utf-8'><title>G7 근거 채점</title>",
             ("<style>body{font-family:sans-serif;max-width:1100px;margin:auto}img{max-width:48%;margin:2px}"
              "section{border-top:1px solid #999;padding:8px 0}pre{white-space:pre-wrap;background:#f4f4f4;padding:6px}</style>"),
             "<p>채점은 같은 이름의 CSV 에 key 별로 적는다. 기준표: docs/design/g7-eval.md \"채점 기준표\".</p>"]
    for (key, state, truth, answer), blurred in items:
        imgs = "".join(f"<img src='{html.escape(p.resolve().as_uri())}'>" for p in sorted(blurred.glob("*.jpg")))
        parts.append(f"<section><h3>{key}</h3>{imgs}<h4>구조화 상태</h4><pre>{html.escape(state)}</pre>"
                     f"<h4>정답 요약</h4><p>{html.escape(truth)}</p><h4>답</h4><pre>{html.escape(answer)}</pre></section>")
    path.write_text("\n".join(parts), encoding="utf-8")


# ---------------------------------------------------------------- 집계


def rationale_label(row: Mapping[str, str]) -> bool | None:
    """일치면 True. 채점이 비었거나 값이 틀리면 None(미채점)."""
    v, fab, sev = row.get(RATIONALE_COLUMNS[4], ""), row.get(RATIONALE_COLUMNS[5], ""), row.get(RATIONALE_COLUMNS[6], "")
    if v not in VERDICTS or fab not in YES_NO or sev not in YES_NO:
        return None
    return v == "일치" and fab == "N" and sev == "N"


def cohen_kappa(a: Sequence[bool], b: Sequence[bool]) -> float | None:
    """두 평가자 이진 판정의 κ. 둘 다 한 값만 썼으면(기대 일치 1) 정의되지 않아 None."""
    n = len(a)
    if n == 0 or n != len(b):
        return None
    po = sum(x == y for x, y in zip(a, b, strict=True)) / n
    pa, pb = sum(a) / n, sum(b) / n
    pe = pa * pb + (1 - pa) * (1 - pb)
    return None if pe == 1 else (po - pe) / (1 - pe)


def p95_ms(values: Iterable[float]) -> float | None:
    """최근접 순위(nearest-rank) p95."""
    v = sorted(values)
    return v[math.ceil(0.95 * len(v)) - 1] if v else None


def consensus(per_rater: Mapping[str, Mapping[str, bool | None]], adjudication: Mapping[str, bool] | None = None
              ) -> tuple[dict[str, bool], list[str], list[str]]:
    """key → 최종 판정. 평가자가 모두 같으면 그 값, 다르면 조정값, 조정이 없으면 False(보수). (판정, 갈린 key, 미채점 key)."""
    keys = sorted({k for m in per_rater.values() for k in m})
    final, split, missing = {}, [], []
    for k in keys:
        votes = [m.get(k) for m in per_rater.values()]
        if any(v is None for v in votes):
            missing.append(k)
            final[k] = False
        elif len(set(votes)) == 1:
            final[k] = bool(votes[0])
        else:
            split.append(k)
            final[k] = bool((adjudication or {}).get(k, False))
    return final, split, missing


def agreement(per_rater: Mapping[str, Mapping[str, bool | None]]) -> list[dict[str, Any]]:
    out = []
    for r1, r2 in combinations(sorted(per_rater), 2):
        keys = [k for k in per_rater[r1] if per_rater[r1].get(k) is not None and per_rater[r2].get(k) is not None]
        a = [bool(per_rater[r1][k]) for k in keys]
        b = [bool(per_rater[r2][k]) for k in keys]
        out.append({"raters": [r1, r2], "n": len(keys),
                    "simple": (sum(x == y for x, y in zip(a, b, strict=True)) / len(keys)) if keys else None,
                    "kappa": cohen_kappa(a, b)})
    return out


def _load_ratings(score_csvs: Sequence[Path], label) -> dict[str, dict[str, bool | None]]:
    out = {}
    for p in score_csvs:
        rater = p.stem.split("_", 1)[1] if "_" in p.stem else p.stem
        out[rater] = {r["key"]: label(r) for r in _read_csv(p)}
    return out


def _adjudication(path: Path | None, label) -> dict[str, bool] | None:
    if path is None:
        return None
    return {r["key"]: bool(label(r)) for r in _read_csv(path) if label(r) is not None}


def aggregate_rationale(bundle: Path, answers: Path, sheets: Path, score_csvs: Sequence[Path],
                        adjudication_csv: Path | None = None, latency_summary: Path | None = None) -> dict[str, Any]:
    key_map = json.loads((sheets / "key_map.json").read_text(encoding="utf-8"))
    manifest = json.loads((bundle / "manifest.json").read_text(encoding="utf-8"))
    strata = {s["sample_id"]: s["strata"] for s in manifest["samples"]}
    per_rater = _load_ratings(score_csvs, rationale_label)
    final, split, missing = consensus(per_rater, _adjudication(adjudication_csv, rationale_label))
    n = len(final)
    rate = sum(final.values()) / n if n else None
    by_stratum: dict[str, dict[str, dict[str, Any]]] = {}
    for key, ok in final.items():
        for dim, val in strata[key_map["keys"][key]].items():
            cell = by_stratum.setdefault(dim, {}).setdefault(val, {"n": 0, "match": 0})
            cell["n"] += 1
            cell["match"] += ok
    for cells in by_stratum.values():
        for c in cells.values():
            c["rate"] = c["match"] / c["n"]
    rows = read_jsonl(answers)
    if latency_summary is not None:  # vlm_int.py summary.json(지속 동시 4 전체 p95, 초)
        s = json.loads(latency_summary.read_text(encoding="utf-8"))
        p95 = (s["requests"]["sustained4"]["total_p95"] or 0) * 1000 if s["requests"].get("sustained4") else None
        lat_src = "vlm_int"
    else:
        p95 = p95_ms(r["latency_ms"] for r in rows if r.get("content") and r.get("latency_ms") is not None)
        lat_src = "answers"
    placeholder = bool(key_map.get("prompt") and key_map["prompt"].get("placeholder"))
    rationale_ok = rate is not None and n >= MIN_SAMPLES and rate >= RATIONALE_PASS and not missing
    latency_ok = p95 is not None and p95 <= LATENCY_P95_MS
    blocked = [why for cond, why in ((placeholder, "자리 표시 프롬프트(25 전)"),
                                     (not manifest.get("product_metric"), "제품 지표가 아닌 출처(SIM·시험 입력)")) if cond]
    return {
        "rationale": {"n": n, "match": sum(final.values()), "rate": rate, "threshold": RATIONALE_PASS, "min_samples": MIN_SAMPLES,
                      "pass": rationale_ok, "split_keys": split, "unrated_keys": missing, "excluded": key_map.get("excluded"),
                      "by_stratum": by_stratum},
        "agreement": agreement(per_rater),
        "latency": {"p95_ms": p95, "threshold_ms": LATENCY_P95_MS, "source": lat_src, "pass": latency_ok},
        "prompt": key_map.get("prompt"),
        "verdict_blocked_by": blocked,
    }


# ---------------------------------------------------------------- 보고서 인용


def citation_existence(jobs: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """보고서마다 225 인용 검사(check_draft)를 결과 sections 에 다시 돌려 '입력에 없는 조항' 문제만 센다(본문 표기 포함)."""
    total = bad = 0
    failed = []
    for i, job in enumerate(jobs):
        sections = job["result"]["sections"]
        total += sum(len(p["citations"]) for s in sections for p in s["paragraphs"])
        _, problems = check_draft(json.dumps({"sections": sections}, ensure_ascii=False), job["input"])
        missing = [p for p in problems if "입력에 없는 조항" in p]
        bad += sum("조항 인용" in p for p in missing)
        if missing:
            failed.append({"job": i, "report_id": job["result"].get("report_id"), "problems": missing})
    return {"reports": len(jobs), "citations": total, "missing_citations": bad,
            "rate": (total - bad) / total if total else None, "failed_reports": failed, "pass": bool(jobs) and not failed}


def export_citation_sheets(jobs: Sequence[Mapping[str, Any]], out: Path, raters: Sequence[str], seed: int = 0) -> dict[str, Any]:
    out.mkdir(parents=True, exist_ok=False)
    rows = []
    for i, job in enumerate(jobs):
        clauses = {c["clause_id"]: c["text"] for c in job["input"]["clauses"]}
        items = {k: v for k, v in enumerate((job["input"].get("plan") or {}).get("items") or [])}
        for si, s in enumerate(job["result"]["sections"]):
            ctx = s["title"]
            if s.get("item_index") is not None and s["item_index"] in items:
                it = items[s["item_index"]]
                ctx += f" / {it['rule_id']} {it['severity']} {it.get('zone_id')}"
            for pi, p in enumerate(s["paragraphs"]):
                for c in p["citations"]:
                    full = clauses.get(c) or next((t for cid, t in clauses.items() if cid.split("#", 1)[0] == c), "")
                    rows.append(((i, si, pi, c), f"{ctx}\n{p['text']}", c, full))
    order = random.Random(seed).sample(range(len(rows)), len(rows))
    key_map = {f"c{n + 1:04d}": list(rows[j][0]) for n, j in enumerate(order)}
    keyed = [(f"c{n + 1:04d}", *rows[j][1:]) for n, j in enumerate(order)]
    for r in raters:
        mine = random.Random(f"{seed}:{r}").sample(keyed, len(keyed))
        _write_csv(out / f"citation_{r}.csv", CITE_COLUMNS, [(*x, "", "") for x in mine])
    (out / "key_map.json").write_text(json.dumps({"keys": key_map}, ensure_ascii=False, indent=1), encoding="utf-8")
    return {"rows": len(rows), "raters": list(raters)}


def citation_label(row: Mapping[str, str]) -> bool | None:
    v = row.get(CITE_COLUMNS[4], "")
    return None if v not in YES_NO else v == "Y"


def aggregate_citations(jobs: Sequence[Mapping[str, Any]], score_csvs: Sequence[Path],
                        adjudication_csv: Path | None = None) -> dict[str, Any]:
    per_rater = _load_ratings(score_csvs, citation_label)
    final, split, missing = consensus(per_rater, _adjudication(adjudication_csv, citation_label))
    n = len(final)
    rate = sum(final.values()) / n if n else None
    existence = citation_existence(jobs)
    return {
        "existence": existence,
        "appropriateness": {"n": n, "appropriate": sum(final.values()), "rate": rate, "threshold": CITE_APPROPRIATE_PASS,
                            "pass": rate is not None and rate >= CITE_APPROPRIATE_PASS and not missing,
                            "split_keys": split, "unrated_keys": missing},
        "agreement": agreement(per_rater),
    }


def summary_table(result: Mapping[str, Any]) -> str:
    r, lat = result["rationale"], result["latency"]
    lines = ["| 항목 | 값 | 기준 | 통과 |", "|---|---|---|---|",
             f"| 근거-상태 일치 | {r['match']}/{r['n']} = {r['rate'] if r['rate'] is None else round(r['rate'], 3)} | ≥ {r['threshold']} (n ≥ {r['min_samples']}) | {r['pass']} |",
             f"| 이벤트 → 근거 p95 | {lat['p95_ms']} ms ({lat['source']}) | ≤ {lat['threshold_ms']} ms | {lat['pass']} |"]
    for a in result["agreement"]:
        lines.append(f"| 평가자 일치 {a['raters'][0]}·{a['raters'][1]} | 단순 {a['simple']}, κ {a['kappa']} (n {a['n']}) | 참고 | - |")
    if result["verdict_blocked_by"]:
        lines.append(f"\nG7 판정 안 함: {', '.join(result['verdict_blocked_by'])}")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = p.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("sheet")
    s.add_argument("--bundle", type=Path, required=True)
    s.add_argument("--answers", type=Path, required=True)
    s.add_argument("--out", type=Path, required=True)
    s.add_argument("--raters", required=True, help="쉼표 구분 평가자 표시(이름이 아닌 기호, 예: A,B)")
    s.add_argument("--seed", type=int, default=0)
    g = sub.add_parser("aggregate")
    g.add_argument("--bundle", type=Path, required=True)
    g.add_argument("--answers", type=Path, required=True)
    g.add_argument("--sheets", type=Path, required=True)
    g.add_argument("--scores", type=Path, nargs="+", required=True)
    g.add_argument("--adjudication", type=Path)
    g.add_argument("--latency-summary", type=Path, help="vlm_int.py summary.json(없으면 answers 지연)")
    g.add_argument("--out", type=Path, required=True)
    cs = sub.add_parser("cite-sheet")
    cs.add_argument("--jobs", type=Path, required=True)
    cs.add_argument("--out", type=Path, required=True)
    cs.add_argument("--raters", required=True)
    cs.add_argument("--seed", type=int, default=0)
    ca = sub.add_parser("cite-aggregate")
    ca.add_argument("--jobs", type=Path, required=True)
    ca.add_argument("--scores", type=Path, nargs="+", required=True)
    ca.add_argument("--adjudication", type=Path)
    ca.add_argument("--out", type=Path, required=True)
    a = p.parse_args(argv)
    if a.cmd == "sheet":
        print(json.dumps(export_rationale_sheets(a.bundle, a.answers, a.out, a.raters.split(","), a.seed), ensure_ascii=False))
    elif a.cmd == "aggregate":
        res = aggregate_rationale(a.bundle, a.answers, a.sheets, a.scores, a.adjudication, a.latency_summary)
        a.out.mkdir(parents=True, exist_ok=True)
        (a.out / "g7_result.json").write_text(json.dumps(res, ensure_ascii=False, indent=1), encoding="utf-8")
        (a.out / "g7_summary.md").write_text(summary_table(res) + "\n", encoding="utf-8")
        print(summary_table(res))
    elif a.cmd == "cite-sheet":
        print(json.dumps(export_citation_sheets(read_jsonl(a.jobs), a.out, a.raters.split(","), a.seed), ensure_ascii=False))
    else:
        res = aggregate_citations(read_jsonl(a.jobs), a.scores, a.adjudication)
        a.out.mkdir(parents=True, exist_ok=True)
        (a.out / "g7_citation_result.json").write_text(json.dumps(res, ensure_ascii=False, indent=1), encoding="utf-8")
        print(json.dumps({k: res[k] for k in ("existence", "appropriateness")}, ensure_ascii=False)[:2000])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
