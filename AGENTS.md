# simai — 에이전트 작업 지침

고정 카메라 4대로 교실을 미터 좌표 3D 디지털 트윈에 올리고 위험을 판정하는 안전 관제 플랫폼이다.
기획서(simai System Specification v1.3)의 정본은 `docs/spec/`(장별 파일), 요구사항 명세서의 정본은 `docs/requirements.md` 다. Notion 에는 링크와 내보낸 사본만 있다.
작업에 필요한 규칙은 이 문서, 아래 규칙 문서, `contracts/`, `docs/spec/` 에 있다. 명세가 필요하면 `docs/spec/README.md` 의 장 목록과 에픽별 표에서 해당 장 파일만 읽는다.
명세와 이 문서가 다르면 명세가 우선하며, 차이를 발견하면 보고한다. 이 문서의 "명세 x.y"는 기획서의 절 번호이고 `docs/spec/` 의 장 파일과 일대일이다(예: 5.2 → `05-processing.md`).

## 먼저 읽을 규칙 문서

| 문서 | 내용 |
|---|---|
| `docs/git-convention.md` | 브랜치·커밋·MR 규칙과 Jira 연동 (팀 공통, 정본) |
| `docs/jira-convention.md` | Jira 에픽·스토리 작성, 포인트, 라벨, 상태, 스프린트 규칙 |
| `ai/CONVENTION.md`, `server/CONVENTION.md`, `front/CONVENTION.md` | 파트별 코드 규칙 |
| `contracts/README.md` | 구간 계약 스키마와 검증 방법 |
| `docs/spec/README.md` | 기획서 장 목록, 개정 이력, 에픽별로 읽을 장, 명세 갱신 규칙 |

## 깨면 안 되는 규칙

1. **Tier 0가 위치의 정본이다.** World XYZ, 검증된 관절, RiskEvent는 Tier 0(명세 5.2, 3.2)만 만든다.
   HMR·정적 3D·VLM 결과로 Tier 0 값을 덮어쓰거나 위험 판정에 넣지 않는다.
2. **모르면 비운다.** 지지 표면을 모르면 `ground_anchor_xyz`를 비우고 Z=0으로 채우지 않는다.
   2대 미만에서 보인 관절은 지어내지 않는다(null).
3. **좌표 규약.** World는 미터, 오른손, +Z 상방, 바닥 Z=0. 시각은 UTC int64 ms, 각도는 라디안.
   Three.js 변환 (x, z, −y)는 뷰어 어댑터 한 곳에서만 한다.
4. **AI는 제안만 한다.** VLM은 경보를 낮추거나 끄지 못한다. `proposed_severity`로 상향 의견만 낸다.
5. **LIVE만 실제 알림.** PLANNED·WHAT_IF·REPLAY 결과는 알림 채널로 보내지 않는다.
6. **덮어쓰지 않는다.** 시계열·이벤트는 추가만 하고, 구역·규칙·보정·Scene은 새 버전을 추가한다(첫 운영 배포 전 규칙 보완 예외: 명세 11.4).
7. **관측과 추정을 구분한다.** `joint_source`(observed/inferred), `surface_source`, `input_provenance`를 빠뜨리지 않는다.
   정답 데이터(DATASET_GT, SIM)로 얻은 지표를 제품 성능으로 쓰지 않는다.
8. **얼굴.** 외형 텍스처에 관측된 얼굴 픽셀을 그대로 써도 된다(기본 OBSERVED, BLURRED·EXCLUDED 선택). 생성형 얼굴 복원·표정 추정은 하지 않는다(30~50 px 물리 한계, 지어낸 얼굴 금지). 재식별(Re-ID)은 전신 임베딩을 기하 게이트 뒤 보조 비용으로만 쓰고 Tier 0 위치·ID 정본을 덮지 않으며, 도입은 10/30 이후 G2 측정 뒤(지금 범위 아님, 명세 19.1 미결)로 한다. 외부 LLM 에는 텍스트만 보낸다.

## 계약 우선

