# ai/experiments/hmr

EXP-02(HMR, Fast SAM 3D Body) 실험 코드. 규칙은 `ai/CONVENTION.md`.

- `verify_mhr_gltf.py`, `mhr_pose_viewer.html`(S15P31A101-88, `docs/design/mhr-skinned-gltf.md`): `verify_mhr_gltf.py` 가 만든 .glb·포즈 시퀀스 JSON 을 브라우저로 연 `mhr_pose_viewer.html` 에서 파일로 골라 재생한다(같은 폴더를 `python -m http.server` 로 띄우면 `?glb=<파일>&poses=<파일>&t=<초>` 로 바로 열림). 산출물은 커밋하지 않는다(MHR 자산 SAM License).
- E-GATE-1(S15P31A101-87, `docs/design/hmr-multiview-aggregation.md`): `egate1_frames.py`(녹화 → 같은 시각 카메라별 그림, 노트북) → `run_multiview_hmr.py`(GPU, 카메라별 HMR 결과 묶음 `egate1_bundle.py` 형식,
  큐 항목은 `egate1_jobs.py`) → `egate1_eval.py`(노트북, 묶음 + Tier 0 world_tracks → 한 카메라 제외·실루엣·발-ground_anchor 지표, `pose_frames.jsonl`, Run 기록). 집계는 `simai_ai.hmr.multiview`.
