# CI 파이프라인: Jenkins (S15P31A101-28)

MR 마다 정적 검사·단위 시험·계약 예시 검증을 자동으로 돌린다(명세 21.3, 21.4, NFR-28). 저장소 루트의 `Jenkinsfile` 과 Jenkins 이미지
`deploy/jenkins/` 로 구성하고, EC2 운영 compose(`deploy/ec2/compose.yaml`, S15P31A101-66)에 `jenkins`·`ci-db` 서비스로 올린다.
이미지 빌드·배포(CD)는 S15P31A101-254 가 같은 Jenkinsfile 의 `배포` 단계로 붙였다(`docs/design/ec2-deploy.md` "자동 배포").

명세 21.4 의 CI 도구를 GitLab CI 에서 Jenkins 로 바꿨다(팀 결정 2026-10-21, 기획서 개정 이력 참조). 단계 구성(정적 검사 → 단위 →
계약·공통 벡터 → 통합 → 이미지 빌드, GPU 없는 실행)은 그대로다.

## 구성

```
GitLab ──Webhook──▶ https://simai.live/jenkins/ (Nginx) ──▶ jenkins 컨테이너 ── 검사를 직접 실행
                                                              └─ ci 망 ──▶ ci-db (서버 시험 DB)
```

- **Docker 를 쓰지 않는다.** 검사는 Jenkins 컨테이너 안에서 바로 돈다. Jenkins 는 호스트 Docker 소켓을 받지 않는다(아래 "정한 것").
- `jenkins`: Jenkins 2.580.1 LTS(JDK 21) + 각 파트 도구. 망은 `edge`(Git clone·플러그인 내려받기)와 `ci`. 메모리 상한 4 GB.
- `ci-db`: 운영 `db` 와 같은 이미지(`timescaledb-ha` pg17-all). `ci` 망(internal, 밖으로 못 나감)에만 붙는다. 운영 `db`·`ec2-internal`
  망과 분리돼 있어 CI 가 운영 데이터에 닿지 않는다. 메모리 상한 1 GB.
- Jenkins 화면과 Webhook 은 Nginx `/jenkins/` 뒤에 있다(`JENKINS_OPTS=--prefix=/jenkins`). 별도 포트를 열지 않는다.

## 단계

