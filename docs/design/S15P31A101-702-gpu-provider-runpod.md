# S15P31A101-702 GPU 공급자 추상화·RunPod·화면 켜기/끄기·자동 정지

관련 명세: 3.12(GPU 실행 사이트·최소 사양·"한 시점에 GPU 인스턴스는 하나"·GPU 사용 원칙), 3.2(Tier 0 우선), 12.6(관리 REST), 14(비밀 값).
사용자 결정 2026-11-14: 팀원 누구나 화면에서 GPU 를 켜고 끈다. 공급자는 RunPod 먼저, 나중 AWS 서울 GPU(-703)도 같은 화면·같은 경로.
앞 티켓: -699 G1 세션 등록(`docs/design/S15P31A101-699-gpu-session-register.md`), -700 Pod 자동 부팅, -701 게이트웨이 자동 전환.
-704 상단바 「판정 지연」은 GPU 점 정보창 한 줄로 옮겼다. DB V76(추가만). 계약 변경 없음(관리 REST 는 계약 밖, -699 의 `current` 와 같다).

## 공급자 인터페이스

`server/.../domain/gpu/provider/GpuProvider`:

| 메서드 | 뜻 |
|---|---|
| `id()` | `RUNPOD`·`AWS`(= `gpu_session.provider`·설정 `provider`) |
| `configured()` | API 키 등이 있는지. 없으면 켜기·재고 조회 503 `GPU_PROVIDER_NOT_CONFIGURED`, 자동 정지는 아무것도 안 함 |
| `offers(OfferQuery{cloudType, gpuCount})` | 지금 재고·시간당 가격(GPU 종류 × 지역 칸, 재고 HIGH·MEDIUM·LOW·NONE) |
| `create(LaunchSpec)` | 인스턴스 만들기 → 인스턴스 ID(G1 등록의 `instance_id` 와 같은 값) |
| `terminate(instanceId)` | 지우기(과금 멈춤). 이미 없으면 성공 |
| `state(instanceId)` | RUNNING·STOPPED·GONE·UNKNOWN |

`LaunchSpec` 은 비밀 아닌 환경 변수(`plainEnv`)와 비밀 참조(`secretEnv`: 환경 변수 이름 → 공급자 비밀 저장소 이름)를 나눠 받는다. 구현이
공급자 문법으로 참조를 넣고(RunPod `{{ RUNPOD_SECRET_이름 }}`) 서버는 원문을 모른다. 실패는 `GpuProviderException(code)`:
NOT_CONFIGURED·OUT_OF_STOCK·REJECTED·UNAVAILABLE·BAD_RESPONSE(문구·로그에 키·본문을 싣지 않고 HTTP 상태만).
`GpuProviders` 가 빈 목록에서 id 로 찾는다. 세션 토큰·기동 설정은 공급자와 무관하다(G1).

## RunPod 호출(공식 문서 2026-11 확인, 실제 호출은 하지 않았다)

| 일 | 호출 |
|---|---|
| 재고·가격 | `GET https://api.runpod.io/v2/catalog/gpus?include=AVAILABILITY&product=POD&count=1&cloud=SECURE\|COMMUNITY` → `gpus[]: {id, name, memory, price: {secure, community}, availability, dataCenters: [{id, name, availability}]}`. 가격은 GPU 1대 시간당 USD 온디맨드 |
| 만들기 | `POST https://rest.runpod.io/v1/pods` `{name, templateId, computeType: GPU, cloudType, gpuTypeIds: [종류], gpuTypePriority: custom, gpuCount: 1, dataCenterIds: [지역], dataCenterPriority: custom, interruptible, env}` → `{id, costPerHr, desiredStatus}` |
| 끄기 | `DELETE /v1/pods/{id}`(지우기, 404 도 성공). 컨테이너 디스크·볼륨 없음이라 정지(stop)보다 지우기가 과금이 깨끗하다 |
| 상태 | `GET /v1/pods/{id}` → `desiredStatus` RUNNING·EXITED·TERMINATED, 404 → GONE |

인증 `Authorization: Bearer <SIMAI_RUNPOD_API_KEY>`(EC2 `.env` → compose → `simai.gpu.runpod.api-key`, `toString` 도 키를 숨김).
만들기 4xx 본문에 "no longer any instances"·"no instances available" 이 있으면 OUT_OF_STOCK. 시험은 가짜 RunPod 서버(`HttpServer`)로 한다.

만들 때 넣는 환경 변수(-700 `simai-gpu-boot` 가 읽는 이름):

