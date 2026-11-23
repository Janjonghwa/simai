# S15P31A101-703 AWS 서울 GPU 공급자(G5)

관련 명세: 3.12(GPU 실행 사이트·최소 사양·"한 시점에 GPU 인스턴스는 하나"·GPU 사용 원칙), 3.2(Tier 0 우선·GPU 예산), 12.6(관리 REST), 14(비밀 값).
사용자 요청 2026-11-14: 지금은 RunPod 이지만 AWS 서울 리전(ap-northeast-2) GPU 도 고려 중이라 그 경로까지 만든다. 설정만 채우면 화면에서 AWS 로 켤 수 있어야 하고,
실제 AWS 호출은 설정이 있을 때만(기본 꺼짐). 앞 티켓: -699(G1 등록), -700(G2 자동 부팅), -701(G3 게이트웨이 전환), -702(G4 공급자 추상화·RunPod).
**DB V77(추가만). 계약 변경 없음**(관리 REST 는 계약 밖, -702 와 같다). 실제 AWS 계정 작업·호출은 하지 않았다(아래 체크리스트는 사람이 콘솔에서 할 일).

## 구조

```
화면 「GPU 설정」(공급자 AWS) ─ GpuPowerService(-702, 공급자 공통) ─ GpuProviders.find("AWS") ─ AwsGpuProvider
                                                                                         └ AwsGpuApi ─ SdkAwsGpuApi(AWS SDK 2.55.16 ec2·pricing)
                                                                                                     └ (시험) FakeAwsGpuApi
켜기 → RunInstances(user-data) → 인스턴스 호스트(DLAMI): IMDSv2 → docker run <GPU 이미지>(기본 CMD auto)
      → simai-bootstrap auto → simai-gpu-boot: IMDSv2 역할 자격 → Secrets Manager(서울) JSON 비밀 → G1 등록(SIMAI_PUBLIC_HOST·직접 포트) → -700 흐름 그대로
```

| GpuProvider 메서드 | AWS 호출 | 비고 |
|---|---|---|
| `configured()` | (자격 증명 체인 확인, 5분 기억) | `simai.gpu.aws.enabled` + 이미지 + 자격 증명. 하나라도 없으면 설정 화면 「AWS (준비 안 됨)」, 재고·켜기 503 |
| `offers` | DescribeInstanceTypeOfferings(가용 영역) · DescribeSubnets · DescribeInstanceTypes · 온디맨드 Price List GetProducts(6시간 기억) 또는 DescribeSpotPriceHistory | 칸 = 인스턴스 종류 × 가용 영역. 실시간 재고 API 가 없어 `stock=UNKNOWN`(화면 「모름」, 고를 수 있음). 설정한 서브넷이 없는 가용 영역은 NONE |
| `create` | RunInstances | 시작 템플릿(`$Default` 판) + 인스턴스 종류 + 가용 영역의 서브넷(없으면 Placement) + 보안 그룹 + 스팟(일회성·중단 시 지움) + 태그 + user-data + IMDSv2 필수·홉 2 + 안에서 끄면 지움 + clientToken=켠 요청 ID |
| `terminate` | TerminateInstances | `InvalidInstanceID.NotFound` 도 성공 |
| `state` | DescribeInstances | pending·running → RUNNING, stopping·stopped → STOPPED, shutting-down·terminated·없음 → GONE |

오류 매핑(`SdkAwsGpuApi.failure`, 문구·로그는 AWS 오류 코드와 HTTP 상태만): `InsufficientInstanceCapacity` 등 → OUT_OF_STOCK(화면 409 재고 없음, 다른 칸을 고름),
`VcpuLimitExceeded`(할당량)·`UnauthorizedOperation`(IAM)·`InvalidParameter*` → REJECTED(502, 켠 요청 사건 detail 에 코드), 스로틀·5xx → UNAVAILABLE, 자격 증명 없음 → NOT_CONFIGURED.

가격: `GpuOffer.pricePerHourUsd` 는 **인스턴스 1대** 시간당(RunPod 은 GPU 1대). 온디맨드는 공식 Price List(엔드포인트 us-east-1, `regionCode=ap-northeast-2`·Linux·Shared·
`preInstalledSw=NA`·`capacitystatus=Used`)에서, 못 읽으면 설정 표(`simai.gpu.aws.on-demand-prices`, 코드 기본값 = 아래 비용 표)를 30분 기억. 스팟은 가용 영역별 지금 가격.
재고 표의 칸 가격은 칸마다(스팟은 가용 영역마다 다르다)로 바꿨다(RunPod 은 같은 값이라 화면 변화 없음).

## 비밀 값 전달(정함: 인스턴스 역할 + Secrets Manager, 컨테이너가 표준 라이브러리로 받음)

