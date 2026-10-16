"""Risk Engine 자리표시 테스트. extras 없는 기본 설치에서 돌아야 한다(ai/CONVENTION.md 실행 절)."""

from importlib.metadata import version


def test_risk_imports_on_base_install():
    import simai_ai
    import simai_ai.risk  # noqa: F401

    assert simai_ai.__version__ == version("simai-ai")
