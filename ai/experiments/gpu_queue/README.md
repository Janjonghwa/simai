# GPU 실험 큐와 Run 기록 (S15P31A101-36)

GPU Pod 에서 실험 Job 을 하나씩 순서대로 돌리고, Job 마다 Run 기록(`contracts/experiments/run_record.schema.json`)을 남기는 도구다.
근거: 명세 20.5(GPU Job Queue·잠금·TTL/heartbeat·타임아웃·메모리 회수·재시작 복구, 재현·측정), 3.2(Tier 0 와 분석 워커가 같은 GPU, Tier 0 자원 우선), 21.5(이미지 다이제스트를 `env_hash` 로), AGENTS.md(GPU 는 검증할 때만 켜고, LIVE 관제 시간에 EXP Job 을 돌리지 않는다).

GPU 를 켜기 전에 노트북에서 Job 파일을 완성하고 `check` 로 형식을 확인한다. Pod 에서는 큐만 돌리고 끝나면 바로 Stop 한다.

## 파일

| 파일 | 하는 일 |
|---|---|
| `gpu_queue.py` | 큐 CLI: `run`(실행), `check`(노트북 확인), `stamp`(git 정보 찍기), `status`(상태 보기) |
| `run_record.py` | Run 기록 조립: git, GPU(nvidia-smi), env 해시, 가중치·데이터·설정 해시, 결과 URI, 스키마 검증 |
| `jobs.example.json` | Job 파일 예시 (`queue_egate0.sh` 의 H2·H4 를 옮긴 형태) |
| `tier0_priority_probe.py` | Tier 0 GPU 우선 보호 Pod 측정: `load`(더미 분석 부하), `watch`(Tier 0 표시 파일의 지연 표본 판정). `docs/design/tier0-gpu-priority.md` |

표준 라이브러리만 쓴다. 스키마 검증에만 `jsonschema`·`referencing` 이 필요하며, `uv run gpu_queue.py ...` 로 실행하면 스크립트 머리의 PEP 723 선언으로 자동 설치된다.
`python3 gpu_queue.py` 로 돌리고 jsonschema 가 없으면 기록은 남기되 검증을 건너뛰었다고 로그에 쓴다.

## 쓰는 순서

```sh
# 1) 노트북: Job 파일 확인 (GPU·상태 폴더를 건드리지 않음)
cd ai
uv run python experiments/gpu_queue/gpu_queue.py check my_jobs.json
#    Pod 에 저장소 없이 파일만 복사한다면 노트북 git 정보를 찍은 사본을 보낸다
uv run python experiments/gpu_queue/gpu_queue.py stamp my_jobs.json -o my_jobs.stamped.json

# 2) Pod: 큐 실행 (상태 폴더 기본값 $SIMAI_ROOT/queue = /workspace/simai/queue, Network Volume)
nohup uv run setup/gpu_queue/gpu_queue.py --contracts setup/contracts run my_jobs.stamped.json > logs/queue.log 2>&1 &
uv run setup/gpu_queue/gpu_queue.py status my_jobs.stamped.json      # 잠금·heartbeat·Job 상태

# 3) 끝나면 결과(runs/)를 노트북으로 가져오고 Pod 를 Stop 한다
```

`--repo` 는 git 정보를 읽을 저장소(기본: 이 스크립트가 있는 저장소), `--contracts` 는 스키마 폴더(기본: 저장소의 `contracts/`)다.
Pod 에 저장소가 있으면 git 정보를 저장소에서 직접 읽고, 없으면 `stamp` 로 넣은 `git` 값을 쓴다. 둘 다 없으면 시작하지 않는다(종료 코드 4).

## Job 파일 (JSON)

