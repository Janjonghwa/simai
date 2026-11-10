# 통합 이미지에 시연 가중치 굽기

S15P31A101-577. 기획서 §3.12(GPU 실행 환경: 볼륨 없음, 지역 고정 안 함, 가중치는 해시 목록으로 확인), §3.2(Tier 0 우선, 분석 워커는 별도 프로세스), §21.5(이미지 다이제스트).
코드(시작 순서): `deploy/scripts/simai_bootstrap.py`(`Supervisor`), `ai/services/worker/vlm_gate.py`·`runner.py`(`type_gate`)·`__main__.py`.
코드: `deploy/tier0/Dockerfile`(`weights-*` 단계, `unified` 의 `COPY --link`), `deploy/scripts/bake_weight.py`(굽기 단계 받기 도구), `deploy/scripts/simai_bootstrap.py`(`_seed_from_baked`, `pick_mapanything`),
`deploy/weights.json`(MapAnything 네 항목 `group: mapanything`), `deploy/docker-bake.hcl`(`unified` 의 `secret`·`WEIGHTS_IMAGE`), `deploy/scripts/check_image.sh`(2b), `deploy/README.md`("시연 가중치 굽기").
시험(시작 순서): `ai/tests/deploy/test_simai_supervisor.py`(VLM 이 느려도 아바타·PPE·워커 먼저, VLM 실패해도 나머지 유지, 아바타·PPE 묶음 실패), `ai/tests/services/worker/test_vlm_gate.py`.
시험: `ai/tests/deploy/test_simai_bootstrap.py`(구운 사본·MapAnything 한 판·Dockerfile↔목록 대조·받기 도구), `ai/tests/deploy/test_simai_scene.py`(장면 준비의 MapAnything 한 판).

## 문제

새 RunPod Pod(볼륨 없음, GPU 지역 고정 안 함)는 시작할 때마다 HF·GitHub 에서 가중치 약 45GB 를 받았다. 시연 경로(lobby)가 받는 것:
tier0 0.13GB → (Tier 0 준비 뒤) vlm 31.5GB + avatar 2.95GB → 현장 세팅 static(MapAnything 4.91GB + WorldMirror 2.0 5.05GB + LaMa 0.21GB).
`preflight`·`scene`·`after-live`·`check --sets static` 은 static 묶음 전체라 같은 크기의 MapAnything apache 판(4.91GB)도 받았다.

## 시연 경로가 실제로 읽는 가중치

| 항목(목록 이름) | 크기 | 시연 경로 | 근거 |
|---|---|---|---|
| yolo26x-pose | 0.13GB | 씀: Tier 0 검출기(엔진 원본) | `simai_bootstrap.py:2613` lobby `prepare("tier0")`, `deploy/tier0/entrypoint.sh:16` |
| yolo26x-seg | 0.14GB | 씀: 아바타 사람 분할 | `ai/src/simai_ai/hmr/segmentation.py:135`, avatar 묶음 `simai_bootstrap.py:1308` |
| sam-3d-body-dinov3 (model.ckpt) | 2.11GB | 씀: 아바타 SAM-3D-Body | `ai/src/simai_ai/hmr/loader.py:14`, `ai/services/avatar/__main__.py:57` |
| sam-3d-body-dinov3-mhr | 0.70GB | 씀: 아바타 MHR 리그 | `ai/src/simai_ai/hmr/loader.py:15`, `ai/src/simai_ai/hmr/runtime.py:29` |
| map-anything-model·config | 4.91GB | 씀: 현장 세팅 MapAnything(기본 폴더 `weights/map-anything`) | `ai/src/simai_ai/scene/reconstruct_tasks.py:64`, Supervisor `simai_bootstrap.py:1112`·`1329` |
| map-anything-apache-model·config | 4.91GB | **안 씀**(실험 비교용: 3차 세션 u4, README 장면 예시). -539 큐는 목록에 이름이 있는지만 본다 | `deploy/scripts/gpu_session1.py:380`, `deploy/scripts/pod_final_map_539.sh:119`(이름 검사만, 추론은 WM2) |
| hy-worldmirror-2.0-model·config | 5.05GB | 씀: 최종 맵(-539) 깊이(기본 켜짐) | `reconstruct_tasks.py:363-372`(`final_map_pipeline` 기본 True), `:390`, Supervisor `simai_bootstrap.py:1113` |
| big-lama | 0.21GB | 씀: 최종 맵 빈칸 채움 | `reconstruct_tasks.py:615`, Supervisor `simai_bootstrap.py:1118` |
| ppe-t1-v4-all-e08 | 약 0.1GB(비공개라 API 미확인) | 씀: PPE 서비스(명시 선택, 13차 세션에서 씀) | `simai_bootstrap.py:1208`, `ai/services/ppe/__main__.py:50` |
| cosmos3-nano-*(18개) | 31.5GB | 씀: vLLM VLM | `simai_bootstrap.py:1256` |
| moge-2-vits/vitl-normal | 0.1·1.3GB | **안 씀**: exp02 묶음만(통합 이미지 기본 묶음 아님, MoGe 패키지도 통합 이미지에 없음) | `deploy/weights.json` sets `["exp02"]`, `deploy/tier0/Dockerfile` "HMR 코드 사본" 주석, `check_image.sh` unified import 점검 |

