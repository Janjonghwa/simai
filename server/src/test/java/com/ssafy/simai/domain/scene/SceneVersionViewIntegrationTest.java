package com.ssafy.simai.domain.scene;

import static org.springframework.test.web.servlet.request.MockMvcRequestBuilders.get;
import static org.springframework.test.web.servlet.result.MockMvcResultMatchers.jsonPath;
import static org.springframework.test.web.servlet.result.MockMvcResultMatchers.status;

import com.ssafy.simai.global.config.JwtConfig;
import com.ssafy.simai.global.security.AccessTokenIssuer;
import java.time.Instant;
import java.util.UUID;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.Test;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.boot.test.context.SpringBootTest;
import org.springframework.boot.webmvc.test.autoconfigure.AutoConfigureMockMvc;
import org.springframework.http.HttpHeaders;
import org.springframework.jdbc.core.JdbcTemplate;
import org.springframework.security.oauth2.jose.jws.MacAlgorithm;
import org.springframework.security.oauth2.jwt.JwsHeader;
import org.springframework.security.oauth2.jwt.JwtClaimsSet;
import org.springframework.security.oauth2.jwt.JwtEncoder;
import org.springframework.security.oauth2.jwt.JwtEncoderParameters;
import org.springframework.test.context.ActiveProfiles;
import org.springframework.test.web.servlet.MockMvc;
import org.springframework.transaction.annotation.Transactional;

/** 버전을 지정한 Scene 보기: 볼 수 있는 상태·glb 조건과 권한(S15P31A101-613) */
@SpringBootTest
@AutoConfigureMockMvc
@ActiveProfiles("test")
@Transactional
class SceneVersionViewIntegrationTest {

    @Autowired MockMvc mvc;
    @Autowired JdbcTemplate jdbc;
    @Autowired JwtEncoder encoder;

    final String suffix = UUID.randomUUID().toString().substring(0, 8);
    String admin;
    String viewer;
    String site;
    String cal;

    @BeforeEach
    void setUp() {
        site = jdbc.queryForObject("SELECT site_id FROM site LIMIT 1", String.class);
        admin = token(user("scene-a-" + suffix, "ADMIN"), "ADMIN");
        viewer = token(user("scene-v-" + suffix, "VIEWER"), "VIEWER");
        cal = "cal-view-" + suffix;
        jdbc.update("""
                INSERT INTO calibration_version(calibration_version,site_id,state,provider_source,reason,effective_at,calibration_ref,calibration_hash,created_at)
                VALUES (?,?,'PROVISIONAL','G1_AUTO','site_setup',now(),'s3://test/cal.json',?,now())
                """, cal, site, "sha256:" + "b".repeat(64));
    }

    @Test
    void 미리보기_CANDIDATE_는_보이고_거절_glb_없음_형식_오류는_404_다() throws Exception {
        String preview = scene("preview", "CANDIDATE", "scenes/preview.glb");
        String rejected = scene("rejected", "REJECTED", "scenes/rejected.glb");
        String noGlb = scene("noglb", "CANDIDATE", null);

        mvc.perform(get("/api/scenes/{v}/view", preview).header(HttpHeaders.AUTHORIZATION, admin))
                .andExpect(status().isOk())
                .andExpect(jsonPath("$.scene_version").value(preview))
                .andExpect(jsonPath("$.state").value("CANDIDATE"))
                .andExpect(jsonPath("$.current").value(false))
                .andExpect(jsonPath("$.calibration_version").value(cal))
                .andExpect(jsonPath("$.compatibility").doesNotExist());
        for (String v : new String[] {rejected, noGlb, "no-such-scene", "Bad Version!"}) {
            mvc.perform(get("/api/scenes/{v}/view", v).header(HttpHeaders.AUTHORIZATION, admin))
                    .andExpect(status().isNotFound()).andExpect(jsonPath("$.code").value("SCENE_VERSION_NOT_FOUND"));
        }
        mvc.perform(get("/api/scenes/{v}/view", preview).header(HttpHeaders.AUTHORIZATION, viewer))
                .andExpect(status().isForbidden());
    }

    @Test
    void 세팅_맵처럼_긴_버전도_보인다() throws Exception {
        // 세팅 워커가 만드는 버전 모양(scene-job-<job uuid>-<uuid>, 83자). 63자 제한이던 때는 404 였다(-644).
        String v = "scene-job-df141aed-4f0d-4434-9704-c75140197c29-" + java.util.UUID.randomUUID();
        jdbc.update("""
                INSERT INTO scene_version (scene_version, site_id, calibration_version, state, gltf_key, is_upper_bound, valid_from)
                VALUES (?,?,?,?,?,false,now())
                """, v, site, cal, "CANDIDATE", "scenes/setup.glb");
        mvc.perform(get("/api/scenes/{v}/view", v).header(HttpHeaders.AUTHORIZATION, admin))
                .andExpect(status().isOk())
                .andExpect(jsonPath("$.scene_version").value(v));
        mvc.perform(get("/api/scenes/{v}/view", "s" + "x".repeat(160)).header(HttpHeaders.AUTHORIZATION, admin))
                .andExpect(status().isNotFound()).andExpect(jsonPath("$.code").value("SCENE_VERSION_NOT_FOUND"));
    }

    private String scene(String name, String state, String gltf) {
        String v = "scene-view-" + name + "-" + suffix;
        jdbc.update("""
                INSERT INTO scene_version (scene_version, site_id, calibration_version, state, gltf_key, is_upper_bound, valid_from)
                VALUES (?,?,?,?,?,false,now())
                """, v, site, cal, state, gltf);
        return v;
    }

    private long user(String login, String role) {
        return jdbc.queryForObject("INSERT INTO app_user(login_id,password_hash,role,created_at) VALUES (?, 'x', ?, now()) RETURNING user_id",
                Long.class, login, role);
    }

    private String token(long id, String role) {
        Instant now = Instant.now();
        var claims = JwtClaimsSet.builder().issuer(JwtConfig.ISSUER).subject(String.valueOf(id)).issuedAt(now)
                .expiresAt(now.plusSeconds(300)).claim(AccessTokenIssuer.ROLE_CLAIM, role).build();
        return "Bearer " + encoder.encode(JwtEncoderParameters.from(JwsHeader.with(MacAlgorithm.HS256).build(), claims)).getTokenValue();
    }
}