| 단계 | 하는 일 | 도는 조건(MR 에서 바뀐 경로) |
|---|---|---|
| 변경 경로 | MR 이면 대상 브랜치와의 merge-base 이후 바뀐 파일로 아래 단계를 고른다. MR 이 아닌 빌드(dev·master push, 수동)는 모두 돈다 | 항상 |
| 판 점검 | `deploy/scripts/check_versions.sh`: `front/.node-version`·`build.gradle` toolchain·`requires-python` 이 이미지 판(`docker-bake.hcl`)과 같은지 | 항상 |
| contracts | `uv run contracts/scripts/validate_examples.py`: `examples/` 통과, `examples/invalid/` 실패 확인 | `contracts/` |
| server | `ci-db` 에 시험 DB `simai_ci` 를 지우고 새로 만든 뒤 `sh gradlew build`. `@SpringBootTest` 가 Flyway 로 V1·V2·R 을 적용한다. JUnit 결과를 Jenkins 에 올린다 | `server/`, `contracts/` |
| front | `pnpm install --frozen-lockfile` → `format:check` → `lint`(계약 생성물 최신 여부 포함) → `typecheck` → `test` → `build` | `front/`, `contracts/` |
| gateway | 패키지 4개(recorder, relay, rtsp_probe, site_alert)를 차례로 `uv sync --frozen` → `uv run pytest` | `gateway/`, `contracts/` |
| ai | `uv sync --frozen` → `uv run lint-imports`(의존 방향). **pytest 는 새벽 작업에서만**: 작업 매개변수 `FULL_AI_TESTS=true` 이면 `pytest -m "not gpu"` 전체, 아니면(MR·dev push) 생략한다. 새벽 작업 `simai-nightly-ai` 가 매일 02:00 KST 에 dev 로 돈다(아래 설정 8). 빌드가 한 줄로 하나씩 돌아 ai 전체 시험(30분대) 하나가 뒤 MR 을 막았기 때문이다(2026-10-26, 빌드 #43·#44, S15P31A101-269). MR 에서 ai 를 바로 확인하려면 로컬에서 `scripts.select_tests` 로 고른 시험을 돌린다(`docs/design/test-selection.md`) | `ai/`, `contracts/`, `deploy/` |

`Jenkinsfile`·`deploy/jenkins/` 가 바뀌면 모든 단계가 돈다. 결과는 GitLab 커밋 상태 `jenkins` 로 MR 에 보인다(running → success·failed·canceled).

## 정한 것과 근거

- **호스트 Docker 소켓을 Jenkins 에 주지 않는다**: 소켓을 가진 컨테이너는 EC2 root 와 같다. 같은 서버에 운영 DB·`/opt/simai/.env` 가
  있어, Jenkins 가 뚫리거나 MR 코드가 소켓을 쓰면 운영 서버 전체가 넘어간다. 검사에는 호스트 권한이 필요 없으므로 도구를 Jenkins 이미지에
  넣고 직접 돌린다(2026-10-22 결정).
- **도구 판은 배포 이미지와 같은 변수**: Jenkins 이미지는 `docker-bake.hcl` 의 `NODE_WEB`(Node 24.19.0)·`UV_IMAGE`(uv 0.11.27)·
  `PYTHON_VERSION`(3.11.15)을 그대로 쓴다. pnpm 은 corepack 이 `front/package.json` 의 `packageManager` 를 읽는다. JDK 21 은 Jenkins 공식
  `lts-jdk21`. 프로젝트 쪽 판이 바뀌면 `판 점검` 단계가 실패해 알려 준다.
- **서버 시험 DB**: `@SpringBootTest` 는 DataSource 없이 뜨지 않고, H2 는 V1 의 TimescaleDB·PostGIS 확장을 못 만든다. 운영과 같은 이미지의
  `ci-db` 를 상시 띄우고, 빌드마다 `DROP DATABASE … WITH (FORCE)` → `CREATE DATABASE` 로 빈 DB 를 준다. 접속은 `SPRING_DATASOURCE_*` 로
  넣어 server 코드를 고치지 않는다. 비밀번호 `simai-ci` 는 `ci` 망 안에서만 닿는 일회용 DB 용이다.
  Spring 시험은 설정이 다른 컨텍스트를 여러 개 캐시하고 컨텍스트마다 연결 풀을 잡는다. 그래서 `ci-db` 연결 상한을 100
  (`TS_TUNE_MAX_CONNS`, 메모리 1 GB 자동 조정값 25 대신)으로 올리고 CI 풀을 컨텍스트당 5 로 줄였다(첫 빌드에서 "too many clients" 로 실패).
- **이미지에 넣은 것**: `git`(ai 의 gpu_queue·deploy 시험이 저장소 git 정보를 읽는다), `libtk8.6`(site_alert Tk 시험. 없으면 pytest 가
  건너뛰지 않고 ImportError 로 실패한다. 화면이 없으면 시험이 스스로 건너뛴다), `postgresql-client`(시험 DB 만들기).
- **트리거**: GitLab 플러그인 Webhook. MR 은 대상 브랜치가, push 는 그 브랜치가 `dev`·`master` 일 때만 돈다. 같은 MR 에 새 push 가 오면
  대기 중인 빌드를 취소한다. 관제 서비스와 같은 EC2 라 `disableConcurrentBuilds` 로 한 번에 하나만 돈다.
- **바뀐 경로만**: 파트마다 커밋 속도가 달라(front·ai 는 하루 여러 번) 관계없는 단계를 돌리지 않는다. `contracts/` 가 바뀌면 계약을 읽는
  파트(ai·gateway 시험, front `check:generated`, server `contracts.gradle` 계약 클래스)도 돈다.
- **캐시**: uv·pnpm·Gradle 내려받기를 `${WORKSPACE}@tmp/cache` 에 둔다. 저장소 안에 두면 git 상태가 바뀌어 git 정보를 읽는 시험에 영향을
  줄 수 있다. `.venv`·`node_modules` 는 작업 공간에 남아 다음 빌드가 다시 쓴다.
- **작업 공간 정리**: 첫 단계에서 `git clean -ffdx -e .venv -e node_modules -e .gradle` 로 추적 안 되는·무시 파일을 지운다. 작업 공간을
  빌드 사이에 재사용해서, 다른 브랜치 빌드가 남긴 `__pycache__` 등으로 `__init__.py` 없는 폴더가 남아 ai 구조 시험이 실패했다(S15P31A101-258).
- **gradlew**: 실행 권한 없이(100644) 커밋돼 있어 `sh gradlew` 로 부른다. **husky**: `HUSKY=0`.
- **ai lint**: ruff 등은 아직 정하지 않았다(`ai/CONVENTION.md`). 정해지면 ai 단계에 더한다. 지금은 합의된 `lint-imports` 만 돈다.

## Jenkins 준비 (EC2)

1. 이미지와 서비스(저장소 최상위, `/opt/simai/repo`):

   ```sh
   TAG=$(cat REVISION) docker buildx bake -f deploy/docker-bake.hcl jenkins
   sed -i "s/^SIMAI_JENKINS_TAG=.*/SIMAI_JENKINS_TAG=$(cat REVISION)/" /opt/simai/.env   # 없으면 줄을 더한다
   docker compose --env-file /opt/simai/.env -f deploy/ec2/compose.yaml up -d jenkins ci-db web
   ```

   `web` 은 Nginx `/jenkins/` 경로가 든 이미지로 다시 띄운다.
2. `https://simai.live/jenkins/` 첫 화면: 잠금 해제 비밀번호는
   `docker compose … exec jenkins cat /var/jenkins_home/secrets/initialAdminPassword`. 플러그인은 이미지에 들어 있으니 "Select plugins to
   install" → 아무것도 고르지 않고 넘어간다. 관리자 계정을 만들고 Jenkins URL 을 `https://simai.live/jenkins/` 로 둔다.
   **Manage Jenkins → Security** 에서 가입을 막고(Allow users to sign up 끔) 권한은 "Logged-in users can do anything" 으로 둔다(익명 읽기 끔).
3. GitLab 연결: GitLab 프로젝트 **Settings → Access tokens** 에서 `api` 권한 Project Access Token 을 만든다(역할 Developer). Jenkins
   **Manage Jenkins → System → GitLab** 에 연결 이름 **`gitlab`**(Jenkinsfile 의 `gitLabConnection('gitlab')`), 주소
   `https://lab.ssafy.com`, 토큰은 "GitLab API token" 자격 증명으로 등록하고 Test Connection. 저장소 clone 용으로 `read_repository` 토큰을
   "Username with password"(사용자 이름 아무 값, 비밀번호 = 토큰) 자격 증명으로 등록한다. 연결의 고급 설정에서 Connection timeout 15 s,
   Read timeout 60 s 로 둔다(기본 10 s 면 SSAFY GitLab 이 느릴 때 마지막 결과 올리기가 시간 초과로 실패해 MR 이 running 에 머문다,
   2026-10-23 빌드 #11).
4. 작업: **New Item → Pipeline**(이름 `simai`) → Pipeline script from SCM, Git, 저장소 `https://lab.ssafy.com/s15-final/S15P31A101.git`,
   위 clone 자격 증명, Branch Specifier `origin/${gitlabSourceBranch}`, Script Path `Jenkinsfile`. 작업에 같은 이름의 파라미터를 두지
   않는다: 수동 빌드용으로 `gitlabSourceBranch`(기본 `dev`)를 두었더니 Webhook 빌드에서도 그 기본값이 GitLab 이 넘긴 값보다 우선해
   MR 빌드가 dev 를 받아 왔다(2026-10-23, 빌드 #8·#9). 다시 돌리려면 MR 에 `Jenkins please retry a build` 댓글을 달거나 새로 push 한다.
5. 트리거: 작업 설정 **Triggers → Build when a change is pushed to GitLab** 를 켜고 Push Events·Opened Merge Request Events, 고급 설정에서
   "Filter branches by name" 에 `dev,master`(MR 은 대상 브랜치, push 는 그 브랜치 기준), "Cancel pending merge request builds on update"
   를 켠다. "Rebuild open Merge Requests" 는 **On push to source branch** 로 둔다(기본 Never 면 MR 을 처음 열 때만 빌드되고 그 뒤
   push 에는 돌지 않는다, 2026-10-23 확인). 고급 설정의 Secret token 을 **Generate** 하고 저장한다. 트리거는 Jenkinsfile 의 `triggers` 에 두지 않는다: Declarative
   파이프라인은 빌드마다 그 블록으로 트리거를 다시 만들어 저장한 Secret token 을 지운다(첫 연결 때 401 Invalid token 으로 확인).
6. Webhook: 위 주소(`https://simai.live/jenkins/project/simai`)와 Secret token 을 GitLab **Settings → Webhooks** 에 넣고 Push events·
   Merge request events·Comments 를 고른다(Comments 는 댓글 재빌드용, 작업 트리거의 "Comments" regex `Jenkins please retry a build`). **Test → Push events** 가 200 이면 연결됐다. Webhook 을 고쳐 URL 을 바꾸면 GitLab 이 Secret token 을
   지우므로 다시 넣는다.
7. 머지 조건 "Pipelines must succeed" 는 꺼져 있다(2026-10-21). Jenkins 가 MR 에 상태를 올리는 것을 확인한 뒤 켤지 정한다. 상태가 오지
   않는데 켜면 모든 MR 이 막힌다.
8. 새벽 전체 시험 작업(S15P31A101-269): **New Item → Pipeline**(이름 `simai-nightly-ai`) → `simai` 와 같은 저장소·clone 자격 증명·
   Script Path `Jenkinsfile`, Branch Specifier 는 `*/dev`(Webhook 이 아니라 `gitlabSourceBranch` 가 없다). **이 빌드는 매개변수가 있습니다**에
   Boolean `FULL_AI_TESTS`(기본 true), **Triggers → 주기적으로 빌드**에 `TZ=Asia/Seoul` 다음 줄 `0 2 * * *`(매일 02:00 KST 정각, 컨테이너
   시계는 UTC). GitLab 트리거는 걸지 않는다. Webhook 빌드가 아니라 `gitlabActionType` 이 없어 배포 단계는 타지 않고, 모든 단계가 돈다.
   결과는 그 dev 커밋의 `jenkins` 상태로도 올라간다.

비밀 값(관리자 비밀번호, GitLab 토큰, Webhook Secret)은 Jenkins 자격 증명 저장소(`jenkins-home` 볼륨)에만 둔다. 저장소·`.env` 에 넣지 않는다.

## 로컬에서 같은 검사 돌리기

```sh
sh deploy/scripts/check_versions.sh
uv run contracts/scripts/validate_examples.py
(cd ai && uv sync --frozen && uv run lint-imports && uv run pytest -m "not gpu")   # Windows 는 PYTHONUTF8=1
for p in recorder relay rtsp_probe site_alert; do (cd gateway/$p && uv sync --frozen && uv run pytest); done
(cd front && pnpm install --frozen-lockfile && pnpm format:check && pnpm lint && pnpm typecheck && pnpm test && pnpm build)
docker run -d --name simai-ci-db -p 5432:5432 -e POSTGRES_PASSWORD=simai-ci timescale/timescaledb-ha:pg17-all
(cd server && SPRING_DATASOURCE_URL=jdbc:postgresql://localhost:5432/postgres SPRING_DATASOURCE_USERNAME=postgres \
  SPRING_DATASOURCE_PASSWORD=simai-ci ./gradlew build)
```

## 버린 대안

- **Jenkins 에 호스트 Docker 소켓을 주고 단계마다 컨테이너로 검사**(처음 초안): 파트별 이미지로 깔끔하게 격리되지만 Jenkins 가 EC2 root 와
  같아진다. 운영 DB·비밀 값과 같은 서버라 버렸다.
- **GitLab CI**: 저장소 안 설정 하나와 `gitlab-runner` 만으로 되지만, 팀이 Jenkins 로 정했다(2026-10-21). 같은 검사 묶음이라 옮기기 쉽다.
- **CI 전용 서버**: 운영과 완전히 분리되지만 서버가 하나 더 필요하다.
- **server 시험에 H2·Testcontainers**: H2 는 확장을 못 만든다. Testcontainers 는 Docker 소켓이 필요하다.

## 잔여 위험

- 검사가 Jenkins 컨테이너(빌트인 노드) 안에서 돌아 MR 코드가 Jenkins 홈(자격 증명 포함)을 읽을 수 있다. 저장소가 비공개이고 팀원만 MR 을
  올린다는 전제다. 호스트·운영 DB 로는 넘어가지 못한다. 더 나누려면 도구를 담은 별도 에이전트 컨테이너를 붙인다.
- CI 가 관제 서비스와 같은 EC2 의 CPU 를 쓴다(ai 시험 20분 이상). 한 번에 하나만 돌지만, 시연·관제 시간에는 빌드를 멈추는 편이 안전하다.
- 플러그인 판은 이미지를 빌드할 때의 최신 호환판이다(고정하지 않음).
- ai 시험 하나가 Linux 에서만 실패한다(아래). 고칠 때까지 ai 단계는 실패로 끝난다.

## 넘긴 일

- 이미지 빌드·배포(CD): S15P31A101-254 에서 dev 빌드 끝에 붙였다. 호스트의 배포 스크립트 하나만 실행하는 제한 SSH 키를 쓴다
  (`docs/design/ec2-deploy.md` "자동 배포").
- Risk Engine 공통 벡터(부록 D-G3-04): Tier 0 이미지와 EC2 컨테이너 이미지 두 곳에서 같은 벡터를 돌리는 단계.
- 통합 시험(Docker Compose EC2 스택 + 가짜 Tier 0 송신기, 명세 21.3).

## 검증에서 찾은 것

- `ai/tests/services/tier0/test_live_input.py::test_partial_port_start_failure_closes_already_opened_listeners` 가 Linux 에서 실패한다
  (Windows 는 통과). `VideoIngestServer.stop()` 이 `close()` 만 부르는데, Linux 는 다른 스레드가 `accept()`(시간 제한 0.5 s)로 기다리는 듣기
  소켓을 그 대기가 끝날 때까지 열어 둔다. 그래서 `stop()` 직후 잠깐 그 포트에 접속된다. `close()` 앞에 `shutdown(SHUT_RDWR)` 를 부르면
  바로 깨운다. Tier 0 는 Linux 에서 돌므로 ai 파트에 넘긴다(이 티켓 범위 밖).
- Windows 폴더를 Docker 에 마운트해 ai 시험을 돌리면 gpu_queue·gpu_session1·crash_recovery 시험 4개가 실패한다(파일 시각·잠금 동작 차이).
  컨테이너 안 디스크로 복사하면 통과한다. CI 는 Linux 디스크에 checkout 하므로 해당하지 않는다.

## 검증

- `deploy/scripts/check_versions.sh`: 지금 판 모두 일치(exit 0). `.node-version` 을 24.20.0 으로 바꾼 사본에서 Node FAIL, exit 1.
- Jenkins 이미지 로컬 빌드: jenkins 계정으로 Node 24.19.0, pnpm 11.24.0(corepack), uv 0.11.27, Python 3.11.15(tkinter import 됨),
  psql 17.11, git 2.47.3, Java 21.0.12.
- EC2 기동(2026-10-22): `https://simai.live/jenkins/` Jenkins 2.580.1. jenkins 컨테이너에서 `ci-db` 접속됨, 운영 `db` 이름이 풀리지 않음,
  `/var/run/docker.sock` 없음. GitLab 연결 Test Connection 성공.
- 수동 빌드(이 브랜치, 모든 단계):
  - #2: server 시험 1건 실패 "too many clients"(ci-db 상한 25) → 상한 100·풀 5 로 고침.
  - #3: server 통과(49 s). front 시험 508개 통과, `pnpm build` 가 VITE 환경 변수 없음으로 실패 → front 단계에 운영과 같은 값을 줌.
  - #4: 판 점검·contracts·server(17 s)·front(104 s)·gateway(104 s, 596개) 통과. ai 4786 통과·1 실패(위 "검증에서 찾은 것" 첫 항목,
    ai 파트에 전달), 31분 55초.
- ai 시험 시간(로컬 Windows 28.7분): 4,828개 중 가장 느린 100개가 20.5분(71%). 시뮬레이터·실제 프로세스 종단 시험(G3·G6, Tier 0 복구,
  보정 드리프트·번들 조정, 시나리오 재생)이다. 너무 느리면 이것들에 `slow` 표시를 달아 MR 에서 빼고 dev push 에서만 돌린다(ai 파트와 합의).
- GitLab Webhook(Push·MR events) Test → 200, Jenkins 빌드 #7 이 "Started by GitLab push"(dev)로 시작됨. dev 에는 아직 `Jenkinsfile` 이
  없어 바로 실패했다(이 MR 이 머지되면 해소).
