# S15P31A101-700 GPU 자동 부팅(Pod 쪽): 켜면 사람 손 없이 관제 준비까지

관련 명세: 3.2(Tier 0 우선·GPU 예산), 3.12(GPU 실행 사이트·저장 위치·"한 시점 GPU 인스턴스 하나"), 12.2·12.5(I1·워커 토큰), 14(비밀 값).
사용자 결정 2026-11-14: 팀원 누구나 GPU 를 켜면 세션이 준비된다(RunPod 먼저, 나중 AWS 서울 GPU 도 같은 경로). 앞 티켓 -699(서버 등록, `docs/design/S15P31A101-699-gpu-session-register.md`).
뒤 티켓: -701 게이트웨이 자동 전환, -702 공급자 API·화면 켜기/끄기·자동 정지, -703 AWS 뼈대.

## 지금(15차) → 이 티켓 뒤

| 15차 손 작업(약 16분) | 이 티켓 뒤 |
|---|---|
| `worker-token.sh issue` + `prep-pod.sh` 토큰·시작 묶음 복사 | 등록 응답 토큰 → 권한 600 파일, `start` → 시작 묶음 |
| Re-ID 1 GB 를 노트북 → EC2 점프로 업로드 | GPU 캐시(S3 서울)에서 받기. 처음 한 번만 손으로(또는 `seed-reid`) |
| 가중치 34 GB+ 받기 | Tier 0 가중치(구운 사본)만 먼저, 세팅·VLM 은 lobby 가 Tier 0 준비 뒤 뒤에서(기존 감시 프로세스 순서) |
| TensorRT 엔진 빌드 6분 | 같은 GPU 종류·판이면 캐시에서 받기(수 초~수십 초) |
| vLLM 컴파일 3분 | 컴파일 캐시 받기 |
| preflight·`start-lobby.sh` | `simai-bootstrap preflight --sets tier0` → `simai-bootstrap lobby` 자동 |
| 게이트웨이 전환 | -701 |

## 단계 흐름

```
tini → simai-start(sshd; 비밀 값은 넘겨받은 명령에만) → simai-bootstrap auto
   부팅 비밀값·ID·EC2 주소 셋 다 있음 → (비밀 값을 꺼내기 전에) exec simai-gpu-boot auto
   비밀값만 있음 → 64(템플릿 오류) / 없음 → 지금까지의 tier0
simai-gpu-boot auto
 0. 이 인스턴스 세션이 EC2 에 의해 끝났다는 기록(/var/tmp/simai-gpu-boot/ended.json)이 있으면 등록하지 않고 대기
 1. 비밀 값(부팅 비밀값·TLS 비밀 키·HF 토큰)을 환경에서 꺼냄 → 로그는 가린다(_secrets + 서명 주소 쿼리)
 2. nvidia-smi(이름·sm·메모리·개수) · TLS 키 · 공급자 사실 → POST /api/gpu/sessions (EC2 잠깐 없으면 2→60 s 백오프로 계속, 4xx 는 멈춤)
 3. 토큰 → <SIMAI_ROOT>/../.simai-private/gpu-session/<워커 ID>.token·tier0.token(600), session.json(비밀 아님) · 보고기 시작
 4. [보고 WEIGHTS] 뒤에서 캐시 받기: trt-engine → $SIMAI_ROOT/engines, reid → $SIMAI_ROOT/reid, vllm-compile → <SIMAI_CACHE_ROOT>/vllm(받는 동안 막지 않음)
    시작 묶음 lobby-bundle/: calibration.json(서명 주소 GET + sha256 확인) · laptop_token.sha256(쉼표) · tls/tier0.{crt,key}
    simai-bootstrap preflight --sets tier0 (Tier 0 가중치·GPU 최소 사양·예산·texrecon/open3d/ultralytics)
    엔진·Re-ID 받기를 기다림(상한 SIMAI_BOOT_CACHE_WAIT_S 600 s)
 5. start.config 가 null → 세팅 경로(아래). 아니면 simai-bootstrap lobby ... (토큰은 파일 경로로만)
 6. [ENGINE] 이번 Tier 0 표시 파일 없음 → [TIER0] phase≠running → [VLLM] Tier 0 준비·vLLM /health 대기 → [READY]
    (vLLM 이 Tier 0 준비 뒤 1800 s 안에 안 뜨면 READY + "vLLM 미준비")
 7. READY 뒤: 엔진(없던 것)·Re-ID(손으로 둔 묶음이 캐시에 없을 때) 올림, vLLM 은 /health 60 s 뒤 컴파일 캐시 올림
 8. 409 GPU_SESSION_ENDED → lobby SIGTERM → ended.json → 대기. SIGTERM → lobby SIGTERM → 보고 ENDED(PROVIDER_STOP) → 0
```