## 결정

- **굽는 것(약 13.4GB): tier0·avatar·ppe 전부 + static 중 MapAnything 기본 판.** 이미지의 `/opt/simai/baked/<목록 path>` 에 파일과 옆 `<path>.sha256` 을 둔다.
  모델(또는 작은 묶음)마다 단계 하나·층 하나(`weights-yolo` 0.27GB, `weights-sam3d` 2.81GB, `weights-mapanything` 4.91GB, `weights-wm2` 5.05GB, `weights-lama` 0.21GB, `weights-ppe` 약 0.1GB).
- **받는 것: VLM(Cosmos3-Nano 31.5GB)만.** Tier 0 준비 뒤 워커 가중치 단계가 지금처럼 받는다.
- **층 위치와 캐시.** 굽기 단계는 `simai-base`·잠금·코드와 무관하다: 단계 이미지는 따로 고정한 python slim(`WEIGHTS_IMAGE` = bake 의 `PYTHON_RISK_ENGINE` 다이제스트), 받기 도구는 bind(층 없음), 원본·해시는 RUN 줄에 있다.
  그래서 그 RUN 줄이 바뀔 때만 다시 받는다. `unified` 에는 `COPY --link` 로 apt·코드 층보다 앞에 넣어, 앞 층(잠금)이 바뀌어도 다시 만들지 않고 같은 다이제스트라 레지스트리에 다시 올리지 않는다.
  받기 도구는 받은 파일·폴더 mtime 을 0 으로 둬, 빌드 캐시를 잃고 다시 받아도 내용이 같으면 같은 층이 되게 했다(최선 노력, 아래 잔여 위험).
- **토큰.** BuildKit 비밀 `hf_token`(bake `secret = ["type=env,id=hf_token,env=HF_TOKEN"]`, 파일 `/run/secrets/hf_token`)만 쓴다. ARG·ENV·층·이력에 남지 않는다. HF 단계만 비밀을 연다.
  받기 도구는 토큰을 첫 요청에만 싣는다(`add_unredirected_header`, HF 가 보내는 다른 호스트 CDN 리다이렉트에는 안 실림, `simai-bootstrap` 과 같은 방식).
- **부트스트랩.** `$SIMAI_ROOT/<path>` 가 없을 때만 구운 사본을 본다: 옆 `.sha256` 이 목록 해시와 다르면(다른 판) 해시하지 않고 받기로, 같으면 사본을 한 번 해시해 맞을 때
  `$SIMAI_ROOT/<path>` 에 심볼릭 링크를 건다(상태 `baked`). 해시가 다르면(손상) 경고만 남기고 받는다. tier0 밖 항목은 검증 스탬프를 남겨 다음 시작에 다시 해시하지 않는다.
  있는 파일의 규칙(맞으면 present, 다르면 지우지 않고 시작 거부)은 그대로라 볼륨에 남은 파일이 사본보다 앞선다.
- **링크 방식.** 심볼릭 링크. 하드 링크는 overlayfs 아래 층(이미지) 파일을 위 층으로 통째 복사하고(copy-up), `$SIMAI_ROOT` 가 볼륨이면 파일 시스템이 달라 걸리지 않는다. 링크를 못 걸면 복사한다(권한 없는 Windows 시험 환경).
  Tier 0(`SIMAI_TIER0_WEIGHTS`)·아바타(`prepare_checkpoint` 의 링크)·MapAnything·WM2(폴더 안 파일 링크)·LaMa 는 모두 `$SIMAI_ROOT` 경로를 그대로 연다.
- **MapAnything 중복 제거.** 목록에서 지우지 않고(실험 도구가 쓴다) 네 항목에 `group: mapanything` 을 붙였다. 장면 준비·after-live·`check --sets`·`preflight` 는 한 판만 받는다:
  장면 명령의 `--model <판의 폴더>` → `SIMAI_MAPANYTHING_MODEL` → 기본 `weights/map-anything`(현장 세팅 워커와 같은 규칙). u4·README 예시처럼 apache 폴더를 `--model` 로 주면 apache 판만 받는다.
  `SIMAI_WEIGHTS_SET=static` 으로 부르는 실험 경로(run·queue·인자 없는 check, MapAnything 이미지의 m1·m2 비교)는 목록 그대로 두 벌이다. Supervisor 의 현장 세팅 받기는 원래 기본 판만 받았다(-473).
