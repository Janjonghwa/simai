# S15P31A101-714 이전 맵 재사용은 고른 맵의 세팅 묶음을 그대로 쓰고, 지금 카메라와 맞는 맵을 고른다

관련 명세: 4.3(현장 세팅·1단계 카메라 헬퍼), 4.4(틀어짐 감시), 4.5(보정·Scene 계보), 12.2·12.6(기기·세팅 REST), 13.4(세팅 화면).
계약 `messages/coverage_guide` 1.2.0(묶음 v7.56.0). DB V78.
앞 문서: `S15P31A101-614-setup-reuse-mode.md`(재사용 모드), `-689`(재사용 판정·늘 고르기), `-712`(1단계 시점 확인), `-686`(새 배경),
`-687`(카메라 확인 맵 앞), `-688`(완료 조건·시간 예산), `-690`(높이 확인), `-691`(세팅 화면), `world-reanchor.md`(-584).

## 문제(16차, 2026-11-15)

- 재사용이 「가장 최근 완료 세팅」 하나(현재 관제 맵)만 기준으로 삼았다. 15차(11-13 16:31) NEW 세팅이 기록상 COMPLETED 라 기준이 됐지만 그날은 카메라
  위치 문제가 있던 날이고(카메라 확인 마감 INCOMPLETE), 14차 보정이 남아 있는데도 고를 수 없었다.
- 재사용이 그 보정을 출발점으로 배경·초기 복원·Stage A 를 다시 돌려 보정을 새로 맞췄다. 다른 날 영상에서 `worsened_pairs=['cam_B-cam_C']` 로 실패했다.

## 사용자 결정(2026-11-15)과 이 티켓의 답

| 결정 | 구현 |
|---|---|
| 재사용 = 그 맵 생성 때의 세팅 묶음을 그대로 | 보정·카메라 높이·구역·Scene 을 그대로 쓰고 워커 작업을 만들지 않는다(아래 「묶음」) |
| 오늘 할 일은 카메라가 그때와 같은 자리인지 확인뿐 | -712 시점 확인을 **고른 맵(후보 맵마다)의 사진·렌즈** 기준으로 돌린다. 4대 MATCH 일 때만 시작 |
| 최근 완료 맵을 날짜와 고르고, 기본은 지금 카메라와 가장 잘 맞는 맵 | 후보 목록(최근 8개)·순위(대응 수 합)·추천. 맞지 않는 맵은 이유와 함께 꺼짐 |
| 완료 뒤 문제가 확인된 세팅은 빼거나 표시 | DB 사실로만 판정하는 제외 규칙 5개(아래). 목록에는 남겨 이유를 보인다 |
| 미세 보정 단계는 근거로 | 측정 근거가 없어 **남기지 않음**. 대신 「그대로 사용」의 회전 한도를 0.01 rad 로 좁혔다 |

## 묶음 정의

후보 맵 하나 = 새 맵(NEW) 세팅 세션 S 가 만든 것:

- **Scene**: `S.scene_version`(정밀 맵, 다시 굽기 APPLIED 면 옮긴 맵). 재사용 세션은 시작 때의 값을 `reuse_scene_version` 에 남긴다.
- **보정**: `S.calibration_version`(원문 calibration_document). Tier 0 에 보낼 때는 내용 그대로(아래 「Tier 0 로 가는 보정」).
- **카메라 높이**: `S.camera_heights_m`. 재사용 세션 높이가 되고 현장 기본값(다음 세팅·GPU 기동 `--measured-heights`)으로도 남긴다. 1단계 입력 높이는 쓰지 않는다.
- **구역**: 그 맵의 보정(S 의 보정 + 그 맵을 재사용하며 같은 내용으로 다시 낸 보정)으로 **마지막에 발행한 구성 묶음**의 구역·이은 작업면 버전
  (`config_bundle_item`). 재사용한 날 구역을 고쳤으면 그 고친 구역이 그 맵의 최신 구역이다. 지금 관제 구역이 다른 맵(다른 World)의 것이어도 쓰지 않는다.

## 재사용 흐름(건너뛴 단계와 근거)