lobby 인자(등록 응답 → 인자): `--ec2-base-url start.ec2_base_url`, `--camera-ids start.camera_ids`, `--measured-heights`(키가 있는 카메라만, 0 으로 채우지 않음),
`--scene-version start.scene_version`(null 이면 Tier 0 기본 초기값 `scene-0`, I1 scene_reference 를 받으면 EC2 현재 Scene 을 따른다), `--config-version start.config.config_version`,
`--gateway-id tier0.gateway_id`, `--worker-id worker.worker_id`, `--control-port 17000 --video-ports <cam>:17001,…`(템플릿 노출 포트, 14차 start-lobby 와 같은 듣는 포트 방식),
`--public-host SIMAI_TIER0_ADVERTISE_HOST`(기본 127.0.0.1 = SSAFY 망 기본 경로), `--services SIMAI_BOOT_SERVICES`(없으면 lobby 기본).

## 보고 단계와 실패 사유

| 단계 | 언제 |
|---|---|
| WEIGHTS | 등록 직후 ~ lobby 띄우기 전(캐시 받기·시작 묶음·preflight) |
| ENGINE | lobby 를 띄운 뒤 이번 Tier 0 가 표시 파일을 쓰기 전(엔진 받기 실패면 여기서 빌드) |
| TIER0 | 표시 파일이 생겼고 `phase=running`·`reserve=OK` 전 |
| VLLM | Tier 0 준비, vLLM /health 200 대기(VLM 가중치 31.5 GB 받기 포함) |
| READY | 관제 준비 끝 |
| FAILED | `reason_code`: TLS_KEY_MISSING·TLS_PIN_MISMATCH·CALIBRATION_FETCH_FAILED·CALIBRATION_HASH_MISMATCH·NO_START_CONFIG·NO_LAPTOP_TOKEN·NO_CAMERAS·WEIGHTS_FAILED(preflight 2)·GPU_TOO_SMALL(3)·PREFLIGHT_FAILED·ENGINE_BUILD_FAILED(lobby 5, ENGINE 중)·TIER0_CONFIG_FAILED(2)·TIER0_EXITED·LOBBY_STOPPED(0)·LOBBY_USAGE(64) |
| ENDED | SIGTERM 으로 스스로 멈춤(`PROVIDER_STOP`) |

FAILED 는 세션을 끝내지 않는다(서버 규칙). 인스턴스는 컨테이너를 끝내지 않고 FAILED 를 계속 보고하며 기다린다. 끝내면 공급자가 컨테이너를 다시 띄워
다시 등록하고, 그 등록이 (사람이 손으로 이어 가는) 진짜 세션을 SUPERSEDED 로 밀어내기 때문이다. 끌지는 -702 자동 정지가 정한다.
등록 전 실패(GPU 없음·공개 주소 모름·등록 거부)는 보고할 세션이 없어 로그만 남기고 기다린다. 설정 누락(ID·EC2 주소)만 64 로 끝낸다(등록하지 않으니 재시작 반복이 해롭지 않다).

## start.config 가 null(세팅 경로)

Tier 0 live 모드는 보정 문서가 있어야 뜨고(`services/tier0/settings.py` "calibration_file 이 없다"), 현장 세팅 워커도 활성 보정이 없으면 `INTRINSICS_UNAVAILABLE` 이다
(`docs/design/site-setup-pipeline.md`). 그래서 구성 묶음이 없으면 lobby 를 띄우지 않고 Tier 0 가중치·preflight·TensorRT 엔진(`engine_cache ensure`, 캐시에도 올림)까지
예열한 뒤 FAILED `NO_START_CONFIG` 로 기다린다. 사람은 토큰 파일·엔진이 준비된 Pod 에서 손 경로(시작 묶음을 올리고 `start-lobby`)로 이어 간다.
MVP 현장은 이미 구성 묶음이 있어(cfg-41 등) 운영 경로에서는 거의 생기지 않는다. 보정 없이 첫 세팅을 하는 경로가 생기면 이 분기를 그쪽으로 바꾼다.

