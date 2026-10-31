# Tier 0 perception.step CPU 프로파일 조사

S15P31A101-377. 관련 명세: §5.1~5.2(입력·추적·삼각측량·관절 검증), §6.6(끝-끝 지연), §20.4~20.5(입력 출처·Run 기록). 조사 범위에서 제품 코드·계약·지연 문턱은 변경하지 않는다. 리뷰 생략(사용자 지시).

**판정: CPU 융합의 주요 비용과 동시 부하의 추가 지연은 확인, 9차의 약 220ms 전체 원인은 보류.** 5명 CPU 융합은 단독 중앙/p95 52.67/69.70ms, 디코딩+JPEG 동시 실행 66.12/81.14ms였다. cProfile과 py-spy 모두 Cross-view가 약 66%, World Track이 약 27%로 나왔다. 실제 LIVE `step`에 포함되는 YOLO 추론·TrackTrack과 보정 콜백을 재생하지 않았으므로, 나머지 시간을 추론이나 GIL 탓으로 확정하지 않는다. 개선 코드는 이번 조사 범위에서 적용하지 않았다.

## 재현 범위와 입력

9차 j5 로그의 HKDBG `perception`은 `perception.step` 전체의 벽시계 시간이다. LIVE에서는 `DetectorFeed.poll`의 YOLO 추론·결과 변환·카메라 내 TrackTrack, 관측 수집, Cross-view, World Track/관절 게이트, 접지 추정, payload, 선택적인 보정/아바타 콜백까지 포함한다. `tracks`는 tick 뒤 export한 수이며 카메라별 검출 수·비교 후보 수가 아니다. 로그는 총 단계 시간이 0.3s를 넘은 tick만 기록하므로 전체 tick의 p50/p95나 인원별 분포로 해석하면 안 된다. 기록된 0.01s 단위 값에도 반올림 오차가 있다.

녹화 `rec-20261029-015512z-lobby-railing-pose`의 파일 목록·프레임 저널 첫 행을 확인했다. H.264·코덱 정보·프레임 헤더(시각/offset/size)·연결 이벤트는 있으나 저장된 2D 검출/관절은 없다. 따라서 GPU/모델을 사용하지 않고 다음처럼 합성했다.

- `FileCalibrationProvider`로 제공된 `session9/fastrot266/calibration.json`을 그대로 읽는다. PROVISIONAL, 2304×1296, 왜곡 계수 포함 카메라 4대다.
- 네 카메라 공통 전신 시야의 결정적인 위치 6개를 고르고, 1m 이상 간격을 확보한다. 인원별로 앞의 N개를 사용한다. 인체 높이 약 1.7m, COCO17, 미터·+Z 상방, XY ±8cm 주기 이동이다.
- 카메라 외부/내부 파라미터와 실제 왜곡식으로 투영한 뒤 픽셀 가우스 잡음 σ=2px와 독립 관절 누락 5%를 적용한다. seed=377, 신뢰도는 머리/어깨 0.9·나머지 0.75다. 누락은 None이다. 전신/카메라 통째 가림, ID 전환, 사람 교차는 기본 입력에 없다.
- 합성 `Observation2DFrame`을 `FrameFeed` 프로토콜로 실제 `Perception.step`에 넣는다. 보간 버퍼·Cross-view·삼각측량·World Track·관절 게이트·접지·payload는 제품 구현이다. 10Hz마다 네 카메라를 같은 시각에 공급하며 Local ID는 안정적으로 유지한다.
- 프레임/관절 합성은 측정 전 완료한다. GPU·영상 검출기·Local Tracker·네트워크·저널·보정 감시/자동 보정·아바타 콜백은 제외한다. 단독 실행과 부하 실행 모두 같은 입력과 시각을 사용한다.

이 재현은 **4대×N명×17관절의 융합 CPU 비용**을 비교한다. 실제 9차 관측을 재생하거나 `step` 전체를 재현한 것은 아니다. `frames.source=SIM`, `tracks.source=SIM`, `is_upper_bound=true`이며 제품 성능이나 G2 통과 근거로 사용하지 않는다. 실제 `step`에 포함된 GPU 추론 시간은 이번 CPU 조사로 추정하지 않는다.

