ALTER TABLE zone ADD COLUMN work_id bigint REFERENCES work_unit(work_id), ADD COLUMN layout_id bigint REFERENCES layout_plan(layout_id), ADD COLUMN layout_item_id text;
ALTER TABLE static_object ADD COLUMN work_id bigint REFERENCES work_unit(work_id), ADD COLUMN layout_id bigint REFERENCES layout_plan(layout_id), ADD COLUMN layout_item_id text;
ALTER TABLE zone ADD COLUMN layout_signature jsonb;
ALTER TABLE static_object ADD COLUMN layout_signature jsonb;
CREATE UNIQUE INDEX zone_layout_item_current_idx ON zone (work_id, layout_item_id) WHERE valid_to IS NULL AND layout_item_id IS NOT NULL;
CREATE UNIQUE INDEX static_object_layout_item_current_idx ON static_object (work_id, layout_item_id) WHERE valid_to IS NULL AND layout_item_id IS NOT NULL;