```jsonc
{
  "queue": {
    "name": "exp02-egate0",            // 상태 하위 폴더 이름
    "workdir": "/workspace/simai",     // 명령·상대 경로의 기준 (기본: Job 파일 폴더)
    "continue_on_failure": true,       // 실패해도 다음 Job 진행 (Job 별로 덮을 수 있음)
    "poll_s": 1, "kill_grace_s": 10,   // 감시 주기, 중단 시 TERM → KILL 사이 대기
    "vram_wait_s": 300,                // 필요 VRAM 이 빌 때까지 기다리는 최대 시간
    "gpu_query_cmd": ["nvidia-smi"],
    "lock": { "heartbeat_s": 15, "ttl_s": 120 },
    "live_guard": { ... },             // 아래 "LIVE 관제 보호"
    "volume": { "root": "/workspace", "datacenter": "AP-JP-1" }  // 결과 URI 를 volume://AP-JP-1/... 로
  },
  "record": { ... },                   // 모든 Job 의 Run 기록 기본값
  "git": { "commit": "...", "branch": "...", "dirty_worktree": false },  // stamp 가 넣는다
  "jobs": [
    {
      "name": "H2_bench_opt",          // 영문·숫자·._-, 큐 안에서 유일 (재시작 때 이 이름으로 끝난 Job 을 안다)
      "command": ["envs/x/bin/python", "setup/hmr/benchmark.py", "--preset", "opt"],  // 인자 목록, 또는 문자열(bash -c)
      "expected_s": 600,               // 예상 시간 (계획 출력, 넘기면 경고)
      "timeout_s": 1800,               // 넘기면 프로세스 트리를 끝내고 FAILED
      "vram_mb": 20000,                // 필요 VRAM. 0 이면 확인 안 함. nvidia-smi 가 없으면 확인 생략
      "cwd": ".", "env": { "KEY": "v" },
      "continue_on_failure": true,
      "record": { ... }                // 이 Job 의 Run 기록 값 (queue 기본값 위에 덮음)
    }
  ]
}
```

`record` 에 들어가는 값과 Run 기록 필드:

| Job 파일 `record` | Run 기록 | 비고 |
|---|---|---|
| `experiment_id`, `e_gate`, `jira_key`, `owner`, `experiment_state`, `model_version`, `schema_version` | 같은 이름 | `schema_version` 기본 2.1.0(run_record 계약 버전). 검증·운영 Run 은 `experiment_id` `VAL-01`(`e_gate` 없음). `e_gate` 외 필수 |
| `input_provenance` | `input_provenance` | **필수.** Job 정의에서 그대로 받는다. 정답·시뮬레이션 입력이면 `is_upper_bound: true` |
| `dataset`: 경로 목록 | `input_provenance.frames.dataset_hash` | 이미 적혀 있으면 덮지 않음 |
| `weights`: 경로 목록 | `weights_hash` | 파일 하나면 그 해시, 여럿·폴더면 (경로, 해시) 목록의 해시. 없는 경로가 있으면 비운다 |
| `config`: `{path, file?, overrides?}` | `config` | `path` 는 저장소 상대 경로, `file` 은 Pod 의 실제 위치. 없으면 Job 파일 자체를 설정으로 기록 |
| `env`: `{image_digest?, lock_files?, freeze_cmd?}` | `env_hash` | 이미지 다이제스트만 있으면 그 값, 여럿이면 묶은 해시(구성은 `notes`). 아무것도 없으면 빈 문자열 |
| `gpu`: `{provider, region, datacenter, shared_with_tier0}` | `gpu` | 모델명·드라이버·CUDA 는 nvidia-smi 에서. 없으면 빈 문자열. `provider` 기본: `RUNPOD_POD_ID` 가 있으면 RUNPOD, `VAST_TCP_PORT_22` 가 있으면 VAST, 아니면 LOCAL(사이트 미정 클라우드는 Job 에 OTHER) |
| `results`: `[{kind, path}]` | `results` | 끝난 뒤 있으면 URI·sha256. Job 로그는 항상 `LOG` 로 들어간다 |
| (큐) | `git_commit`, `branch`, `dirty_worktree` | 저장소 또는 `stamp` |
| (큐) | `started_at`, `finished_at`, `status`, `failure_reason` | UTC ms. 끝난 시각을 모르면(큐가 죽음) `finished_at` 을 비운다 |
| (큐) | `notes` | 큐·Job 이름, 시도 번호, **종료 코드**, 명령, 해시 구성, 경고 (스키마에 종료 코드 칸이 없어 여기에 둔다) |
| (Job 이 남김) | `metrics`, `performance` | Job 이 `$SIMAI_RUN_DIR/metrics.json`(스칼라만), `performance.json` 에 쓴 값 |

Job 프로세스에는 `SIMAI_RUN_ID`, `SIMAI_RUN_DIR`(이 Run 의 폴더), `SIMAI_QUEUE_NAME`, `SIMAI_QUEUE_TOKEN` 이 주어진다.
`SIMAI_QUEUE_TOKEN` 은 프로세스 트리를 찾는 표식이라 Job 이 지우면 안 된다.

## 상태 폴더