```
1단계(맵 카드): 후보 맵 목록·추천(GET /api/setup-sessions/reuse-check, 10 s) ← 헬퍼 1.2.0 맵별 시점 확인(라운드마다)
  「다음」 → POST /api/setup-sessions {mode: REUSE, reuse_setup_session_id}   (배경 촬영 창·GPU 경고창·겹침 확인창 없음)
  서버: 같은 판정으로 MATCH 확인 → 세션 COMPLETED(진행 100, job 없음, 보정 = 그 맵 보정, 높이 = 그 맵 높이, reuse_view_check 남김)
3단계(구역): zones/seed 가 그 맵의 구역으로 채움 → 사람이 확인·수정 → 「세팅 완료」
  완료: 구역 적용 → 완료 기록 → 그 맵이 관제 맵이 아니면 켬(활성화 이력 추가) → 그 맵의 보정으로 구성 묶음 발행
```

| 단계 | 재사용(그대로 사용) | 근거 |
|---|---|---|
| 배경 촬영(-686) | 건너뜀 | 세팅 배경은 워커 복원(MapAnything·Stage A) 입력이다. 관제 배경은 Tier 0 누적기(-358)가 관제 중에 따로 모은다. 보정을 다시 계산하지 않으므로 필요 없다 |
| 초기 복원·높이 확인(-690)·Stage A | 건너뜀 | 사용자 결정(재계산 금지). 높이는 그 맵의 높이 |
| 카메라 확인 걷기(-687) | 건너뜀 | 오늘 할 일은 「같은 자리인지」뿐이고 그 확인은 1단계 시점 확인이 맵 앞(시작 전)에서 한다. 앞에서 통과하면 뒤에서 막지 않는다 |
| READY_FOR_ZONES | 세션을 바로 COMPLETED 로 만든다 | 워커 단계가 없다. 구역 단계 관문(카메라 확인 PASSED)은 재사용 세션이면 보지 않는다 |
| 세팅 완료 관문(-688) | 단계만 본다 | World 채택·카메라 확인 대신 시작 때 시점 확인을 통과했다(`SetupCompletionGate` reuseAsIs) |

- 사용자 원칙: 재사용을 막는 판단은 모두 1단계(시작 전)에 보이고, 시작한 뒤에는 막지 않는다. 맞는 맵이 없으면 재사용 칸을 끄고 이유 두 줄(무엇이/어떻게)을
  보인다. 새 맵 만들기는 늘 켜져 있고 조용히 NEW 로 바꾸지 않는다. 사람이 움직일 일이 없어 큰 모달도 없다.
- GPU: 재사용 시작은 GPU 가 필요 없다(워커 없음). Tier 0 가 켜져 있으면 구성 묶음으로, 꺼져 있으면 다음 GPU 기동 때 가장 최근 묶음의 보정으로 시작한다(`GpuStartConfig`).
- 옛 재사용 경로(워커 `REUSE_SCENE`)는 EC2 가 더는 보내지 않는다. 워커 코드·스냅샷 호환 처리(`SetupReuseMode.read`)는 배포 중 남은 세션을 위해 그대로 둔다.

## Tier 0 로 가는 보정(구성 묶음 발행)

Tier 0 는 구성 묶음의 보정이 자기 저장소에 없으면 "세팅 출처 + `previous_calibration_version` = 지금 활성 보정"일 때만 새 기준으로 채택한다(-467
`setup_candidate_problem`). 옛 보정 X 를 그대로 보내면 다른 보정을 쓰는 Tier 0 는 거부한다. 그래서 완료 때(`SetupReuseCalibration`):

1. Tier 0 가 알린 지금 보정이 X 와 **같은 내용**(EC2 가 원문에서 다시 잰 content_hash 가 같음)이면 그 버전을 그대로 싣는다.
2. 아니면 X 의 원문을 **계보 키만 바꾼 새 버전** `X-reuse-<세션 8자>` 로 낸다: `calibration_version`·`previous_calibration_version`(= Tier 0 지금 보정, 모르면 뺌)·
   `setup_session_id`(재사용 세션)·`created_at`. 이 키들은 content_hash 에 들어가지 않으므로 해시가 X 와 같고, 다시 잰 해시가 다르면 내지 않는다
   (`CalibrationService.reissueForReuse`). 카메라 K·R·t·World 는 한 글자도 바뀌지 않는다. 보정은 새 버전을 더한다(규칙 6).
