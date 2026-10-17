# Tier 0 2D 관측 Parquet 보관 설계

S15P31A101-162. 기획서 §11.2(`observation_2d` 는 EC2 DB 가 아니라 RunPod Network Volume 의 Parquet), §11.5(2D 관측 7~14일), §12 I1(EC2 에는 필요할 때 요약만), §18.4(정리 확인·세션 단위 삭제), §19.1, 요구사항 FR-141 기준이다.
코드는 `ai/src/simai_ai/io/obs2d_layout.py`(경로·설정·보관 기간 정리, 표준 라이브러리만)와 `obs2d_parquet.py`(기록기·리더, pyarrow), 시험은 `ai/tests/io/test_obs2d_store.py`, 용량 측정은 `ai/tests/io/obs2d_sim.py` 다.

## 범위

- 입력: Local Tracker 가 내는 `observation_batch` payload(`observation_batch_payload()`) 또는 봉투째 메시지. 계약은 바꾸지 않는다.
- 범위 밖: EC2 로 보내는 I1 요약(S15P31A101-155), Tier 0 조립(`services/tier0`)에서 기록기를 붙이는 일, 보정 재계산(calibration_ba) 자체.

## 저장 형식

행 하나 = 검출 하나. 사람이 없는 프레임은 `detection_index` 부터 null 인 행 하나로 남겨 "처리됨"을 보존한다.

| 열 | 형식 | 내용 |
|---|---|---|
| `capture_time_ms` | int64 | `estimated_capture_time_ms` (UTC ms) |
| `camera_id`, `timestamp_source`, `calibration_version`, `joint_layout`, `session_id`, `input_provenance` | 사전 문자열 | `input_provenance` 는 JSON 문자열 |
| `stream_epoch`, `frame_sequence` | int64 | I0 프레임 식별자 |
| `timestamp_uncertainty_ms` | float64 | 모르면 기록하지 않는다(0 으로 채우지 않음) |
| `detection_index` | int32 | 프레임 안 검출 순서 |
| `local_track_id`, `world_id` | 문자열 | 카메라 내 트랙 ID, 카메라 간 연결 결과(없으면 null) |
| `bbox`, `score`, `keypoints_2d` | float32 | keypoint 는 `[u, v, conf]` 또는 null(검출 안 됨). conf 0 과 null 을 구분한다 |

- 계약에 없는 필드(영상·crop 등)는 열이 없어 저장되지 않는다. 얼굴 픽셀·영상을 남기지 않는다(§11.5).
- 수는 float32 다. 2304 px 에서 정밀도가 약 0.0002 px 로 검출 잡음보다 훨씬 작다. 왕복 일치는 float32 반올림 값 기준이다.
- 파일 메타데이터(`simai.obs2d`)에 형식 버전, 카메라, 구간, 세션, part, `source_id`, `input_provenance` 를 둔다.

## 파일과 경로

`{root}/{dir_template}/obs2d.{camera_id}.{YYYYMMDDTHHMM}Z.{session_id}.{part:04d}.parquet`

| 설정 | 기본값 | 근거 |
|---|---|---|
| `root` | 환경변수 `SIMAI_OBS2D_ROOT` (없으면 오류) | Pod 에서는 Network Volume 아래, 예: `/workspace/simai/obs2d` |
| `dir_template` | `{date}/{hour}/{camera_id}` | `{session_id}` 도 쓸 수 있다. 모두 UTC |
| `roll_interval_ms` | 1시간 | 1분 단위, 1시간을 나누어떨어지게 |
| `retention_days` | 14 (7~14 만 허용) | §11.5 |
| `store_max_hz` | 15 (5~15 만 허용) | §11.2 저장 5~15 Hz |
| `row_group_rows` | 20,000 | 메모리 버퍼 |
| `compression` | zstd | 표본에서 snappy 보다 약 10% 작다 |

