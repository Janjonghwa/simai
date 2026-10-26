# 영향 받는 ai 시험만 고르는 스크립트 (S15P31A101-255)

MR 과 Owner 노트북에서 바뀐 파일로부터 정적 import 그래프·보조 모듈 전이·문자열 리터럴/경로 규칙상 영향 받는 `ai/tests/**` 시험만 골라 돌리는 선택기(`ai/scripts/select_tests.py`)와 그 규칙·실측·CI 적용 방안을 적는다.

관련 문서: 명세 21.3·21.4(단위·계약·통합 시험 및 CI), `docs/design/ci-pipeline.md`(EC2 Jenkins CI 파이프라인, S15P31A101-28), `ai/CONVENTION.md`.

## 1. 목적 (2026-10-23 결정: 로직 층 선택 실행)

- `ai/` 전체 pytest(`-m "not gpu"`)는 현재 시험 파일 **196개**(총 **5,235개** CPU 시험, `ci-pipeline.md` 최초 작성 시점 148개/4,828개)로 로컬 Windows 에서 약 **28.7분 이상**, EC2 Jenkins 컨테이너에서 **31분 55초**(약 **28~40분**)가 걸린다(`docs/design/ci-pipeline.md`).
- 모든 MR 마다 30분 이상 전체 시험을 돌리면 개발 피드백이 늦어지고, 관제 서비스와 같은 EC2 의 CPU 를 오래 점유한다.
- **2026-10-23 결정**: 시험 파일 수백 곳에 수동으로 `slow` marker 를 붙이거나 커버리지 DB 를 유지하는 대신, **정적 import 의존 그래프 + 시험 보조 모듈 전이 + 문자열/경로 보수적 규칙**으로 바뀐 코드가 영향을 주는 시험만 골라 MR 과 Owner 노트북에서 돌린다. 이 규칙으로도 잡히지 않는 완전 동적 실행 경로는 **새벽(Nightly) 전체 실행**과 `ALL` 조건 실행이 잡는다.

## 2. 코드와 시험 위치

| 구분 | 경로 | 역할 |
|---|---|---|
| 선택 스크립트 | `ai/scripts/select_tests.py` | 변경 파일 수집(`git diff -z`), `grimp` 그래프 + `ast` 역추적 + 보조 모듈/문자열/경로 규칙, 시험 경로 출력 |
| 설정 파일 | `ai/scripts/select_tests.json` | 상시 포함 시험(`always_include`) 및 경로 규칙 매핑(`path_rules`) |
| 단위·이력 시험 | `ai/tests/test_select_tests.py` | 합성 패키지 직접·간접 선택, 교차 디렉터리 보조 모듈 전이(F1), 외부 경로 규칙(F2), 동적·subprocess·데이터 문자열/경로 매칭(F3), 얕은 clone 방어(F4), 암묵적 `__init__.py`, CLI 종료 코드 0, `-z` 경로 파싱, `stderr` ALL 사유 로그 등 28개 시험 |

### 스크립트 위치 선정 근거 (`ai/scripts/select_tests.py`)

1. `ai/tests/architecture/test_import_rules.py` 의 `test_risk_contract_covers_every_feature_package()` 는 `ai/src/simai_ai/*/__init__.py` 하위 패키지 목록이 `ai/pyproject.toml` 의 `risk-isolated` 금지 목록과 정확히 일치할 것을 요구하며, `test_simai_ai_has_only_subpackages()` 는 `src/simai_ai` 직하위 단일 파일이나 비패키지 폴더를 금지한다.
2. 다른 MR(`-250`)이 `ai/pyproject.toml`·`ai/uv.lock` 을 수정 중이어서 본 티켓에서는 `ai/pyproject.toml` 을 건드리지 않는다. 또한 `simai-ai` 기본 패키지(`dependencies = []`)는 `grimp` 가 없는 EC2 Risk Engine 최소 컨테이너에도 설치된다.
3. 저장소 스크립트 관례(`contracts/scripts/`, `deploy/scripts/`)와 `ai/CONVENTION.md` 의 실행 방식(`ai/` 작업 디렉터리에서 `uv run python -m ...`)에 맞춰 `ai/scripts/select_tests.py` 에 두었다.
   - `ai/` 디렉터리에서: `uv run python -m scripts.select_tests`
   - 저장소 루트에서: `uv run --project ai python ai/scripts/select_tests.py`

