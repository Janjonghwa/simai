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
          viewer3d/     3D 관제 뷰어 (3D 담당 영역)
        lib/            http, queryClient, stomp, env 등 두 도메인 이상이 쓰는 것
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
- `apps/admin/src/features/viewer3d/`는 3D 담당 영역이다. 바깥에서는 입구인 `Viewer3D` 컴포넌트만 import하고 내부 파일을 직접 import하지 않는다.
- World(미터, 오른손, +Z 상방) → Three.js `(x, z, −y)` 변환은 `viewer3d`의 어댑터 한 곳에서만 한다(`AGENTS.md` 규칙 3).
- 앱끼리 import 금지, viewer3d 입구 규칙, `@/` alias(`../` 금지), `vitest`는 `src/__tests__`에서만 쓰기는 `apps/admin/eslint.config.js`의 `no-restricted-imports`로 막는다. 테스트는 viewer3d 내부도 직접 import할 수 있다.
- 파트 사이 필드 정의의 정본은 저장소 루트 `contracts/`다. `packages/contracts`의 생성 타입은 손으로 고치지 않고, 필요한 변경은 스키마에 먼저 요청한다(`AGENTS.md` 「계약 우선」).

## 도구

- Node 24.19.0. `front/.node-version`에 고정했고, fnm을 `--use-on-cd`로 설정하면 `front/`에 들어갈 때 자동으로 바뀐다. 다른 Node 버전이면 `pnpm install`이 거부된다(`engineStrict`).
- pnpm 11.24.0 (`package.json`의 `packageManager`). 작업 명령은 turbo로 각 패키지에 나눠 실행한다.
- 포맷은 Prettier(`@simai/eslint-config/prettier-base`), 린트는 ESLint flat config. 앱은 각자 `eslint.config.js`를 두고, `front/eslint.config.js`는 루트 수준 설정 파일만 검사한다.
- 환경변수는 앱 폴더의 `.env.example`을 `.env.local`로 복사해 채운다(`.env.local`은 커밋하지 않음). 앱은 시작할 때 `src/lib/env.ts`로 검사하고, 비었거나 형식이 틀리면 화면을 그리기 전에 멈춘다.

| 명령 (`front/`에서)                 | 하는 일                                                       |
| ----------------------------------- | ------------------------------------------------------------- |
| `pnpm install`                      | 의존성 설치, git 훅 연결(`core.hooksPath` = `front/.husky/_`) |
| `pnpm dev`                          | 개발 서버 (`pnpm --filter admin dev`로 앱 하나만)             |
| `pnpm build`                        | 전체 빌드                                                     |
| `pnpm lint`                         | 전체 ESLint                                                   |
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
- REST는 `src/lib/http.ts`를 거친다. 실패는 `ApiError`로 올라오고, 서버 메시지가 없으면 훅이 든 한국어 기본 문구를 쓴다.
- STOMP 클라이언트는 `src/lib/stomp.ts`의 하나를 공유한다. 화면에서 연결을 끊지 않는다. 새 메시지가 오면 해당 Query 캐시를 갱신한다.
- 구간 메시지는 `packages/contracts` 검증기를 통과한 것만 쓴다.
- 낙관적 갱신을 하지 않는다. 서버가 가진 상태(ACK, 이벤트 상태 등)는 서버 응답이나 메시지가 온 뒤에 그린다.
- 렌더링 중에 부작용(외부 변수·DOM 변경, 네트워크 요청, 타이머 등록, 다른 컴포넌트 상태 변경)을 두지 않는다. Effect나 이벤트 핸들러에서 한다.

### 상태

- 화면 상태는 zustand로 관리한다. 값을 쓰지 않는 컴포넌트가 2단계 이상 props로 넘기기만 하면 스토어로 옮긴다. 바로 아래 자식에게만 넘기는 값은 props로 충분하다.
- 스토어 하나는 화면 하나의 관심사 하나만 갖는다. 새 스토어는 `src/features/<도메인>/<domain>.store.ts`에 두고, 두 도메인 이상이 쓸 때만 `src/store/`로 올린다.
- 렌더링에 쓰는 값은 selector로 구독하고(`useXStore((s) => s.value)`), 렌더 밖 콜백에서는 `useXStore.getState()`로 그 순간 값을 읽는다.
- 30 FPS로 바뀌는 World 상태는 React 상태로 두지 않는다. 스토어에 두고 3D 렌더 루프가 `getState()`로 직접 읽는다.
- 스토어는 컴포넌트 없이 액션과 상태 전이를 테스트할 수 있어야 한다.

### 타입

- `any`를 쓰지 않는다. 타입을 모르면 `unknown`으로 받고 좁혀서 쓴다.
- 서버와 주고받는 타입은 앱에 따로 정의하지 않고 `packages/contracts`에 둔다.

### 자동 생성물

- `routeTree.gen.ts`, `packages/contracts`의 생성물, `packages/ui`의 shadcn 컴포넌트는 직접 고치지 않는다. 생성 도구로 다시 만든다.
- import 순서와 Tailwind 클래스 순서를 손으로 정렬하지 않는다. Prettier 플러그인이 정렬한다.

### 테스트

- `src/__tests__/`에 소스 구조를 그대로 따라 둔다(예: `src/__tests__/features/<도메인>/<domain>.store.test.ts`). 테스트 코드는 이 폴더 밖에 두지 않는다.
- 대상은 순수 로직(데이터 연령 계산, 메시지 처리 등)과 훅, 스토어다. 렌더 스냅샷 테스트는 두지 않는다.
- 테스트는 서로 의존하지 않고, 어느 환경에서나 같은 결과를 낸다.
- `import.meta.env`를 읽는 모듈은 테스트에서 import하지 않는다. Vite가 테스트에도 `.env.local`을 읽어서, 그 파일이 없는 PC·CI에서만 실패한다. 검사·계산은 값을 인자로 받는 함수로 떼어 그 함수를 시험한다(`parseEnv`).

## 화면

- 설명용 작은 글씨(힌트, 명세 번호, "데모" 표시 등)를 화면에 넣지 않는다.
- 빨강·노랑은 위험 등급에만, 주황은 지연 표시에만 쓴다.
- 색·간격 값을 새로 넣기 전에 `packages/ui`의 토큰에 이미 있는지 먼저 확인한다. simAI 토큰은 shadcn 토큰(`background`, `card`, `primary`, `muted-foreground` 등)에 더해 `surface-2`, `surface-3`, `faint`, `point-soft`, `sev-critical`, `sev-caution`, `delay`, `delay-soft`, `ok`, `stripe-a`, `stripe-b`, 로고 `brand-hat`, 떠 있는 패널 그림자 `shadow-float`, 글꼴 `font-mono`가 있다(`packages/ui/src/styles/globals.css`). 컴포넌트에 색 값(`#…`, `rgba(…)`)을 직접 쓰지 않는다.
- 다크(기본)·화이트 모드는 `<html class="dark">`로 가르고, 상단 바 관리자 메뉴에서 바꾼다(`features/layout/theme.store.ts`, 브라우저에 저장). 토큰은 두 모드 값을 모두 둔다.
- 화면 부품은 shadcn 컴포넌트로 만든다. 필요한 컴포넌트가 없으면 `packages/ui`에서 `pnpm exec shadcn add <이름>`으로 추가하고, 모양은 쓰는 쪽의 `className`과 토큰으로 맞춘다.
- 관제 화면의 상단 바와 하단 메뉴는 `features/layout`에 있다. 실시간 관제처럼 상단 바 아래까지 화면을 채우는 화면은 라우트에 `staticData: { fullBleed: true }`를 준다.
