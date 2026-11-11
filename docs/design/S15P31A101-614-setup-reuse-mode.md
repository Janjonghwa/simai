# 세팅 모드 새 모델 / 이전 모델 재사용 (S15P31A101-614, C13)

- **티켓**: S15P31A101-614 (Owner: Claude 서브에이전트)
- **배경**: 팀원 곽나혁의 새 작업 관리 흐름(Lead·사용자 합의 11-11). 날짜 작업마다 세팅을 하되, 방이 그대로면 맵(정적 3D GLB)을 다시 만들지 않고 보정만 다시 해 지금 맵의 World 에 올린다.
- **범위**: contracts(site_setup 입력·진행·결과), 워커(`ai/services/worker/site_setup.py`), 서버(세팅 생성·진행·결과 반영·세션 조회, Scene 정합 판정, 맵 보강 차단), 프론트는 맵 보강 차단 사유 문구 한 줄.
- **범위 밖**: 세팅 화면의 모드 선택 UI·현재 관제 구역 채우기(프론트 티켓), 세팅 완료 C17(-615), 날짜 작업 모델(-610·-612).
- **관련 명세**: 3.2(GPU 우선순위), 4.1(World), 4.3(현장 세팅 자동 보정), 4.5(보정·Scene 버전 계보), 5.3(정적 3D), 11.4(버전 추가만), 12.5·12.6(I5·세팅 REST).
- **연관 설계**: `docs/design/site-setup-pipeline.md`(세팅 흐름), `docs/design/world-reanchor.md`(-584 World 다시 맞추기), `docs/design/S15P31A101-605-map-supplement-capture.md`·`S15P31A101-607-supplement-capture-flow.md`(맵 보강·다시 굽기), `docs/design/daily-work-units.md`(-610).

## 1. 흐름

```
POST /api/setup-sessions {work_unit_id, camera_heights_m, mode: NEW | REUSE}
  ├─ NEW(기본): 지금과 같다(작업 입력에 mode 없음 = FULL_SETUP)
  └─ REUSE: 현재 관제 맵(현장당 하나, CURRENT_COMMITTED)과 그 보정이 있어야 한다 → 작업 입력 mode=REUSE_SCENE

워커 site_setup (REUSE_SCENE)
  (a) 배경 수집 → (b)~(d) 내부 파라미터·초기 World → (e) Stage A    ← NEW 와 같다(스냅샷 mode=REUSE_SCENE)
  (f) World 다시 맞추기(-584, 기준 = Tier 0 활성 보정) → 새 보정 문서 등록(artifacts.calibration_document)
      ├─ 채택(world_anchor.accepted): JOINT_BA_AND_VERIFY(65) → READY_FOR_ZONES(85) → COMPLETED(100)
      │     미리보기·카메라 프레임·굽기 사진·AI 구역 초안·굽기 없음. 새 GLB·preview_scene_version 없음
      └─ 거부·기록 없음: 같은 작업 안에서 NEW 로 이어 감(g 미리보기·초안 → h 굽기)
            그 스냅샷부터 mode=FULL_SETUP + reuse_fallback_reason(= world_anchor.reason)

EC2
  READY_FOR_ZONES: 새 보정을 실은 구성 묶음 발행(Tier 0 가 새 보정 채택). 재사용이면 미리보기 현재화·초안 작업 없음
  COMPLETED: 재사용이면 새 Scene 등록·활성화 없음 → 현재 관제 맵 그대로. 세션 mode=REUSE
  세션 조회: mode(실제)·requested_mode(요청)·reuse_fallback_reason·world_anchor
```

화면은 재사용이면 구역을 현재 관제 구역으로 채운다(AI 초안이 필요 없다). 재사용도 보정은 다시 하므로 몇 분 걸린다(7절).

## 2. 계약 (contracts v7.34.0, site_setup 1.11.0 · job_request 3.10.0 · job_callback 2.18.0 · job_heartbeat 2.10.0)

