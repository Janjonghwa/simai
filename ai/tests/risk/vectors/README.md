# Risk Engine 공통 테스트 벡터

같은 입력에 같은 판정이 나오는지 Tier 0 배포 이미지와 EC2 컨테이너 이미지 양쪽에서 확인하는 입력·기대 출력 세트다(명세 3.2, 15.1 G3, 21.3).
형식과 실행기의 정본은 `ai/src/simai_ai/risk/vectors.py` 머리말이다.

```sh
# ai/ 에서
uv run python -m simai_ai.risk.vectors tests/risk/vectors   # 모두 통과하면 종료 코드 0
uv run pytest tests/risk                                   # 위 실행을 포함한 시험
```

## 형식 요약 (format_version 1.x)

| 필드 | 내용 |
|---|---|
| `vector_id`, `description` | 식별자와 확인하려는 것 |
| `origin` | `LIVE` / `PLANNED` / `REPLAY` / `WHAT_IF`. LIVE 가 아니면 알림 후보 0건을 함께 검사한다 |
| `rules` | `[{rule_id, rule_version}]`. 등록부(`default_registry`)에서 만든다 |
| `zones` | 구역 목록. 판정 시각(`clock_ms`)에 유효한 버전만 쓴다 |
| `steps[]` | `clock_ms`(고정 시계, 줄지 않음), `world_state`(I1 payload, `contracts/messages/world_state.schema.json`), `expect.findings` |

- 기대 판정은 `rule_id, rule_version, severity, world_ids, zone_id, needs_check, reasons, metrics`를 모두 적는다. 순서는 상관없다.
- 사람 대상 판정은 `world_ids`에 World ID를 적는다. 사람 없는 구역·시스템 판정(R7 형태)은 `world_ids: []`, `zone_id`를 채운다. 가짜 World ID를 만들지 않는다.
- `metrics`는 절대 오차 1e-9 안에서 같으면 통과, 나머지는 정확히 같아야 한다.
- 시각은 UTC epoch ms 정수, 좌표는 World(m, +Z 상방). 모르는 값은 null 로 두고 Z=0 으로 채우지 않는다.
- 정답·시뮬레이션 입력으로 만든 벡터의 결과를 제품 성능으로 쓰지 않는다.

## 현재 벡터

| 파일 | 확인하는 것 |
|---|---|
| `empty_scene.json` | 빈 장면, 규칙 없음 → 판정 없음 |
| `no_rules.json` | 구역 안 사람(관측·예측, 접지 미확인 포함), 규칙 없음 → 판정 없음 |
| `replay_zone_versions.json` | REPLAY, 두 단계, 구역 버전 전환 → 판정 없음 |

구체 규칙(R1~R7)이 등록되면 해당 이슈에서 규칙별 벡터를 이 디렉터리에 추가한다.
