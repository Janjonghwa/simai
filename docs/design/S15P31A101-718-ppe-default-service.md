# S15P31A101-718 GPU 자동 부팅 기본 서비스에 PPE 넣기

관련 명세: 3.2(GPU 예산·접수), 3.12(최소 사양), 5.5(PPE). 선행: -507(PPE 서비스, 명시 선택), -577(가중치 단계 분리), -634(PPE 생존 파일, A3 결정 대기), -700(GPU 자동 부팅), -705(VRAM 별 서비스). Jira: <https://ssafy.atlassian.net/browse/S15P31A101-718>.

17차: 셀프서비스 자동 부팅 Pod 에서 PPE 가 뜨지 않아 관제 화면이 「보호구 감시 멈춤」. 작업 설정은 안전모·조끼 필수였다. `simai-gpu-boot` 가 `SIMAI_BOOT_SERVICES` 없이 lobby 기본(`tier0,vllm,worker,avatar`)을 썼고 RunPod 템플릿에도 그 값이 없었다.

## 결정

- **자동 부팅 경로만 ppe 를 더한다**(`simai_gpu_boot.BOOT_SERVICES = "tier0,vllm,worker,avatar,ppe"`). VRAM 70,000 MiB(`PPE_MIN_VRAM_MB`) 이상(H100 80GB·96GB급)에서만 이 값을 쓴다. 48GB급(45,000~70,000 MiB)은 0.95 를 다 쓰면 여유가 2 GiB 남짓이라 lobby 기본(ppe 없음)을 쓰고 단계 보고 설명에 'ppe 꺼짐: VRAM 부족'을 붙인다(독립 검토 지적). VRAM 을 모르면 lobby 기본(preflight 가 멈춘다). 24GB급 축소(`tier0`)와 `SIMAI_BOOT_SERVICES` 명시값 우선은 그대로다.
  - 버린 대안: `simai_bootstrap.LOBBY_SERVICES` 에 ppe 추가. 손 `start-lobby`·`preflight` 기본까지 바뀌고, -507 의 "명시 선택" 결정과 그것을 고정한 시험(`test_ppe_is_opt_in_...`)을 뒤집는다. -634 에서 Lead 결정 대기로 남긴 항목이라 이 티켓 범위를 넘는다. 자동 부팅만 바꾸면 영향은 셀프서비스 Pod 하나다.
  - 템플릿(`SIMAI_BOOT_SERVICES`)으로 넣지 않는다: RunPod·AWS 양쪽 템플릿을 따로 맞춰야 하고 빠뜨리면 같은 일이 난다.
- **아바타·PPE 가중치를 따로 준비한다**(`Supervisor.sidecar_errors`, 서비스별). 예전에는 한 단계(`sidecar-weights`)에서 함께 받아 PPE 가중치가 실패하면 아바타(시연 필수)도 뜨지 않았다. PPE 가 기본이 되면 이 결합이 새 위험이라 끊었다. 기록 단계 이름도 `avatar-weights`·`ppe-weights` 로 나눴다. 파일은 배타 생성이라 덮이지 않지만, 단계 이름으로 묶어 읽으면 아바타 실패 뒤 PPE 성공이 실패를 가린다(독립 검토 지적).

## 확인한 것(바꾸지 않음)

| 항목 | 결과 |
|---|---|
| PPE 가 죽음·멈춤 | `_tend`/`_failed` 가 PPE 만 백오프 뒤 다시 띄운다(포기는 vLLM 만). 생존 파일 멈춤도 PPE 만 KILL(-634). Tier 0·워커·아바타·vLLM 은 그대로 — 시험 추가 |
| GPU 몫 | PPE 기본 0.05 는 분석 예산(0.70) 밖의 별도 몫. Tier 0 0.20 + 분석 0.70 + PPE 0.05 = 0.95 ≤ 0.95 라 preflight 예산 점검 통과. 비율이라 80GB(약 4.0GB)·96GB(약 4.8GB) 모두 같고, 체크포인트는 약 0.1GB |
| 이미지 가중치 | 통합 이미지 `weights-ppe` 단계가 `weights/ppe/T1-v4-ALL-seed20261021-epoch08.pt` 를 `/opt/simai/baked` 에 굽는다. `check_image.sh` 2b 가 이미 구운 ppe 를 대조한다 |
| 공급자 | `boot_services` 는 RunPod·AWS 공통 경로라 둘 다 적용된다 |

## 바꾼 것

| 위치 | 내용 |
|---|---|
| `deploy/scripts/simai_gpu_boot.py` | `BOOT_SERVICES`·`PPE_MIN_VRAM_MB` 추가, `boot_services` 가 70,000 MiB 이상에서 그것을, 48GB급은 lobby 기본 + 'ppe 꺼짐' 설명을 돌려준다 |
| `deploy/scripts/simai_bootstrap.py` | 아바타·PPE 가중치 단계 분리(기록 `avatar-weights`·`ppe-weights`), 실패 알림·문(gate)을 서비스별로 |
| `deploy/scripts/check_image.sh` | 이미지 안 `simai-gpu-boot` 의 80·96GB 기본 목록에 ppe 가 있는지 점검 |
| `deploy/README.md` | 감시 프로세스 5·`SIMAI_BOOT_SERVICES` 설명 갱신, 손 경로 lobby 기본에 ppe 가 없다는 운영 안내 |
| `ai/tests/deploy/test_simai_gpu_boot.py`, `test_simai_supervisor.py` | 기본 목록 = lobby 기본 + ppe·예산 통과, 80GB 자동 부팅 lobby·preflight 인자, 96GB 켜짐·70,000 MiB 경계·48GB급 꺼짐, 아바타/PPE 가중치 한쪽 실패(기록 분리)·둘 다 실패, PPE 종료 시 PPE 만 재기동 |

## 잔여 위험

- PPE 를 켜면 몫 합이 상한 0.95 에 꼭 맞는다(예산 안 여유 0). 남는 0.05(80GB 약 4GB)에 드라이버와 CUDA 컨텍스트(프로세스당 수백 MB, PPE 로 하나 더)가 든다. 다음 GPU 세션에서 `nvidia-smi` 로 실측한다.
- 손 lobby 는 여전히 PPE 를 명시해야 한다(-634 A3 결정은 Lead 에게 남긴다).
