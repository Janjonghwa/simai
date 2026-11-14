# S15P31A101-705 GPU 켜기 표 전체 GPU·VRAM 등급·서비스 자동 축소·상단바 「GPU」·설정 페이지

관련 명세: 3.12(최소 사양: Tier 0 단독 24GB급, Tier 0 + 분석 48GB 이상, 80GB 권장, 시연 기준 H100), 3.2(GPU 메모리 예산표), 13(관제 화면 상단바).
사용자 결정 2026-11-14(아래 1~8). 앞 티켓: -702(켜기 패널·GPU 점·GPU 설정·`GET /api/gpu/offers`), -703(AWS 재고 UNKNOWN·칸별 가격), -700(부팅·`SIMAI_BOOT_SERVICES`).
**계약·DB 변경 없음**(관리 REST 는 계약 밖, 단계 보고는 기존 `detail` 칸에 글만 더함).

## 사용자 결정과 반영

| # | 결정 | 반영 |
|---|---|---|
| 1 | 켜기 패널에 모든 GPU | 행 = 공급자 카탈로그의 모든 GPU + (없어도) 선호 GPU. 선호 목록은 정렬·기본 선택에만 쓴다 |
| 2 | 재고 글은 켤 수 있음/없음/모름 | 칸 `available`(true·false·null) 추가, 화면은 「켤 수 있음」·「없음」·「모름」. 등급 글(많음·보통·적음)은 쓰지 않음. 개수는 자리만(`count`, 지금은 아무도 안 줌) |
| 3 | H100·H200 계열 「추천」을 위에, 기본은 추천 중 켤 수 있는 첫 칸 | 행 `recommended`, 정렬 맨 앞. 자동 선택 ① 추천 × 선호 지역 × 재고 확인(HIGH·MEDIUM·LOW) → ② 선호 GPU × 선호 지역 × 재고(모름 포함) |
| 4 | VRAM 등급, 막지 않고 확인창 | 행 `tier`, 제한·위험은 확인창 경고 + 「그래도 켜기」 |
| 5 | VRAM 별 서비스 자동 축소 | `simai_gpu_boot.boot_services`: `SIMAI_BOOT_SERVICES` 가 비었을 때만 VRAM 으로 기본값, 보고 설명에 축소 사실 |
| 6 | 상단바 글자는 「GPU」만 | 점 + 「GPU」. 상태 글은 정보창 첫 줄·`aria-label` |
| 7 | 노트북 GPU 는 켜기 표에 넣지 않음 | 표는 공급자 카탈로그만 본다(바꾼 것 없음). 손 경로 MANUAL 세션이 점에 보이는 동작 그대로 |
| 8 | 설정 페이지 신설 | `/settings`, 계정 메뉴는 「설정」 진입 + 로그아웃 |

## VRAM 등급(서버 `domain/gpu/GpuTiers`, AWS·RunPod 공통, 한 곳에서만 정함)

VRAM 은 **GPU 1장** 기준이다(AWS 인스턴스가 8장이어도 Tier 0·워커 묶음은 한 장을 쓴다). RunPod 카탈로그 `memory`(GB), AWS `DescribeInstanceTypes` GPU 메모리(GB).

| 등급(`tier`) | 등급(판매 GB) | 경계(공급자 GB 값) | 화면 | 색 | 근거(명세 3.12·3.2) | 켤 때 |
|---|---|---|---|---|---|---|
| RECOMMENDED | 80GB급 이상 | ≥ 74 | (H100·H200 계열만 「추천」) | `primary` | 시연 기준 80GB(H100). 3.2 예산표가 80GB 기준 | 확인창만 |
| BELOW_RECOMMENDED | 48GB급 | 44 ~ 73 | 「권장 미만」 | 노랑 `sev-caution` | Tier 0 + 분석 동시 실행 최소. 48GB급 VLM 몫(0.30 → 13~14 GiB)은 KV 부족 계산, 19.1 미결 | 확인창에 "VLM·아바타가 늦거나 빠질 수 있음" |
| LIMITED | 24GB급 | 22 ~ 43 | 「제한」 | 주황 `sev-high` | Tier 0 단독 24GB급. 48GB 미만에는 분석 부하를 올리지 않는다 | **경고 확인창**: 세팅(맵 굽기)·VLM 근거·아바타가 안 됨, Tier 0 관제만 |
| RISKY | 24GB급 미만 | < 22 | 「위험」 | 빨강 `sev-critical` | Tier 0 최소 사양 미만 | **경고 확인창**: 관제 준비가 실패할 수 있음(개발·시험용) |
| (null) | 모름 | — | 표시 없음 | — | 모르면 비운다 | 확인창만 |