## 3. 선택 규칙

1. **입력(변경 파일 목록)**
   - 기본 실행 시 `<base>...HEAD` (기본 `--base origin/dev`, 로컬에 `origin/dev` 가 없으면 `dev` 대체)의 브랜치 변경분과 현재 작업 트리의 staged·unstaged·untracked 파일을 합쳐 읽는다(`git diff --name-status -M -z` 및 `git ls-files --others --exclude-standard -z` 로 NUL 구분 파싱해 공백·비 ASCII·따옴표 경로도 깨지지 않는다).
   - 특정 커밋 검사(`--commit <rev>`)는 `<rev>^{commit}` 과 부모 `<rev>^^{commit}` 존재를 먼저 확인하고 `git diff --name-status -M -z <rev>^ <rev>` 로 읽는다(얕은 clone 등으로 부모가 없으면 예외를 발생시켜 CLI 에서는 `ALL` 로 안전 복귀). 파일 목록 직접 전달(CLI 인자 및 `select_tests(changed_files)` 함수)도 지원한다.
2. **`ai/src/simai_ai/**`·`ai/services/**` 변경**
   - 바뀐 `.py` 파일 경로를 모듈 이름(`simai_ai.geometry.triangulation`, `services.tier0.service` 등)으로 바꾼다.
   - `grimp.build_graph("simai_ai", "services")` 로 패키지 의존 그래프를 만들고, 바뀐 모듈을 직접·간접으로 import 하는 모든 하위(downstream, 즉 importer 방향) 모듈 집합을 구한다.
   - 패키지 `__init__.py` 가 직접 바뀌었으면 그 패키지와 모든 하위 모듈(`pkg.*`) 및 그것들을 import 하는 모듈을 영향 집합에 넣는다. 또한 하위 모듈 `pkg.core` 가 바뀌어 `pkg/__init__.py` 가 전이 영향 집합에 들어오면, `pkg.sibling` 을 import 할 때 파이썬이 `pkg/__init__.py` 를 먼저 실행하는 암묵적 패키지 초기화 의존성까지 반영해 `pkg.*` 를 import 하는 시험도 모두 선택한다.
   - 삭제되거나 이름이 바뀐 파일은 디스크에서 사라져 새 `grimp` 그래프에 없더라도 옛 모듈 이름으로 영향 집합에 넣고, `src/simai_ai`·`services` 에 대한 `ast` 스캔으로 옛 모듈을 import 하는 모듈을 보완한다.
3. **`ai/experiments/**`·`ai/sim/**` 변경**
   - `grimp` 루트(`simai_ai`, `services`) 밖이므로 `ai/experiments/**` 와 `ai/sim/**` 의 `.py` 파일들을 `ast` 로 정적 분석해 내부 모듈 간 import 전파를 수행한다.
   - `experiments/<실험>/` 디렉터리를 `sys.path` 에 넣고 단일 모듈 이름(예: `import fs3db_common`)으로 불러오는 시험(`tests/experiments/<실험>/**`)도 매칭한다.
4. **시험 파일(`ai/tests/**`) 및 교차 디렉터리 시험 보조 모듈 전이 (F1)**
   - `ai/tests/**` 아래의 비-시험 `.py` 파일(`test_*.py`·`*_test.py`·`conftest.py`·`__init__.py` 제외, 예: `recording_session.py`, `live_video.py`, `golden.py`, `tier0_site.py`, `site_driver.py`)을 디렉터리와 무관하게 파일 stem 과 `tests.*` 모듈 경로로 색인한다.
   - 보조 모듈끼리의 import 및 문자열 리터럴 참조에 대해 전이 닫힘(transitive closure)을 계산한다(예: `tests/scenarios/golden.py` → `from recording_session import ...` → `tests/io/recording_session.py` → `services.tier0.video_ingest`).
   - 각 시험 파일의 유효 import·문자열 집합에 상위 `conftest.py`(루트 `tests/conftest.py` 포함, 예: `live_video.py` fixture)와 전이 연결된 모든 보조 모듈의 import·문자열을 합산한다.
