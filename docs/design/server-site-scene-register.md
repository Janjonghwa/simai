# EC2 현장 등록·수정과 Scene 등록·확정

S15P31A101-322. 첫 운영 배포 초기화(S15P31A101-319, [ec2-deploy.md](ec2-deploy.md) "운영 현장 시드")의 2단계(site)·4단계(Scene) 경로다.
명세 v1.3 수정 37(12.6), 3.7·4.5(Scene), 11.1(site). 코드는 `server/.../domain/site/SiteController`, `domain/scene/SceneAdminController`,
업로드 목적 `domain/upload`(`UploadPurpose.SCENE_GLTF`, `Requester.Kind.ADMIN`), V16. 시험은 `SiteSceneIntegrationTest`.
워커가 복원·검증해 커밋하는 흐름(`POST /api/scenes/rebuild`)은 S15P31A101-137 이고 같은 `scene_version` 상태를 쓴다.

## API

| 메서드 | 경로 | 권한 | 하는 일 |
|---|---|---|---|
| GET | `/api/sites/current` | 로그인 | 현장 하나(`site_id`·`name`·`world_origin`·`established_calibration_version`·`created_at_ms`) |
| POST | `/api/sites` | 관리자 | `{site_id, name, world_origin?}` 등록. 현장이 이미 있으면 409 `SITE_EXISTS` |
| PUT | `/api/sites/current` | 관리자 | 보낸 칸만 고친다(`name`, `world_origin`(null 이면 비움), `site_id`) |
| POST | `/api/uploads/sign` | 관리자 | `purpose: SCENE_GLTF`, `model/gltf-binary`, 512 MiB 이하 → 키 `scenes/{uuid}.glb` 와 PUT 서명 주소 |
| POST | `/api/scenes` | 관리자 | `{scene_version, calibration_version, gltf_key, quality?}` → 201 CANDIDATE |
| POST | `/api/scenes/{v}/commit` | 관리자 | CANDIDATE → COMMITTED. 확정 직전에 보정 상태와 glb 실재·크기를 다시 본다. 이전 현재 Scene 은 `valid_to` 로 닫는다 |
| POST | `/api/scenes/{v}/reject` | 관리자 | `{reason}` → REJECTED |
| GET | `/api/scenes/{v}` | 관리자 | 상태·보정·품질·확정 시각·유효 기간·등록자(객체 키 없음) |

확정하면 `GET /api/scenes/current` 가 곧바로 그 Scene(`default=false`)을 준다. 그 뒤에 시연용 기본 Scene 설정(`SIMAI_SCENE_DEFAULT_GLTF_KEY`)을 비운다(시드 8단계).

## 명세가 비워 둔 곳에서 정한 것

