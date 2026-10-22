# 모델 가중치 — manifest

`deploy/weights.json`(simai-bootstrap 가중치 목록, S15P31A101-205)의 출처·라이선스·해시 근거다.
가중치 파일은 저장소에 넣지 않는다. 이 문서에는 출처·라이선스·경로·해시와 해시를 얻은 기록만 둔다(AGENTS.md 저장소 규칙, 명세 3.12).
목록을 바꿀 때는 덮어쓰지 않고 새 항목(새 이름·경로)을 더하고, 이 문서에 획득 기록을 함께 남긴다.

## yolo26x-pose (Tier 0 2D 검출)

| 항목 | 값 |
|---|---|
| 저장 경로 | `$SIMAI_ROOT/weights/yolo/yolo26x-pose.pt` |
| 원본 | https://github.com/ultralytics/assets/releases/download/v8.4.0/yolo26x-pose.pt |
| sha256 | `08ed9e01d22a6f248b04f2f9992016aca9a32250b9ab57057d886a09d026700d` |
| 라이선스 | AGPL-3.0 (Ultralytics) |
| 쓰는 곳 | Tier 0 이미지(`deploy/tier0`), TensorRT 엔진 캐시 키(가중치 해시 앞 12자) |

획득 기록:

- 2026-10-16 E-GATE-0 때 일본 Network Volume 에서 `ai/experiments/hmr/setup_runpod.sh weights` 가
  `ultralytics.utils.downloads.attempt_download_asset('yolo26x-pose.pt')` 로 받았다. ultralytics 8.4.x 의 이 함수는 저장소
  `ultralytics/assets` 의 릴리스 `v8.4.0`(함수 기본 인자, 노트북 설치본 8.4.95·8.4.178 에서 확인)에서 받으므로 원본은 위 URL 이다.
- 같은 스크립트가 받은 직후 `sha256sum` 으로 `weights/hmr_weights.sha256` 을 만들었고, 그 사본이 노트북
  `ai/experiments/hmr/runs/weights/hmr_weights.sha256`(커밋하지 않는 `runs/`, 파일 sha256 `34d136f28c3d973d…`)에 있다. 해당 줄:

  ```
  08ed9e01d22a6f248b04f2f9992016aca9a32250b9ab57057d886a09d026700d  /workspace/simai/weights/yolo/yolo26x-pose.pt
  ```

- 원본 URL 에서 다시 받아 해시를 대조하지는 않았다(대용량 다운로드를 하지 않는 노트북 작업). 첫 GPU 세션에서 `simai-bootstrap` 이
  원본에서 받아 이 값과 대조한다. 다르면 시작하지 않으므로(종료 코드 2) 그때 이 문서와 목록을 새 항목으로 고친다.

## 실험 이미지 가중치 (S15P31A101-207)

HMR 이미지(`deploy/exp02`, 묶음 `exp02`)와 MapAnything 이미지(`deploy/mapanything`, 묶음 `static`)가 시작할 때 `simai-bootstrap` 으로 받는다.
HF 원본은 모두 **커밋(revision) 고정**이다(`main` 이 바뀌어도 같은 파일). 2026-10-21 에 HF API(`/api/models/<저장소>`)로 본 각 저장소의 마지막 수정이
E-GATE-0(2026-10-16) 전이어서, E-GATE-0 때 받은 파일과 같은 revision 이다.

| 이름(`weights.json`) | 묶음 | 저장 경로(`$SIMAI_ROOT/`) | 원본(HF 저장소 @revision 앞 7자) | sha256 | 라이선스 | 해시 근거 |
|---|---|---|---|---|---|---|
| yolo26x-pose | tier0·exp02 | `weights/yolo/yolo26x-pose.pt` | 위 절 | 위 절 | AGPL-3.0 | 위 절 |
| sam-3d-body-dinov3 | exp02 | `weights/sam-3d-body-dinov3/model.ckpt` | `facebook/sam-3d-body-dinov3` @11aaa34 (승인제) | `b5a2f9d305dd02626b967aa2e86021fba07065df66ce7a7e00ffb9664f150abf` | SAM License (Meta) | E-GATE-0 `hmr_weights.sha256`(아래) |
| sam-3d-body-dinov3-mhr | exp02 | `weights/sam-3d-body-dinov3/assets/mhr_model.pt` | 같은 저장소 `assets/mhr_model.pt` (696,110,248 B) | `352e271a6c42729c68554ceaea0c955e866970160c31e35506d782dc0f7377bc` | SAM License (Meta) | HF API LFS oid(승인 계정, 2026-10-21, 아래) |
| moge-2-vits-normal | exp02 | `weights/moge/moge-2-vits-normal/model.pt` | `Ruicheng/moge-2-vits-normal` @26b477f | `79a16621928c2bf0ed04659218c55c01075e950507f40bb3332fb4c873d3e1dc` | MIT | HF API LFS oid |
| moge-2-vitl-normal | exp02 | `weights/moge/moge-2-vitl-normal/model.pt` | `Ruicheng/moge-2-vitl-normal` @cb0e8bb | `280741fd09bc3f403ccff9967784c2a391b52d2c0742ae3efdb21d9f90cc1a01` | MIT | HF API LFS oid |
| map-anything-apache-model | static | `weights/map-anything-apache/model.safetensors` | `facebook/map-anything-apache` @00f9c24 | `fa06c0fdccefc5048e072c85935d5789b1e36b307f3859033c17f9dcb9fd5201` | Apache-2.0 | HF API LFS oid = E-GATE-0 C2 `weights_hash` |
| map-anything-apache-config | static | `weights/map-anything-apache/config.json` | 같은 저장소 | `65701d09d99ed37a21d295f0d138978b3d584ab3bccdbcb4a2853da212b676c5` | Apache-2.0 | 고정 revision 에서 받아 sha256 (5,776 B) |
| map-anything-model | static | `weights/map-anything/model.safetensors` | `facebook/map-anything` @a1d87e9 | `981f060c64664dff3272b5f5a823d350abe71a2f144444db4cfc325f3ed5a3a0` | CC-BY-NC-4.0 (비상업, 연구 비교용) | HF API LFS oid = E-GATE-0 C1 `weights_hash` |
| map-anything-config | static | `weights/map-anything/config.json` | 같은 저장소 | `65701d09…676c5` (apache 와 같은 파일) | CC-BY-NC-4.0 | 고정 revision 에서 받아 sha256 |

