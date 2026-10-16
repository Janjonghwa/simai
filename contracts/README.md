# contracts

구간 계약의 정본(JSON Schema, draft 2020-12). 명세 12장, 20.4, 21.2 기준. **초안 v1.0.0.**

| 경로 | 내용 | 명세 |
|---|---|---|
| `common/defs.schema.json` | 공통 타입(좌표, 시각, 상태 enum, `input_provenance`, `reproducibility`) | 4.1, 6.2, 12.1, 20.4 |
| `messages/envelope.schema.json` | 공통 봉투 | 12.1 |
| `messages/world_state.schema.json` | I1 `world_state` (LIVE 계약, Lead 승인 필요) | 5.2, 12.3 |
| `providers/calibration.schema.json` | CalibrationProvider 출력 | 4장, 20.4 |
| `providers/world_tracks.schema.json` | WorldTrackProvider 출력 (JSONL 한 줄) | 5.2, 20.4 |
| `experiments/scene_output.schema.json` | EXP-01 정적 장면 결과 | 5.3, 20.2 |
| `experiments/human_mesh_output.schema.json` | EXP-02 `avatar_asset` / `pose_frame` | 5.6, 20.3, 20.4 |
| `experiments/run_record.schema.json` | 실험 Run 기록 | 20.5 |

나머지 I0~I5 메시지(`risk_event_transition`, `health`, `local_alert` 등)는 M1 착수 전에 `messages/`에 추가한다.

## 스키마로 막는 불변 규칙

- 지지 표면이 VALID가 아니거나 `z_validity`가 UNAVAILABLE이면 `ground_anchor_xyz`는 null (Z=0 대체 금지)
- `DATASET_GT`·`SIM` 보정은 CALIBRATED 불가, CALIBRATED는 독립 검증점 결과 필수
- 정답·시뮬레이션 입력(`is_upper_bound: true`) 결과는 COMMITTED Scene 불가
- HMR 포즈의 root 출처는 `TIER0`만 허용, 외형 텍스처는 `face_policy`(EXCLUDED/BLURRED)와 `consent_ref` 필수
- 좌표계 선언은 World(m, 오른손, +Z)만 허용

스키마로 표현할 수 없는 규칙(R의 직교성, 관절 배열 길이 일치, `is_upper_bound`와 출처의 일치 등)은 각 모듈의 계약 시험에서 검사한다(명세 21.3).

## 검증

```
uv run contracts/scripts/validate_examples.py
```

- `examples/<schema>[.<variant>].json`은 통과해야 한다.
- `examples/invalid/<schema>.<case>.json`은 실패해야 한다. 불변 규칙을 하나 추가하면 그 위반 예시도 하나 추가한다.
- 스키마를 바꾸면 예시도 같이 바꾼다. 버전 규칙: 필드 추가는 마이너, 삭제·의미 변경은 메이저(12.1).
