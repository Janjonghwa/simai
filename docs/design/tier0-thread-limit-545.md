# S15P31A101-545 Tier 0 수치 연산 스레드와 지연 계측

명세 3.2, 5.2, 6.6. Tier 0 위험 판정과 아바타 게이트는 바꾸지 않는다. 구현: `deploy/scripts/simai_bootstrap.py`, `ai/src/simai_ai/tracking/cross_view.py`, `ai/services/tier0/perception.py`. 시험: `ai/tests/deploy/test_simai_supervisor.py`, `ai/tests/tracking/test_cross_view.py`.

## 노트북 합성 벤치

Windows 11 (보고된 플랫폼 Windows-10-10.0.26200), Intel Family 6 Model 170, 논리 CPU 22, Python 3.11.15, NumPy 2.4.6. GPU 없음. `ai/`에서 설정별 새 프로세스로 `uv run --offline python tests/tracking/bench_world_track.py --people 3 4 5 6 --ticks 10` 실행. 각 설정에서 OMP, MKL, OPENBLAS, NUMEXPR에 동일 숫자를 지정했고 기본값은 네 환경 변수를 제거했다. 기존 벤치의 예열 2 tick 제외, seed 7+인원. 4대, COCO 17관절, 잡음·일부 누락·걷기 합성 입력이다. 아래는 평균 ms (cross-view / 실제 재사용 World Track); p95는 10개 표본의 보간값이다. 독립적 1회 측정이며 과학적 성능 주장이나 제품 정확도 지표가 아니다.

| 스레드 | 3명 평균 / p95 | 4명 평균 / p95 | 5명 평균 / p95 | 6명 평균 / p95 |
|---|---|---|---|---|
| 1 | 20.6 / 22.3; 2.5 / 3.2 | 28.1 / 31.1; 3.4 / 4.0 | 40.5 / 47.8; 4.8 / 5.2 | 47.8 / 51.6; 5.5 / 6.3 |
| 2 | 20.2 / 22.6; 2.6 / 3.2 | 27.8 / 30.9; 3.3 / 4.3 | 36.9 / 40.0; 5.1 / 7.2 | 48.1 / 53.0; 5.6 / 6.5 |
| 4 | 21.0 / 23.3; 3.3 / 4.0 | 29.1 / 32.0; 3.7 / 4.5 | 42.7 / 49.1; 4.2 / 4.7 | 48.3 / 51.5; 8.6 / 10.2 |
| 기본 | 19.9 / 21.8; 2.6 / 3.2 | 29.1 / 34.1; 3.6 / 4.4 | 41.9 / 48.6; 4.8 / 5.9 | 56.7 / 70.4; 6.6 / 7.9 |

셀은 `cross_view 평균 / p95; world_track_reuse 평균 / p95`. 최대값(ms, 같은 순서): 1 = 22.3/3.4, 31.6/4.0, 48.5/5.4, 52.1/6.4; 2 = 22.9/3.4, 31.5/4.7, 40.2/7.5, 53.0/6.5; 4 = 23.4/4.0, 32.3/4.6, 49.3/4.7, 51.8/10.6; 기본 = 22.0/3.5, 35.7/4.8, 51.4/6.2, 72.3/7.9.

차이가 작고 잡음이 커 스레드 과다가 원인인지 결론낼 수 없다. 기본값 1은 이 소규모 연산에서 2·4 대비 뚜렷한 손해가 없고 Pod의 제한된 CPU 경합을 줄이려는 보수적 선택이다. `SIMAI_TIER0_BLAS_THREADS` 양의 정수로 덮어쓴다. 기동 전에 OMP/MKL/OpenBLAS/NumExpr 값을 설정해 torch intra-op의 OMP 기본도 제한한다. 명시적인 `torch.set_num_threads`는 쓰지 않는다. 디코더 설정은 바꾸지 않지만 라이브러리 간접 영향과 검출 처리량은 Pod에서 확인해야 한다. 상속 값을 그대로 두거나 2·4를 기본으로 쓰는 대안은 과다 구독 방지 효과가 불확실하여 보류한다.

추가 단계 계측의 기본 설정 5명 12 tick(예열 포함): 쌍 계산 평균 7.775 ms, 클러스터링 25.964 ms, 최종 검증·삼각측량 3.487 ms. 이는 wall 시간이고 계측 자체의 비용도 포함한다. 다음 원인 후보는 클러스터링의 후보 병합·경쟁 계산이다. 재현용으로 위 벤치 도구의 `scene_frames` 루프에서 `associate(..., timing=StageTiming(), timing_people=5)`를 호출해 단계별 `summary()`를 확인한다. 세부 구간의 합은 준비·이력 갱신 등 나머지를 포함하지 않는다.

## 다음 GPU 세션 전후 비교

Lead의 `session13r/README.md`와 `start-lobby.sh` 기준, 같은 Pod 유형·할당 CPU·이미지 외 구성·4대 보정·Re-ID·워커·녹화 및 재생 속도를 유지한다. 동일 세션 장비를 쓰고 이미지/코드 변경 전후를 기록한다. 기존 버전(스레드 제한 전)과 이 변경을 포함한 버전 각각을 새로 기동하여 안정화 후 같은 `replay.sh normal <IP> <17000매핑> <17001매핑>` 녹화를 10분씩 반복한다. 변경 전에는 네 수치 연산 환경 변수의 실제 상속 상태를, 변경 후에는 기동 로그와 `SIMAI_TIER0_BLAS_THREADS`를 기록한다. 기본 1 외 비교는 별도 재시작하여 수행한다. 재생은 LIVE 입력이므로 해당 세션의 운영 경보 주의사항을 따른다.

각 실행의 status와 로그를 저장하고 사람 수 버킷별 `stage_timing`의 `judge_tick`, `cross_view_2d_triangulation`, `world_track` wall/thread_cpu 평균·p95·최대·표본 수를 비교한다. 변경 후에는 `cross_view_pairs`, `cross_view_clustering`, `cross_view_validate_triangulate`도 확인한다(이전 버전에는 없음). `detector_timing.detect_trt`, 입력·디코딩 지연, 프레임 드롭, CPU quota/throttling, 아바타 게이트 상태와 World 지연도 함께 비교한다. 통계 창은 최근 256개여서 10분 전체 분위수가 아니므로 주기적으로 status를 수집하고 창별 표본 수를 보존한다. 사람이 적은 버킷과 예열·장면 차이를 구분한다. 실제 효과, 디코딩 영향, torch 라이브러리별 스레드 동작은 Pod 확인 전 미해결이다. 수집 후 기존 절차대로 토큰을 폐기하고 Pod를 Stop한다.
