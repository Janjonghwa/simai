# 관제 앱 아바타 구독·인증 연결 (S15P31A101-326)

기획서 §13.1~13.2·§14와 `contracts/messages/avatar_frame.schema.json`을 따른다. 계약 변경은 없다.

## 구현과 시험

- `front/apps/admin/src/lib/stomp.ts`: `/topic/avatar`를 연결·재연결마다 구독한다. 앱에서 계약 실패를 세고 기존 world 경로처럼 원문과 수신 시각을 Source에 넘긴다.
- `front/apps/admin/src/lib/http.ts`: 선택 옵션 `responseType: 'arrayBuffer'`로 바이너리를 받는다. 성공 응답만 바이트로 읽고 오류는 기존 ProblemDetail 처리에 넘긴다. 경로 검사·Bearer 주입·401 갱신·한 번 재시도·시간 제한을 공유한다.
- `front/apps/admin/src/lib/avatarMeshFetch.ts`: 앱의 인증 HTTP로 바이트를 받아 Viewer의 `avatarMeshFetch`에 맞춘다. `SpatialViewportHost.tsx`는 이 함수를 prop으로 넘기는 두 줄만 추가한다.
- `src/__tests__/lib/stomp.test.ts`, `api.test.ts`: 구독·재구독·Source 원문/수신 시각·바이트 보존·Bearer·401 갱신·재시도 실패 시 세션 종료·외부 URL 거부를 검증한다.
- `src/__tests__/features/layout/SpatialViewportHostObjects.test.tsx`: 실제 호스트가 인증 fetch를 Viewer prop으로 넘기는지 검증한다.

## 선택과 근거

별도 인증 fetch를 만들면 토큰 갱신·오류·시간 제한 정책이 갈라지므로 기존 HTTP에 바이너리 응답 선택만 추가했다. JSON 응답은 기존 기본값이다. 바이너리도 zod로 반환 자료형을 검사하며 sha256·리그 검사는 기존 Viewer가 맡는다.

포즈는 STOMP 재생 버퍼에 보관하지 않는다. Source가 준비되지 않았거나 Viewer를 다시 열었으면 다음 포즈를 기다린다. 사람의 존재·위치·위험은 world·events가 계속 정본이고 기존 0.5 s 캡슐 폴백을 쓴다. 새 시간 기준이나 수치는 정하지 않았다.

## 남은 위험과 후속 작업

- mock의 기존 브로커는 임의 토픽을 보낼 수 있어 시험에서 `/topic/avatar`를 직접 송출했다. 개발 mock의 주기적 포즈·인증 GLB 핸들러는 추가하지 않았다. 이를 위해서는 합성 리그·무결성 해시·사람/보정 버전을 갖춘 별도 시나리오가 필요하다.
- 실제 EC2 중계(-325)·Pod 루프(-324)와 묶은 GLB 표시 및 0.5 s 폴백은 Lead의 통합 시험에서 확인한다. 이번 검증은 네트워크 없이 수행한다.
- 동시 작업 -323과 충돌할 수 있는 호스트 수정은 import와 prop 한 줄씩이다.
