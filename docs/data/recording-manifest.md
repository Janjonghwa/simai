# 녹화 manifest 형식

4대 동기화 녹화 세션 하나의 파일 목록·해시·프레임 수·시각 범위·카메라 설정·동의 범위·저장 위치를 적는 문서다
(명세 15.3 시나리오 데이터셋, 11.5 보관과 개인정보, 21.1). 녹화 도구 `gateway/recorder` 가 만든다(S15P31A101-107).

- 영상·사람 이미지는 저장소에 넣지 않는다. 세션 디렉터리는 Network Volume 에 두고, 저장소에는 `manifest.json` 만 둔다.
- **형식의 정본은 계약 스키마 [`contracts/data/recording_manifest.schema.json`](../../contracts/data/recording_manifest.schema.json) 이다**(S15P31A101-158).
  이 문서는 필드 뜻과 운용을 설명하며, 스키마와 다르면 스키마가 우선한다. 필드를 바꿀 때는 스키마를 먼저 고치고
  `contracts/examples/recording_manifest*.json` 과 이 문서를 같이 고친다(필드 추가는 마이너, 삭제·의미 변경은 메이저).
- 예시: 정상 [`contracts/examples/recording_manifest.json`](../../contracts/examples/recording_manifest.json)(RTSP, 사람 포함),
  [`recording_manifest.test_input.json`](../../contracts/examples/recording_manifest.test_input.json)(합성·로컬 파일), 위반 `contracts/examples/invalid/recording_manifest.*.json`.
- 녹화 파일 형식(세그먼트·프레임 헤더 사이드카)은 [`gateway/recorder/README.md`](../../gateway/recorder/README.md) 의 "녹화 형식"에 있다.
  사이드카 한 줄은 스키마의 `$defs/frame_line` 이고, 그 안의 `header` 는 I0 `frame_header` 계약 그대로다.

## 어디에 두나

| 무엇 | 위치 |
|---|---|
| 세션 디렉터리 전체(영상, 사이드카, `session.json`, `manifest.json`) | Network Volume `volume://AP-JP-1/simai/recordings/<session_id>/` (`storage.uri`) |
| 녹화 직후 노트북 사본 | `ai/experiments/recordings/<session_id>/` (`.gitignore` 의 `recordings/`) |
| 저장소에 커밋하는 것 | `docs/data/sessions/<session_id>.json` = `manifest.json` 사본 (영상 없음) |

`docs/data/sessions/` 처럼 `.gitignore` 에 걸리지 않는 이름을 쓴다(`recordings/`, `datasets/`, `runs/`, `output/` 는 무시된다).
Network Volume 에 올린 뒤 `uv run simai-recorder verify <세션 디렉터리>` 로 해시를 확인하고 커밋한다.

## 필드

시각은 UTC int64 ms, 경로는 세션 디렉터리 기준 상대 경로(POSIX), 해시는 `sha256:<64자 16진수>`(`contracts/common/defs.schema.json#/$defs/sha256` 과 같은 형식)다.
모르는 값은 null 로 둔다(지어내지 않는다).

### 세션

| 필드 | 형식 | 내용 |
|---|---|---|
| `manifest_version` | `"1.x.y"` | 이 형식의 버전(현재 `1.1.0`). 필드 추가는 마이너, 삭제·의미 변경은 메이저. 1.1.0 은 `input_kinds` 가 카메라 입력 종류를 모두 담는지 검사를 더했다(1.0.0 녹화 도구 출력은 그대로 통과) |
| `session_id` | `^[a-z0-9][a-z0-9-]*$` | 기본 `rec-<UTC yyyymmdd-hhmmss>z-<시나리오>` |
| `scenario` | 문자열 | 15.3 시나리오 이름(예: `empty_room_10min`, `walk_calibration`, `position_grid`) |
| `description`, `notes` | 문자열 | 사람이 읽는 설명 |
| `input_kinds` | `RTSP` / `FILE` / `SYNTHETIC` 목록 | 카메라 입력 종류(`cameras[].source.kind` 의 집합). 카메라에 있는 종류가 빠지면 위반(스키마가 강제) |
| `is_test_input` | bool | RTSP 가 아닌 입력이 하나라도 있으면 true, 아니면 false(스키마가 강제). **true 인 세션은 제품 성능·게이트 판정에 쓰지 않는다**(AGENTS 규칙 7) |
| `started_at_ms`, `ended_at_ms` | int | 녹화 시작·끝(노트북 시계) |
| `requested_duration_s` | number / null | 요청한 녹화 시간. null = Ctrl+C 까지 |
| `common_capture_window_ms` | `[from, to]` / null | 네 카메라가 모두 찍고 있던 촬영 시각 구간 |
| `calibration_version` | 문자열 / null | 이 녹화에 쓸 보정 버전(있으면) |
| `consent` | 객체 / null | 아래 "동의와 보관". RTSP 녹화는 필수 |
| `storage.uri` | `volume://…/` 또는 `s3://…/` | 세션 디렉터리를 둔 곳. 로컬 경로 금지 |
| `storage.layout` | 문자열 | 세션 디렉터리 안 파일 배치 |
| `recorded_by` | 객체 | `tool`, `version`, `git_commit`(녹화 도구 커밋), `gateway_id`(노트북) |
| `deleted_at_ms` | int (선택) | 보관 기한이 지나 Network Volume 에서 지운 시각. 아래 "동의와 보관" |
| `cameras` | 배열 | 카메라별 항목 |

