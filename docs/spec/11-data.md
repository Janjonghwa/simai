# 11. 데이터

Spatial DB는 EC2의 PostgreSQL + TimescaleDB(시계열) + PostGIS(공간)로 구성한다. 영상·사진·3D 자산은 객체 저장소에 두고 DB에는 주소만 저장한다. 2D 관측 원본은 EC2 가 아니라 RunPod Network Volume 에 둔다(11.2).

**DB 운용.**

- 이미지는 `timescale/timescaledb-ha` 의 -all 태그를 쓴다(TimescaleDB·PostGIS 포함).
- `risk_event`는 일반 테이블로 둔다. `notification`이 `risk_event(event_id, source)`를 복합 FK 로 참조해야 하기 때문이다(11.4).
- `world_track_state`, `skeleton_3d`, `system_metric`은 hypertable 로 만들고 압축과 보관 정책을 건다.
- PostGIS 는 저장·조회에만 쓴다. 구역 판정 계산은 Risk Engine 한 곳에서만 한다(3.2).

## 11.1 구성·기준 데이터

자주 바뀌지 않으며, 수정 대신 버전을 추가한다.

| 테이블 | 주요 내용 |
|---|---|
| `site` | World 좌표계 정의(원점·축·스케일 근거), BEV 평면도 참조, 좌표계 이전 이력 |
| `camera` | 카메라 ID, 모델, 하드웨어·펌웨어 버전, 해상도·fps |
| `calibration_version` | 카메라별 K·왜곡·R·t·높이, 재투영 오차, 스케일 근거, 상태 |
| `scene_version` | 정적 3D 자산 주소, 사용한 보정 버전, 상태 |
| `zone` | 다각형 + 높이 범위, 판정 유형(GROUND_FOOTPRINT/VOLUME_3D), 유형, 심각도, 요구 조건, 유효 시작·종료 |
| `rule_version` | 규칙 ID, 임계값 세트, 적용 기간(시험 프로파일 포함, 7.4) |
| `equipment_asset` | GLB 주소, 반경·형상 파라미터(확장, 부록 C) |
| `layout_plan` | 가상 레이아웃·계획 궤적(F-02·F-01), 출처 PLANNED |
| `config_bundle` | 구역·규칙·보정·형상(등록 정적 객체·작업면 OBB) 버전 묶음, 해시, 발행 시각, 현장 적용 시각(RunPod Tier 0 ACK) |

## 11.2 시계열 관측

| 테이블 | 주요 내용 | 빈도 | 보관 |
|---|---|---|---|
| `observation_2d` | 촬영 시각, 카메라, 카메라 내 트랙, bbox, keypoint 배열. **EC2 DB 에 두지 않고** RunPod Network Volume 에 Parquet 로 보관한다(위치는 19.1 볼륨 구조의 `obs2d`). EC2 에는 필요할 때 카메라별 요약(I1 `observation_summary`, 2D 원본 없음)만 보낸다 | 저장 5\~15 Hz(처리는 카메라당 12 FPS 이상, 5.1) | 7\~14일(보정 재계산용) |
| `world_track_state` | World ID, XYZ, 불확실성, 속도, 관측 상태, gap, 기여 카메라, 보정·Scene 버전 | 10 Hz | 11.5 |
| `skeleton_3d` | World ID, 3D 관절, 관절 가시성, 계산 방식 | 5\~10 Hz | 11.5 |
| `system_health` | 시스템 상태, 카메라별 상태, lag, 보정 상태, 하트비트 | 1 Hz | 11.5 |
| `system_metric` | 구간별 지연, FPS, 큐 길이, GPU 사용률, 프레임 손실 | 1\~5 Hz | 11.5 |

`ppe_state`, `channel_state`, `watch_feature`, `region_state`(와 11.3 의 `watch_binding`)는 MVP DB 에 두지 않는다(부록 C).

## 11.3 이벤트·조치

| 테이블 | 주요 내용 |
|---|---|
| `risk_event` | 규칙·버전, World ID·구역, 출처, 생명주기·검증 상태, severity·confidence·confidence_reasons, 발생·해소 시각, 근거 스냅샷, 관련 이벤트(related_event_id), 보존 표시 |
| `risk_event_update` | RunPod Tier 0 전이의 시간순 기록(event_sequence, config_version, 지연 전달·감사 전용 여부) |
| `vlm_assessment` | 이벤트, 모델·버전, 입력 참조, 근거 문장, 인용 조항 ID, 상향 의견(proposed_severity), 지연 |
| `notification` | 이벤트, 채널, 대상, 발송·수신 확인·ACK 시각 (LIVE 이벤트만 연결 가능. 시험 알림은 이 테이블에 넣지 않음) |
| `action_ticket` | 조치 상태, 사진 주소, 해소 상태와의 불일치 표시 |
| `media_clip` | 이벤트 연결 영상 구간의 객체 키, 카메라·구간(촬영 시각)·해시·크기, 얼굴 흐림 여부, 삭제 예정일, 보존 표시. Tier 0 가 I1 `media_clip` 으로 보내고, 보낸 뒤 붙는 CRITICAL 자동 보존은 고치지 않고 `media_clip_retention` 으로 이력을 더한다 |
| `report` | TBM 보고서·일일 일지, 입력 참조, 인용 목록 |

PLANNED·WHAT_IF 결과는 별도 스키마 `sim.*`에 저장한다.

## 11.4 스키마 원칙

