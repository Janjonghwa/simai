# 관측 픽셀 UV 텍스처 누적 (S15P31A101-89)

## 범위와 명세

기획서 §20.3 4번(2단계), §5.6·13.2(외형과 캡슐 폴백), §12.5의 `hmr_appearance_update`,
§11.5(외형 자산 보관), §3.2(Tier 0 우선)를 구현한다. `avatar_asset` 1.0.0의
`texture`, `surface_source.ratios/face_ratios`, `face_policy`, 버전 필드를 그대로 쓴다.
I1·계약·front·Tier 0 위치/관절/ID/판정은 바꾸지 않는다. 출력은 안전 판단 권한이 없는 Tier 1 외형이다.

**명세와 작업 지시의 차이:** §20.3은 UV 투영 도구로 PyTorch3D/nvdiffrast를 적었지만,
이번 Owner 지시서는 새 의존성 없이 torch/numpy 래스터화를 명시했다. 그 지시를 따랐다.
또한 지시서의 “EXCLUDED면 mask_uri 등을 비움”은 계약의 조건과 다르다. 정본 계약에 따라
EXCLUDED도 몸 텍스처가 있으면 출처 PNG를 보내고, 얼굴 관측 비율만 0으로 둔다.

## 코드와 데이터 흐름

```text
Tier 0 공유 snapshot + CalibrationProvider + HMR 집계 포즈
  → 기존 체형 고정 PersonRig의 LBS → World 메시 (Tier 0 pelvis에 정렬)
  → 렌즈 왜곡 제거 (색과 사람 마스크를 같은 K로 remap)
  → torch z-buffer (동시각 다른 사람 메시 포함)
  → UV별 가시성/시선각/마스크 검사 → RGB 가중 누적
  → 좁은 보간·얼굴 정책 → PNG 두 장 → AVATAR_ASSET 업로드
  → 새 avatar_asset 버전 알림 → 그 버전의 포즈
```

- `hmr/raster.py`: 원근 보정 깊이와 면 번호, canonical UV의 면·barycentric 대응.
- `hmr/appearance.py`: 사람 한 명의 ID 보호, 가중 누적, 출처 지도·얼굴 정책, 무손실 PNG.
- `hmr/appearance_stream.py`: 기존 루프·보정·입력 연결, 외형 버전 발행과 업로드 재시도.
- `hmr/appearance_report.py`: 선택한 진단 폴더에 버전별 PNG/메타데이터와 전후 비교 그림.
- `hmr/stream.py`, `services/avatar/`: 기본 꺼짐 설정, 자산 송신과 기존 포즈 연결.
- 시험: `tests/hmr/test_appearance.py`, `test_avatar_loop.py`, `tests/services/avatar/test_transport.py`.

MHR UV는 아래 원점이다. `gltf_export.py`와 동일하게 `v_gltf = 1 - v_mhr`를 한 번 적용하고
PNG의 위쪽 행을 v=0으로 둔다. UV 텍셀 중심은 `((x+.5)/N, (y+.5)/N)`, 카메라 픽셀 중심은
기존 OpenCV 보정의 정수 좌표다. 실제 내보낸 TEXCOORD_0와 합성 색 기울기로 방향을 검증한다.
자산 정점·UV 이음매의 대응은 MHR faces/texcoord_faces를 그대로 쓰며 임의 UV를 생성하지 않는다.

LBS는 기존 PersonRig와 부모 기준 관절 회전(모르는 손 관절은 bind)을 쓴다. 집계된 root 회전을 적용하고
관절 1을 Tier 0 pelvis에 맞춘다. pelvis가 없으면 메시 위치를 만들지 않는다. 지지면 Z=0이나
HMR translation으로 보충하지 않으며 뷰어 좌표 변환 `(x,z,-y)`를 여기에서 하지 않는다.

## 수치와 근거

