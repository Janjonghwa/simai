"""OpenCV 스레드 수 제한 (S15P31A101-662 자원 격리). Tier 0·아바타·분석 워커 진입점이 시작할 때 한 번 부른다.

cv2 는 기본으로 보이는 CPU 수만큼 스레드 묶음을 만든다(Pod 224개). 같은 컨테이너의 Tier 0 수신·디코딩·검출·판정 스레드와 사이드카가
CPU 할당량(cgroup)을 나눠 쓰므로, cv2 의 병렬 구간(resize·imencode·remap)이 판정 스레드의 CPU 를 빼앗지 않게 1로 묶는다(배경 누적·
회전 복구 자식은 이미 1, -563). 되돌리기: SIMAI_CV2_THREADS=keep(cv2 기본값 그대로). cv2 가 없으면(노트북·CI) 건너뛴다.
"""

from __future__ import annotations

from collections.abc import Mapping

CV2_THREADS_ENV = "SIMAI_CV2_THREADS"
DEFAULT_CV2_THREADS = "1"
KEEP = ("keep", "default")


def limit_cv2_threads(env: Mapping[str, str]) -> int | None:
    """SIMAI_CV2_THREADS(기본 1)만큼 cv2.setNumThreads. 고친 값을 돌려주고, keep·cv2 없음이면 None. 형식이 틀리면 ValueError."""
    raw = (env.get(CV2_THREADS_ENV) or DEFAULT_CV2_THREADS).strip().lower()
    if raw in KEEP:
        return None
    if not raw.isdigit():
        raise ValueError(f"{CV2_THREADS_ENV} 는 0 이상 정수 또는 keep 이다: {raw!r}")
    try:
        import cv2
    except ImportError:
        return None
    cv2.setNumThreads(int(raw))
    return int(raw)
