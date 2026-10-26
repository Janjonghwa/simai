# front 코드 규칙

프론트 파트(`front/`)의 구조와 코드 규칙이다. 브랜치·커밋·MR은 `docs/git-convention.md`를 따른다.

## 구조

```
front/
  apps/
    admin/              관제 사이트 (데스크톱, Three.js 3D 관제·비상 2D·구역 편집·TBM·시스템 상태·CCTV)
      src/
        routes/         라우트 정의 (TanStack Router 파일 라우트). _app.tsx 아래가 공통 레이아웃을 쓰는 화면
        features/       화면별 도메인 폴더
          layout/       상단 바·하단 메뉴(도크) 등 공통 레이아웃
          monitor/      실시간 관제 화면 (3D 장면 또는 2D 평면 위에 이벤트 패널, 3D·2D 전환)
          events/       위험 이벤트 목록·상세·ACK·경보 배너·경보음
          plan/         2D 평면 (3D 가 멈췄을 때의 비상 화면, BEV 2D 개요). three.js 없이 SVG
          zones/        구역 조회(직사각형 가정)·구역 편집 화면
          viewer3d/     3D 관제 뷰어 (3D 담당 영역)
        hooks/          두 도메인 이상이 쓰는 훅 (useLinkAlive 등)
        lib/            http, api, queryClient, stomp, realtime, bindSession, env, alarmSound 등 두 도메인 이상이 쓰는 것
        store/          두 도메인 이상이 쓰는 스토어 (세션, 실시간 연결 상태)
        mocks/          개발 서버·테스트용 REST·STOMP mock (msw 핸들러, STOMP 브로커, mock 계정·실시간 시나리오)
        __tests__/      src 구조를 따름
      .env.example      환경변수 이름과 로컬 예시 값
    pwa/                반장 사이트 (휴대폰 PWA). M1에 위험 알림, M2에 조치 상태(기획서 §19.2)
  packages/
    ui/                 공통 UI 컴포넌트(shadcn)와 simAI 토큰
    contracts/          ../contracts 스키마에서 생성한 타입·검증기 (손으로 고치지 않음)
    eslint-config/      ESLint·Prettier 공통 설정 (@simai/eslint-config)
    typescript-config/  tsconfig 공통 설정 (@simai/typescript-config)
```

## 의존 방향

- 앱은 `packages/`만 import한다. `admin`과 `pwa`는 서로 import하지 않는다. 둘이 같이 쓰는 것은 `packages/`로 올린다.
- `apps/admin/src/features/viewer3d/`는 3D 담당 영역이다. 바깥에서는 입구(`@/features/viewer3d`: `Viewer3D`, `createExternalSource`, 타입)만 import하고 내부 파일을 직접 import하지 않는다. 입구는 three.js 를 끌고 오므로 첫 화면에서 쓰는 모듈(`lib/` 등)은 타입만 정적으로 import하고 값은 동적 import로 부른다(`lib/stomp.ts`).
- World(미터, 오른손, +Z 상방) → Three.js `(x, z, −y)` 변환은 `viewer3d`의 어댑터 한 곳에서만 한다(`AGENTS.md` 규칙 3). World → 2D 평면(+Y 를 화면 위로 뒤집음) 변환은 `features/plan/plan.ts`의 `toPlan` 한 곳에서만 한다.
- `features/plan`은 3D 가 멈췄을 때 쓰는 비상 화면이라(명세 13.6) three.js·`@react-three/*`·`@/features/viewer3d`를 import 하지 않는다. `__tests__/features/plan/independence.test.ts`가 소스를 검사한다.
- 앱끼리 import 금지, viewer3d 입구 규칙, `@/` alias(`../` 금지), `vitest`는 `src/__tests__`에서만 쓰기는 `apps/admin/eslint.config.js`의 `no-restricted-imports`로 막는다. 테스트는 viewer3d 내부도 직접 import할 수 있다.
- 파트 사이 필드 정의의 정본은 저장소 루트 `contracts/`다. `packages/contracts`의 생성 타입은 손으로 고치지 않고, 필요한 변경은 스키마에 먼저 요청한다(`AGENTS.md` 「계약 우선」).

## 도구

