"""config_ack 봉투 schema_version 이 메시지 계약 버전과 같은지 본다(-648).

예전에는 계약 묶음 번호 2.0.0 을 실어 EC2 가 메이저 불일치로 config_ack 를 전부 버렸다.
"""

import json
from pathlib import Path

from simai_ai.config.sync import CONFIG_ACK_SCHEMA_VERSION

REPO = Path(__file__).resolve().parents[3]


def test_config_ack_봉투_버전은_계약_예시와_같다():
    example = json.loads((REPO / "contracts/examples/config_ack.json").read_text(encoding="utf-8"))
    assert CONFIG_ACK_SCHEMA_VERSION == example["schema_version"]


def test_config_ack_메이저는_1이다():
    assert CONFIG_ACK_SCHEMA_VERSION.split(".")[0] == "1"
