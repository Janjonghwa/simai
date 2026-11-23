# S15P31A101-841 GPU 패널 공급자 탭(AWS 기본)

관련 명세: 3.12(GPU 실행 사이트·"한 시점에 GPU 인스턴스는 하나"), 12.6(관리 REST), 13장(상단바 GPU 점·켜기 패널).
사용자 요청 2026-11-23: 상단바 GPU 를 눌렀을 때 AWS·RunPod GPU 를 탭으로 고르고, 앞으로 기본 탭은 AWS. 배경은 AWS 서울 `g7e.4xlarge`
할당량 승인. AWS 준비(자격 증명·시작 템플릿)는 -840 에서 진행 중이라 운영은 지금 AWS 「준비 안 됨」이다.
앞 티켓: -702(공급자 추상화·켜기/끄기), -703(AWS 공급자), -705(재고 표), -806(연습 목업 GPU 늘 켜짐).
**DB 변경 없음, 계약 변경 없음**(관리 REST 는 계약 밖, -702 와 같다).

## 정한 규칙

| 규칙 | 내용 | 근거 |
|---|---|---|
| 기본 공급자 | AWS 가 켤 수 있게 준비됐으면(서버 `configured()` = enabled·이미지·자격 증명 + GPU 설정의 AWS 시작 템플릿) AWS, 아니면 RunPod | 사용자 「앞으로 AWS 기본」. 시작 템플릿까지 보는 까닭: 템플릿 없이 AWS 가 기본이 되면 게이트 창의 켜기가 늘 503 이다 |
| 설정의 `provider` | 뜻을 「기본 공급자」로 바꾸지 않고 「선호값(선호 GPU·지역·클라우드·스팟·가격 상한)이 어느 공급자의 것인지」로 둔다 | 기존 값·화면·검사(AWS 면 인스턴스 종류·가용 영역 꼴)를 그대로 두고 DB 마이그레이션이 필요 없다. 운영 DB 의 저장값(RUNPOD)이 AWS 기본을 막지 않는다 |
| 다른 공급자의 선호 | 설정 공급자가 아닌 공급자로 재고를 보거나 켜면 그 공급자의 서버 기본 선호(`simai.gpu.defaults`)를 쓴다. 가격 상한은 서버 기본(없음), 스팟 아님 | 선호값은 공급자마다 형식이 다르다(RunPod 표시 이름·데이터센터 vs AWS 인스턴스 종류·가용 영역) |
| AWS 서버 기본 선호·허용 목록 | 인스턴스 `g7e.4xlarge` 하나, 가용 영역 `ap-northeast-2a` → `2b` → `2c` → `2d` (`GpuProperties.Defaults.awsGpuTypes·awsRegions`). `awsGpuTypes` 는 AWS 허용 목록이기도 해서 설정에 AWS 선호를 저장해도 목록 밖 종류는 표·켜기에서 뺀다(겹치는 게 없으면 목록 그대로) | 할당량이 그 종류로 승인됐고 AWS 예산이 약 200달러라 p5.48xlarge(시간당 약 76달러, 3시간이면 예산 끝) 같은 종류를 잘못 누르지 않게 한다(사용자 결정 11-23). 가용 영역은 서울 4곳 전부(실제로 파는 곳만 표에 남는다, -703) |
| 표 행·켤 수 있는 종류 한정 | 설정 공급자가 아닌 공급자와 AWS 는 재고 표 행을 선호 목록(AWS 는 허용 목록 안)으로만 한정하고 카탈로그 후보를 묻지 않는다. `POST /api/gpu/launches` 도 그 목록 밖 `gpu_type` 이면 400 `INVALID_GPU_SETTINGS`. 설정 공급자인 RunPod 은 지금처럼 카탈로그 전체(-705) | 화면만 숨기고 API 로 켜지는 일이 없게 한다 |
| 공급자별 템플릿 | 켤 때 RunPod 템플릿·AWS 시작 템플릿·서브넷·보안 그룹은 지금 설정 버전 것을 그대로 쓴다 | 두 공급자 칸이 한 설정 버전에 이미 함께 있다(-703 V77) |
| 한 시점에 하나 | 켜졌거나 켜는 중이면 공급자와 무관하게 409 `GPU_ALREADY_ON` | 명세 3.12. 끄기 막힘(LIVE·세팅)도 그대로 |
| 화면 기본 탭 | 서버 `default` 를 따르지 않고 늘 AWS. AWS 가 준비 안 됐으면 AWS 탭에 까닭·할 일과 「RunPod 탭으로」 단추(켜기 단추 없음) | 사용자 요청 「기본 탭 AWS」에 가장 가깝다. 준비 안 된 탭을 열어도 할 일이 바로 보여 막히지 않는다(-840 이 끝나면 화면 변경 없이 AWS 표가 뜬다) |
| 켜진 GPU | 켜졌거나 켜는 중이면 그 GPU 의 공급자 탭(열린 켠 요청의 provider, 없으면 살아 있는 세션의 provider)으로 연다. 다른 탭은 「한 번에 하나」 안내 + 그 탭으로 가는 단추. 손으로 켠(MANUAL) GPU 는 어느 탭에서나 끄기 화면 | 세션·켠 요청 응답에 이미 provider 가 있어 서버를 바꾸지 않았다 |
| 게이트 창 | 공급자 없이 재고를 묻고 켠다(서버 기본 공급자) | 바꿀 필요가 없다. 운영 지금은 RunPod, -840 뒤 AWS 로 저절로 바뀐다 |

