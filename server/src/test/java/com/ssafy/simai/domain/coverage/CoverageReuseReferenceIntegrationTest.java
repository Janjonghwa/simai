package com.ssafy.simai.domain.coverage;

import static org.assertj.core.api.Assertions.assertThat;
import static org.mockito.ArgumentMatchers.anyString;
import static org.mockito.Mockito.when;

import com.ssafy.simai.domain.scene.SceneCameraImageService;
import com.ssafy.simai.global.security.TokenHashes;
import java.util.UUID;
import org.junit.jupiter.api.AfterEach;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.Test;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.boot.test.context.SpringBootTest;
import org.springframework.jdbc.core.JdbcTemplate;
import org.springframework.test.context.ActiveProfiles;
import org.springframework.test.context.bean.override.mockito.MockitoBean;
import tools.jackson.databind.JsonNode;
import tools.jackson.databind.ObjectMapper;
import tools.jackson.databind.node.ObjectNode;

/**
 * 재사용 시점 확인 기준(S15P31A101-712): 현재 관제 맵의 카메라별 사진 서명 주소 + 그 보정의 렌즈 내부값을 기기 수요 응답에 싣는다.
 * 렌즈가 없는 카메라는 빼고, 사진이 없으면 싣지 않으며(null), 수요가 없으면 싣지 않는다.
 * S15P31A101-714: 수요 응답은 고를 수 있는 최근 완료 맵마다의 기준(reuse_references)을 싣고, 옛 헬퍼용 reuse_reference 는 그 첫 항목이다.
 */
@SpringBootTest
@ActiveProfiles("test")
class CoverageReuseReferenceIntegrationTest {

    @Autowired
    JdbcTemplate jdbc;

    @Autowired
    ObjectMapper mapper;

    @Autowired
    CoverageReuseReference reference;

    @Autowired
    CoverageGuideService coverageGuides;

    @MockitoBean
    SceneCameraImageService images;

    private final String suffix = UUID.randomUUID().toString().substring(0, 8);
    private final String calibration = "cal-view-ref-" + suffix;
    private final String scene = "scene-view-ref-" + suffix;
    private final String laptop = "gw-view-ref-" + suffix;
    private final UUID session = UUID.randomUUID();
    private final String job = "job-view-ref-" + suffix;
    private final String workName = "시점기준작업-" + suffix;
    private String site;

    @BeforeEach
    void setUp() {
        coverageGuides.clearForTest();
        site = jdbc.queryForObject("SELECT site_id FROM site LIMIT 1", String.class);
        jdbc.update("""
                INSERT INTO calibration_version (calibration_version, site_id, state, provider_source, reason, effective_at,
                    calibration_ref, calibration_hash, created_at)
                VALUES (?, ?, 'CALIBRATED', 'G1_AUTO', 'bootstrap', now(), 'file:///cal/view.json', ?, now())
                """, calibration, site, "sha256:" + "b".repeat(64));
        for (String cam : new String[] {"cam_A", "cam_B", "cam_C"}) {
            jdbc.update("""
                    INSERT INTO calibration_camera (calibration_version, camera_id, image_size, k_matrix, distortion_model,
                        distortion_coeffs, r_matrix, t_vector)
                    VALUES (?, ?, '{2304,1296}', '[[1200.0,0,1151.5],[0,1200.0,647.5],[0,0,1]]'::jsonb, 'OPENCV_PINHOLE',
                        '{-0.12,0.03,0,0,0}', '[[1,0,0],[0,1,0],[0,0,1]]'::jsonb, '{0,0,-2.44}')
                    """, calibration, cam);
        }
        jdbc.update("""
                INSERT INTO scene_version (scene_version, site_id, calibration_version, state, gltf_key, is_upper_bound,
                    committed_at, valid_from)
                VALUES (?, ?, ?, 'COMMITTED', ?, false, now(), now())
                """, scene, site, calibration, "scenes/" + scene + ".glb");
        jdbc.update("""
                INSERT INTO scene_activation (site_id, scene_version, previous_scene_version, activated_at, actor, reason)
                VALUES (?, ?, NULL, now(), 'test', '시점 확인 기준 시험')
                """, site, scene);
        jdbc.update("INSERT INTO device (device_id, kind, token_hash, issued_at) VALUES (?, 'LAPTOP', ?, now())",
                laptop, TokenHashes.sha256Hex("test-only-view-ref-" + suffix));
    }

    /** 이 맵을 만든 새 맵 세팅을 세팅 완료(C17)까지 마친 것으로 둔다(재사용 후보, S15P31A101-714) */
    private void completedMap() {
        jdbc.update("""
                INSERT INTO calibration_document (calibration_version, content, sha256, uploaded_by, uploaded_at)
                VALUES (?, ?, ?, NULL, now())
                """, calibration, "{}".getBytes(), "sha256:" + "d".repeat(64));
        long user = jdbc.queryForObject("""
                INSERT INTO app_user (login_id, password_hash, display_label, role, created_at)
                VALUES (?, 'hash', '시점기준관리자', 'ADMIN', now()) RETURNING user_id
                """, Long.class, "view-ref-admin-" + suffix);
        jdbc.update("INSERT INTO analysis_job(job_id,job_type,priority,status,attempt,max_attempts,timeout_ms,input,created_at)"
                + " VALUES (?, 'site_setup','BACKGROUND','SUCCEEDED',1,1,1000,'{}'::jsonb,now())", job);
        jdbc.update("""
                INSERT INTO work_unit (site_id, work_date, name, starts_at, ends_at, planned_people, status, created_by, created_at, updated_at)
                VALUES (?, current_date - 2, ?, now(), now() + interval '1 hour', 4, 'PLANNED', ?, now(), now())
                """, site, workName, user);
        long work = jdbc.queryForObject("SELECT work_id FROM work_unit WHERE name = ?", Long.class, workName);
        ObjectNode heights = mapper.createObjectNode();
        jdbc.queryForList("SELECT camera_id FROM camera WHERE site_id = ? AND removed_at IS NULL", String.class, site)
                .forEach(c -> heights.put(c, 2.44));
        jdbc.update("""
                INSERT INTO setup_session (setup_session_id, site_id, work_unit_id, job_id, status, progress_pct, camera_heights_m,
                    height_tolerance_cm, requested_by, started_at, completed_at, updated_at, calibration_version, scene_version)
                VALUES (?, ?, ?, ?, 'COMPLETED', 100, ?::jsonb, 5, ?, now(), now(), now(), ?, ?)
                """, session, site, work, job, heights.toString(), user, calibration, scene);
        jdbc.update("UPDATE work_unit SET active_setup_session_id = ?, setup_completed_at = now() WHERE work_id = ?", session, work);
    }

