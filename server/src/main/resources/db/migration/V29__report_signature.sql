ALTER TABLE upload_grant DROP CONSTRAINT upload_grant_purpose_check;
ALTER TABLE upload_grant ADD CONSTRAINT upload_grant_purpose_check CHECK (purpose IN
 ('EVENT_FRAME','EVENT_CLIP','TICKET_PHOTO','AVATAR_ASSET','VIDEO_SEGMENT','SCENE_GLTF','STATIC_RECONSTRUCT_INPUT','REPORT_SIGNATURE'));
ALTER TABLE report ADD COLUMN locked_at timestamptz, ADD COLUMN content_sha256 text;
CREATE TABLE report_signature (
 signature_id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
 report_id bigint NOT NULL REFERENCES report(report_id), report_version integer NOT NULL CHECK (report_version >= 1),
 signer_name text NOT NULL CHECK (char_length(signer_name) BETWEEN 1 AND 40),
 signature_image_key text NOT NULL UNIQUE, image_sha256 text NOT NULL, report_content_sha256 text NOT NULL,
 legal_notice_ack boolean NOT NULL CHECK (legal_notice_ack), recorded_by bigint NOT NULL REFERENCES app_user(user_id),
 signed_at timestamptz NOT NULL
);
CREATE INDEX report_signature_report_idx ON report_signature(report_id, signature_id);