| 이름 | 값 |
|---|---|
| `SIMAI_GPU_BOOT_SECRET` | `{{ RUNPOD_SECRET_simai_gpu_boot_secret }}`(참조 글자만) |
| `SIMAI_TIER0_TLS_PRIVATE_KEY` | `{{ RUNPOD_SECRET_simai_tier0_tls_key }}` |
| `HF_TOKEN` | `{{ RUNPOD_SECRET_hf_token }}` |
| `SIMAI_GPU_BOOT_ID` | `simai.gpu.runpod.boot-id`(`SIMAI_GPU_BOOT_ID`, 기본 `gpu-boot-runpod`, 비밀 아님) |
| `SIMAI_EC2_BASE_URL` | `SIMAI_PUBLIC_BASE_URL`(비면 켜기 503 `EC2_BASE_URL_MISSING`) |
| `SIMAI_GPU_PROVIDER` | `RUNPOD` |
| `SIMAI_PRICE_PER_HOUR_USD` | 켤 때 목록 가격(→ G1 등록 `price_per_hour_usd` 로 세션 기록에 남는다) |
| `SIMAI_GPU_LAUNCH_ID` | 켠 요청 ID(기록 대조용, -700 은 읽지 않아도 된다) |

비밀 참조 목록은 `simai.gpu.runpod.secret-env`(코드 기본값)로 바꿀 수 있다. 이미지·포트(TCP 17000·17001·22)·디스크·`SIMAI_TIER0_TLS_CERT_PEM`·
`PUBLIC_KEY` 같은 고정 값은 템플릿에 둔다.

## 상태 흐름

```
꺼짐(OFF) --켜기(POST /api/gpu/launches)--> 켜는 중(STARTING: gpu_launch 기록 → 공급자 만들기 CREATED, G1 등록 전)
   --G1 등록(instance_id 일치)--> 준비 중(PREPARING: stage REGISTERED·WEIGHTS·ENGINE·TIER0·VLLM)
   --READY 또는 그 세션 I1 연결--> 연결(CONNECTED)
   --끄기·자동 정지--> (STOP_REQUESTED → 공급자 지우기 → STOPPED → 그 인스턴스 열린 세션 ADMIN_STOPPED) → 꺼짐
오류(ERROR): 세션 stage FAILED / 만들기 실패 15분 / 인스턴스는 남았는데 세션이 끝남 / 끄기 실패
```

- 켜기·끄기 기록: `gpu_launch`(누가·언제·무엇을·얼마에·자동 선택인지, 공급자 호출 전에 한 번), `gpu_launch_event`(CREATED·CREATE_FAILED·
  STOP_REQUESTED·STOPPED·STOP_FAILED·GONE, 행위자·사유 코드). 만들기 결과와 끝은 부분 UNIQUE 로 한 번. G1 세션과는 provider + instance_id 로 잇는다
  (등록 메시지에 이미 instance_id·gpu_type·region·price 칸이 있어 계약을 바꾸지 않았다).
- 한 번에 하나: 열린 G1 세션이나 끝나지 않은 켠 요청이 있으면 409 `GPU_ALREADY_ON`. 확인과 기록은 advisory 잠금 `simai.gpu_launch` 안, 공급자
  호출은 잠금 밖(기록된 요청 행이 다음 켜기를 막는다). 서버가 그 사이 죽어 결과가 5분 없으면 자동 정지 점검이 CREATE_FAILED(REQUEST_LOST)로 닫는다.
- 끄기: 관제(LIVE)·세팅 중이면 409 `GPU_STOP_BLOCKED_LIVE`·`_SETUP`. 켠 요청이 없는 RunPod 세션(콘솔로 켬)도 instance_id 로 끈다(기록은 세션 ENDED
  ADMIN_STOPPED 와 로그). MANUAL(손 세션)은 409 `GPU_STOP_UNSUPPORTED`. 끈 뒤 `GpuSessionService.end(…, ADMIN_STOPPED)` 로 토큰을 바로 폐기한다.
- 상태 조회: G1 `GET /api/gpu/sessions/current` 에 필드만 더했다(-701 의 `gateway` 와 같은 방식): `launch`(가장 최근 켠 요청: state REQUESTED·
  RUNNING·FAILED·STOPPED, 종류·지역·클라우드·스팟·가격·요청자 login_id·시각·instance_id·끝 사유·실패 코드), `power`(OFF·STARTING·PREPARING·
  CONNECTED·ERROR), `stop_block`(LIVE·SETUP·null).

