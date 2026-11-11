package com.ssafy.simai.domain.zone;

import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.assertThatThrownBy;
import static org.springframework.test.web.servlet.request.MockMvcRequestBuilders.get;
import static org.springframework.test.web.servlet.request.MockMvcRequestBuilders.put;
import static org.springframework.test.web.servlet.result.MockMvcResultMatchers.jsonPath;
import static org.springframework.test.web.servlet.result.MockMvcResultMatchers.status;

import com.ssafy.simai.global.config.JwtConfig;
import com.ssafy.simai.global.exception.BusinessException;
import com.ssafy.simai.global.security.AccessTokenIssuer;
import java.time.Instant;
import java.util.UUID;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.Test;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.boot.test.context.SpringBootTest;
import org.springframework.boot.webmvc.test.autoconfigure.AutoConfigureMockMvc;
import org.springframework.http.HttpHeaders;
import org.springframework.http.MediaType;
import org.springframework.jdbc.core.JdbcTemplate;
import org.springframework.security.oauth2.jose.jws.MacAlgorithm;
import org.springframework.security.oauth2.jwt.JwsHeader;
import org.springframework.security.oauth2.jwt.JwtClaimsSet;
import org.springframework.security.oauth2.jwt.JwtEncoder;
import org.springframework.security.oauth2.jwt.JwtEncoderParameters;
import org.springframework.test.context.ActiveProfiles;
import org.springframework.test.web.servlet.MockMvc;
import org.springframework.test.web.servlet.ResultActions;
import org.springframework.transaction.annotation.Transactional;
import tools.jackson.databind.JsonNode;
import tools.jackson.databind.ObjectMapper;
import tools.jackson.databind.node.ArrayNode;
import tools.jackson.databind.node.ObjectNode;

/** 세팅 중 구역 목록 저장과 세팅 완료 때 적용(S15P31A101-612) */
@SpringBootTest
@AutoConfigureMockMvc
@ActiveProfiles("test")
@Transactional
class SetupZonePlanIntegrationTest {

    @Autowired MockMvc mvc;
    @Autowired JdbcTemplate jdbc;
    @Autowired JwtEncoder encoder;
    @Autowired ObjectMapper mapper;
    @Autowired SetupZoneApplier applier;

    final String suffix = UUID.randomUUID().toString().substring(0, 8);
    final UUID session = UUID.randomUUID();
    String site;
    long user;
    long work;
    String admin;
    String viewer;

    @BeforeEach
    void setUp() {
        site = jdbc.queryForObject("SELECT site_id FROM site LIMIT 1", String.class);
        user = jdbc.queryForObject("INSERT INTO app_user(login_id,password_hash,role,created_at) VALUES (?, 'x', 'ADMIN', now()) RETURNING user_id",
                Long.class, "zones-" + suffix);
        admin = token(user, "ADMIN");
        viewer = token(jdbc.queryForObject("INSERT INTO app_user(login_id,password_hash,role,created_at) VALUES (?, 'x', 'VIEWER', now()) RETURNING user_id",
                Long.class, "zones-v-" + suffix), "VIEWER");
        work = jdbc.queryForObject("""
                INSERT INTO work_unit(site_id,work_date,name,starts_at,ends_at,planned_people,status,created_by,created_at,updated_at)
                VALUES (?,current_date,'test',now(),now()+interval '1 hour',1,'PLANNED',?,now(),now()) RETURNING work_id
                """, Long.class, site, user);
        String job = "job-zones-" + suffix;
        jdbc.update("INSERT INTO analysis_job(job_id,job_type,priority,status,attempt,max_attempts,timeout_ms,input,created_at) VALUES (?, 'site_setup','BACKGROUND','SUCCEEDED',1,1,1000,'{}'::jsonb,now())", job);
        jdbc.update("""
                INSERT INTO setup_session(setup_session_id,site_id,work_unit_id,job_id,status,camera_heights_m,height_tolerance_cm,requested_by,started_at,updated_at)
                VALUES (?,?,?,?,'COMPLETED','{}'::jsonb,5,?,now(),now())
                """, session, site, work, job, user);
        jdbc.update("UPDATE work_unit SET active_setup_session_id = ? WHERE work_id = ?", session, work);
        // 이 시험만 보도록 지금 관제 구역을 비운다(트랜잭션이라 되돌려진다)
        jdbc.update("UPDATE static_object SET valid_to = now() WHERE valid_to IS NULL AND site_id = ?", site);
        jdbc.update("UPDATE zone SET valid_to = now() WHERE valid_to IS NULL AND site_id = ?", site);
    }