## 측정 방법

일반 시간 측정은 인원/모드별 독립 프로세스에서 warmup 12 tick 이후 80 tick, 2회(총 160 표본)다. 매 tick 종료 뒤 100ms 주기에 맞춰 측정 밖에서 대기한다. 1회는 base→media, 2회는 media→base로 순서를 바꾼다. 표의 중앙/p95는 두 실행의 원시 표본을 합친 값이며 반복 중앙값 범위도 원본 JSON에 남긴다. 신뢰구간이나 장시간 운영 결과는 아니다.

`base`는 인지만 실행한다. `decode`는 PyAV로 실제 녹화 H.264 네 개를 각각 스레드 하나·25fps로 디코딩하고 BGR로 변환한다. 디코더/색 변환 native thread는 각각 1개, 색 변환 문맥은 카메라별로 재사용한다. `jpeg`는 녹화 첫 그림 네 개를 측정 전에 디코딩해 둔 뒤 한 스레드에서 카메라를 순회하며 전체 해상도 JPEG quality=85로 총 25장/s 인코딩한다. `media`는 둘을 함께 실행한다. 최신 그림만 메모리에 유지하고 출력은 즉시 폐기한다. 프레임 준비와 초기 import는 측정 밖이다. JPEG 25장/s는 명시적인 지속 부하 조건이며 실제 CRITICAL 사진의 비정기 burst 빈도와 같지 않다. 생산 디코더의 색 변환 기본 4스레드·NVDEC 경로도 대체하지 않는다.

cProfile은 별도 실행에서 warmup 12 tick 뒤 60 tick의 `step` 구간만 enable하고 대기 구간은 제외한다. py-spy 0.4.2는 별도 실행에서 warmup 뒤 실제 `os.getpid()`에 `record --native --gil --rate 20 --duration 11 --format raw`로 부착한다(측정 120 tick). `.venv/Scripts/python.exe`를 py-spy가 직접 실행한 첫 시도는 `Failed to find python version from target process`로 실패했고, 실제 PID 부착은 성공했다. launcher 프로세스와 실제 Python 프로세스의 차이로 판단했으며, 권한 문제나 native 미지원으로 해석하지 않는다.

처음 200Hz로 수집한 1~4명 실행에서는 `1.00s behind in sampling` 경고와 최대 약 7.6~9.0s의 step 정지가 관측됐다. 네 실행은 `spy-initial-base-*`로 보존하고 시간 집계와 비용 순위에서 모두 제외했다. 20Hz로 다시 수집했으며, 아래 표에는 1~6명 모두 20Hz 결과만 쓴다. 20Hz도 p95를 크게 늘리므로 일반 시간 집계에 넣지 않는다. native 심볼에는 DLL/PYD 스택이 있지만 일부는 주소나 확장 모듈 진입점으로만 풀리므로 함수 순위는 Python 프레임과 cProfile로 교차 확인한다.

py-spy의 `--gil` 결과는 GIL을 보유한 스택 표본만 포함한다. GIL 해제 상태의 H.264/JPEG native 실행과 GIL 대기 시간은 분모에 없으므로 표본 비율을 전체 CPU/벽시계 점유율로 쓰지 않는다. Windows `thread_time_ns`는 관측상 15.625ms 수준으로 양자화돼 GIL 대기 시간을 빼는 정밀 근거로 사용하지 않는다.

환경은 Intel64 Family 6 Model 170 Stepping 4(22 논리 CPU), Windows build 26200, Python 3.11.15, NumPy 2.4.6(OpenBLAS 0.3.31), PyAV 18.1.0, OpenCV headless 5.0.0.93이다. 기준 커밋은 `336b25b259a3436921ea6bea11aa105905223500`이다. GPU는 사용/조회하지 않았다. 전원·클럭·CPU affinity를 고정하지 않았고, 실측 간 다른 노트북 부하는 통제하지 못했다. 예비 비주기 실행의 5명 중앙값은 19.70ms였지만 정규 측정은 10Hz 주기 실행으로 통일했다. 이 차이를 특정 하드웨어 원인으로 단정하지 않는다.