| 항목 | 정한 값 | 근거 |
|---|---|---|
| 현장 수 | 하나(등록은 현장이 없을 때만) | MVP 는 현장 하나(16장), 서버 전체가 `SiteLookup` 의 한 행에 붙는다 |
| site_id 바꾸기 | `PUT /api/sites/current` 의 `site_id`. 구역·작업면·구성 묶음·보정·실측 정물·평면도·Scene·BEV 프레임 중 하나라도 이 현장에 붙어 있으면 409 `SITE_IN_USE`. 카메라는 함께 옮김 | Flyway V2 가 개발 현장 `classroom-1` 을 늘 시드한다. 운영 ID(보정 문서 `coordinate_frame.site_id` 와 같아야 함)로 바꾸는 일은 초기화 직후 보정을 올리기 전 한 번이다. 기록이 붙은 뒤 바꾸면 그 기록이 가리키는 현장이 갈린다 |
| site_id 형식 | 소문자·숫자·`-` 1~63자 | `classroom-1` 과 같은 꼴. 객체 키·구성 묶음 경로에 들어간다 |
| world_origin | 객체 또는 null, 모양은 검사하지 않음 | 원점·축 근거는 보정(4.1, `world_axes_source`)이 정한다 |
| glb 업로드 | 업로드 목적 `SCENE_GLTF`(관리자만, 키는 서버가 정함), 등록이 발급을 쓰인 것으로 표시 | 아무 키나 받으면 남의 객체·없는 객체를 Scene 으로 걸 수 있다. 같은 glb 로 두 Scene 을 만들지 않는다 |
| 업로드 주체 | 업로드 체인에 관리자 역할을 더하고 Java 에서는 `Requester.Kind.ADMIN`, 발급 기록에는 사람(`requester USER`, `requested_by_user`) | 조치 사진(반장만)과 Scene glTF(관리자만)를 가른다. DB CHECK 는 바꾸지 않는다 |
| 크기 | 512 MiB | 교실 텍스처 메시(EXP-01)는 수십~수백 MB |
| 보정 상태 | PROVISIONAL·CALIBRATED 만(UNCALIBRATED·DRIFTED·없는 버전 409 `SCENE_CALIBRATION_INVALID`) | 4.5 는 생성을 CALIBRATED 뒤로 묶지만, 직접 등록은 교실 PROVISIONAL 보정(-293)에도 쓴다. Lead 확인 필요 |
| scene_version | 관리자가 정함, 소문자·숫자·`.`·`_`·`-` 1~63자, `current` 불가, 겹치면 409 | 계약 `version_id` 는 비어 있지만 않으면 된다. `GET /api/scenes/current` 와 경로가 겹친다 |
| 확정 | 전역 잠금(advisory) 하나로 차례로. 현재 COMMITTED(`valid_to` 없음)를 그 시각으로 닫고 새 Scene 에 `committed_at`·`valid_from` | 현재 Scene 은 하나여야 한다. 닫은 Scene 은 리플레이 확장(Q4 그 시각의 Scene)에 쓴다 |
| 거절 | 사유 1~500자 필수, 확정·거절된 Scene 은 다시 바꾸지 않음(409) | 기록은 고치지 않는다(AGENTS 6) |
| 관리자가 다루는 상태 | CANDIDATE 만 확정·거절. VALIDATING 은 건드리지 않음 | VALIDATING 은 워커 검증(-137)이 쥔 상태다. -137 이 COMMITTED 로 바꿀 때도 같은 잠금(`simai.scene.commit`)을 써야 현재 Scene 이 하나로 남는다 |
| 확정 전 재검사 | 보정이 아직 PROVISIONAL·CALIBRATED 인지, glb 가 저장소에 있고(HEAD) 발급 크기와 같은지. 아니면 409 `SCENE_CALIBRATION_INVALID`·`SCENE_GLTF_MISSING` | 확정하면 화면 배경 전체가 이 Scene 이 되고 기본 Scene 으로 물러나지 않는다. 서명 주소만 받고 올리지 않은 glb 를 걸면 배경이 깨진다. 서명이 크기·해시를 묶으므로 크기가 같으면 그 내용이다 |
| PUT 본문 | `site_id`·`name`·`world_origin` 중 하나는 있어야 함(아니면 400) | 오타 본문에 아무것도 바꾸지 않은 채 200 을 주지 않는다 |
| world_origin 수정 | 기록이 붙은 뒤에도 고칠 수 있음 | 원점·축 근거의 설명 칸이다. 좌표 자체는 보정 문서가 정하므로 보정·Scene 을 바꾸지 않는다 |
| is_upper_bound | false | 직접 등록 Scene 은 실제 현장 복원물이다. SIM·정답 입력이면 워커 흐름(-137)이 출처와 함께 채운다 |

## 시험

`SiteSceneIntegrationTest`(실제 포트): 현장 조회(VIEWER), 이름·원점 근거 수정·null 로 비우기, 등록은 현장이 있으면 409, 권한 403,
잘못된 값 400, 보정이 붙은 현장의 site_id 바꾸기 409, 비었으면 카메라와 함께 옮기고 되돌리기(다른 시험 기록이 남아 있으면 건너뜀),
관리자 서명 → Scene 등록 → 확정 → 현재 Scene(저장소는 가짜, HEAD 크기 1000), 두 번째 확정이 앞 Scene 을 닫음·현재 하나, 확정 뒤 재확정 409, 같은 glb 재사용·같은 버전 거부,
거절과 사유, glb 없음·크기 다름·등록 뒤 DRIFTED 면 확정 409(CANDIDATE 유지), 발급 없는 키·UNCALIBRATED·없는 보정·잘못된 버전, 아는 칸 없는 PUT 400, 반장·열람 403, SCENE_GLTF 는 관리자만·조치 사진은 반장만.
`UploadSignIntegrationTest` 는 그대로 통과(관리자 조치 사진 403 유지).

## 남은 일

- 실측 정물(`scale_reference`, `GET·POST /api/scale-references`, FR-135) 등록은 따로 한다(시드 2단계의 축척 근거).
- 워커 생성 Scene(-137)이 들어오면 품질 검증(VALIDATING)과 보정 버전 변경 시 재생성 표시를 이 상태 흐름에 붙인다.
- 직접 등록을 PROVISIONAL 보정에도 받는 것은 Lead 확인이 필요하다.
- 관제 앱 관리자 화면(현장·Scene 등록)은 프론트 몫이다.
- 쓰이지 않은 `SCENE_GLTF` 발급과 그 객체를 지우는 작업이 없다(올리고 등록하지 않은 glb). 클립·영상 조각 삭제 작업처럼 붙인다.
- 등록자와 서명 발급자가 같은 관리자인지는 보지 않는다(관리자끼리라 위험이 작다).
- `scene_version` 에 '현재 하나'를 막는 유일 색인이 없다. 지금은 확정 경로 하나가 잠금으로 지킨다.