## 자동 선택

선호 GPU 순서가 먼저, 그 안에서 선호 지역 순서로 재고(NONE 아님)이고 가격 상한 안(상한 없으면 통과, 가격을 모르면 상한이 있을 때 탈락)인 첫 칸.
GPU 를 먼저 보는 까닭: TensorRT 엔진 캐시가 GPU 종류별이라 같은 종류를 지키는 편이 기동이 빠르다(다른 종류면 화면이 안내). 선호 지역에 재고가 없으면
기본은 비고(켜기 409 `GPU_OUT_OF_STOCK`, 재고는 있는데 상한 밖이면 `GPU_PRICE_OVER_CAP`) 표에 재고 있는 다른 지역 4곳까지 보여 사람이 고른다.
사람이 고른 칸은 켤 때 재고를 다시 묻고 같은 검사를 한다.

## 자동 정지(1분 주기, `GpuAutoStopper` → `GpuPowerService.autoStop`)

**절대 끄지 않는 때**(어떤 까닭보다 먼저, `GpuActivity`, deploy.sh `ACTIVE_SQL` 과 같은 판단): 진행 중 작업(work_unit IN_PROGRESS), 세팅 중
(COMPLETED·FAILED 아닌 세팅 세션 또는 대기·실행 중 맵 보강 다시 굽기), 녹화 업로드 중(RECORDING_SEGMENT 업로드 서명이 10분 안, deploy.sh
`SIMAI_DEPLOY_HOLD_UPLOAD_MINUTES` 기본과 같음), 분석 작업 중(만료 전 ACTIVE 작업 대여: 관제 뒤 사람 지운 배경 묶음 등).

그 밖에 끄는 까닭(사유 코드가 `gpu_launch_event.reason_code` 에 남는다):

| 까닭 | 조건 | 근거 |
|---|---|---|
| BOOT_TIMEOUT | 만든 뒤 25분(`simai.gpu.boot-timeout`) 안에 G1 등록 없음 | 목표 5~7분 + 첫 엔진 빌드(6분)·이미지 받기 여유. 등록 못 하는 인스턴스는 쓸모가 없고 과금만 된다 |
| SESSION_ENDED | 그 인스턴스의 세션이 끝났는데(STALE·SUPERSEDED·END_REPORTED·폐기) 인스턴스가 남음 | 토큰이 무효라 관제할 수 없다. -700 은 끝난 뒤 다시 등록하지 않고 기다린다 |
| FAILED | 세션 stage FAILED 가 20분(`failed-grace`) 이어지고 그 세션 I1 연결이 없음 | -700: 인스턴스는 FAILED 면 스스로 끝내지 않는다. I1 이 붙어 있으면 사람이 손으로 잇는 중일 수 있어 두다 |
| IDLE | 설정 분(기본 30, 0 이면 끔)이 지나도록 GPU 를 쓰지 않음 | 시작점 = max(켠 요청(없으면 세션 등록), 마지막 실행 종료, 세팅 세션 갱신, 작업 대여 활동, 녹화 업로드 서명). 실행(work_run)이 끝난 뒤부터 센다 |

- G1 STALE(보고 10분 없음 + I1 없음 → 세션 종료)과 겹치지 않는다: G1 은 세션(토큰)을 끝내고, 이 티켓은 남은 공급자 인스턴스(과금)를 정리한다.
  보고가 끊긴 세션은 G1 이 STALE 로 끝낸 뒤 다음 점검에서 SESSION_ENDED 로 끈다.
- 공급자에서 이미 사라진 인스턴스(GET 404·TERMINATED)는 끄지 않고 GONE 으로 닫는다.
- 끄기 실패는 STOP_FAILED 를 남기고(점 ERROR) 다음 주기에 다시 한다.
- 0분(끔)은 시연 당일 "아침에 확보해 끝까지 유지"(명세 3.12) 용이다. 그때도 BOOT_TIMEOUT·SESSION_ENDED·FAILED 는 끈다.

## GPU 설정(버전 추가 저장)