- 경계를 판매 GB(80·48·24) 대신 74·44·22 로 둔 까닭: RunPod 카탈로그는 판매 GB(80·48·24)를 주지만 AWS 는 `sizeInMiB / 1024` 반올림이라 48GB L40S 가
  45GB 안팎, 24GB A10G 가 22GB 로 온다. 부팅의 기존 MiB 상수(`simai_bootstrap` 80GB급 경고 76,000 · 48GB급 45,000 · 24GB급 22,500 MiB ≈ 74.2·43.9·22.0 GiB)와
  같은 경계라 화면 등급과 부팅 서비스 축소(아래)가 같은 GPU 를 같은 칸에 넣는다. 사이에 걸치는 실제 GPU 는 없다(A100 40GB → 제한, 94GB H100 NVL → 추천).
- **추천** = 이름(공급자 ID 또는 표시 이름)에 H100·H200 이 있고(앞뒤가 글자·숫자가 아닐 때, GH200 제외) 80GB 이상(또는 VRAM 모름). 까닭: 시연 기준 GPU 이고,
  TensorRT 엔진 캐시가 GPU 종류별(-700)이라 쓰던 종류를 지키는 편이 기동이 빠르다. A100 80GB·B200·RTX PRO 6000 96GB 는 등급만 RECOMMENDED 이고 「추천」 표시는 없다.
- 색은 front CONVENTION 의 위험 등급 색 예외(-704 카메라 지연 배지·-702 GPU 점과 같은 쓰임)로 적었다.

## 행 순서·칸·기본 선택(`GpuPowerService.offers`·`autoPick`)

- 행 정렬: 추천 → 등급(RECOMMENDED → BELOW → LIMITED → RISKY → 모름) → 선호 순서 → VRAM 큰 순 → 이름 순. 화면은 서버 순서를 그대로 그린다.
- 열(지역): 선호 지역(순서) + 추천·선호 GPU 재고가 있는 다른 지역 최대 4곳(-702 와 같은 수). 모든 GPU 로 넓히면 지역 열이 너무 늘어 추천·선호 기준으로 둔다.
- 칸: 기존 `stock`(지우지 않음) + `available`(HIGH·MEDIUM·LOW → true, NONE·칸 없음 → false, UNKNOWN → null) + 가격·상한 안. 행 `available` = 보이는 칸 중 하나라도 true → true, 아니면 모름이 있으면 null, 아니면 false.
- RunPod 카탈로그에 지역(dataCenters)이 없는 GPU 도 행으로 둔다(`GpuOffer.region = null`, 칸은 모두 없음, 가격은 보임).
- AWS 는 물어볼 종류를 알아야 해서 `GpuProvider.catalogGpuTypes()`(기본 빈 목록)를 더했다: AWS 는 예비 가격표의 키(서울 GPU 인스턴스 10종, -703 후보 표)를 준다.
  이 리전에서 팔지 않는 종류는 가격(Price List)도 묻지 않는다.
- 기본 선택: ① 추천 GPU(표 순서라 선호가 먼저) × 선호 지역에서 **재고가 확인된** 첫 칸 → ② 선호 GPU 순서 × 선호 지역에서 재고 있음·모름인 첫 칸. 둘 다 가격 상한 안.
  ①에서 모름을 빼는 까닭: AWS 는 모든 칸이 모름이라 선호(g6e 등)보다 비싼 추천(p5.48xlarge 시간당 약 76달러)이 기본이 되면 안 된다.
  선호 밖 지역은 자동으로 고르지 않는다(-702 와 같음, 지연이 커질 수 있어 사람이 표에서 고른다). `POST /api/gpu/launches` 빈 본문(자동)도 같은 규칙.
