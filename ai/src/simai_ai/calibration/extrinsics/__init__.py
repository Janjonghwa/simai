"""녹화 세션 하나로 카메라 외부 파라미터 보정을 끝까지 돌리는 단계 (명세 4.3, FR-23, S15P31A101-214).

- `extract`: 녹화 세션 → 카메라별 YOLO pose keypoint 시퀀스 파일(영상은 쓰지 않는다). io·detection extras
- `keypoints`: keypoint 시퀀스 파일 형식, 한 명 걷기 대응·공통 시각 정렬 → 번들 조정 관측(PersonObservation)
- `initial`: MapAnything cameras.json → World 기준 초기 외부 파라미터(-102 정렬 또는 사람 세로축 임시 정렬)
- `cli`: `extract`·`adjust` 실행 도구. 번들 조정·판정은 `calibration.bundle`·`criteria` 그대로 쓴다

설계 근거는 docs/design/extrinsics-from-recording.md.
"""