    @Test
    void 목록은_통째로_저장하고_관제_구역에는_쓰지_않는다() throws Exception {
        String keep = zone("유지");
        int zonesBefore = jdbc.queryForObject("SELECT count(*) FROM zone", Integer.class);
        mvc.perform(get("/api/setup-sessions/{id}/zones", session).header(HttpHeaders.AUTHORIZATION, admin))
                .andExpect(status().isOk()).andExpect(jsonPath("$.revision").value(0)).andExpect(jsonPath("$.items.length()").value(0));

        ArrayNode items = mapper.createArrayNode();
        items.addObject().put("zone_id", keep).put("source", "AI").put("candidate_id", "cand-1").putNull("zone");
        ObjectNode added = items.addObject().put("source", "MANUAL");
        added.set("zone", zoneBody("새 구역"));
        added.set("surface", surfaceBody());
        JsonNode saved = body(putPlan(admin, items, 0).andExpect(status().isOk()));
        assertThat(saved.path("revision").asInt()).isEqualTo(1);
        assertThat(saved.path("items").size()).isEqualTo(2);
        assertThat(saved.path("items").get(1).path("zone").path("name").asString()).isEqualTo("새 구역");
        // 세팅 중에는 관제 구역이 바뀌지 않는다
        assertThat(jdbc.queryForObject("SELECT count(*) FROM zone", Integer.class)).isEqualTo(zonesBefore);

        putPlan(admin, items, 0).andExpect(status().isConflict()).andExpect(jsonPath("$.code").value("SETUP_ZONE_PLAN_CONFLICT"));
        putPlan(admin, items, 1).andExpect(status().isOk()).andExpect(jsonPath("$.revision").value(2));
        putPlan(viewer, items, 2).andExpect(status().isForbidden());
        mvc.perform(get("/api/setup-sessions/{id}/zones", UUID.randomUUID()).header(HttpHeaders.AUTHORIZATION, admin))
                .andExpect(status().isNotFound());
    }

    @Test
    void 모양이나_값이_틀린_목록과_닫힌_세팅은_거부한다() throws Exception {
        ArrayNode badSource = mapper.createArrayNode();
        badSource.addObject().put("source", "VLM").set("zone", zoneBody("a"));
        putPlan(admin, badSource, 0).andExpect(status().isBadRequest()).andExpect(jsonPath("$.code").value("INVALID_SETUP_ZONE_PLAN"));

        ArrayNode noBody = mapper.createArrayNode();
        noBody.addObject().put("source", "AI");
        putPlan(admin, noBody, 0).andExpect(status().isBadRequest());

        ArrayNode dup = mapper.createArrayNode();
        dup.addObject().put("zone_id", "z-a").put("source", "AI");
        dup.addObject().put("zone_id", "z-a").put("source", "AI");
        putPlan(admin, dup, 0).andExpect(status().isBadRequest());

        ArrayNode badPolygon = mapper.createArrayNode();
        ObjectNode z = zoneBody("꼬인 구역");
        ArrayNode poly = z.putArray("polygon_xy");
        poly.addArray().add(0.0).add(0.0);
        poly.addArray().add(1.0).add(1.0);
        poly.addArray().add(1.0).add(0.0);
        poly.addArray().add(0.0).add(1.0);
        badPolygon.addObject().put("source", "MANUAL").set("zone", z);
        putPlan(admin, badPolygon, 0).andExpect(status().isBadRequest());

        jdbc.update("UPDATE work_unit SET status='IN_PROGRESS', actual_started_at=now() WHERE work_id=?", work);
        putPlan(admin, mapper.createArrayNode(), 0).andExpect(status().isConflict())
                .andExpect(jsonPath("$.code").value("SETUP_ZONES_CLOSED"));
    }