- Node 24.19.0. `front/.node-version`에 고정했고, fnm을 `--use-on-cd`로 설정하면 `front/`에 들어갈 때 자동으로 바뀐다. 다른 Node 버전이면 `pnpm install`이 거부된다(`engineStrict`).
- pnpm 11.24.0 (`package.json`의 `packageManager`). 작업 명령은 turbo로 각 패키지에 나눠 실행한다.
- 포맷은 Prettier(`@simai/eslint-config/prettier-base`), 린트는 ESLint flat config. 앱은 각자 `eslint.config.js`를 두고, `front/eslint.config.js`는 루트 수준 설정 파일만 검사한다.
- 환경변수는 앱 폴더의 `.env.example`을 `.env.local`로 복사해 채운다(`.env.local`은 커밋하지 않음). 앱은 시작할 때 `src/lib/env.ts`로 검사하고, 비었거나 형식이 틀리면 화면을 그리기 전에 멈춘다. 빌드(`vite build`)와 개발 서버도 `vite.config.ts`에서 같은 검사(`parseEnv`)로 멈춘다.
- `VITE_API_BASE_URL`은 비워 두면 화면과 같은 출처로 부른다(운영 nginx). 개발 서버는 실서버로 갈 경로를 `API_PROXY_TARGET`(기본 `http://localhost:8080`, 예: `https://simai.live`)으로 넘긴다(`src/lib/devProxy.ts`). mock 을 끄면 `/api` 전체, 켜면 `VITE_API_REAL` 경로만(비우면 넘기지 않음) 넘기고, `vite preview`는 mock 이 없어 `/api` 전체를 넘긴다. 같은 출처라 갱신 쿠키(`SameSite=Strict`)가 실리고 서버 CORS 설정이 필요 없다. 프록시는 경로를 바꾸지 않고(쿠키 `Path=/api/auth`) 브라우저 `Origin`을 대상 서버의 출처로 바꾼다. 개발 중에 base URL 로 서버를 직접 부르면 다른 사이트가 되어 갱신이 안 된다. 브라우저는 `localhost`로 연다(`Secure` 쿠키는 `http://localhost`에서만 저장된다).
- `VITE_API_MOCK=true`면 개발 서버가 `src/mocks/handlers.ts`로 REST를, `src/mocks/stompBroker.ts`·`src/mocks/realtime.ts`로 STOMP를 흉내 낸다(msw, world 10Hz·health 1Hz·위험 이벤트 40초 주기, 메시지는 `src/mocks/realtimeScenario.ts`). 이벤트 상태(`event_sequence`·`acks`)는 `src/mocks/events.ts`가 들고 이벤트 목록·ACK REST 에 답하며, ACK 를 받으면 갱신 이벤트를 `/topic/events`로 다시 보낸다. `main.tsx`가 DEV에서만 켜서 빌드에는 들어가지 않는다. 프레임이 많아 msw 콘솔 기록은 끈다. 개발 도구 콘솔에서 `simaiMock.pauseHealth(5000)`(health 끊김), `simaiMock.drop()`(연결 끊김), `simaiMock.expireAuth()`(다음 ACK 를 토큰 만료 401 로 거절)로 흉내 낸다. mock 계정은 `src/mocks/accounts.ts`에 있고, 테스트도 같은 핸들러·브로커를 `msw/node`로 쓴다.
- mock 을 켠 채 일부 경로만 실서버로 보내려면 `VITE_API_REAL`에 `/api/…` 경로를 쉼표로 적는다(예: `/api/auth`면 로그인·갱신·로그아웃만 실서버). `main.tsx`가 그 경로의 mock handler 를 빼서(`src/mocks/realPaths.ts`) 처리되지 않은 요청으로 내보내고(`onUnhandledFrame: 'bypass'`), 개발 서버 프록시가 그 경로를 실서버로 넘긴다. `passthrough()` handler 를 앞에 두지 않는다(msw 3 서비스 워커에서 같은 출처의 `Accept: application/json` 요청까지 mock 없이 내보냈다). mock 은 `src/mocks/mockUser.ts`로 mock 토큰(`mock-token.{id}`)과 실서버 JWT(payload 의 `sub`·`role`·`exp`, 서명은 보지 않음)를 함께 읽는다. 만료된 JWT 는 ACK mock 만 401 로 거절하고, STOMP mock 은 만료를 보지 않는다. 모두 mock 일 때 갱신 쿠키는 mock 로그인이 `Set-Cookie`로 주고 msw 가 저장해 새로고침에도 남는다.
- 계약 타입·검증기는 `packages/contracts`가 저장소 루트 `contracts/`에서 만든다(`pnpm --filter @simai/contracts generate`, 결과는 `src/generated`). 지금은 관제 앱이 STOMP로 받는 I2 메시지 4종(`world_state`, `risk_event`, `viewer_health`, `action_mismatch`)만 만든다. 늘리려면 `scripts/generate.mjs`의 `MESSAGES`에만 더한다(목록 `MESSAGE_TYPES`·`MessageByType`·검증기를 함께 만든다). 계약이 바뀌면 다시 만들어 커밋하고, `pnpm lint`는 생성물이 계약과 다르면 실패한다(`check:generated`, 줄바꿈 차이는 보지 않는다). 생성물은 `packages/contracts/.gitattributes`로 LF 로 체크아웃한다. 검증기는 미리 만든 코드라 브라우저에서 스키마를 컴파일하지 않는다(CSP `unsafe-eval` 불필요). 계약 예제가 바뀌어도 잡도록 contracts 테스트는 turbo 캐시를 쓰지 않는다.
- 받은 메시지는 `isMessageType(msg_type)`으로 아는 종류인지 먼저 보고 `validateMessage(msg_type, data)`로 검사한다. 생성 타입은 오타가 걸리도록 닫혀 있지만(모르는 필드 없음), 실행 중 검증은 계약대로 모르는 필드를 받는다.

