# S15P31A101-840 AWS g7e 준비 스크립트

관련 명세: 3.12(GPU 실행 사이트·최소 사양·이미지와 CUDA, R580 이상), 14(비밀 값 저장 위치·노출 금지).
앞 티켓: -703(AWS 서울 공급자, 이 문서는 그 「신청·준비 체크리스트」 3·5~11번을 스크립트로), -720(Blackwell sm_120 대응).
2026-11-23: 서울 G and VT 온디맨드 할당량 승인, 사용자가 `g7e.4xlarge` 로 정함. 인프라 담당 부재·Lead 에 AWS 자격 증명이 없어, 사용자가 관리자 프로필을 만들면 Lead 가 돌린다.
**실제 AWS·Docker Hub·EC2 에는 한 번도 돌리지 않았다**(가짜 aws·docker·ssh 시험만). 서버·front·ai 패키지 코드는 고치지 않았다(서버에 g7e.4xlarge 를 막는 곳 없음: 형식 정규식·가격표 4.9155 있음).

## 코드·시험 위치

- `deploy/aws/setup_gpu_aws.sh`(bash, Git Bash·Linux), `deploy/aws/README.md`(순서·회전 절차·키 정리).
- `ai/tests/deploy/test_setup_gpu_aws.py`: 상태를 파일로 기억하는 가짜 aws·docker·ssh(ssh 는 원격 명령을 시험 폴더 .env 에 그대로 실행 → .env 병합 스크립트까지 시험).
  할당량 부족·가용 영역 없음·옛 드라이버 실패, dry-run 쓰기 0, 처음 만들기 → 다시 돌리면 쓰기 0, `--update-secret` 같으면 건너뜀·다르면 put 한 번, `--rotate-key`,
  비밀 입력 없이 만들기 거부, 모든 경로에서 `SECRET-CANARY-…` 가 stdout·stderr·aws/docker/ssh 명령줄에 없음, 계정 ID 가림.
- 문서: -703 체크리스트에 [스크립트] 표시·`g7e.4xlarge` 기본·DLAMI 파라미터 확인.

## 정한 값과 근거

| 항목 | 값 | 근거 |
|---|---|---|
| 인스턴스 기본 | `g7e.4xlarge`(vCPU 16) | 사용자 결정. -720 위험 5(vCPU 8 에서 Tier 0 4코어)를 피한다. 할당량 L-DB2E81BA ≥ 16 을 check 가 본다 |
| DLAMI | SSM `/aws/service/deeplearning/ami/x86_64/base-oss-nvidia-driver-gpu-ubuntu-22.04/latest/ami-id`(대상 리전에서 읽음) | AWS DLAMI 문서 「AWS Deep Learning Base GPU AMI (Ubuntu 22.04)」의 SSM Parameter Query(아래 출처). AMI 이름 `Deep Learning Base OSS Nvidia Driver GPU AMI (Ubuntu 22.04) YYYYMMDD` |
| 드라이버 판단 | ① AMI 설명에 `driver … NNN.xx` 가 있으면 주 번호 ≥ 580 ② 없으면 이름 끝 날짜 ≥ 20261113 이면 통과 ③ 그 밖 「주의」+릴리스 노트 링크. G7e 는 설명의 지원 목록 → 없으면 같은 날짜 기준 | 20261113 판 릴리스 노트: `nvidia_driver 595.112.07`, `default_cuda /usr/local/cuda-13.2/`, `supported_ec2_instances … G7, G7e …`. 설명 문구 형식은 문서로 확인하지 못해 날짜 기준을 예비로 둔다. 580 미만은 실패(-720 위험 2) |
| 정책 | 고객 관리형 `simai-gpu-instance-access`·`simai-gpu-launcher-access`(설계 JSON 그대로, 리전·계정·시작 템플릿 ID 채움) | 인라인 정책은 판이 없다. 관리형은 바뀐 것만 새 판(기본 판 문서와 공백 뺀 비교, 판 5개면 가장 오래된 비기본 판 삭제) |
| 시작 템플릿 | AMI·키 페어·인스턴스 프로파일 이름·보안 그룹 1개·루트 gp3 200GB 삭제 시 함께·IMDSv2 필수·홉 2. 네트워크 인터페이스·인스턴스 종류·user-data 없음 | 설계 체크리스트 9. 기본 판이 이 값들을 모두 갖고 세 칸이 없으면 같음, 아니면 새 판 + 기본 판 지정. 루트 장치 이름은 AMI 의 RootDeviceName |
| 보안 그룹 | 22·17000-17001 ← EC2 공인 IP/32(+`--ssh-cidr`·`--i0-cidr`) | 설계 표. IP 는 `--ec2-ip` 우선, 없으면 `ssh simai-ec2 curl https://checkip.amazonaws.com`. 없는 규칙만 연다(지우지 않음) |
| 키 페어 | `simai-gpu-aws`(ed25519), 개인 키 `~/.ssh/simai-gpu-aws.pem` 600 | 응답을 파일로만(CR 제거). 파일이 있으면 건드리지 않고 공개 키만 import |
| ECR 사본 | `simai/tier0`, 태그 `src-<원본 다이제스트 앞 12자>` | 다시 돌릴 때 같은 원본인지 태그로 안다(ECR 다이제스트가 원본과 다를 수 있음). push 에 시간 제한 없음. 결과 다이제스트를 ECR 에서 읽어 `.env` 에 |
| 비밀 | `simai/gpu-boot` JSON 키 `SIMAI_GPU_BOOT_SECRET`·`SIMAI_TIER0_TLS_PRIVATE_KEY`·`HF_TOKEN`·`SIMAI_TIER0_TLS_CERT_PEM` | `simai_gpu_boot.py` `AWS_SECRET_KEYS`·`AWS_PUBLIC_KEYS` 와 같은 이름. PEM 은 줄바꿈 그대로(JSON `\n`), 끝 줄바꿈 하나 |
| 비밀 전달 | 부팅 비밀값·HF 토큰은 `read -rs` 또는 파일, TLS 는 파일 경로(기본값 없음). JSON 은 awk 로 이스케이프해 umask 077 임시 파일 → `file://`, trap 으로 삭제 | 명령줄(ps)·화면·로그에 안 나오게. jq·python 없이(노트북 Git Bash) |
| IAM 키 | 응답을 변수로만 → 600 임시 파일 → `ssh … sudo -n bash -c <병합 스크립트>` 의 stdin | 키가 있으면 건너뜀(`--rotate-key` 때만 새로, 2개면 거부). EC2 쓰기가 실패하면 새 키 끝 4자리를 알려 지우게 한다 |
| EC2 .env | 있는 줄은 바꾸고 없으면 덧붙임, 백업 `.env.bak-<UTC 시각>`, 권한·소유자 유지(`cat >`). ENABLED·IMAGE 가 같고 새 키가 없으면 쓰지 않음(비밀 아닌 두 줄만 `grep` 으로 읽음) | 재시작·compose 는 하지 않는다(Jenkins 배포와 겹치지 않게, 11-15 사고). 안내 문장만 |
| 출력 | ID·이름·다이제스트·가용 영역·「GPU 설정」 값. 계정 ID 는 `****1234`(aws 오류·docker 출력도 가림) | 요청 |
| aws cli | v1·v2 공통 옵션만(`--query`/`--output text`, `file://`, `get-login-password`). Windows aws 의 CR 은 지우고, `file://` 은 `cygpath -m` | 노트북 v1 1.46(Windows python) |