- 사람이 고른 칸은 등급과 상관없이 켠다(막지 않음). 재고·상한 검사는 -702 그대로.

## 재고 표시 규칙(화면 `gpuStatus.availabilityLabel`·`cellUsable`)

| 칸 | 글 | 고르기 |
|---|---|---|
| available true | 「켤 수 있음」(공급자가 개수를 주면 「n개」) | 가능(상한 안이면) |
| available false 또는 칸 없음 | 「없음」 | 불가 |
| available null(AWS) | 「모름」 | 가능(켜기가 재고 없음으로 거절하면 409 안내) |

Lead 조사대로 RunPod 카탈로그(REST v2 `include=AVAILABILITY`, GraphQL `lowestPrice`)는 등급만 주고 개수 칸은 null 이라 개수는 보이지 않는다. 서버가 `available` 을 싣지 않은
옛 응답이면 화면이 `stock` 에서 고른다(UNKNOWN → 모름, NONE → 없음, 나머지 → 켤 수 있음).

## 부팅 서비스 자동 축소(`deploy/scripts/simai_gpu_boot.py` `boot_services`)

`SIMAI_BOOT_SERVICES` 가 있으면 그 값 그대로(명시값 우선). 비었을 때 nvidia-smi `memory.total`(MiB)로:

| VRAM(MiB) | 예 | lobby `--services` | 보고 설명(`detail` 끝에 붙임) |
|---|---|---|---|
| ≥ 45,000(48GB급, `MIN_WORKER_VRAM_MB`) | H100 81,559 · H200 · A100 80GB · L40S 46,068 · A6000 49,140 | 넘기지 않음(lobby 기본 `tier0,vllm,worker,avatar`) | 없음 |
| 22,500 ~ 45,000(24GB급, `MIN_VRAM_MB`) | RTX 4090 24,564 · A10 23,028 · L4 23,034 · A100 40GB 40,960 | `tier0` | "VRAM n GiB(24GB급): Tier 0 관제만, 세팅·맵 굽기·VLM·아바타 꺼짐" |
| < 22,500 | RTX A4000 16GB · RTX 4000 Ada 20GB | `tier0` | "VRAM n GiB: Tier 0 최소 사양(24GB급) 미만, 개발·시험용(준비 실패 가능)" |
| 모름 | — | 넘기지 않음 | 없음(preflight 가 사양을 모르면 멈춘다) |

- 경계는 `simai_bootstrap` 의 기존 상수(45,000·22,500 MiB)를 그대로 쓴다. 화면 등급 경계(74·44·22GB)와 같은 값이라 같은 GPU 를 같은 칸에 넣는다(48GB 판매 GPU 는 46,000~49,000 MiB, 24GB 는 23,000~24,600 MiB).
- 48~79GB 는 Lead 표대로 지금 기본을 유지했다(VLM 몫 미결 19.1). lobby 감시 프로세스는 이미 48GB급 미만이면 Tier 0 단독으로 내리지만(3.12), 부팅이 미리 `tier0` 를 넘기면
  preflight·lobby 인자가 맞고 `want_vllm` 이 거짓이라 vLLM 을 기다리지 않으며, 축소 사실이 보고에 남는다.
- 보고: `Reporter.note` 를 정하면 ENDED 를 뺀 모든 단계 보고의 `detail` 끝에 붙는다(계약 칸 그대로, 500자 자름은 기존대로). FAILED(예: 24GB급 미만의 GPU_TOO_SMALL)에도 붙는다.
- **세팅(site_setup)은 분석 워커가 한다.** 24~47GB 는 워커를 띄우지 않으니 세팅·맵 굽기가 안 된다. 화면 확인창 문구(제한)를 이와 맞췄다.

## 상단바 GPU 점

- 버튼 글자는 「GPU」만(점 색·깜빡임은 -702 그대로). `aria-label` 은 상태 글(「GPU 연결」·「GPU 준비 중」·「GPU 꺼짐」·「GPU 오류」).
- 마우스 올림 정보창 첫 줄에 상태 글(굵게), 그 아래 -702 정보 줄. 관리자가 아닌 계정도 정보창이 뜬다(상태 글만이라도 보이게, 전에는 꺼짐이면 정보창이 없었다).