- **apache 대신 기본 판을 남긴 이유.** 현장 세팅 워커가 지금 읽는 것이 `weights/map-anything`(CC-BY-NC)이다(`reconstruct_tasks.py:64`). 판을 바꾸는 것은 결과가 달라지는 일이라 이 티켓 범위가 아니다(라이선스는 따지지 않는다는 사용자 결정).

## 새 Pod 시작에 기대하는 효과

| | 전 | 후 |
|---|---|---|
| 시작 뒤 받는 양(lobby) | 약 44.7GB (preflight·scene·after-live 를 쓰면 apache +4.9GB) | 31.5GB (VLM 만) |
| Tier 0 시작 전 | GitHub 에서 yolo 0.13GB 받기 + 해시 | 사본 해시 0.13GB(1초 안팎) |
| 아바타·현장 세팅 가중치 | HF(승인제 토큰)·GitHub 받기 약 13GB | 사본 해시 약 13GB(수 초~수십 초, 백그라운드) + 링크, HF·GitHub 불필요 |
| 이미지 받기 | 약 24.3GB(풀린 크기) | 약 37.7GB 추정. 레지스트리(Docker Hub) 받기는 층 병렬이고 컨테이너 시작 전에 끝난다 |

- HF 장애·속도 제한·승인제 토큰 문제가 Tier 0·아바타·현장 세팅을 막지 않는다. VLM 만 HF 에 의존한다.
- 전체 시작 시간이 얼마나 줄지는 Docker Hub 받기 속도와 HF 받기 속도의 차이에 달려 있어 GPU 세션에서 잰다(측정 전).
- 아바타·PPE·워커는 VLM 받기를 기다리지 않는다(아래 "시작 순서").

## 시작 순서 (감시 프로세스, 같은 티켓 후속)

예전: Tier 0 준비 → vlm·avatar·ppe 를 한 번에 받음 → 다 끝나야 vLLM·아바타·PPE → vLLM `/health` 뒤 워커. 구운 아바타 가중치가 몇 초면 준비돼도 아바타는 31.5GB 받기 뒤에 떴다.

지금(`simai_bootstrap.py` `Supervisor.step`·`_prepare_worker_weights`·`_gate_open`):

1. Tier 0 먼저(표시 파일 `phase=running`·`reserve OK`·이번 Tier 0). 그 전에는 받지도 띄우지도 않는다(그대로).
2. 준비되면 분석 워커를 바로 띄우고 현장 세팅 가중치(MapAnything·WM2·LaMa) 받기도 바로 시작한다.
3. 가중치 스레드 하나가 아바타·PPE 묶음(기록 `sidecar-weights`) → VLM 묶음(기록 `worker-weights`)을 차례로 준비한다. 아바타·PPE 는 자기 묶음이 끝나는 대로, vLLM 은 VLM 묶음이 끝난 뒤 뜬다.
4. 워커는 vLLM 전에 돌기 때문에 `SIMAI_WORKER_VLM_GATE=health`(감시 프로세스가 vllm 이 있을 때 넣는다)로 `vlm_rationale`·`incident_summary` 를 vLLM `/health` 200 뒤에만 대여 요청에 넣는다(`ai/services/worker/vlm_gate.py`, `Worker(type_gate=...)`, 5초마다 다시 확인, 바뀔 때만 로그). 그 전에는 EC2 대기열에 남아 시도를 쓰지 않는다. `vlm_zone_draft` 는 VLM 없이 기하 후보를 내므로 미루지 않는다.
5. 실패: VLM 묶음이 실패하면 vLLM 만 포기(`given_up`)하고 이유를 한 번 로그(`VLM 미준비 …`)·기록(`worker-weights` exit 2)에 남긴다. 워커·아바타·PPE·Tier 0 는 계속. 아바타·PPE 묶음이 실패하면 그 둘만 띄우지 않는다.