## 버린 대안

- jq·python 으로 JSON 만들기: 노트북 Git Bash 에 jq 가 없고 Windows python 은 Git Bash 경로를 모른다. awk 이스케이프로 충분.
- 비밀을 `--secret-string '<JSON>'` 인자로: ps·셸 기록에 남는다.
- 인라인 역할·사용자 정책: 판 이력이 없다.
- 시작 템플릿 전체 JSON 비교: AWS 가 칸 순서·기본값을 바꿔 돌려줘 늘 다르게 보인다 → 필요한 값 포함 여부로 비교.
- 원본 IP 를 바꾸면 예전 규칙 자동 삭제: 쓰는 규칙을 지울 위험. 남은 규칙은 check 표에서 사람이 본다.
- 부팅 비밀값 회전 자동화(`admin gpu-boot rotate` + RunPod Secret API): RunPod 쪽을 바꾸는 외부 쓰기이고 켜진 세션을 끊을 수 있다 → README 의 손 절차.
- 서버 재시작까지 스크립트로: Jenkins 배포와 겹치면 롤백된다(11-15). 사람이 「배포 완료」 뒤에.

## 남은 위험

1. **실제 AWS 에서 한 번도 안 돌렸다.** 가짜 aws 는 우리가 쓰는 질의 모양에 맞춰 답한다. 실제 응답 차이(예: `get-policy-version` 문서 모양, `describe-images --filter`, `sort_by(imagePushedAt)`)가 있으면
   정책 새 판이 불필요하게 생기거나 단계가 실패할 수 있다. 첫 실행은 `check` → `apply --dry-run` → `apply` 순서로 보고 고친다.
2. **G7e 가용 영역.** 서울의 어느 가용 영역에 g7e.4xlarge 가 있는지 확인하지 못했다. check 가 `DescribeInstanceTypeOfferings` 로 보고 없으면 실패한다. 온디맨드 재고(용량)는 API 로 미리 알 수 없다(-703 위험 3).
3. **AMI 드라이버.** 20261113 판 릴리스 노트로 595.112.07·G7e 지원을 확인했지만, AMI 설명 문구 형식은 확인하지 못해 날짜 기준을 예비로 둔다. SSM latest 가 바뀌면 드라이버도 바뀐다(다시 apply 하면 시작 템플릿 새 판).
   실 GPU 확인은 -720 첫 실행 체크리스트 1.
4. IAM 정책 조건(`ec2:LaunchTemplate`·`aws:RequestTag`)은 -703 그대로이고 실제로 막히는지 모른다(-703 위험 1).
5. 원격 `.env` 병합은 원격 로그인 셸이 bash 라고 가정한다(`printf %q` 의 `$'…'`). EC2 Ubuntu 기본값.
6. shellcheck 가 노트북에 없어 돌리지 못했다(`bash -n` 과 시험만).

## 출처

- AWS Deep Learning Base GPU AMI (Ubuntu 22.04): https://docs.aws.amazon.com/dlami/latest/devguide/aws-deep-learning-x86-base-gpu-ami-ubuntu-22-04.html (2026-11-23 조회, SSM 파라미터·이름 형식·G7e 알림)
- 20261113 판 릴리스 노트: https://docs.aws.amazon.com/dlami/latest/devguide/aws-deep-learning-ami-gpubaseoss-ul2204-2026-11-13.md (드라이버 595.112.07, CUDA 13.2, G7e)

## 넘긴 일

- 실제 실행 결과(가용 영역, AMI ID·드라이버, 다이제스트, 걸린 시간)를 이 문서와 -703 에 덧붙이고, 명세 3.12·14·18 정리 MR 에 넘긴다(-703 「명세 정리 대상」).
