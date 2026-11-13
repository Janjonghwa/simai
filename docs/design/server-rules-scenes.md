# EC2 규칙 버전·현재 Scene 조회

S15P31A101-264. 명세 3.7(정적 배경), 4.5(Scene 은 CALIBRATED 뒤에만), 7.1(R1~R10), 11.4-1(규칙 판은 새 판을 더함), 12.6(`GET /api/rules`·
`GET /api/scenes/current`, 열람), FR-3·113. 코드는 `server/.../domain/rule/`(`RuleViews`·`RuleCatalog`·`RuleController`),
`server/.../domain/scene/`(`SceneService`·`SceneController`). 시험은 `RuleSceneIntegrationTest`.
규칙 판의 원천은 ai 등록부(`ai/src/simai_ai/risk/rule_catalog.py` → `R__rule_versions.sql`), Scene 생성은 S15P31A101-137(M3).

## 응답

`GET /api/rules`

```json
{
  "rule_profile": "OPERATION",
  "config_version": "cfg-3",
  "pending_config_version": null,
  "rules": [
    { "rule_id": "R1", "name": "위험 구역 진입", "stage": "MVP", "active": true,
      "current": { "rule_id": "R1", "rule_version": 1, "profile": "OPERATION", "thresholds": { "margin": 0.3, "r6": { } },
                   "default_severity": null, "legal_basis": null, "reads": ["tracks.observation_state"],
                   "valid_from_ms": 1760342400000, "valid_to_ms": null } },
    { "rule_id": "R6", "name": "위험 구역 내 관측 상실", "stage": "MVP", "active": true, "current": null, "applied_with": ["R1"] },
    { "rule_id": "R8", "name": "장비 근접", "stage": "EXTENSION", "active": false, "current": null }
  ],
  "versions": [ { "rule_id": "R1", "rule_version": 1, "profile": "OPERATION", "valid_from_ms": 1760342400000, "valid_to_ms": null } ]
}
```

`GET /api/scenes/current`

```json
{ "scene_version": "scene-3", "state": "COMMITTED", "default": false, "calibration_version": "cal-8",
  "committed_at_ms": 1760342400000, "quality": { }, "gltf_url": "https://...", "gltf_expires_at_ms": 1760342700000 }
```

## 명세가 비워 둔 곳에서 정한 것

| 항목 | 정한 값 | 근거 |
|---|---|---|
| 현재 규칙 | Tier 0 가 적용했다고 알린(`config_bundle.applied_at`, config_ack APPLIED) 이 현장의 마지막 묶음이 실은 판(`config_bundle_item` RULE)과 그 묶음의 `rule_profile`. 발행만 하고 아직 적용 전이거나 거부된 더 새 묶음은 `pending_config_version` 으로만 알린다. 적용된 묶음이 없으면 현장 규칙 기준(`site.rule_profile`, 기본 교실 시험, S15P31A101-669)의 유효 판(`RuleSelection`, 묶음을 만들 때와 같은 질의) | 관제 화면이 "지금 현장에서 판정에 쓰는 규칙"을 보게. 거부된 묶음의 규칙을 현재로 보이면 판정 기준을 잘못 안내한다 |
| 규칙 목록 | R1~R10 을 7.1 표 순서로 모두 싣고 이름·단계(MVP·EXTENSION)·활성 여부를 준다. 판이 없는 규칙은 `active=false`, `current=null`. 7.1 표 밖의 판이 생기면 이름 없이 뒤에 싣는다 | 작업 "R1~R10 활성 여부·임계값". 확장 규칙(R8~R10)도 비활성으로 보인다 |
| R6 | 자기 판이 없다. 감싼 규칙의 `thresholds.r6` 가 설정 객체이면 그 규칙으로 적용되므로 `applied_with` 에 싣고, 하나라도 있으면 활성(`r6: null` 은 꺼진 것). 등록부가 R6 자기 판을 두면 그 판도 `current` 로 싣는다 | ai 등록부가 R6 값을 감싼 규칙에 함께 적는다(rule_catalog.py) |
| 적용 기간 | `versions` 에 모든 판(닫힌 판·교실 시험 판 포함)의 `valid_from_ms`·`valid_to_ms` 만 싣는다(값 없이) | FR-113 "적용 기간 조회". 값까지 모두 실으면 화면에 쓰지 않는 무게가 붙는다 |
| 권한 | 두 API 모두 로그인한 사용자(열람 이상). 토큰 없음 401 | 12.6 "열람" |
| 현재 Scene | COMMITTED 이고 `valid_to` 가 없는 가장 최근(`committed_at`) Scene. `viewer_health.scene.committed_version` 도 같은 질의(`SceneService.CURRENT_COMMITTED`)를 쓴다 | 화면의 Scene 불일치 경고(FR-12)와 같은 기준 |
| glTF 주소 | 5분짜리 GET 서명 주소(`ObjectStorage.signGet`, -69). `gltf_key` 가 없거나 저장소를 쓸 수 없으면(미설정·자격 증명 없음) 주소만 null 로 두고 Scene 정보는 준다(503 로 막지 않는다) | 객체 저장소는 비공개, 서명 주소로만 읽는다(14장) |
| 기본 Scene | COMMITTED Scene 이 없으면 설정 `simai.scene.default-gltf-key`(`SIMAI_SCENE_DEFAULT_GLTF_KEY`)의 객체를 `state=DEFAULT`, `default=true`, 버전 `simai.scene.default-version`(기본 `scene-demo`)으로 준다. 설정도 없으면 404 `SCENE_NOT_FOUND` | 이슈 "Scene 생성 전에는 시연용 기본 Scene 을 등록해 돌려준다". DB 행(`scene_version`)으로 두면 보정 버전 FK(4.5 CALIBRATED 뒤에만)가 필요해 설정으로 등록한다. 기본 Scene 은 보정·품질 근거가 없어 그 칸은 null |
| 마이그레이션 | 없음 | 기존 표(`rule_version`·`config_bundle_item`·`scene_version`)만 읽는다 |

## 시험

`RuleSceneIntegrationTest`(실제 포트): 운영 묶음의 판·임계값·적용 기간·R3 비활성·R6 감싼 규칙·확장 규칙, 교실 시험 묶음의 1001 판과 R6 비활성,
적용한 묶음이 현재이고 발행만 한 더 새 묶음은 대기, `r6: null` 이면 R6 비활성·R6 자기 판은 current, 저장소를 쓸 수 없으면 주소만 null, 토큰 없음 401, COMMITTED Scene 과 glTF 서명 주소(만료 5분), 끝난 Scene 뒤 기본 Scene, 기본 Scene 도 없으면 `SCENE_NOT_FOUND`.

## 남은 일

- S15P31A101-137(Scene 생성·검증·커밋, M3)이 `scene_version` 을 채우면 이 조회가 그대로 쓴다. 그때 응답 칸(`quality` 모양)을 맞춘다.
- 운영에서 시연용 기본 glTF 를 버킷에 올리고 `SIMAI_SCENE_DEFAULT_GLTF_KEY` 를 정한다(배포 작업).
- 관제 앱이 배경 Scene 을 불러오는 코드는 아직 없다(프론트).
