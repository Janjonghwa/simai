"""세팅 굽기·재구성 자식 프로세스의 CPU 몫(S15P31A101-672).

224코어 호스트·약 24 vCPU cgroup 의 Pod 에서 굽기가 `os.cpu_count()`(224) 스레드를 띄우면 컨테이너 CFS 예산을 잠식해 Tier 0 가
밀린다. 그렇다고 고정 소수 스레드로 묶으면 굽기(Open3D TSDF·texrecon, OpenMP)가 몇 배 느려진다(명세 5.3 은 굽기에 CPU 를 최대한 쓴다).
그래서 프로세스 affinity·cgroup 상한을 반영한 가용 코어의 절반을 기본으로 쓰고 나머지를 Tier 0 몫으로 남긴다. `SIMAI_BAKE_NUM_THREADS` 로 덮어쓸 수 있다.
"""

from __future__ import annotations

import os
from pathlib import Path


def cgroup_cpu_quota() -> int | None:
    """컨테이너 cgroup v2/v1 CPU 상한(올림 정수 코어 수). 제한이 없거나 읽지 못하면 None."""
    for max_path in (Path("/sys/fs/cgroup/cpu.max"),):
        try:
            raw = max_path.read_text(encoding="utf-8").strip().split()
            if len(raw) >= 2 and raw[0] != "max":
                quota, period = int(raw[0]), int(raw[1])
                if quota > 0 and period > 0:
                    return max(1, (quota + period - 1) // period)
        except (OSError, ValueError):
            pass
    for base in (Path("/sys/fs/cgroup/cpu,cpuacct"), Path("/sys/fs/cgroup/cpu")):
        try:
            quota = int((base / "cpu.cfs_quota_us").read_text(encoding="utf-8").strip())
            period = int((base / "cpu.cfs_period_us").read_text(encoding="utf-8").strip())
            if quota > 0 and period > 0:
                return max(1, (quota + period - 1) // period)
        except (OSError, ValueError):
            pass
    return None


def usable_cpu_count() -> int:
    """프로세스 CPU 친화성과 cgroup 상한을 반영한 가용 코어 수."""
    count = os.cpu_count() or 1
    if hasattr(os, "sched_getaffinity"):
        try:
            affinity = len(os.sched_getaffinity(0))
            if affinity > 0:
                count = min(count, affinity)
        except OSError:
            pass
    quota = cgroup_cpu_quota()
    if quota is not None:
        count = min(count, quota)
    return max(1, count)


def default_bake_threads() -> int:
    """가용 코어의 절반(최소 1). 나머지 절반은 Tier 0·관제 몫으로 남긴다."""
    return max(1, usable_cpu_count() // 2)