## 설정 페이지(`/settings`, `features/settings/SettingsPage`)

| 구역 | 누구 | 출처 | 비고 |
|---|---|---|---|
| 화면(다크·화이트 모드) | 모든 계정 | `theme.store`(브라우저 저장) | 계정 메뉴에서 옮김 |
| 경보 소리(켜기·끄기) | 확인 권한 계정(관리자·반장) | `alarmSound.setMuted`(브라우저 저장) | 상단바 소리 버튼은 그대로 둔다(같은 값, 차단 표시·빠른 전환) |
| GPU | 관리자 | `GET·POST /api/gpu/settings`(-702, 새 버전) | 창(`GpuSettingsDialog`) → 구역(`GpuSettingsSection`). 저장 뒤 창을 닫지 않고 「저장했습니다」, 「취소」 대신 「되돌리기」 |

- 계정 메뉴: 「설정」 + 구분선 + 로그아웃. 모든 계정이 쓸 구역(화면)이 있어 메뉴를 숨기지 않고, 계정이 쓸 수 없는 구역은 보이지 않는다(지금 권한 규칙: GPU 는 ADMIN, 소리는 `useCanAck`).
- 라우트는 `_app` 아래(로그인 필요). 하단 메뉴(독)에는 넣지 않았다(계정 메뉴로만 들어옴). `routeTree.gen.ts` 는 TanStack 생성기(`@tanstack/router-generator`, vite 플러그인과 같은 설정)로 다시 만들었다(손으로 고치지 않음).
- **뺀 것**(설정 성격이 약하거나 서버 기능이 필요): CCTV 떠 있는 창 위치·크기 기억(`simai.cctv.float`, 끌어 옮기면 저장되는 배치 기억), 이벤트 「모두 지우기」 기준(목록 동작),
  작업 화면 위치 기억(sessionStorage), 사용자·권한 관리·현장 정보(서버 기능 없음), 규칙 기준 선택(-669 에서 뺌).

## 버린 대안

| 안 | 버린 까닭 |
|---|---|
| 추천·등급을 화면에서 계산 | AWS·RunPod 공통 규칙을 서버 한 곳에 둬야 자동 선택(서버)과 표시가 어긋나지 않는다 |
| 추천 = 80GB 이상 전부 | A100 80GB·B200 이 맨 위·기본이 된다. 시연 기준·엔진 캐시 연속성은 H100·H200 계열이다(등급은 RECOMMENDED 로 같다) |
| 자동 선택에서 추천의 「모름」도 고름 | AWS 에서 시간당 약 76달러 p5.48xlarge 가 기본이 된다 |
| 자동 선택이 선호 밖 지역까지 | EC2·노트북 왕복 지연이 커져 판정 지연(-704)이 나빠질 수 있다. 표에서 사람이 고른다 |
| 지역 열을 모든 GPU 재고 기준으로 | RunPod 지역이 수십 곳이라 표가 가로로 너무 길어진다 |
| 제한·위험을 막기 | 사용자 결정 "막지 않는다". 개발·시험에 작은 GPU 를 쓸 수 있어야 한다 |
| 24GB 미만이면 preflight 최소 사양(22,500 MiB)을 낮춤 | 명세 3.12 "사양 미만이면 Tier 0 를 띄우지 않는다"와 어긋나 이 티켓에서 바꾸지 않았다(남은 위험 1) |
| 상단바 소리 버튼도 설정 페이지로만 | 경보 중 바로 끄고 브라우저 차단(「!」)을 보는 곳이라 상단바에 남겼다(같은 값을 두 곳에서) |
| GPU 설정을 창으로 두고 설정 페이지에서 열기 | 사용자 결정 "창을 따로 두지 말고 페이지 한 구역으로" |

## 남은 위험