| 명령 (`front/`에서)                 | 하는 일                                                       |
| ----------------------------------- | ------------------------------------------------------------- |
| `pnpm install`                      | 의존성 설치, git 훅 연결(`core.hooksPath` = `front/.husky/_`) |
| `pnpm dev`                          | 개발 서버 (`pnpm --filter admin dev`로 앱 하나만)             |
| `pnpm build`                        | 전체 빌드                                                     |
| `pnpm lint`                         | 전체 ESLint, 계약 생성물이 최신인지 검사                      |
| `pnpm typecheck`                    | 전체 `tsc --noEmit`                                           |
| `pnpm test`                         | 전체 테스트                                                   |
| `pnpm format` / `pnpm format:check` | Prettier 적용 / 검사                                          |

git 훅 (`front/.husky/`):

- `pre-commit`: 스테이징된 파일에 `front/`가 없으면 그냥 통과한다. 있으면 lint-staged(ESLint `--fix`, Prettier)와 `typecheck`를 돌리고, 실패하면 커밋을 막는다.
- `commit-msg`: 첫 줄이 `[S15P31A101-번호] `로 시작하지 않으면 커밋을 막는다. `Merge`, `Revert`, `fixup!`, `squash!`는 통과한다.

## 코드

### 파일 배치

- `src/routes/*.tsx`는 라우트 정의(`createFileRoute`, `validateSearch`)만 갖는다. 화면 본문은 `src/features/<도메인>/<Name>Page.tsx`에 둔다.
- 도메인 폴더 하나가 한 화면을 책임진다: `<Name>Page.tsx`, `api.ts`, `types.ts`, `use*.ts`, 그 화면 전용 컴포넌트.
- 두 도메인 이상이 쓰는 것만 `src/hooks/`, `src/lib/`로 올린다.
- import는 상대 경로 대신 `@/` alias(`@/*` → `./src/*`)를 쓴다.

### 데이터

