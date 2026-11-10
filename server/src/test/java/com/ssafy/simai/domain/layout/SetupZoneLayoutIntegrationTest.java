package com.ssafy.simai.domain.layout;

import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.assertThatThrownBy;
import static org.springframework.security.test.web.servlet.request.SecurityMockMvcRequestPostProcessors.jwt;
import static org.springframework.test.web.servlet.request.MockMvcRequestBuilders.put;
import static org.springframework.test.web.servlet.request.MockMvcRequestBuilders.post;
import static org.springframework.test.web.servlet.request.MockMvcRequestBuilders.get;
import static org.springframework.test.web.servlet.result.MockMvcResultMatchers.status;
import static org.springframework.test.web.servlet.result.MockMvcResultMatchers.jsonPath;

import java.util.UUID;
import java.util.concurrent.Executors;
import java.time.OffsetDateTime;
import org.junit.jupiter.api.AfterEach;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.Test;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.boot.test.context.SpringBootTest;
import org.springframework.boot.webmvc.test.autoconfigure.AutoConfigureMockMvc;
import org.springframework.jdbc.core.JdbcTemplate;
import org.springframework.http.MediaType;
import org.springframework.security.core.authority.SimpleGrantedAuthority;
import org.springframework.test.context.ActiveProfiles;
import org.springframework.test.web.servlet.MockMvc;
import org.springframework.transaction.support.TransactionTemplate;
import tools.jackson.databind.ObjectMapper;

/** 실제 PostgreSQL 잠금·외래키와 PostGIS 개구부 변환을 검증한다. */
@SpringBootTest
@AutoConfigureMockMvc
@ActiveProfiles("test")
class SetupZoneLayoutIntegrationTest {
    @Autowired JdbcTemplate jdbc;
    @Autowired SetupZoneLayoutService service;
    @Autowired MockMvc mvc;
    @Autowired ObjectMapper mapper;
    @Autowired LayoutZoneService layoutZones;
    @Autowired TransactionTemplate transaction;
    private final String suffix = UUID.randomUUID().toString().substring(0, 8);
    private final UUID session = UUID.randomUUID();
    private final String zone = "z-test-" + suffix;
    private final String job = "job-test-" + suffix;
    private final String cal = "cal-test-" + suffix;
    private final String bundle = "cfg-test-" + suffix;
    private String site;
    private long user;
    private long work;

    @BeforeEach
    void setUp() {
        site = jdbc.queryForObject("SELECT site_id FROM site LIMIT 1", String.class);
        user = jdbc.queryForObject("INSERT INTO app_user(login_id,password_hash,display_label,role,created_at) VALUES (?, 'hash','test','ADMIN',now()) RETURNING user_id", Long.class, "layout-test-" + suffix);
        work = jdbc.queryForObject("""
                INSERT INTO work_unit(site_id,work_date,name,starts_at,ends_at,planned_people,status,created_by,created_at,updated_at)
                VALUES (?,current_date,'test',now(),now()+interval '1 hour',1,'PLANNED',?,now(),now()) RETURNING work_id
                """, Long.class, site, user);
        jdbc.update("INSERT INTO analysis_job(job_id,job_type,priority,status,attempt,max_attempts,timeout_ms,input,created_at) VALUES (?, 'site_setup','BACKGROUND','SUCCEEDED',1,1,1000,'{}'::jsonb,now())", job);
        jdbc.update("""
                INSERT INTO setup_session(setup_session_id,site_id,work_unit_id,job_id,status,camera_heights_m,height_tolerance_cm,requested_by,started_at,updated_at)
                VALUES (?,?,?,?,'COMPLETED','{}'::jsonb,5,?,now(),now())
                """, session, site, work, job, user);
        jdbc.update("UPDATE work_unit SET active_setup_session_id = ? WHERE work_id = ?", session, work);
        jdbc.update("""
                INSERT INTO zone(zone_id,zone_version,site_id,name,zone_kind,eval_type,polygon,z_min_m,z_max_m,severity,valid_from)
                VALUES (?,1,?,'test','OPENING','VOLUME_3D',ST_GeomFromText('POLYGON((0 0,2 0,2 1,0 1,0 0))',0),0,2,'HIGH',now())
                """, zone, site);
        jdbc.update("INSERT INTO setup_session_zone(setup_session_id,zone_id,zone_version,created_at) VALUES (?,?,1,now())", session, zone);
        jdbc.update("""
                INSERT INTO calibration_version(calibration_version,site_id,state,provider_source,reason,effective_at,calibration_ref,calibration_hash,created_at)
                VALUES (?,?,'PROVISIONAL','G1_AUTO','site_setup',now(),'s3://test/cal.json',?,now())
                """, cal, site, "sha256:" + "b".repeat(64));
        jdbc.update("""
                INSERT INTO config_bundle(config_version,site_id,issued_at,issued_by,bundle_uri,bundle_hash,components,calibration_version,rule_profile)
                VALUES (?,?,now(),?,'https://test/cfg',?,'{}'::jsonb,?,'OPERATION')
                """, bundle, site, user, "sha256:" + "c".repeat(64), cal);
        jdbc.update("INSERT INTO config_bundle_item(config_version,item_type,item_id,item_version) VALUES (?,'ZONE',?,1)", bundle, zone);
    }

