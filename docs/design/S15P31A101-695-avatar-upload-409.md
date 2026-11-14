# 아바타 자산 업로드 서명 HTTP 409 반복 진단 및 버전 전진 복구 (S15P31A101-695)

15차 GPU 세션(`2026-11-13`, `evidence-695/logs/avatar.log`)에서 아바타 서비스가 `world_id=w-mwen4l48-0004`(23회)와 `world_id=w-mwennxhq-0031`(72회)에 대해 `epoch=1794554024727 version=0`으로 `/api/uploads/sign`을 반복 호출하며 총 95회 회차 실패(로그 경고 190줄)를 낸 원인을 코드와 로그로 확정하고, AGENTS 규칙 6(덮어쓰기 금지)을 지키면서 다음 `appearance_version`으로 전진하도록 수정한다.

## 관련 명세 절

| 절 | 내용 | 이 문서에서 |
|---|---|---|
| 12.6 · 14 | `POST /api/uploads/sign` 목적별 객체 키 발급 및 `upload_grant` 기록 | 동일 `(world_id, stream_epoch, appearance_version)`에 같은 해시·크기 재요청은 멱등 성공, 다른 내용 요청은 409(`UPLOAD_KEY_CONFLICT`) 유지 및 서버 경고 로그 추가 |
| 20.3 · 20.4 | EXP-02 상시 아바타 스트림(`avatar_asset`·`avatar_pose_batch`) 버전 단조 증가 불변식 | 같은 `stream_epoch` 내 트랙 재진입 시 버전 단조 증가 유지 및 409 충돌 시 `pending_version` 전진 |
| AGENTS 규칙 6 | 이미 발급·기록된 버전 경로를 다른 내용으로 덮어쓰지 않고 새 버전을 추가 | 서버의 `AVATAR_ASSET` 덮어쓰기 금지는 그대로 유지하고 워커가 새 `appearance_version`을 발급받아 진행 |

## 코드·시험 위치

| 구분 | 경로 | 역할 |
|---|---|---|
| 예외 판별 | `ai/src/simai_ai/hmr/diagnostics.py` | 업로드 서명 단계의 키 충돌(`HTTP 409`) 판별 함수 `is_upload_conflict` 추가 |
| 아바타 업로드 전송 | `ai/services/avatar/transport.py` | `/api/uploads/sign` 비정상 응답 시 `ValueError`에 `status` 속성을 함께 부여 |
| 아바타 포즈·자산 루프 | `ai/src/simai_ai/hmr/stream.py` | `stream_epoch` 동안 `world_id`별 최고 예약 버전(`_high_versions`)·마지막 확정 버전(`_last_versions`) 보존, GLB 업로드 409 시 이미 구운 `pending_glb`를 유지한 채 `pending_version` 전진 |
| 외형 누적 파이프라인 | `ai/src/simai_ai/hmr/appearance_stream.py`, `ai/src/simai_ai/hmr/appearance_worker.py` | PNG 업로드 409 시 대기 중 외형 자산의 `appearance_version` 전진 및 `previous_appearance_version` 순서 보장 |
| 업로드 서명 서비스 | `server/src/main/java/com/ssafy/simai/domain/upload/UploadSignService.java` | `upload_grant` 충돌로 `UPLOAD_KEY_CONFLICT`(409) 반환 시 원인(`purpose`, `key`, `requester`, `sizeBytes`, `sha256`) 경고 로그 기록 |
| 재현·통합 시험 | `server/src/test/java/com/ssafy/simai/domain/upload/UploadSignIntegrationTest.java`, `ai/tests/services/avatar/test_transport.py`, `ai/tests/hmr/test_avatar_loop.py` | 같은 해시 멱등 재서명(만료 후 포함), 다른 해시·크기 409 거부 및 다음 버전 발급, 워커 409 버전 전진 및 `LOST` 재진입 단조 증가 검증 |

## 원인 확정 및 근거 (`file:line`)

### 1. 세션 15 로그 타임라인 근거 (`evidence-695/logs/avatar.log`)
- `w-mwen4l48-0004`:
  - `avatar.log:1975` (`07:29:54`): `epoch=1794554024727 version=0 bytes=1344488` 업로드 **성공**.
  - `avatar.log:2249` (`07:32:36`): 활성 트랙이 사라져 `people=0`이 되면서 메모리 상태에서 제거됨.
  - `avatar.log:6163~6251` (`07:42:08~07:42:14`): 같은 `w-mwen4l48-0004`가 다시 활성화된 뒤 5프레임 초기 체형을 새로 구워 `version=0`으로 서명을 요청했으나 23회 연속 `HTTP=409` 실패.