## 인원별 시간

단위 ms, 각 셀은 **중앙 / p95**. 각 인원·모드에 160개 표본이다.

| 사람 | 단독 base | 디코딩+JPEG media | 중앙값 증가 |
|---:|---:|---:|---:|
| 1 | 23.12 / 26.24 | 31.41 / 43.20 | +8.30 (+35.9%) |
| 2 | 30.15 / 36.28 | 38.01 / 51.99 | +7.86 (+26.1%) |
| 3 | 35.13 / 41.90 | 45.33 / 60.21 | +10.20 (+29.0%) |
| 4 | 43.91 / 50.70 | 58.43 / 73.25 | +14.52 (+33.1%) |
| 5 | 52.67 / 69.70 | 66.12 / 81.14 | +13.45 (+25.5%) |
| 6 | 59.63 / 75.89 | 76.01 / 97.55 | +16.38 (+27.5%) |

일반 시간 측정의 전체 2,240 tick에서 200ms 초과는 0건이었다. 100ms 초과는 5명 base 1/160, 5명 media 2/160, 6명 media 6/160이었다. 6명 media의 2회차 단독 p95는 103.39ms로, 합산 p95만 보면 실행 간 변동이 가려진다. 전체 50회 실행에서 warmup 뒤 출력 트랙 최소/최대는 지정 인원과 일치했다. media의 실적은 각 카메라 25.11~25.43fps, JPEG 합계 24.24~25.00fps였다(준비 프레임도 카운터에 포함돼 decode는 25를 조금 넘는다).

프로파일러를 붙인 별도 실행의 중앙/p95는 다음과 같다. **이 값을 위 일반 측정과 섞지 않는다.**

| 사람 | cProfile (60 tick) | py-spy native/GIL (120 tick) | GIL 표본 수 / step 표본 수 |
|---:|---:|---:|---:|
| 1 | 33.22 / 37.30 | 25.55 / 36.09 | 60 / 60 |
| 2 | 45.25 / 49.91 | 33.68 / 43.34 | 66 / 66 |
| 3 | 62.39 / 74.34 | 41.41 / 62.66 | 80 / 80 |
| 4 | 72.81 / 92.22 | 50.75 / 70.29 | 99 / 98 |
| 5 | 87.01 / 106.36 | 61.64 / 131.98 | 131 / 131 |
| 6 | 107.77 / 136.91 | 69.06 / 91.51 | 130 / 129 |

## 함수별 비용과 원인 범위

cProfile 누적 시간의 **tick당 평균 ms**다. 호출 스택의 부모와 자식 시간이 겹치므로 행을 합산하지 않는다. `undistort_many`는 tick당 4회, `JointGate.update`는 인원 수만큼 호출된다.

| 함수 | 1명 | 2명 | 3명 | 4명 | 5명 | 6명 |
|---|---:|---:|---:|---:|---:|---:|
| `Perception.step` | 30.81 | 42.43 | 63.11 | 73.55 | 89.20 | 110.41 |
| `CrossViewAssociator.associate` | 25.75 | 32.35 | 45.68 | 51.00 | 58.64 | 71.61 |
| `CrossViewAssociator._pairs` | 11.23 | 13.00 | 17.55 | 19.69 | 21.58 | 26.96 |
| `CrossViewAssociator._validate` | 7.01 | 9.08 | 13.28 | 14.27 | 17.11 | 20.38 |
| `RigCamera.undistort_many` | 5.51 | 6.69 | 8.84 | 9.00 | 9.75 | 10.58 |
| `WorldTrackManager.update` | 3.71 | 7.98 | 14.11 | 18.53 | 24.14 | 33.27 |
| `JointGate.update` | 2.37 | 5.44 | 9.55 | 12.94 | 16.46 | 23.68 |

