# 분석 워커 `bev_floorplan` 작업 (S15P31A101-239)

API 표 v11 "BEV 평면도 생성"(FR-22, 분석 워커, MVP)의 워커 쪽이다. BEV 생성 로직은 S15P31A101-95(`simai_ai.scene.bev`·`bev_export`·
`room_outline`), 대여 루프는 S15P31A101-19 가 만들었다. 이 티켓은 그 둘을 I5 `bev_floorplan` 처리기로 잇고 입력·결과 계약을 정한다.
EC2 쪽(작업 등록·`floor_plan` 저장)은 S15P31A101-240, 워커 전용 서명 API 는 S15P31A101-248 이다.

## 관련 명세

| 절 | 내용 | 이 문서에서 |
|---|---|---|
| 4.1 BEV 평면도 | 보조 화면. 좌표·스케일 기준 아님, 정확도 목표 없음, 안전 판단에 안 씀. RunPod 분석 워커가 만든다 | 결과 `purpose`·`safety_use` 고정 |
| 12.5 | `bev_floorplan`: 보정 버전, 카메라 프레임 주소(정적 3D 가 있으면 Scene 버전) → BEV 이미지 주소. BACKGROUND | 입력·결과 계약 |
| 14 | 객체 저장소는 비공개, 임시 서명 주소로만 업로드·조회 | `ObjectStore` 주입점 |
| 3.2 | Tier 0 우선(접수 문·LIVE 선점 반납) | 러너 그대로, 처리기는 취소 확인 |

계약: `contracts/messages/bev_floorplan.schema.json`(신설), `job_request` 3.2.0, `job_callback` 2.2.0(둘 다 마이너, 조건부 `$ref`).

## 결정 (2026-10-23, 이연주 승인)

- **객체 저장소 접근:** 워커 전용·대여 묶음 서명 API `POST /api/worker/jobs/{job_id}/objects/sign`(워커 토큰, `lease_id`·`op` GET|PUT·대상).
  GET 은 그 작업 input 에 있는 주소만, PUT 키는 EC2 가 정한다. EC2 구현은 -248. 이 티켓은 주입점(`services/worker/objects.py`
  `ObjectStore.get/put`)만 두고 시험은 가짜로 한다. 다른 작업(vlm_rationale·static_reconstruct·incident_summary)도 이 주입점을 쓴다.
- **보정 문서:** input 의 `calibration_ref`(s3)·`calibration_hash` 로 받아 바이트 sha256 을 확인한다. 다르면 재시도 불가 FAILED.
- **계약:** input `frames[]`(camera_id·frame_uri·촬영 시각 계약), 결과는 닫힌 값 객체 + 전체 메타데이터 사본 `metadata_uri`,
  `scene_version` 은 정적 3D 층을 안 쓰면 null.

## 티켓 항목과 코드

| 항목 | 위치 |
|---|---|
| 처리기 등록 | `ai/services/worker/__main__.py` `build_handlers(config, store)`. `store` 가 있을 때만 `bev_floorplan` 을 넣는다 |
| 입력 → BEV | `ai/services/worker/bev_floorplan.py` `BevFloorplanHandler`. 생성은 `generate_bev`, 저장 형식은 `save_floorplan`(PNG 알파 = coverage + JSON) 그대로 |
| 이미지 업로드·결과 | 같은 파일. PNG·JSON 을 `ObjectStore.put` 으로 올리고 돌려받은 주소를 결과에 싣는다 |
| BACKGROUND·Tier 0 우선 | 우선순위는 EC2 대여 순서, 반납은 `runner.Worker`. 처리기는 받기·생성·올리기 사이마다 `ctx.cancelled` 를 보고 `CANCELLED` 로 멈춘다 |

## 처리 순서와 실패 코드

1. 입력을 계약(`bev_floorplan.schema.json#/$defs/input`)으로 검사하고 카메라 중복을 본다 → `BEV_INPUT_INVALID`.
   워커 클라이언트는 `job_request` 입력을 스키마로 검사하지 않으므로 처리기가 한다. 3.1.0 형식(`frame_uris` 만)도 여기서 걸린다.
2. 보정 문서를 받아 해시(`CALIBRATION_HASH_MISMATCH`), JSON·`calibration` 스키마·버전 일치(`CALIBRATION_INVALID`)를 본다.
3. 프레임을 받아 디코딩한다(OpenCV `imdecode`, BGR) → 읽을 수 없으면 `BEV_FRAME_UNREADABLE`. `Frame.frame_ref` 는 파일 입력 형식
   `{file_uri, frame_index: 0}`.
4. `generate_bev`(기본 `BevConfig`: CALIBRATED 만, 0.02 m/px, 범위 자동) → `BevInputError` 는 `BEV_INPUT_REJECTED`, 덮인 픽셀이 없으면
   `BEV_NO_COVERAGE`.