획득 기록:

- **sam-3d-body-dinov3 `model.ckpt`**: 위 yolo 절과 같은 E-GATE-0 해시 파일(노트북 사본 `ai/experiments/hmr/runs/weights/hmr_weights.sha256`)의 줄
  `b5a2f9d3…0abf  /workspace/simai/weights/sam-3d-body-dinov3/model.ckpt`. 그때 `snapshot_download("facebook/sam-3d-body-dinov3")` 가 받은 `main` 이고, 저장소 마지막 수정은 2025-12-10 이라 지금 revision 11aaa34 와 같다.
- **`assets/mhr_model.pt`**: E-GATE-0 해시 파일은 폴더 최상위 `*.ckpt`·`*.pt` 만 해시해 이 파일이 없다. 저장소가 승인제라 토큰 없는 HF API·HEAD 요청은 401 이고 LFS 해시를 주지 않는다.
  2026-10-21 승인 계정 토큰으로 아래 명령을 돌려 채웠다(크기 696,110,248 B 일치). 같은 응답의 `model.ckpt` oid 는 위 E-GATE-0 값 `b5a2f9d3…0abf` 와 같았다.
  이미지 `jonghwa1492/tier0:*-3926a8772649` 안의 `/opt/simai/deploy/weights.json` 은 이 값을 채우기 전 판이라 비어 있다. 그 이미지로 exp02 를 돌릴 때는
  저장소 사본의 목록을 `SIMAI_WEIGHTS_MANIFEST=<사본>/deploy/weights.json` 로 넘긴다(다음 이미지 빌드부터는 이미지 안 목록에 들어 있다). 명령(토큰 값은 출력하지 않는다):

  ```sh
  curl -s -H "Authorization: Bearer $HF_TOKEN"     "https://huggingface.co/api/models/facebook/sam-3d-body-dinov3/tree/11aaa346c7204874a1cbafe3d39a979080b2c55a/assets"     | python -c "import sys,json; [print(x['path'], x['size'], x['lfs']['oid']) for x in json.load(sys.stdin) if x.get('lfs')]"
  ```

  나온 oid(= 파일 sha256)를 `weights.json` 의 `sam-3d-body-dinov3-mhr.sha256` 에 넣고 이 표의 근거 칸을 "HF API LFS oid(승인 계정, 날짜)"로 고친다.
  `model.ckpt` 의 oid 가 위 E-GATE-0 값과 같은지도 같은 명령(`tree/<revision>`)으로 대조한다.
- **MoGe2·map-anything `model.*`**: 공개 저장소라 HF API `tree/main?recursive=true` 의 `lfs.oid`(LFS 객체 sha256)를 그대로 썼다(2026-10-21). map-anything 두 파일은
  E-GATE-0 Run 기록(`ai/experiments/static_scene/runs/C1_room4_bf16/metrics.json`·`C2_room4_apache_bf16/metrics.json` 의 `weights_hash`, 커밋하지 않는 `runs/`)과 같다.
  MoGe2 는 E-GATE-0 때 Fast 코드가 HF 캐시로 받았던 것이라 해시 기록이 없다. 이미지에서는 `fs3db_common.use_local_moge()` 가 이 확인된 파일을 쓴다.
  S15P31A101-238 부터 HMR 은 알려진 K(보정·데이터셋)를 먼저 쓰지만 MoGe2 두 항목은 묶음에 남긴다: K 를 모르는 입력(E-GATE-0 재현 14장, 라이브 서버)의 예비 경로다(`docs/experiments/EXP-02_design.md` "K 입력").
- **map-anything `config.json`**: LFS 가 아니어서 고정 revision 의 `resolve/<revision>/config.json` 을 받아(5,776 B) sha256 을 냈다. 두 저장소의 config 가 같은 파일이다.
- 큰 파일은 노트북에서 받지 않았다. 첫 실험 이미지 세션에서 `simai-bootstrap` 이 받아 대조한다. 다르면 시작하지 않으므로(종료 코드 2) 그때 이 문서와 목록을 새 항목으로 고친다.
- 이미지에 넣는 코드 사본(가중치 아님): Fast-SAM-3D-Body @d72aa36(MIT), DINOv3 허브 코드 @6876159(DINOv3 License, 2026-10-16 이전 마지막 커밋),
  map-anything @3d10cf7(Apache-2.0), DINOv2 허브 코드 @7764ea0(Apache-2.0, 같은 기준). 두 허브 코드는 `torch.hub` 가 GitHub `main` 을 그때그때 받던 것을
  커밋으로 고정한 것이다(`pretrained=False` 경로: 코드만 쓰고 가중치는 위 목록에서 읽는다).
