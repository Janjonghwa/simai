package com.ssafy.simai.domain.site;

import static org.assertj.core.api.Assertions.assertThat;

import java.nio.charset.StandardCharsets;
import java.sql.Connection;
import java.time.Instant;
import java.util.List;
import org.junit.jupiter.api.Test;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.boot.test.context.SpringBootTest;
import org.springframework.core.io.ClassPathResource;
import org.springframework.jdbc.core.JdbcTemplate;
import org.springframework.jdbc.datasource.DataSourceUtils;
import org.springframework.jdbc.datasource.init.ScriptUtils;
import org.springframework.test.context.ActiveProfiles;
import org.springframework.transaction.annotation.Transactional;

/**
 * S15P31A101-673: V69 시드 카메라 ID 통일(cam1~cam4 → cam_A~cam_D) 마이그레이션 검증.
 * 1) 새 DB 에서 Flyway 적용 뒤 classroom-1 시드 카메라가 cam_A~cam_D 인지 본다.
 * 2) 운영 DB 상태(lobby / cam_A~cam_D, cam1 없음)에서 V69 를 실행해도 아무것도 바뀌지 않는지 본다.
 * 3) cam1~cam4 를 참조하는 기록이 있으면 그 카메라는 바꾸지 않는지 본다.
 */
@SpringBootTest
@ActiveProfiles("test")
class CameraSeedMigrationIntegrationTest {

    private static final ClassPathResource V69_SCRIPT =
            new ClassPathResource("db/migration/V69__seed_camera_id_cam_a.sql");

    @Autowired
    JdbcTemplate jdbc;

    @Test
    void 새_DB_시드_카메라는_cam_A부터_cam_D이고_모든_camera_id_참조_표를_검사한다() throws Exception {
        List<String> cameras = jdbc.queryForList(
                "SELECT camera_id FROM camera WHERE site_id = 'classroom-1' ORDER BY camera_id",
                String.class);
        assertThat(cameras).containsExactly("cam_A", "cam_B", "cam_C", "cam_D");

        Integer legacyCount = jdbc.queryForObject(
                "SELECT count(*) FROM camera WHERE camera_id IN ('cam1', 'cam2', 'cam3', 'cam4')",
                Integer.class);
        assertThat(legacyCount).isZero();

        String v69Sql = V69_SCRIPT.getContentAsString(StandardCharsets.UTF_8);
        List<String> fkTables = jdbc.queryForList("""
                SELECT DISTINCT tc.table_name
                FROM information_schema.table_constraints tc
                JOIN information_schema.constraint_column_usage ccu
                  ON tc.constraint_name = ccu.constraint_name
                 AND tc.table_schema = ccu.table_schema
                WHERE tc.constraint_type = 'FOREIGN KEY'
                  AND tc.table_schema = 'public'
                  AND ccu.table_name = 'camera'
                """, String.class);
        assertThat(fkTables).isNotEmpty();
        for (String table : fkTables) {
            assertThat(v69Sql).as("FK 참조 표 %s 가 V69 조건에 있어야 함", table).contains(table);
        }

        List<String> cameraIdTables = jdbc.queryForList("""
                SELECT DISTINCT c.table_name
                FROM information_schema.columns c
                JOIN information_schema.tables t
                  ON c.table_schema = t.table_schema AND c.table_name = t.table_name
                WHERE c.table_schema = 'public'
                  AND t.table_type = 'BASE TABLE'
                  AND c.column_name = 'camera_id'
                  AND c.table_name <> 'camera'
                  AND c.table_name NOT LIKE '_timescaledb%'
                """, String.class);
        for (String table : cameraIdTables) {
            assertThat(v69Sql).as("camera_id 열이 있는 표 %s 가 V69 조건에 있어야 함", table).contains(table);
        }
    }

    @Test
    @Transactional
    void 운영_DB처럼_lobby에_cam_A부터_cam_D가_있고_cam1이_없으면_아무것도_바뀌지_않는다() throws Exception {
        clearCameraReferences();
        jdbc.update("DELETE FROM camera");
        jdbc.update("INSERT INTO site (site_id, name, created_at) VALUES ('lobby', '로비', now()) ON CONFLICT (site_id) DO NOTHING");
        jdbc.update("""
                INSERT INTO camera (camera_id, site_id, model, nominal_fps)
                VALUES ('cam_A', 'lobby', 'Tapo C110', 25),
                       ('cam_B', 'lobby', 'Tapo C110', 25),
                       ('cam_C', 'lobby', 'Tapo C110', 25),
                       ('cam_D', 'lobby', 'Tapo C110', 25)
                """);

        var before = jdbc.queryForList("SELECT camera_id, site_id, model, nominal_fps FROM camera ORDER BY camera_id");
        runV69();
        var after = jdbc.queryForList("SELECT camera_id, site_id, model, nominal_fps FROM camera ORDER BY camera_id");

        assertThat(after).isEqualTo(before);
    }

    @Test
    @Transactional
    void 참조_기록이_있는_시드_카메라는_바꾸지_않는다() throws Exception {
        clearCameraReferences();
        jdbc.update("DELETE FROM camera WHERE site_id = 'classroom-1'");
        jdbc.update("""
                INSERT INTO camera (camera_id, site_id, model, nominal_fps)
                VALUES ('cam1', 'classroom-1', 'Tapo C110', 25),
                       ('cam2', 'classroom-1', 'Tapo C110', 25),
                       ('cam3', 'classroom-1', 'Tapo C110', 25),
                       ('cam4', 'classroom-1', 'Tapo C110', 25)
                """);

        // cam1 은 FK 표(coverage_guide), cam2 는 일반 camera_id 표(system_metric)에서 참조 중
        jdbc.update("""
                INSERT INTO coverage_guide (calibration_version, camera_id, message, metrics, created_at)
                VALUES (NULL, 'cam1', '방향 점검', '{}'::jsonb, now())
                """);
        jdbc.update("""
                INSERT INTO system_metric (measured_at, source_id, metric, camera_id, value)
                VALUES (?, 'gw-test', 'fps', 'cam2', 25.0)
                """, java.sql.Timestamp.from(Instant.now()));

        runV69();

        List<String> cameras = jdbc.queryForList(
                "SELECT camera_id FROM camera WHERE site_id = 'classroom-1' ORDER BY camera_id",
                String.class);
        assertThat(cameras).containsExactly("cam1", "cam2", "cam_C", "cam_D");
    }

    private void clearCameraReferences() {
        jdbc.update("DELETE FROM coverage_guide");
        jdbc.update("DELETE FROM calibration_camera");
        jdbc.update("DELETE FROM media_clip");
        jdbc.update("DELETE FROM video_segment");
        jdbc.update("DELETE FROM system_metric");
        jdbc.update("DELETE FROM setup_session_artifact");
        jdbc.update("DELETE FROM setup_zone_opinion");
    }

    private void runV69() throws Exception {
        Connection conn = DataSourceUtils.getConnection(jdbc.getDataSource());
        try {
            ScriptUtils.executeSqlScript(conn, V69_SCRIPT);
        } finally {
            DataSourceUtils.releaseConnection(conn, jdbc.getDataSource());
        }
    }
}