## GPU 캐시: 키

객체 키는 `gpu-cache/<kind>/<cache_key>.tar`(버킷 `simai-k15a101-media-apne2`, EC2 `simai.storage.bucket`). `<digest16>` 은 아래 값의 정렬 JSON SHA-256 앞 16자.

| kind | cache_key | digest 에 넣는 값 | 내용 |
|---|---|---|---|
| `trt-engine` | `<GPU 이름 slug>/<sm_XX>/<digest16>` | GPU 이름(같은 sm_90 이라도 H100·H200 따로)·sm·Tier 0 가중치 sha256(weights.json)·mode·imgsz·max_batch·카메라 해상도·TensorRT(`tensorrt-cu13-bindings`)·ultralytics·torch·ModelOpt 판 | `engines/<모델>/<sm>/trt<판>/<엔진>.engine` + 메타 `.json` |
| `vllm-compile` | `<GPU 이름 slug>/<sm_XX>/<digest16>` | GPU 이름·sm·vLLM·torch·Triton 판·VLM 가중치 sha256 묶음·양자화·max_model_len·gpu_memory_utilization·max_num_seqs·`SIMAI_VLLM_EXTRA_ARGS` | `<SIMAI_CACHE_ROOT>/vllm/`(vLLM 프로세스의 Triton·Inductor·CUDA 캐시 + `VLLM_CACHE_ROOT=…/vllm/vllm` 의 torch.compile 캐시) |
| `reid` | `cope-<COPE 가중치 sha 앞16>-clip-<CLIP sha 앞16>` | Tier 0 embedder 가 확인하는 두 해시(`ai/src/simai_ai/reid/embedder.py` 기본값, 시험이 대조) | `cope/`·`weights/cope_msmt17.pth`·`clip-cache/ViT-B-16.pt`·(있으면)`insightface/` |

판을 하나라도 모르면(metadata 없음) 키를 만들지 않고 캐시 없이 빌드한다(모르면 비운다). 받은 캐시는 한 번 더 확인된다: 엔진은 `engine_cache` 메타데이터 키·엔진 sha256
(다르면 옆으로 옮기고 다시 빌드), Re-ID 는 embedder 의 sha256, vLLM 은 자기 해시 폴더. 받은 tar 는 저장소가 기록한 SHA-256(올릴 때 서명에 묶은 체크섬)과 같아야 풀고,
그 값을 모르면 쓰지 않는다. 푸는 것은 Python `tarfile` data 필터(절대 경로·`..`·링크·장치 거부).

## GPU 캐시: S3 접근 방식(서버 최소 변경)

**정함: 그 GPU 세션의 워커 토큰으로 EC2 에 한 객체씩 짧은 서명 주소를 받는다.** `POST /api/worker/gpu-cache/sign`(워커 체인 `/api/worker/**`, 계약 밖 API 전용 JSON —
워커 객체 서명 `/api/worker/jobs/{id}/objects/sign` 과 같은 성격).

- 요청 `{kind, cache_key, method: GET|PUT, size_bytes?, sha256?}`. 응답 `{object_key, hit, size_bytes?, sha256?, method?, url?, headers?, expires_at_ms?}`(`Cache-Control: no-store`).
- GET: 없으면 `hit:false`, 있으면 서명 GET 주소 + 저장소가 기록한 sha256(HEAD, `ChecksumMode.ENABLED`).
- PUT: 이미 있으면 `hit:true`(주소 없음, 올리지 않는다). 없으면 형식·크기(≤ 4 GiB, S3 단일 PUT 5 GiB 안)·sha256 을 묶고 **`If-None-Match: *` 조건부 쓰기**까지 서명에 묶은 주소.
  두 인스턴스가 동시에 올려도 먼저 올린 것만 남는다(S3 412). 키가 내용 조합이라 같은 키 = 같은 의미이고, 덮어쓰지 않는다(AGENTS 규칙 6).
- 권한: 열린 GPU 세션에 묶인 워커만(`GpuSessionService.openSessionForWorker`, 아니면 403). 세션이 끝나면 워커 토큰이 폐기돼 401. 부팅 비밀값은 워커 체인 밖이라 401.
- 서버 변경: `GpuCacheService`·`GpuCacheController`·`GpuPaths.CACHE_SIGN`, `ObjectStorage.signPut(..., ifAbsent)`, `GpuSessionService.openSessionForWorker`.
  **계약 변경 없음(계약 묶음 v7.53.0 그대로), DB 마이그레이션 없음**(upload_grant 를 쓰지 않는다: 존재·해시는 HEAD 로, 덮어쓰기 금지는 조건부 쓰기로).