- 컴포넌트에 fetch, try/catch, pending 상태를 두지 않는다. `use*.ts` 훅으로 빼고 컴포넌트는 `{ data, pending, error }`처럼 결과만 받는다. 서버 데이터를 다루는 훅의 안쪽은 TanStack Query다.
- REST는 `src/lib/api.ts`의 인스턴스(`src/lib/http.ts`의 `createHttp`)를 거친다. 응답은 zod 스키마로 검사한다. 실패는 `ApiError`로 올라온다. `kind`는 `HTTP`(4xx·5xx), `NETWORK`, `TIMEOUT`(기본 10초), `INVALID_RESPONSE`(성공 상태인데 본문이 JSON 이 아니거나 스키마와 다름)이고, 응답을 못 받았으면 `status`는 0이다. 서버 문구(`serverMessage`)는 `message`에 넣지 않고, 보여 줄지는 훅이 정한다(4xx 만). 서버 메시지가 없으면 훅이 든 한국어 기본 문구를 쓴다.
- Query 기본값(`src/lib/queryClient.ts`): 4xx·`INVALID_RESPONSE`는 다시 보내지 않고 연결 실패·시간 초과·5xx만 두 번까지 다시 보낸다. 창 포커스 때 다시 받지 않는다. mutation 은 다시 보내지 않는다.
- STOMP 연결은 앱에 하나다(`src/lib/stomp.ts`의 `createRealtime`, 인스턴스는 `src/lib/realtime.ts`). 로그인·로그아웃·계정 바뀜에 맞춰 `src/lib/bindSession.ts`가 열고 닫으며, 화면은 연결을 다루지 않는다. 토큰은 연결·재연결마다 `beforeConnect`에서 `authSession.freshToken`으로 받아 CONNECT 헤더에 싣는다(만료 1분 전부터는 먼저 갱신한다. STOMP 는 REST 처럼 401 을 받아 갱신할 길이 없다). 같은 사용자의 토큰 갱신으로는 연결을 다시 열지 않는다. 구독 토픽은 `/topic/world`·`/topic/health`·`/topic/events`다. 연결이 (다시) 열리면 `/topic/health` 를 기다리지 않고 `GET /api/health`(FE 제안 형식: viewer_health 메시지 그대로, `features/layout/useHealthSnapshot`)로 지금 상태를 먼저 채운다. 그 사이 더 새 health 를 받았으면 덮지 않고(`seedHealth`), 실패하면 알리지 않고 `/topic/health` 를 기다린다.
- 받은 본문은 세션마다 만든 3D 뷰어 Source(`createExternalSource`)로 그대로 넘긴다. STOMP 는 3D 뷰어 코드(three.js 묶음)를 기다리지 않고 연결하고, Source 는 뷰어 코드를 받는 대로 붙인다. Source 는 구독 전 메시지를 남기지 않으므로, 연결은 계약을 통과한 가장 새 world·health 와 이벤트별 가장 새 risk_event(넘치면 해소된 것부터 뺌)를 두었다가, 화면에 준 Source 를 3D 뷰어가 구독하는 순간(늦게 붙거나 다시 띄움) 그 뷰어에 넘긴다(`withReplay`). 3D 뷰어에는 앱 처리 뒤에 넘기고 실패를 가둔다(3D 가 고장 나도 2D·경보는 계속 받는다). `world_state`는 앱도 검사해 최신 프레임 하나를 `realtime.store`의 `world`에 둔다(2D 평면용, `t_latest_global`이 더 새 것만, 재연결 뒤 첫 프레임이나 1.5초 넘게 못 받은 뒤의 프레임은 순서와 상관없이 받음). `viewer_health`는 앱도 검사해 `src/store/realtime.store.ts`에 두고(상단 바), health 가 3초 넘게 없으면 연결 끊김으로 본다(`isLinkAlive`). 이벤트 목록처럼 화면이 따로 쓰는 메시지는 해당 Query 캐시를 갱신한다.
- 실시간 위험 이벤트는 Query 키 `['events', 'live']` 하나에 모은다(`features/events/eventsCache.ts`). 첫 목록은 REST 로 받고, `/topic/events`는 `bindEventsCache`가 같은 캐시에 합친다(`mergeEvent`: `event_sequence`가 같거나 작으면 버리고, 확인 기록 `acks`만 합친다). STOMP 로 먼저 받은 것만 있으면 목록을 받은 것이 아니므로 `loaded`로 따로 표시하고, 목록을 받을 때까지(STOMP 재연결 뒤 포함) 다시 시도한다. ACK 는 응답이나 서버가 보낸 갱신 이벤트로 그린다.
- 구역은 `GET /api/zones`를 3D 뷰어 데모와 같은 직사각형 형식(`contract: 'DEMO_UNCONFIRMED'`, `aabb`)으로 가정한다(`features/zones`, 응답 계약은 BE -120·FE -189). 관제 화면은 같은 응답을 3D 뷰어 Source 에도 넘긴다(`pushZones`, 뷰어가 준비됐을 때도 다시 넘김).
- 시스템 상태 화면(`features/system`)은 `viewer_health.tier0_health`(I1 health 원문, 계약은 객체인지만 검사)에서 화면이 쓰는 칸만 느슨하게 읽는다(`health.ts`, 형식이 틀린 칸만 비우고 id 가 없는 카메라만 뺌). `tier0_health` 는 EC2 가 마지막으로 받은 값이라 `tier0_link` 가 CONNECTED 가 아니면 읽지 않는다(상단 바도 같다). 촬영 → 현장 소리·관제 화면·반장 PWA 지연만 `GET /api/metrics`(관리자 전용, BE -70)에서 받는데, 형식 계약이 없어 `contract: 'DEMO_UNCONFIRMED'` 형식을 가정한다(구간 이름은 명세 6.6 의 `capture_to_site_alert`, `capture_to_remote_delivery` 관제 화면·반장 PWA). 값이 없으면 0 으로 채우지 않고 비우고, 연결이 끊기면 지금 값을 비운다(상단 바와 같다). 지연은 `delay` 색만 쓴다.
- 이벤트 클립(`features/clips`, FR-59)은 관리자만, LIVE CRITICAL·HIGH 이벤트 상세에서 본다. 명세엔 `GET /api/clips/{id}`·`PUT /api/clips/{id}/retention` 경로만 있어 이벤트별 목록(`GET /api/events/{id}/clips`)과 응답을 `contract: 'DEMO_UNCONFIRMED'` 형식으로 가정한다(클립 칸 이름은 계약 `media_clip` 과 같다, BE 곽나혁). 재생 주소는 재생을 누를 때만 받는다(EC2 가 조회 기록을 남긴다). EC2 가 `h264-annexb` 원본을 MP4 로 감싸 준다고 가정한다. 보존 표시는 낙관적으로 바꾸지 않고 목록을 다시 받아 그린다. 목록은 상세를 보는 동안 다시 받고, 실패해도 받아 둔 목록은 둔다(4xx·형식 오류면 상세를 다시 열 때까지 받지 않는다).
- 구간 메시지는 `packages/contracts` 검증기를 통과한 것만 쓴다(`validateMessage(msg_type, data)`).
- 낙관적 갱신을 하지 않는다. 서버가 가진 상태(ACK, 이벤트 상태 등)는 서버 응답이나 메시지가 온 뒤에 그린다.
- 렌더링 중에 부작용(외부 변수·DOM 변경, 네트워크 요청, 타이머 등록, 다른 컴포넌트 상태 변경)을 두지 않는다. Effect나 이벤트 핸들러에서 한다.

