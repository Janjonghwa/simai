-- 현장 세팅 산출물 등록 근거(S15P31A101-467, docs/design/site-setup-pipeline.md 13절).
-- site_setup 워커가 하트비트·결과의 artifacts 로 알린 새 보정 문서·미리보기/정식 GLB 를 EC2 가 calibration_version·scene_version 으로
-- 등록하고, 그 버전이 어느 세팅 세션의 것인지 남긴다(추가만). 버전 계보 검사는 다른 세팅 세션에 귀속된 버전을 거부한다.
-- 미리보기·정식 Scene 은 같은 작업에서 둘이 나와 scene_version.job_id(작업당 하나, 부분 유일)로 묶을 수 없어 이 표를 쓴다.

CREATE TABLE setup_session_artifact (
    setup_session_id uuid        NOT NULL REFERENCES setup_session(setup_session_id) ON DELETE CASCADE,
    kind             text        NOT NULL CHECK (kind IN ('CALIBRATION', 'PREVIEW_SCENE', 'SCENE')),
    version          text        NOT NULL,
    job_id           text        NOT NULL,
    lease_id         uuid        NOT NULL,
    object_uri       text        NOT NULL,
    sha256           text        NOT NULL CHECK (sha256 ~ '^[0-9a-f]{64}$'),
    size_bytes       bigint      CHECK (size_bytes IS NULL OR size_bytes > 0),
    registered_at    timestamptz NOT NULL,
    PRIMARY KEY (setup_session_id, kind, version)
);

CREATE INDEX setup_session_artifact_version ON setup_session_artifact (version, kind);

COMMENT ON TABLE setup_session_artifact IS '세팅 세션이 등록한 보정·Scene 버전(객체 주소·sha256, 추가만). 다른 세션 귀속 버전은 계보 검사에서 거부';
COMMENT ON COLUMN setup_session_artifact.sha256 IS '워커가 알린 내용 SHA-256(16진수). 보정 문서는 EC2 가 내려받아 대조, GLB 는 저장소 체크섬과 대조';
