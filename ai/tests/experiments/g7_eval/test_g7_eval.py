"""G7 평가 도구 (S15P31A101-35): 표본 층 분포, 녹화 프레임 고르기, 답 생성기가 VlmFrame(흐린 픽셀)만 보내는지,
채점표에 모델 정보 없음, 집계(일치율·κ·p95·조정), 인용 존재율. 가짜 VLM·가짜 채점, CPU.
"""

from __future__ import annotations

import base64
import csv
import importlib
import io
import json
import sys
from collections import Counter
from pathlib import Path

import numpy as np
import pytest

AI = Path(__file__).resolve().parents[3]
REPO = AI.parent
for p in (AI / "experiments" / "g7_eval", AI / "tests" / "services" / "tier0", AI / "tests" / "io"):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

from simai_ai.vlm.client import VlmClient, VlmSettings
from simai_ai.vlm.face_blur import Person

T0 = 1_792_552_246_000
FACE_KPS = ((110.0, 110.0, 0.9), (104.0, 100.0, 0.9), (116.0, 100.0, 0.9), (95.0, 102.0, 0.8), (125.0, 102.0, 0.8))
PERSON = Person(bbox=(65.0, 60.0, 155.0, 300.0), keypoints=FACE_KPS + (None,) * 12)


def _mod(name):
    sys.modules.pop(name, None)
    return importlib.import_module(name)


@pytest.fixture
def S():
    return _mod("g7_samples")


@pytest.fixture
def A():
    return _mod("g7_answers")


@pytest.fixture
def G():
    return _mod("g7_score")


def _event(eid, rule, sev="CRITICAL", reasons=(), wids=("w-1",), t=T0 + 2000):
    return {"payload": {"event_id": eid, "event_sequence": 1, "rule_id": rule, "rule_version": 1, "severity": sev,
                        "confidence": "HIGH", "needs_check": bool(reasons), "confidence_reasons": list(reasons),
                        "world_ids": list(wids), "zone_id": "z-1", "metrics": {"d_m": 0.5}, "raised_at": t,
                        "calibration_version": "cal-1", "scene_version": "s-1"}}


def _ws(t, ids, msg_id):
    return {"msg_id": msg_id, "payload": {"t_latest_global": t, "tracks": [
        {"world_id": w, "position_xy": [1.0, 2.0], "cameras": [{"camera_id": "cam_A"}, {"camera_id": "cam_B"}]} for w in ids]}}


# ---------------------------------------------------------------- 표본


def test_candidates_take_first_line_and_nearest_earlier_world_state(S):
    events = [_event("e1", "R2", reasons=("Z_UNKNOWN",)), {**_event("e1", "R2"), "later": True}, _event("e2", "R4", wids=("w-2",))]
    states = [_ws(T0 + 1000, ["w-1"], "m1"), _ws(T0 + 1900, ["w-1", "w-2"], "m2"), _ws(T0 + 2100, ["w-1"], "m3")]
    c = S.candidates_from_replay(events, states, {"e1": {"event_id": "e1", "summary": "단상 가장자리"}})
    assert [x["event_id"] for x in c] == ["e1", "e2"]
    assert c[0]["input"]["world_state_ref"] == "m2" and c[0]["input"]["structured_state"]["track_count"] == 2
    assert c[0]["strata"] == {"rule_id": "R2", "severity": "CRITICAL", "check": "확인 필요", "cameras": "2대 이하", "people": "2+"}
    assert c[0]["truth"]["summary"] == "단상 가장자리" and c[1]["truth"] is None


def test_stratify_spreads_evenly_and_is_deterministic(S):
    cands = []
    for rule, n in (("R1", 40), ("R2", 6), ("R4", 3)):
        for k in range(n):
            cands.append({"event_id": f"{rule}-{k:02d}", "strata": {"rule_id": rule, "check": "확정" if k % 2 else "확인 필요",
                                                                     "people": "1", "severity": "CRITICAL", "cameras": "3대 이상"}})
    picked = S.stratify(cands, 20, ("rule_id", "check"), seed=3)
    cells = Counter((c["strata"]["rule_id"], c["strata"]["check"]) for c in picked)
    assert len(picked) == 20 and cells[("R4", "확인 필요")] == 2 and cells[("R4", "확정")] == 1  # 작은 칸은 다 들어간다
    assert cells[("R2", "확정")] == 3 and cells[("R2", "확인 필요")] == 3
    assert abs(cells[("R1", "확정")] - cells[("R1", "확인 필요")]) <= 1
    assert [c["event_id"] for c in S.stratify(cands, 20, ("rule_id", "check"), seed=3)] == [c["event_id"] for c in picked]
    assert S.filter_candidates(cands, ["R2"], ["CRITICAL"]) == cands[40:46]


