"""규칙 표시 이름·표시 번호 사전 시험 (S15P31A101-804)."""

from __future__ import annotations

import pytest

from simai_ai.risk.names import DISPLAY_RULE_IDS, REPORT_NAME_GUIDE, RULE_DESCRIPTIONS, display_rule_id

IMPLEMENTED_RULES = {"R1", "R2", "R3", "R4", "R5", "R6", "R7", "R9", "R11"}


def test_unimplemented_rules_r8_r10_are_excluded():
    assert set(RULE_DESCRIPTIONS) == IMPLEMENTED_RULES
    assert set(DISPLAY_RULE_IDS) == IMPLEMENTED_RULES
    assert "R8" not in RULE_DESCRIPTIONS
    assert "R10" not in RULE_DESCRIPTIONS


@pytest.mark.parametrize(
    ("internal_id", "expected"),
    [
        ("R1", "R1"),
        ("R2", "R2"),
        ("R3", "R3"),
        ("R4", "R4"),
        ("R5", "R5"),
        ("R6", "R6"),
        ("R7", "R7"),
        ("R9", "R8"),
        ("R11", "R9"),
        ("R8", "R8"),
        ("R10", "R10"),
        ("R99", "R99"),
    ],
)
def test_display_rule_id_compacts_only_r9_and_r11(internal_id, expected):
    assert display_rule_id(internal_id) == expected


def test_report_name_guide_excludes_r8_r10_and_specifies_display_numbers():
    assert "장비 근접" not in REPORT_NAME_GUIDE
    assert "불확실 점유 근접" not in REPORT_NAME_GUIDE
    assert "R9=안전모 미착용" in REPORT_NAME_GUIDE
    assert "R11=조끼 미착용" in REPORT_NAME_GUIDE
    assert "R9→R8" in REPORT_NAME_GUIDE
    assert "R11→R9" in REPORT_NAME_GUIDE
    assert "입력 규칙 ID와 등급 값 자체는 바꾸지 않는다." in REPORT_NAME_GUIDE