### 인증

- access 토큰은 `src/store/session.store.ts`에 메모리로만 둔다. 갱신 토큰은 서버가 주는 HttpOnly 쿠키(`simai_refresh`, `Path=/api/auth`, 14일)라 앱이 읽지 않는다. 서버 계약은 `docs/design/server-auth.md`.
- `/api/auth/*`(로그인·갱신·로그아웃)는 `features/auth/authSession.ts`만 부른다. 토큰을 싣지 않는 http(`lib/api.ts`의 `authApi`, 만료된 Bearer 를 실으면 서버가 쿠키를 보기 전에 401 로 거절한다)로 `credentials: 'include'`를 붙여 부르고, 쿠키가 꼬이지 않게 한 줄로 세운다(로그아웃은 진행 중인 갱신 뒤, 로그인은 진행 중인 로그아웃 뒤).
- 앱을 켤 때 갱신으로 로그인을 되살린다(`main.tsx`, 3초까지 기다리고 그린다). 기다리기를 그만둬도 요청은 끊지 않고(요청 시간 제한 30초만), 그 사이 로그인을 시도했으면 복구 결과를 버린다. 로그아웃은 화면을 바로 로그아웃하고 서버에 쿠키 폐기를 부탁한다. 서버에 알리지 못하면(실패·탭 닫기) localStorage 표식(`simai.logoutPending`)을 남기고, 다음에 앱을 켤 때 복구 대신 로그아웃을 다시 보낸다(공용 PC 에서 이전 사용자로 들어가지 않게). 새로 로그인하면 표식을 지운다.
- REST 가 401 이면 `lib/http`가 `authSession.refresh`로 새 토큰을 받아 한 번만 다시 보낸다. 동시에 온 401 은 갱신 하나를 같이 기다리고, 409 `REFRESH_TOKEN_ROTATED`(다른 탭이 먼저 갱신)는 잠깐 뒤 다시 갱신한다. 큐에서 기다리는 사이 세션이 바뀌면 갱신하지 않는다. 서버 꺼짐(5xx·연결 실패·시간 초과)과 계속 겹친 409 는 세션을 두고, 그 밖의 실패(401 쿠키 거부, 403·404, 본문이 틀림)는 `signOutIfCurrent(token)`으로 그 토큰이 지금 토큰일 때 세션을 끝낸다. 다시 보낸 요청도 401 이면 세션을 끝낸다.
- 화면은 `features/auth`의 훅(`useLogin`, `useLogout`, `useCurrentRole`)만 쓰고 세션 저장소·토큰을 직접 다루지 않는다.
- 로그인이 필요한 화면은 `_app` 아래에 둔다. `_app`의 `beforeLoad`가 라우터 컨텍스트의 `sessionStore`로 막는다. 세션이 바뀌면 `bindSession`이 실시간 연결을 열거나 닫고 `router.invalidate()`로 가드를 다시 돌리며, 사용자가 끝나면(로그아웃·다른 계정) 가드가 끝난 뒤 Query 캐시를 지운다. 화면에서 로그인 화면으로 `navigate`하지 않는다.

