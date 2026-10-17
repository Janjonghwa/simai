"""최소 비용 1:1 배정 (헝가리안, 명세 5.2 "전체 1:1 배정").

scipy 를 기본·extras 의존성에 더하지 않으려고 표준 라이브러리로 둔다. 크기가 작다(카메라 한 대의 검출 × 클러스터,
한 시각 수십 개 이하). 알고리즘은 포텐셜을 쓰는 O(n²·m) 헝가리안(Kuhn-Munkres, 행 ≤ 열)이고, 행이 열보다
많으면 전치해서 푼다. 결과는 `scipy.optimize.linear_sum_assignment` 와 같은 뜻이다: min(행, 열) 개의 짝을 고르고
고른 비용 합이 최소다.

비용은 유한한 수만 받는다. 금지 짝은 호출자가 충분히 큰 유한 값으로 두고 결과에서 걸러낸다.
"""

from __future__ import annotations

import math
from collections.abc import Sequence


def linear_sum_assignment(cost: Sequence[Sequence[float]]) -> list[tuple[int, int]]:
    """비용 행렬(행 n × 열 m)의 최소 비용 배정. 행 번호 순서의 (행, 열) 목록, 길이 min(n, m)."""
    n = len(cost)
    if n == 0:
        return []
    m = len(cost[0])
    if any(len(row) != m for row in cost):
        raise ValueError("비용 행렬의 행 길이가 서로 다르다")
    if m == 0:
        return []
    for row in cost:
        for value in row:
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
                raise ValueError(f"비용은 유한한 수여야 한다 (받은 값 {value!r}). 금지 짝은 큰 유한 값으로 둔다")
    if n > m:
        transposed = [[cost[i][j] for i in range(n)] for j in range(m)]
        return sorted((i, j) for j, i in linear_sum_assignment(transposed))

    inf = math.inf
    u = [0.0] * (n + 1)
    v = [0.0] * (m + 1)
    p = [0] * (m + 1)  # p[j]: 열 j 에 배정된 행 (1부터, 0 은 비어 있음)
    way = [0] * (m + 1)
    for i in range(1, n + 1):
        p[0] = i
        j0 = 0
        minv = [inf] * (m + 1)
        used = [False] * (m + 1)
        while True:
            used[j0] = True
            i0 = p[j0]
            delta = inf
            j1 = 0
            row = cost[i0 - 1]
            for j in range(1, m + 1):
                if used[j]:
                    continue
                cur = row[j - 1] - u[i0] - v[j]
                if cur < minv[j]:
                    minv[j] = cur
                    way[j] = j0
                if minv[j] < delta:
                    delta = minv[j]
                    j1 = j
            for j in range(m + 1):
                if used[j]:
                    u[p[j]] += delta
                    v[j] -= delta
                else:
                    minv[j] -= delta
            j0 = j1
            if p[j0] == 0:
                break
        while True:
            j1 = way[j0]
            p[j0] = p[j1]
            j0 = j1
            if j0 == 0:
                break
    return sorted((p[j] - 1, j - 1) for j in range(1, m + 1) if p[j] != 0)
