"""MapAnything 초기 추정 → World 좌표계 고정 (명세 4.1, 4.3 1·2단계, 4.4 PROVISIONAL, S15P31A101-102).

- `prepare`: AnyCalib 내부 파라미터로 교실 사진의 왜곡을 펴고(펴진 K 를 함께 계산) 사람 영역 마스크를 만든다. MapAnything
  known-K 실행(`ai/experiments/static_scene/run_mapanything.py --mode prepared`)의 입력 폴더(`simai.mapanything_inputs`)
- `inputs`: MapAnything 실행 결과(cameras.json, raw.npz)와 사람 마스크 → 카메라별 점(MapAnything world)
- `frame`: 바닥 평면(Z = 0)·중력, 경계 벽 두 방향으로 X·Y, 두 벽이 만나는 바닥 모서리로 원점, 우선순위로 스케일을 정한다.
  못 정하면 비우고 사유를 남긴다(바닥 없음 → Z = 0 을 지어내지 않음, 축 모호 → 실패)
- `measured_objects`: 실측 정물 목록(`calibration/data/measured_objects.json`)의 정물(노트북 윗판 등)을 펴진 그림에서 찾아
  MapAnything 점으로 재고, 같은 실행의 `fix --scale-evidence` 입력(MEASURED_OBJECT)을 만든다(S15P31A101-100)
- `cli`: `prepare`·`fix`·`measure-objects`. `fix` 의 결과 파일은 외부 파라미터 도구(-214)의 `--world-alignment` 입력
  (`simai.world_alignment`)이고 PROVISIONAL 보정 문서(계약 calibration)와 원점·축·스케일·재투영 근거를 함께 담는다

설계 근거는 docs/design/world-frame-from-mapanything.md, 실측 정물은 docs/design/measured-object-scale.md.
"""