```
$SIMAI_QUEUE_DIR (기본 $SIMAI_ROOT/queue)
  queue.lock                    잠금 + heartbeat (주인 host·pid·token, 현재 Job)
  hash_cache.json               (경로, 크기, 수정 시각) → sha256. 큰 가중치를 매번 다시 읽지 않는다
  <queue>/state.json            Job 별 상태·시도 수·run_id 목록
  <queue>/queue.log
  <queue>/runs/<run_id>/job.log, run_record.json, metrics.json(Job 이 쓰면)
```

`run_id` 는 `<queue>-<job>-<UTC 시각>-a<시도>` 다. 시도마다 새 폴더·새 기록을 만들고 이전 기록은 덮지 않는다.

## 동작 규칙

- **순서:** Job 파일 순서대로 하나씩. 시작 전에 모든 Job 의 Run 기록을 미리 만들어 스키마를 통과하는지 본다(실패하면 GPU 작업 없이 종료 코드 4).
- **잠금:** `queue.lock` 을 `O_CREAT|O_EXCL` 로 만든다. 다른 큐가 잡고 있으면 바로 종료(코드 2). heartbeat 가 TTL 보다 오래됐거나, 같은 호스트에서 주인 pid 가 없으면 회수한다. 잠금을 잃으면(다른 큐가 회수) 돌던 Job 을 끝내고 종료한다.
- **타임아웃·정리:** Job 은 새 세션(프로세스 그룹)으로 띄운다. 타임아웃·중단 때는 그룹에 TERM → `kill_grace_s` 뒤 KILL, 그다음 `SIMAI_QUEUE_TOKEN` 이 남은 프로세스(setsid 로 그룹을 벗어난 자손)를 KILL 한다. 정상 종료 뒤에도 같은 정리를 해서 GPU 메모리를 잡은 프로세스가 남지 않게 한다.
- **재시작 복구:** `succeeded`·`failed` Job 은 다시 돌지 않는다(`--rerun JOB`, `--rerun-failed` 로만). `running` 으로 남은 Job(큐가 죽음)은 남은 프로세스를 표식으로 찾아 끝내고, `finished_at` 없는 ABORTED 기록을 남긴 뒤 다시 돈다. `aborted`(LIVE 중단, 신호, VRAM 부족)도 다시 돈다.
- **VRAM:** `vram_mb` 만큼 비어 있지 않으면 `vram_wait_s` 동안 기다리고, 그래도 없으면 ABORTED 기록을 남기고 다음 Job 으로 간다.
- **신호:** SIGTERM·SIGINT 를 받으면 돌던 Job 을 끝내고 ABORTED 기록, 잠금을 풀고 종료(코드 130). 그 Job 은 다음 실행 때 다시 돈다.

종료 코드: 0 전부 성공, 1 실패한 Job 또는 스키마를 통과하지 못한 기록이 있음, 2 잠금, 3 LIVE 관제로 멈춤, 4 Job 파일 오류, 130 신호.

## LIVE 관제 보호

LIVE 관제 시간에는 EXP Job 을 돌리지 않는다(명세 20.5, 3.2). 아래 중 하나라도 맞으면 LIVE 로 본다.

| 설정 (`queue.live_guard`) | 기본값 | 확인 |
|---|---|---|
| `marker_files` | `$SIMAI_ROOT/state/tier0.live` | 파일이 있으면 LIVE. Tier 0 는 기동 첫 단계(GPU 선확보 전)에 `phase=starting` 으로 쓴다. `marker_max_age_s` 를 주면 그보다 오래 갱신되지 않은 파일은 무시(Tier 0 비정상 종료 대비). 파일 확인이 '없음' 말고 다른 오류로 실패하면 모름 → LIVE |
| `process_patterns` | `-m\s+services\.tier0\b`, `/simai-tier0(\s\|$)` | Linux `/proc` 명령줄 정규식. 두 번째는 Tier 0 이미지 진입점(엔진 준비 동안도 LIVE). 큐 자신과 큐가 띄운 Job 은 제외 |
| `check_command` | 없음 | 인자 목록만(문자열 셸 명령은 Job 파일 오류: 셸이 '명령 없음'을 일반 종료 코드로 바꿔 구별할 수 없다). 종료 코드 0 이면 LIVE, `not_live_exit_codes`(기본 `[1]`)면 LIVE 아님, 실행 실패·시간 초과·그 밖의 코드는 모름 → LIVE (예: `["test", "-f", "/path"]`) |
| `on_live` | `wait` | `wait`: 끝날 때까지 `max_wait_s` 만큼 기다림 / `stop`: 큐를 멈추고 종료 코드 3 |
| `enabled` | `true` | `false` 는 관제가 절대 없는 개발 Pod 에서만 |