    @AfterEach
    void cleanUp() {
        Long layout = jdbc.queryForObject("SELECT layout_id FROM work_unit WHERE work_id = ?", Long.class, work);
        jdbc.update("UPDATE work_unit SET layout_id = NULL, active_setup_session_id = NULL WHERE work_id = ?", work);
        if (layout != null) {
            jdbc.update("DELETE FROM layout_plan_revision WHERE layout_id = ?", layout);
            jdbc.update("DELETE FROM layout_plan WHERE layout_id = ?", layout);
        }
        jdbc.update("DELETE FROM config_bundle_item WHERE config_version = ?", bundle);
        jdbc.update("DELETE FROM config_bundle WHERE config_version = ?", bundle);
        jdbc.update("DELETE FROM setup_session_zone WHERE setup_session_id = ?", session);
        jdbc.update("DELETE FROM setup_session WHERE setup_session_id = ?", session);
        jdbc.update("DELETE FROM analysis_job WHERE job_id = ?", job);
        jdbc.update("DELETE FROM zone WHERE zone_id = ?", zone);
        jdbc.update("DELETE FROM calibration_version WHERE calibration_version = ?", cal);
        jdbc.update("DELETE FROM work_unit WHERE work_id = ?", work);
        jdbc.update("DELETE FROM app_user WHERE user_id = ?", user);
    }

    @Test
    void 동시_반영과_재시도는_하나의_원본_참조만_만든다() throws Exception {
        try (var pool = Executors.newFixedThreadPool(2)) {
            var a = pool.submit(() -> service.add(work, zone, user));
            var b = pool.submit(() -> service.add(work, zone, user));
            assertThat(java.util.Set.of(a.get().path("result").asString(), b.get().path("result").asString()))
                    .containsExactlyInAnyOrder("LINKED", "ALREADY_LINKED");
        }
        assertThat(jdbc.queryForObject("SELECT jsonb_array_length(l.items) FROM layout_plan l JOIN work_unit w ON w.layout_id=l.layout_id WHERE w.work_id=?", Integer.class, work)).isEqualTo(1);
        assertThat(service.add(work, zone, user).path("result").asString()).isEqualTo("ALREADY_LINKED");
    }

    @Test
    void 발행되지_않은_구역과_다른_구역은_레이아웃을_생성하지_않는다() {
        jdbc.update("DELETE FROM config_bundle_item WHERE config_version = ?", bundle);
        assertThatThrownBy(() -> service.add(work, zone, user)).isInstanceOf(RuntimeException.class);
        assertThatThrownBy(() -> service.add(work, "other-zone", user)).isInstanceOf(RuntimeException.class);
        assertThat(jdbc.queryForObject("SELECT layout_id FROM work_unit WHERE work_id=?", Long.class, work)).isNull();
    }

    @Test
    void 열람자는_반영할_수_없다() throws Exception {
        mvc.perform(put("/api/work-units/{id}/layout/zones/{zone}", work, zone)
                .with(jwt().jwt(j -> j.subject(Long.toString(user))).authorities(new SimpleGrantedAuthority("ROLE_VIEWER"))))
                .andExpect(status().isForbidden());
    }

    @Test
    void 참조_개구부는_시작할_때_중복하지_않고_종료해도_원본을_보존한다() {
        service.add(work, zone, user);
        long layout = jdbc.queryForObject("SELECT layout_id FROM work_unit WHERE work_id=?", Long.class, work);
        transaction.executeWithoutResult(status -> {
            layoutZones.lock();
            layoutZones.sync(site, work, layout, user, OffsetDateTime.now());
            layoutZones.end(site, work, OffsetDateTime.now());
        });
        assertThat(jdbc.queryForObject("SELECT count(*) FROM zone WHERE zone_id=? AND valid_to IS NULL", Integer.class, zone)).isEqualTo(1);
        assertThat(jdbc.queryForObject("SELECT count(*) FROM zone WHERE work_id=? AND layout_item_id IS NOT NULL", Integer.class, work)).isZero();
    }