def test_bundle_picks_nearest_recorded_frames_and_marks_test_input(S, tmp_path):
    import live_video as lv
    from recording_session import Segment, write_session

    def seg(cam, seqs, start):
        enc = lv.encode(list(seqs))
        frames = []
        for (au, key), seq in zip(enc.frames, seqs, strict=True):
            meta = lv.frame_meta(cam, 0, seq, start + 40 * seq, key)
            meta["received_at_ms"] = start + 40 * seq + 30
            frames.append((meta, au))
        return Segment(0, lv.codec_meta(cam, 0, enc), frames)

    # cam_A 0..149(T0~T0+6 s), cam_B 는 T0+2 s 부터 → 이벤트 t=T0+4000 의 -4000 ms(T0) 프레임은 cam_B 에 없다(채우지 않음)
    session = write_session(tmp_path, "rec-test-g7", {"cam_A": [seg("cam_A", range(150), T0)],
                                                     "cam_B": [seg("cam_B", range(100), T0 + 2000)]})
    cands = S.candidates_from_replay([_event("e1", "R2", t=T0 + 4000)], [_ws(T0 + 3990, ["w-1"], "m1")])
    m = S.load_recording_manifest(session)

    def writer(path, image):
        path.write_text(json.dumps({"index": lv.read_index(image)}), encoding="utf-8")

    manifest = S.build_bundle(cands, tmp_path / "bundle", source={"kind": "RECORDING", "is_test_input": m["is_test_input"]},
                              headers=S.read_headers(session), session=session, writer=writer)
    inp = json.loads((tmp_path / "bundle" / "samples" / "g7-001" / "input.json").read_text(encoding="utf-8"))
    got = {(f["camera_id"], f["offset_ms"]): f for f in inp["frames"]}
    assert set(got) == {("cam_A", -4000), ("cam_A", 0), ("cam_B", 0)}
    sdir = tmp_path / "bundle" / "samples" / "g7-001"
    assert json.loads((sdir / got[("cam_A", 0)]["file"]).read_text())["index"] == 100
    assert json.loads((sdir / got[("cam_A", -4000)]["file"]).read_text())["index"] == 0
    assert all(f["face_policy"] == "BLURRED" and f["sha256"].startswith("sha256:") for f in inp["frames"])
    assert manifest["product_metric"] is False and manifest["missing_truth"] == 1  # 합성 녹화 = 시험 입력
    with pytest.raises(FileExistsError):
        S.build_bundle(cands, tmp_path / "bundle", source={"kind": "SIM"}, headers=None, session=None, writer=None)


# ---------------------------------------------------------------- 답 생성


def _bundle(tmp_path, n=3, product=True):
    b = tmp_path / "bundle"
    samples = []
    for i in range(n):
        sid = f"g7-{i + 1:03d}"
        sdir = b / "samples" / sid / "frames"
        sdir.mkdir(parents=True)
        (sdir / "cam_A_p0.jpg").write_bytes(str(20 + i).encode())
        inp = {**_event(f"e{i}", "R2")["payload"], "observation_time": T0, "structured_state": {"tracks": []},
               "frames": [{"camera_id": "cam_A", "file": "frames/cam_A_p0.jpg", "offset_ms": 0, "estimated_capture_time_ms": T0,
                           "face_policy": "BLURRED"}]}
        (b / "samples" / sid / "input.json").write_text(json.dumps(inp, ensure_ascii=False), encoding="utf-8")
        (b / "samples" / sid / "truth.json").write_text(json.dumps({"summary": f"정답 {i}"}, ensure_ascii=False), encoding="utf-8")
        samples.append({"sample_id": sid, "strata": {"rule_id": "R2", "severity": "CRITICAL", "check": "확정" if i % 2 else "확인 필요",
                                                     "cameras": "2대 이하", "people": "1"}})
    (b / "manifest.json").write_text(json.dumps({"source": {"kind": "RECORDING"}, "product_metric": product, "samples": samples},
                                                ensure_ascii=False), encoding="utf-8")
    return b


class Provider:
    def people(self, image):
        return [PERSON]


def _decode(data):
    return np.full((320, 240, 3), int(data), np.uint8) + np.arange(240, dtype=np.uint8)[None, :, None]


