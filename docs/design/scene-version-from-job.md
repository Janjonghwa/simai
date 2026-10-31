# static_reconstruct 결과와 활성 Scene (S15P31A101-365, S15P31A101-380, S15P31A101-381)

## 범위와 근거

명세 4.5(14차 개정: 워커 Scene 도 PROVISIONAL 관리자 확인), 11.4(Scene 버전·출처·유효 기간), 12.5(I5 제출), 12.6(Scene 조회·관리), 13.1~13.2(배경·버전 불일치)를 따른다. 계약 정본은 `experiments/scene_output` 2.1.0과 `messages/job_callback` 2.3.0이다. 계약은 바꾸지 않는다. `server/CONVENTION.md`는 이 작업 기준 저장소에 없다.

지시서 작성 시점과 달리 -322의 직접 glb 등록·확정 API가 이미 있다. 기존 API를 유지하고 확정 시 같은 활성화 서비스를 쓰게 한다. 명세 11·13장은 성공 결과의 자동 활성화를 정하지 않았고 12.6에는 관리자 확정 경로가 있다. 따라서 성공 결과는 **CANDIDATE로만 추가**하고, 관리자 확인 뒤 활성화한다. 워커의 `state: COMMITTED`도 자동 활성화 권한으로 취급하지 않는다.

## 저장과 전환

- `SceneJobListener`: `JobFinished` 성공 이벤트를 제출 트랜잭션 안에서 받는다. 별도로 `scene_output` 계약을 검사한다(I5 callback 자체는 static 결과를 열린 객체로 받음). 입력 작업과 결과의 보정 버전을 대조하고 보정 FK로 현장을 정한다. 결과의 현장·출처 보정 버전은 계약의 선택 필드이므로 있을 때만 대조한다. 잘못된 결과는 제출 전체를 롤백해 올바른 결과를 재제출할 수 있다.
- `scene_version`: glb 키·quality·input_provenance·is_upper_bound·job_id와 `scene_output` 원문을 저장한다. 원문에는 자산 순서·주소·해시·크기·surface_values·ratios·method/후처리·품질/정합 수치·재현 근거가 있다. 측정하지 않은 값은 채우지 않는다. 별도 정합 판정/임계값을 만들어 검증됐다고 주장하지 않는다.
- 워커 glb와 출처 지도는 서버 버킷의 `static-3d/{job_id}/{제출 lease_id}/{한 단계 이름}`만 허용한다. 다른 작업·이전 대여·다른 버킷의 객체를 열람자에게 서명하지 않는다. `volume://` 등 로컬 산출물은 서버 배경으로 제공할 수 없어 이 경로에서는 거절한다. 워커 객체 서명 API로 업로드한 `s3://` 주소를 제출해야 한다.
- 대여 재전송은 기존 `JobLeaseService`가 DUPLICATE로 처리한다. `scene_version.job_id` 부분 유일 인덱스와 INSERT의 충돌 처리가 같은 작업의 후보 추가도 막는다. 다른 작업이 같은 Scene ID를 내면 기존 내용을 보존하고 충돌로 거절한다.
- `SceneActivationService`: API와 CLI의 활성 전환을 기존 advisory lock으로 직렬화한다. 후보 또는 이미 확정된 버전을 같은 현장에서 선택한다. REJECTED·VALIDATING·상한 입력은 활성화하지 않는다. 워커 후보와 직접 등록 모두 `PROVISIONAL`·`CALIBRATED` 보정에서 관리자 확인으로 활성화를 허용하고(`SceneActivationService.CALIBRATION_STATES`), `UNCALIBRATED`·`DRIFTED`는 `SCENE_CALIBRATION_INVALID`로 거절한다(S15P31A101-380). glb·출처 지도 객체 존재와 보고된 크기(있을 때)를 HEAD로 확인한다. 객체 바이트의 SHA-256 재계산은 하지 않는다.
- `scene_activation`: 대상·직전 버전·UTC 시각·관리자 식별자·사유를 추가한다. 같은 활성 버전 재요청은 새 이력을 만들지 않는다. 되돌리기도 `activate`로 예전 버전을 지정하고 새 이력을 추가한다. Scene 원문과 최초 committed_at/valid_from은 재활성화 시 바꾸지 않는다.
- 기존 API의 최초 확정 시 valid_from을 기록하고 이전 열린 COMMITTED의 valid_to를 닫는 동작은 유지한다. **재활성화 이후 기간의 정본은 활성 이력**이다. 시각 T의 배경은 해당 현장의 `activated_at <= T` 중 마지막 activation_id의 버전이다(동일 ms에는 ID 순서). 최초 활성화 이전 구간은 기존 Scene 유효 기간을 사용한다. `valid_to IS NULL`만으로 현재 배경을 고르는 새 코드를 만들면 안 된다.

## 조회와 뷰어 반영

`GET /api/scenes/current`는 매 요청 DB의 마지막 활성 이력을 읽는다. 이력이 없으면 기존의 열린 COMMITTED, 그것도 없으면 `simai.scene.default-gltf-key` 설정으로 폴백한다. 설정도 없으면 기존대로 404다. 활성(`COMMITTED`) Scene 응답에는 보정 버전의 현재 보정 상태 `calibration_state`(`PROVISIONAL`·`CALIBRATED` 등)를 함께 싣는다(모르면 싣지 않으며, `DEFAULT` 기본 Scene에는 싣지 않는다, S15P31A101-380). MVP의 현재 현장 조회는 여전히 현장 하나를 전제로 하며, CLI 전환/이력은 `--site`로 현장을 명시한다.

