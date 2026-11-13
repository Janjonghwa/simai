"""S15P31A101-663 판정 중심 최적화의 동치 시험 (docs/design/S15P31A101-663-judge-core-optimize.md).

실제 Perception(Cross-view → World Track → 접지 → world_state)과 JudgmentLoop(구성 묶음·Risk Engine·LifecycleTracker·저널)를
합성 장면(judge_core_scenes.py)으로 돌려, 이전 경로와 지금 경로의 trace 가 바이트 단위로 같은지 본다. trace 는 tick 마다
world_state payload 전체(World ID·상태·위치·σ·관절·identity 필드·접지), Cross-view 클러스터(키·불확실 사유·경쟁 후보)와 쌍 근거
(탈락 사유·비용·경계·잔차), Track 별 Re-ID 내부 판단(갤러리 유사도·margin·스왑 의심 기한), ID 교정(병합·분할), RiskEvent 전이
payload, 현장 경보 신호, 끝의 World Track 진단 계수·발급 ID 목록이다. 부동소수도 repr 왕복 문자열이라 허용 오차가 없다(비트 단위로 같아야 한다).

- 기준 경로(reference_mode): 바꾸기 전 함수의 고정 사본(legacy_663_judge_core.py)과, 메모를 끈 지금 계산 함수
  (`_assign_graph_compute`·`_pairs_compute`·`_graph_linkage_compute`·`WorldState.from_payload`)를 끼운다.
- 선택: 환경 변수 SIMAI_JUDGE_EQUIV_BASE=<git 커밋>(예: 540936b4) 이면 그 커밋의 ai/src·ai/services 를 꺼내 별도
  프로세스로 같은 장면 전체(mixed 300 + crowd 3·5·7·9 × 200 tick)를 돌려 비교한다. 이후 티켓이 동작을 바꾸면 깨지므로 기본은 건너뛴다.
"""

from __future__ import annotations

import io
import json
import os
import subprocess
import sys
import tarfile
from collections import Counter
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
AI_DIR = HERE.parents[2]
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

import judge_core_scenes as jc
import legacy_663_judge_core as legacy


@pytest.fixture
def reference_mode(monkeypatch):
    """이전 구현 경로로 바꿔 끼운다(모듈 설명)."""
    from simai_ai.risk.world import WorldState
    from simai_ai.tracking import cross_view as cv
    from simai_ai.tracking import world_track as wt

    def assign_graph_without_memo(self, prepared, pairs, pair_joints, prior_pairs, flags):
        self._prepared_now = prepared
        self._memo = None
        return self._assign_graph_compute(prepared, pairs, pair_joints, prior_pairs, flags)

    def apply() -> None:
        monkeypatch.setattr(cv.CrossViewAssociator, "_assign_graph", assign_graph_without_memo)
        monkeypatch.setattr(cv.CrossViewAssociator, "_pairs", lambda self, prepared: self._pairs_compute(prepared))
        monkeypatch.setattr(cv.CrossViewAssociator, "_preferred_partition_pairs", legacy.preferred_partition_pairs)
        monkeypatch.setattr(cv, "_median_1d", legacy.median)
        monkeypatch.setattr(wt.WorldTrackManager, "_cluster_gallery_sim", legacy.cluster_gallery_sim)
        monkeypatch.setattr(WorldState, "from_payload_fast", classmethod(lambda cls, payload: cls.from_payload(payload)))

    return apply


def _trace(scene: jc.Scene, tmp: Path) -> tuple[str, dict]:
    trace = jc.run_scene(scene, tmp)["trace"]
    return jc.trace_json(trace), trace


def _coverage(trace: dict) -> Counter:
    seen: Counter = Counter()
    for tick in trace["ticks"]:
        for tr in tick["transitions"]:
            seen[(tr["rule_id"], tr["transition"])] += 1
        for rev in tick["revisions"]:
            seen[("revision", rev[0])] += 1
    for key, value in trace["diag"].items():
        seen[key] += value
    return seen


def test_mixed_scene_matches_reference(tmp_path, reference_mode):
    """혼합 장면 300 tick: 근접 2인·가림·2+2 분할 그림자·병합·분할·재입장·낙상·구역 진입."""
    current, trace = _trace(jc.mixed(300), tmp_path / "current")
    reference_mode()
    reference, _ = _trace(jc.mixed(300), tmp_path / "reference")
    assert current == reference
    # 장면이 의도한 상황을 실제로 지났는지(장면이 바뀌어 비교가 헐거워지지 않게)
    seen = _coverage(trace)
    assert seen[("R1", "RAISED")] >= 1 and seen[("R4", "RAISED")] >= 1 and seen[("R5", "RAISED")] >= 1
    assert seen[("revision", "MERGE")] >= 1
    assert seen["reid.reentry_reconnected"] >= 1
    assert seen["uncertain.attached"] >= 1
    assert len(trace["issued"]) > len(jc.mixed(300).actors)  # 2+2 분할 그림자가 ID 를 하나 더 낸다