`gpu_settings_version`(V76): 공급자(RUNPOD·AWS)·선호 GPU 목록(1~10, 순서)·선호 지역 목록(1~20, 순서)·클라우드(SECURE·COMMUNITY)·스팟 여부·
시간당 가격 상한(없음 또는 0 < x ≤ 100)·자동 정지 분(0~1440)·RunPod 템플릿 ID(영숫자·`_`·`-` 64자)·저장 시각·저장한 관리자. 저장마다 새 행
(이력, 덮어쓰지 않음), 가장 큰 버전이 지금 설정. 없으면 서버 기본값(H100 80GB HBM3 → H100 NVL → H100 PCIe, AP-JP-1 → OC-AU-1, 보안,
온디맨드, 상한 없음, 30분, 템플릿 없음 → 켜기 503). 비밀 값은 받지도 두지도 않는다. 켠 요청에 그때 설정 버전을 남긴다.

| REST(관리자, 계약 밖) | |
|---|---|
| `GET /api/gpu/settings` | `{settings, providers: [{id, configured}]}` |
| `POST /api/gpu/settings` | 새 버전 → 201 settings. 틀리면 400 `INVALID_GPU_SETTINGS` |
| `GET /api/gpu/offers` | `{provider, cloud_type, interruptible, max_price_per_hour_usd, gpu_types, regions(preferred), cells(stock·price·within_cap), default}` |
| `POST /api/gpu/launches` | `{gpu_type?, region?}` → 201 launch(둘 다 없으면 자동 선택) |
| `POST /api/gpu/stop` | 204 |

## 화면(front `features/gpu`)

- **상단바** `GpuIndicator`: 시계 앞에 점 + 짧은 글자만. 초록 「GPU 연결」 / 노랑 깜빡임 「GPU 준비 중」(켜는 중 포함) / 회색 「GPU 꺼짐」 /
  빨강 「GPU 오류」. 상단바의 「판정 지연 n ms」(-704)는 뺐다. 관리자가 아니면 상태 API 를 부르지 않고 Tier 0 연결로만 연결·꺼짐을 보이며 누를 수 없다.
- **마우스 올림 정보창** `GpuInfoLines`: GPU 종류·지역, 경과·예상 비용(시간당 가격 × 경과, 켠 요청부터, 서버 시계 기준), 판정 지연(-704 와 같은
  계산·색), 준비 중이면 단계 「켜는 중 → 가중치 → 엔진 → 준비 완료」(TIER0·VLLM 은 엔진 단계로 묶음), 오류면 까닭. 모르는 줄은 뺀다.
- **누르면 패널** `GpuPanel`: 꺼져 있으면 `GpuLaunchForm`(GPU 종류 × 지역 표, 칸마다 재고 많음·보통·적음·없음과 시간당 가격, 기본은 서버의
  선호 순서 첫 재고, 재고 없음·상한 밖은 못 고름, 기본과 다른 GPU 종류면 "엔진 캐시가 GPU 종류별이라 첫 실행은 엔진 빌드로 몇 분 더 걸립니다" →
  「켜기」 → 확인창). 켜져 있으면 요약 + 「GPU 끄기」(LIVE·세팅 중이면 까닭과 함께 꺼 둠) → 확인창.
- **경고창** `useGpuGate`·`GpuGateDialog`: 작업 관리 「작업 전 확인」 확인창의 「작업 시작」(`PreflightStage`)과 세팅 1단계 「다음」(세션을
  만드는 세팅 시작, `SetupPage.requestStep1Submit`)을 감싼다. 꺼짐 → "GPU가 꺼져 있습니다 / 세팅과 관제에는 GPU가 필요합니다(약 5~7분)" +
  [GPU 켜기](켜기 표) [취소]. 준비 중 → "GPU 준비 중" + 단계, 연결되면 창을 닫고 하던 단계를 잇는다. 오류 → 까닭 + 닫기. 관리자가 아니면 막지 않는다.
- **GPU 설정** `GpuSettingsDialog`: 관리자 메뉴 「GPU 설정」(관리자만). 공급자·선호 GPU·선호 지역(한 줄에 하나, 위가 먼저)·클라우드·온디맨드/스팟·
  가격 상한(비우면 없음)·자동 정지 분(0 = 끔)·템플릿 ID. 버전 번호를 보인다.
- 상태 주기: 준비 중 3초, 그 밖 10초. 재고 표는 패널을 열 때만 받는다(공급자 API 호출).
- 개발 mock `mocks/gpu.ts`(켜면 4초씩 켜는 중 → 가중치 → 엔진 → 연결).

## 버린 대안