class _Resp(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _fake_vlm(sent, replies=(("근거: 단상 가장자리", "stop"),)):
    def opener(req, timeout):
        sent.append(json.loads(req.data))
        content, finish = replies[min(len(sent), len(replies)) - 1]
        return _Resp(json.dumps({"model": "cosmos3-nano", "choices": [{"message": {"content": content}, "finish_reason": finish}],
                                 "usage": {"prompt_tokens": 10, "completion_tokens": 5}}).encode())
    return opener


def _text(body):
    return next(p["text"] for p in body["messages"][-1]["content"] if p["type"] == "text")


def test_answers_with_product_prompt_match_processor(A, tmp_path):
    from simai_ai.vlm.rationale import PROMPT, RETRY_NOTE

    bundle = _bundle(tmp_path, 2)
    sdir = bundle / "samples" / "g7-001"
    (sdir / "frames" / "cam_B_m4000.jpg").write_bytes(b"30")
    (sdir / "frames" / "cam_B_p40.jpg").write_bytes(b"31")
    inp = json.loads((sdir / "input.json").read_text(encoding="utf-8"))
    inp["frames"] += [{**inp["frames"][0], "camera_id": "cam_B", "file": "frames/cam_B_p40.jpg", "estimated_capture_time_ms": T0 + 40},
                      {**inp["frames"][0], "camera_id": "cam_B", "file": "frames/cam_B_m4000.jpg", "estimated_capture_time_ms": T0 - 4000}]
    (sdir / "input.json").write_text(json.dumps(inp, ensure_ascii=False), encoding="utf-8")
    good = json.dumps({"rationale": "사람이 단상 가장자리에 서 있다.", "proposed_severity": None}, ensure_ascii=False)
    # 표본 1: 첫 답이 잘림 → 재질문 → 정상. 표본 2: 형식 오류 두 번 → VLM_ANSWER_INVALID
    sent = []
    client = VlmClient(VlmSettings(), encode=lambda px: ("image/png", b"x"), opener=_fake_vlm(sent, [('{"rationale": "사람', "length"), (good, "stop"),
                                                                                                       ("자유 문장", "stop"), ("자유 문장", "stop")]))
    meta = A.run(bundle, tmp_path / "ans", client, PROMPT, Provider(), decode=_decode)
    assert meta["ok"] == 1 and meta["error"] == 1 and len(sent) == 4
    first = _text(sent[0])
    assert "1. 카메라 cam_B, 이벤트 시점 기준 -4.0초" in first and "2. 카메라 cam_A, 이벤트 시점 기준 +0.0초" in first
    assert "cam_B, 이벤트 시점 기준 +0.0" not in first and "3." not in first  # 사후 프레임(+40 ms)은 처리기처럼 뺀다
    assert _text(sent[1]) == first + RETRY_NOTE
    rows = [json.loads(x) for x in (tmp_path / "ans" / "answers.jsonl").read_text(encoding="utf-8").splitlines()]
    assert rows[0]["content"] == good and rows[0]["retried"] is True and rows[0]["error"] is None
    assert rows[1]["content"] is None and rows[1]["error"]["code"] == "VLM_ANSWER_INVALID"  # 채점표에서 빠진다


def test_answers_send_only_blurred_vlmframes(A, tmp_path):
    bundle = _bundle(tmp_path)
    sent = []
    client = VlmClient(VlmSettings(), encode=lambda px: ("image/png", np.ascontiguousarray(px).tobytes()), opener=_fake_vlm(sent))
    meta = A.run(bundle, tmp_path / "ans", client, A.PLACEHOLDER, Provider(), decode=_decode)
    assert meta["ok"] == 3 and meta["prompt"] == {"id": "g7-placeholder-0", "placeholder": True}
    for i, body in enumerate(sent):
        (part,) = [p for p in body["messages"][-1]["content"] if p["type"] == "image_url"]
        px = np.frombuffer(base64.b64decode(part["image_url"]["url"].split(",", 1)[1]), np.uint8).reshape(320, 240, 3)
        original = _decode(str(20 + i).encode())
        assert not np.array_equal(px, original) and np.array_equal(px[200:], original[200:])  # 얼굴 영역만 바뀜
    rows = [json.loads(x) for x in (tmp_path / "ans" / "answers.jsonl").read_text(encoding="utf-8").splitlines()]
    assert rows[0]["content"] == "근거: 단상 가장자리" and rows[0]["prompt"]["placeholder"] is True
    with pytest.raises(FileExistsError):  # 답을 덮어쓰지 않는다
        A.run(bundle, tmp_path / "ans", client, A.PLACEHOLDER, Provider(), decode=_decode)


def test_answers_record_no_frames_when_detection_fails(A, tmp_path):
    class Broken:
        def people(self, image):
            raise RuntimeError("GPU OOM")

    sent = []
    meta = A.run(_bundle(tmp_path, 1), tmp_path / "ans", VlmClient(VlmSettings(), opener=_fake_vlm(sent)), A.PLACEHOLDER,
                 Broken(), decode=_decode)
    assert meta["no_frames"] == 1 and sent == []  # 흐리지 못한 프레임은 보내지 않는다


def test_load_prompt(A):
    assert A.load_prompt("placeholder") is A.PLACEHOLDER
    with pytest.raises(ValueError):
        A.load_prompt("json")  # 모듈:이름 아님
    with pytest.raises(ValueError):
        A.load_prompt("json:dumps")  # Prompt 꼴 아님


# ---------------------------------------------------------------- 채점표·집계


def _answers(tmp_path, n=3):
    d = tmp_path / "ans"
    d.mkdir()
    rows = [{"sample_id": f"g7-{i + 1:03d}", "prompt": {"id": "rationale-v9", "placeholder": False}, "content": f"답 {i}",
             "model": "cosmos3-nano", "latency_ms": 1000 + 100 * i, "input_tokens": 9, "output_tokens": 9} for i in range(n)]
    rows[-1] = {"sample_id": rows[-1]["sample_id"], "prompt": rows[0]["prompt"], "content": None,
                "error": {"code": "VLM_TIMEOUT", "retryable": True}}
    (d / "answers.jsonl").write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows), encoding="utf-8")
    return d / "answers.jsonl"


