"""시나리오 재생 정답 비교 (S15P31A101-41, 명세 21.3, 부록 D 계층 SC): 시나리오마다 파일 모드 Tier 0 를 두 번 돌려 기대 이벤트
파일과 비교하고 두 출력이 같은지 본다. 음성 대조: 규칙 임계값·기대 이벤트를 바꾸거나, 시계를 실제 시각으로 바꾸거나, 그림과
헤더 결합을 어긋나게 하거나, 자동 해소를 끼워 넣으면 비교가 실패해야 한다(비교기가 차이를 놓치지 않는다).

SIM 상한 결과다(검출기·추적기 대역). 제품 성능으로 쓰지 않는다(AGENTS 규칙 7).
"""

from __future__ import annotations

import copy
import json
import os
import shutil
import sys
import time
import uuid
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

import golden  # noqa: E402

from services.tier0 import service as service_module  # noqa: E402
from services.tier0.file_input import ReplayClock  # noqa: E402

SLOW_MS = 20_000  # 장면이 이보다 길면 slow (SIMAI_SCENARIO_SLOW=1 일 때만 pytest 로 돈다)


def _params():
    out = []
    for name in golden.case_names():
        case, _ = golden.load_case(name)
        if case["sim"]["duration_ms"] > SLOW_MS:
            out.append(pytest.param(name, id=name, marks=[
                pytest.mark.slow,
                pytest.mark.skipif(os.environ.get("SIMAI_SCENARIO_SLOW") != "1",
                                   reason="느린 시나리오: SIMAI_SCENARIO_SLOW=1 또는 tests/scenarios/golden.py 로 돌린다")]))
        else:
            out.append(pytest.param(name, id=name))
    return out


@pytest.mark.parametrize("name", _params())
def test_scenario_matches_expected_and_is_deterministic(tmp_path, name) -> None:
    outcome = golden.run_case(name, tmp_path)
    assert outcome.diffs == [], "\n".join(outcome.diffs + golden.summary(outcome.replay, outcome.start_ms))


def test_every_case_names_appendix_d_ids_and_hand_written_windows() -> None:
    """시나리오마다 부록 D 시험 ID 가 있고, 기대 전이에는 손으로 정한 시각 창이 있다(찍은 값 하나가 아니라 구간)."""
    spec = (golden.AI_DIR.parent / "docs" / "spec" / "appendix-d-regression.md").read_text(encoding="utf-8")
    names = golden.case_names()
    assert len(names) >= 6
    for name in names:
        case, expected = golden.load_case(name)
        assert all(f"| {d} |" in spec for d in case["d_ids"]), (name, case["d_ids"])
        assert expected["events"], name
        for event in expected["events"]:
            for t in event["transitions"]:
                lo, hi = t["at_ms"]
                assert hi > lo, (name, event["rule_id"], t)


# --------------------------------------------------------------------------- 음성 대조 (한 번 돌린 출력을 같이 쓴다)


@pytest.fixture(scope="module")
def enter_exit(tmp_path_factory):
    tmp = tmp_path_factory.mktemp("enter-exit")
    case, expected = golden.load_case("r1_enter_exit")
    scene = golden.build_scene(case, tmp / "scene")
    replay = golden.run_replay(case, scene, tmp / "run")
    assert golden.compare(expected, replay, scene.start_ms) == []
    return case, expected, scene, replay


def test_negative_control_changed_expected_event_fails(enter_exit) -> None:
    """기대 이벤트 하나를 바꾸면(등급·시각 창·전이 하나 빠짐·이벤트 하나 빠짐) 비교가 실패하고 무엇이 다른지 말한다."""
    _case, expected, scene, replay = enter_exit

    def changed(edit):
        doc = copy.deepcopy(expected)
        edit(doc)
        return golden.compare(doc, replay, scene.start_ms)

    def severity(d):
        d["events"][1]["severity"] = "CRITICAL"

    def window(d):
        d["events"][1]["transitions"][0]["at_ms"] = [2500, 3067]  # 실제 발생은 이보다 이르다

    def drop_transition(d):
        d["events"][1]["transitions"].pop()  # 해소가 없다고 적음

    def drop_event(d):
        d["events"].pop(0)  # R5 를 기대에서 뺌 → 예상 밖 이벤트

    assert any("severity" in x and "CRITICAL" in x for x in changed(severity))
    assert any("RAISED 시각 기대 [2500, 3067]" in x for x in changed(window))
    assert any("전이 순서" in x for x in changed(drop_transition))
    assert any("[예상 밖 이벤트] R5" in x for x in changed(drop_event))