5명 self time 상위는 `numpy.linalg.svd` 6.69ms, `JointGate._gate` 2.92ms, Cross-view `_assign` 2.60ms, NumPy ufunc reduce 2.47ms, `_two_view_arrays` 2.33ms, einsum 2.25ms, `_refine_batch` 2.22ms, `undistort_radius` 2.11ms다. 전체 상위 25개 self/cumulative 함수와 호출 수는 데이터 JSON에 있다.

py-spy 5명 단독의 step 표본 131개 중 `associate` 87개(66.4%), `WorldTrackManager.update` 35개(26.7%), `_pairs` 32개(24.4%), `JointGate.update` 25개(19.1%), `_validate` 24개(18.3%)로 cProfile 비용 분해와 비슷했다. 함수의 여러 소스 줄은 한 함수로 합쳤고, 같은 스택의 중복 프레임은 한 번만 센 누적 표본이다. 이 비율은 겹치며 전체 벽시계 비율이 아니다.

현재 코드는 이미 S15P31A101-168·-237의 카메라별 왜곡 제거, 카메라 쌍별 후보 검증, 클러스터 묶음 삼각측량과 결과 재사용을 수행한다([기존 설계](tier0-geometry-budget.md)). 그래서 같은 벡터화를 새 개선안으로 중복 제안하지 않는다. 그래도 카메라 쌍의 N×N 비교, 최선 우선 병합, 사람마다 별도 관절 게이트와 작은 NumPy/LAPACK 호출 비용이 남아 있다. 본 입력에서 인원 증가에 따라 CPU 비용이 증가한다는 것은 확인했으나, 과거 코드나 다른 호스트와의 속도 차이를 이번 측정만으로 회귀라고 판정하지 않는다.

## GIL·동시 부하 판정

5명에서 같은 합성 입력을 두 차례씩 비교했다(각 160 표본).

| 부하 | 중앙 / p95 ms | base 대비 중앙 증가 |
|---|---:|---:|
| 없음 | 52.67 / 69.70 | — |
| H.264 4대×25fps + BGR | 65.68 / 79.19 | +13.01ms (+24.7%) |
| JPEG 총25fps | 52.64 / 69.07 | −0.03ms (변동 범위) |
| 둘 다 | 66.12 / 81.14 | +13.45ms (+25.5%) |

media py-spy 실행은 중앙/p95 90.54/146.90ms로 관측 교란이 컸다. 전체 GIL 표본 184개 중 인지 메인 스레드 130개, decode 스레드 46개, JPEG 스레드 8개였다. 즉 디코딩/JPEG 스레드가 GIL을 보유하는 구간은 확인된다. 그러나 이 표본만으로 메인 스레드의 GIL 대기 시간을 산출할 수 없다.

추가 인과 대조군으로 **이 하네스 프로세스에서만** `sys.setswitchinterval(0.001)`을 적용했다(기본 0.005, 제품 설정 변경 없음). 5명 media 2회 중앙값은 80.29/80.37ms, p95는105.28/107.88ms로 개선되지 않았다. 전환 비용과 CPU/메모리·스케줄링 경쟁도 섞이므로 GIL 경합 부재의 증거는 아니다. **동시 부하에 의한 지연 증가 확인, 지배 원인이 GIL이라는 주장은 보류**한다. 실제 9차의 키프레임부터 130~170패킷을 몰아 디코딩하는 이벤트 사진 burst는 이번 지속 25fps 조건과 다르다.

## 개선안과 후속 티켓 제안

아래 효과는 보장값이 아니라 본 합성 CPU 프로파일을 바탕으로 한 작업 가설이다. 새 Jira 티켓을 생성하거나 공유하지 않았다. 포인트는 팀 규칙의 상대 작업량이다.