- `w-mwennxhq-0031`:
  - `avatar.log:13417~14714` (`08:03:07~08:06:16`): `epoch=1794554024727`에서 `version=0`(`mesh.glb`, `bytes=1344480`), `version=1`(PNG), `version=2`(`mesh.glb`, `bytes=1344488`), `version=3~5`(PNG)까지 정상 업로드 **성공**.
  - `avatar.log:16837` (`08:11:36`): `people=0`이 되면서 메모리 상태에서 제거됨.
  - `avatar.log:18864~19149` (`08:16:33~08:17:39`): 같은 `w-mwennxhq-0031`가 다시 등장한 뒤 `version=0`부터 서명을 다시 요청해 72회 연속 `HTTP=409` 실패.

### 2. 왜 같은 `world_id`가 `version=0`부터 다시 요청했는가 (`ai/src/simai_ai/hmr/stream.py`)
- `AvatarLoop._step`의 `self.people = {wid: st for wid, st in self.people.items() if wid in active}`(`stream.py:348`, 수정 전)는 한 프레임이라도 `track_lifecycle == "LOST"`이거나 트랙 목록에서 빠진 `world_id`의 `PersonState`를 즉시 삭제했다.
- 이후 Tier 0가 같은 `stream_epoch` 안에서 동일 `world_id`를 다시 내보내면(트랙 복구 또는 재등장) `PersonState(rev, ...)`(`stream.py:360`, 수정 전)가 `version=-1, high_version=-1`로 새로 생성되어, 5프레임(`initial_freeze_frames=5`) 뒤 새로 구운 GLB(이전 `v0`과 `sha256`·`size_bytes`가 다름)를 다시 `version=0`으로 예약했다(`_reserve_version`, `stream.py:226-229`).

### 3. 왜 서버가 409(`UPLOAD_KEY_CONFLICT`)를 반환했는가 (`UploadSignService.java`)
- `UploadSignService.avatarKey`(`UploadSignService.java:171-192`)는 `(world_id, stream_epoch, appearance_version)`을 `avatars/<world_id>/e<epoch>/v<version>/mesh.glb`로 고정한다.
- `UploadSignService.sign`(`UploadSignService.java:102-123`)의 `INSERT INTO upload_grant ... ON CONFLICT (object_key) DO UPDATE`는 `AVATAR_ASSET`에 대해:
  - 같은 작업자이고 `(content_type, size_bytes, sha256)`가 모두 같을 때만 `issued_at`·`expires_at`을 갱신해 멱등하게 재서명한다(`UploadSignService.java:114-116`).
  - `sha256` 또는 `size_bytes`가 다르면 `upload_grant.purpose NOT IN ('AVATAR_ASSET', 'STATIC_RECONSTRUCT_INPUT')` 조건(`UploadSignService.java:117-118`)에 의해 만료 뒤에도 덮어쓰기를 거부하고 `UPLOAD_KEY_CONFLICT`(HTTP 409)를 던진다(AGENTS 규칙 6 준수).
- 이때 `granted.isEmpty()` 분기에 서버 로그 출력이 없어 서버 로그에는 409 사유가 남지 않았다.

### 4. 왜 `version=0`에서 빠져나오지 못하고 무한 반복했는가 (`stream.py`, `transport.py`)
- `AvatarLoop._step`(`stream.py:439-443`, 수정 전)은 일시적 네트워크 오류 시 같은 바이트를 같은 버전으로 재시도하기 위해 `state.pending_glb`와 `state.pending_version = 0`을 먼저 저장한 뒤 `self._counted_upload(...)`를 호출한다.
- `AvatarUpload._upload`(`transport.py:54-55`)가 서명 409에 대해 `ValueError("아바타 업로드 서명 실패 HTTP=409")`를 던지면 `_step`이 예외로 중단되면서 `state.pending_version = 0`과 새 GLB(`state.pending_glb`)가 그대로 남았다.
- 그 결과 다음 회차마다 `state.pending_glb is not None`이고 `state.pending_version == 0`인 상태로 같은 충돌 요청을 반복하며 해당 회차 전체의 포즈 송신까지 가로막았다.