Job 시작 전에 확인하고(VRAM 을 기다린 뒤 띄우기 직전에 한 번 더), 도는 동안에도 확인한다(첫 확인은 Job 감시 주기 `queue.poll_s`, 그 뒤 `live_guard.poll_s` 마다). VRAM 을 기다리는 동안 LIVE 가 시작되면 VRAM 이 비어도 띄우지 않는다. 도중에 LIVE 가 시작되면 Job 을 끝내고 ABORTED(`LIVE 관제 시작으로 중단`) 기록을 남긴 뒤, `wait` 면 LIVE 가 끝나고 같은 Job 을 다시 돈다.
표시 파일은 Tier 0 서비스(`services/tier0/service.py`)가 기동 첫 단계에 만들고 5초마다 갱신하며 정상 종료 때 지운다. 같은 파일의 `gpu_priority` 칸은 분석 워커 접수 문이 읽는다(`docs/design/tier0-gpu-priority.md`).

## 기존 큐 스크립트 옮기기

`ai/experiments/hmr/queue_min.sh`·`queue_egate0.sh` 는 그대로 둔다. 다음 GPU 세션부터 이 큐로 옮길 때:

1. 스크립트의 각 줄(`run $PY $S/benchmark.py ...`)을 `jobs[]` 하나로 만든다. `command` 는 인자 목록으로, `$PY`·`$S` 는 실제 경로(`envs/fast_sam3db/bin/python`, `setup/hmr/...`)로 풀고 `workdir` 를 `${SIMAI_ROOT:-/workspace/simai}` 로 둔다.
2. `| grep ...` 필터는 뺀다. 전체 출력이 `runs/<run_id>/job.log` 에 남고 `queue.log` 에 시작·끝·이유가 남는다.
3. `timeout 900 ... live_server.py` 같은 줄은 `timeout_s: 900` 으로 옮긴다(시간이 다 되면 FAILED 로 기록되므로, 시간 제한이 정상 종료인 라이브 서버는 서버 쪽 `--duration` 같은 옵션으로 끝내는 편이 낫다).
4. 설치 스크립트를 기다리는 `while pgrep ...` 은 설치 자체를 첫 Job 으로 넣고 `continue_on_failure: false` 로 둔다.
5. 각 Job 의 `record` 에 `model_version`, `weights`, `dataset`, `config`(`path` 는 `ai/experiments/hmr/...`, `file` 은 `setup/hmr/...`)를 적는다. `jobs.example.json` 이 H2·H4 를 옮긴 예다(`jira_key`·`vram_mb` 는 실제 값으로 채운다).
6. 노트북에서 `check` → `stamp` → Pod 로 복사 → `run`.

## 한계

- 프로세스 명령줄 LIVE 확인, 그룹을 벗어난 자손 정리, 큐가 죽은 뒤 남은 프로세스 찾기는 Linux(`/proc`) 기준이다. Windows(노트북 시험)에서는 `taskkill /T` 로 Job 프로세스 트리만 끝내고, 정상 종료 뒤 남은 자손은 찾지 못한다.
- 잠금은 파일 생성의 원자성에 기댄다. 두 Pod 가 같은 Network Volume 을 동시에 붙이는 구성은 쓰지 않으므로(GPU Pod 하나) 충분하다고 본다.
- `performance.vram_peak_mb` 는 큐가 재지 않는다(같은 GPU 의 다른 프로세스와 구분하기 어려움). Job 이 `performance.json` 으로 남긴다.

## 시험

`ai/tests/experiments/gpu_queue/` (노트북, GPU·RunPod 없이): 샘플 CPU Job 두 개 순서 실행과 Run 기록 스키마, nvidia-smi 대역으로 GPU 정보·VRAM 확인, 타임아웃과 손자 프로세스 정리, 실패 시 중단, 잠금(동시 실행·TTL 회수), 큐 강제 종료 뒤 재시작 복구, LIVE 표시 파일(stop·wait·도중 중단), VRAM 대기 중 LIVE 시작, LIVE 확인 명령 실행 실패, 진입점 명령줄 정규식, Job 파일 확인, stamp.
Linux 전용 시험(프로세스 명령줄 LIVE, setsid 자손 정리, SIGTERM)은 Windows 에서 건너뛴다.

```sh
cd ai && uv run pytest tests/experiments/gpu_queue
```
