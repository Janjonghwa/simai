"""헝가리안 1:1 배정이 완전 탐색과 같은 최소 비용을 내는지 본다."""

import itertools
import random

import pytest

from simai_ai.tracking.assignment import linear_sum_assignment


def _brute(cost):
    n, m = len(cost), len(cost[0])
    best = None
    if n <= m:
        for cols in itertools.permutations(range(m), n):
            total = sum(cost[i][c] for i, c in enumerate(cols))
            best = total if best is None else min(best, total)
    else:
        for rows in itertools.permutations(range(n), m):
            total = sum(cost[r][j] for j, r in enumerate(rows))
            best = total if best is None else min(best, total)
    return best


@pytest.mark.parametrize("shape", [(1, 1), (3, 3), (2, 5), (5, 2), (4, 4), (4, 6), (6, 3)])
def test_matches_brute_force(shape):
    rng = random.Random(hash(shape) & 0xFFFF)
    for _ in range(30):
        cost = [
            [rng.choice([rng.uniform(-5, 5), float(rng.randint(0, 3))]) for _ in range(shape[1])]
            for _ in range(shape[0])
        ]
        pairs = linear_sum_assignment(cost)
        assert len(pairs) == min(shape)
        assert len({r for r, _ in pairs}) == len(pairs) and len({c for _, c in pairs}) == len(pairs)
        assert pairs == sorted(pairs)
        assert sum(cost[r][c] for r, c in pairs) == pytest.approx(_brute(cost))


def test_empty_and_errors():
    assert linear_sum_assignment([]) == []
    assert linear_sum_assignment([[], []]) == []
    with pytest.raises(ValueError):
        linear_sum_assignment([[1.0, 2.0], [1.0]])
    with pytest.raises(ValueError):
        linear_sum_assignment([[float("inf"), 1.0]])
    with pytest.raises(ValueError):
        linear_sum_assignment([[True, 1.0]])