### 카메라 (`cameras[]`)

| 필드 | 내용 |
|---|---|
| `camera_id` | `^[A-Za-z0-9_-]+$`. 보정·I0 와 같은 ID |
| `source` | 입력 설명. RTSP 는 `{"kind":"RTSP","url":"rtsp://host:554/stream1"}`(계정 없음). 파일은 `file_name`, 합성은 해상도·fps, 장애 흉내는 `faults` |
| `settings` | `codec`, `profile`, `width`, `height`, `nominal_fps`(스트림에서 읽음), `firmware`(설정에서 사람이 적음, 모르면 null), `resolution_changed`(epoch 사이 해상도가 바뀌었는지) |
| `timestamp_source` | 이 카메라 헤더의 `timestamp_source`. C110 기본 `RECEIVE_ESTIMATED` (명세 3.9) |
| `capture_time_model` | 촬영 시각 추정 설정 `latency_ms`, `uncertainty_ms`, `uncertainty_no_pts_ms`, `window_ms` (G0 전 잠정값) |
| `frame_count` | 모든 세그먼트의 프레임 수 합 |
| `capture_time_range_ms` | 첫·마지막 추정 촬영 시각 |
| `reconnects` | 끊긴 횟수(= 끊긴 뒤 새 epoch 를 연 시도) |
| `open_failures` | 접속 실패 횟수 |
| `dropped_before_keyframe` | 새 epoch 에서 키프레임 전에 버린 패킷 수 합 |
| `errors` | 녹화 루프가 멈춘 오류 문장(정상이면 빈 배열) |
| `events` | `events.jsonl` 의 `{path, bytes, sha256}` |
| `segments` | stream_epoch 별 세그먼트 |

### 세그먼트 (`cameras[].segments[]`)

| 필드 | 내용 |
|---|---|
| `stream_epoch` | 재접속마다 늘어나는 번호 (명세 12.7) |
| `video`, `frames`, `codec_info` | 각 파일의 `{path, bytes, sha256}`: `epoch_NNNN.h264`, `epoch_NNNN.frames.jsonl`, `epoch_NNNN.codec_info.json` |
| `frame_count`, `keyframe_count` | 프레임·키프레임 수 |
| `frame_sequence_range` | `[0, frame_count-1]` |
| `capture_time_range_ms` | 추정 촬영 시각 범위 |
| `received_time_range_ms` | 노트북 수신 시각 범위 |
| `timestamp_sources` | 헤더에 나온 `timestamp_source` 값들 |
| `timestamp_uncertainty_ms_max` | 헤더 `timestamp_uncertainty_ms` 최댓값 |
| `keyframe_interval_s_p50` | 키프레임 간격 중앙값(C110 stream1 실측 2초) |
| `camera_to_gateway_gap` | 도착 간격이 공칭 간격의 1.5배(`threshold_ms`)를 넘은 횟수 `count`, 최대 간격 `max_ms`, 도착 간격 p95 (명세 12.7, -9 측정 도구와 같은 기준) |
| `integrity` | `sequence_contiguous`(frame_sequence 빈칸 없음), `first_is_keyframe`, `bytes_covered`(마지막 프레임이 파일 끝까지). 하나라도 false 면 검증 실패 |

## 동의와 보관 (`consent`, 명세 11.5)

| 필드 | 내용 |
|---|---|
| `contains_people` | 사람이 찍혔는지. 사람 없는 교실 녹화는 false |
| `consent_ref` | 참여자 동의서 문서 ID. 동의서 원본은 저장소 밖에 둔다. `contains_people` 이 true 면 필수 |
| `scope` | 동의 범위(용도·전송 리전) 코드 목록. `contains_people` 이 true 면 필수 |
| `retention_until` | `YYYY-MM-DD`. 이 날이 지나면 Network Volume 에서 세션 디렉터리를 지운다. RTSP 녹화는 필수 |