def test_negative_control_changed_rule_threshold_fails(tmp_path, enter_exit) -> None:
    """규칙 임계값(해소 유지 시간 1.5 s → 4 s)을 바꿔 같은 장면을 다시 돌리면 해소가 기대 창(8.3~9.3 s)에 나지 않는다
    (장면 끝 11 s 까지 해소가 없다)."""
    case, expected, scene, _ = enter_exit
    slower = copy.deepcopy(case)
    slower["lifecycle"] = dict(golden.DEFAULT_LIFECYCLE, clear_hold_ms=4000)
    replay = golden.run_replay(slower, scene, tmp_path / "run")
    diffs = golden.compare(expected, replay, scene.start_ms)
    assert any(x.startswith("[이벤트 R1 진입] 전이 순서") for x in diffs), diffs


def _dump(message) -> str:
    return json.dumps(message, ensure_ascii=False, separators=(",", ":"))


def _forged(replay: golden.Replay, event_lines: list[str], **stats) -> golden.Replay:
    return golden.Replay(replay.out_dir, replay.world_lines, event_lines, {**replay.stats, **stats})


def _resolve_first(replay: golden.Replay, rule_id: str, at_ms: int) -> golden.Replay:
    """그 규칙의 첫 이벤트를 at_ms(절대 시각)에 해소된 것처럼 바꾼다: 그 뒤 줄은 버리고, 전이 문법(event_sequence·resolved_at)은
    맞춘다. 전이 일관성 검사가 아니라 기대 비교가 잡는지 보려는 것이다."""
    msgs = [json.loads(x) for x in replay.event_lines]
    target = next(m["payload"]["event_id"] for m in msgs if m["payload"]["rule_id"] == rule_id)
    kept = [m for m in msgs if m["payload"]["event_id"] != target or m["payload"]["updated_at"] < at_ms]
    last = [m for m in kept if m["payload"]["event_id"] == target][-1]
    resolved = copy.deepcopy(last)
    resolved["payload"].update(lifecycle="RESOLVED", updated_at=at_ms, resolved_at=at_ms, verification="VERIFIED",
                               event_sequence=last["payload"]["event_sequence"] + 1)  # 계약: 해소는 VERIFIED 뿐
    return _forged(replay, [_dump(m) for m in kept + [resolved]])


def test_negative_control_injected_auto_resolve_is_caught(tmp_path) -> None:
    """반례('모르면 안전' 경로): 관측을 잃은 동안 해소 줄이 끼어들면(자동 해소) 비교가 실패한다. 줄 사이 문법은 맞춰서
    전이 일관성 검사가 아니라 규칙 정답(해소는 나간 뒤 창에만)이 잡는다."""
    outcome = golden.run_case("r1_observation_loss", tmp_path, repeat=False)
    assert outcome.diffs == []
    _, expected = golden.load_case("r1_observation_loss")
    forged = _resolve_first(outcome.replay, "R1", outcome.start_ms + 5000)
    diffs = golden.compare(expected, forged, outcome.start_ms)
    assert not any(x.startswith("[전이 일관성]") for x in diffs), diffs
    assert any("관측을 잃은 사람의 진입" in x and "RESOLVED 시각 기대 [12300, 13300]" in x for x in diffs), diffs


def test_negative_control_illegal_non_milestone_transitions_fail(enter_exit) -> None:
    """리뷰 F1 반례: 이정표 밖 전이가 틀려도(계약은 통과) 실패한다 — 해소 뒤 재개 없는 UPDATED, event_sequence 반복,
    대상 World ID 바꿔치기(개수는 같음)."""
    _case, expected, scene, replay = enter_exit
    msgs = [json.loads(x) for x in replay.event_lines]
    r1 = [m for m in msgs if m["payload"]["rule_id"] == "R1"]

    post = copy.deepcopy(r1[-1])  # RESOLVED 뒤 10 s 에 UPDATED(resume_count 그대로, resolved_at null)
    post["payload"].update(lifecycle="UPDATED", resolved_at=None, updated_at=scene.start_ms + 10_000,
                           event_sequence=r1[-1]["payload"]["event_sequence"] + 1)
    same_seq = copy.deepcopy(msgs)
    for m in same_seq:
        m["payload"]["event_sequence"] = 1
    other_target = copy.deepcopy(msgs)
    for m in other_target:
        if m["payload"]["rule_id"] == "R1" and m["payload"]["lifecycle"] != "RAISED":
            m["payload"]["world_ids"] = ["w-other-0001"]

    for name, lines, needle in (("해소 뒤 UPDATED", msgs + [post], "해소 뒤에 재개 아닌 전이"),
                                ("sequence 반복", same_seq, "event_sequence 1 다음이 1"),
                                ("대상 바꿔치기", other_target, "world_ids 가 바뀌었다")):
        assert all(golden.schema_errors(m, "risk_event") == [] for m in lines), name  # 계약만으로는 못 잡는다
        diffs = golden.compare(expected, _forged(replay, [_dump(m) for m in lines]), scene.start_ms)
        assert any(x.startswith("[전이 일관성]") and needle in x for x in diffs), (name, diffs)
    missing = copy.deepcopy(other_target)
    diffs = golden.compare(expected, _forged(replay, [_dump(m) for m in missing]), scene.start_ms)
    assert any("world_state 에 없는 대상 World ID ['w-other-0001']" in x for x in diffs), diffs