### 상태

- 화면 상태는 zustand로 관리한다. 값을 쓰지 않는 컴포넌트가 2단계 이상 props로 넘기기만 하면 스토어로 옮긴다. 바로 아래 자식에게만 넘기는 값은 props로 충분하다.
- 스토어 하나는 화면 하나의 관심사 하나만 갖는다. 새 스토어는 `src/features/<도메인>/<domain>.store.ts`에 두고, 두 도메인 이상이 쓸 때만 `src/store/`로 올린다.
- 렌더링에 쓰는 값은 selector로 구독하고(`useXStore((s) => s.value)`), 렌더 밖 콜백에서는 `useXStore.getState()`로 그 순간 값을 읽는다.
- 30 FPS로 바뀌는 World 상태는 React 상태로 두지 않는다. 스토어에 두고 3D 렌더 루프가 `getState()`로 직접 읽는다(뷰어의 데이터 저장소). 2D 평면은 world 를 쓰는 컴포넌트(`LivePlan`)만 구독해 그 층만 다시 그리고, 바탕·구역 층은 `memo`로 바뀔 때만 그린다.
- 자주 바뀌는 값(health 1Hz, 시계)은 그 값을 보이는 작은 컴포넌트가 직접 구독한다. 위 컴포넌트가 받아서 props 로 내리면 화면 전체가 다시 그려진다(`features/layout/TopBar.tsx`).
- 스토어는 컴포넌트 없이 액션과 상태 전이를 테스트할 수 있어야 한다.

### 타입

- `any`를 쓰지 않는다. 타입을 모르면 `unknown`으로 받고 좁혀서 쓴다.
- 서버와 주고받는 타입은 앱에 따로 정의하지 않고 `packages/contracts`에 둔다. 단, REST 요청·응답은 서버 OpenAPI(springdoc)가 나오기 전까지 도메인 `types.ts`에 zod 스키마로 두고, 나오면 생성 타입으로 바꾼다.

### 자동 생성물

- `routeTree.gen.ts`, `packages/contracts`의 생성물, `packages/ui`의 shadcn 컴포넌트는 직접 고치지 않는다. 생성 도구로 다시 만든다.
- import 순서와 Tailwind 클래스 순서를 손으로 정렬하지 않는다. Prettier 플러그인이 정렬한다.

### 테스트

- `src/__tests__/`에 소스 구조를 그대로 따라 둔다(예: `src/__tests__/features/<도메인>/<domain>.store.test.ts`). 테스트 코드는 이 폴더 밖에 두지 않는다.
- 대상은 순수 로직(데이터 연령 계산, 메시지 처리 등)과 훅, 스토어다. 렌더 스냅샷 테스트는 두지 않는다.
- 테스트는 서로 의존하지 않고, 어느 환경에서나 같은 결과를 낸다.
- `import.meta.env`를 읽는 모듈은 테스트에서 import하지 않는다. Vite가 테스트에도 `.env.local`을 읽어서, 그 파일이 없는 PC·CI에서만 실패한다. 검사·계산은 값을 인자로 받는 함수로 떼어 그 함수를 시험한다(`parseEnv`). 앱 REST 인스턴스(`@/lib/api`)를 쓰는 훅은 `vi.mock('@/lib/api')`로 `createHttp` 인스턴스를 시험 주소에 만들어 바꿔 끼운다(`__tests__/features/events/useLiveEvents.test.tsx`).

## 화면