    @Test
    void 참조_작업면도_시작과_종료에서_중복하거나_삭제하지_않는다() {
        String object = "ws-test-" + suffix;
        jdbc.update("""
                INSERT INTO static_object(object_id,object_version,site_id,kind,obb,top_height_m,name,valid_from,zone_id)
                VALUES (?,1,?,'WORK_SURFACE','{"center":[1,1,0.5],"size":[1,1,1],"yaw_rad":0}'::jsonb,1,'원본',now(),?)
                """, object, site, zone);
        jdbc.update("INSERT INTO config_bundle_item(config_version,item_type,item_id,item_version) VALUES (?,'STATIC_OBJECT',?,1)", bundle, object);
        try {
            service.add(work, zone, user);
            long layout = jdbc.queryForObject("SELECT layout_id FROM work_unit WHERE work_id=?", Long.class, work);
            transaction.executeWithoutResult(status -> {
                layoutZones.lock();
                layoutZones.sync(site, work, layout, user, OffsetDateTime.now());
                layoutZones.end(site, work, OffsetDateTime.now());
            });
            assertThat(jdbc.queryForObject("SELECT count(*) FROM static_object WHERE object_id=? AND valid_to IS NULL", Integer.class, object)).isEqualTo(1);
            assertThat(jdbc.queryForObject("SELECT count(*) FROM static_object WHERE work_id=? AND layout_item_id IS NOT NULL", Integer.class, work)).isZero();
            assertThat(jdbc.queryForObject("SELECT count(*) FROM zone WHERE zone_id=? AND valid_to IS NULL", Integer.class, zone)).isEqualTo(1);
        } finally {
            jdbc.update("DELETE FROM config_bundle_item WHERE config_version=? AND item_id=?", bundle, object);
            jdbc.update("DELETE FROM static_object WHERE object_id=?", object);
        }
    }

    @Test
    void 작업면_연결이_끊기면_원본을_대체하지_않고_확인을_요구한다() {
        String object = "ws-test-" + suffix;
        jdbc.update("""
                INSERT INTO static_object(object_id,object_version,site_id,kind,obb,top_height_m,name,valid_from,zone_id)
                VALUES (?,1,?,'WORK_SURFACE','{"center":[1,1,0.5],"size":[1,1,1],"yaw_rad":0}'::jsonb,1,'원본',now(),?)
                """, object, site, zone);
        jdbc.update("INSERT INTO config_bundle_item(config_version,item_type,item_id,item_version) VALUES (?,'STATIC_OBJECT',?,1)", bundle, object);
        try {
            service.add(work, zone, user);
            long layout = jdbc.queryForObject("SELECT layout_id FROM work_unit WHERE work_id=?", Long.class, work);
            jdbc.update("UPDATE static_object SET valid_to=now() WHERE object_id=?", object);
            assertThatThrownBy(() -> transaction.executeWithoutResult(status -> {
                layoutZones.lock();
                layoutZones.sync(site, work, layout, user, OffsetDateTime.now());
            })).isInstanceOf(com.ssafy.simai.global.exception.BusinessException.class);
            assertThat(jdbc.queryForObject("SELECT count(*) FROM static_object WHERE work_id=? AND layout_item_id IS NOT NULL", Integer.class, work)).isZero();
            assertThat(jdbc.queryForObject("SELECT count(*) FROM zone WHERE zone_id=? AND valid_to IS NULL", Integer.class, zone)).isEqualTo(1);
        } finally {
            jdbc.update("DELETE FROM config_bundle_item WHERE config_version=? AND item_id=?", bundle, object);
            jdbc.update("DELETE FROM static_object WHERE object_id=?", object);
        }
    }