- user-data(서버가 켤 때마다 만드는 bash)에는 비밀 아닌 값만: `SIMAI_GPU_BOOT_ID`·`SIMAI_EC2_BASE_URL`·`SIMAI_GPU_PROVIDER=AWS`·`SIMAI_PRICE_PER_HOUR_USD`·`SIMAI_GPU_LAUNCH_ID`·
  `SIMAI_SECRETS_SOURCE=aws-secretsmanager`·`SIMAI_AWS_SECRET_ID`·`SIMAI_AWS_REGION`·`SIMAI_IMAGE_DIGEST`(이미지 다이제스트에서)·`SIMAI_PUBLIC_PORT_17000=17000`·`_17001=17001`·
  `simai.gpu.aws.extra-env`(기본 `SIMAI_TIER0_GPU_RESERVE_MB=8192`, 있으면 `SIMAI_TIER0_PINNED_SPKI_SHA256`). 호스트가 IMDSv2 로 `SIMAI_INSTANCE_ID`·`SIMAI_REGION`(가용 영역)·`SIMAI_PUBLIC_HOST` 를 붙인다.
  만드는 쪽이 막는다: 비밀 이름(`LaunchSpec.secretEnv` 의 키)과 이름에 TOKEN·SECRET·PASSW·PRIVATE·API_KEY·CREDENTIAL 이 든 평문 변수(두 출처 이름 제외)·줄바꿈 값은 REJECTED.
  환경 파일은 따옴표 친 heredoc(`<<'SIMAI_ENV_END'`)으로 600 권한 `/run`(tmpfs)에 쓴다.
- 비밀 셋(부팅 비밀값·Tier 0 TLS 비밀 키·HF 토큰)은 Secrets Manager(서울) JSON 비밀 하나(`simai/gpu-boot`, 키 = 환경 변수 이름)에만 있다. 인증서(`SIMAI_TIER0_TLS_CERT_PEM`, 공개)도
  같은 JSON 에 둘 수 있다(여러 줄 PEM 을 user-data 에 넣지 않으려고). 환경에 이미 있으면 환경이 먼저.
- **aws cli·boto 없이** 된다: `simai_gpu_boot.py` 가 IMDSv2(PUT 토큰 → 역할 이름 → 임시 자격·리전)와 SigV4(`hmac`·`hashlib`)로 `secretsmanager.GetSecretValue` 를 부른다.
  서명은 AWS 문서 예시(IAM ListUsers, 2015-08-30)와 같은 값인지 시험한다. 이미지 의존성 변화 없음. 역할 자격·비밀 값은 로그 가림 대상에 넣는다.
- 바꾼 Pod 쪽 코드: `simai_bootstrap.py auto` 가 `SIMAI_SECRETS_SOURCE=aws-secretsmanager`(+ID·EC2 주소)도 `simai-gpu-boot` 로 넘기고, `simai_gpu_boot.py` 가 `run()` 맨 앞에서
  받는다(일시 실패는 `SIMAI_AWS_SECRETS_WAIT_S` 기본 180 s 동안 2→30 s 백오프, 거절은 바로 `SECRETS_REJECTED` → 등록하지 않고 대기 → 서버 BOOT_TIMEOUT 정지). `simai-start.sh` 는 고치지 않았다(비밀이 환경에 없다).
- 컨테이너(도커 브리지)가 IMDS 에 닿으려면 홉 2 가 필요하다 → RunInstances 가 `HttpPutResponseHopLimit=2`·`HttpTokens=required` 를 직접 넣는다(시작 템플릿 값보다 우선).
- S3 GPU 캐시는 -700 의 **서명 API(세션 워커 토큰)를 그대로** 쓴다. 인스턴스 역할에 S3 권한을 주지 않는다: RunPod 과 같은 경로, 세션이 끝나면 토큰 폐기, 버킷이 서울이라 받는 속도는 같다.
- 부팅 비밀값 ID 는 RunPod 과 같은 `SIMAI_GPU_BOOT_ID` 하나를 쓴다(서버 설정이 하나). Secrets Manager 의 `SIMAI_GPU_BOOT_SECRET` 은 RunPod Secret 과 같은 원문이다.

## 설정 칸