`scope` 코드(스키마 `$defs/consent_scope`. 코드를 더하려면 스키마에 먼저 더한다, 마이너):

| 코드 | 뜻 |
|---|---|
| `dataset_internal` | 팀 내부 개발·게이트 평가(15.3) |
| `runpod_ap_jp_1` | RunPod 일본 리전(AP-JP-1) 전송·보관(3.12) |
| `runpod_us` | RunPod 미국 리전(일본 GPU 를 못 구했을 때, 3.12) |
| `aws_seoul` | AWS 서울 리전 전송·보관 |
| `presentation` | 발표·시연 자료에 영상 사용 |
| `hmr_texture` | EXP-02 외형·텍스처 실험(별도 동의, 11.5. 얼굴 영역 규칙은 명세 5.6) |

녹화 도구는 RTSP 세션에 `consent` 가 없거나, 보관 기한이 없거나, 사람이 찍히는데 동의서 ID·범위가 없으면 녹화를 시작하지 않는다.
보관 기한이 지나 지우면 저장소의 manifest 는 지우지 않고(추가만, AGENTS 규칙 6) 같은 파일에 `deleted_at_ms` 를 더하는 커밋을 한다.

## 검사

`simai-recorder record`·`manifest`·`verify` 가 `simai_recorder.manifest.validate_manifest` 로 검사한다. 검사는 계약 스키마로 하고
(저장소 루트의 `contracts/` 를 직접 읽는다. 다른 위치면 `SIMAI_CONTRACTS_DIR`), 스키마가 막는 것은 다음과 같다.

- 필수 필드, `session_id` 형식, `scenario` 있음, `storage.uri` 가 `volume://`·`s3://`
- 동의와 보관 규칙(위 표): RTSP 녹화는 `consent` 필수, 보관 기한은 실제 있는 날짜, 사람이 찍히면 `consent_ref`·`scope` 필수
- `source.url` 에 계정(`user:pass@`)이 없음, 실제 카메라(RTSP)에 장애 흉내(`faults`) 없음
- 파일 경로가 세션 디렉터리 기준 상대 경로(`..`·절대 경로 불가), 해시 형식 `sha256:<64자 소문자 16진수>`
- `input_kinds` 가 `cameras[].source.kind` 를 모두 담음, `is_test_input` 이 입력 종류와 맞음(로컬 파일·합성 카메라가 섞인 세션은 `is_test_input: true`)
- 세그먼트 `integrity` 가 모두 true

스키마로 표현할 수 없는 규칙은 `validate_manifest` 가 더 본다: 시각·프레임 번호 범위의 from ≤ to, `camera_id` 중복.
`verify` 는 여기에 더해 파일마다 크기·sha256 을 다시 계산해 비교한다. 녹화 시작 전에는 세션 설정의 `consent`·`storage_uri` 를
같은 스키마 정의(`$defs/consent_requirement`, `storage.uri`)로 검사하고, 어긋나면 녹화를 시작하지 않는다.
오류 문장에는 받은 값을 넣지 않는다(RTSP 주소의 계정이 화면·로그에 찍히지 않게). 스키마를 찾지 못하면(저장소 밖 실행, 틀린 `SIMAI_CONTRACTS_DIR`)
그 경로를 적은 설정 오류로 멈춘다.

## Tier 0 에서 읽기

`simai_ai.io.VideoFileFrameSource.from_manifest(<세션 디렉터리>, epochs=...)` 가 manifest 를 녹화 도구와 같은 판정(같은 스키마·`format: date` 검사·스키마 밖 규칙, `simai_ai.io.recording_manifest_errors`)으로 검사한 뒤
카메라마다 세그먼트 하나를 녹화 파일 입력(timestamp_source `FILE`, 명세 20.4)으로 읽는다(`dataset_id = recording/<session_id>`).
재접속으로 세그먼트가 여럿인 카메라는 `epochs={"cam_A": 1}` 처럼 고른다. 프레임별 촬영 시각이 필요하면 I0 재생으로 읽는다
([`gateway/recorder/README.md`](../../gateway/recorder/README.md) "Tier 0 에서 다시 읽기").
위반이면 `ValueError` 이고, 문장에는 경로와 위반 규칙만 적는다(받은 값 없음). manifest 를 직접 넘기는 `recordings_from_manifest` 도
`camera_id` 중복·범위 역전은 다시 본다. jsonschema 가 필요하다(extras `calibration` → `tier0` 묶음, 또는 dev 그룹).