GPU 우선순위·예산(Tier 0 0.20 + 분석 0.70 + PPE 0.05 ≤ 0.95, 각 프로세스 상한)·Tier 0 준비 판정은 바꾸지 않았다. 바뀐 점은 vLLM 이 워커·아바타보다 늦게 GPU 를 잡는다는 것이다.
버린 대안: (가) 워커도 vLLM `/health` 까지 기다리기 — 현장 세팅이 VLM 받기(수 분) 뒤로 밀린다. (나) 워커를 먼저 띄우고 VLM 작업을 그대로 받기 — vLLM 이 뜨기 전 작업이 `VLM_UNAVAILABLE` 로 시도를 쓴다.
(다) 아바타·PPE·VLM 을 세 스레드로 — 구운 사본이 아니면 대역을 다툰다. 아바타·PPE 가 먼저 끝나는 직렬 한 스레드로 충분하다.
같은 초에 여러 스레드가 부트스트랩 기록을 쓰므로 `_write_record` 를 배타 생성(`open(..., "x")`) + `~n` 으로 바꿨다(덮어써 깨지는 일을 막는다).

## 버린 대안

- **VLM 까지 다 굽기(약 67GB 이미지, 압축해도 비슷).** 무료 플랜 레지스트리 한 저장소에 매 빌드 첫 푸시가 길고, RunPod 컨테이너 디스크·이미지 받기 시간이 크게 는다. VLM 은 Tier 0 준비 뒤 백그라운드로 받아도 관제 시작을 막지 않는다.
- **FP8 로 미리 양자화한 Cosmos 를 굽기(약 10GB).** 지금은 vLLM 온라인 FP8(`--quantization fp8`, -525)이라 체크포인트 형식이 다르다. 오프라인 양자화 체크포인트가 같은 정확도·지연인지 GPU 검증이 먼저라 미뤘다.
- **가중치 단계를 `simai-base` 위에서 돌리기.** 베이스(uv·CUDA 다이제스트)가 바뀔 때마다 13GB 를 다시 받는다. 따로 고정한 python slim 으로 뗐다.
- **`simai_bootstrap.py` 로 굽기.** 그 파일이 바뀔 때마다(자주) 굽기 단계 캐시가 깨진다. 표준 라이브러리만 쓰는 작은 도구를 따로 뒀다.
- **하드 링크·복사.** 위 "링크 방식".
- **apache 항목을 다른 묶음 이름으로 옮기기.** -539 큐의 이름 검사·u4 큐가 깨진다. 묶음은 그대로 두고 받을 때 고른다.

## 잔여 위험

- 이미지가 약 13.4GB 커진다(풀린 크기 약 24.3 → 37.7GB 추정, safetensors 는 거의 압축되지 않는다). 처음 한 번은 그만큼 더 올린다. 컨테이너 디스크 150GB 기준은 그대로 충분하다.
- 빌드 셸에 HF 토큰이 있어야 한다. sam-3d-body(승인제)·PPE(비공개)를 읽을 수 있는 계정이어야 하고, 없으면 그 단계가 401·403 으로 멈춘다(Pod 의 RunPod Secret `hf_token` 과 같은 토큰이면 13차 세션에서 둘 다 받았다).
- `COPY --link`·mtime 0 의 층 재현성은 같은 빌드 기계의 BuildKit 캐시가 있으면 확실하고, 캐시를 잃고 다시 받을 때 같은 다이제스트가 나오는지는 빌드로 확인하지 않았다(이 티켓은 빌드하지 않았다. `docker buildx build --check` 는 통과).
- vLLM 이 워커(MapAnything 피크 약 6.4GB)·아바타가 GPU 를 잡은 뒤에 뜬다. 각 프로세스 상한 합이 0.95 이하라 vLLM 0.30 몫은 남지만, vLLM 기동 시 빈 메모리 확인을 GPU 세션에서 본다.
- VLM 이 끝내 안 뜨면 `vlm_rationale`·`incident_summary` 는 이 워커가 받지 않고 EC2 대기열에 남는다(예전에는 받아서 `VLM_UNAVAILABLE` 로 실패). Tier 0 RiskEvent 는 영향 없음(AI 는 제안만).
- 볼륨(`/workspace`)에 남은 링크는 그 이미지의 사본을 가리킨다. 다른 이미지로 같은 볼륨을 쓰면 링크가 끊기거나(→ 새 사본으로 다시 건다) 다른 내용을 가리킬 수 있다(→ 있는 파일 규칙대로 해시 불일치, 시작 거부). 목록은 판이 바뀌면 새 path 를 쓰는 규칙이라 드물다.
- 굽기 단계 원본·해시는 `deploy/weights.json` 과 Dockerfile 두 곳에 있다. 시험(`test_dockerfile_bakes_exactly_the_demo_weights_with_manifest_hashes_and_sources`)이 어긋나면 실패한다.

## 넘긴 일

- 이미지 빌드·`check_image.sh simai/unified:<TAG>`(2b: 구운 파일 = 목록의 시연 묶음, 망 없는 컨테이너에서 모두 `baked`)·푸시·새 Pod 시작 시간 측정(Lead).
- FP8 오프라인 체크포인트 검토(GPU 검증 뒤).