    @Test
    void 세팅에서_저장한_구역만_발행_후_반영한다() throws Exception {
        String body = """
                {"setup_session_id":"%s","publish":false,"zone":{"op":"CREATE","body":{
                  "name":"신규 구역","zone_kind":"OPENING","eval_type":"VOLUME_3D",
                  "polygon_xy":[[3,0],[4,0],[4,1],[3,1]],"z_min_m":0,"z_max_m":2,"severity":"HIGH"}}}
                """.formatted(session);
        var admin = jwt().jwt(j -> j.subject(Long.toString(user))).authorities(new SimpleGrantedAuthority("ROLE_ADMIN"));
        String response = mvc.perform(post("/api/zones/commit").with(admin).contentType(MediaType.APPLICATION_JSON).content(body))
                .andExpect(status().isOk()).andReturn().getResponse().getContentAsString();
        String created = mapper.readTree(response).path("zone_id").asString();
        try {
            assertThat(jdbc.queryForObject("SELECT count(*) FROM setup_session_zone WHERE setup_session_id = ? AND zone_id = ?", Integer.class, session, created)).isEqualTo(1);
            mvc.perform(put("/api/work-units/{id}/layout/zones/{zone}", work, created).with(admin))
                    .andExpect(status().isConflict());
            jdbc.update("INSERT INTO config_bundle_item(config_version,item_type,item_id,item_version) VALUES (?,'ZONE',?,1)", bundle, created);
            mvc.perform(put("/api/work-units/{id}/layout/zones/{zone}", work, created).with(admin))
                    .andExpect(status().isOk());
            mvc.perform(put("/api/work-units/{id}/layout/zones/{zone}", work, created).with(admin))
                    .andExpect(status().isOk()).andExpect(jsonPath("$.result").value("ALREADY_LINKED"));
            long layout = jdbc.queryForObject("SELECT layout_id FROM work_unit WHERE work_id=?", Long.class, work);
            mvc.perform(get("/api/work-units/{id}", work).with(admin))
                    .andExpect(status().isOk()).andExpect(jsonPath("$.layout_id").value(layout));
            mvc.perform(get("/api/layouts/{id}", layout).with(admin))
                    .andExpect(status().isOk()).andExpect(jsonPath("$.items.length()").value(1));
            assertThat(jdbc.queryForObject("SELECT jsonb_array_length(l.items) FROM layout_plan l JOIN work_unit w ON w.layout_id=l.layout_id WHERE w.work_id=?", Integer.class, work)).isEqualTo(1);
        } finally {
            jdbc.update("DELETE FROM config_bundle_item WHERE config_version = ? AND item_id = ?", bundle, created);
            jdbc.update("DELETE FROM setup_session_zone WHERE setup_session_id = ? AND zone_id = ?", session, created);
            jdbc.update("DELETE FROM zone WHERE zone_id = ?", created);
        }
    }

    @Test
    void 세팅_구역을_실제로_발행한_뒤_작업에서_조회하고_재시도한다() throws Exception {
        byte[] calibration = java.nio.file.Files.readAllBytes(java.nio.file.Path.of("../contracts/examples/calibration.json"));
        jdbc.update("INSERT INTO calibration_document(calibration_version,content,sha256,uploaded_by,uploaded_at) VALUES (?,?,?,?,now())",
                cal, calibration, "sha256:" + "b".repeat(64), user);
        String created = null;
        String generatedBundle = null;
        try {
            String body = """
                    {"setup_session_id":"%s","publish":true,"zone":{"op":"CREATE","body":{
                      "name":"실제 발행 구역","zone_kind":"OPENING","eval_type":"VOLUME_3D",
                      "polygon_xy":[[3,0],[4,0],[4,1],[3,1]],"z_min_m":0,"z_max_m":2,"severity":"HIGH"}}}
                    """.formatted(session);
            var admin = jwt().jwt(j -> j.subject(Long.toString(user))).authorities(new SimpleGrantedAuthority("ROLE_ADMIN"));
            var response = mapper.readTree(mvc.perform(post("/api/zones/commit").with(admin)
                    .contentType(MediaType.APPLICATION_JSON).content(body)).andExpect(status().isOk())
                    .andReturn().getResponse().getContentAsString());
            created = response.path("zone_id").asString();
            generatedBundle = response.path("bundle").path("config_version").asString();
            assertThat(generatedBundle).isNotBlank();
            mvc.perform(put("/api/work-units/{id}/layout/zones/{zone}", work, created).with(admin))
                    .andExpect(status().isOk()).andExpect(jsonPath("$.result").value("LINKED"));
            mvc.perform(put("/api/work-units/{id}/layout/zones/{zone}", work, created).with(admin))
                    .andExpect(status().isOk()).andExpect(jsonPath("$.result").value("ALREADY_LINKED"));
            long layout = jdbc.queryForObject("SELECT layout_id FROM work_unit WHERE work_id=?", Long.class, work);
            mvc.perform(get("/api/work-units/{id}", work).with(admin)).andExpect(status().isOk())
                    .andExpect(jsonPath("$.layout_id").value(layout));
            mvc.perform(get("/api/layouts/{id}", layout).with(admin)).andExpect(status().isOk())
                    .andExpect(jsonPath("$.items.length()").value(1));
        } finally {
            if (generatedBundle != null) {
                jdbc.update("DELETE FROM config_ack WHERE config_version=?", generatedBundle);
                jdbc.update("DELETE FROM config_bundle_file WHERE config_version=?", generatedBundle);
                jdbc.update("DELETE FROM config_bundle_item WHERE config_version=?", generatedBundle);
                jdbc.update("DELETE FROM config_bundle WHERE config_version=?", generatedBundle);
            }
            if (created != null) {
                jdbc.update("DELETE FROM setup_session_zone WHERE setup_session_id=? AND zone_id=?", session, created);
                jdbc.update("DELETE FROM zone WHERE zone_id=?", created);
            }
            jdbc.update("DELETE FROM calibration_document WHERE calibration_version=?", cal);
        }
    }
}