| 우선 | 후속 스토리 제안 | 예상 효과·근거 | 작업량·완료 조건 |
|---|---|---|---|
| 1 | LIVE perception의 검출·Local Tracker·기하·콜백 시간을 분리하고 2D 재생 번들을 남긴다 | 220ms의 누락된 비용을 확인한다. 성능 향상 수치를 미리 주장하지 않는다. | 3pt. 실제 느린 tick에서 단계 합/전체 시간, 카메라별 검출 수, 후보 수, 전체 tick 분포, 버전/환경을 기록. 모델 출력 2D 관측과 보정으로 CPU 구간이 재생돼야 함. |
| 2 | World Track의 관절 게이트를 클러스터 전체 묶음 계산으로 바꾼다 | 5명 gate 누적 16.46/89.20=18.5%. 이 부분을 절반 줄이면 전체 CPU 구간 약9% 감소라는 Amdahl 추정(일반 중앙값 기준 약5ms 규모). 220ms 전체에 그대로 적용 불가. | 5pt. `WorldTrackManager` 호출 경계와 개인별 뼈/속도 이력은 유지. 기존 깨끗한/거친/가림 입력 회귀와 범주형·수치 동등성, 1~6명/10명 p95 재측정. |
| 3 | 카메라 쌍의 SVD·작은 배열 호출을 같은 판정식 안에서 더 묶는다 | `_pairs` 약24%·SVD self 약7.5%. pairs 비용을 25~50% 줄인다면 전체 CPU 구간 약6~12% 감소라는 가설. 앞 스토리 효과와 중복될 수 있음. | 5pt. 퇴화·해 유일성·이상치 거부·애매한 배정 결과까지 같아야 함. SVD를 정규방정식 고윳값으로 바꾸거나 후보를 임의로 버리는 방식은 제외. |
| 4 | 디코딩 부하와 인지의 프로세스 분리를 시제품으로 비교한다 | 본 5명 media 추가 중앙 13.45ms가 비교 출발점. GIL 공유는 없앨 수 있지만 CPU·메모리 경쟁은 남아 개선 미보장. | 3pt 시제품, 채택 구현은 별도. 영상 전체 복사 대신 최신성 있는 제한 큐/공유 메모리 검토, 촬영 시각·단조 ID·drop 정책·실패 복구 유지. IPC 포함 p95/CPU/메모리와 사진 burst를 비교해 채택/기각 기록. |

관절을 줄이거나 2대 미만 관측을 추정으로 채우기, 판정 문턱 완화, HMR로 위치 대체, 무조건적인 후보 잘라내기는 제안에서 제외한다. 기존 [기하 예산 설계](tier0-geometry-budget.md)의 묶음 게이트/병렬화 잔여 작업과 중복 티켓 여부를 Lead가 확인하면 된다. 요구사항/스키마의 의미는 바뀌지 않아 이번 조사로 요구사항 갱신은 필요 없다.

## 남은 위험과 검증

- 실제 9차 YOLO/TrackTrack·콜백·수신 지연·스레드 구성·정확한 실행 커밋을 재현하지 않았다. 노트북 CPU 결과를 H100 Pod 성능으로 환산하지 않는다.
- 기본 합성은 동시 촬영·전신·안정 ID·짧은 약9초 상태 이력이다. 장시간 가림/교차/상태 누적·오연결과 사진 burst를 대표하지 않는다. 단독 원인 확정은 후속 LIVE 단계 분리 계측에 넘긴다.
- 2회·고정 seed·전원/affinity 미고정 결과이며 cProfile과 native 수집도 교란한다. 프로파일 시간으로 운영 SLA를 판정하지 않는다.
- `uv run --offline --no-sync python -B -m pytest tests/geometry/test_triangulation.py tests/geometry/test_joint_gate.py tests/tracking/test_cross_view.py -o cache_dir=C:/Users/SSAFY/simai-local/codex-tmp/377/pytest-cache -q`: **114 passed in 10.01s**. basetemp는 사전 설정된 `PYTEST_ADDOPTS`를 그대로 사용했다.
- 두 새 스크립트 AST 파싱, worktree 내부 출력 거부 guard(실제 쓰기 없이 ValueError), 모든 측정 결과의 정확한 출력 인원 확인, Run 기록 **50개**의 JSON Schema 검증 통과. 제품 코드·의존성·계약 변경 없음. 커밋·push·reset·브랜치 삭제·GPU 사용·비밀 파일 읽기 없음.