    @AfterEach
    void tearDown() {
        coverageGuides.clearForTest();
        jdbc.update("UPDATE work_unit SET active_setup_session_id = NULL WHERE name = ?", workName);
        jdbc.update("DELETE FROM setup_session WHERE setup_session_id = ?", session);
        jdbc.update("DELETE FROM work_unit WHERE name = ?", workName);
        jdbc.update("DELETE FROM app_user WHERE login_id = ?", "view-ref-admin-" + suffix);
        jdbc.update("DELETE FROM analysis_job WHERE job_id = ?", job);
        jdbc.update("DELETE FROM calibration_document WHERE calibration_version = ?", calibration);
        jdbc.update("DELETE FROM device WHERE device_id = ?", laptop);
        jdbc.update("DELETE FROM scene_activation WHERE scene_version = ?", scene);
        jdbc.update("DELETE FROM scene_version WHERE scene_version = ?", scene);
        jdbc.update("DELETE FROM calibration_camera WHERE calibration_version = ?", calibration);
        jdbc.update("DELETE FROM calibration_version WHERE calibration_version = ?", calibration);
    }

    @Test
    void 현재_관제_맵의_사진_주소와_렌즈를_싣고_렌즈가_없는_카메라는_뺀다() {
        when(images.images(anyString())).thenReturn(signed(true));
        ObjectNode ref = reference.current(site);
        assertThat(ref).isNotNull();
        assertThat(ref.get("scene_version").asString()).isEqualTo(scene);
        assertThat(ref.get("calibration_version").asString()).isEqualTo(calibration);
        assertThat(ref.get("expires_at_ms").asLong()).isEqualTo(1_793_690_600_000L);
        JsonNode cameras = ref.get("cameras");
        assertThat(cameras.size()).isEqualTo(3); // cam_D 는 렌즈가 없다
        JsonNode a = cameras.get(0);
        assertThat(a.get("camera_id").asString()).isEqualTo("cam_A");
        assertThat(a.get("url").asString()).isEqualTo("https://bucket.example/cam_A.jpg");
        assertThat(a.at("/image_size/0").asInt()).isEqualTo(2304);
        assertThat(a.at("/K/0/0").asDouble()).isEqualTo(1200.0);
        assertThat(a.at("/distortion/model").asString()).isEqualTo("OPENCV_PINHOLE");
        assertThat(a.at("/distortion/coeffs/0").asDouble()).isEqualTo(-0.12);
        assertThat(a.has("R")).isFalse();
    }

    @Test
    void 사진이_없으면_싣지_않고_수요가_있을_때만_기기_응답에_싣는다() {
        when(images.images(anyString())).thenReturn(signed(false));
        assertThat(reference.current(site)).isNull();

        when(images.images(anyString())).thenReturn(signed(true));
        completedMap();
        assertThat(coverageGuides.demandFor(laptop).get("reuse_reference").isNull()).isTrue();
        assertThat(coverageGuides.demandFor(laptop).get("reuse_references").size()).isZero();
        coverageGuides.queryForAdmin(); // 관리자 1단계 화면 조회 → 수요
        JsonNode demand = coverageGuides.demandFor(laptop);
        assertThat(demand.get("active_demand").asBoolean()).isTrue();
        assertThat(demand.at("/reuse_reference/scene_version").asString()).isEqualTo(scene);
        assertThat(demand.at("/reuse_references/0/scene_version").asString()).isEqualTo(scene);
        assertThat(demand.at("/reuse_references/0/cameras").size()).isEqualTo(3);
        assertThat(coverageGuides.referenceMissing(scene)).isFalse();

        // 고를 수 있는 맵인데 사진이 없으면 싣지 않고, 재사용 판정이 「사진 없음」으로 보이게 기억해 둔다
        when(images.images(anyString())).thenReturn(signed(false));
        JsonNode noPhotos = coverageGuides.demandFor(laptop);
        assertThat(noPhotos.get("reuse_references").size()).isZero();
        assertThat(noPhotos.get("reuse_reference").isNull()).isTrue();
        assertThat(coverageGuides.referenceMissing(scene)).isTrue();
    }

    private ObjectNode signed(boolean withImages) {
        ObjectNode node = mapper.createObjectNode().put("scene_version", scene);
        if (!withImages) {
            node.putNull("bundle_id").putNull("expires_at_ms").putArray("images");
            return node;
        }
        node.put("bundle_id", "bundle-" + "c".repeat(32)).put("source", "SITE_SETUP").put("expires_at_ms", 1_793_690_600_000L);
        var list = node.putArray("images");
        for (String cam : new String[] {"cam_A", "cam_B", "cam_C", "cam_D"}) {
            list.addObject().put("camera_id", cam).put("url", "https://bucket.example/" + cam + ".jpg");
        }
        return node;
    }
}