    @Test
    void 완료_때_목록대로_유지_새_버전_생성_보관을_한_번에_적용한다() throws Exception {
        String keep = zone("유지");
        String change = zone("바꿈");
        String drop = zone("뺌");
        ArrayNode items = mapper.createArrayNode();
        items.addObject().put("zone_id", keep).put("source", "AI").putNull("zone");
        items.addObject().put("zone_id", change).put("source", "AI").set("zone", zoneBody("바꾼 이름"));
        ObjectNode added = items.addObject().put("source", "MANUAL");
        added.set("zone", zoneBody("새 구역"));
        added.set("surface", surfaceBody());
        putPlan(admin, items, 0).andExpect(status().isOk());

        SetupZoneApplier.ApplyResult result = applier.apply(session, true, user);
        assertThat(result.kept()).containsExactly(keep);
        assertThat(result.updated()).containsExactly(change);
        assertThat(result.archived()).containsExactly(drop);
        assertThat(result.created()).hasSize(1);
        String created = result.created().getFirst();

        assertThat(currentVersion(keep)).isEqualTo(1);
        assertThat(currentVersion(change)).isEqualTo(2);
        assertThat(jdbc.queryForObject("SELECT name FROM zone WHERE zone_id=? AND valid_to IS NULL", String.class, change)).isEqualTo("바꾼 이름");
        assertThat(jdbc.queryForObject("SELECT count(*) FROM zone WHERE zone_id=? AND valid_to IS NULL", Integer.class, drop)).isZero();
        assertThat(jdbc.queryForObject("SELECT count(*) FROM setup_session_zone WHERE setup_session_id=? AND zone_id=?", Integer.class, session, created)).isEqualTo(1);
        assertThat(jdbc.queryForObject("SELECT count(*) FROM static_object WHERE zone_id=? AND valid_to IS NULL", Integer.class, created)).isEqualTo(1);
    }

    @Test
    void World_가_바뀐_세팅에는_기존_구역을_잇지_않는다() throws Exception {
        String keep = zone("유지");
        ArrayNode items = mapper.createArrayNode();
        items.addObject().put("zone_id", keep).put("source", "AI");
        putPlan(admin, items, 0).andExpect(status().isOk());
        assertThatThrownBy(() -> applier.apply(session, false, user)).isInstanceOf(BusinessException.class)
                .hasMessage("SETUP_ZONE_WORLD_MISMATCH");

        jdbc.update("UPDATE zone SET valid_to = now() WHERE zone_id = ?", keep);
        assertThatThrownBy(() -> applier.apply(session, true, user)).isInstanceOf(BusinessException.class)
                .hasMessage("SETUP_ZONE_STALE");
    }

    @Test
    void 활성_세션이_아니면_목록을_고치거나_적용하지_않는다() throws Exception {
        jdbc.update("UPDATE work_unit SET active_setup_session_id = NULL WHERE work_id = ?", work);
        putPlan(admin, mapper.createArrayNode(), 0).andExpect(status().isConflict())
                .andExpect(jsonPath("$.code").value("SETUP_ZONES_CLOSED"));
        assertThatThrownBy(() -> applier.apply(session, true, user)).isInstanceOf(BusinessException.class)
                .hasMessage("SETUP_ZONES_CLOSED");
    }