- **입력 `mode`**: `FULL_SETUP`(기본·생략) | `SUPPLEMENT_REBAKE`(-605) | `REUSE_SCENE`(새). 추가 입력은 없다. 기준 World 는 워커가 이미 들고 있는 Tier 0 활성 보정이다(-584 와 같음). 서버는 REST `NEW` → 생략, `REUSE` → `REUSE_SCENE` 으로 옮긴다.
- **progress·result `mode`**(`$defs/effective_mode`): 그 대여가 실제로 따르는 모드. REUSE_SCENE 요청은 판정 전까지 `REUSE_SCENE`, 거부되면 그 스냅샷부터 `FULL_SETUP`. 같은 대여 안에서 되돌아가지 않는다.
- **`world_anchor`**(`$defs/world_anchor_summary`, 모든 모드에서 선택): 보정 문서 `world_anchor` 요약 `{accepted, reason, reference_calibration_version, max_center_residual_m?, max_orientation_change_rad?}`. reason 은 보정 문서 값 그대로에 `NO_REFERENCE`(문서에 기록 없음)를 더했다. accepted ↔ ANCHORED, ANCHORED 면 기준 버전 필수, NO_REFERENCE 면 기준 null.
- **`reuse_fallback_reason`**: REUSE_SCENE 을 전체 세팅으로 바꾼 이유. 있으면 mode=FULL_SETUP, world_anchor(accepted=false) 필수. 값은 world_anchor.reason 과 같다(받는 쪽 검사).
- **REUSE_SCENE 규칙**(`$defs/reuse_rules`, progress·result 공통): READY_FOR_ZONES 이후면 calibration_version·height_checks·world_anchor 필수, world_anchor 는 채택, PREVIEW_AND_VLM_DRAFT·BAKING_TEXTURE 단계 없음, zone_drafts_ref·bake_skipped·supplement·reuse_fallback_reason 불가, artifacts 는 preview_scene·scene·camera_frames·bake_images 불가(calibration_document·floor_coverage 만).
- **결과의 버전 필드**: 1.10.0 까지 필수였던 `preview_scene_version`·`scene_version` 은 REUSE_SCENE 이 아닐 때만 필수다. REUSE_SCENE 은 둘 다 null 이거나 생략(워커는 생략). "scene_version null ⇒ bake_skipped" 규칙은 REUSE_SCENE 을 뺀다.
- **진행률**: 단계를 건너뛰어도(65 → 85 → 100) 같은 대여 안에서 단조 증가다. 서버 단계 순서 검사(감소 금지)와 맞는다.
- 예시: `job_request.site_setup_reuse`, `job_heartbeat.site_setup_reuse_ready`·`_reuse_fallback`, `job_callback.site_setup_reuse`·`_reuse_fallback`. 반례: `invalid/job_callback.site_setup_reuse_with_preview`·`_with_scene_artifact`·`_without_anchor`·`_rejected_anchor`·`_reuse_fallback_wrong_mode`·`_reuse_fallback_without_anchor`·`site_setup_anchor_reason_mismatch`, `invalid/job_heartbeat.site_setup_reuse_preview_stage`·`_reuse_ready_without_anchor`, `invalid/job_request.site_setup_rest_mode_value`.

REST 예시:

```jsonc
// POST /api/setup-sessions
{"work_unit_id": 12, "camera_heights_m": {"cam1": 2.44, "cam2": 2.44, "cam3": 2.48, "cam4": 2.48}, "mode": "REUSE"}
// 202 / GET /api/setup-sessions/{id} (발췌, 채택으로 끝남)
{"status": "COMPLETED", "mode": "REUSE", "requested_mode": "REUSE", "reuse_fallback_reason": null,
 "calibration_version": "cal-lobby-init-…-jba2-…", "preview_scene_version": null, "scene_version": null,
 "world_anchor": {"accepted": true, "reason": "ANCHORED", "reference_calibration_version": "cal-…", "max_center_residual_m": 0.0612, "max_orientation_change_rad": 0.0703},
 "supplement": {"can_upload": false, "can_rebake": false, "rebake_blocked_reason": "REUSED_SCENE", …}}
// 거부로 새 모델로 이어 감
{"mode": "NEW", "requested_mode": "REUSE", "reuse_fallback_reason": "CENTER_RESIDUAL_EXCEEDED",
 "world_anchor": {"accepted": false, "reason": "CENTER_RESIDUAL_EXCEEDED", …}, "preview_scene_version": "scene-preview-…", …}
```