## 수정 내용

1. **`stream_epoch` 내 `world_id`별 버전 단조 증가 보존 (`ai/src/simai_ai/hmr/stream.py`)**:
   - `AvatarLoop`에 `self._high_versions`와 `self._last_versions` 사전을 두어 트랙이 `LOST`/비활성으로 `self.people`에서 제거되더라도 같은 `stream_epoch` 동안 해당 `world_id`가 예약·확정한 최고 버전을 기억한다.
   - 같은 `world_id`가 다시 `active`에 들어오면 `PersonState(..., high_version=self._high_versions.get(wid, -1))`로 초기화해 `version=0`이 아니라 직전 최고 버전 다음 번호를 예약하고, `previous_appearance_version`도 마지막 확정 버전(`_previous_version`)으로 채워 계약 불변식(`0 <= previous_appearance_version < appearance_version`)을 지킨다.
2. **HTTP 409 키 충돌 시 이미 구운 자산을 유지한 채 다음 버전으로 전진 (`stream.py`, `appearance_stream.py`, `diagnostics.py`, `transport.py`)**:
   - `AvatarUpload._upload`가 서명 실패 `ValueError`에 `error.status = response.status`를 부착하고, `is_upload_conflict(error)`가 `HTTP 409` 여부를 판별한다.
   - 일시적 네트워크·저장소 PUT 오류(`OSError` 등)는 기존처럼 같은 `pending_version`과 같은 바이트로 재시도해 서버의 멱등 재서명을 받는다.
   - 반면 `is_upload_conflict(error)`(HTTP 409)이면 `state.pending_glb`(또는 외형 `pending`)를 다시 굽지 않고 그대로 둔 채 `state.pending_version = self._reserve_version(state, person.world_id)`로 다음 버전을 예약해 다음 회차에 새 버전 경로(`/v{next}/...`)로 즉시 업로드한다.
3. **서버 409 진단 로그 추가 (`UploadSignService.java`)**:
   - `granted.isEmpty()`로 `UPLOAD_KEY_CONFLICT`를 던지기 직전 `purpose`, `key`, `requester`, `sizeBytes`, `sha256`을 `WARN` 로그로 남겨 운영 중 충돌 원인을 서버 로그에서도 즉시 확인할 수 있게 했다.

## 버린 대안

1. **서버 `UploadSignService`에서 `AVATAR_ASSET`도 다른 해시 덮어쓰기를 허용하는 안**:
   - 이미 발행된 `avatars/<world_id>/e<epoch>/v<version>/mesh.glb` 경로의 객체를 다른 내용으로 덮어쓰면 AGENTS 규칙 6("덮어쓰지 않는다")과 `avatar_asset` 테이블의 버전별 불변 기록(`V14__avatar_asset.sql`)을 깨뜨리므로 기각했다.
2. **`AvatarUpload._upload` 내부에서 임의로 `version + 1`을 바꿔 서명받는 안**:
   - 전송 계층이 혼자 `version`을 올려 `s3://.../v1/mesh.glb`를 반환하면 호출자인 `AvatarLoop`는 `appearance_version=0`으로 `avatar_asset` 메시지를 만들게 되어, 서버 `AvatarStreamService.requireAvatarAssetKey`의 경로·버전 일치 검증에서 거부된다. 버전 번호는 상태 소유자인 `AvatarLoop`가 올려야 한다.
3. **트랙이 `LOST`가 되어도 `self.people`에서 절대 지우지 않는 안**:
   - 장기 운영 시 퇴장한 `world_id`마다 `PersonState`(표본·메시 바이트·이력)가 메모리에 무한 누적된다. 무거운 `PersonState`는 기존처럼 비활성 시 정리하고, `world_id -> int` 두 개(`_high_versions`, `_last_versions`)만 유지하는 편이 메모리와 단순성 모두 유리하다.

## 잔여 위험

- 아바타 워커 프로세스 자체가 재시작되면 새 `stream_epoch = clock_ms()`를 발급받으므로 이전 프로세스와 키가 겹치지 않는다. 만약 외부 시험 코드에서 고정된 `stream_epoch`를 수동 주입해 재시작하더라도, 첫 409 응답 직후 `_reserve_version`으로 버전이 전진하므로 동일 키에 고착되지 않는다.