## 재실행과 산출물

`ai/`에서 PowerShell로 실행한다. 네트워크·의존성 동기화 없이 기존 `.venv`를 사용하며 `.env`를 읽지 않는다. 모든 임시/원시 산출물은 지정된 외부 경로에만 둔다.

```powershell
$env:UV_CACHE_DIR='C:/Users/SSAFY/simai-local/codex-tmp/377/uv-cache'
$env:TEMP='C:/Users/SSAFY/simai-local/codex-tmp/377'
$env:TMP=$env:TEMP
$env:PYTHONDONTWRITEBYTECODE='1'
$env:PYTHONUTF8='1'

# 아래 n/mode/rep만 바꿔 같은 명령으로 일반 시간을 측정한다.
uv run --offline --no-sync python -B -m experiments.profile_perception --people 5 --ticks 80 --mode base --pace --output C:/Users/SSAFY/simai-local/codex-tmp/377/r1-base-5.json
uv run --offline --no-sync python -B -m experiments.profile_perception --people 5 --ticks 80 --mode media --pace --output C:/Users/SSAFY/simai-local/codex-tmp/377/r1-media-5.json

# 1~6명 cProfile / py-spy: 각 n으로 반복. 원시 산출물은 임시 경로에만 쓴다.
uv run --offline --no-sync python -B -m experiments.profile_perception --people 5 --ticks 60 --pace --cprofile C:/Users/SSAFY/simai-local/codex-tmp/377/cp-5.pstats --output C:/Users/SSAFY/simai-local/codex-tmp/377/cp-5.json
uv run --offline --no-sync python -B -m experiments.profile_perception --people 5 --ticks 120 --pace --spy-rate 20 --spy-output C:/Users/SSAFY/simai-local/codex-tmp/377/spy-base-5.raw --output C:/Users/SSAFY/simai-local/codex-tmp/377/spy-base-5.json

# 5명 decode/jpeg 대조군은 --mode decode / --mode jpeg, 각 80tick×2회.
# GIL 대조군도 80tick×2회, media에 다음 옵션만 추가한다.
uv run --offline --no-sync python -B -m experiments.profile_perception --people 5 --ticks 80 --mode media --pace --switch-interval-ms 1 --output C:/Users/SSAFY/simai-local/codex-tmp/377/switch-1.json

uv run --offline --no-sync python -B -m experiments.summarize_perception_profile
```

하네스는 모든 tick이 새 결과를 내는지와 warmup 뒤 출력 트랙이 요청 인원 이상인지를 검사한다. `--fail-above-ms 200`은 p95가 문턱을 넘으면 결과 저장 뒤 종료 코드 2를 내는 지연 재현 판정이다. 시간 문턱은 조사용이며 제품 요구사항을 수정하지 않는다.

영구 수치·표본·상위 함수·프로파일 해시·스키마 검증된 Run 기록은 `ai/experiments/runs/perception-profile-377/data.json`(gitignore, 노트북에만. 567 KB 라 저장소에 넣지 않는다)에 보관한다. 각 `runs[].record`가 `contracts/experiments/run_record.schema.json`을 통과한다. CPU 실행이라 GPU 필드는 NOT_USED, Docker 이미지가 없어 `env_hash`는 unavailable로 적는다. dirty worktree이므로 독립 리뷰/채택 근거가 아니다. 원시 `.pstats`/py-spy `.raw`는 tmp/377에만 두며 JSON의 URI·SHA-256으로 연결한다.

정규 시간 측정 뒤 하네스에 py-spy 빈도 옵션/메타데이터를 추가했다. 각 Run의 당시 스크립트 SHA-256과 최종 하네스 SHA-256을 별도로 남겼으며, 해당 변경은 입력 생성·step 호출·시간 계산을 바꾸지 않는다. 원시 영상 해시는 원본 manifest에 있고 이 문서의 데이터 JSON에는 manifest 경로·해시를 남겼다. Owner의 사람 이름은 지시서에 없어 Run에는 이슈의 Owner 역할로 기록했다.