| 항목 | 초기값 | 근거·의미 |
|---|---|---|
| 출력 | 1024×1024 RGB albedo, 단일 채널 uint8 출처 PNG | 요청된 기준 해상도. PNG는 무손실 |
| 카메라 시각 | World 시각 ±40ms, 묶음 내 최대 차이 40ms | 기존 공유 통로 100ms보다 좁혀 움직임에 따른 다른 시각 색 혼합을 줄임. 제품 합격 기준 아님 |
| 시선각 | 법선·시선 내적 ≥0.25 | 약 75.5°보다 비스듬한 표면 배제 |
| 시선 가중치 | 내적² | 정면 관측을 우선하고 측면의 부정확한 투영 영향 완화 |
| 해상도 가중치 | min(투영 삼각형 면적 / UV 텍셀 면적, 1) | 텍셀보다 작은 영상 정보를 과신하지 않음 |
| 마스크 경계 | 거리 <1px 배제, min(거리/4px, 1) | 가림 경계/분할 경계의 색 섞임 완화. bbox 경계도 같은 처리 |
| 이상 관측 | 기존 관측과 RGB 최대 채널 차이 >80/255 거절 | 강한 색 변화/잘못 붙은 사람을 보수적으로 배제. 첫 관측부터 적용하며 조명 변화는 잔여 위험 |
| 보간 | 관측에서 4방향 최대 2텍셀 확장 | 좁은 이음매만 메움. 보간 색·가중치를 관측 누적으로 되먹이지 않음 |
| 얼굴 흐림 | 15×15 Gaussian, sigma=3 | 관측된 얼굴 텍셀만 정규화 합성곱. 미관측 얼굴은 채우지 않음 |
| ID 재개 | 안정된 서로 다른 입력 30프레임 | 명세 초기값. 중복 시각은 안정 횟수에 포함하지 않음 |
| 누적 간격 | 회차 완료 뒤 최소 2000ms | 포즈 루프의 여유 확보. 느린 CPU에서도 매 프레임 누적으로 고착되지 않음 |
| 발행 | 새 관측이 있고, 관측 비율 +0.02 또는 마지막 발행 뒤 30000ms | 2%p 이상 개선 때 먼저 전송하고 색만 개선된 경우는 30초로 제한. 최초 관측은 바로 발행 |
| 래스터 임시 조각 | 262144 후보/묶음 | 큰 삼각형도 임시 텐서가 무제한 커지지 않게 함 |

가중치는 세 가중치의 곱이다. `T_new=(W_old*T_old+w*C_obs)/(W_old+w)`를 쓰고 보간은
W를 증가시키지 않는다. 카메라 순서는 ID 순으로 고정한다. 색·가중치 사본에서 계산하여
도중 admission 차단이면 그 회차 전체를 버린다. numpy/torch CPU와 CUDA가 같은 식을 쓴다.
CUDA 설정에서는 투영·법선·색 누적과 래스터화를 GPU에서 계산하고, 보정 remap·PNG는 CPU다.

z-buffer는 면 단위 최근접 깊이를 비교하며 타인의 메시를 같은 버퍼에 포함한다.
near plane을 가로지르는 메시가 있으면 그 카메라 관측을 통째로 제외한다(잘린 가림막 뒤 색을 채우지 않음).
아직 메시가 없는 사람이나 불확실 ID의 bbox는 보수적 가림 마스크로 뺀다.
다른 사람의 메시 대표 카메라도 시각 허용 범위를 통과해야 가림막으로 쓴다.
렌즈 왜곡은 저장소의 정방향/왕복 유효성 검사와 remap을 사용한다(NONE·PINHOLE·FISHEYE).
보정별 remap 캐시는 최대 4대이며 보정이 바뀌면 새 키를 쓴다.

얼굴 면은 MHR 정점·면 번호 JSON을 지정하거나, head/face/jaw/eye 이름 관절과 그 자식의
스킨 영향 ≥0.1 정점에 닿은 면으로 정한다. 자동 영역은 머리 전체를 포함하는 보수적 영역이다.
둘 다 알 수 없으면 기동을 실패시킨다. 얼굴을 모른 채 몸으로 취급해 보간하지 않는다.
정밀 얼굴 영역 파일은 `{asset_sha256, vertex_indices: [...], face_indices: [...]}`이며 자산 해시를 검사한다.
실자산의 얼굴 경계 확인은 9차 세션 필수 항목이다.