1. **24GB 미만(위험)은 실제로 관제 준비가 실패한다.** 부팅은 `tier0` 로 줄이지만 `simai-bootstrap preflight` 가 22,500 MiB 미만이면 최소 사양 미만(종료 3)으로 멈춰
   FAILED `GPU_TOO_SMALL` 이 된다. 화면 경고는 "준비가 실패할 수 있음"으로 맞췄다. "개발·시험용으로 쓸 수 있게"가 목표면 preflight 최소값(명세 3.12)을 바꾸는 별도 결정이 필요하다(Lead 보고).
2. 48~79GB 는 지금 기본(vLLM·워커·아바타 포함)이라 vLLM 이 KV 부족으로 못 뜰 수 있다(3.12 계산, 실측 없음). 그때는 -700 대로 1800 s 뒤 READY + "vLLM 미준비"다.
3. RunPod 카탈로그 행이 수십 개라 표가 길다(세로 스크롤, 머리줄 고정). 지역 열은 추천·선호 기준 최대 4곳 추가라 그 밖 지역에만 있는 GPU 는 「없음」으로 보인다.
4. 추천 판정이 이름 문자열(H100·H200)에 기댄다. 공급자가 이름을 바꾸면(예: "Hopper 80GB") 추천에서 빠진다(등급은 VRAM 으로 그대로). 시험이 RunPod 이름 꼴(`NVIDIA H100 80GB HBM3`·`H100 NVL`)과 AWS 꼴(`p5.48xlarge · H100 ×8`)을 확인한다.
5. AWS 카탈로그 후보는 예비 가격표 키(10종)뿐이다. 새 인스턴스 종류는 `simai.gpu.aws.on-demand-prices` 에 넣거나 선호 목록에 적어야 표에 나온다.
6. AWS GPU 메모리(`sizeInMiB`)의 실제 값은 확인하지 않았다(설계 -703 과 같이 AWS 를 부르지 않음). 48GB급 GPU 가 44GB 아래(45,056 MiB 미만)로 보고되면 「제한」으로 보인다.
   부팅 쪽은 nvidia-smi 로 다시 판단하므로 서비스 축소와 화면 표시가 그때만 어긋날 수 있다.

## 코드·시험 위치

- 서버: `domain/gpu/GpuTiers`(새), `GpuPowerService.offers`·`autoPick`·`rows`·`table`, `provider/GpuProvider.catalogGpuTypes`(기본 메서드)·`GpuOffer.region` null 뜻,
  `RunPodProvider.offers`(지역 없는 GPU 행), `AwsGpuProvider.catalogGpuTypes`·팔지 않는 종류 가격 생략.
- 시험(서버): `GpuTiersTest`(경계·추천 이름·available), `GpuPowerIntegrationTest`(카탈로그 전체 행·정렬·tier·available·지역 없는 행·기본 = 추천 중 켤 수 있는 첫 칸·선호 밖 GPU 켜기),
  `GpuPowerAwsIntegrationTest`(카탈로그 p5 행이 위·available null·기본은 선호 g6e).
- 부팅: `deploy/scripts/simai_gpu_boot.py` `boot_services`·`Reporter.note`, `deploy/README.md` 환경 변수 표. 시험 `ai/tests/deploy/test_simai_gpu_boot.py`(VRAM 별 기본값·명시값 우선·24GB급 통합).
- front: `features/gpu/{types,gpuStatus,GpuLaunchForm,GpuIndicator,GpuPanel,GpuSettingsSection}`, `features/settings/SettingsPage`, `routes/_app/settings.tsx`, `routeTree.gen.ts`(생성),
  `features/layout/TopBar`, `mocks/gpu.ts`, `front/CONVENTION.md`(색 예외·상단바·설정 화면). 시험 `__tests__/features/gpu/{GpuIndicator,gpuStatus,GpuSettingsSection,useGpuGate}`,
  `settings/SettingsPage`, `layout/TopBar`.

## 명세 정리 대상

- 3.12: 켜기 표의 VRAM 등급(80·48·24GB)·추천(H100·H200)·막지 않고 확인, 부팅의 VRAM 별 서비스 기본값(48GB급 미만 `tier0`).
- 13장: 상단바 GPU 점 글자 「GPU」(상태는 정보창·화면 읽기 이름), 계정 메뉴 「설정」, 설정 화면 구역(화면·경보 소리·GPU).
