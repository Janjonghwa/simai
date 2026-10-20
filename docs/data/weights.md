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

## 자리만 (실험 이미지 티켓)

| 이름 | 원본 | 상태 |
|---|---|---|
| sam-3d-body-dinov3 | HF `facebook/sam-3d-body-dinov3`(승인제) | 같은 E-GATE-0 해시 파일에 `model.ckpt` 해시가 있다. HMR 이미지 티켓에서 라이선스 원문과 함께 목록에 올린다 |
| map-anything | 미정 | MapAnything 이미지 티켓에서 정한다 |