5. PNG·JSON 을 올린다.

위 코드는 모두 재시도 불가다(같은 입력이면 같다). 객체 저장소 오류(`OBJECT_GET_FAILED`·`OBJECT_PUT_FAILED`)만 `ObjectStoreError.retryable`
을 따른다.

## 명세에 없어 정한 것

- **입력 `frame_source` 필수.** 결과 `input_provenance.frames.source` 에 들어가고 SIM 이면 상한 결과다(규칙 7). 워커가 프레임 주소만
  보고 출처를 정할 수 없어 EC2 가 싣는다. 결정 파일에 없던 필드라 MR 에 적는다.
- **결과 `room_outline`** 은 `room_outline.convex_outline_from_mask` 출력 그대로(World 다각형, 늘 inferred, 정확도 주장 없음,
  `openings` null). 덮인 픽셀이 없으면 4 에서 실패하므로 성공 결과에서는 늘 채워진다. 스키마는 외곽을 끈 설정을 위해 null 도 받는다.
- **`input_provenance`** 는 공통 정의 `common/defs.schema.json#/$defs/input_provenance` 를 그대로 `$ref` 한다.
- **`scene_version` null.** `StaticSceneLayer` 구현이 없어 정적 3D 층을 쓰지 않는다. 입력의 `scene_version` 은 지금 무시한다.
- **진입점은 아직 저장소를 주지 않는다.** -248 의 서명 클라이언트 전에는 `bev_floorplan` 을 등록하지 않는다. 올릴 수 없는 작업을
  대여해 시도 횟수를 쓰지 않게 하려는 것이다.

## 버린 대안

- 처리기에 저장소 없이 항상 등록하고 업로드 단계에서 실패: 실제 EC2 작업이 시도 횟수만 쓰고 FAILED 로 끝난다.
- `save_floorplan` 대신 PNG 를 직접 만들기: 채널 순서·알파 규칙을 다시 짜게 된다. 임시 폴더에 쓰고 바이트를 읽는다.
- result 에 `generate_bev` 메타데이터 전체: 계약 밖 형식(`simai.bev_floorplan` 0.1.0)을 닫힌 스키마로 고정하게 된다. 사본으로 둔다.
- `pyproject` worker extra 에 jsonschema·OpenCV 추가: 잠금 재생성이 지금 uv 판에서 lock revision 을 바꾼다. Pod 는
  `--extra tier0 --extra worker` 한 가상환경(`unified-env.md`)이라 tier0 쪽 extras 로 둘 다 이미 깔린다.

## 시험

`ai/tests/services/worker/test_bev_floorplan.py`. 객체 저장소·디코더는 가짜, 보정은 계약 예시 `calibration.g1_auto`(CALIBRATED).

- 종단: 실제 HTTP 가짜 EC2(`conftest.run_main`)로 대여 → 실행 → PNG·JSON 업로드 → 제출. 가짜 EC2 가 `job_callback` 2.2.0 을 계약으로 검증한다.
  결과의 해시·주소·픽셀 ↔ World·방 외곽·`input_provenance`·`scene_version` null 을 본다.
- 해시 불일치, 3.1.0 입력·빈 프레임·같은 카메라 두 장, 보정 버전 불일치, 영상 크기 불일치, 저장소 일시 오류(재시도 → 2번째 시도 성공),
  취소 뒤 업로드 없음, 저장소 없으면 등록 안 됨.
- HTTP 가짜 EC2 fixture(`http_ec2`·`run_main`)는 `test_main.py` 에서 `conftest.py` 로 옮겼다(두 파일이 같이 쓴다).

## 잔여 위험·넘긴 일

- -248(완료): 워커 전용 서명 API 와 워커 쪽 HTTP 서명 클라이언트 `HttpObjectStore`. `__main__` 이 저장소를 넘겨 bev_floorplan 처리기가 등록된다(`server-object-storage.md`).
- -240: 작업 등록(input 의 `calibration_ref`·`calibration_hash`·`frame_source`·`frames`), `floor_plan` 의 픽셀 ↔ 미터 대응 저장(컬럼이면 V3 마이그레이션).
- 명세 12.5(입력 표)·12.6(서명 API 행) 반영은 명세 정리 MR.
- 사람 없는 프레임은 EC2 가 고른다. 사람이 찍힌 프레임은 바닥에 늘어진 얼룩이 된다(`exclude_masks` 연결은 하지 않았다).
- `generate_bev` 는 중간에 멈추지 않는다. LIVE 선점 뒤 최대 수십 초 CPU 를 더 쓴다(GPU 는 쓰지 않는다).
