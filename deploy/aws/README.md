# AWS 서울 GPU 준비 (`setup_gpu_aws.sh`)

`docs/design/S15P31A101-703-aws-gpu-provider.md` 「신청·준비 체크리스트」 3·5~11번을 한 스크립트로 한다(S15P31A101-840, 설계 메모 `docs/design/S15P31A101-840-aws-g7e-setup-script.md`).
기본 인스턴스는 `g7e.4xlarge`(RTX PRO 6000 Blackwell 96GB × 1, vCPU 16). **실제 AWS 에서 아직 한 번도 돌리지 않았다**(가짜 aws·docker·ssh 시험만).

## 준비물

- 노트북 aws cli(v1 1.46 이상 또는 v2)와 관리자 프로필: `aws configure --profile simai`(리전 `ap-northeast-2`). 할당량(체크리스트 2)은 이미 승인돼 있어야 한다.
- Docker(로그인된 Docker Hub, 원본 이미지를 받을 수 있어야 함), `ssh simai-ec2`(EC2 공인 IP 읽기·`.env` 쓰기, EC2 의 `sudo -n` 이 되어야 함).
- 원본 이미지 다이제스트 참조: RunPod 템플릿이 쓰는 `jonghwa1492/tier0@sha256:…`.
- Tier 0 TLS 키·인증서 파일(RunPod Secret 과 같은 고정 키. 예: `C:/Users/SSAFY/simai-local/tier0-tls/tier0.key`·`tier0.crt`).
- 부팅 비밀값 원문과 HF 토큰: 화면에 보이지 않게 묻거나 `--boot-secret-file`·`--hf-token-file`(본인만 읽는 파일)로 준다. 명령줄에 값을 쓰지 않는다.

## 순서

```bash
cd deploy/aws
./setup_gpu_aws.sh check                                   # 읽기만: 계정·할당량·가용 영역·서브넷·DLAMI 드라이버·자원 표
./setup_gpu_aws.sh apply --dry-run --source-image jonghwa1492/tier0@sha256:<64자> \
  --tls-key <tier0.key> --tls-cert <tier0.crt>              # 쓰기 호출을 출력만
./setup_gpu_aws.sh apply --source-image jonghwa1492/tier0@sha256:<64자> \
  --tls-key <tier0.key> --tls-cert <tier0.crt>              # 부팅 비밀값·HF 토큰을 묻는다
```

- `check` 가 실패(할당량 부족, 가용 영역 없음, 드라이버 R580 미만)면 `apply` 전에 고친다. 「주의」는 읽고 판단한다(DLAMI 판을 못 정하면 릴리스 노트 링크를 보여 준다).
- `apply` 는 다시 돌려도 된다: 있으면 건너뛰고, 정책·시작 템플릿은 바뀐 것만 새 판, 비밀은 `--update-secret` 일 때만 비교해 다르면 새 판.
- 이미 있는 IAM 사용자 키는 건드리지 않는다. 키를 새로 만들어 EC2 `.env` 를 바꾸려면 `--rotate-key`(예전 키는 서버 재시작을 확인한 뒤 IAM 콘솔에서 지운다).
- EC2 공인 IP 는 `--ec2-ip` 가 우선이고 없으면 `ssh simai-ec2` 로 읽는다. 팀 IP 를 22 번에 더 열려면 `--ssh-cidr`, 집 노트북에서 I0 를 직접 붙으려면 `--i0-cidr`.
- 개인 키는 `~/.ssh/simai-gpu-aws.pem`(600)에만 생긴다. 이미 있으면 건드리지 않고 공개 키만 올린다.

## 끝나고

1. 서버 재시작: 스크립트는 `/opt/simai/.env` 에 `SIMAI_GPU_AWS_ACCESS_KEY_ID`·`SIMAI_GPU_AWS_SECRET_ACCESS_KEY`·`SIMAI_GPU_AWS_ENABLED=true`·`SIMAI_GPU_AWS_IMAGE=<ECR@sha256>` 만 쓰고(백업 `.env.bak-<시각>`)
   재시작·compose 는 하지 않는다. Jenkins 배포 중이 아닐 때(「배포 완료」 로그 뒤) 다음 배포로 반영하거나 EC2 에서 `docker compose --env-file /opt/simai/.env -f /opt/simai/repo/deploy/ec2/compose.yaml up -d server`.
2. 화면 「GPU 설정」: 스크립트 끝에 찍힌 값을 넣는다 — 공급자 AWS, 선호 인스턴스 종류 `g7e.4xlarge`, 선호 가용 영역, 시작 템플릿 ID, 온디맨드. 재고 표에 칸이 「모름」으로 보이면 준비 끝(체크리스트 12).
3. 첫 켜기 검증: -703 체크리스트 13번과 -720 「다음 AWS 첫 실행 체크리스트」(`docs/design/S15P31A101-720-blackwell-sm120.md`).

## 부팅 비밀값 원문이 없을 때(회전, 손으로)

서버는 AWS 에도 RunPod 과 같은 부팅 비밀값 ID(`SIMAI_GPU_BOOT_ID`, 기본 `gpu-boot-runpod`)를 넘긴다. 원문은 RunPod Secret 에만 있어 꺼낼 수 없으므로 새로 발급해 두 곳을 함께 바꾼다.

1. EC2 에서 새 원문 발급(이전 원문은 즉시 무효, 한 번만 표시):
   `docker compose --env-file /opt/simai/.env -f /opt/simai/repo/deploy/ec2/compose.yaml run --rm server admin gpu-boot rotate --id gpu-boot-runpod`
   (ID 를 처음 만드는 경우만 `admin gpu-boot register --id gpu-boot-runpod --worker-id <워커 ID>`, 워커는 먼저 `admin worker register`. 폐기된 ID 는 `reactivate`.)
2. 출력된 원문을 본인만 읽는 파일에 두고 `./setup_gpu_aws.sh apply --update-secret --boot-secret-file <파일> --tls-key … --tls-cert … --hf-token-file …` 로 Secrets Manager 를 바꾼다.
3. RunPod 콘솔 Secrets 의 `simai_gpu_boot_secret` 도 같은 원문으로 바꾼다. 두 곳이 다르면 한쪽 공급자는 등록이 401 이다.
4. 원문 파일을 지운다. 켜져 있는 GPU 세션이 없을 때 한다.

## 키 정리

- 노트북 관리자 프로필(`simai`)의 키는 준비가 끝나면 IAM 콘솔에서 비활성화하거나 지운다(다시 필요하면 새로 만든다).
- `simai-gpu-launcher` 키는 EC2 `.env` 에만 있다. 주기적으로 `--rotate-key` 로 바꾸고 예전 키를 지운다. 새면 IAM 콘솔에서 바로 비활성화한다.
- 부팅 비밀값·HF 토큰 파일을 썼다면 지운다. 스크립트 임시 파일(umask 077)은 끝날 때 지운다.