| 어디 | 칸 | 비고 |
|---|---|---|
| 화면 「GPU 설정」(DB, V77 새 칸, 추가만) | `aws_launch_template_id`(lt-…), `aws_subnet_ids`(최대 10, 가용 영역마다 하나, 비우면 기본 VPC), `aws_security_group_ids`(최대 5, 비우면 시작 템플릿) | 공급자 AWS 면 선호 GPU 칸 = 인스턴스 종류(`g6e.2xlarge` 꼴), 선호 지역 칸 = 가용 영역(`ap-northeast-2a` 꼴)이어야 저장된다. 스팟 = 기존 온디맨드/스팟 칸. 클라우드 종류 칸은 AWS 면 숨김 |
| EC2 `.env` → compose → `simai.gpu.aws` | `SIMAI_GPU_AWS_ENABLED`(기본 false), `SIMAI_GPU_AWS_ACCESS_KEY_ID`·`_SECRET_ACCESS_KEY`(비밀, 비우면 기본 자격 증명 체인), `SIMAI_GPU_AWS_IMAGE`(ECR 서울 사본, 다이제스트), `SIMAI_GPU_AWS_SECRET_ID`(기본 `simai/gpu-boot`), `SIMAI_TIER0_PINNED_SPKI_SHA256`(선택), `SIMAI_GPU_AWS_REGION`(기본 서울) | 자격 증명은 화면·DB 에 두지 않는다. `toString` 이 키를 가린다. EC2 서버가 SSAFY 제공이라 인스턴스 역할을 붙일 수 없어(compose 주석) 실제로는 전용 IAM 사용자 키 |

화면 변경(최소): 공급자 목록의 AWS 가 준비 안 됐으면 「AWS (준비 안 됨)」, AWS 를 고르면 칸 이름이 인스턴스 종류·가용 영역으로 바뀌고 시작 템플릿·서브넷·보안 그룹 칸, 재고 「모름」.

## 서울 리전 GPU 인스턴스 후보

서울 제공 여부: AWS 문서 "Amazon EC2 instance types by Region"(docs.aws.amazon.com/ec2/latest/instancetypes/ec2-instance-regions.html, 2026-11-14 확인)의 서울 가속 컴퓨팅 목록 =
F2·G4dn·G5·G5g·G6·G6e·G6f·Gr6·Gr6f·G7e·Inf1·Inf2·P4d·P5·P5en·P6-B300. **제품군 단위 표라 크기별(예: p5.4xlarge)·가용 영역별 제공은 이 문서로 확인하지 못했다**
(서버의 `DescribeInstanceTypeOfferings` 가 실제로 판다는 가용 영역만 표에 싣는다). P5e·P4de·P6-B200 은 서울 목록에 없다.

| 인스턴스(서울) | GPU | vCPU | 명세 3.12 대비 | 온디맨드 $/h | 스팟 최저 $/h(가용 영역) |
|---|---|---|---|---|---|
| p5.48xlarge | H100 80GB × 8 | 192 | 시연 기준(H100 80GB) 충족. 8장이라 7장은 놂 | 75.9552 | 57.3981(2a) |
| p5.4xlarge | H100 80GB × 1 | 16 | 시연 기준에 꼭 맞음 | 서울 가격 없음(서울 제공 확인 못 함, 가격 집계에 "-") | — |
| p5en.48xlarge | H200 141GB × 8 | 192 | 충족(과함) | 87.3485 | 24.6585(2a) |
| p4d.24xlarge | A100 40GB × 8 | 96 | 1장 40GB: Tier 0 단독(24GB↑)만, 분석 동시 실행(48GB↑) 미달 | 30.4103 | 10.1367(2b) |
| g7e.2xlarge | RTX PRO 6000 Blackwell 96GB × 1 | 8 | 80GB↑·vCPU 8 충족. Blackwell(sm_120)은 실제로 돌려 본 적 없음(19.1) | 4.1348 | 2.8447(2b) |
| g7e.4xlarge | 위 × 1 | 16 | 위 + CPU 여유 | 4.9155 | 4.4240 |
| g6e.2xlarge | L40S 48GB × 1 | 8 | 48GB(최소, VLM 몫 미결 19.1)·vCPU 8 | 2.7565 | 1.4211(2a) |
| g6e.4xlarge | L40S 48GB × 1 | 16 | 위 + CPU 여유 | 3.6936 | 1.7616 |
| g6e.xlarge | L40S 48GB × 1 | 4 | vCPU 8 미달 | 2.2880 | 1.3555(2a) |
| g5.2xlarge | A10G 24GB × 1 | 8 | Tier 0 단독만(분석 부하 금지) | 1.4903 | 0.6811 |
| g5.xlarge | A10G 24GB × 1 | 4 | vCPU 미달 | 1.2370 | 0.5834(2d) |
| P6-B300 | B300 × 8 | — | 가격·크기 확인 못 함 | — | — |

가격 출처: DoiT 스팟·온디맨드 가격 집계(compute.doit.com/spot/ap-northeast-2/<종류>, 2026-11-14 조회, Linux 로 보이나 페이지에 OS 표기 없음). **AWS 공식 값이 아니다.**
운영 서버는 공식 Price List·DescribeSpotPriceHistory 를 직접 읽고 이 표는 그것을 못 읽을 때의 예비값이다. 스팟은 시시각각 바뀐다. 참고: us-east-1 p5.4xlarge 온디맨드 $6.88/h(같은 집계).
그 밖 비용: 공인 IPv4 $0.005/h, 루트 EBS gp3(200GB 약 $0.0146/h 수준, 인스턴스를 지우면 함께 지움), 인터넷 송신(영상은 들어오는 방향이라 거의 없음).