| 안 | 버린 까닭 |
|---|---|
| RunPod GraphQL(`gpuTypes.lowestPrice`·`dataCenters.gpuAvailability`) | 쿼리 두 번에 키를 쿼리 문자열로 싣는다(로그에 남기 쉬움). REST v2 카탈로그 하나로 지역별 재고·가격을 받는다 |
| Pod 정지(stop) | 볼륨 없음(컨테이너 디스크)이라 다시 켤 일이 없고, 멈춘 Pod 도 디스크 과금·GPU 재확보 실패가 있다. 지우기로 한다 |
| 부팅 비밀값을 서버가 Pod 환경 변수에 원문으로 | 서버가 원문을 갖게 된다(명세 14). RunPod Secret 참조 글자만 넣는다 |
| 상태 표에 state 칸을 두고 UPDATE | 덮어쓰지 않는다(AGENTS 6). 사건 행을 덧붙이고 읽을 때 만든다(G1 과 같음) |
| 켜는 중 상태를 새 엔드포인트로 | -699 `current` 하나로 화면이 다 알 수 있게 필드만 더했다(-701 과 같은 방식, 큰 변경 없음) |
| 자동 정지에서 LIVE 중이라도 끝난 세션 인스턴스는 끄기 | "LIVE·세팅·업로드 중에는 절대 끄지 않는다"(사용자)를 그대로 지킨다. 끝난 세션 인스턴스는 관제가 끝난 뒤 끈다(과금은 그동안 남음) |
| 지역을 GPU 보다 먼저 | 엔진 캐시가 GPU 종류별이라 종류를 지키는 편이 기동이 빠르다. 지역 차이(지연)는 선호 지역 목록 순서와 표에서 사람이 고른다 |
| 스팟 가격 표시 | 카탈로그에 스팟 가격이 없다. 상한 비교·표시는 온디맨드 가격(위쪽 한도)으로 한다 |

## 남은 위험

1. **API 로 만든 Pod 의 Secret 참조 해석·템플릿 env 병합을 확인하지 못했다.** RunPod 문서는 템플릿 env 의 `{{ RUNPOD_SECRET_… }}` 만 설명하고
   API `env` 가 템플릿 env 를 덮는지·합치는지 말하지 않는다. 첫 GPU 세션에서 `SIMAI_GPU_BOOT_SECRET` 이 풀리는지(-700 은 비밀값 없으면 등록하지
   않아 BOOT_TIMEOUT 으로 꺼진다), 템플릿의 `SIMAI_TIER0_TLS_CERT_PEM` 등이 남는지 본다. 덮어쓰면 고정 값도 서버 `plainEnv` 로 옮긴다.
2. 카탈로그 v2 의 지역별 `availability` 가 실제 만들기 성공과 다를 수 있다(낮음 재고). 실패하면 CREATE_FAILED(OUT_OF_STOCK) 를 남기고 화면이 다른 칸을
   고르게 한다. 다른 지역으로 자동 재시도는 하지 않았다(사람이 가격·지역을 보고 고른다).
3. 기본 지역 ID(AP-JP-1·OC-AU-1)는 문서 목록에서 확인하지 않은 값이다. GPU 설정에서 카탈로그가 보여 주는 실제 ID 로 바꾼다.
4. 켠 요청과 공급자 만들기 사이 서버가 죽으면 Pod 은 생겼는데 ID 를 모를 수 있다(REQUEST_LOST 로 닫음). 그 Pod 이 G1 에 등록하면 켠 요청 없는
   RunPod 세션으로 보이고 끄기·자동 정지가 instance_id 로 끈다. 등록도 못 하면 RunPod 콘솔에서 지워야 한다(`SIMAI_GPU_LAUNCH_ID` 로 찾음).
5. 버려진 세팅 세션(COMPLETED·FAILED 가 아닌 채 남음)은 자동 정지를 영원히 막는다(사용자 규칙 "세팅 중엔 절대"). 세팅 대기 시간 초과(-641)가 일부를 닫는다.
6. 끄기는 RUNPOD 키가 있어야 한다. 키를 지우면 켜진 GPU 를 화면에서 끄지 못한다(RunPod 콘솔).
7. 스팟(interruptible)은 RunPod 이 중단할 수 있다. 중단되면 G1 STALE → SESSION_ENDED/GONE 으로 정리되지만 관제는 끊긴다.

## -703(AWS)이 붙을 자리

- `GpuProvider` 구현 빈 `AwsGpuProvider`(id `AWS`) 하나를 더하면 설정 공급자 `AWS` 로 같은 화면·켜기·끄기·자동 정지·표가 쓰인다(`GpuProviders` 가 찾음).
  `offers` 는 EC2 인스턴스 유형(예: p5)·가용 영역 칸, `create` 는 user-data/Secrets Manager 로 부팅 비밀값(`secretEnv` 를 Secrets Manager 이름으로),
  `terminate` 는 TerminateInstances, `state` 는 DescribeInstances.
