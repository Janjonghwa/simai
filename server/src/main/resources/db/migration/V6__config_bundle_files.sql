-- simAI V6: 구성 묶음 파일·보정 문서 원문 (S15P31A101-120·-58, 기획서 3.2 구성 묶음, 11.1)
-- Tier 0 는 구성 묶음의 매니페스트와 구성 요소(구역·규칙·보정·작업면)를 EC2 에서 https 로 내려받아 해시를 검사한다.
-- 그래서 EC2 가 발행한 파일 바이트를 그대로 둔다(같은 주소는 늘 같은 바이트). 묶음과 같이 덮어쓰지 않는다.
-- 보정 문서는 Tier 0 가 만들지만 EC2 로 보내는 길이 아직 없어 관리자가 올린다(POST /api/calibrations).

CREATE TABLE calibration_document (
    calibration_version text NOT NULL REFERENCES calibration_version (calibration_version),
    content bytea NOT NULL,
    sha256 text NOT NULL,
    uploaded_by bigint REFERENCES app_user (user_id),
    uploaded_at timestamptz NOT NULL,
    PRIMARY KEY (calibration_version)
);

COMMENT ON TABLE calibration_document IS '보정 문서 원문(contracts/providers/calibration). 구성 묶음 calibration 구성 요소로 그대로 내려준다';

CREATE TABLE config_bundle_file (
    config_version text NOT NULL REFERENCES config_bundle (config_version),
    name text NOT NULL CHECK (name IN ('manifest', 'zones', 'rules', 'calibration', 'operational_geometry')),
    content bytea NOT NULL,
    sha256 text NOT NULL,
    PRIMARY KEY (config_version, name)
);

COMMENT ON TABLE config_bundle_file IS '구성 묶음이 가리키는 파일 바이트(매니페스트·구성 요소). Tier 0 가 서명 주소로 내려받는다';

-- 묶음에 넣은 규칙 묶음(운영·교실 시험 프로필, 기획서 7.4). 다음 발행의 기본값이다
ALTER TABLE config_bundle ADD COLUMN rule_profile text CHECK (rule_profile IN ('OPERATION', 'CLASSROOM_TEST'));