5. **동적 import·subprocess·데이터 파일에 대한 문자열 리터럴 휴리스틱 및 경로 규칙 (F2, F3)**
   - **문자열 리터럴 매칭**: 바뀐 파일(`ai/**`, `deploy/**` 등)의 stem(길이 3 이상), 파일명(`name.ext`), `ai/` 상대 경로, 저장소 루트 상대 경로, 모듈 점 표기(`sim.run`, `services.tier0.service` 등)를 추출해, 시험 파일·보조 모듈·`conftest.py` 안의 `ast.Constant` 문자열 리터럴에 토큰/경로 단위로 등장하면 해당 시험을 선택한다. 이로써 `_load("run_mapanything")`, `gpu_queue.py`, `site_driver.py`, `sim.run`, `sim/scenarios/*.json` 등 간접 참조 시험을 정적으로 포착한다.
   - **경로 규칙(`path_rules`, `ai/scripts/select_tests.json`)**:
     - `deploy/**` → `tests/deploy` (`deploy/` 스크립트·Dockerfile·Compose 변경 시 `ai/tests/deploy` 실행)
     - `ai/sim/**` → `tests/sim`
     - `ai/experiments/<name>/**` → `tests/experiments/<name>` (디렉터리가 존재할 때)
     - `ai/services/tier0/**` → `tests/deploy/test_session1_live.py`, `tests/deploy/test_crash_recovery_g3.py`
6. **바뀐 `ai/tests/**` 파일 직접 처리**
   - 바뀐 파일이 시험 파일(`test_*.py`, `*_test.py`)이고 디스크에 존재하면 그대로 포함한다(삭제된 시험 파일은 출력에서 제외).
   - 바뀐 파일이 시험 보조 파일(`conftest.py` 제외, 보조 `.py`·fixture·테스트 벡터 JSON 등)이면 같은 디렉터리(없으면 가장 가까운 상위 시험 디렉터리)의 모든 시험 파일을 포함하고, 그 보조 모듈을(같은 디렉터리든 다른 디렉터리든) 참조하는 다른 모든 시험도 전이 포함한다.
7. **항상 포함 (`always_include`)**
   - `ai/scripts/select_tests.json` 의 `always_include` 목록(현재 `["tests/architecture"]`)은 `ai/` 변경 또는 `path_rules` 매칭이 하나라도 있을 때 항상 결과에 포함한다.
8. **빈 출력**
   - `ai/`, `contracts/`, 그리고 `path_rules` 대상(`deploy/**` 등) 밖(`front/`, `server/`, `gateway/`, `docs/`, `AGENTS.md` 등)만 바뀌었거나 변경 파일이 없으면 빈 출력을 내고 종료 코드 `0` 으로 끝난다.

## 4. ALL 조건과 이유

아래 조건 중 하나라도 해당하면 부분 선택 대신 `stdout` 에 `ALL` 한 줄을 출력하고, `stderr` 에 `[select_tests] ALL: <사유>` 한 줄을 남겨 디버깅을 돕는다(종료 코드는 항상 `0`).

| ALL 조건 | 이유 |
|---|---|
| `ai/uv.lock`, `ai/pyproject.toml` | 서드파티 의존성 판, 패키지 구성, `[tool.pytest.ini_options]`·`[tool.importlinter]` 설정 변경은 전체 시험에 영향을 준다. |
| 어느 `conftest.py` (`ai/tests/**/conftest.py`) | 공통 fixture, 자식 프로세스 `PYTHONPATH` 훅(`winsock_retry`), 가짜 서버/시뮬레이터 구동 방식이 바뀌면 정적 분석만으로 영향 범위를 한정할 수 없다. |
| `contracts/**`, `ai/src/simai_ai/contracts/**` | 구간 계약 JSON Schema 와 생성 Python 모델은 Tier 0·Risk Engine·Worker·Provider·Simulator 전체가 공유하는 정본이다. |
| 스크립트 자신(`ai/scripts/select_tests.py`, `ai/scripts/__init__.py`)과 설정(`ai/scripts/select_tests.json`) | 선택 로직이나 규칙 설정이 바뀌었을 때는 선택기 자체의 회귀를 막기 위해 전체 시험을 확인한다. |
| 그래프 생성 실패·설정 파싱 오류·잘못된 CLI 인자 등 예외 발생 시 | 예상치 못한 오류나 잘못된 인자로 시험을 건너뛰거나 파이프라인이 중단되지 않도록 `stderr` 에 원인을 남기고 `stdout` 에 `ALL`, 종료 코드 `0` 을 반환한다. |