def test_sheets_hide_model_and_implementation(G, tmp_path):
    bundle, answers = _bundle(tmp_path), _answers(tmp_path)
    info = G.export_rationale_sheets(bundle, answers, tmp_path / "sheets", ["A", "B"])
    assert info == {"rated": 2, "excluded": 1, "raters": ["A", "B"]}
    for name in ("rationale_A.csv", "rationale_B.csv", "rationale_A.html"):
        text = (tmp_path / "sheets" / name).read_text(encoding="utf-8-sig")
        for hidden in ("cosmos3", "rationale-v9", "g7-00", "latency", "1000", "tokens"):
            assert hidden not in text, (name, hidden)
    key_map = json.loads((tmp_path / "sheets" / "key_map.json").read_text(encoding="utf-8"))
    assert sorted(key_map["keys"].values()) == ["g7-001", "g7-002"] and key_map["excluded"] == {"g7-003": "VLM_TIMEOUT"}


def _fill(path, labels):
    rows = list(csv.reader(path.open(encoding="utf-8-sig")))
    for r in rows[1:]:
        r[4:7] = labels[r[0]]
    with path.open("w", encoding="utf-8-sig", newline="") as f:
        csv.writer(f).writerows(rows)


def test_aggregate_rate_agreement_adjudication_and_blocking(G, tmp_path):
    bundle, answers = _bundle(tmp_path, 3, product=True), _answers(tmp_path)
    G.export_rationale_sheets(bundle, answers, tmp_path / "sheets", ["A", "B"])
    a, b = tmp_path / "sheets" / "rationale_A.csv", tmp_path / "sheets" / "rationale_B.csv"
    _fill(a, {"s001": ("일치", "N", "N"), "s002": ("일치", "N", "N")})
    _fill(b, {"s001": ("일치", "N", "N"), "s002": ("일치", "Y", "N")})  # s002 는 지어냄으로 갈림
    res = G.aggregate_rationale(bundle, answers, tmp_path / "sheets", [a, b])
    r = res["rationale"]
    assert (r["n"], r["match"], r["split_keys"]) == (2, 1, ["s002"]) and r["pass"] is False  # 갈리면 보수로 불일치, n<100
    assert res["agreement"][0]["simple"] == 0.5
    assert res["latency"] == {"p95_ms": 1100, "threshold_ms": 5000, "source": "answers", "pass": True}
    assert res["verdict_blocked_by"] == []
    adj = tmp_path / "adj.csv"
    adj.write_text("key,verdict(일치/부분/불일치),지어냄(Y/N),등급변경(Y/N)\ns002,일치,N,N\n", encoding="utf-8")
    res = G.aggregate_rationale(bundle, answers, tmp_path / "sheets", [a, b], adj)
    assert res["rationale"]["match"] == 2 and res["rationale"]["rate"] == 1.0
    sid = json.loads((tmp_path / "sheets" / "key_map.json").read_text(encoding="utf-8"))["keys"]["s001"]
    stratum = {"g7-001": "확인 필요", "g7-002": "확정"}[sid]
    assert res["rationale"]["by_stratum"]["check"][stratum] == {"n": 1, "match": 1, "rate": 1.0}


