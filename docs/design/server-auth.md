# 서버 로그인·JWT·갱신 토큰·역할 권한 (S15P31A101-61)

## 관련 명세

- 14장: 뷰어·PWA 계정 로그인, 역할 3종(관리자·반장·열람), 구역·작업면·규칙·레이아웃 수정은 관리자만, 반장 PWA 는 긴 갱신 토큰으로 관제 중 로그인 유지
- 12.6: `POST /api/auth/login` 공개, 나머지 API 는 열람·반장·관리자·기기 권한
- 요구사항 FR-1(로그인 후 JWT), FR-2(API별 권한), FR-35(반장 로그인 유지), NFR-12·13
- DB: `app_user`·`refresh_token`(V1), `docs/design/db-tables.md`
- 계정·기기·워커 토큰은 API 없이 EC2 관리 명령으로 만든다(61, 222 팀 결정)

## 코드·시험 위치

`server/src/main/java/com/ssafy/simai/` 아래:

- `domain/auth`: 로그인·갱신·로그아웃 API(`controller`·`service`·`dto`), 갱신 토큰(`entity`·`repository`)
- `domain/user`: 계정 `app_user`(`entity`·`repository`·`service`)
- `global/command`: 관리 명령(`admin create-user`). 기기·워커 토큰 발급·회전·폐기(222)도 여기에 붙인다
- `global/config`(공통 빈·보안 필터 체인·JWT 키), `global/security`(access 토큰 발급), `global/properties`(`simai.auth.*`)
- `global/exception`: `ErrorCode`(도메인별 enum, 상태·문구), `BusinessException`, `GlobalExceptionHandler`(ProblemDetail + `code`)
- 시험(`server/src/test/java/com/ssafy/simai/`, 실제 DB):
  - `domain/auth/AuthIntegrationTest`: 로그인 성공·실패, 역할별 허용·거부, 만료·발급자·서명 위조 토큰
  - `domain/auth/RefreshTokenIntegrationTest`: 갱신·교체, 재사용 감지, 만료, 막힌 계정, 로그아웃
  - `global/command/AdminCommandsTest`·`AdminCommandLineTest`: 계정 생성, 중복·약한 비밀번호·잘못된 옵션, 웹 서버 없이 기동

## 동작

- 로그인 `POST /api/auth/login`: 요청 `{username, password}`(username 은 `app_user.login_id`),
  응답 본문 `{accessToken, tokenType, expiresIn, user: {id, role, displayLabel}}`(초 단위) + 갱신 토큰 쿠키.
  관제 앱(`front/apps/admin/src/features/auth/types.ts`)이 읽는 `accessToken`·`user.id`·`user.role` 형식을 따랐다.
- 갱신 토큰은 본문에 싣지 않고 쿠키 `simai_refresh`(`HttpOnly; Secure; SameSite=Strict; Path=/api/auth; Max-Age=14일`)로만 주고받는다.
  프론트 JS 가 읽을 수 없어 XSS 로 훔칠 수 없고, 갱신·로그아웃 요청에만 실린다. 프론트는 `credentials: 'include'` 로 부른다.
- 갱신 `POST /api/auth/refresh`(본문 없음, 쿠키): 쓴 갱신 토큰을 폐기하고 새 access 토큰과 새 쿠키를 준다(응답은 로그인과 같다).
  폐기된 토큰이 다시 오면 유출로 보고 그 계정의 살아 있는 갱신 토큰을 모두 폐기한다. 막힌 계정·쿠키 없음은 거부한다(401, 쿠키도 지운다).
  단, 갱신으로 교체된 지 30초 안에 같은 토큰이 다시 오면(탭 두 개·네트워크 재시도로 동시에 갱신) 탈취로 보지 않고
  409 `REFRESH_TOKEN_ROTATED` 만 준다. 이때는 쿠키를 지우지 않는다(먼저 끝난 요청이 넣은 새 쿠키가 있다). 프론트는 한 번 더 갱신한다.
  로그아웃으로 폐기된 토큰은 뒤이은 새 토큰이 없어 이 예외에 들지 않는다.