## 5. 놓칠 수 있는 경우와 보완

문자열 리터럴 휴리스틱과 `path_rules` 로 코드베이스 내 기존의 동적 로드(`_load("run_mapanything")`, `site_driver.py`), `subprocess` 실행(`sim.run`, `gpu_queue.py`, `deploy/scripts/*`), 시나리오 JSON(`sim/scenarios/*.json`) 결합은 모두 선택 결과에 포함된다.

다만 앞으로 새로 추가될 코드에서 파일 이름이나 모듈 경로를 문자열 상수로조차 남기지 않고 런타임 계산으로만 조합해 동적으로 불러오는 극단적인 경우가 있을 수 있다.

**보완 체계**:
- **매일 새벽(Nightly) 전체 실행**과 `dev`·`master` 브랜치 빌드, 그리고 계약·설정 변경 시의 **`ALL` 실행**이 정적/휴리스틱 선택기 밖의 모든 결합까지 전수 검증한다.

## 6. 최근 머지 커밋 실측 비교 (전체 196개 시험 파일 / 5,235개 CPU 시험 기준)

현재 코드베이스 기준 전체 시험 파일은 **196개**(`pytest -m "not gpu" --collect-only -q` 기준 **5,235개** 시험)이다. 아래 표는 최근 머지 커밋 8건에 대해 (1) 커밋 전체 변경 파일 기준과 (2) 시험 파일(`ai/tests/**`) 변경을 제외한 소스/외부 변경만 입력했을 때의 선택 결과, 커밋이 실제로 수정한 시험 파일 포함 여부, 그리고 대표 사례의 로컬 실측 시간을 정리한 것이다.