오류: `mode` 가 NEW·REUSE 가 아니면 400 `INVALID_SETUP_REQUEST`. REUSE 인데 현재 관제 맵이나 그 보정 행이 없으면 409 `SETUP_REUSE_SCENE_MISSING`, 현재 맵이 Tier 0 의 지금 보정과 다른 World(Scene 정합 MISMATCH)면 409 `SETUP_REUSE_SCENE_MISMATCH`. 날짜 작업이 오늘이 아니면 409 `WORK_NOT_TODAY`(4절).

## 3. 거부되면 같은 작업에서 새 모델로 이어 간다 (결정과 근거)

- **선택**: World 다시 맞추기가 거부(카메라 이동·축척 차이·방향 변화·기준 없음·꺼짐)되면 실패시키지 않고 같은 작업 안에서 FULL_SETUP 의 (g)·(h) 를 이어 간다.
- **근거**: 거부 시점까지 한 일(배경 수집·내부 파라미터·Stage A·새 보정 등록)은 NEW 와 똑같다. NEW 의 미리보기·굽기도 바로 이 새 보정 문서로 정렬한다(-584 3.3). 실패시키면 관리자가 NEW 로 다시 시작해 같은 몇 분을 또 쓴다. 이어 가는 데 추가 입력이 필요 없다.
- **표시**: 그 스냅샷부터 `mode=FULL_SETUP` + `reuse_fallback_reason`, 세션은 `mode=NEW`·`requested_mode=REUSE`·`reuse_fallback_reason`. 화면은 "카메라가 움직여 새 모델로 세팅했습니다(사유)"를 보여 주고 구역은 AI 초안으로 그린다.
- **기록 없음**: 보정 문서에 world_anchor 가 없으면(비교할 Tier 0 활성 보정이 없음 등) 같은 World 라고 지어내지 않고 `NO_REFERENCE` 로 이어 간다(AGENTS 규칙 2).
- **재대여**: 재대여는 첫 스냅샷부터 다시 따른다. 서버는 새 대여의 첫 스냅샷에서 앞선 대여의 world_anchor 를 지운다.

## 4. 오늘만 검사 (팀원 요구)

세팅 시작(`SetupService.create`, 재시도 포함)이 -610 의 `WorkUnitRepository.requireToday(siteId, workId, now)` 를 부른다. 날짜 작업(`work_unit.daily=true`, V54)만 작업 날짜가 오늘(Asia/Seoul)인지 보고 아니면 409 `WORK_NOT_TODAY`, 예전 작업(daily=false)은 보지 않는다. 처음에는 -610 이 dev 에 없어 같은 규칙을 세팅 쪽에 임시로 두었다가, 작업 중 -610 이 머지되어 그 호출로 바꿨다(임시 코드·오류 코드는 남기지 않았다).

## 5. Scene 정합(SceneCompatibility) 처리

- **문제**: 재사용이 채택되면 현재 맵은 옛 보정 A 로 만든 Scene 이고 Tier 0 는 새 보정 B 를 쓴다. Tier 0 는 세팅 보정을 새 World 계보로 채택하므로(`activate(new_world=True)`, world_frame_id = B) 기존 판정은 world_frame_id 가 달라 `DIFFERENT_WORLD`·MISMATCH 로 맵을 흐린다. 하지만 B 의 World 는 world_anchor 로 A 의 World 에 옮겨진 같은 좌표계다.
- **선택**: B 의 보정 문서가 world_anchor 채택(ANCHORED)이고 그 기준 보정이 A 이거나 A 와 같은 world_frame_id 계보면 같은 World 로 본다. 기준 보정도 다른 보정에 맞춰졌으면 사슬을 8단계까지 따라간다(재사용을 거듭한 경우). 같은 World 면 카메라 자세 변화를 **채택 한도에 맞춘 값(중심 0.25 m, 방향 10°)** 으로 판정해 VERIFIED/SMALL_CHANGE 또는 DIMMED/LARGE_CHANGE 를 낸다. 실제 변화량과 카메라별 값은 그대로 싣는다.
- **한도 근거**: -584 채택 기준은 수평 배치 잔차 0.15 m·축척 ±3%·방향 10° 다. 12·13차 실측 같은 배치 쌍은 수평 적용 잔차 ≤ 11.4 cm, 판정에 쓰지 않는 높이 차 ≤ 9.4 cm(이전 바닥 기울기), 방향 ≤ 6.0° 였다(3차원 중심 차 ≈ 15 cm 이하). 기존 0.10 m·2° 로 보면 방이 그대로인 재사용도 거의 늘 흐리게 되어 재사용의 뜻이 없다. 중심은 수평 한도 0.15 m 에 높이 차 0.10 m 를 더한 0.25 m, 방향은 채택 한도 10° 를 그대로 쓴다.
- **"무조건 VERIFIED" 를 버린 이유**: 기준 보정이 Scene 보정과 다를 수 있고(같은 계보의 회전 복구 후보) 사슬을 따라가면 차이가 쌓일 수 있다. 실제 자세 차이를 한 번 더 보는 쪽이 안전하다(표시용 Visual 층이지만 맵이 크게 어긋나면 흐려야 한다).
- **계약 영향 없음**: reason 값은 기존 SAME_CALIBRATION·SMALL_CHANGE·LARGE_CHANGE·DIFFERENT_WORLD·UNKNOWN 그대로다(viewer_health·프론트 zod 파서 그대로). 위치·위험 판정에는 쓰지 않는다(규칙 1).
- **세팅 시작 가드**: REUSE 요청 때 현재 맵과 Tier 0 지금 보정의 판정이 MISMATCH 면 409 로 막는다. 맞춰도 맵과 어긋날 것이기 때문이다. 판정을 모르면(Tier 0 상태 없음) 막지 않는다. 워커의 World 다시 맞추기가 다시 확인한다.