- 로그아웃 `POST /api/auth/logout`(본문 없음, 쿠키): 그 갱신 토큰을 폐기하고 쿠키를 지운다(`Max-Age=0`). 쿠키가 없거나 모르는 토큰이어도 204.
  토큰을 본문 없는 DELETE·URL 에 싣지 않으려고 POST 로 둔다(RFC 7009 토큰 폐기도 POST).
- 로그인·갱신·로그아웃은 공개 경로다(갱신·로그아웃은 access 토큰이 만료된 뒤에도 불러야 한다). 나머지는 인증이 필요하다.
- 실패 문구는 원인을 나누지 않는다: 로그인 401 `LOGIN_FAILED`, 갱신 401 `INVALID_REFRESH_TOKEN`. 없는 아이디도 해시 비교를 한 번 한다.
- 보안 필터가 거부한 응답도 같은 ProblemDetail 본문을 싣는다: access 토큰 없음·만료·위조 401 `UNAUTHORIZED`(+ `WWW-Authenticate`), 역할 부족 403 `FORBIDDEN`.
  관제 앱은 `UNAUTHORIZED` 면 갱신 후 재시도, `INVALID_REFRESH_TOKEN` 이면 로그인 화면으로 보낸다.
- 역할은 컨트롤러에 `@PreAuthorize` 로 붙인다(열람 = 인증만, 반장 = `hasAnyRole('FOREMAN','ADMIN')`, 관리자 = `hasRole('ADMIN')`).
  12.6 의 "반장(조회는 관리자도)" 처럼 역할마다 다른 곳이 있어 역할 계층은 두지 않았다. `Role` 은 DB CHECK 와 같은 3값이고, 공개·기기 권한은 Role 이 아니다.
- 계정 생성: 평소 기동에는 아무것도 하지 않는다. EC2 에서 `java -jar simai.jar admin create-user --login <아이디> --role <ADMIN|FOREMAN|VIEWER> [--label <표시 이름>]`
  을 실행하면 웹 서버 없이(필요한 빈만 지연 생성, JWT 키 불필요) 떠서 비밀번호를 두 번 입력받아 만들고 끝난다(종료 코드 0 성공, 1 처리 실패, 2 옵션 오류).
  비밀번호는 셸 기록·프로세스 목록에 남지 않게 인자로 받지 않는다. 같은 아이디가 있으면 덮어쓰지 않는다.
- 계정 막기: `admin disable-user --login <아이디>` 가 `disabled_at` 을 찍고(지우지 않음) 그 계정의 갱신 토큰을 모두 폐기한다.
  이미 막혀 있으면 처음 시각을 그대로 둔다. 이미 나간 access 토큰은 만료(최대 30분)까지 남는다.
- 스키마는 Flyway 만 만든다. JPA 는 `ddl-auto: validate` 로 엔티티가 테이블과 맞는지만 기동 때 검사한다.

## 명세에 없어 정한 값

| 값 | 정한 것 | 근거 |
|---|---|---|
| 서명 | HS256, 키는 `SIMAI_JWT_SECRET`(Base64, 32바이트 이상). 없거나 짧으면 서버가 뜨지 않는다 | 서버 하나가 발급·검증을 다 한다. RFC 7518 3.2 최소 길이 |
| access 토큰 유효 시간 | 30분 | 갱신 토큰으로 다시 받는다. 막힌 계정의 토큰이 살아 있는 시간도 이만큼으로 줄어든다 |
| 갱신 토큰 유효 시간 | 14일, 갱신마다 새 토큰으로 교체 | 반장 PWA 가 시연·관제 기간 중 다시 로그인하지 않게 한다 |
| 갱신 토큰 형식 | JWT 가 아닌 무작위 256비트(Base64url), DB 에는 SHA-256 16진 해시만 | 원문을 저장하지 않는다(DB 설계). 무작위 값이라 소금 없이 충분 |
| 클레임 | `iss=simai`, `sub=user_id`, `role`, `iat`, `exp` | 위치·이벤트 기록에는 user_id 만 쓴다(11.4) |
| 비밀번호 | `{bcrypt}` 접두어(DelegatingPasswordEncoder), 8자 이상 72바이트 이하 | 나중에 알고리즘을 바꿔도 기존 해시를 읽는다. BCrypt 는 72바이트까지만 받는다(넘는 로그인 시도는 같은 401) |
| 동시 갱신 유예 | 교체 후 30초, 409 `REFRESH_TOKEN_ROTATED` | 같은 쿠키를 든 탭·재시도가 계정 전체 폐기로 이어지지 않게 한다. 재사용 감지는 그 뒤부터 |
| 쿠키 Secure | 기본 켬. `SIMAI_REFRESH_COOKIE_SECURE=false` 로 끌 수 있음 | https 가 아닌 로컬 시험(Postman 등)에서만 끈다 |
| CORS | `SIMAI_CORS_ALLOWED_ORIGINS` 에 적은 출처만, `/api/**`, 자격 증명(쿠키) 허용 | 관제 앱 개발 서버(Vite)가 다른 포트에서 부른다. 운영은 같은 출처라 비워 둔다 |

