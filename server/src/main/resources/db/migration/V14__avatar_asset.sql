-- Worker upload grants (AVATAR_ASSET) and append-only avatar_asset versions (I6 / S15P31A101-325).

ALTER TABLE upload_grant
    ADD COLUMN requested_by_worker text REFERENCES worker (worker_id);

ALTER TABLE upload_grant
    DROP CONSTRAINT upload_grant_purpose_check;

ALTER TABLE upload_grant
    ADD CONSTRAINT upload_grant_purpose_check
        CHECK (purpose IN ('EVENT_FRAME', 'EVENT_CLIP', 'TICKET_PHOTO', 'AVATAR_ASSET'));

ALTER TABLE upload_grant
    DROP CONSTRAINT upload_grant_requester_check;

ALTER TABLE upload_grant
    ADD CONSTRAINT upload_grant_requester_check
        CHECK (requester IN ('TIER0', 'DEVICE', 'USER', 'WORKER'));

ALTER TABLE upload_grant
    DROP CONSTRAINT upload_grant_requester_ref_check;

ALTER TABLE upload_grant
    ADD CONSTRAINT upload_grant_requester_ref_check
        CHECK (
            (requester = 'DEVICE') = (requested_by_device IS NOT NULL)
            AND (requester = 'USER') = (requested_by_user IS NOT NULL)
            AND (requester = 'WORKER') = (requested_by_worker IS NOT NULL)
        );

CREATE TABLE avatar_asset (
    world_id                    text        NOT NULL,
    stream_epoch                bigint      NOT NULL CHECK (stream_epoch >= 0),
    appearance_version          integer     NOT NULL CHECK (appearance_version >= 0),
    msg_id                      uuid        NOT NULL,
    worker_id                   text        NOT NULL,
    identity_revision           integer     NOT NULL CHECK (identity_revision >= 0),
    previous_appearance_version integer     CHECK (previous_appearance_version IS NULL OR previous_appearance_version >= 0),
    calibration_version         text        NOT NULL,
    model_version               text        NOT NULL,
    mesh_format                 text        NOT NULL CHECK (mesh_format IN ('GLB_SKINNED', 'GLTF_SKINNED')),
    mesh_uri                    text        NOT NULL,
    mesh_sha256                 text        NOT NULL,
    mesh_size_bytes             bigint      NOT NULL CHECK (mesh_size_bytes > 0),
    vertex_count                integer     NOT NULL CHECK (vertex_count > 0),
    face_count                  integer     NOT NULL CHECK (face_count > 0),
    joint_count                 integer     NOT NULL CHECK (joint_count BETWEEN 1 AND 1024),
    joint_parents_sha256        text        NOT NULL,
    pelvis_joint_index          integer     NOT NULL CHECK (pelvis_joint_index >= 0 AND pelvis_joint_index < joint_count),
    rig_source                  text        NOT NULL,
    texture_uri                 text,
    texture_sha256              text,
    surface_mask_uri            text,
    surface_ratios              jsonb       NOT NULL,
    face_ratios                 jsonb       NOT NULL,
    face_policy                 text        NOT NULL CHECK (face_policy IN ('OBSERVED', 'BLURRED', 'EXCLUDED')),
    input_provenance            jsonb       NOT NULL,
    payload                     jsonb       NOT NULL,
    created_at                  timestamptz NOT NULL,
    sent_at                     timestamptz NOT NULL,
    received_at                 timestamptz NOT NULL,
    PRIMARY KEY (world_id, stream_epoch, appearance_version),
    CONSTRAINT avatar_asset_previous_version_check CHECK (
        (appearance_version = 0 AND previous_appearance_version IS NULL)
        OR (appearance_version > 0 AND previous_appearance_version IS NOT NULL AND previous_appearance_version < appearance_version)
    )
);

CREATE INDEX idx_avatar_asset_world_epoch ON avatar_asset (world_id, stream_epoch, appearance_version DESC);
