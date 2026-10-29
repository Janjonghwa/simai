# 보정 문서 EC2 업로드·구성 묶음 발행 CLI

S15P31A101-332. 명세 3.2(구성 묶음 발행), 4.4(보정 상태), 4.5(보정 버전·덮어쓰기 금지), 12.6(`GET /api/calibrations/current`, `POST /api/calibrations`, `POST /api/config-bundles`, `GET /api/config-bundles`), 14(`ADMIN` 역할 인증), 계약 `contracts/providers/calibration.schema.json` 2.x.
코드는 `ai/src/simai_ai/calibration/publish.py`(`python -m simai_ai.calibration.publish`), 시험은 `ai/tests/calibration/test_publish.py`.
연관 문서는 `docs/design/server-calibration-status.md`, `docs/design/server-config-bundle.md`, `docs/design/server-auth.md`.

## 흐름

1. 로컬 보정 문서 파일을 바이트·UTF-8 원문(`raw_text`) 그대로 읽고 JSON 파싱(`NaN`/`Infinity` 거부) 뒤 계약 구조(`check_calibration_structure`)와 기하·상태 불변식(`check_calibration`)을 검사한다.
2. `simai_ai.calibration.document.content_hash` 로 `content_hash` 를 다시 재 문서의 `content_hash` 와 대조하고, `coordinate_frame.site_id` 가 비어 있지 않은지(또 `--site-id` 인자가 주어졌다면 그 값과 일치하는지) 확인한다. 로컬 검사에 실패하면 네트워크 요청을 한 번도 보내지 않고 종료 코드 `2`(`EXIT_VALIDATION`)로 멈춘다.
3. 환경 변수 `SIMAI_ADMIN_USER`·`SIMAI_ADMIN_PASSWORD` 에서 관리자 계정을 읽고 `POST /api/auth/login` 으로 Bearer 토큰을 발급받는다(역할이 `ADMIN` 이 아니면 중단).
4. 올리기 전 `GET /api/calibrations/current` 를 호출해 서버 현장 `site_id`(응답에 포함된 경우)와 문서 `coordinate_frame.site_id` 가 같은지, 그리고 문서 `calibration_version` 이 서버의 현재 보정 버전(`calibration_version` 또는 `latest_uploaded_version`)과 겹치지 않는지 확인한다. 같으면 덮어쓰지 않고 멈춘다(AGENTS 규칙 6).
5. `--dry-run` 이면 여기서 멈추고 서버에 아무것도 쓰지 않은 채 한 줄 JSON 요약을 내보낸다.
6. `POST /api/calibrations` 에 `{"document_text": raw_text, "reason": ...}` 를 보내 보정 문서를 등록한다.
7. `POST /api/config-bundles` 를 호출해 새 보정 버전을 묶은 구성 묶음(`config_version`)을 발행한다.
8. `GET /api/calibrations/current` 와 `GET /api/config-bundles` 로 업로드된 `calibration_version` 과 발행된 `config_version` 이 서버에서 조회되는지 확인한 뒤, 표준 출력에 한 줄 JSON 요약(`calibration_version`, `config_version`, `server` 응답 요약)을 출력한다.

## 명세가 비워 둔 곳에서 정한 것

| 항목 | 정한 값 | 근거 |
|---|---|---|
| `--server` 기본값 | 기본값 없음 (`required=True`), 경로 없는 `http(s)://<호스트>` 만 허용 | 기본값으로 운영 주소(`https://simai.live`)를 두면 로컬·개발 시험 중 실수로 운영 EC2 의 보정·구성 묶음을 바꿀 수 있다 |
| 관리자 계정 전달 | 환경 변수 `SIMAI_ADMIN_USER`, `SIMAI_ADMIN_PASSWORD` 만 사용. CLI 인자(`--password` 등)로 받지 않음. 계정·비밀번호·JWT 토큰 값은 표준 출력·표준 오류·예외 문장에 남기지 않음 | 명령줄 인자는 `ps`·셸 기록·CI 로그에 그대로 남는다 |
| 업로드 본문 형식 | JSON 재직렬화(`document`) 대신 파일 원문 문자열(`document_text`) 전송 | `content_hash` 와 서버 `CalibrationContentHash` 는 정수와 실수(`1000` ≠ `1000.0`)를 구분한다. 재직렬화로 실수 표기가 바뀌거나 원문 `sha256` 이 Tier 0 로컬 파일과 갈리는 일을 막는다(`docs/design/server-calibration-status.md`) |
| 올리기 전 중복·현장 검사 | 로컬 스키마·`content_hash` 검사 후, 업로드 전에 `GET /api/calibrations/current` 로 `site_id`·현재 버전(`calibration_version`, `latest_uploaded_version`) 대조 + 서버 `400 CALIBRATION_SITE_MISMATCH`·`409 CALIBRATION_VERSION_CONFLICT` 즉시 중단 | 이미 쓰인 보정 버전은 덮어쓰지 않는다(AGENTS 규칙 6, 명세 4.5). 현장이 다른 문서가 올라가 구성 묶음까지 발행되는 사고를 막는다 |
| HTTP 클라이언트와 리다이렉트 | 외부 패키지 없이 표준 라이브러리 `urllib.request` 사용, HTTP 리다이렉트 추적 비활성화(`_NoRedirect`), 기본 타임아웃 `10.0` 초 | `Bearer` 토큰이 담긴 `Authorization` 헤더가 리다이렉트를 타고 다른 호스트나 평문 HTTP 로 넘어가지 않게 한다 |
| `--dry-run` 범위 | 로컬 계약·해시 검사 + 로그인 + `GET /api/calibrations/current` 대조까지만 수행, `POST /api/calibrations`·`POST /api/config-bundles` 호출 안 함 | 실제 서버의 현재 보정 버전과 충돌하는지까지 쓰기 없이 미리 검증할 수 있게 한다 |
| 종료 코드 | 성공 `0`, 올리기 전 검사·버전·현장 불일치 `2`(`EXIT_VALIDATION`), 서버 4xx/5xx·연결·발행 확인 실패 `1`(`EXIT_SERVER`) | 셸 스크립트나 자동화 파이프라인에서 입력 문서 문제와 서버/네트워크 문제를 구분할 수 있게 한다 |

