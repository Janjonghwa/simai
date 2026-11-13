# S15P31A101-693 작업 종료 → 녹화 업로드 → 다시 보기

## 범위와 결정

명세 5.1·8.5·11.5·12.2·12.6·18.1·부록 E, 사용자 결정 2026-11-13 A. `WorkSessionService.end`가 트랜잭션으로 확정한 `actual_started_at ~ actual_ended_at`을 기존 기기 토큰 `GET /api/devices/recordings/windows`로 찾는다. 종료 API에 외부 호출을 넣지 않는다. 커밋 전에는 구간이 보이지 않고, 재조회는 멱등이다. 기존 명세의 관제 종료 후 업로드·관제 중 중지 규칙은 이번 결정으로 바뀐다.

## 구현

- `gateway/ops/simai-gateway.ps1`: 관제 시작 때 기존 upload-worker도 시작한다. 기존 기기 REST를 실행 완료 후 10초 간격으로 반복한다. PID 검사와 로컬 named mutex로 중복 실행을 막는다. 관제 중에도 끝난 구간만 업로드하며, 관제를 끄고 미완료 업로드가 없으면 작업자를 끝낸다.
- `SegmentWriter`: 중계 녹화에서만 30초가 지난 첫 키프레임에서 파일을 나눈다. I0 epoch·sequence는 그대로이며 파일 이름에 시작 순번을 붙인다. 영상·사이드카 핸들을 모두 닫은 뒤 `.closed`를 만든다. 업로더는 진행 중 세션의 열린 파일을 읽거나 포장하지 않는다. 강제 종료 경로는 프로세스 종료를 확인한 뒤 세션을 확정한다.
- 녹화 manifest 1.3.0에 선택 `part_start_sequence`를 추가한다. 기존 전체 epoch 파일도 그대로 읽는다. 파일 입력은 같은 epoch의 조각을 이어 읽고, AI RecordingReplay는 파일 회전을 재접속으로 내지 않는다. 완료된 작업의 조각 계획은 로컬 상태에 구간 시각과 함께 캐시하여 다음 조회마다 긴 녹화 전체를 다시 읽지 않는다. 종료 작업이 없으면 열린 세션의 영상 메타데이터도 읽지 않는다.
- `recording_upload_progress` 1.0.0: 기기가 작업·녹화별 예정 수·실패 수·계획 확정 여부를 보고한다. 실제 등록 수는 기존 `video_segment`로 계산한다. 모든 녹화의 미확정 계획부터 보고해 첫 녹화만 완료되었는데 전체 완료로 보이는 것을 줄인다. 미보고는 WAITING, 미확정/미등록은 UPLOADING, 확정 수와 등록 수 일치만 COMPLETED, 확정 0개는 NO_RECORDING, 실패 보고 또는 미완료 보고 5분 단절은 FAILED, 작업 종료 기준 14일 경과는 EXPIRED다. 상태 표는 최신 전달 상태이며 영상·이벤트 정본을 덮지 않는다.
- `GET /api/videos`의 선택 `recording_uploads` 배열에 `recording_upload_status` 1.0.0 항목을 싣는다. 관리자 권한·재생 서명·조회 기록 정책은 기존 API를 쓴다. 프런트는 10초 간격, 최대 10분/60회 자동 조회하고 완료·미녹화·만료에는 멈춘다. 열린 화면에 새 영상이 즉시 반영된다. 권한·로그인 만료·보관 만료·통신 실패·확정 미녹화를 구분하고 수동 다시 확인을 제공한다. 준비 시각은 약속하지 않는다.

## 선택 이유와 버린 대안

현장 경보 단말·relay 연결은 경보/I0·CCTV의 생존과 지연을 책임진다. 업로드 요청을 실시간 소켓에 넣으면 재연결·누락 복구 계약이 더 필요하다. 이미 기기 인증과 구간 검증을 하는 recording_windows 조회가 가장 작고 재시작 복구도 가능하다. 새 상시 연결, 브라우저에서 노트북 직접 호출, 서버 종료 트랜잭션에서 노트북 호출은 채택하지 않았다.

