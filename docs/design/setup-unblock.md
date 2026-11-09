# 세팅 차단 해소: 워커 `site_setup` 지연 예산 예외 및 DINOv2 오프라인 허브 로드 (S15P31A101-566)

- **티켓**: `S15P31A101-566`
- **명세 절**: 3.2 (GPU 공유·우선 접수 문), 6.6 (지연 예산), 15.1 (정적 장면 재구성 MapAnything), 20.5 (안전 경로 격리)
- **코드 경로**:
  - `ai/src/simai_ai/gpu/admission.py` (`SETUP_JOB_TYPE`, `AdmissionPolicy.setup_lag_budget_ms`, `Admission.admit_types`, `Admission.admits`, `AdmissionGate`)
  - `ai/src/simai_ai/gpu/budget.py` (`WorkerGpuSettings.from_env`, `SIMAI_WORKER_SETUP_LAG_BUDGET_MS`)
  - `ai/services/worker/runner.py` (`Worker._lease_gated`, `Worker._hold`)
  - `ai/src/simai_ai/scene/run_mapanything.py` (`resolve_local_hub_dir`, `use_local_dinov2_hub`)
  - `ai/experiments/static_scene/setup_runpod.sh` (`facebookresearch_dinov2_main` 로컬 캐시 고정)
  - `deploy/scripts/check_image.sh`, `deploy/scripts/exp_image_check.py`
- **테스트 경로**:
  - `ai/tests/gpu/test_admission.py`
  - `ai/tests/services/worker/test_runner_tier0_priority.py`
  - `ai/tests/experiments/static_scene/test_known_k_session3.py`

---

## 1. 배경 및 13차 세션 실측 근거

13차 GPU 세션(`C:/Users/SSAFY/simai-local/session13/RESULTS.md`)에서 현장 세팅(`site_setup`)이 두 가지 원인으로 연속 차단·실패했다.

1. **워커 우선 접수 문(`AdmissionGate`)의 `site_setup` 지연 차단**:
   - `worker-manual-s13.log` 실측: `Tier 0 우선 접수 문: 받지 않음 (Tier 0 지연 845ms > 예산 600ms)` 및 `Tier 0 우선 접수 중지 1~10분째: Tier 0 지연 776~955ms > 예산 600ms` 상태로 10분 이상 `site_setup` 작업을 대여하지 못했다.
   - `lobby-s13.log.gz` 실측: 서버는 이미 진행 중(`IN_PROGRESS`)인 작업이 있으면 새 세팅 시작 요청을 HTTP 409(`CONFLICT`)로 막는다. 즉, 세팅을 시작하는 시점에는 LIVE 판정과 경합하는 진행 중 분석 작업이 없으며, 세팅(`site_setup`)이 끝나야 현장 관제 자체가 성립한다.
2. **MapAnything DINOv2 `torch.hub` GitHub 504 타임아웃**:
   - `pod.tgz`(`mapanything.log`, `mapanything.error.json`) 실측: `site_setup` 2차(`09:07:37Z`)·3차(`09:20:39Z`) 시도 모두 MapAnything 단계에서 `Loading pretrained dinov2_vitg14 from torch hub` 직후 `urllib.error.HTTPError: HTTP Error 504: Gateway Time-out`으로 실패했다.
   - 원인: 통합 이미지(`deploy/tier0/Dockerfile`)와 MapAnything 이미지(`deploy/mapanything/Dockerfile`) 모두 `/root/.cache/torch/hub/facebookresearch_dinov2_main`에 DINOv2 허브 코드를 내장하고 있으나, `uniception`이 `torch.hub.load("facebookresearch/dinov2", model_name, pretrained=False)`(기본 `source="github"`)를 호출할 때 `torch.hub._parse_repo_info`가 GitHub에 기본 브랜치를 질의한다. PyTorch `torch.hub`는 `URLError`(망 단절)일 때만 로컬 캐시로 폴백하고, `HTTPError`(HTTP 504 등)는 `URLError` 분기보다 먼저 잡아 그대로 예외를 던진다.

---

## 2. 선택한 설계와 값의 근거

### 2.1 워커 `site_setup` 지연 예산 예외 (`SIMAI_WORKER_SETUP_LAG_BUDGET_MS`)

- **정책 모델**:
  - `AdmissionPolicy`에 `setup_lag_budget_ms: int | None = None`을 추가하고, `WorkerGpuSettings.from_env`에서 환경변수 `SIMAI_WORKER_SETUP_LAG_BUDGET_MS`(빈 값 또는 `"-"`이면 `None`=무제한, 양의 정수면 상한 ms)로 읽는다.
  - `Admission`에 `admit_types: frozenset[str] = frozenset()`과 `admits(job_type: str) -> bool`을 추가한다. `allowed_types(types)`는 `[t for t in types if self.admits(t)]`를 돌려준다.