- 파일은 촬영 시각 구간 기준이다(현재 시각을 읽지 않는다). 같은 카메라의 촬영 시각은 앞으로만 간다고 보고(Local Tracker 가 보장), 새 구간 프레임이 오면 그 카메라의 이전 파일을 닫는다. 촬영 시각이 거꾸로 간 프레임은 버리고 센다.
- 쓰는 중에는 `<최종 이름>.inprogress` 를 배타 생성(`xb`)해 쓰고, 닫을 때 최종 이름으로 바꾼다. 대상이 있으면 덮어쓰지 않고 오류다(Windows `os.rename`, POSIX `os.link` 후 임시 파일 삭제).
- 새 기록기는 같은 카메라·구간·세션의 기존 part(닫힌 것·쓰는 중인 것 모두) 다음 번호로 연다. 재시작하면 새 파일로 이어 쓴다.
- 프로세스가 죽으면 열린 파일은 footer 가 없어 읽을 수 없다(최대 `roll_interval_ms` 만큼 잃는다). 리더는 닫힌 파일만 읽고, 남은 `.inprogress` 는 보관 기간 정리에서 함께 지운다. 잃는 양을 줄이려면 `roll_interval_ms` 를 줄인다.
- 솎기: 카메라마다 저장 예정 시각을 `1000 / store_max_hz` 간격 격자로 두고, 촬영 시각이 예정 시각 − 주기 × 0.2 이상인 첫 프레임을 저장한다. 15 fps 입력은 지터가 있어도 모두 남고, 25 fps 입력은 평균 15 Hz(또는 5 Hz)로 줄어든다.

## 읽기

`read_observations(root, start_ms, end_ms, camera_ids, session_ids, columns)` 가 촬영 시각 `[start, end)`·카메라·세션으로 골라 하나의 Arrow 표로 돌려준다(촬영 시각·카메라·프레임·검출 순서). `iter_frames(table)` 은 `frame_observation` 모양으로 되돌린다. 파일 이름의 구간으로 먼저 거르고 행 단위로 다시 거른다.

## 보관 기간과 삭제

- 파일 안 모든 행이 보관 기간을 넘겼을 때만 지운다: 구간 시작 + 1시간 + 보관 기간 ≤ 지금. 지금 시각은 주입한다.
- `purge_expired(root, now_ms, retention_days, dry_run=True)` 기본은 미리보기다. 지운 뒤 비게 된 디렉터리도 지운다(root 는 남긴다). 이름 형식이 아닌 파일과 root 밖 파일은 건드리지 않는다.
- CLI: `uv run python -m simai_ai.io.obs2d_layout [--root DIR] [--retention-days 14] [--now-ms MS] [--apply]` (기본 미리보기, JSON 출력). Network Volume 정리 작업이 주기적으로 부르고, 18.4 주간 확인은 미리보기 결과가 비었는지 본다.
- 참여자 삭제 요청(§18.4): `list_files(root, session_ids=[...], include_in_progress=True)` 로 고른 뒤 `delete_files(..., dry_run=False)`.

## 용량 (표본 실측에서 계산)

`uv run python tests/io/obs2d_sim.py` 결과. 표본은 카메라 4대 25 fps, 사람 10명(§11.4 용량 기준), 60초다. SIM 입력이라 관절 신뢰도·검출 누락 분포가 실제와 다르므로 대략값이다(`is_upper_bound = true`).

| 저장 상한 | 저장 빈도(카메라당) | 바이트/행 | 1시간 | 하루 8시간 | 7일(8h/일) | 14일(8h/일) | 7일(24h) | 14일(24h) |
|---|---|---|---|---|---|---|---|---|
| 15 Hz | 15.0 Hz | 233 | 501 MB | 4.0 GB | 28 GB | 56 GB | 84 GB | 168 GB |
| 5 Hz | 5.0 Hz | 242 | 174 MB | 1.4 GB | 9.7 GB | 19.5 GB | 29 GB | 58 GB |

- 대부분이 COCO17 keypoint 51개(float32, 204 바이트/행)다. 잡음이 있는 좌표라 zstd 로 거의 줄지 않는다.
- 운영 가정(하루 8시간, 14일, 15 Hz)이면 Network Volume 에 약 56 GB 가 필요하다. 볼륨 크기가 모자라면 `store_max_hz` 를 5 Hz 로 낮추거나 보관을 7일로 줄인다(§11.4 "필요하면 5 Hz").

## 남은 일

- `services/tier0` 조립에서 기록기를 붙이고 세션 ID 를 정한다. 정리 CLI 를 주기적으로 돌리는 방법(cron·Pod 기동 스크립트)을 정한다.
- 루트 경로 `/workspace/simai/obs2d` 는 §19.1 볼륨 구조 목록에 없는 새 디렉터리다. 확정되면 명세 볼륨 구조에 더한다.
- Network Volume 크기 확인(§19.1).