def test_second_run_invariants_are_checked_too(tmp_path, monkeypatch, enter_exit) -> None:
    """리뷰 F2 반례: 두 번째 실행만 local_alert·저널·결합 오류가 있어도(출력 바이트는 같음) 실패한다."""
    case, _expected, scene, replay = enter_exit
    runs = iter([replay, _forged(replay, replay.event_lines, local_alerts=1, journal_offset=1, misbound=1)])
    monkeypatch.setattr(golden, "build_scene", lambda *_a, **_k: scene)
    monkeypatch.setattr(golden, "run_replay", lambda *_a, **_k: next(runs))
    outcome = golden.run_case("r1_enter_exit", tmp_path)
    assert not any(x.startswith("[결정성]") for x in outcome.diffs)
    for key in ("local_alerts", "journal_offset", "misbound"):
        assert any(x.startswith(f"[두 번째 실행] [불변식] {key}") for x in outcome.diffs), outcome.diffs
    assert not any(not x.startswith("[두 번째 실행]") for x in outcome.diffs)


def test_known_limitation_is_separate_from_rule_truth(tmp_path) -> None:
    """리뷰 F3: 늦은 카메라 시나리오의 '해소 확인 불가'는 현재 한계(known_limitations)라, 융합 시각이 고쳐져 나간 뒤 창에 해소되면
    실패가 아니라 갱신 알림이 나고, 사람이 아직 구역 안에 있을 때 해소되면(규칙 정답 위반) 실패한다."""
    outcome = golden.run_case("r1_lagging_camera_unverifiable", tmp_path, repeat=False)
    assert outcome.diffs == [] and outcome.notices == []
    _, expected = golden.load_case("r1_lagging_camera_unverifiable")
    fixed = _resolve_first(outcome.replay, "R1", outcome.start_ms + 8800)
    diffs, notices = golden.evaluate(expected, fixed, outcome.start_ms)
    assert diffs == [] and any("slow-camera-fusion-time" in n and "전이 순서" in n for n in notices), (diffs, notices)
    early = _resolve_first(outcome.replay, "R1", outcome.start_ms + 5000)  # 사람은 6 s 까지 구역 안에 서 있다
    diffs, _ = golden.evaluate(expected, early, outcome.start_ms)
    assert any("RESOLVED 시각 기대 [8300, 9300]" in x for x in diffs), diffs


class WallClockReplayClock(ReplayClock):
    """반례 시계: 녹화 시각 대신 실제 시각을 낸다(명세 21.3 '시계는 주입식' 을 어김)."""

    def wall_ms(self) -> int:
        return time.time_ns() // 1_000_000


def test_counterexample_real_wall_clock_breaks_the_comparison(tmp_path, monkeypatch, enter_exit) -> None:
    """시계를 실제 시각으로 바꾸면 녹화가 한꺼번에 풀리고 시각이 녹화 시각이 아니라 비교가 실패한다
    (정답 파일이 주입 시계에 기대고 있고, 실제 시각에 기대는 시험이 아니다)."""
    case, expected, scene, _ = enter_exit
    monkeypatch.setattr(service_module, "ReplayClock", WallClockReplayClock)
    replay = golden.run_replay(case, scene, tmp_path / "run")
    diffs = golden.compare(expected, replay, scene.start_ms)
    assert diffs, "실제 시각 시계로도 정답과 같았다(시험이 시계를 보지 않는다)"