1. 시계열·이벤트는 추가만 한다. 구역·규칙·보정·Scene은 유효 기간이 있는 새 버전을 추가한다.
2. 모든 계산 결과는 출처를 안다: 위치(보정·Scene 버전), 이벤트(규칙 버전), 근거(모델 버전).
3. `notification`은 출처가 LIVE인 `risk_event`만 참조할 수 있게 한다. 구현: notification에 source 컬럼을 두고 risk_event(event_id, source)를 복합 FK로 참조하며 source = ‘LIVE’ 체크를 건다(일반 CHECK는 다른 테이블을 볼 수 없으므로). 발송 워커도 LIVE만 소비하고, DB 기록 차단과 실제 발송 차단을 각각 테스트한다.
4. 촬영·수신·판정 시각을 모두 UTC로 저장한다.
5. 위치 기록에는 이름이 없다.

**용량.** 10명 기준 `world_track_state`는 하루 8시간에 약 300만 행(압축 전 수백 MB). 가장 무거운 `observation_2d`는 하루 수 GB까지 가능하므로 EC2 가 아니라 RunPod Network Volume 에 두고 보관을 짧게 하며, 필요하면 5 Hz로 낮춘다. 지급 EC2 의 디스크 용량은 확인 필요다(19.1).

**리플레이 질의.** 최소 리플레이(M3)는 시각 T의 `world_track_state`와 T에 활성인 `risk_event`를 쓴다. 리플레이 확장은 여기에 `skeleton_3d`, T에 유효한 `zone`·`rule_version`, T에 COMMITTED인 `scene_version`을 더한다(부록 C).

## 11.5 보관과 개인정보

보관 기간은 동의서와 18.4 의 확인 절차가 같은 값을 쓴다.

| 데이터 | 보관 |
|---|---|
| 원본 영상 | 관제 중 RunPod로 상시 전송(TLS), 저장하지 않음(링 버퍼와 이벤트 클립만 예외) |
| 이벤트 영상 클립 | 14일 후 자동 삭제. CRITICAL 이벤트 연결분은 자동 보존 표시, 아차사고는 관리자가 보존 표시를 붙이거나 뗌. 보존분은 접근 기록을 남기고 프로젝트 종료 시 파기. 얼굴은 흐림 처리하지 않는다(사용자 결정, 16장). 열람은 권한자로 제한하고 조회 기록을 남긴다(14장). 흐림 단계는 선택 사항으로 남기며, 켜면 업로드 전에 반드시 거치고 실패하면 흐리지 않은 클립으로 대신하지 않는다. 얼굴 복원·표정 추정·재식별은 하지 않는다. EC2 단절 중 클립은 Network Volume 에 먼저 저장 후 복구 시 업로드(5.1). Tier 0 에 남은 미업로드 클립은 보존 표시가 없고 삭제 예정일이 지난 것만 올리지 않고 지우며, 보존 표시가 있는 클립은 전체 파기 기한 전까지 복원·업로드한다 |
| PPE crop(확장) | 판정 직후 삭제, 저장 없음 |
| 2D 관측 | RunPod Network Volume, 7\~14일 |
| 좌표·상태·이벤트 로그 | 얼굴 없음. 프로젝트 발표 후 30일 안 파기 |
| 시나리오 데이터셋 | 참여자 별도 동의, 보관 기간 명시 |
| 내부 테스트 영상 | 수업 중 우연히 찍힌 사람이 있는 영상은 내부 개발·테스트에만 쓴다(녹화 `consent.purpose = dev_test`). 보관 기한 필수, 커밋 안 함, 노트북·프로젝트 Network Volume 에만 두고 프로젝트 종료 시 삭제. 시연·발표·보고서·공개 자료에는 쓰지 않는다 |
| HMR 외형/텍스처·추론 결과(실험) | 별도 참여자 동의, 접근 범위·저장 위치·보관/삭제 기간을 실험 시작 전 확정. 관측 crop 은 얼굴 흐림·보관 기간·동의 규칙을 따른다. 실제 얼굴 재식별 정보 생성 금지 |
| 전체 | 프로젝트 발표 후 30일 안에 모든 데이터를 파기한다(보존 표시분 포함) |

- 자동 삭제는 객체 저장소 수명 주기 규칙과 TimescaleDB 보관 정책, Network Volume 정리 작업으로 구현한다.
- 참여자 동의서에 촬영 사실, 외부 클라우드 전송 범위(관제 중 카메라 영상 상시 전송 포함), 보관 기간, 삭제 요청 방법을 명시한다.
- 동의서와 비참여자 안내문에 이벤트 클립 항목을 넣는다: 얼굴이 찍힌 이벤트 클립을 흐림 없이 14일 보관하고, CRITICAL 이벤트에 연결된 클립은 보존 표시로 프로젝트 종료까지(발표 후 30일 안 파기) 남으며, 열람은 권한자만 한다.
- **국외 이전 고지.** 받는 자(RunPod 일본 리전(개발·시연, 3.12. 일본 GPU를 못 구하면 미국 리전으로 바뀔 수 있음), LLM API 제공사)의 명칭·연락처, 이전하는 항목·시기·방법, 거부 방법과 거부의 효과를 적는다(개인정보 보호법 제28조의8. 조문 원문 확인 필요). AWS 는 서울 리전이다. LLM API 제공사의 명칭·연락처는 확인 필요다.
- **비참여자.** 같은 교실의 교육생·관람자에게 안내문을 붙이고 촬영 구역을 표시하며, 촬영을 거부하는 절차를 둔다.
- 전송 구간은 모두 암호화한다(14장).