추천(개발·시험): `g7e.2xlarge`(서울에서 1장 80GB 이상인 유일한 선택지, 단 Blackwell 첫 실행 위험) → `g6e.4xlarge` → `g6e.2xlarge`. 시연 기준 H100 80GB 를 서울에서 지키려면
`p5.48xlarge`(시간당 약 76달러, 8장 중 1장만 씀)뿐이라 시연 당일 하루 유지(3.12)면 약 600~900달러다. 운영 GPU 기준(H100 80GB)을 지키는 쪽은 지금처럼 RunPod 일본 H100 이 훨씬 싸다.

## 서비스 할당량(Service Quotas, EC2, 서울)

| 할당량 이름(콘솔) | 단위 | 필요(예) |
|---|---|---|
| Running On-Demand G and VT instances | vCPU | g6e·g7e.2xlarge 8, .4xlarge 16 → 16 신청 |
| All G and VT Spot Instance Requests | vCPU | 스팟을 쓸 때 16 |
| Running On-Demand P instances | vCPU | p5.48xlarge·p5en 192, p4d 96 |
| All P5 Spot Instance Requests / All P4, P3 and P2 Spot Instance Requests | vCPU | P 스팟을 쓸 때 |

새 계정은 G·P 가 0 인 경우가 많다(콘솔에서 지금 값 확인). 대기 시간은 공식 기준이 없다: 작은 G 신청(8~16 vCPU)은 보통 몇 시간~2일, P(192 vCPU)는 지원 사례 검토로
며칠이 걸리거나 사용 이력이 적은 계정은 거절될 수 있다(경험치, 확인하지 않음). 할당량이 없으면 켜기가 `VcpuLimitExceeded` → 502, 켠 요청 사건 detail 에 코드가 남는다.

## AMI

AWS Deep Learning Base OSS Nvidia Driver GPU AMI(Ubuntu 22.04): NVIDIA 드라이버·Docker·NVIDIA Container Toolkit·AWS CLI v2 가 들어 있어 user-data 가 그대로 돈다.
최신 AMI ID 는 SSM 공개 파라미터 `/aws/service/deeplearning/ami/x86_64/base-oss-nvidia-driver-gpu-ubuntu-22.04/latest/ami-id` 로 찾는다(리전별 값, AWS DLAMI 문서에서 이름 확인 2026-11-23).
AMI 이름 형식은 `Deep Learning Base OSS Nvidia Driver GPU AMI (Ubuntu 22.04) YYYYMMDD`. 20261113 판 릴리스 노트: NVIDIA 595.112.07, 기본 CUDA 13.2, supported_ec2_instances 에 G7e(-840 이 확인, 판단 방법은 설계 메모).
**드라이버가 CUDA 13 을 지원해야 한다**(3.12, R580 이상). G7e(Blackwell RTX PRO 6000)를 쓰면 그 GPU 를 지원하는 판인지도 본다. 확인하지 못했다(체크리스트 8).
대안: Ubuntu 공식 AMI + 직접 설치(시간 김, 굽기 필요) — 버렸다.

## 보안 그룹(`simai-gpu`)

| 방향 | 포트 | 출처 | 까닭 |
|---|---|---|---|
| 인바운드 TCP | 22 | EC2 서버 공인 IP/32, 팀 IP 대역 | 호스트 SSH(키 페어). SSAFY 망은 EC2 점프(`ssh -J simai-ec2`) |
| 인바운드 TCP | 17000-17001 | EC2 서버 공인 IP/32, (집에서 직접 붙을 때) 노트북 공인 IP/32 | I0 제어·영상. SSAFY 망 기본은 노트북 → EC2 SSH 로컬 포워드 → 인스턴스(3.12), Tier 0 알림 주소 127.0.0.1 그대로 |
| 아웃바운드 | 전체 | — | EC2 등록(443)·S3 서명 주소·ECR·Secrets Manager·HF |

0.0.0.0/0 으로 열지 않는다(Tier 0 는 TLS·기기 토큰이 있지만 표면을 줄인다).

## IAM

**EC2 서버(전용 IAM 사용자 `simai-gpu-launcher`, 액세스 키 → EC2 `.env`)** — `<ACCOUNT>`·`<LT_ID>` 는 실제 값으로.