| 커밋 (티켓) | 변경 성격 | 전체 변경 입력 시 (경로 / 시험 파일 수) | `ai/tests/**` 제외 입력 시 (경로 / 시험 파일 수) | 커밋 수정 시험 포함 여부 | 대표 실행 시험 수 / 로컬 실측 시간 | 전체(196파일, 28~40분) 대비 |
|---|---|---:|---:|:---:|---:|---|
| `dcb56b1` (`-244`) | 기획서 v1.3 수정 27 (`docs/`, `AGENTS.md`만) | `0`개 경로 (`0` / 196 파일) | `0`개 경로 (`0` / 196 파일) | 해당 없음 (0/0) | `0`개 / **0초** (pytest 생략) | **100% 단축** (파일 0%) |
| `a38d925` (`-238`) | HMR K 입력 및 배포 스크립트 (`ai/experiments/hmr/**`, `deploy/**`) | `15`개 경로 (`15` / 196 파일) | `15`개 경로 (`15` / 196 파일) | **2 / 2 전부 포함** (`test_exp_images.py`, `test_hmr_k_input.py`) | `354 passed, 8 skipped` / **216.64초** (3분 37초) | **약 87~91% 단축** (파일 7.7%) |
| `145c4ee` (`-19`) | 분석 워커 대여·연장·제출 (`ai/services/worker/**`) | `9`개 경로 (`10` / 196 파일) | `9`개 경로 (`10` / 196 파일) | **3 / 3 전부 포함** (`test_client.py`, `test_main.py`, `test_runner.py`) | `187 passed` / **73.80초** (1분 14초) | **약 96~97% 단축** (파일 5.1%) |
| `9aef6c3` (`-251`) | Tier 0 영상 수집 (`ai/services/tier0/video_ingest.py`) | `19`개 경로 (`20` / 196 파일) | `19`개 경로 (`20` / 196 파일) | **1 / 1 포함** + 교차 디렉터리 보조 모듈(`recording_session`, `live_video`) 경유 6개 시험 모두 포함 | `354개 수집` / **402.80초** (6분 43초) | **약 76~83% 단축** (파일 10.2%) |
| `f8b1152` (`-236`) | Tier 0 기본 CPU 디코더 (`io/decoder.py`, `services/tier0/**`, `deploy/**`) | `71`개 경로 (`71` / 196 파일) | `71`개 경로 (`71` / 196 파일) | **3 / 3 전부 포함** (`test_session1_c3.py`, `test_session1_live.py`, `test_decoder_default.py`) | 71개 시험 파일 선택 | **파일 기준 63.8% 축소** (파일 36.2%) |
| `4df4b6e` (`-237`) | Tier 0 기하 배치 가속 (`triangulation`, `joint_gate`, `cross_view`) | `74`개 경로 (`75` / 196 파일) | `70`개 경로 (`71` / 196 파일) | **1 / 1 포함** (`test_geometry_speedup_regression.py`) | 75개 시험 파일 선택 | **파일 기준 61.7% 축소** (파일 38.3%) |
| `6393a88` (`-245`) | 삼각측량·보정 비유한 SVD 방어 (`geometry/*`, `cross_view`, `recalibration`) | `96`개 경로 (`97` / 196 파일) | `95`개 경로 (`96` / 196 파일) | **3 / 3 전부 포함** (`test_watch.py`, `test_geometry_speedup_regression.py`, `test_nonfinite_lapack.py`) | 97개 시험 파일 선택 | **파일 기준 50.5% 축소** (파일 49.5%) |
| `a420b5a` (`-249`) | 아바타 포즈 스트림 계약 (`contracts/**`, `simai_ai/contracts/models.py`) | `ALL` (`196` / 196 파일) | `ALL` (`196` / 196 파일) | **전체 포함** (`ALL`) | `5,235`개 전체 실행 (약 28~40분) | 계약 변경 안전 전체 실행 |

- 모든 과거 커밋에서 `ai/tests/**` 변경분을 입력에서 빼고 **소스/배포 파일만 넣었을 때도 해당 커밋이 함께 수정한 시험 파일들을 100% 빠짐없이 선택**했다(`full_missing_mod_tests == []`, `src_missing_mod_tests == []`).
- 특히 `9aef6c3`(`-251`, `video_ingest.py`)은 `tests/io/recording_session.py` 와 `tests/services/tier0/live_video.py` 를 다른 디렉터리에서 bare import 하는 `tests/scenarios/test_scenario_golden.py`, `tests/io/test_pyav_decoder.py`, `tests/calibration/extrinsics/test_extract.py`, `tests/deploy/test_session1_c3.py` 등까지 모두 포함한다.

## 7. CI 및 Owner 적용 방안

### CI 적용 제안 (`Jenkinsfile` — 곽나혁 님 S15P31A101-28 연동)

> **2026-10-26 변경(S15P31A101-269)**: Jenkins 는 빌드를 한 줄로 하나씩 돌려 `ALL` 빌드(30분대) 하나가 뒤 MR 을 막았다. 그래서 지금 Jenkins 의 MR·dev push 빌드는 ai `pytest` 를 돌리지 않고(`lint-imports` 까지만), 새벽 작업 `simai-nightly-ai`(매일 02:00 KST, `FULL_AI_TESTS=true`)만 전체를 돈다. 아래 MR 선택 실행은 Jenkins 에서는 쓰지 않고 Owner 노트북에서 쓴다. Jenkins 를 동시 실행으로 바꾸면 MR 선택 실행을 다시 붙일 수 있다.