## 6. 맵 보강(-607)과의 관계

다시 굽기(SUPPLEMENT_REBAKE)는 세션이 만든 맵(미리보기·굽기 입력 사진 BAKE_IMAGE·보정 문서)을 기준으로 한다. 재사용 세션(mode=REUSE)은 맵을 만들지 않았고 현재 맵은 다른 세션의 것이라 기준이 없다. 그래서 세션 조회 `supplement.can_upload=false`·`can_rebake=false`·`rebake_blocked_reason="REUSED_SCENE"` 이고, 업로드·다시 굽기 요청은 409 `SUPPLEMENT_REUSED_SCENE` 이다. 맵을 보강하려면 새 모델로 세팅한다. 재사용을 요청했다가 새 모델로 이어 간 세션(mode=NEW)은 NEW 와 같이 보강할 수 있다. 프론트 문구는 `REBAKE_BLOCKED_LABEL.REUSED_SCENE`.

## 7. 정한 수치와 근거

| 값 | 내용 | 근거 |
| --- | --- | --- |
| 진행률 65 → 85 → 100 | 재사용 채택의 JOINT_BA·READY·COMPLETED | NEW 와 같은 단계 값. 건너뛰어도 단조 |
| `ANCHORED_MAX_CENTER_M` 0.25 m, `ANCHORED_MAX_ROTATION_RAD` 10° | 같은 World 로 옮긴 보정의 Scene 정합 한도 | 5절 |
| `MAX_ANCHOR_HOPS` 8 | world_anchor 사슬 최대 단계 | 하루 한 번 재사용 기준 1주 이상. 순환 방지 |
| V55 | 마이그레이션 번호 | V53 -607, V54 -610(둘 다 머지됨) |

## 8. 버린 대안

1. **`base_scene_version` 입력**: 이전 모델은 언제나 현재 관제 맵 하나라(현장당 하나) 고를 것이 없다. 워커 기준 World 도 Tier 0 활성 보정이라 입력이 필요 없다.
2. **거부 시 실패**: 3절. 같은 몇 분을 다시 쓰게 된다.
3. **Tier 0 가 world_anchor 채택 보정을 같은 계보로 채택(new_world=False)**: 정합 판정이 자연스럽지만 Tier 0 배포·런타임 계보 규칙(회전 복구 bootstrap)을 함께 바꿔야 한다. 서버 판정만 바꾸면 Tier 0 는 그대로다.
4. **scene_compatibility 에 새 reason(예: WORLD_ANCHORED)**: viewer_health(I2)와 프론트 zod enum 을 함께 바꿔야 한다. 기존 값으로 충분하다.

## 9. 잔여 위험