```json
{
  "Version": "2012-10-17",
  "Statement": [
    { "Sid": "Describe", "Effect": "Allow",
      "Action": ["ec2:DescribeInstances", "ec2:DescribeInstanceTypes", "ec2:DescribeInstanceTypeOfferings",
                 "ec2:DescribeSubnets", "ec2:DescribeSpotPriceHistory"],
      "Resource": "*", "Condition": { "StringEquals": { "aws:RequestedRegion": "ap-northeast-2" } } },
    { "Sid": "Pricing", "Effect": "Allow", "Action": "pricing:GetProducts", "Resource": "*" },
    { "Sid": "RunTaggedInstanceFromTemplate", "Effect": "Allow", "Action": "ec2:RunInstances",
      "Resource": "arn:aws:ec2:ap-northeast-2:<ACCOUNT>:instance/*",
      "Condition": { "StringEquals": { "aws:RequestTag/simai:managed": "gpu",
                                       "ec2:LaunchTemplate": "arn:aws:ec2:ap-northeast-2:<ACCOUNT>:launch-template/<LT_ID>" },
                     "StringLike": { "ec2:InstanceType": ["g5.*", "g6e.*", "g7e.*", "p4d.*", "p5.*", "p5en.*"] } } },
    { "Sid": "RunOtherResources", "Effect": "Allow", "Action": "ec2:RunInstances",
      "Resource": ["arn:aws:ec2:ap-northeast-2::image/*",
                   "arn:aws:ec2:ap-northeast-2:<ACCOUNT>:launch-template/<LT_ID>",
                   "arn:aws:ec2:ap-northeast-2:<ACCOUNT>:subnet/*",
                   "arn:aws:ec2:ap-northeast-2:<ACCOUNT>:security-group/*",
                   "arn:aws:ec2:ap-northeast-2:<ACCOUNT>:network-interface/*",
                   "arn:aws:ec2:ap-northeast-2:<ACCOUNT>:volume/*",
                   "arn:aws:ec2:ap-northeast-2:<ACCOUNT>:key-pair/*"] },
    { "Sid": "TagOnCreateOnly", "Effect": "Allow", "Action": "ec2:CreateTags",
      "Resource": ["arn:aws:ec2:ap-northeast-2:<ACCOUNT>:instance/*", "arn:aws:ec2:ap-northeast-2:<ACCOUNT>:volume/*"],
      "Condition": { "StringEquals": { "ec2:CreateAction": "RunInstances" } } },
    { "Sid": "TerminateOnlyOurs", "Effect": "Allow", "Action": "ec2:TerminateInstances",
      "Resource": "arn:aws:ec2:ap-northeast-2:<ACCOUNT>:instance/*",
      "Condition": { "StringEquals": { "aws:ResourceTag/simai:managed": "gpu" } } },
    { "Sid": "PassInstanceRole", "Effect": "Allow", "Action": "iam:PassRole",
      "Resource": "arn:aws:iam::<ACCOUNT>:role/simai-gpu-instance",
      "Condition": { "StringEquals": { "iam:PassedToService": "ec2.amazonaws.com" } } }
  ]
}
```

- 지우기를 `simai:managed=gpu` 태그로 묶는 까닭: 부팅 비밀값을 가진 누군가가 G1 등록에 다른 `instance_id` 를 실어도 서버가 계정의 다른 인스턴스를 지우지 못한다.
- 스팟을 처음 쓰면 서비스 연결 역할 `AWSServiceRoleForEC2Spot` 이 필요하다(콘솔에서 한 번 만들거나 스팟을 한 번 콘솔로 요청).

**인스턴스 역할(`simai-gpu-instance`, 시작 템플릿의 IAM 인스턴스 프로파일)**

```json
{
  "Version": "2012-10-17",
  "Statement": [
    { "Sid": "BootSecret", "Effect": "Allow", "Action": "secretsmanager:GetSecretValue",
      "Resource": "arn:aws:secretsmanager:ap-northeast-2:<ACCOUNT>:secret:simai/gpu-boot-*" },
    { "Sid": "EcrLogin", "Effect": "Allow", "Action": "ecr:GetAuthorizationToken", "Resource": "*" },
    { "Sid": "EcrPull", "Effect": "Allow",
      "Action": ["ecr:BatchGetImage", "ecr:GetDownloadUrlForLayer", "ecr:BatchCheckLayerAvailability"],
      "Resource": "arn:aws:ecr:ap-northeast-2:<ACCOUNT>:repository/simai/tier0" }
  ]
}
```

S3 권한은 주지 않는다(GPU 캐시는 -700 서명 API). 비밀을 고객 관리 KMS 키로 암호화했다면 그 키의 `kms:Decrypt` 를 더한다(기본 `aws/secretsmanager` 키는 필요 없음).

## 이미지 받기 시간 줄이기

1. **ECR 서울 사본(정함, 필수)**: Docker Hub 비공개 저장소는 인스턴스에 Docker Hub 자격 증명(또 하나의 비밀)이 필요하다. ECR 서울은 인스턴스 역할로 로그인하고 같은 리전이라 빠르다.
   user-data 는 이미지가 `*.dkr.ecr.*.amazonaws.com/*` 면 `aws ecr get-login-password` 로 로그인한다. 다이제스트를 고정해 RunPod 과 같은 판을 쓴다(사본 올리기는 외부 전송이라 사용자 확인 뒤).