1. **MR 빌드 (`env.gitlabMergeRequestId != null`)**:
   `ai` 단계(`dir('ai')`)에서 `lint-imports` 를 돌린 뒤 `select_tests` 결과에 따라 분기한다(`stderr` 로 출력되는 `[select_tests] ALL: ...` 사유는 빌드 로그에 그대로 남고 `SELECTED` 변수에는 `stdout` 만 담긴다).

   ```sh
   uv sync --locked --extra tier0 --extra worker
   uv run lint-imports
   SELECTED=$(uv run python -m scripts.select_tests --base "origin/${gitlabTargetBranch:-dev}")
   if [ -z "$SELECTED" ]; then
     echo "선택된 ai 시험 없음 (lint-imports 만 실행)"
   elif [ "$SELECTED" = "ALL" ]; then
     uv run pytest -m "not gpu" -q
   else
     uv run pytest -m "not gpu" -q $SELECTED
   fi
   ```

2. **새벽(Nightly) 정기 빌드 및 `dev`·`master` push 빌드**:
   - 선택기를 거치지 않고 전체 `uv run pytest -m "not gpu" -q` 를 실행해 전수 검증한다.

### Owner 노트북 적용

- Owner 가 로컬에서 작업할 때도 `ai/` 에서 아래 한 줄로 영향 받는 시험만 바로 골라 실행할 수 있다.

  ```sh
  # 변경된 시험 목록 확인
  uv run python -m scripts.select_tests

  # PowerShell (Windows)에서 선택된 시험만 실행 (ALL 이면 전체, 비어 있으면 생략)
  $sel = uv run python -m scripts.select_tests; if ($sel -eq "ALL") { uv run pytest -m "not gpu" -q } elseif ($sel) { uv run pytest -m "not gpu" -q $sel }
  ```

## 8. 버린 대안

- **`slow` pytest marker 수백 개 수동 부착**: 시험 파일 196개(5,235개 시험)에 일일이 표시를 달고 관리하는 부담이 크고, 진행 중인 다른 MR 들과 시험 파일 충돌이 잦다. 무엇보다 느린 통합·시뮬레이터 시험이 검증하는 대상 모듈(기하·복구·Tier 0)을 고쳤을 때 MR 에서 그 시험을 빼버리면 정작 잡아야 할 회귀를 놓친다.
- **`pytest-xdist` 병렬 실행**: CI 가 도는 EC2 인스턴스는 관제 서비스와 함께 자원을 쓰며 버스트 CPU 및 컨테이너 메모리 상한(4 GB)이 걸려 있다. 다중 워커로 프로세스를 여러 개 띄우면 CPU 크레딧 고갈·메모리 압박과 로컬 소켓/타이밍 시험의 간헐 실패가 심해진다.
- **`pytest-testmon` (커버리지 DB 기반 시험 선택)**: 빌드마다 `.testmondata` 커버리지 DB 를 기록·보존·공유해야 하고, 자식 프로세스를 띄우는 시험이 많은 저장소 구조상 커버리지 추적 오버헤드와 캐시 정합성 유지 부담이 크다. `grimp` + `ast` 정적 분석은 별도 DB 없이 결정론적으로 같은 결과를 낸다.

## 9. 검증 및 넘긴 일

### 검증

- `uv run pytest tests/test_select_tests.py -q`: 28개 단위·회귀·이력 시험 통과 (직접·간접 import, 하위 모듈 구분, `__init__.py` 변경 및 암묵적 패키지 초기화 의존성, 상대 import, 교차 디렉터리 시험 보조 모듈 전이 F1, `deploy/**` 외부 경로 규칙 F2, 동적·subprocess·데이터 파일 문자열/경로 매칭 F3, 얕은 clone 이력 시험 방어 F4, CLI 인자 오류 시 종료 코드 0 + `ALL` 복귀, `git -z` 특수 경로 파싱, `ALL` 사유 `stderr` 출력).
- `uv run pytest tests/architecture -q`: 의존 방향 및 통합 환경 시험(47 passed) 전부 통과.
- `PYTHONUTF8=1 uv run lint-imports`: 6개 계약 전부 Kept.
- `uv lock --check`: `ai/uv.lock` 불변 확인.

### 넘긴 일

- `Jenkinsfile` 의 `ai` 단계에 위 MR 선택 실행 스니펫과 새벽 전체 실행 스케줄을 반영하는 작업은 CI 담당(곽나혁 님, `S15P31A101-28`)에게 제안을 전달해 반영한다.
