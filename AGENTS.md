# simai — 에이전트 작업 지침

고정 카메라 4대로 교실을 미터 좌표 3D 디지털 트윈에 올리고 위험을 판정하는 안전 관제 플랫폼이다.
기획서(simai System Specification v1.2 r4)와 요구사항명세서의 원본은 Notion이며 저장소에는 사본을 두지 않는다.
작업에 필요한 규칙은 이 문서, 아래 규칙 문서, `contracts/`에 있고, 그 밖의 명세 내용이 필요하면 작업 지시에 해당 절을 함께 받는다.
명세와 이 문서가 다르면 명세가 우선하며, 차이를 발견하면 보고한다. 이 문서의 "명세 x.y"는 기획서의 절 번호다.

## 먼저 읽을 규칙 문서

| 문서 | 내용 |
|---|---|
| `docs/git-convention.md` | 브랜치·커밋·MR·Jira 규칙 (팀 공통, 정본) |
| `ai/CONVENTION.md`, `server/CONVENTION.md`, `front/CONVENTION.md` | 파트별 코드 규칙 |
| `contracts/README.md` | 구간 계약 스키마와 검증 방법 |

## 깨면 안 되는 규칙

1. **Tier 0가 위치의 정본이다.** World XYZ, 검증된 관절, RiskEvent는 Tier 0(명세 5.2, 3.2)만 만든다.
   HMR·정적 3D·VLM 결과로 Tier 0 값을 덮어쓰거나 위험 판정에 넣지 않는다.
2. **모르면 비운다.** 지지 표면을 모르면 `ground_anchor_xyz`를 비우고 Z=0으로 채우지 않는다.
   2대 미만에서 보인 관절은 지어내지 않는다(null).
3. **좌표 규약.** World는 미터, 오른손, +Z 상방, 바닥 Z=0. 시각은 UTC int64 ms, 각도는 라디안.
   Three.js 변환 (x, z, −y)는 뷰어 어댑터 한 곳에서만 한다.
4. **AI는 제안만 한다.** VLM은 경보를 낮추거나 끄지 못한다. `proposed_severity`로 상향 의견만 낸다.
5. **LIVE만 실제 알림.** PLANNED·WHAT_IF·REPLAY 결과는 알림 채널로 보내지 않는다.
6. **덮어쓰지 않는다.** 시계열·이벤트는 추가만 하고, 구역·규칙·보정·Scene은 새 버전을 추가한다.
7. **관측과 추정을 구분한다.** `joint_source`(observed/inferred), `surface_source`, `input_provenance`를 빠뜨리지 않는다.
   정답 데이터(DATASET_GT, SIM)로 얻은 지표를 제품 성능으로 쓰지 않는다.
8. **얼굴.** 얼굴 복원·표정 추정·재식별은 하지 않는다. 외형 텍스처에 원본 얼굴 픽셀을 저장하지 않는다.

## 계약 우선

- 파트 사이 필드 정의의 정본은 `contracts/`의 JSON Schema다. 코드를 바꾸기 전에 스키마를 먼저 바꾼다.
- 스키마 변경은 버전 규칙을 따른다: 필드 추가는 마이너, 삭제·의미 변경은 메이저. I1 LIVE 계약(`world_state` 등)은 Lead 승인 없이 바꾸지 않는다.
- 스키마를 고치면 `contracts/examples/`의 예시도 고치고 검증한다: `uv run contracts/scripts/validate_examples.py`

## 저장소

```
contracts/   JSON Schema 정본 (common, messages, providers, experiments, examples)
ai/          기능별 패키지(src/simai_ai), 배포 단위(services), 실험(experiments). 규칙은 ai/CONVENTION.md
gateway/     노트북 중계·현장 경보 앱
server/      EC2 Spring Boot
front/       Three.js 뷰어, 반장 PWA
deploy/      모델별 Docker 이미지, Compose, RunPod 템플릿
docs/        팀 규칙, 실험 설계, 결정 기록, 데이터 manifest (명세 사본은 두지 않음)
```

- 영상·사진·가중치·데이터셋·대형 3D 산출물은 커밋하지 않는다. RunPod Network Volume에 두고 저장소에는 manifest(경로·해시)만 둔다.
- 비밀 값(API 키, 토큰, RTSP 계정)은 `.env`에만 둔다.

## 작업 방식

- 브랜치·커밋·MR은 `docs/git-convention.md`를 따른다. 요약: 브랜치 `S15P31A101-{번호}-{설명}`, 커밋 `[S15P31A101-{번호}] 한국어 한 줄`, MR은 `dev`로, squash merge. Jira 이슈가 먼저 있어야 한다.
- MR은 `/mr` 스킬로 올린다. 공개 전에 사용자 승인을 받는다.
- 자기 이슈 범위 밖의 파일은 고치지 않는다.
- 실험 Run마다 `contracts/experiments/run_record.schema.json` 형식의 기록을 남긴다.
- RunPod는 한 시점에 한 리전만 쓴다(개발: 미국 A40, 시연 전 시연 리전으로 이전). GPU Pod는 하나이며 Tier 0가 자원을 먼저 쓴다. LIVE 관제 시간에 EXP Job을 돌리지 않는다.