3. 그 보정으로 묶음을 발행하고 세션 `calibration_version` 을 그 버전으로 둔다. Tier 0 가 아직 알리지 않은 동안 다른 발행(작업 시작 등)도 그 보정을 싣도록
   `ConfigBundleService.pendingSetupCalibration` 이 재사용 세션 보정도 본다.

- Scene 정합: 맵(X)과 Tier 0(X')의 버전이 달라도, 두 원문이 EC2 에 있고 한쪽이 재사용 세션 보정이며 content_hash 가 같으면 `SAME_CALIBRATION`·VERIFIED 다
  (`SceneCompatibility`). Tier 0 알림의 해시만으로는 판단하지 않는다.
- Tier 0 위치 정본 규칙(1)은 그대로다: 위치는 Tier 0 가 그 보정으로 만든다. 헬퍼 결과는 재사용을 막을지의 근거일 뿐 보정·위치를 만들지 않는다.

## 맵 후보·순위 규칙(`SetupReuseCandidates`·`SetupReuseCheck`)

- 후보: 이 현장의 `mode=NEW`·`status=COMPLETED`·`scene_version`·`calibration_version` 이 있는 세팅, 세팅 완료 시각(없으면 작업 끝난 시각) 내림차순 8개.
- 헬퍼 기준: 제외되지 않은 최근 후보 5개(`reuse_references`)마다 그 Scene 굽기 입력 사진(-593 `SceneCameraImageService`)과 그 보정의 렌즈.
  사진·렌즈가 없으면 싣지 않고 그 맵을 「사진 없음」으로 보인다.
- 맵마다 상태:

| 상태 | 조건 | 고를 수 있나 |
|---|---|---|
| EXCLUDED | 아래 제외 규칙 | 아니오(이유 표시) |
| CHANGED | 시점 확인 CHANGED(2회 연속) 카메라가 있다, 또는 Tier 0 가 지금 그 맵 보정을 쓰는데 -540 회전 감시가 재세팅을 요구한다 | 아니오(카메라 표시) |
| SHIFTED | 4대 SAME 이지만 순수 회전이 0.01 rad 를 넘는 카메라가 있다 | 아니오(카메라 표시) |
| MATCH | 4대 SAME, 회전 ≤ 0.01 rad | 예 |
| UNCONFIRMED | 결과 없음(헬퍼 꺼짐·첫 라운드 전)·사진 없음·일부 UNKNOWN(가림·확인 중·영상 없음) | 아니오(확인 중) |

- 순위: MATCH(카메라별 상호 매칭 대응 수 합이 많은 순 = 지금 화면과 가장 잘 맞음, 같으면 최근) → UNCONFIRMED → SHIFTED → CHANGED → EXCLUDED(각 최근 순).
  추천 = 첫 MATCH. 화면은 사용자가 고른 맵이 아직 MATCH 면 그 맵, 아니면 추천으로 돌아간다. 시작(POST)도 같은 판정이라 그 사이 어긋나면 409.
- 모르면 쓰지 않는다: -712 는 「모르면 막지 않는다」였지만 그때는 워커가 마지막으로 확인했다. 이제 아무도 다시 확인하지 않으므로 4대 MATCH 만 시작한다(규칙 2).

### 제외 규칙(DB 사실만)

| 이유 | 사실 | 근거 |
|---|---|---|
| CAMERA_CHECK_NOT_PASSED | 그 세팅의 `camera_check` 기록이 있고 그 보정으로 PASSED 가 아니다 | 15차 같은 「카메라 확인 마감 INCOMPLETE」. 기록이 없는 옛 세팅(카메라 확인 도입 전, 예: 14차)은 문제 사실이 없어 가리지 않는다 |
| NOT_COMPLETED | 세팅 완료(C17, `work_unit.setup_completed_at`)를 이 세션으로 하지 않았다 | 미리보기·정밀 맵 자동 현재화만으로 관제 맵이 된 맵(사람이 구역을 확인해 올린 맵이 아니다). -689 가 「현재 관제 맵」을 기준으로 삼아 15차가 뽑힌 원인 |
| MAP_RETIRED | 그 Scene 이 더는 COMMITTED 가 아니다 | 켤 수 없다 |
| NO_CALIBRATION | 그 보정 원문이 없다 | 묶음에 실을 수 없다 |
| CAMERAS_CHANGED | 그때 높이를 적은 카메라 집합 ≠ 지금 현장 카메라 | 카메라를 더하거나 뺐다 |

「사용자 무효」는 DB 에 사실이 없어 이번에는 규칙으로 두지 않았다(넘긴 일).

## 미세 보정 판단

- 남기지 않는다. 몇 픽셀 틀어짐을 흡수하는 짧은 보정이 얼마나 자주 필요한지(거치대가 날마다 얼마나 움직이는지) 측정한 값이 없고, 있으면 "보정을 다시 맞춘다"는
  16차 실패 경로를 작게 되살린다.
- 대신 「그대로 사용」 판정을 좁혔다: -712 SAME 은 10° 순수 회전까지 SAME 이었다(보정을 다시 맞추던 때의 -584 한도). 보정을 그대로 쓰면 1° 도 8 m 앞에서 약 14 cm
  어긋나므로 0.01 rad(≈0.57°)를 넘으면 SHIFTED 로 막는다. 헬퍼는 분석 해상도(긴 변 960 px, 로비 광각 초점 ≈ 400 px)에서 대응의 절반 이상이 3 px 안이면 회전
  0 으로 보므로(≈0.0075 rad) 그 경계 위 추정 잡음만 넘긴다. 0.57° 의 시선 어긋남은 8 m 앞 약 8 cm 로 -584 배치 잔차 한도 15 cm 안이다.
- SHIFTED 가 자주 나오면(17차 현장 측정) 그때 미세 보정 단계를 「실패하면 원래 보정 + 경고」로 더할지 정한다.

## 계약·REST·DB

- `coverage_guide` 1.2.0: 선택 `reuse_view_checks`(1~5, 항목 = `reuse_view_check`). 실으면 1.2.0, 같은 Scene 두 번은 EC2 가 400. 1.0.0·1.1.0 그대로 받는다.
- 기기 수요 응답(계약 밖): `reuse_references`(후보 맵 기준 목록), 옛 헬퍼용 `reuse_reference` = 첫 항목. 응답이 커져 헬퍼 응답 상한을 64 KiB → 256 KiB 로 올렸다.
- `GET /api/setup-sessions/reuse-check`(계약 밖): 옛 필드 + `recommended_setup_session_id`·`as_is_max_rotation_rad`·`candidates[]`. `blocked_reason` 에
  `VIEW_UNCONFIRMED` 추가, `SCENE_MISMATCH`·`LAST_SETUP_REJECTED` 는 새 판정에서 쓰지 않는다(카메라 근거가 맵마다라서).
- `POST /api/setup-sessions`·`.../retry`: 선택 `reuse_setup_session_id`. 409 `SETUP_REUSE_MAP_UNAVAILABLE`·`SETUP_REUSE_VIEW_UNCONFIRMED` 추가.
  REUSE 는 `camera_heights_m` 을 쓰지 않고, 세팅 워커가 없어도 된다.
- 세션 조회: `reuse_source{setup_session_id, scene_version, view_check}`(재사용 세션만), `job_id` null 가능.
- DB V78: `setup_session.job_id` NULL 허용, `reuse_source_session_id`·`reuse_scene_version`·`reuse_view_check` 와 짝 검사(재사용 세션 ⇔ job 없음 ⇔ REUSE).
- 배포 순서: EC2·화면 먼저, 헬퍼 나중. 옛 헬퍼는 `reuse_reference`(첫 후보) 하나만 비교해 1.1.0 으로 보내고, EC2 는 그 맵만 판정한다(나머지는 확인 중).

## 정한 수치

| 값 | 위치 | 근거 |
|---|---|---|
| 그대로 사용 회전 한도 0.01 rad | `SetupReuseCheck.AS_IS_MAX_ROTATION_RAD` | 위 「미세 보정 판단」 |
| 목록 8개 | `SetupReuseCandidates.MAX_LISTED` | 하루 한 번 새 맵이면 한 주 남짓. 가린 맵도 이유를 보이려고 넉넉히 |
| 헬퍼 비교 5개 | `MAX_REFERENCES`·계약 maxItems·헬퍼 `MAX_REFERENCES` | 카메라당 SIFT 를 후보마다 한 번 더(분석 960 px, 수십 ms) → 4대 × 5 ≈ 1~2 s(노트북 CPU). 헬퍼 라운드 2 s(GPU)·16~24 s(CPU) 안 |
| 헬퍼 응답 상한 256 KiB | `service.MAX_RESPONSE_BYTES` | 서명 주소(약 1.5 KB) × 4대 × 5개 + 렌즈 ≈ 40 KB 에 여유 |
| 다시 낸 보정 이름 `<원래>-reuse-<세션 8자>` | `SetupReuseCalibration` | 버전은 불변이고 세션마다 하나 |

## 버린 대안

- **이전 보정을 출발점으로 재계산(지금까지)**: 사용자 결정으로 금지(16차 worsened_pairs).
- **옛 보정 X 를 그대로 묶음에 싣기**: Tier 0 가 계보 검사로 거부한다(저장소에 없거나 previous ≠ 활성). Tier 0 의 채택 규칙을 넓히는 것(-467 안전장치 약화, I1 계약)보다
  계보 키만 바꾼 같은 내용의 새 버전이 기존 경로를 그대로 쓴다.
- **EC2 가 보정 문서를 새로 써서 content_hash 를 다시 계산**: 해시 대상 키를 바꾸지 않으므로 계산이 필요 없다. 다시 잰 해시가 같을 때만 낸다.
- **재사용도 짧은 워커 작업으로 보정 재발행·카메라 확인 걷기**: GPU·사람 걷기(약 1분+)가 다시 들어가 1~2분 목표를 넘고, 오늘 할 일은 시점 확인뿐이라는 결정과 맞지 않는다.
- **UNKNOWN 이면 막지 않기(-712 원칙 유지)**: 다시 확인하는 단계가 없어졌으므로 모르면 쓰지 않는다. 새 맵은 늘 고를 수 있다.
- **헬퍼 메시지를 맵마다 하나씩 보내기**: 각도 안내와 한 메시지라 맵 수만큼 각도 안내가 중복된다. 목록 필드 하나로 충분하다.
- **사용자 「무효」 단추를 이번에 추가**: 결정 4 는 DB 사실로만 판정하라 했다. 새 사실을 만드는 기능은 넘긴 일.

## 남은 위험

- **헬퍼가 꺼져 있으면 재사용을 못 쓴다**: 모든 후보가 UNCONFIRMED(결과 없음)다. 「카메라 헬퍼가 켜져 있는지 확인」 안내는 1단계 각도 안내 상태(-691)로 보인다.
  새 맵은 늘 고를 수 있다.
- **무늬 없는 벽만 보는 카메라**(NO_TEXTURE)는 늘 UNKNOWN 이라 그 현장은 재사용을 못 쓴다(로비 4대는 해당 없음, 17차 확인).
- **조명·혼잡**: 대응이 줄어 VIEW_DIFFERENT 오탐·OCCLUDED 확인 중이 날 수 있다(-712 그대로, 2회 연속 확인). 사람이 카메라 앞을 비우면 다음 라운드에 풀린다.
- **5 cm 이하 이동**은 SAME 으로 지나갈 수 있다(-712 시차 한도). 그대로 쓰면 그만큼 위치가 어긋난다(-584 배치 한도 15 cm 안).
- **Tier 0 가 묶음 도착 전에 보정을 바꾸면**(회전 복구 bootstrap 등) 새 버전의 previous 가 맞지 않아 묶음이 거부되고 Tier 0 는 이전 보정으로 판정한다(-467 과 같은 위험).
  화면은 Scene 정합 흐림으로 알린다. 그 뒤 발행(작업 시작 등)은 Tier 0 의 지금 보정을 싣고 그 맵의 보정을 다시 내지 않는다(세팅 완료는 하루 1회).
  드문 경우지만 자동으로 되돌리지 않는다(넘긴 일: config_ack 거부 감지·재발행).
- **녹화 재생 시험**: 헬퍼는 실제 카메라를 보므로 다른 날 녹화를 Tier 0 에 재생하면 판정이 녹화와 맞지 않는다(-712 그대로).
- **정적 객체(구역에 잇지 않은 장애물)** 는 그 맵의 버전으로 되돌리지 않는다. 구역·이은 작업면만 되돌린다. 다른 World 의 맵을 오가면 잇지 않은 장애물이 어긋날 수 있다.
- 구성 묶음 발행이 같은 보정의 지난 세션 `config_version` 을 덮어쓰는 기존 동작(`ConfigBundleService.build`)은 그대로다(표시용).
- 화면 문구 「약 1~2분」은 표시용이다(17차 실측 필요).

## 넘긴 일

- 17차 현장: (a) 그대로 둔 날 추천 맵이 4대 MATCH 로 1~2분 안에 완료까지 가는지, (b) 카메라 하나를 1~2° 돌리면 SHIFTED 로 막히는지와 빈도(미세 보정 판단 근거),
  (c) 14차·15차·새 맵이 섞인 목록에서 15차가 「카메라 확인을 통과하지 못한 맵」으로 보이는지, (d) Tier 0 가 다른 보정을 쓰는 상태에서 완료 → 다시 낸 보정 채택·
  Scene 정합 VERIFIED, (e) 헬퍼 라운드 시간 증가.
- 사용자 무효 표시(맵 목록에서 「이 맵 쓰지 않기」, 새 열·감사 기록).
- 재사용 묶음 거부(Tier 0 config_ack REJECTED) 감지·재발행.
- 워커 `REUSE_SCENE` 경로 정리(배포 뒤 남은 세션이 없을 때).
- 명세 정리 MR: 4.3·13.4(재사용 = 고른 맵 묶음 그대로, 1단계 맵 목록·추천·제외), 4.5(같은 내용 보정 재발행 계보), 12.6(REST·오류 코드).

## 코드·시험 위치

- 계약: `contracts/messages/coverage_guide.schema.json`(1.2.0), 예시 `contracts/examples/coverage_guide.reuse_view_checks.json`, 반례
  `contracts/examples/invalid/coverage_guide.{schema_1_1_0_with_view_checks,empty_view_checks}.json`, `ai/src/simai_ai/contracts/models.py`(생성).
- 헬퍼: `gateway/camera_helper/src/simai_camera_helper/view_check.py`(`MultiViewChecker`), `message.py`(1.2.0), `service.py`(`reuse_references`·응답 상한).
  시험 `gateway/camera_helper/tests/test_view_check.py`(맵별 비교·목록에서 빠진 맵 버림·1.2.0 계약·클라이언트 목록).
- 서버: `SetupReuseCandidates`(신규, 후보·제외), `SetupReuseCheck`(맵별 판정·순위·추천·시작 판정), `SetupService.createReuse`, `SetupReuseCalibration`(신규),
  `CalibrationService.reissueForReuse`·`sameContent`, `SetupCompletionService`(맵 켜기·보정), `SetupCompletionGate`(reuseAsIs), `SetupZoneReuseSeed`(신규)·
  `SetupZonePlanService`(채우기·관문), `CoverageReuseReference.references`, `CoverageGuideService`(맵별 보관·`reuse_references`), `SceneCompatibility`(같은 내용),
  `ConfigBundleService.pendingSetupCalibration`, `ErrorCode`, `V78__setup_session_reuse_source.sql`.
  시험 `SetupReuseModeIntegrationTest`(후보·제외·순위·SHIFTED·CHANGED·TIER0_DRIFT, 그대로 사용 흐름: 워커 없음·높이·구역 채우기·맵 켜기·보정 재발행·정합 VERIFIED,
  Tier 0 가 같은 보정이면 재발행 없음), `CoverageGuideIntegrationTest`(1.2.0 맵별 보관·중복 거부), `CoverageReuseReferenceIntegrationTest`(후보 기준·사진 없음),
  `SceneCompatibilityTest`, `SetupCompletionGateTest`.
- 화면: `front/apps/admin/src/features/setup/useReuseScene.ts`(맵 목록·문구·고른 맵), `SetupModeChoice.tsx`(맵 목록), `SetupPage.tsx`(재사용 시작·구역 단계 직행·
  복원), `cameraCheck.ts`, `types.ts`, 목업 `mocks/setup.ts`. 시험 `reuseChoice.test.ts`, `SetupPage.test.tsx`.