- 설명용 작은 글씨(힌트, 명세 번호, "데모" 표시 등)를 화면에 넣지 않는다.
- 빨강·노랑은 위험 등급에만, 주황은 지연 표시에만 쓴다.
- 확인 대기 경보(`features/events/events.ts`의 `isAlarming`: LIVE·CRITICAL/HIGH·RAISED/UPDATED 이고 지금 경보 단계 뒤 ACK 가 없음)는 경보 배너·소리·점멸로 알린다. 배너와 소리는 관제 앱 어느 화면에서나 나오고(`AppLayout`의 `AlarmBanner`·`AlarmSoundLoop`), 소리는 EC2 연결이 끊기면 멈춘다. 열람 계정은 확인 권한이 없어 끌 수 없으므로 소리 없이 배너만 본다. 브라우저 자동 재생 제한 때문에 클릭·키 입력에서 소리를 깨우고, 나중에 멈춰도 다음 입력에서 다시 깨운다(`lib/alarmSound.ts`의 `installAudioUnlock`). 점멸은 `motion-safe:animate-alarm-blink`로 써서 움직임 줄이기 설정이면 멈춘다.
- 장애(FR-11)는 EC2 가 viewer_health 로 알린 `remote_state`·`laptop.link` 로 정해 모든 화면 상단 바 아래 장애 띠(`features/layout/FailureBand`)에 보인다: 관제 정지 / 현장 장비 응답 없음(노트북 하트비트 끊김) / 원격 전달 단절 · 현장 경보 동작 중. 경보 배너는 그 아래다(`AppLayout`). 실시간 관제처럼 화면을 채우는 화면은 장면 위에 띄우고, 다른 화면은 본문을 가리지 않게 본문 위에 자리를 차지한다. EC2 와 끊겼으면 마지막 상태로 띠를 그리지 않고 상단 바 LOST 만 보인다. Scene 불일치(FR-12, `scene.mismatch`)는 상단 바와 시스템 상태 화면의 장면 칸에 두 버전을 보인다. 둘 다 줄무늬(`bg-stripes`)로만 보이고 위험 등급 색·지연 색을 쓰지 않는다.
- 색·간격 값을 새로 넣기 전에 `packages/ui`의 토큰에 이미 있는지 먼저 확인한다. simAI 토큰은 shadcn 토큰(`background`, `card`, `primary`, `muted-foreground` 등)에 더해 `surface-2`, `surface-3`, `faint`, `point-soft`, `sev-critical`, `sev-caution`, `delay`, `delay-soft`, `ok`, `stripe-a`, `stripe-b`, 로고 `brand-hat`, 떠 있는 패널 그림자 `shadow-float`, 글꼴 `font-mono`가 있다(`packages/ui/src/styles/globals.css`). 컴포넌트에 색 값(`#…`, `rgba(…)`)을 직접 쓰지 않는다.
- 다크(기본)·화이트 모드는 `<html class="dark">`로 가르고, 상단 바 관리자 메뉴에서 바꾼다(`features/layout/theme.store.ts`, 브라우저에 저장). 토큰은 두 모드 값을 모두 둔다.
- 화면 부품은 shadcn 컴포넌트로 만든다. 필요한 컴포넌트가 없으면 `packages/ui`에서 `pnpm exec shadcn add <이름>`으로 추가하고, 모양은 쓰는 쪽의 `className`과 토큰으로 맞춘다.
- 실시간 관제는 왼쪽 위 「3D · 2D」 토글로 보기를 바꾸고, 3D 가 멈추면(렌더 실패·WebGL 끊김·3D 코드 받기 실패) 2D 평면으로 바꾼다. 화면에 들어와 3D 가 2초 안에 첫 화면을 그리지 못하면 그동안 2D 평면을 보인다(그보다 빨리 뜨면 2D 를 비추지 않는다). 2D 를 보다가 3D 로 바꾸면 3D 가 준비될 때까지 2D 를 그대로 둔다. 2D 일 때는 3D 뷰어를 그리지 않는다. 경보 배너·소리·이벤트 패널은 보기와 상관없이 그대로다.
- 2D 평면은 연결이 끊겼거나 world 가 1.5초 넘게 없으면(`isWorldFresh`) 흐리게 그린다. 경보 대상은 병합 전 ID(`aliases`)까지 본다. 확인 대기는 점멸에 더해 선을 굵게 해 움직임 줄이기 설정에서도 구분한다.
- 관제 화면의 상단 바와 하단 메뉴는 `features/layout`에 있다. 실시간 관제처럼 상단 바 아래까지 화면을 채우는 화면은 라우트에 `staticData: { fullBleed: true }`를 준다.