- 파트 사이 필드 정의의 정본은 `contracts/`의 JSON Schema다. 코드를 바꾸기 전에 스키마를 먼저 바꾼다.
- 스키마 변경은 버전 규칙을 따른다: 필드 추가는 마이너, 삭제·의미 변경은 메이저. I1 LIVE 계약(`world_state` 등)은 Lead 승인 없이 바꾸지 않는다.
- 스키마를 고치면 `contracts/examples/`의 예시도 고치고 검증한다: `uv run contracts/scripts/validate_examples.py`

## 저장소

```
contracts/   JSON Schema 정본 (common, messages, providers, experiments, data, examples)
ai/          기능별 패키지(src/simai_ai), 배포 단위(services), 실험(experiments). 규칙은 ai/CONVENTION.md
gateway/     노트북 중계·현장 경보 앱
server/      EC2 Spring Boot
front/       Three.js 뷰어, 반장 PWA
watch/       갤럭시워치(Wear OS) 앱, 확장(부록 C.6). 빌드·설치는 watch/README.md
deploy/      모델별 Docker 이미지, Compose, RunPod 템플릿
docs/        기획서(spec/), 요구사항 명세서(requirements.md), 팀 규칙, 구현 설계(design/), 데이터 manifest(data/), 실험 설계(experiments/)
```

- `docs/` 하위 폴더:
  - `spec/`: 기획서 정본(장별 파일). `requirements.md`: 요구사항 명세서 정본.
  - `design/`: 구현 설계 문서. 티켓 단위로 관련 명세 절, 코드·시험 위치, 명세에 없어 정한 수치와 근거, 버린 대안, 잔여 위험, 넘긴 일을 적는다. 명세 정리 MR 이 근거로 쓴다.
  - `data/`: 데이터 manifest. 공개 데이터의 출처·라이선스·경로·해시, 녹화 세션 manifest 형식(`recording-manifest.md`)과 세션별 사본(`sessions/<session_id>.json`).
  - `experiments/`: EXP 실험 설계와 색인.
- 영상·사진·가중치·데이터셋·대형 3D 산출물은 커밋하지 않는다. 저장소에는 manifest(경로·해시)만 둔다. 실물은 GPU 실행 사이트의 저장 위치(명세 3.12)에 두고, 볼륨이 없으면 인스턴스를 지우기 전에 결과 묶음을 노트북으로 가져온다(Network Volume 은 2026-10-20 에 폐기).
- 비밀 값(API 키, 토큰, RTSP 계정)은 `.env`에만 둔다.

## 작업 방식

- 브랜치·커밋·MR은 `docs/git-convention.md`를 따른다. 요약: 브랜치 `S15P31A101-{번호}-{설명}`, 커밋 `[S15P31A101-{번호}] 한국어 한 줄`, MR은 `dev`로, squash merge. Jira 이슈가 먼저 있어야 한다.
- MR은 `/mr` 스킬로 올린다. 공개 전에 사용자 승인을 받는다.
- 자기 이슈 범위 밖의 파일은 고치지 않는다.
- 실험 Run마다 `contracts/experiments/run_record.schema.json` 형식의 기록을 남긴다.
- GPU 실행 사이트·지역은 고정하지 않는다(개발·시험은 그때그때 남은 GPU·지역, 볼륨 없음. 최소 사양은 명세 3.12). 한 시점에 GPU 인스턴스는 하나이며 Tier 0가 자원을 먼저 쓴다. LIVE 관제 시간에 오프라인·배치 EXP Job(`hmr_*` I5 작업 포함)은 돌리지 않는다(예외: 시연 범위인 EXP-02 상시 HMR 아바타 스트림 루프는 분석 워커 GPU 상한 안에서 돌되, Tier 0 지연 감시 표시 파일로 지연 초과·모름이면 멈추고 뷰어는 0.5 s 뒤 캡슐로 돌아간다, 명세 3.2).
- GPU Pod는 검증할 때만 켠다. 코드·설치 스크립트·실험 큐는 노트북에서 완성하고, Pod에서 큐를 실행한 뒤 바로 Stop 한다.
- 노트북↔GPU 인스턴스는 인스턴스 공개 IP:포트 SSH 직접 연결(기본, 키 인증만)로 잇는다. 막힌 망의 예비는 RunPod 에서만 되는 HTTPS 프록시 443(토큰 인증)이다. Tailscale은 쓰지 않는다.
