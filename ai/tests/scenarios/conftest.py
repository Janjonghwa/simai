"""시나리오 재생 정답 비교 시험 공용 (S15P31A101-41): marker slow 등록.

느린 시나리오(장면 20 s 초과)는 SIMAI_SCENARIO_SLOW=1 일 때만 pytest 로 돈다. 전체는 golden.py 스크립트가 돌린다.
"""


def pytest_configure(config):
    config.addinivalue_line("markers", "slow: 오래 걸리는 시나리오 재생 (SIMAI_SCENARIO_SLOW=1 일 때만 돈다, 전체는 golden.py)")
