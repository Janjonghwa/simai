package com.ssafy.simai.domain.zone;

import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.assertThatThrownBy;
import static org.springframework.test.web.servlet.request.MockMvcRequestBuilders.get;
import static org.springframework.test.web.servlet.request.MockMvcRequestBuilders.post;
import static org.springframework.test.web.servlet.request.MockMvcRequestBuilders.put;
import static org.springframework.test.web.servlet.result.MockMvcResultMatchers.jsonPath;
import static org.springframework.test.web.servlet.result.MockMvcResultMatchers.status;

import com.ssafy.simai.domain.setup.SetupCameraCheckFixtures;
import com.ssafy.simai.domain.setup.SetupZoneHeightChanges;
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
        // 카메라 확인 통과(S15P31A101-646): 구역 목록 저장·채우기는 통과한 보정에서만 받는다
        jdbc.update("""
                INSERT INTO calibration_version (calibration_version, site_id, state, provider_source, reason, effective_at,
                    calibration_ref, calibration_hash, created_at)
                VALUES (?, ?, 'PROVISIONAL', 'G1_AUTO', 'setup', now(), 'file:///cal/zones.json', ?, now())
                """, "cal-zones-" + suffix, site, "sha256:" + "b".repeat(64));
        jdbc.update("""
                INSERT INTO setup_session(setup_session_id,site_id,work_unit_id,job_id,status,camera_heights_m,height_tolerance_cm,requested_by,started_at,updated_at,
                                          calibration_version,camera_check)
                VALUES (?,?,?,?,'COMPLETED','{}'::jsonb,5,?,now(),now(),?,CAST(? AS jsonb))
                """, session, site, work, job, user, "cal-zones-" + suffix,
                SetupCameraCheckFixtures.passedJson(mapper, "cal-zones-" + suffix));
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
    void 카메라_확인을_통과하지_않은_보정에서는_구역_목록을_저장하거나_채우지_못한다() throws Exception {
        // S15P31A101-646: 관제에서 뜰 카메라 경고는 세팅의 카메라 확인으로 먼저 통과해야 구역 단계로 간다. 읽기는 막지 않는다
        ArrayNode items = mapper.createArrayNode();
        items.addObject().put("source", "MANUAL").set("zone", zoneBody("확인 전"));
        jdbc.update("UPDATE setup_session SET camera_check = CAST(? AS jsonb) WHERE setup_session_id = ?",
                mapper.writeValueAsString(SetupCameraCheckFixtures.check(mapper, "cal-zones-" + suffix, "MEASURING")), session);
        putPlan(admin, items, 0).andExpect(status().isConflict())
                .andExpect(jsonPath("$.code").value("SETUP_CAMERA_CHECK_NOT_PASSED"));
        seed(admin).andExpect(status().isConflict()).andExpect(jsonPath("$.code").value("SETUP_CAMERA_CHECK_NOT_PASSED"));
        importPrevious(admin).andExpect(status().isConflict())
                .andExpect(jsonPath("$.code").value("SETUP_CAMERA_CHECK_NOT_PASSED"));
        mvc.perform(get("/api/setup-sessions/{id}/zones", session).header(HttpHeaders.AUTHORIZATION, admin))
                .andExpect(status().isOk());
        // 다른 보정(재대여 전 보정)의 통과는 지금 보정의 통과가 아니다
        jdbc.update("UPDATE setup_session SET camera_check = CAST(? AS jsonb) WHERE setup_session_id = ?",
                SetupCameraCheckFixtures.passedJson(mapper, "cal-zones-old-" + suffix), session);
        putPlan(admin, items, 0).andExpect(status().isConflict())
                .andExpect(jsonPath("$.code").value("SETUP_CAMERA_CHECK_NOT_PASSED"));
        jdbc.update("UPDATE setup_session SET camera_check = NULL WHERE setup_session_id = ?", session);
        putPlan(admin, items, 0).andExpect(status().isConflict());
        jdbc.update("UPDATE setup_session SET camera_check = CAST(? AS jsonb) WHERE setup_session_id = ?",
                SetupCameraCheckFixtures.passedJson(mapper, "cal-zones-" + suffix), session);
        putPlan(admin, items, 0).andExpect(status().isOk());
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

    @Test
    void 새_모델은_처음_채우기를_하지_않고_이전_구역_불러오기로만_확인_필요로_더한다() throws Exception {
        String a = zone("가");
        String b = zone("나");
        String layoutZone = layoutZone();
        anchor(true);

        // S15P31A101-758: 새 모델은 World 이어짐이어도 이전 구역을 기본으로 이어 오지 않는다(AI 초안만으로 시작)
        JsonNode seeded = body(seed(admin).andExpect(status().isOk()));
        assertThat(seeded.path("revision").asInt()).isZero();
        assertThat(seeded.path("items").size()).isZero();

        JsonNode imported = body(importPrevious(admin).andExpect(status().isOk()));
        assertThat(imported.path("revision").asInt()).isEqualTo(1);
        assertThat(reviewOf(imported, a)).isTrue();
        assertThat(reviewOf(imported, b)).isTrue();
        // 레이아웃이 만든 구역은 세팅 목록으로 고치지 못해 확인 표시를 달지 않는다
        assertThat(reviewOf(imported, layoutZone)).isFalse();
        assertThat(imported.path("items").get(0).path("zone").isNull()).isTrue();
        assertThat(imported.path("import_skipped").size()).isZero();

        // 다시 눌러도 이미 목록에 있는 구역은 더하지 않는다(저장도 하지 않는다)
        JsonNode again = body(importPrevious(admin).andExpect(status().isOk()));
        assertThat(again.path("revision").asInt()).isEqualTo(1);
        assertThat(again.path("items").size()).isEqualTo(3);
        // 저장한 목록(관리자가 지운 뒤)에도 덧붙인다
        putPlan(admin, mapper.createArrayNode(), 1).andExpect(status().isOk());
        JsonNode reimported = body(importPrevious(admin).andExpect(status().isOk()));
        assertThat(reimported.path("revision").asInt()).isEqualTo(3);
        assertThat(reimported.path("items").size()).isEqualTo(3);
        importPrevious(viewer).andExpect(status().isForbidden());
    }

    @Test
    void 이전_구역_불러오기는_AI_초안_구역과_같은_자리인_구역을_빼고_이름을_알린다() throws Exception {
        String opening = zoneAt("단상 개구부", "POLYGON((0 0,1.2 0,1.2 1,0 1,0 0))");
        String apart = zoneAt("작업대 상판", "POLYGON((5 5,6 5,6 6,5 6,5 5))");
        String wide = zoneAt("구역 1", "POLYGON((-2 -2,4 -2,4 4,-2 4,-2 -2))");
        anchor(true);
        // AI 초안(개구부)을 먼저 수락해 저장한 목록: 초안 구역은 초안 다각형을 0.3 m 민 볼록 다각형이다
        ArrayNode items = mapper.createArrayNode();
        ObjectNode ai = items.addObject().put("source", "AI").put("candidate_id", "cand-opening");
        ObjectNode body = zoneBody("단상 개구부 1");
        ArrayNode poly = body.putArray("polygon_xy");
        poly.addArray().add(-0.2).add(-0.3);
        poly.addArray().add(1.5).add(-0.3);
        poly.addArray().add(1.5).add(1.3);
        poly.addArray().add(-0.2).add(1.3);
        ai.set("zone", body);
        putPlan(admin, items, 0).andExpect(status().isOk());

        JsonNode imported = body(importPrevious(admin).andExpect(status().isOk()));
        assertThat(imported.path("revision").asInt()).isEqualTo(2);
        assertThat(imported.path("import_skipped").size()).isEqualTo(1);
        assertThat(imported.path("import_skipped").get(0).asString()).isEqualTo("단상 개구부");
        // 떨어진 구역, 개구부를 품은 넓은 구역(크기가 4배 넘게 다르다)은 다른 자리라 불러온다
        assertThat(reviewOf(imported, apart)).isTrue();
        assertThat(reviewOf(imported, wide)).isTrue();
        assertThat(imported.path("items").size()).isEqualTo(3);
        for (JsonNode item : imported.path("items")) {
            assertThat(item.path("zone_id").asString("")).isNotEqualTo(opening);
        }
    }

    @Test
    void 재사용은_확인_표시_없이_채우고_World_끊김은_채우지_않는다() throws Exception {
        String a = zone("가");
        // World 끊김(새 모델, 채택 아님): 빈 목록 그대로, 이전 구역은 좌표가 맞지 않아 불러올 수 없다
        JsonNode cut = body(seed(admin).andExpect(status().isOk()));
        assertThat(cut.path("revision").asInt()).isZero();
        assertThat(cut.path("items").size()).isZero();
        importPrevious(admin).andExpect(status().isConflict())
                .andExpect(jsonPath("$.code").value("SETUP_ZONE_WORLD_MISMATCH"));

        jdbc.update("UPDATE setup_session SET requested_mode = 'REUSE', mode = 'REUSE' WHERE setup_session_id = ?", session);
        JsonNode reuse = body(seed(admin).andExpect(status().isOk()));
        assertThat(reuse.path("revision").asInt()).isEqualTo(1);
        assertThat(reviewOf(reuse, a)).isFalse();
        assertThat(applier.apply(session, true, user).kept()).containsExactly(a);
    }

    @Test
    void 확인_수정_일괄_확인을_저장하고_확인_필요가_남으면_적용하지_않는다() throws Exception {
        String a = zone("가");
        String b = zone("나");
        String c = zone("다");
        anchor(true);
        JsonNode seeded = body(importPrevious(admin).andExpect(status().isOk()));
        assertThatThrownBy(() -> applier.apply(session, true, user)).isInstanceOf(SetupZonesUnconfirmedException.class)
                .hasMessage("SETUP_ZONES_UNCONFIRMED").extracting("remaining").isEqualTo(3);

        // 하나 확인, 하나 수정(본문을 보내면 확인 필요를 보내도 확인된 것으로 저장), 새 구역은 늘 확인된 것
        ArrayNode items = (ArrayNode) seeded.path("items").deepCopy();
        for (JsonNode item : items) {
            String id = item.get("zone_id").asString();
            if (id.equals(a)) ((ObjectNode) item).put("needs_review", false);
            if (id.equals(b)) ((ObjectNode) item).set("zone", zoneBody("나 고침"));
        }
        items.addObject().put("source", "MANUAL").put("needs_review", true).set("zone", zoneBody("새 구역"));
        JsonNode saved = body(putPlan(admin, items, 1).andExpect(status().isOk()));
        assertThat(reviewOf(saved, a)).isFalse();
        assertThat(reviewOf(saved, b)).isFalse();
        assertThat(reviewOf(saved, c)).isTrue();
        assertThat(saved.path("items").get(3).path("needs_review").asBoolean()).isFalse();
        assertThatThrownBy(() -> applier.apply(session, true, user)).isInstanceOf(SetupZonesUnconfirmedException.class)
                .extracting("remaining").isEqualTo(1);

        // 남은 것 모두 확인
        ArrayNode all = (ArrayNode) saved.path("items").deepCopy();
        all.forEach(item -> ((ObjectNode) item).put("needs_review", false));
        putPlan(admin, all, 2).andExpect(status().isOk());
        SetupZoneApplier.ApplyResult result = applier.apply(session, true, user);
        assertThat(result.kept()).containsExactlyInAnyOrder(a, c);
        assertThat(result.updated()).containsExactly(b);
        assertThat(result.created()).hasSize(1);
    }

    @Test
    void 구역별_높이_차이_판정을_세션과_구역_목록_응답에_싣고_자동_확인하지_않는다() throws Exception {
        String changed = zone("바뀜");
        String unchanged = zone("그대로");
        String unknown = zone("모름");
        anchor(true);

        ObjectNode zh = mapper.createObjectNode();
        zh.put("previous_scene_version", "scene-lobby-prev");
        zh.put("compared_scene_version", "scene-preview-1");
        zh.put("cell_m", 0.10);
        zh.put("height_diff_threshold_m", 0.15);
        zh.put("changed_area_ratio_threshold", 0.10);
        ArrayNode zonesNode = zh.putArray("zones");

        ObjectNode zChanged = zonesNode.addObject();
        zChanged.put("zone_id", changed).put("status", "CHANGE_SUSPECTED").putNull("reason")
                .put("changed_area_ratio", 0.36).put("max_height_diff_m", 0.35)
                .put("total_cells", 100).put("compared_cells", 100).put("changed_cells", 36);
        zChanged.putArray("max_diff_xy").add(1.5).add(1.5);

        ObjectNode zUnchanged = zonesNode.addObject();
        zUnchanged.put("zone_id", unchanged).put("status", "UNCHANGED").putNull("reason")
                .put("changed_area_ratio", 0.0).put("max_height_diff_m", 0.02)
                .put("total_cells", 100).put("compared_cells", 100).put("changed_cells", 0);
        zUnchanged.putArray("max_diff_xy").add(2.4).add(2.4);

        ObjectNode snap = mapper.createObjectNode();
        snap.set("zone_height_changes", zh);
        String validJson = SetupZoneHeightChanges.fromSnapshot(mapper, snap);
        assertThat(validJson).isNotNull();

        // UNKNOWN 인데 비율이 들어간 깨진 스냅샷은 거부한다
        ObjectNode badSnap = snap.deepCopy();
        ((ObjectNode) badSnap.at("/zone_height_changes/zones/1")).put("status", "UNKNOWN").put("reason", "INSUFFICIENT_COVERAGE");
        assertThat(SetupZoneHeightChanges.fromSnapshot(mapper, badSnap)).isNull();

        jdbc.update("UPDATE setup_session SET zone_height_changes = CAST(? AS jsonb) WHERE setup_session_id = ?",
                validJson, session);

        // 세션 단건 조회에도 zone_height_changes 가 실린다
        JsonNode sessionView = body(mvc.perform(get("/api/setup-sessions/{id}", session).header(HttpHeaders.AUTHORIZATION, admin))
                .andExpect(status().isOk()));
        assertThat(sessionView.at("/zone_height_changes/compared_scene_version").asString()).isEqualTo("scene-preview-1");

        // 불러오기 응답과 GET 응답 모두 구역별 height_change 를 싣고, 워커 결과에 없는 구역은 사유와 함께 UNKNOWN 으로 채운다
        JsonNode seeded = body(importPrevious(admin).andExpect(status().isOk()));
        assertThat(seeded.at("/zone_height_changes/previous_scene_version").asString()).isEqualTo("scene-lobby-prev");
        assertThat(heightChangeOf(seeded, changed).path("status").asString()).isEqualTo("CHANGE_SUSPECTED");
        assertThat(heightChangeOf(seeded, changed).path("changed_area_ratio").asDouble()).isEqualTo(0.36);
        assertThat(heightChangeOf(seeded, unchanged).path("status").asString()).isEqualTo("UNCHANGED");
        assertThat(heightChangeOf(seeded, unknown).path("status").asString()).isEqualTo("UNKNOWN");
        assertThat(heightChangeOf(seeded, unknown).path("reason").asString()).isEqualTo("INSUFFICIENT_COVERAGE");
        assertThat(heightChangeOf(seeded, unknown).path("changed_area_ratio").isNull()).isTrue();

        // UNCHANGED 여도 needs_review 가 자동으로 풀리지 않는다(사람이 확인해야 세팅 완료 가능)
        assertThat(reviewOf(seeded, changed)).isTrue();
        assertThat(reviewOf(seeded, unchanged)).isTrue();
        assertThat(reviewOf(seeded, unknown)).isTrue();
        assertThatThrownBy(() -> applier.apply(session, true, user)).isInstanceOf(SetupZonesUnconfirmedException.class)
                .extracting("remaining").isEqualTo(3);

        // 새로 그린 구역(zone_id null)은 height_change 가 null 이고, 클라이언트가 height_change 필드를 함께 보내도 PUT 이 통과한다
        ArrayNode items = (ArrayNode) seeded.path("items").deepCopy();
        items.addObject().put("source", "MANUAL").set("zone", zoneBody("새 구역"));
        JsonNode updated = body(putPlan(admin, items, 1).andExpect(status().isOk()));
        assertThat(updated.path("items").get(3).path("height_change").isNull()).isTrue();
    }

    private static JsonNode heightChangeOf(JsonNode plan, String zoneId) {
        for (JsonNode item : plan.path("items")) {
            if (item.hasNonNull("zone_id") && zoneId.equals(item.get("zone_id").asString())) {
                return item.path("height_change");
            }
        }
        throw new AssertionError("목록에 없는 구역: " + zoneId);
    }

    private void anchor(boolean accepted) {
        jdbc.update("UPDATE setup_session SET world_anchor = jsonb_build_object('accepted', ?) WHERE setup_session_id = ?",
                accepted, session);
    }

    private String layoutZone() {
        String id = "z-plan-layout-" + suffix;
        jdbc.update("""
                INSERT INTO zone(zone_id,zone_version,site_id,name,zone_kind,eval_type,polygon,severity,valid_from,work_id,layout_item_id)
                VALUES (?,1,?,'레이아웃','OPENING','GROUND_FOOTPRINT',ST_GeomFromText('POLYGON((0 0,2 0,2 1,0 1,0 0))',0),'HIGH',now(),?,'item-1')
                """, id, site, work);
        return id;
    }

    private static boolean reviewOf(JsonNode plan, String zoneId) {
        for (JsonNode item : plan.path("items")) {
            if (item.hasNonNull("zone_id") && zoneId.equals(item.get("zone_id").asString())) {
                return item.path("needs_review").asBoolean(false);
            }
        }
        throw new AssertionError("목록에 없는 구역: " + zoneId);
    }

    private ResultActions seed(String token) throws Exception {
        return mvc.perform(post("/api/setup-sessions/{id}/zones/seed", session).header(HttpHeaders.AUTHORIZATION, token));
    }

    private ResultActions importPrevious(String token) throws Exception {
        return mvc.perform(post("/api/setup-sessions/{id}/zones/import-previous", session).header(HttpHeaders.AUTHORIZATION, token));
    }

    private String zoneAt(String name, String wkt) {
        String id = "z-plan-at-" + Math.abs(name.hashCode()) + "-" + suffix;
        jdbc.update("""
                INSERT INTO zone(zone_id,zone_version,site_id,name,zone_kind,eval_type,polygon,severity,valid_from)
                VALUES (?,1,?,?,'OPENING','GROUND_FOOTPRINT',ST_GeomFromText(?,0),'HIGH',now())
                """, id, site, name, wkt);
        return id;
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