## 기각한 대안

- **관리자 웹 화면에 보정 문서 파일 업로드 칸을 두는 안**: 보정 문서(`calibration.json`)는 회차 영상 보정(-331)·걷기 보정(`extrinsics.cli`)·World 고정(`world_frame.cli`)을 돌린 작업자/Tier 0 환경에서 만들어진다. 브라우저로 파일을 옮겨 수동 업로드하면 계약·`content_hash` 사전 검사를 건너뛰기 쉽고 CLI 파이프라인과 이어지지 않아 제외했다.
- **`--user` / `--password` 명령줄 인자를 받는 안**: 프로세스 목록과 셸 히스토리에 비밀 값이 노출되므로 기각하고 환경 변수만 받도록 고정했다.
- **`POST /api/calibrations` 에 파싱된 `document` 객체를 다시 `json.dumps` 해 보내는 안**: 원문 줄바꿈·공백과 부동소수점 표기가 달라져 파일 원문 `sha256` 이 바뀌므로 `document_text` 원문 전송으로 통일했다.

## 시험

- `ai/tests/calibration/test_publish.py` (표준 라이브러리 `http.server.ThreadingHTTPServer` 가짜 서버):
  - 정상 업로드·구성 묶음 발행·확인 조회 순서, `document_text` 의 `1000.0` 원문 보존, 한 줄 JSON 요약 출력, 비밀 값(`SIMAI_ADMIN_USER`, `SIMAI_ADMIN_PASSWORD`, `accessToken`) 비노출 검증.
  - `--dry-run` 시 서버 쓰기 요청(`POST /api/calibrations`, `POST /api/config-bundles`) 0건 검증.
  - 계약 스키마 위반·상태 불변식 위반·`content_hash` 불일치·`NaN` 수치 시 네트워크 요청 전 즉시 실패(`EXIT_VALIDATION`).
  - 현장 `site_id` 불일치(`--site-id`, `GET /api/calibrations/current`, `400 CALIBRATION_SITE_MISMATCH`) 시 중단.
  - 서버 현재 버전과 동일한 `calibration_version` 업로드 시도 시 쓰기 전 중단.
  - 서버 `401`·`403`·`409`·`500`·`503` 응답과 발행 묶음 버전 불일치 시 비정상 종료(`EXIT_SERVER`) 및 비밀 값 비노출 검증.

## 남은 위험과 후속 과제

- **Tier 0 가 연결된 상태에서 새 보정 버전을 먼저 업로드할 때의 순서**: EC2 `ConfigBundleService.latestCalibration()` 은 Tier 0 가 I1 `calibration_update` 알림을 한 번이라도 보낸 뒤에는 `calibrations.currentVersion()`(Tier 0 가 가장 최근에 알린 버전)을 기준으로 구성 묶음을 만든다(알림 기록이 전혀 없을 때만 가장 최근에 업로드한 문서를 사용한다). 따라서 Tier 0 가 이미 가동 중이고 이전 보정 버전을 알린 상태에서 CLI 로 새 보정 문서를 올린 뒤 곧바로 `POST /api/config-bundles` 를 호출하면, Tier 0 가 새 버전의 `calibration_update` 를 먼저 보내지 않은 경우 서버가 `409 CURRENT_CALIBRATION_MISSING` 을 돌려주거나 이전 보정 버전으로 묶음을 발행할 수 있다. 이 CLI 는 발행된 묶음의 `calibration_version` 이 업로드한 문서의 `calibration_version` 과 다르면 즉시 오류(`EXIT_SERVER`)로 알려 잘못된 묶음이 조용히 통과하지 않게 막는다.