## TLS 키: (a) 공급자 비밀 저장소의 고정 키

**정함 (a).** 지금 손 세션이 쓰는 고정 Tier 0 TLS 키를 RunPod Secret(`SIMAI_TIER0_TLS_PRIVATE_KEY`, PEM 또는 PEM 의 base64 한 줄)으로, 인증서는 공개라 템플릿 환경 변수
(`SIMAI_TIER0_TLS_CERT_PEM`)로 준다. 없으면 `.simai-private/tls/tier0.{crt,key}`(엘리스 블록 스토리지 등). 둘 다 없으면 등록은 하고(지문 없이) FAILED `TLS_KEY_MISSING`.
선택 `SIMAI_TIER0_PINNED_SPKI_SHA256`(노트북과 같은 값)이 있으면 기동 전에 지문을 대조해 다르면 FAILED `TLS_PIN_MISMATCH`. 등록 메시지에 `tls_public_key_sha256` 을 싣는다.

근거:
- 노트북(게이트웨이·현장 경보 단말)의 고정 지문 `SIMAI_TIER0_PINNED_SPKI_SHA256` 을 바꾸지 않는다 → -701 이 지문 갱신 경로를 새로 만들 필요가 없고, 지금 손 세션과 같은 신뢰 규칙이다.
- (b) 부팅 때 생성은 세션마다 지문이 바뀌어, 노트북이 새 지문을 믿을 경로가 필요하다. EC2 가 알려 주면 "EC2 가 뚫려도 Tier 0 를 사칭하지 못한다"(-699 보안 원칙)가 깨지고,
  사람이 옮기면 손 작업이 남는다.
- 비용: 비밀값이 둘(부팅 비밀값 + TLS 키). 둘 다 공급자 비밀 저장소에만 있고 EC2 에는 없다. TLS 키가 새면 지금과 같이 키를 바꾸고 노트북 지문을 갱신한다(지금과 같은 범위).

## 비밀 값 처리

- `simai-start` 가 `SIMAI_GPU_BOOT_SECRET`·`SIMAI_TIER0_TLS_PRIVATE_KEY` 를 HF 토큰처럼 맨 앞에서 지우고 exec 바로 앞에서만 내보낸다(ssh-keygen·sshd·SSH 환경 파일에 없음, 이름에 SECRET·PRIVATE).
- `simai-bootstrap auto` 는 비밀 값을 꺼내기 전에 `simai-gpu-boot` 로 exec 한다. 다른 모드는 두 이름을 환경에서 지우고(`take_token`) 자식 환경에서 빼며 `_redact` 대상에 넣는다.
- `simai-gpu-boot` 는 시작하자마자 꺼내 자기 안에만 들고, 자식에는 HF 토큰만 가중치를 받는 `simai-bootstrap`(preflight·lobby)에 넘긴다. 토큰은 파일 경로로만 넘긴다(lobby 명령줄에 원문 없음).
  로그는 비밀 값·`hf_…`·서명 주소 쿼리를 가린다. 보고 `detail` 도 가리고 500자로 자른다.

## 정한 수치와 근거

| 값 | 기본 | 근거 |
|---|---|---|
| 등록 재시도 | 2 s 부터 두 배, 60 s 상한, 멈출 때까지 | EC2 재배포 1~2분(-699 stale-after 근거와 같은 상황)을 넘겨야 한다. 4xx 는 설정 문제라 다시 하지 않는다 |
| `SIMAI_BOOT_CACHE_WAIT_S` | 600 s | Re-ID 1 GB 를 느린 망(약 2 MB/s)에서도 받는다. 넘으면 캐시 없이 진행(엔진은 빌드, Re-ID 는 꺼짐) |
| `SIMAI_BOOT_VLLM_READY_TIMEOUT_S` | 1800 s | VLM 31.5 GB 를 HF 에서 약 20 MB/s 로 받아도 들어온다. 넘으면 READY + 설명(관제는 Tier 0 가 핵심) |
| `SIMAI_BOOT_VLLM_CACHE_SETTLE_S` | 60 s | /health 직후에는 컴파일 산출물이 아직 쓰이는 중일 수 있다 |
| 캐시 상한 | 4 GiB | S3 단일 PUT 5 GiB 안. 서버 `GpuCacheService.MAX_BYTES` 와 같다 |
| 포트 | 제어 17000, 영상 17001(카메라 넷 공용) | RunPod 템플릿 노출 포트·14차 start-lobby 와 같다 |