## REST(관리자, 계약 밖)

| | 바뀐 점 |
|---|---|
| `GET /api/gpu/settings` | `providers[]` 에 `default`(켜기 기본 공급자) 추가 |
| `GET /api/gpu/offers?provider=RUNPOD\|AWS` | provider 없으면 기본 공급자(전에는 설정 공급자). 모르는 ID 400 `INVALID_GPU_SETTINGS`, 준비 안 됨 503 `GPU_PROVIDER_NOT_CONFIGURED` |
| `POST /api/gpu/launches {provider?, gpu_type?, region?}` | provider 추가. 없으면 기본 공급자 |

동작 변화(주의): provider 없이 오는 요청은 이제 설정 공급자가 아니라 기본 공급자 규칙을 따른다. AWS 가 준비 안 된 지금 운영에서는 RunPod 이라 달라지지 않는다.

## 화면

- `GpuPanel` 머리줄 오른쪽에 탭 「AWS」「RunPod」(role=tablist/tab, aria-selected·aria-controls, 고른 탭만 Tab 키, 좌우·Home·End 로 옮기면 바로 고름). 세팅 화면 왼쪽 칸 탭(SetupCalibrateStep)과 같은 모양·토큰.
- `GpuLaunchForm` 에 `provider`·`onSwitchProvider` 추가: 탭 공급자의 표(`useGpuOffers(enabled, provider)`, 쿼리 키에 공급자), 켜기 본문에 provider. 503 `GPU_PROVIDER_NOT_CONFIGURED` 면 「○○ GPU 는 아직 준비 중입니다」 + 할 일 + 다른 탭 단추.
- AWS 표가 4개 가용 영역에서 잘려(가로 스크롤 없음 -752) 행 이름 「종류 · GPU」를 두 줄로 나눴다(RunPod 이름은 그대로).
- 설정 구역: 「켜기 기본은 AWS 입니다(준비 안 됐으면 RunPod). 아래 선호값은 고른 공급자로 켤 때만 씁니다.」 한 줄. `useCachedGpuOffers(provider)` 는 탭별 캐시에서 그 공급자 표를 찾는다.
- 목업(`mocks/gpu.ts`): 두 공급자 모두 준비됨·기본 AWS, 공급자별 표(AWS 는 서버 예비 가격표·「모름」), 켜기의 provider 를 받는다. 처음부터 켜진 GPU(연습 빌드 -806)는 AWS `g7e.4xlarge`/`ap-northeast-2a` 라 패널이 AWS 탭으로 열린다. 남긴 값에 `launch.provider` 를 더했고 옛 값(없음)은 RunPod 으로 읽어 키·판 번호는 그대로다.