출처 지도가 있으면 `surface_source_map_urls`를 재질 순서의 서명 URL 배열로 보낸다(-360 뷰어가 허용하는 배열 형식). glTF 주소와 같은 저장소 기본 서명 수명인 5분을 사용한다. 지도가 없거나 저장소를 쓸 수 없거나 한 장이라도 서명 실패하면 필드를 생략한다. 일부 지도만 보내 재질 인덱스를 바꾸지 않는다. surface_values·ratios는 원문에 보존하며 여기서 관측 비율이나 지도 픽셀을 바꾸지 않는다.

`ViewerHealthService`가 같은 현재 Scene 질의를 사용하고, 기존 `HealthPublisher`가 `/topic/health`에 DB 변경을 1초 주기로 반영한다. 별도 Scene 자산 갱신 토픽은 없다. -381부터 열린 뷰어는 활성 Scene 변경을 받으면 현재 자산을 자동 재조회한다(반복 실패는 10초 간격). EC2는 기존 인증 I1의 `scene_reference`를 1초 주기로 보내 Tier 0의 다음 `world_state.scene_version`을 갱신한다. 서버 프로세스 재기동이나 수동 새로고침이 필요 없다.

배경 정합은 서버 `SceneCompatibility`가 Scene 생성 보정 대비 누적 변화로 판정한다. 같은 World 계보에서 카메라 중심 최대 이동 0.10 m 이하·최대 회전 2° 이하이면 VERIFIED, 문턱 초과·근거 부족이면 DIMMED + 재생성 필요, 다른 World이면 MISMATCH(숨김)이다. REST와 health가 같은 판정을 Scene·현재 프레임 보정 버전에 결속해 보내며, 임시 보정 기준 배경도 표시한다. 계보 전달 경로·수식·문턱 근거와 남은 현장 검증은 [Scene–보정 계보 정합](scene-calibration-lineage.md)에 기록한다. 이 판정은 보기용이며 Tier 0 위치·위험 판정·알림에 사용하지 않는다.

## 9차 세션 명령 예시

아래 `simai.jar`는 배포된 서버 jar를 뜻한다. 서버와 같은 DB/객체 저장소 환경을 주입한 관리 터미널에서 실행한다(비밀값은 명령에 적지 않는다). 오프라인 배치 작업은 LIVE 관제 중 실행하지 않는다. 세션 전에 배포를 완료하며 세션 중 merge/자동 재기동하지 않는다.

```sh
# static-input.json에는 워커가 받는 카메라 프레임 주소·보정 버전 등을 넣는다.
java -jar simai.jar admin job enqueue --type static_reconstruct --input-file static-input.json --timeout-ms 600000 --max-attempts 2

# 워커가 제출한 후보의 scene_version, job_id, quality와 최근 전환 이력을 확인한다.
java -jar simai.jar admin scene list --site classroom-1

# 저장된 정합·출처 근거와 미리보기를 확인한 뒤 명시적으로 활성화한다.
java -jar simai.jar admin scene activate --site classroom-1 --version scene-session9 --actor lead --reason '9차 세션 배경 확인'

# 열람 권한으로 GET /api/scenes/current의 버전·glTF·지도 주소와 열린 뷰어의 자동 배경 전환을 확인한다.
# 문제가 있으면 목록에서 직전 버전을 골라 같은 명령으로 되돌린다.
java -jar simai.jar admin scene activate --site classroom-1 --version scene-session8 --actor lead --reason '9차 배경 정합 문제로 되돌림'
```

CLI는 기존 admin 명령처럼 서버 OS/실행 환경 접근 권한을 전제로 한다. `--actor`는 그 운영자가 입력한 감사용 식별자이며 사용자 JWT 인증을 대신하지 않는다. 직접 등록 API는 기존 ADMIN JWT 권한을 유지한다.

## 코드·시험과 남은 위험

코드는 `server/.../domain/scene/`, `global/command/SceneCommand.java`, 새 Flyway `V17__scene_job_activation.sql`에 있다. 시험은 `WorkerJobIntegrationTest`의 실제 I5 제출 → DB 후보/근거 → CLI·서비스 활성화(PROVISIONAL·CALIBRATED 허용)·되돌림 → HTTP 조회(`calibration_state`, `surface_source_map_urls`)와 health, 잘못된 출력 경로/보정·불완전 계약 롤백, 상한·상태(UNCALIBRATED·DRIFTED)·누락 자산 거절 및 기존 `SiteSceneIntegrationTest`, `RuleSceneIntegrationTest`다. DB 없는 응답 경계(`calibration_state` 포함·생략, DEFAULT 미포함)는 `SceneServiceTest` 5개가 검증한다.

G4 수치에 대한 새 자동 합격 판정은 범위 밖이다. 상한 입력 이외의 품질 판단은 관리자가 원문 지표로 확인한다. 실제 S3와 GPU 워커 업로드·브라우저의 DIMMED 표시는 9차 세션에서 확인해야 한다. 출처 지도 없는 과거 배경은 기존 표현을 유지한다. 객체 저장소의 변경/삭제 뒤 활성 버전이 깨질 수 있으며, 활성화 시 HEAD는 이후 객체 불변성을 보장하지 않는다. 재활성화 기간을 읽는 리플레이 확장은 위 활성 이력을 사용해야 한다.

-380 Owner 검증에서는 `compileJava compileTestJava`와 `SceneServiceTest`(5개), 로컬 `simai-testdb`(localhost:5432) 기반 통합 시험 `WorkerJobIntegrationTest`(14개), `SiteSceneIntegrationTest`(4개), `RuleSceneIntegrationTest`(8개), `ConfigBundleIntegrationTest`(10개) 총 41개가 모두 통과했다.
