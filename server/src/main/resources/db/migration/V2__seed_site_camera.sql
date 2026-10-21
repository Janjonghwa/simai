-- simAI V2: 현장 1개와 카메라 4대 시드 (S15P31A101-211, 기획서 16장 "계정·기기·기준 데이터 생성")
-- MVP 는 공사장 1개다(16장 MVP 현장·계정 범위). 시연·개발 현장은 교실 테스트베드다(1장).
-- ID 는 계약 예시·Tier 0·게이트웨이가 이미 쓰는 값을 따른다: site_id = coordinate_frame.site_id 'classroom-1', camera_id 'cam1'~'cam4'.
-- 카메라 ID 에는 설치 위치를 넣지 않는다. 설치·재배치로 위치가 바뀌어도 ID 는 그대로이고, 위치는 보정(calibration_camera)이 정한다.
-- RTSP 주소·계정은 DB 에 넣지 않는다. 노트북 게이트웨이의 .env 에만 둔다(14장 카메라 RTSP 계정·비밀 값).
-- 해상도는 노트북이 보내는 codec_info(I0)로 채운다. 프레임률은 운영 고정값 25 fps(3.3).

INSERT INTO site (site_id, name, created_at)
VALUES ('classroom-1', '교실 테스트베드', now())
ON CONFLICT (site_id) DO NOTHING;

INSERT INTO camera (camera_id, site_id, model, nominal_fps)
VALUES ('cam1', 'classroom-1', 'Tapo C110', 25),
       ('cam2', 'classroom-1', 'Tapo C110', 25),
       ('cam3', 'classroom-1', 'Tapo C110', 25),
       ('cam4', 'classroom-1', 'Tapo C110', 25)
ON CONFLICT (camera_id) DO NOTHING;