def test_aggregate_blocks_placeholder_and_non_product(G, tmp_path):
    bundle = _bundle(tmp_path, 2, product=False)
    d = tmp_path / "ans"
    d.mkdir()
    (d / "answers.jsonl").write_text("".join(json.dumps({"sample_id": f"g7-00{i}", "prompt": {"id": "g7-placeholder-0", "placeholder": True},
                                                         "content": "답", "latency_ms": 900}) + "\n" for i in (1, 2)), encoding="utf-8")
    G.export_rationale_sheets(bundle, d / "answers.jsonl", tmp_path / "sheets", ["A"])
    a = tmp_path / "sheets" / "rationale_A.csv"
    _fill(a, {"s001": ("일치", "N", "N"), "s002": ("부분", "N", "N")})
    res = G.aggregate_rationale(bundle, d / "answers.jsonl", tmp_path / "sheets", [a])
    assert res["rationale"]["match"] == 1 and len(res["verdict_blocked_by"]) == 2
    assert "판정 안 함" in G.summary_table(res)


def test_kappa_and_p95(G):
    a = [True] * 8 + [False] * 2
    b = [True] * 7 + [False] * 3
    # po 0.9, pa 0.8, pb 0.7 → pe 0.62, κ = 0.28/0.38
    assert G.cohen_kappa(a, b) == pytest.approx(0.28 / 0.38)
    assert G.cohen_kappa([True] * 3, [True] * 3) is None
    assert G.p95_ms(range(1, 101)) == 95 and G.p95_ms([3000]) == 3000 and G.p95_ms([]) is None


def test_latency_from_vlm_int_summary(G, tmp_path):
    bundle, answers = _bundle(tmp_path, 2), _answers(tmp_path, 2)
    G.export_rationale_sheets(bundle, answers, tmp_path / "sheets", ["A"])
    s = tmp_path / "summary.json"
    s.write_text(json.dumps({"requests": {"sustained4": {"total_p95": 2.12}}}), encoding="utf-8")
    res = G.aggregate_rationale(bundle, answers, tmp_path / "sheets", [tmp_path / "sheets" / "rationale_A.csv"], latency_summary=s)
    assert res["latency"]["source"] == "vlm_int" and res["latency"]["p95_ms"] == pytest.approx(2120)


# ---------------------------------------------------------------- 보고서 인용


def _tbm_job():
    req = json.loads((REPO / "contracts/examples/job_request.tbm_report.json").read_text(encoding="utf-8"))
    cb = json.loads((REPO / "contracts/examples/job_callback.tbm_report.json").read_text(encoding="utf-8"))
    return {"input": req["payload"]["input"], "result": cb["payload"]["result"]}


def test_citation_existence_counts_missing_clauses(G):
    good = _tbm_job()
    res = G.citation_existence([good])
    assert res["pass"] and res["missing_citations"] == 0 and res["rate"] == 1.0 and res["citations"] == 4
    bad = json.loads(json.dumps(good))
    bad["result"]["sections"][1]["paragraphs"][0]["citations"].append("산업안전보건기준규칙 제999조")
    res = G.citation_existence([good, bad])
    assert not res["pass"] and res["missing_citations"] == 1 and res["citations"] == 9
    assert res["rate"] == pytest.approx(8 / 9) and res["failed_reports"][0]["job"] == 1


def test_citation_sheet_and_appropriateness(G, tmp_path):
    jobs = [_tbm_job()]
    info = G.export_citation_sheets(jobs, tmp_path / "cs", ["A"])
    assert info["rows"] == 4
    path = tmp_path / "cs" / "citation_A.csv"
    rows = list(csv.reader(path.open(encoding="utf-8-sig")))
    assert all(r[3] for r in rows[1:])  # 조항 본문이 채워진다
    for i, r in enumerate(rows[1:]):
        r[4] = "Y" if i < 3 else "N"
    with path.open("w", encoding="utf-8-sig", newline="") as f:
        csv.writer(f).writerows(rows)
    res = G.aggregate_citations(jobs, [path])
    assert res["appropriateness"]["rate"] == 0.75 and res["appropriateness"]["pass"] is False and res["existence"]["pass"]