2. **AMI 에 이미지 미리 굽기(권장, 두 번째 단계)**: DLAMI 로 한 번 띄워 `docker pull` 한 뒤 AMI 를 만들어 시작 템플릿에 넣는다. user-data 는 `docker image inspect` 로 있으면 받지 않는다.
   단 EBS 스냅샷은 처음 읽을 때 블록을 늦게 가져와(지연 로딩) 첫 기동이 느릴 수 있다 → 빠른 스냅샷 복원(FSR, 가용 영역·스냅샷마다 시간 과금)이나 가중치 선읽기로 보완. 이미지가 바뀔 때마다 AMI 를 다시 굽는다.
3. EBS 스냅샷만 따로 붙이기: 2 와 같은 지연 로딩 문제에 시작 템플릿 관리가 늘어나 버렸다.

## RunPod 과 차이

| | RunPod(지금) | AWS 서울 |
|---|---|---|
| 과금 | 초 단위(컨테이너 디스크 포함), Pod 지우면 끝 | Linux 초 단위(최소 60초) + EBS·공인 IPv4. 지우면 EBS 도 지움(DeleteOnTermination) |
| 포트 | 외부 포트가 무작위 매핑(`RUNPOD_TCP_PORT_*`) | 공개 IP·듣는 포트 그대로(`SIMAI_PUBLIC_PORT_*=같은 값`) |
| 재고 | 카탈로그가 지역별 재고 HIGH~LOW 를 준다 | 실시간 재고 API 없음(「모름」), 만들기가 `InsufficientInstanceCapacity` 로 거절. 계정 할당량이 먼저 |
| 시작 시간 | 호스트에 이미지가 있으면 수십 초 | 인스턴스 부팅 1~3분 + 이미지 받기(ECR 서울, 굽기면 생략) → 목표 5~7분(-700)에 몇 분 더할 수 있음(실측 필요) |
| 지연 | EC2(서울) → 일본 Pod 약 41 ms | EC2 → 서울 인스턴스 수 ms(같은 리전, 실측 필요) |
| 비밀 | RunPod Secret 참조 글자 | Secrets Manager + 인스턴스 역할 |
| 단일 H100 80GB | 있음(시간당 약 3달러 안팎) | 서울은 8장 묶음 p5.48xlarge 뿐(시간당 약 76달러) |
| SSH | 컨테이너 sshd(계정 키) | 호스트 sshd(키 페어) → `docker exec` |

## 신청·준비 체크리스트(사람이 콘솔에서, 순서대로)

**[스크립트]** 표시는 `deploy/aws/setup_gpu_aws.sh`(S15P31A101-840, `check` 읽기만·`apply` 멱등, 사용법 `deploy/aws/README.md`)가 한다. 기본 인스턴스는 2026-11-23 사용자 결정으로 `g7e.4xlarge`(vCPU 16, 서울 온디맨드 할당량 승인됨).


1. 계정·결제: 팀 S3 버킷(`simai-k15a101-media-apne2`)과 같은 AWS 계정, 리전 서울. AWS Budgets 에 월 예산 경보를 건다.
2. Service Quotas(서울, EC2): `Running On-Demand G and VT instances` 16 vCPU(필요하면 `All G and VT Spot Instance Requests` 16, P 를 쓸 거면 `Running On-Demand P instances` 192) 신청 → 승인 대기.
3. **[스크립트]** ECR 서울 비공개 저장소 `simai/tier0` 만들기 → RunPod 이 쓰는 이미지 다이제스트를 그대로 복사(사용자 확인 뒤 push). 다이제스트를 적어 둔다.
4. EC2 서버에서 부팅 비밀값 확인(손, 원문이 없으면 `deploy/aws/README.md` 회전 절차 — `admin gpu-boot rotate --id gpu-boot-runpod`): RunPod 과 같은 `SIMAI_GPU_BOOT_ID` 의 원문을 쓴다(새로 만들면 `admin gpu-boot register --id <ID> --worker-id <워커>`, RunPod Secret 도 같이 바꿈).
5. **[스크립트]** Secrets Manager(서울) 비밀 `simai/gpu-boot`(다른 이름이면 `SIMAI_GPU_AWS_SECRET_ID`): 키·값 JSON `SIMAI_GPU_BOOT_SECRET`, `SIMAI_TIER0_TLS_PRIVATE_KEY`(고정 Tier 0 키, PEM 또는 base64 한 줄), `HF_TOKEN`, `SIMAI_TIER0_TLS_CERT_PEM`(공개 인증서).
6. **[스크립트]** IAM 역할 `simai-gpu-instance`(신뢰: ec2.amazonaws.com) + 위 인스턴스 정책 + 인스턴스 프로파일.
7. **[스크립트]** 보안 그룹 `simai-gpu`(위 표, EC2 서버 공인 IP/32). 키 페어 하나(호스트 SSH, 노트북 `~/.ssh/` 에만).
8. **[스크립트 check]** AMI 확인: DLAMI Base OSS Nvidia Driver(Ubuntu 22.04) 최신 ID, 드라이버 R580 이상(CUDA 13)인지 릴리스 노트로 확인. G7e 를 쓸 거면 Blackwell 지원 판인지.
9. **[스크립트]** 시작 템플릿 `simai-gpu`: AMI, 루트 gp3 200GB(삭제 시 함께 삭제), 키 페어, IAM 인스턴스 프로파일, 보안 그룹, **네트워크 인터페이스는 넣지 않음**(서버가 서브넷·보안 그룹을 정한다),
   인스턴스 종류·user-data 비움(서버가 정함), 메타데이터 IMDSv2 필수. 서브넷을 따로 쓰면 「공인 IPv4 자동 할당」이 켜진 서브넷이어야 한다(기본 VPC 는 켜져 있음). → `lt-…` 를 적는다.
