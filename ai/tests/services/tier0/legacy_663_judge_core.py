"""S15P31A101-663 이전 구현의 고정 사본(dev 540936b4, 함수 본문 그대로·들여쓰기만 바꿈). 동치 시험 기준 경로용이다.

- `preferred_partition_pairs`: `CrossViewAssociator._preferred_partition_pairs` (허용 간선을 쌍마다 계산하던 반복).
- `cluster_gallery_sim`: `WorldTrackManager._cluster_gallery_sim` (tick 안 재사용 없음).
- `median`: `_pair_3d_distance` 가 쓰던 `float(np.median(values))`.

고치지 않는다(비교 기준이 바뀐다). 그 밖의 이전 경로(메모 없는 `_graph_linkage`·쌍 문턱, `WorldState.from_payload`)는 지금 모듈에
남아 있는 계산 함수를 그대로 부른다(test_judge_core_equivalence.reference_mode).
"""

# ruff: noqa: B023  (이전 구현 그대로: 분기 한정 탐색의 닫힘 변수)
from __future__ import annotations

import itertools
import math
import statistics
from typing import Any

import numpy as np

TrackKey = tuple[str, str]
PairEvidence = Any
_Work = Any


def median(values: np.ndarray) -> float:
    return float(np.median(values))


def preferred_partition_pairs(
    self,
    works: list[_Work],
    alive: np.ndarray,
    cost: np.ndarray,
    marginal: np.ndarray,
    blocked: np.ndarray,
    member: np.ndarray,
    pairs: dict[tuple[TrackKey, TrackKey], PairEvidence],
    pair_joints: dict[tuple[TrackKey, TrackKey], np.ndarray],
    prior_pairs: set[tuple[TrackKey, TrackKey]],
) -> set[tuple[int, int]] | None:
    """연결 성분 안에 기하 비호환(삼각 불일치·카메라 중복) 충돌이 있을 때 분기 한정(branch-and-bound)으로
    최적 제약 그래프 분할을 구해 같은 클러스터에 속하는 `(i, j)` 쌍 집합을 돌려준다. 충돌이 없으면 None."""
    cfg = self.config
    active = np.flatnonzero(alive).tolist()
    if len(active) < 3:
        return None
    adj: dict[int, list[int]] = {u: [] for u in active}
    valid_edges: list[tuple[int, int]] = []
    for idx_a, i in enumerate(active):
        for j in active[idx_a + 1 :]:
            c = float(cost[i, j])
            if not (c < cfg.new_cluster_cost) or blocked[i, j] or marginal[i, j]:
                continue
            shares_a = (member & member[i]).any(axis=1)
            shares_b = (member & member[j]).any(axis=1)
            near = (shares_b & (cost[i] - c < cfg.ambiguity_margin)) | (
                shares_a & (cost[j] - c < cfg.ambiguity_margin)
            )
            near[[i, j]] = False
            if near.any():
                continue
            adj[i].append(j)
            adj[j].append(i)
            valid_edges.append((i, j))
    if len(valid_edges) < 2:
        return None

    visited: set[int] = set()
    preferred: set[tuple[int, int]] = set()
    has_conflict = False
    for start in active:
        if start in visited or not adj[start]:
            continue
        comp: list[int] = []
        queue = [start]
        visited.add(start)
        while queue:
            cur = queue.pop()
            comp.append(cur)
            for nxt in adj[cur]:
                if nxt not in visited:
                    visited.add(nxt)
                    queue.append(nxt)
        comp.sort()
        if len(comp) <= 2:
            preferred.add((comp[0], comp[1]))
            continue
        # 성분 전체가 하나의 유효 클러스터로 합쳐질 수 있는지 먼저 확인한다
        all_keys = [k for u in comp for k in works[u].keys]
        if len({k[0] for k in all_keys}) == len(all_keys):
            c_all, m_all = self._graph_linkage(all_keys[:1], all_keys[1:], pairs, pair_joints, prior_pairs)
            if c_all < cfg.new_cluster_cost and not m_all:
                for u, v in itertools.combinations(comp, 2):
                    preferred.add((u, v))
                continue
        if len(comp) > 12:
            for u, v in itertools.combinations(comp, 2):
                if v in adj[u]:
                    preferred.add((u, v))
            continue
        has_conflict = True
        comp_set = set(comp)
        # 성분 안에서 연결된 유효 부분집합(크기 1..4)과 그 상관 클러스터링 점수를 구한다
        valid_subsets: dict[frozenset[int], tuple[float, float, TrackKey]] = {}
        for u in comp:
            valid_subsets[frozenset((u,))] = (0.0, 0.0, min(works[u].keys))
        max_k = min(4, len(comp))
        for sz in range(2, max_k + 1):
            for sub in itertools.combinations(comp, sz):
                sub_fs = frozenset(sub)
                sub_keys = [k for u in sub for k in works[u].keys]
                if len({k[0] for k in sub_keys}) != len(sub_keys) or len(sub_keys) > 4:
                    continue
                # 허용 간선 부분그래프가 연결되어 있는지 확인
                seen_sub = {sub[0]}
                q_sub = [sub[0]]
                while q_sub:
                    cur = q_sub.pop()
                    for nxt in adj[cur]:
                        if nxt in sub_fs and nxt not in seen_sub:
                            seen_sub.add(nxt)
                            q_sub.append(nxt)
                if len(seen_sub) != sz:
                    continue
                # 부분집합 전체가 기하·삼각·3D 일관성을 만족하는지 확인
                c_sub, m_sub = self._graph_linkage(sub_keys[:1], sub_keys[1:], pairs, pair_joints, prior_pairs)
                if not (c_sub < cfg.new_cluster_cost) or m_sub:
                    continue
                score = 0.0
                pair_costs: list[float] = []
                for u, v in itertools.combinations(sub, 2):
                    c_uv = float(cost[u, v])
                    if math.isfinite(c_uv):
                        score += cfg.new_cluster_cost - c_uv
                        pair_costs.append(c_uv)
                mean_c = statistics.fmean(pair_costs) if pair_costs else c_sub
                valid_subsets[sub_fs] = (score, -mean_c, min(sub_keys))

        by_first: dict[int, list[tuple[frozenset[int], float, float, TrackKey]]] = {u: [] for u in comp}
        for sub_fs, (sc, neg_mc, min_k) in valid_subsets.items():
            first_u = min(sub_fs)
            by_first[first_u].append((sub_fs, sc, neg_mc, min_k))
        for u in comp:
            by_first[u].sort(key=lambda item: (item[1], item[2]), reverse=True)

        best_key: tuple[float, float] | None = None
        best_part: list[frozenset[int]] = []

        def search(remaining: frozenset[int], acc_score: float, acc_neg_cost: float, chosen: list[frozenset[int]]) -> None:
            nonlocal best_key, best_part
            if not remaining:
                cand_key = (round(acc_score, 9), round(acc_neg_cost, 9))
                if best_key is None or cand_key > best_key:
                    best_key = cand_key
                    best_part = list(chosen)
                return
            # 낙관적 상한 가지치기: 남은 정점이 이룰 수 있는 최대 쌍 수 × new_cluster_cost
            rem_n = len(remaining)
            max_possible_pairs = rem_n * min(3, rem_n - 1) / 2.0
            if best_key is not None and acc_score + max_possible_pairs * cfg.new_cluster_cost < best_key[0] - 1e-9:
                return
            pivot = min(remaining)
            for sub_fs, sc, neg_mc, _ in by_first[pivot]:
                if sub_fs <= remaining:
                    chosen.append(sub_fs)
                    search(remaining - sub_fs, acc_score + sc, acc_neg_cost + neg_mc, chosen)
                    chosen.pop()

        search(frozenset(comp_set), 0.0, 0.0, [])
        for part in best_part:
            if len(part) >= 2:
                for u, v in itertools.combinations(sorted(part), 2):
                    preferred.add((u, v))
    return preferred if has_conflict else None


def cluster_gallery_sim(self, world_id: str, cluster: Any, t: int) -> float | None:
    gallery = self.galleries.get(world_id)
    if gallery is None or len(gallery) == 0:
        return None
    feats = self._cluster_features(cluster, t)
    if not feats:
        return None
    best = -1.0
    for f in feats:
        sim = gallery.similarity(f.embedding, camera_id=f.camera_id, face_embedding=getattr(f, "face_embedding", None))
        if sim is not None and sim > best:
            best = sim
    return best if best >= 0.0 else None