def test_crowd_matches_reference(tmp_path, reference_mode):
    """7명 120 tick: 근접 쌍(0.45 m)·카메라별 가림 창·ROI 거르기(4명 이상)."""
    current, trace = _trace(jc.crowd(7, 120), tmp_path / "current")
    reference_mode()
    reference, _ = _trace(jc.crowd(7, 120), tmp_path / "reference")
    assert current == reference
    assert len(trace["issued"]) >= 7


def test_negative_controls_change_trace(tmp_path, monkeypatch):
    """음성 대조: 메모 무효화 조건을 빼면 trace 가 달라진다(비교가 그 차이를 잡는다).

    - 갤러리 유사도 메모가 갤러리 version 을 보지 않으면(같은 tick 에 갱신된 갤러리의 옛 유사도를 다시 쓰면) Re-ID 판단이 달라진다.
    - 병합 비용 메모 키에서 b 를 빼면 Cross-view 연결이 달라진다.
    """
    from simai_ai.tracking import cross_view as cv
    from simai_ai.tracking import world_track as wt

    scene = jc.crowd(7, 60)
    current, _ = _trace(scene, tmp_path / "current")
    original = wt.WorldTrackManager._cluster_gallery_sim

    def stale_gallery_sim(self, world_id, cluster, t):
        gallery = self.galleries.get(world_id)
        if gallery is None or len(gallery) == 0:
            return None
        hit = self._gallery_sim_memo.get((world_id, cluster.keys))
        return hit[2] if hit is not None else original(self, world_id, cluster, t)

    with monkeypatch.context() as m:
        m.setattr(wt.WorldTrackManager, "_cluster_gallery_sim", stale_gallery_sim)
        stale, _ = _trace(jc.crowd(7, 60), tmp_path / "stale")
    assert stale != current

    def linkage_without_b(self, a, b, pairs, pair_joints, prior_pairs):
        memo = self._memo
        if memo is None:
            return self._graph_linkage_compute(a, b, pairs, pair_joints, prior_pairs)
        key = (tuple(a), ())
        if key not in memo.linkage:
            memo.linkage[key] = self._graph_linkage_compute(a, b, pairs, pair_joints, prior_pairs)
        return memo.linkage[key]

    with monkeypatch.context() as m:
        m.setattr(cv.CrossViewAssociator, "_graph_linkage", linkage_without_b)
        broken, _ = _trace(jc.crowd(7, 60), tmp_path / "broken")
    assert broken != current


@pytest.mark.skipif(not os.environ.get("SIMAI_JUDGE_EQUIV_BASE"), reason="SIMAI_JUDGE_EQUIV_BASE=<git 커밋> 일 때만")
def test_trace_matches_base_commit(tmp_path):
    base_ref = os.environ["SIMAI_JUDGE_EQUIV_BASE"]
    repo = AI_DIR.parent
    data = subprocess.run(["git", "archive", base_ref, "ai/src", "ai/services"], cwd=repo, capture_output=True,
                          check=True).stdout
    base_dir = tmp_path / "base"
    with tarfile.open(fileobj=io.BytesIO(data)) as tar:
        tar.extractall(base_dir, filter="data")
    script = HERE / "judge_core_scenes.py"
    common = {**os.environ, "PYTHONUTF8": "1", "SIMAI_CONTRACTS_DIR": str(repo / "contracts")}
    outputs = {}
    for name, paths in (("base", [base_dir / "ai" / "src", base_dir / "ai"]), ("current", [])):
        env = dict(common)
        env["PYTHONPATH"] = os.pathsep.join([*(str(p) for p in paths), *filter(None, [os.environ.get("PYTHONPATH")])])
        out = tmp_path / f"{name}.json"
        done = subprocess.run([sys.executable, str(script), "--output", str(out)], cwd=AI_DIR, env=env,
                              capture_output=True, text=True, encoding="utf-8", check=True)
        used = json.loads(done.stdout.strip().splitlines()[-1])
        expect_base = name == "base"
        assert str(base_dir) in used["services"] if expect_base else str(base_dir) not in used["services"]
        assert str(base_dir) in used["simai_ai"] if expect_base else str(base_dir) not in used["simai_ai"]
        outputs[name] = out.read_bytes()
    assert outputs["base"] == outputs["current"]