10. **[스크립트]** IAM 사용자 `simai-gpu-launcher` + 위 서버 정책(`<LT_ID>` 채움) → 액세스 키 → EC2 `/opt/simai/.env` 에 `SIMAI_GPU_AWS_ACCESS_KEY_ID`·`_SECRET_ACCESS_KEY`(키는 다른 곳에 남기지 않는다).
11. **[스크립트, 재시작 제외]** EC2 `.env`: `SIMAI_GPU_AWS_ENABLED=true`, `SIMAI_GPU_AWS_IMAGE=<계정>.dkr.ecr.ap-northeast-2.amazonaws.com/simai/tier0@sha256:…`, (선택) `SIMAI_TIER0_PINNED_SPKI_SHA256` → 서버 재시작(배포).
12. 화면 「GPU 설정」: 공급자 AWS(목록에 「(준비 안 됨)」이 없어야 함), 선호 인스턴스 종류(기본 `g7e.4xlarge`, 그 밖 `g7e.2xlarge`·`g6e.4xlarge`), 선호 가용 영역(`ap-northeast-2a` …), 시작 템플릿 ID,
    서브넷·보안 그룹(선택), 온디맨드/스팟, 가격 상한, 자동 정지 분 저장. 재고 표에 칸이 「모름」으로 보이면 준비 끝.
13. 첫 켜기 뒤 확인: user-data 로그(`/var/log/simai-gpu-user-data.log`), 이미지 받기 시간, Secrets Manager 받기 로그(이름만), G1 등록(`provider=AWS`·공인 IP·17000/17001), 등록까지 시간, 끄기 뒤 인스턴스·EBS 가 사라지는지.

## 버린 대안

| 안 | 버린 까닭 |
|---|---|
| user-data 에 비밀 원문 | user-data 는 인스턴스 안 누구나(IMDS)·콘솔 권한자가 읽는다(명세 14) |
| 호스트에서 `aws secretsmanager` 로 받아 `--env-file` 로 넘기기 | DLAMI 에는 aws cli 가 있어 되지만 비밀이 호스트 파일·`docker inspect` 에 남는다. 컨테이너가 직접 받으면 simai-gpu-boot 프로세스 메모리에만 있다(-700 원칙) |
| 컨테이너에 boto3 추가 | 이미지 의존성이 늘고 요청 한 번에 과하다. 표준 라이브러리 SigV4 로 충분 |
| 인스턴스 역할에 S3 `job-objects/gpu_cache/*` 읽기·쓰기 | 서명 API 로 이미 되고, 역할 권한은 인스턴스가 사는 동안 오래 산다 |
| 시작 템플릿·서브넷·보안 그룹을 서버 `.env` 에 | RunPod 템플릿 ID 처럼 화면 설정(버전 이력)이 맞다. 비밀이 아니다 |
| 스팟 배치 점수(GetSpotPlacementScores)로 재고 표시 | 가용 영역 ID ↔ 이름 매핑과 종류마다 호출이 늘어난다. 스팟도 「모름」으로 두고 뒤로 미룸 |
| 용량 없음이면 다른 가용 영역 자동 재시도 | -702 와 같이 사람이 표에서 고른다(가격·지연이 다름) |
| AWS 공급자에 정지(stop) | 볼륨을 남기지 않는 설계(-702)와 같이 지우기 |

## 남은 위험

