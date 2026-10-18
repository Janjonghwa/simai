"""구체 위험 규칙 (명세 7.2). 규칙마다 모듈 하나를 두고 registry.default_registry 에 등록한다.

| 모듈 | 규칙 |
|---|---|
| r1_zone_entry | R1 위험 구역 진입(GROUND_FOOTPRINT·VOLUME_3D, 불확실성 범위, 해소 간격). R6 으로 감싸 등록 |
| r5_entry_prediction | R5 진입 예측(현재 속도로 2초 안 R1 구역 진입, CAUTION, 선행 시간). 속도를 모르면 발생 판정 없이 유지(holding)만 |
| r6_observation_loss | R6 위험 구역 내 관측 상실. 구역 규칙을 감싸는 ObservationLossHold(자기 ID 이벤트 없음) |
| r7_monitoring | R7 구역 감시 능력 저하. 구역 대상 판정(world_ids 없음) |

이 파일은 아무것도 import 하지 않는다. 규칙을 추가할 때 여기를 고치지 않아도 되게 한다(표만 갱신).
"""