## 목표 시간(캐시 적중, H100, 다음 세션 실측)

| 구간 | 예상 |
|---|---|
| 컨테이너 시작 → 등록 | 수 초 |
| 엔진(약 0.2 GB)·Re-ID(약 1 GB) 받기(S3 서울 → Pod 지역) | 수십 초(병렬) |
| preflight(Tier 0 구운 가중치 해시) | 수십 초 |
| lobby → Tier 0 준비(엔진 적재·GPU 선확보) | 1분 안 |
| VLM 31.5 GB 받기 + vLLM 기동(컴파일 캐시) | 3~5분(HF 속도가 좌우) |
| **합계(READY)** | **5~7분**(관제 가능한 TIER0→VLLM 전환은 약 2분) |

## 버린 대안

- **Pod 에 IAM 키(gpu-cache/ 접두만 허용)를 두기**: 오래 사는 키가 Pod·RunPod 계정에 남는다. 세션 워커 토큰은 세션이 끝나면 폐기된다.
- **기존 업로드 서명(`/api/uploads/sign`, upload_grant)에 목적 추가**: 받기(GET·존재 확인) 경로가 없고, upload_grant 목적 CHECK·발급 기록 때문에 마이그레이션이 필요하다. 캐시는 기록이 필요 없다.
- **부팅 비밀값으로 서명받기**: 부팅 비밀값 체인은 두 경로만 보는 최소 권한이 원칙(-699). 새면 영향 범위가 넓어진다.
- **TLS (b) 부팅 때 생성**: 위 "TLS 키".
- **실패하면 컨테이너 종료(재시작으로 재시도)**: 다시 등록이 진짜 세션을 밀어낸다. 대기 + FAILED 보고 + -702 정지가 안전하다.
- **preflight 를 전체 묶음(tier0·avatar·static·vlm)으로**: VLM 31.5 GB 를 다 받기 전에는 Tier 0 를 띄우지 못한다(요구 4 위반). 나머지는 lobby 감시 프로세스가 Tier 0 뒤에서 받는다.
- **VLM 가중치도 S3 캐시에**: 31.5 GB 를 S3 서울 → 해외 Pod 로 받는 속도가 HF 보다 낫다는 근거가 없다(다음 세션에서 HF 받기 시간을 재고 판단).

## 남은 위험

- RunPod Secret 에 여러 줄 PEM 이 그대로 들어가는지 확인하지 못했다 → base64 한 줄도 받는다.
- RunPod 이 컨테이너가 끝나면 다시 띄우는지·Stop 의 SIGTERM 유예는 문서로 확인하지 못했다(그래서 끝내지 않고 기다린다).
- 버킷 수명 주기 규칙이 버킷 전체(예: 14일)면 캐시도 지워진다 → 다시 만들 뿐(느려질 뿐 틀리지 않는다). `gpu-cache/` 접두를 규칙에서 빼는 것은 EC2 운영(사람)이 정한다.
- 조건부 쓰기(`If-None-Match: *`)를 서명 주소에 묶는 것은 S3 가 지원하지만 이 버킷에서 실제로 412 가 나는지는 다음 세션에서 본다(안 되면 HEAD 확인만 남고 동시 업로드 때 나중 것이 덮는다).
- vLLM 컴파일 캐시는 /health 60 s 뒤 묶는다. 그 뒤 요청에서 새로 쓰이는 커널 파일이 묶는 중에 바뀌면 그 항목이 깨질 수 있다(vLLM·Triton 은 깨진 캐시를 다시 컴파일한다).
- vLLM 캐시는 뒤에서 받아 vLLM 이 폴더를 만들기 전에만 옮긴다. VLM 가중치가 이미 있는 저장 위치(볼륨)면 vLLM 이 먼저 떠 캐시를 못 쓸 수 있다.
- lobby 가 정상 종료(코드 0)해도 FAILED `LOBBY_STOPPED` 를 보고한다. 손으로 lobby 를 다시 띄워 이어 가는 동안에도 FAILED 로 보이므로 -702 자동 정지는 "FAILED + Tier 0 I1 연결 있음"을 끄지 말아야 한다.
- 등록 `ports` 는 RunPod 매핑이 없으면 듣는 포트를 그대로 적는다(관리 화면·감시용 기록이라 정본은 I1 tier0_endpoint).
- Re-ID 첫 캐시는 사람이 한 번 만든다(손으로 `$SIMAI_ROOT/reid` 에 올린 세션이 READY 뒤 자동으로, 또는 `simai-gpu-boot seed-reid`).
- GPU 를 실제로 켜서 확인하지 않았다(다음 GPU 세션).