- **재사용도 몇 분 걸린다**: 보정을 다시 하므로 배경 수집·MapAnything 초기 복원·Stage A 시간은 그대로다(맵 굽기만 빠진다). 후속 후보: 카메라 프레임만 이전 배경과 비교하는 빠른 "카메라 그대로" 확인(특징점 정합·호모그래피 잔차)으로 통과하면 이전 보정을 그대로 재사용.
- **바닥 감시 범위(-606)**: 재사용은 미리보기 단계가 없어 새 보정 버전의 `floor_coverage` 를 만들지 않는다. 관제 화면은 Tier 0 지금 보정과 같은 버전만 그리므로 재사용 뒤에는 바닥 겹침이 사라진다. 후속: (f) 의 MapAnything 점구름으로 바닥 감시 범위를 계산해 싣기.
- **재대여 사이 모드 변화**: 1차 대여가 새 모델로 이어 가 미리보기를 현재 맵으로 올린 뒤 실패하고, 2차 대여가 재사용으로 채택되면 현재 맵은 1차 미리보기로 남는다(서버는 2차 결과에서 미리보기 버전을 비우지만 이미 한 활성화는 되돌리지 않는다, 규칙 6). 드물고 정합 판정이 흐리게 보여 준다.
- **카메라 ID 순환·천천히 쌓이는 어긋남**: -584 잔여 위험 그대로(`docs/design/world-reanchor.md` 5절). 재사용은 맵을 바꾸지 않으므로 어긋남이 쌓이면 정합 판정이 흐림으로 알린다.

## 10. -615(세팅 완료, C17)가 할 일

- 세팅 완료에서 "같은 World 인가"(`sameWorld`)를 정할 때 `mode == REUSE`(계약 값 REUSE_SCENE) **또는** `world_anchor.accepted` 를 쓴다. 세션 조회의 `mode`·`world_anchor` 와 DB `setup_session.mode`·`world_anchor`(jsonb) 에 있다. NEW 세팅도 카메라가 그대로면 world_anchor 가 채택되어 같은 World 다.
- sameWorld 면 현재 관제 구역·TBM 배치를 그대로 쓰고, 재사용이면 새 Scene 활성화 단계가 없다(현재 맵 유지). 아니면 새 미리보기·정식 Scene 과 새로 발행한 구역을 쓴다.
- 오늘만 검사는 -610 의 `requireToday` 를 세팅 완료에서도 부른다(-610 설계, 세팅 시작은 -614 가 이미 부른다).

## 11. 코드·시험 위치

- 계약: `contracts/messages/site_setup.schema.json`(1.11.0), `job_request`·`job_callback`·`job_heartbeat` 설명, `contracts/examples/*site_setup_reuse*`, `contracts/examples/invalid/*site_setup_reuse*`·`*anchor_reason_mismatch*`·`*rest_mode_value*`, `ai/src/simai_ai/contracts/models.py`(생성).
- 워커: `ai/services/worker/site_setup.py`(`world_anchor_summary`, `_finish_reuse`, `__call__` 의 모드 분기), `ai/services/worker/client.py`(봉투 2.10.0·2.18.0). 시험 `ai/tests/services/worker/test_site_setup.py`(-614 묶음 4개).
- 서버: `SetupController`(mode), `SetupService`(create·retry·requireToday 호출·requireReusableScene·applySnapshotCore·조회), `SetupReuseMode`(REST ↔ 스냅샷 모드 검사), `SetupSupplementService`(REUSED_SCENE), `SceneCompatibility`(world_anchor 같은 World), `ErrorCode.Setup`, `V55__setup_session_reuse_mode.sql`. 시험 `SetupReuseModeIntegrationTest`, `SetupReuseModeTest`, `SceneCompatibilityTest`.
- 프론트: `front/apps/admin/src/features/setup/supplement.ts`(REUSED_SCENE 문구).

## 12. 명세 후속 (명세 정리 MR 에서)

- 4장 "작업 간 독립성과 계보 조회"·4.5: 세팅 모드(새 모델/재사용)와 재사용 때 Scene 버전을 새로 발급하지 않는다는 점, world_anchor 로 같은 World 를 유지한다는 점(-584 명세 차이와 함께).
- 12.6 세팅 REST: `mode`, 세션 응답 `mode`·`requested_mode`·`reuse_fallback_reason`·`world_anchor`, 새 오류 코드.
