# `kanana-2-v-decide-3b-2611` 벤치마크 (`S15P31A101-538`)

카카오 `kakaocorp/kanana-2-v-decide-3b-2611`(3.67B, 단일 포워드 패스 결정형 VLM, `output_tokens=0`)을 우리 안전 관제 플랫폼의 3가지 시각 판정 과업에 적용해 정확도·일치율·확률 교정 곡선(Calibration Curve)·지연시간·GPU 메모리를 측정하는 벤치마크다.

## 1. 한국어 질문 설계 (`questions.py`)

모델의 `model.system_one(state=..., questions=..., images=..., image_root=...)` 규약(`decision_kanana_v_decide.py`)에 맞춰 3종 질문 묶음을 정의한다.

1. **(A) 위험 이벤트 (`risk_event`)** — 이벤트 크롭(1~4장) + 구조화 상태 JSON(`rule_id`, `zone_id`, `duration_s`, `metrics`)
   - `risk_type` (`choice`): `R1`~`R9` 실제 규칙 정의(`docs/spec/07-risk-rules.md`, `ai/src/simai_ai/risk/rules/`) 중 하나 선택
   - `upgrade` (`score`, 3단계): **현행 유지(0) / HIGH 상향 권고(1) / CRITICAL 상향 권고(2)**.
     - **깨면 안 되는 규칙 4 준수**: 하향·경보 해제·무시 선택지를 절대 두지 않으며, `score_to_proposed_severity()` 가 `simai_ai.vlm.rationale.check_proposal()` 을 통해 현재 등급 이하의 제안을 `(None, "NOT_UPWARD")` 로 버린다.
   - `person_in_zone` (`noul`): 사람이 실제로 위험 구역·가장자리 안에 위치하는지 확률(0~1)
2. **(B) PPE (`ppe`)** — 작업자 크롭(1~4장) + 작업 정책(`ppe_policy`)
   - `helmet` (`noul`): 머리에 안전모를 올바르게 착용했는지 확률(손에 들거나 테이블에 놓인 헬멧은 미착용)
   - `vest` (`noul`): 상체에 안전조끼를 착용했는지 확률
   - **[YOLO26x(기준) | decide-3b | 둘 결합]** 3자 비교:
     - **기준(`YOLO26x`)**: 우리가 학습한 **T1-v4-ALL YOLO26x**(`simai-s15p31a101/simai-ppe-helmet-vest`, revision `36e3ebb1…`, `T1-v4-ALL-seed20261021-epoch08.pt`, sha256 `e373a1048057…`)를 운영 `simai_ai.ppe.detector.PpeDetector` 와 `simai_ai.ppe.state.PpeJudge.combine_bin` 그대로 import 해 같은 사람 크롭에서 추론한다.
     - **결합(`combine_yolo_and_decide_ppe`)**:
       1. `YOLO26x` 가 `NOT_WORN`(`NO_HELMET` / `NO_VEST`)으로 판정한 경우, `decide-3b` 가 절대 `WORN` 이나 `UNKNOWN` 으로 뒤집지 못한다 (`YOLO_NOT_WORN_LOCKED`, AGENTS.md 규칙 4).
       2. `YOLO26x` 가 `UNKNOWN`(미검출·가림·충돌)이거나 저신뢰(`conf < 0.65`) `WORN` 일 때, `decide-3b` 의 미착용 확신(`prob_worn <= 0.40`)이 있으면 위험 방향인 `NOT_WORN` 으로 보조한다.
3. **(C) 맵 크롭 선택 (`map_crop`)** — 동일 3D 벽면·표지판·액자 영역의 4카메라 크롭(`cam_A`~`cam_D`)
   - `cleanest` (`choice`): `cam_A`~`cam_D` 중 빛 반사·가림·초점 흐림·심한 사선 왜곡 없이 디지털 트윈 텍스처로 가장 선명한 시점 선택

## 2. 정답 라벨 및 학습·검증 분할 규칙 (AGENTS.md 규칙 7)

- `docs/data/weights.md`("T1-v4-ALL PPE 검출")에 기록된 대로 운영 가중치 `T1-v4-ALL-seed20261021-epoch08.pt` 는 내부 PPE 녹화 세 세션 전체(`rec-20261028-023200z-ppe-training` 포함)로 재학습되어 학습에서 뺀 세션이 없고, H200 학습 서버의 `T1 corrected-v4` 박스 라벨과 세션 제외 폴드 검증 분할 목록은 노트북(`simai-local/`)에 없다.
- 따라서 노트북의 `session4/egate1-frames-ppe-20261028`(`rec-20261028-023200z-ppe-training`) 표본은 모두 `is_upper_bound=True` (`IN_TRAINING_SET_OF_T1_V4_ALL__PIPELINE_SANITY_ONLY`)로 표시해 **파이프라인 동작 점검용으로만 쓰고 제품 성능 지표로는 쓰지 않는다**(규칙 7).
- 제품 일반화 성능 산출을 위해 13차 세션에서 `T1-v4-ALL` 학습에 쓰이지 않은 신규 Hold-out PPE 검증 세션을 촬영·라벨링하거나 H200 의 세션 제외 폴드 체크포인트·검증 분할 라벨을 회수해야 한다(`prepare_inputs.py` 의 `session13_collection_plan` 에 명시).

## 3. 노트북 검증 및 실행 방법

```bash
# 1) 입력 묶음 생성 (ai/experiments/decide_model/runs/input_bundle, gitignore 적용)
cd ai
uv run --extra calibration python -m experiments.decide_model.prepare_inputs

# 2) 노트북 CPU 드라이런 (--fake) 및 3자 비교 보고서 생성
uv run --extra calibration python -m experiments.decide_model.run_decide --fake
uv run --extra calibration python -m experiments.decide_model.compare

# 3) 단위 시험 실행
uv run --extra calibration pytest tests/experiments/test_decide_model.py
```

## 4. GPU Pod 실행 방법 (`pod_decide.sh`)

```bash
export HF_TOKEN="<HuggingFace_토큰>"
bash ai/experiments/decide_model/pod_decide.sh \
  --bundle ai/experiments/decide_model/runs/input_bundle \
  --out-dir ai/experiments/decide_model/runs/pod_out
```