출처 값은 0/1/2, 얼굴은 128/130뿐이다(129 없음). `ratios`의 분모는 UV가 있는 표면 텍셀,
`face_ratios`의 분모는 얼굴 텍셀이다. atlas 빈 공간은 비율에서 제외한다.
EXCLUDED는 얼굴 누적 자체를 막고 모두 128로 둔다. BLURRED도 관측 텍셀만 130이다.

## ID와 버전

불확실 ID에서는 누적·발행을 멈추고 확정 자산을 유지한다. revision 변경 시 미확정 PNG와
누적 버퍼는 폐기하고 새 체형/외형을 30개의 안정 프레임 뒤 시작한다. 같은 revision의 일시 불확실은
기존 확정 누적을 유지한 채 30프레임을 다시 기다린다. 분할된 새 ID는 별도 상태이며 상속하지 않는다.

처음에는 기존처럼 텍스처 없는 GLB 버전 0을 올린다. 이후 PNG 두 장을 모두 올린 뒤 새 버전을
발행한다. 체형 GLB는 기존 불변 URI를 참조하고 PNG만 `vN/albedo.png`, `vN/surface_source.png`로 올린다.
서버의 기존 `file_name`, `content_type=image/png`, `purpose=AVATAR_ASSET`을 쓴다.
같은 버전 업로드 재시도는 PNG 바이트·메타데이터를 고정한다. 취소한 외형 업로드의 버전 번호는
재사용하지 않고 `previous_appearance_version`은 마지막 확정 버전을 가리킨다.
업로드 실패여도 마지막 확정 자산의 포즈 전송은 계속 시도한다.

`AvatarLoop.reannounce_appearance(world_id, version)`은 확정 이력의 자산을 그대로 다시 알리고
후속 포즈가 그 버전을 가리키게 한다. 다음 신규 버전의 번호는 되돌리지 않는다. 자동 갱신이 켜져 있으면
다음 발행 조건에서 다시 새 외형이 나올 수 있다. 영구 되돌리기는 외형 설정을 끄고 서비스를 재기동한다.
이는 새 stream_epoch이며, 이전 버전 파일·DB 기록은 지우거나 덮어쓰지 않는다.

## 9차 GPU 세션 실행과 확인

코드·의존성 추가 설치 없이 기존 통합 이미지에서 avatar를 선택하고 다음 환경을 주입한다.
비밀값은 기존 주입 경로를 유지한다. 여기에는 실제 비밀값을 기록하지 않는다.

```sh
# 기본 auto(CUDA 면 켜짐). 강제로 켜려면 SIMAI_AVATAR_APPEARANCE=1
# device 기본은 CUDA 가 있으면 cuda
export SIMAI_AVATAR_TEXTURE_SIZE=1024
export SIMAI_AVATAR_FACE_POLICY=OBSERVED
export SIMAI_AVATAR_APPEARANCE_FREEZE_FRAMES=30
export SIMAI_AVATAR_TEXTURE_INTERVAL_MS=2000
export SIMAI_AVATAR_TEXTURE_PUBLISH_MS=30000
export SIMAI_AVATAR_TEXTURE_OUTPUT_DIR="$SIMAI_ROOT/runs/20261030-s9/appearance"
# 검증한 얼굴 번호 파일이 있으면 SIMAI_AVATAR_FACE_REGION으로 지정한다.
```

기본 `SIMAI_AVATAR_APPEARANCE=auto`(2026-10-30 사용자 결정): CUDA 가 있으면 켜고 device 도 cuda 로 둔다.
CUDA 가 없으면(노트북·CI) 끄고 종전 GLB·대표색 경로를 유지한다. CPU 누적은 1인당 3.5초라 기본으로 켜지 않는다.
auto 로 켜졌는데 얼굴 영역을 정하지 못하면 경고를 남기고 누적만 끈다(아바타 루프는 계속). `1` 은 CPU 에서도 강제로 켜고
얼굴 영역 오류에서 멈추며, `0` 은 끈다. GPU 상한은 기존 avatar 프로세스의
메모리 제한 안에 포함한다. 별도 모델·GPU 프로세스·I5 배치 작업을 띄우지 않는다.