## 버린 대안

| 안 | 버린 까닭 |
|---|---|
| 설정 `provider` 를 「기본 공급자」로 의미 변경 | 운영 저장값이 RUNPOD 라 AWS 기본이 되려면 사람이 설정을 다시 저장해야 하고, 선호값 형식 검사와 엉킨다 |
| 공급자별 선호값 칸을 DB 에 따로(마이그레이션) | 지금은 서버 기본 선호로 충분하다. AWS 선호를 화면에서 바꾸려면 설정 공급자를 AWS 로 저장하면 된다 |
| 화면 기본 탭 = 서버 `default` | 지금 운영에서 RunPod 탭이 열려 「기본 탭 AWS」 요청과 다르다 |
| AWS 준비 안 됨이면 탭을 끄기(disabled) | 까닭·할 일을 보일 자리가 없고, 기본 탭이 AWS 라는 요청과 어긋난다 |
| 기본 규칙에서 시작 템플릿을 안 봄(configured 만) | -840 중 자격 증명만 넣은 순간 게이트 창 켜기가 503 으로 막힌다 |

## 남은 위험

1. AWS 가 기본이 된 뒤 용량 없음(`InsufficientInstanceCapacity`)이면 게이트 창 켜기는 409 이고 RunPod 으로 자동으로 넘어가지 않는다(사람이 패널의 RunPod 탭에서 켠다, -702 와 같은 원칙).
2. 서버 기본 선호의 `g7e`(Blackwell)는 실제로 돌려 본 적이 없다(-703 위험 2, 명세 19.1).
3. 다른 공급자 탭에는 설정의 가격 상한이 걸리지 않는다(서버 기본 = 없음). 대신 표 행·켤 수 있는 종류를 선호 목록(AWS 는 g7e.4xlarge)으로 한정했다. AWS 에서 다른 종류를 쓰려면 `simai.gpu.defaults.aws-gpu-types` 를 바꿔 배포해야 한다.
4. 패널을 상태를 받기 전에 열면(앱에서는 상단바가 늘 받아 둠) 잠깐 AWS 탭으로 열렸다가 켜진 GPU 의 탭으로 바뀔 수 있다.

## 코드·시험 위치

- 서버: `domain/gpu/{GpuPowerService(defaultProvider·settingsFor(onlyPreferred)·offers(provider)·launch(provider)),GpuPowerController,GpuSettingsService(forProvider·AWS 허용 목록)}`, `global/properties/GpuProperties.Defaults(awsGpuTypes·awsRegions)`.
- 시험: `GpuPowerAwsIntegrationTest`(기본 공급자 규칙: 준비 안 됨·템플릿 없음·자격 증명 없음 → RunPod, 준비 → AWS / 설정 공급자 아닌 AWS 를 서버 기본 선호로 조회·켜기, 시작 템플릿·서브넷은 설정 것 / 모르는 공급자 400 / 준비 안 된 공급자 503 / 켜는 중 다른 공급자·기본으로 켜기 409 / provider 없이 켜면 AWS).
- front: `features/gpu/{GpuPanel,GpuLaunchForm,GpuSettingsSection,gpuStatus(activeGpuProvider·GPU_PROVIDER_TABS),useGpu,api,types}`, `mocks/gpu.ts`.
  시험 `__tests__/features/gpu/GpuPanel.test.tsx`(기본 AWS 탭·준비 안 됨 안내와 RunPod 이동·키보드·켜기 provider·켜진 공급자 탭·게이트 창 공급자 없음), `GpuIndicator.test.tsx`(RunPod 탭으로 옮겨 켜기), `mocks/gpuPersist.test.ts`(AWS 로 시작·provider 저장·옛 값).

## 명세 정리 대상

- 3.12: 켜기 기본 공급자 규칙(AWS 준비 → AWS, 아니면 RunPod), AWS 허용 종류 `g7e.4xlarge` 하나(예산).
- 13장: GPU 패널 공급자 탭(기본 AWS, 준비 안 됨 안내, 켜진 공급자 탭).