`upload_grant`에는 작업과 전체 예정 조각 수가 없다. `video_segment` 등록 수만으로 업로드 완료와 아직 안 온 조각을 구분할 수 없으므로 작은 진행 상태 표가 필요하다. 로컬 `origin/dev` 최대 V69를 확인하고 V70을 사용했다. 공개 전 Lead가 최신 dev 번호를 재확인해야 한다. 배포 순서는 서버(계약/V70) → 게이트웨이 → 프런트다. I1 LIVE 계약 변경은 없다.

30초는 종료된 작업의 마지막 파일을 닫는 대기와 파일 개수 사이 초기 절충이다. 10초는 기존 REST 재조회 비용, 10분은 화면 자동 조회 상한, 5분은 끊긴 보고를 진행 중으로 무한 표시하지 않기 위한 초기값이다. 실제 네트워크·카메라 GOP와 업로드 시간은 현장 리허설에서 측정한다.

## 시험과 남은 위험

시험 위치: `gateway/recorder/tests/test_upload.py`, `gateway/relay/tests/test_recording.py`, `gateway/ops/simai-gateway.Tests.ps1`, 서버 `RecordingSegmentIntegrationTest`, 프런트 `useReplaySegments.test.tsx`·`ReplayStage.test.tsx`. 계약 예시·반례와 생성 모델도 검증 대상이다. 최종 실행 결과는 Owner 보고에 적는다.

- 실제 카메라·S3·현장 업링크 연속 시험은 하지 않았다. 관제 중 업로드가 I0 지연에 미치는 영향과 동시 업로드 수(기존 2)를 리허설에서 확인한다.
- 카메라가 멈추거나 키프레임을 보내지 않으면 마지막 파일이 닫히지 않아 완료를 확정하지 않는다. 녹화 세션 정지·확정 뒤 회복한다. 잘린 마지막 프레임은 기존 사이드카 복구 규칙으로 제외한다.
- 게이트웨이가 꺼졌거나 녹화를 아예 시작하지 않아 보고가 없으면 녹화 없음으로 추측하지 않고 대기로 남긴다. 화면 자동 확인 상한 이후 수동 확인이 필요하다.
- `reopen`으로 이미 완료한 작업 ID를 재사용하는 복구 운영은 기존 업로드 기록과 구간 정합성 재확인이 필요하다. 정상적인 새 작업 ID 흐름을 기준으로 한다.
- 구형 녹화 도구가 만든 `ended_at_ms: null` 파일에는 닫힘 표시가 없다. 관제를 정지하여 세션을 확정한 다음 업로드한다.


## 최종 검증 기록 (2026-11-14)

- 녹화 패키지 `uv run pytest -q`: 132개 통과. 중계 변경 영역 `test_recording.py`: 5개 통과. 운영 스크립트 Pester: 25개 통과.
- AI 녹화 재생 `test_recording_replay.py`: 21개 통과(녹화 venv에 AI 소스 경로를 지정).
- 프런트 변경 관련 4개 파일 Vitest: 76개 통과. admin·contracts·ui typecheck/ESLint, 루트 ESLint, TS 계약 생성물 확인 통과. 지정 pnpm shim은 다운로드가 차단되어 설치된 실행 파일로 동일 workspace 명령을 실행했다(Node 26.12.1, 지정 Node 24와 차이 있음).
- 계약 예시·반례 605개 검증 통과. 지정 uv script 실행은 의존성 조회가 차단되어 설치된 녹화 venv로 같은 검증 스크립트를 실행했다.
- pytest 두 실행을 같은 지정 basetemp로 병렬 실행한 충돌은 순차 재실행으로 해소했다.
- **미완료:** 서버 recording/video 통합 시험은 Gradle 9.8.1 다운로드 차단으로 시작하지 못했다. V70은 실행 검증하지 못했다.
- **미완료:** AI 전체 계약 시험 및 Python 계약 모델 재생성은 pydantic·datamodel-code-generator 등 의존성 다운로드 차단으로 실행하지 못했다. `ai/src/simai_ai/contracts/models.py`는 손으로 고치지 않았다. 의존성이 있는 환경에서 `uv run python ../contracts/scripts/generate_python_models.py` 후 `uv run pytest tests/contracts -q`를 반드시 수행해야 한다. 따라서 현재 변경은 전체 완료/병합 가능 상태로 간주하지 않는다.