- **허용 조건(안전 불변식 유지)**:
  - 다음 전제 조건이 **모두** 참일 때만 `admit_types`에 `site_setup`을 넣는다:
    1. 표시 파일(`tier0.live`)이 존재하고 `max_marker_age_s`(15초) 이내이며 JSON·`gpu_priority` 형식이 정상(`MARKER_ABSENT`·`MARKER_STALE`·`MARKER_UNREADABLE`·`LIVE_LINGER` 아님)
    2. `phase == "running"` (`NOT_RUNNING` 아님)
    3. `reserve.state in {"OK", "SKIPPED", "DISABLED"}` (`RESERVE_NOT_OK` 아님)
    4. 지연 표본이 존재하고 모르는 표본이 없음 (`samples > 0`, `unknown == 0`, `lag_ms_max` 유한 숫자 — `NO_SAMPLES`·`LAG_UNKNOWN` 아님)
    5. `setup_lag_budget_ms is None` 이거나 `lag_ms_max <= setup_lag_budget_ms`
  - 위 전제가 참이면 일반 작업이 `OVER_BUDGET`(`lag_ms_max > lag_budget_ms`), `QUEUE_WAIT_EXCEEDED`, 또는 `STABILIZING`(`resume_after_s` 대기)으로 막혀 있을 때도 `site_setup`은 대여(`_lease_gated`)와 시작 직전 재확인(`_hold`)을 모두 통과한다.
  - 다른 작업 유형(`vlm_rationale`, `tbm_report`, `static_reconstruct`, `hmr_refine`, `hmr_appearance_update` 등)의 판정과 `Tier 0 우선 접수 문: 받지 않음` 로그 형식은 그대로 유지한다.

### 2.2 DINOv2 로컬 허브(`source="local"`) 우선 로드

- `ai/src/simai_ai/scene/run_mapanything.py`에 `resolve_local_hub_dir`와 `use_local_dinov2_hub`를 둔다.
- `resolve_local_hub_dir("facebookresearch/dinov2")`는 다음 순서로 `hubconf.py`가 있는 로컬 허브 사본을 찾는다:
  1. `SIMAI_DINOV2_HUB_DIR` 환경변수(명시 지정 시)
  2. `Path(torch.hub.get_dir()) / "facebookresearch_dinov2_main"`
  3. `Path(TORCH_HOME) / "hub" / "facebookresearch_dinov2_main"`
  4. `~/.cache/torch/hub/facebookresearch_dinov2_main` 및 `/root/.cache/torch/hub/facebookresearch_dinov2_main`
- 로컬 사본이 존재하면 `use_local_dinov2_hub()`가 `torch.hub.load("facebookresearch/dinov2", ...)` 호출(`source="github"`, `force_reload=False`)을 `orig_load(str(local_dir), ..., source="local")`로 바꿔 GitHub HTTP 요청 자체를 하지 않는다.
- 볼륨 설치 스크립트(`ai/experiments/static_scene/setup_runpod.sh`)에도 이미지와 동일한 커밋(`DINOV2_COMMIT=7764ea0f912e53c92e82eb78a2a1631e92725fc8`)을 `${TORCH_HOME:-$HOME/.cache/torch}/hub/facebookresearch_dinov2_main`에 받아 두도록 맞췄다.
- `deploy/scripts/check_image.sh` 5c절에서 `--network none`뿐 아니라 `urllib.request.urlopen`이 `HTTPError 504`를 던지는 상태에서도 `use_local_dinov2_hub()`로 `dinov2_vits14` 골격이 생성되는지 검증한다.

---

## 3. 버린 대안

1. **`SIMAI_WORKER_LAG_BUDGET_MS` 전체 상향(예: 600ms → 1000ms)**:
   - LIVE 관제 중 `vlm_rationale`·`tbm_report`까지 지연 초과 상태에서 무분별하게 실행되어 명세 3.2·6.6의 Tier 0 지연 목표를 해친다. 세팅(`site_setup`)에만 예외를 두는 편이 격리 원칙에 맞다.
2. **표시 파일 없음/만료/지연 모름(`LAG_UNKNOWN`)에서도 `site_setup` 무조건 허용**:
   - Tier 0 기동 중이거나 GPU 선확보(`reserve`)가 깨진 상태에서 MapAnything·AnyCalib이 먼저 GPU 메모리를 점유하면 Tier 0 엔진이나 선확보가 OOM으로 죽을 수 있다. 따라서 표시 파일 정상·예약 OK·지연 표본 정상 조건은 유지했다.
3. **외부 패키지(`uniception`) 소스 코드 직접 수정**:
   - 통합 이미지는 잠금 휠(`uniception==0.1.7`)을 설치하므로 외부 휠 내부 파일을 패치하면 빌드·잠금 재현성이 깨진다. 우리 진입점(`run_mapanything.py`)에서 `torch.hub.load`를 얇게 감싸는 방식이 외부 코드를 건드리지 않는 최소 변경이다.

---

## 4. 남은 위험

1. **Tier 0 자체가 멈췄거나 시계 동기화 전(`LAG_UNKNOWN`)인 경우**:
   - 안전 불변식(표시 파일 정상·GPU 예약 OK·지연 모름 아님)을 유지했으므로, Tier 0가 첫 지연 표본을 내기 전(기동 직후 수 초)이나 비정상 종료 직후(`live_linger_s` 60초)에는 `site_setup`도 대기한다.
2. **극단적 GPU 과부하 시 상한 필요 가능성**:
   - 기본값은 `SIMAI_WORKER_SETUP_LAG_BUDGET_MS` 미설정(무제한)이다. 운영 중 특정 지연 상한(예: `2000`ms) 이상에서는 세팅도 보류해야 한다면 컨테이너 환경변수 `SIMAI_WORKER_SETUP_LAG_BUDGET_MS=2000`으로 코드 변경 없이 즉시 제한할 수 있다.