## 다음 GPU 세션에서 실측할 것

1. 단계별 시각(`gpu_session_event.received_at`): 등록 → WEIGHTS → ENGINE → TIER0 → VLLM → READY. 첫 번째(캐시 없음)와 두 번째(캐시 적중)를 비교해 목표 5~7분 확인.
2. 캐시 크기·받기/올리기 속도(엔진·Re-ID·vLLM), 같은 GPU 종류에서 엔진 캐시가 `valid` 로 쓰이는지(engine_cache 로그 "빌드" 없음), vLLM 기동 로그의 컴파일 시간.
3. RunPod 이 넣는 `RUNPOD_PUBLIC_IP`·`RUNPOD_TCP_PORT_17000/17001/22`·`RUNPOD_DC_ID` 가 등록에 맞게 들어오는지, Secret 의 PEM 처리.
4. S3 조건부 쓰기 412, HEAD 체크섬(올린 tar 의 sha256 이 GET 서명 응답에 오는지).
5. 409(새 등록으로 밀기)·SIGTERM(Stop) 때 lobby 정지·보고 ENDED 가 실제로 보이는지, 컨테이너 재시작 때 다시 등록하지 않는지.
6. VLM 31.5 GB HF 받기 시간(S3 캐시로 옮길지 판단 재료).

## -702 가 쓸 것

- 켤 때 템플릿 환경 변수: 위 deploy/README "GPU 자동 부팅" 표(부팅 비밀값·TLS 키는 RunPod Secret 참조, `SIMAI_PRICE_PER_HOUR_USD` 는 켤 때 아는 가격).
- 상태: `GET /api/gpu/sessions/current` 의 `stage`·`stage_reason_code`·`stage_detail`·`last_report_at_ms`·`tier0_link`. 이 티켓의 단계 의미는 위 표.
- 자동 정지 판단: FAILED 가 계속되고 `tier0_link=DISCONNECTED` 면 끈다. `NO_START_CONFIG`·`LOBBY_STOPPED` 는 사람이 손 경로로 이어 가는 중일 수 있어 유예를 길게(또는 `tier0_link` 로 판단).
  끈 뒤에는 `GpuSessionService.end(…, ADMIN_STOPPED)`(인스턴스는 SIGTERM 으로 ENDED 를 보내려 하지만 못 보낼 수 있다).
- 인스턴스는 스스로 끝나지 않는다(FAILED 면 기다림). 끄는 것은 -702 의 몫이다.

## 코드·시험 위치

- Pod: `deploy/scripts/simai_gpu_boot.py`(이미지 `simai-gpu-boot`), `deploy/scripts/simai_bootstrap.py`(`auto`·비밀 값 지우기/가리기), `deploy/base/simai-start.sh`(비밀 값 보관),
  `deploy/tier0/Dockerfile`(CMD `auto`, 설치)·`Dockerfile.dockerignore`, 문서 `deploy/README.md` "GPU 자동 부팅".
- 서버: `server/src/main/java/com/ssafy/simai/domain/gpu/GpuCacheService.java`·`GpuCacheController.java`·`GpuPaths.java`, `GpuSessionService.openSessionForWorker`, `global/storage/ObjectStorage.signPut(..., ifAbsent)`.
- 시험: `ai/tests/deploy/test_simai_gpu_boot.py`(가짜 EC2·S3·nvidia-smi·simai-bootstrap: 등록·토큰 파일·시작 묶음·lobby 인자·단계·ENDED·409 뒤 재등록 없음·실패 사유·캐시 왕복·키·안전한 풀기·비밀 값 가리기·auto 넘김),
  `ai/tests/deploy/test_simai_bootstrap.py`(simai-start 비밀 값 순서), `server/src/test/java/com/ssafy/simai/domain/gpu/GpuCacheIntegrationTest.java`.