- 설정 표의 `runpod_template_id` 는 RunPod 전용이다. AWS 용 시작 템플릿 ID 가 필요하면 V 다음 번호로 칸을 더한다(새 버전 행이라 덮어쓰기 없음).
- `GpuPowerService.launch` 의 템플릿 검사(`RUNPOD` 이고 템플릿 없음 → 503)와 `spec` 의 `SIMAI_GPU_PROVIDER=RUNPOD` 를 공급자별로 나누면 된다.

## 운영 준비(값 없이)

1. EC2 `.env`: `SIMAI_RUNPOD_API_KEY`(사용자가 넣음), 필요하면 `SIMAI_GPU_BOOT_ID`(기본 `gpu-boot-runpod`). `deploy/ec2/compose.yaml` 이 server 로
   넘긴다. `.env.example` 은 이 티켓 범위 밖이라 고치지 않았다(Lead 가 두 줄 추가 권장).
2. EC2: `admin worker register --id <워커> --job-types …` → `admin gpu-boot register --id gpu-boot-runpod --worker-id <워커>`(원문 한 번 출력).
3. RunPod Secrets(콘솔): `simai_gpu_boot_secret`(2 의 원문), `simai_tier0_tls_key`(고정 Tier 0 TLS 비밀 키), `hf_token`.
4. RunPod 템플릿: 통합 이미지 다이제스트, TCP 17000·17001·22 노출, 컨테이너 디스크 100GB 이상, `SIMAI_TIER0_TLS_CERT_PEM`·`PUBLIC_KEY`·
   `SIMAI_TIER0_GPU_RESERVE_MB`·`SIMAI_IMAGE_DIGEST` 등 -700 표의 고정 값. 위 비밀 셋도 템플릿 env 에 참조로 넣어 두면 위험 1 에 대비된다.
5. 화면 「GPU 설정」: 템플릿 ID, 선호 GPU·지역(카탈로그에 보이는 ID), 가격 상한, 자동 정지 분 저장.
6. 첫 켜기 뒤 확인: 위험 1(Secret 해석·env 병합), 등록까지 시간, `SIMAI_PRICE_PER_HOUR_USD` 가 세션 기록에 남는지.

## 코드·시험 위치

- 서버: `domain/gpu/provider/{GpuProvider,GpuProviderException,GpuProviders,RunPodProvider}`, `domain/gpu/{GpuPowerService,GpuPowerController,
  GpuAutoStopper,GpuActivity,GpuSettingsService}`, `GpuSessionController.current`(필드 추가), `global/properties/GpuProperties`(AppConfig 등록),
  `global/exception/ErrorCode.Gpu`(GPU_ALREADY_ON 등), `application.yaml simai.gpu`, `db/migration/V76__gpu_power.sql`, `deploy/ec2/compose.yaml`.
- 시험: `GpuPowerIntegrationTest`(가짜 RunPod: 자동 선택·템플릿·비밀 참조·기록·상태 흐름·이미 켜짐 거부, 가격 상한·재고 없음·공급자 재고 거절,
  LIVE·세팅 끄기 거부·끄기 뒤 세션 ADMIN_STOPPED, 유휴 자동 정지와 LIVE·세팅·업로드 중 안 끔, 0분·BOOT_TIMEOUT·SESSION_ENDED·GONE, 설정 버전·권한,
  키 없음 503).
- front: `features/gpu/*`, `features/layout/TopBar.tsx`, `features/work/preflight/PreflightStage.tsx`, `features/setup/SetupPage.tsx`, `mocks/gpu.ts`.
  시험 `__tests__/features/gpu/{gpuStatus,GpuIndicator,useGpuGate,GpuSettingsDialog}`, `layout/TopBar`, `work/preflight/PreflightStage`(GPU 꺼짐 경고창).

## 명세 정리 대상

- 3.12: 화면 켜기·끄기(관리자), 공급자 추상화(RunPod 먼저·AWS -703), 자동 선택 규칙, 자동 정지 규칙(절대 안 끄는 때·네 까닭), GPU 설정 버전.
- 13장: 상단바 GPU 점·정보창(판정 지연 이동)·켜기/끄기 패널·경고창·GPU 설정.
- 18장: 운영 준비(Secrets 이름·템플릿·EC2 env).