## 버린 대안

- jjwt 등 별도 JWT 라이브러리: Spring Security 리소스 서버(Nimbus)로 발급·검증이 다 되고 401 응답(`WWW-Authenticate`)도 표준대로 나온다.
- 역할 계층(ADMIN > FOREMAN > VIEWER): 반장 전용 쓰기(티켓 상태 변경)가 있어 맞지 않는다.
- 기동 때 환경 변수로 첫 관리자 자동 생성: 계정 생성 경로가 둘이 되고 기본 비밀번호가 설정에 남는다. 관리 명령으로 바꿨다.
- 갱신 토큰을 JWT 로: 폐기하려면 어차피 DB 를 봐야 하고, 원문 대신 해시만 저장하는 DB 설계와 맞지 않는다.
- 갱신 토큰을 본문으로 주고 프론트가 localStorage 에 저장: 스크립트 한 줄 주입으로 14일짜리 토큰이 털린다.
  지금 관제 앱에 HTML 을 그대로 넣는 곳은 없지만, LLM 보고서 렌더링·사진 업로드 등 앞으로 생길 통로를 막지 못한다.
- 갱신 토큰을 Redis 에: V1 `refresh_token` 테이블(DB 설계)과 어긋나고, 재사용 감지·폐기 이력과 재시작 내구성을 위해 PostgreSQL 에 둔다.
- 만료된 access 토큰을 내밀면 서버가 저장한 갱신 토큰으로 바꿔 주는 방식: access 토큰 하나만 털려도 계속 갱신된다.

## 잔여 위험

- 계정을 막아도 이미 나간 access 토큰은 최대 30분 쓸 수 있다(갱신은 바로 막힌다).
- 로그인·갱신 시도 횟수 제한이 없다.
- 폐기·만료된 갱신 토큰 행을 지우지 않는다(갱신마다 한 행, 반장 1명이 하루 약 48행). 정리 작업과 `refresh_token(user_id)` 인덱스(Flyway V3)는 아직 없다.
- 교체 후 30초 안에는 유출된 옛 토큰이 와도 409 로만 막고 전체 폐기하지 않는다(새 토큰은 받지 못한다).
- 관제 앱은 아직 갱신을 부르지 않아 access 토큰이 끝나는 30분마다 다시 로그인해야 한다(프론트 작업 필요).

## 넘긴 일

- 기기·워커 토큰 발급·회전·폐기 명령과 인증, 경로별 `SecurityFilterChain`(사용자 JWT / `/api/devices/**` 기기 토큰 / `/api/worker/**` 워커 토큰), 필터는 `global/security`:
  S15P31A101-222(발급과 검증이 같은 해시 규칙·테이블을 쓰므로 61 에서 옮김). 워커 대여 API 는 229
- 관리 명령 추가: 비밀번호 재설정
- 관제 앱·반장 PWA(231) 갱신 호출: 401 이면 `credentials: 'include'` 로 `/api/auth/refresh` 후 재시도, 앱 시작 때 갱신으로 로그인 복구.
  개발 중에는 Vite 프록시로 `/api` 를 서버에 넘기면 쿠키·CORS 설정 없이 같은 출처로 동작한다
- STOMP 연결 인증(관제 앱은 CONNECT 헤더에 `Authorization: Bearer` 를 싣는다): STOMP 서버를 만들 때
- 관제 앱 `types.ts` 를 OpenAPI 생성 타입으로 바꾸기. 문서는 S15P31A101-52 에서 붙였다(`/api/docs/openapi.json`, 화면 `/api/docs`, 토큰 없이 열림)
