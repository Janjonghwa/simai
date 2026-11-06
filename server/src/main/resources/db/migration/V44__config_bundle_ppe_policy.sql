-- simAI V44: 구성 묶음 파일에 안전모 요구 ppe_policy (S15P31A101-518, 명세 7.2 R9·8.10·12.3, docs/design/server-ppe-policy-publish.md)
-- config_bundle 1.3.0 의 선택 구성 요소 ppe_policy(계약 data/ppe_policy 1.0.0)를 다른 구성 요소처럼 바이트·해시로 저장한다.
-- 이전 묶음의 파일은 그대로 둔다(덮어쓰지 않는다).
ALTER TABLE config_bundle_file DROP CONSTRAINT config_bundle_file_name_check;
ALTER TABLE config_bundle_file ADD CONSTRAINT config_bundle_file_name_check
    CHECK (name IN ('manifest', 'zones', 'rules', 'calibration', 'operational_geometry', 'ppe_policy'));