def test_counterexample_misbound_frames_break_the_invariant(tmp_path, enter_exit) -> None:
    """반례(D-G3-08): 그림과 헤더 결합이 한 장씩 어긋나면(검출기가 다른 촬영 시각의 keypoint 를 냄) 불변식이 실패한다."""
    case, expected, scene, _ = enter_exit
    shifted = golden.Scene(scene.run_dir, scene.start_ms, {c: fs[1:] + fs[:1] for c, fs in scene.frames.items()},
                           scene.session, scene.calibration)
    replay = golden.run_replay(case, shifted, tmp_path / "run")
    assert any(x.startswith("[불변식] misbound") for x in golden.compare(expected, replay, scene.start_ms))


def test_determinism_check_is_not_vacuous(tmp_path, enter_exit) -> None:
    """결정성 비교가 실제로 바이트를 본다: msg_id 를 무작위(uuid4)로 내면 두 출력이 달라 실패하고, 주입한 순번 msg_id 면 같다.
    event_id(UUIDv7 무작위 비트)만 정규화한다."""
    case, _, scene, first = enter_exit
    again = golden.run_replay(case, scene, tmp_path / "same")
    assert golden.determinism_diff(first, again) == []
    assert golden.normalized(first) == golden.normalized(again)
    assert first.event_lines != again.event_lines  # 정규화 전에는 event_id 가 다르다(정규화가 하는 일이 있다)
    random_ids = golden.run_replay(case, scene, tmp_path / "random", new_msg_id=lambda: str(uuid.uuid4()))
    assert golden.determinism_diff(first, random_ids)


# --------------------------------------------------------------------------- 스크립트(CI 대체) 종료 코드


def test_script_exit_codes(tmp_path, monkeypatch, capsys) -> None:
    cases = tmp_path / "cases"
    for name, edit in (("r1_enter_exit", None), ("broken", "severity")):
        shutil.copytree(golden.CASES_DIR / "r1_enter_exit", cases / name)
        for f in ("input.json", "expected.json"):
            doc = json.loads((cases / name / f).read_text(encoding="utf-8"))
            doc["scenario_id"] = name
            if edit and f == "expected.json":
                doc["events"][1]["severity"] = "CRITICAL"
            (cases / name / f).write_text(json.dumps(doc, ensure_ascii=False), encoding="utf-8")
    monkeypatch.setattr(golden, "CASES_DIR", cases)
    assert golden.main(["--once", "--out", str(tmp_path / "out")]) == 1
    printed = capsys.readouterr().out
    assert "통과  r1_enter_exit" in printed and "실패  broken" in printed and "실제 이벤트:" in printed
    # 재생 산출물이 --out 에 남는다(통합 시험 입력)
    assert (tmp_path / "out" / "r1_enter_exit" / "run-1" / "out" / "world_state.jsonl").stat().st_size > 0
    assert golden.main(["no_such_case"]) == 2
    assert golden.main(["--out", str(tmp_path / "out")]) == 2  # 비어 있지 않은 --out 은 덮어쓰지 않는다


def test_typo_in_expected_file_is_an_error_not_a_silent_pass(tmp_path, monkeypatch) -> None:
    """반례: 기대 파일의 키 오타(transitions → trasitions)가 조용히 무시되면 그 조건이 비어 시험이 늘 통과한다. 모르는 키는 오류(2)."""
    shutil.copytree(golden.CASES_DIR / "r1_enter_exit", tmp_path / "r1_enter_exit")
    path = tmp_path / "r1_enter_exit" / "expected.json"
    doc = json.loads(path.read_text(encoding="utf-8"))
    doc["events"][1]["trasitions"] = doc["events"][1].pop("transitions")
    path.write_text(json.dumps(doc, ensure_ascii=False), encoding="utf-8")
    monkeypatch.setattr(golden, "CASES_DIR", tmp_path)
    with pytest.raises(golden.CaseError, match="trasitions"):
        golden.load_case("r1_enter_exit")
    assert golden.main(["r1_enter_exit", "--once"]) == 2


@pytest.mark.gpu
def test_scenario_replay_with_real_models_on_runpod() -> None:
    """자리만 둔다: 실제 검출기(YOLO26x-pose TensorRT)·TrackTrack 으로 같은 시나리오를 재생하는 것은 RunPod 몫이다(-183 큐).
    합성 영상에는 사람 그림이 없어 실제 모델 입력이 될 수 없으므로 시나리오 녹화(시연 동의 녹화)가 생긴 뒤 정한다."""
    pytest.skip("RunPod 실제 모델 시나리오 재생은 아직 없다(S15P31A101-41 남은 일)")