1. Tier 0 지연 감시가 허용하고 30샘플 체형 자산이 나온 뒤 외형 로그를 확인한다.
   `외형 누적 world_id=... device=cuda wall_ms=... cpu_ms=... gpu_ms=... accepted=... rejected=...`.
   cpu_ms는 process CPU 시간, wall_ms는 회차 전체 경과 시간, gpu_ms는 CUDA event로 잰
   준비/카메라 계산 구간들의 GPU 스트림 경과 시간(호스트 대기 포함, 순수 커널 합이 아님)이다.
   CPU에서는 gpu_ms=NA다. PNG/업로드 시간은 누적 시간 밖이므로 포즈 신선도/로그와 함께 본다.
2. `아바타 PNG 업로드 성공` 두 줄과 `외형 버전 확정 ... version=N observed=...`를 확인한다.
   객체 위치는 `s3://<bucket>/avatars/<world_id>/e<epoch>/vN/{albedo,surface_source}.png`다.
   GLB와 PNG URI가 서로 다르고 EXCLUDED에서도 몸 출처 PNG가 유지되는지 확인한다.
3. 로컬 진단 위치는 `$SIMAI_AVATAR_TEXTURE_OUTPUT_DIR/<world_id>/e<epoch>/vN/`의
   `albedo.png`, `surface_source.png`, `avatar_asset.json`이다. 진단 저장은 선택이며 실패해도 스트림은 계속된다.
   기존 파일의 내용이 다르면 덮어쓰지 않는다. 결과는 git에 넣지 않고 인스턴스를 지우기 전에 회수한다.
4. 한 사람이 앞/옆/뒤로 돌며 다른 카메라만 새 면을 보는 구간을 녹화한다. 처음 확정 버전과 나중 버전으로
   같은 atlas의 가려졌던 면이 새로 관측되는지, 얼굴 미관측이 남는지 확인한다. 다음 명령은
   두 albedo와 두 출처 지도를 같은 크기로 놓은 비교 PNG를 만든다(초록 관측·노랑 보간·회색 미관측·분홍 얼굴 테두리).

```sh
uv run --offline python -m simai_ai.hmr.appearance_report \
  "$SIMAI_AVATAR_TEXTURE_OUTPUT_DIR/<world_id>/e<epoch>/v1" \
  "$SIMAI_AVATAR_TEXTURE_OUTPUT_DIR/<world_id>/e<epoch>/v3" \
  --output "$SIMAI_ROOT/runs/20261030-s9/appearance-before-after.png"
```

5. 사람이 앞을 가리는 구간, ID 불확실/교정, 분할 ID, 감시 차단에서 색 혼합/누적 재개/새 버전/캡슐을 확인한다.
   전후 atlas 비교는 관측 증가 증거일 뿐이다. 같은 가린 카메라 시점의 3D 표시 품질은 별도 front 텍스처
   그리기 티켓이 연결된 뷰어에서 판정한다. CPU 합성 수치를 제품 성능으로 쓰지 않는다.
6. 실제 인원·카메라 수에서 Tier 0 지연과 avatar 포즈 500ms 신선도, 전체 VRAM, 누적/PNG/전송 비용을 측정한다.
   기준 미달이면 2단계는 HOLD하고 설정을 끈 1단계로 시연한다. 세션 실행자는 기존
   `contracts/experiments/run_record.schema.json` Run 기록을 남기고 종료 뒤 GPU를 Stop한다.

## 로컬 검증과 측정

합성 상자의 해석적 광선-평면 렌더러는 누적 래스터화와 독립이다. 2·4대 카메라의 알려진 RGB 기울기를
복원하고 관측 텍셀 평균 절대 오차 <2/255를 확인한다. 한 카메라에만 보이는 면, 미관측 바닥,
다른 메시 가림, 화면 밖·마스크, PINHOLE/FISHEYE, 이상 색 거절과 가중 평균, ID 보호,
세 얼굴 정책, 2텍셀 보간 상한, 버전/PNG 재시도, 계약 검증, 진단 파일·비교 그림을 검사한다.

2026-10-30 Windows 로컬 CPU, torch 2.11.0+cpu, torch 스레드 1, 상자 24정점/12면,
1024² atlas와 128² 합성 영상 4대, 연속 세 회차의 최종 구현 측정:

| 회차 | wall_ms | cpu_ms | gpu_ms |
|---|---:|---:|---|
| 1 | 3528.925 | 3468.750 | NA |
| 2 | 3769.263 | 3734.375 | NA |
| 3 | 3477.887 | 3281.250 | NA |

최초 UV 대응표 준비 897.626ms, 보간·두 PNG 생성 474.257ms였다. 관측 0.640631,
보간 0.007042, 미관측 0.352327, 얼굴 관측 0.953062였다. 이 측정은 합성 단위 경로의
노트북 CPU 비용이며 EXP Run/실자산 성능 측정이 아니다. 실제 MHR·1080p·CUDA·다인 지연을 추정하는 근거로 쓰지 않는다.
재현은 `tests.hmr.test_appearance`의 `accumulator(resolution=1024, seam=2)`,
카메라 (3,2,1), (-3,2,1), (-3,-2,1), (3,-2,1), t=1000/1001/1002와 동일 시각 view를 사용한다.

`ai/`에서 `UV_CACHE_DIR=.uv-cache`, `PYTHONUTF8=1`, `PYTHONDONTWRITEBYTECODE=1`을 주고 실행했다.
pytest 임시 폴더는 모두 worktree 안으로 지정했으며 종료 뒤 삭제했다.

- `uv run --offline pytest tests/hmr -q -p no:cacheprovider --basetemp=.pytest-tmp-89` → 78 passed, 3 skipped(실제 MHR 자산 미지정).
- `uv run --offline pytest tests/services/avatar -q -p no:cacheprovider --basetemp=.pytest-tmp-89-services` → 13 passed.
- 마지막 bbox 경계 처리 보완 후 `uv run --offline pytest tests/hmr/test_appearance.py -q -p no:cacheprovider --basetemp=.pytest-tmp-89-final` → 18 passed.
- `uv run --offline lint-imports` → 6 kept, 0 broken. `git diff --check` 통과.
- 리뷰 생략(사용자 지시). 전체 pytest·실제 GPU/HMR·EC2/S3 실행은 하지 않았다. 계약 예시는 변경하지 않았다.

## 버린 대안과 남은 위험

- PyTorch3D/nvdiffrast: 지시서의 새 의존성 금지, CUDA 빌드/이미지 변경 비용 때문에 제외.
  UV 텍스처·마스크를 GLB에 삽입하는 방식도 별도 texture 계약·front 티켓과 중복되어 제외했다.
- 생성형 얼굴/옷 복원·Re-ID·Tier 0 값 교정은 도입하지 않는다. 첫 관측이 잘못되면 색 거절이 그 오류를
  고착시킬 수 있다. 조명/자동 노출·움직이는 옷·MHR 포즈 오차·얇은 면·UV 중첩은 실제 자료 확인이 필요하다.
- bbox만 있을 때 사람 경계 밖 배경이 메시 안에 투영될 수 있다. 타인 bbox 제외는 안전 쪽으로 관측을 줄이지만
  가려진 사람이 역으로 앞 사람의 관측까지 막을 수 있다. 정밀 인물 마스크 연결은 상류 제공 시 사용한다.
- 자동 얼굴 영역은 머리 전체여서 얼굴 비율이 정밀 얼굴 면적 비율은 아니다. 검증된 MHR 얼굴 번호로 좁혀야 한다.
- CPU 1024 누적은 LIVE 포즈 간격에 비해 느리다. CUDA는 계산 경로만 준비했고 이 노트북에서는 실행하지 않았다.
  PNG·네트워크 업로드도 포즈를 지연시킬 수 있으며 기존 500ms 검사로 오래된 포즈는 버린다.
  이미 시작한 CUDA 조각은 중간 취소할 수 없지만 다음 래스터 묶음/카메라/발행 단계에서 감시를 다시 확인한다.
- front 텍스처/출처 지도 렌더링은 별도 티켓이다. 실제 EC2/S3 업로드, 라이선스 MHR 얼굴 UV 방향,
  가린 시점의 3D 품질과 GPU 상한 통합은 9차 세션/Lead 확인으로 넘긴다.