    @Test
    void 레이아웃이_만든_구역은_목록에_있어도_그대로_둔다() throws Exception {
        String layoutZone = "z-plan-layout-" + suffix;
        jdbc.update("""
                INSERT INTO zone(zone_id,zone_version,site_id,name,zone_kind,eval_type,polygon,severity,valid_from,work_id,layout_item_id)
                VALUES (?,1,?,'레이아웃','OPENING','GROUND_FOOTPRINT',ST_GeomFromText('POLYGON((0 0,2 0,2 1,0 1,0 0))',0),'HIGH',now(),?,'item-1')
                """, layoutZone, site, work);
        ArrayNode items = mapper.createArrayNode();
        items.addObject().put("zone_id", layoutZone).put("source", "AI");
        putPlan(admin, items, 0).andExpect(status().isOk());
        SetupZoneApplier.ApplyResult result = applier.apply(session, true, user);
        assertThat(result.kept()).containsExactly(layoutZone);
        assertThat(result.archived()).isEmpty();
        assertThat(currentVersion(layoutZone)).isEqualTo(1);
    }

    private int currentVersion(String zoneId) {
        return jdbc.queryForObject("SELECT zone_version FROM zone WHERE zone_id=? AND valid_to IS NULL", Integer.class, zoneId);
    }

    private String zone(String name) {
        String id = "z-plan-" + Math.abs(name.hashCode()) + "-" + suffix;
        jdbc.update("""
                INSERT INTO zone(zone_id,zone_version,site_id,name,zone_kind,eval_type,polygon,severity,valid_from)
                VALUES (?,1,?,?,'OPENING','GROUND_FOOTPRINT',ST_GeomFromText('POLYGON((0 0,2 0,2 1,0 1,0 0))',0),'HIGH',now())
                """, id, site, name);
        return id;
    }

    private ObjectNode zoneBody(String name) {
        ObjectNode z = mapper.createObjectNode();
        z.put("name", name).put("zone_kind", "OPENING").put("eval_type", "GROUND_FOOTPRINT").put("severity", "CRITICAL");
        ArrayNode poly = z.putArray("polygon_xy");
        poly.addArray().add(0.0).add(0.0);
        poly.addArray().add(1.0).add(0.0);
        poly.addArray().add(1.0).add(1.0);
        poly.addArray().add(0.0).add(1.0);
        return z;
    }

    private ObjectNode surfaceBody() {
        ObjectNode s = mapper.createObjectNode();
        s.put("kind", "WORK_SURFACE").put("name", "발판").put("top_height_m", 1.2);
        ObjectNode obb = s.putObject("obb");
        obb.putArray("center").add(0.5).add(0.5).add(0.6);
        obb.putArray("size").add(1.0).add(1.0).add(1.2);
        obb.put("yaw_rad", 0.0);
        return s;
    }

    private ResultActions putPlan(String token, JsonNode items, int revision) throws Exception {
        ObjectNode b = mapper.createObjectNode();
        b.set("items", items);
        b.put("revision", revision);
        return mvc.perform(put("/api/setup-sessions/{id}/zones", session).header(HttpHeaders.AUTHORIZATION, token)
                .contentType(MediaType.APPLICATION_JSON).content(mapper.writeValueAsString(b)));
    }

    private String token(long id, String role) {
        Instant now = Instant.now();
        var claims = JwtClaimsSet.builder().issuer(JwtConfig.ISSUER).subject(String.valueOf(id)).issuedAt(now)
                .expiresAt(now.plusSeconds(300)).claim(AccessTokenIssuer.ROLE_CLAIM, role).build();
        return "Bearer " + encoder.encode(JwtEncoderParameters.from(JwsHeader.with(MacAlgorithm.HS256).build(), claims)).getTokenValue();
    }

    private JsonNode body(ResultActions actions) throws Exception {
        return mapper.readTree(actions.andReturn().getResponse().getContentAsString());
    }
}