1. **실제 AWS 를 한 번도 부르지 않았다.** SDK 요청 모양은 단위 시험만 했다. 첫 켜기에서 IAM 조건(특히 `ec2:LaunchTemplate`·`aws:RequestTag` 조합, 스팟 서비스 연결 역할)이 막으면 `UnauthorizedOperation` 이 사건 detail 에 남는다.
2. 서울에 단일 H100 80GB 가 없다(p5.4xlarge 서울 미확인). 80GB 이상 1장은 g7e(Blackwell, 미실행 19.1), 48GB L40S 는 VLM 몫이 모자랄 수 있다(19.1). H100 은 p5.48xlarge 8장 묶음뿐이라 비싸다.
3. 온디맨드 재고를 미리 모른다(「모름」). 용량 없음은 켜기 거절로만 알고 다른 칸을 사람이 고른다. 새 계정 할당량 0 이면 켜기가 502(`VcpuLimitExceeded`)이고 화면 문구는 일반 오류다.
4. 예비 가격표는 제3자 집계 사본이다(공식 Price List 를 못 읽을 때만, 30분마다 다시 시도). Price List 권한(`pricing:GetProducts`)이 없으면 그 표를 쓴다.
5. user-data 는 첫 부팅에 한 번만 돈다(`--restart no`). docker 받기·실행이 실패하면 인스턴스는 등록 없이 BOOT_TIMEOUT(25분) 뒤 자동 정지된다(그동안 과금).
6. EC2 서버가 인스턴스 역할을 못 붙여 오래 사는 IAM 사용자 키를 `.env` 에 둔다. 최소 권한·태그 조건으로 묶었지만 키가 새면 GPU 를 켤 수 있다 → 주기 교체, Budgets 경보.
7. DLAMI 드라이버 판(CUDA 13·Blackwell 지원)과 이미지 크기·받기 시간, 서울 인스턴스 기동 시간(목표 5~7분 + α)은 실측하지 않았다.
8. 부팅 비밀값 ID 를 RunPod 과 같이 쓴다. 한쪽에서 새면 둘 다 바꾼다(`admin gpu-boot revoke`).
9. 스팟 중단(2분 예고)은 따로 처리하지 않는다: 지워지면 G1 STALE → 자동 정지 GONE/SESSION_ENDED 로 정리되고 관제는 끊긴다(-702 위험 7 과 같음).
10. 시작 템플릿에 네트워크 인터페이스가 있으면 서버의 서브넷·보안 그룹과 부딪혀 RunInstances 가 거절된다(체크리스트 9).

## 코드·시험 위치

- 서버: `domain/gpu/provider/{AwsGpuProvider,AwsGpuApi,SdkAwsGpuApi}`(새), `GpuProvider`(OfferQuery 후보 목록·`Network`·`LaunchSpec.network`, 옛 생성자 유지),
  `GpuPowerService`(공급자별 템플릿·망·`SIMAI_GPU_PROVIDER`, 재고 조회에 설정 후보 전달, 칸 가격), `GpuSettingsService`(AWS 칸·형식 검사), `GpuProviders`(주석),
  `global/properties/GpuProperties.Aws`, `application.yaml simai.gpu.aws`, `db/migration/V77__gpu_settings_aws.sql`, `build.gradle`(ec2·pricing, netty 제외), `deploy/ec2/compose.yaml`·`.env.example`.
- Pod: `deploy/scripts/simai_gpu_boot.py`(Secrets Manager·SigV4·IMDSv2), `deploy/scripts/simai_bootstrap.py`(`auto` 넘김 조건), `deploy/README.md`(AWS 문단).
- front: `features/gpu/{types,gpuStatus,GpuSettingsDialog}`(재고 UNKNOWN 「모름」, AWS 칸, 「준비 안 됨」).
- 시험: `server/.../provider/AwsGpuProviderTest`(가짜 AWS: 준비 안 됨·재고 표·가격 예비·스팟·만들기 요청·user-data 비밀 없음·서브넷·상태 매핑), `SdkAwsGpuApiTest`(RunInstances 모양·Price List 읽기·오류 매핑),
  `GpuPowerAwsIntegrationTest`(기본 꺼짐 503, AWS 설정 → 재고 표 → 켜기 → 끄기, 시작 템플릿 없음 503·용량 없음 409, 설정 형식), `ai/tests/deploy/test_simai_gpu_boot.py`(SigV4 문서 예시,
  가짜 IMDS·Secrets Manager 로 등록, 거절·일시 실패, `auto` 넘김), front `__tests__/features/gpu/GpuSettingsDialog.test.tsx`(AWS 칸).

## 명세 정리 대상

- 3.12: 실행 사이트에 AWS 서울(선택, 기본 꺼짐) 경로·후보 표·최소 사양 대비, 비밀 전달(Secrets Manager + 인스턴스 역할), 연결(공개 IP·직접 포트, EC2 경유 동일).
- 14: AWS 비밀 저장 위치(Secrets Manager)와 EC2 서버의 GPU 켜기 전용 IAM 사용자 키.
- 18: 운영 준비 체크리스트(할당량·ECR·Secrets Manager·시작 템플릿·IAM).
