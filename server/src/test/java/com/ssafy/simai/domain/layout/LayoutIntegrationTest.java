package com.ssafy.simai.domain.layout;

import static org.assertj.core.api.Assertions.assertThat;
import static org.springframework.test.web.servlet.request.MockMvcRequestBuilders.get;
import static org.springframework.test.web.servlet.request.MockMvcRequestBuilders.post;
import static org.springframework.test.web.servlet.request.MockMvcRequestBuilders.put;
import static org.springframework.test.web.servlet.result.MockMvcResultMatchers.jsonPath;
import static org.springframework.test.web.servlet.result.MockMvcResultMatchers.status;

import com.jayway.jsonpath.JsonPath;
import com.ssafy.simai.domain.user.entity.AppUser;
import com.ssafy.simai.domain.user.entity.Role;
import com.ssafy.simai.domain.user.repository.AppUserRepository;
import java.time.Instant;
import java.util.List;
import java.util.UUID;
import org.junit.jupiter.api.AfterEach;
import org.junit.jupiter.api.Test;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.boot.test.context.SpringBootTest;
import org.springframework.boot.webmvc.test.autoconfigure.AutoConfigureMockMvc;
import org.springframework.http.HttpHeaders;
import org.springframework.http.MediaType;
import org.springframework.jdbc.core.JdbcTemplate;
import org.springframework.security.crypto.password.PasswordEncoder;
import org.springframework.test.context.ActiveProfiles;
import org.springframework.test.web.servlet.MockMvc;
import tools.jackson.databind.json.JsonMapper;
import tools.jackson.databind.node.ArrayNode;
import tools.jackson.databind.node.ObjectNode;

/** 가상 레이아웃 API(GET·POST·PUT /api/layouts) 통합 시험(S15P31A101-30) */
@SpringBootTest
@AutoConfigureMockMvc
@ActiveProfiles("test")
class LayoutIntegrationTest {

    private static final String PASSWORD = "correct-horse";
    private static final JsonMapper MAPPER = JsonMapper.builder().build();

    @Autowired
    MockMvc mvc;

    @Autowired
    JdbcTemplate jdbc;

    @Autowired
    AppUserRepository users;

    @Autowired
    PasswordEncoder passwordEncoder;

    private final String suffix = UUID.randomUUID().toString().substring(0, 8);

    @AfterEach
    void cleanUp() {
        jdbc.update("""
                DELETE FROM layout_plan_revision WHERE layout_id IN (SELECT layout_id FROM layout_plan
                    WHERE created_by IN (SELECT user_id FROM app_user WHERE login_id LIKE ?))
                """, "%-" + suffix);
        jdbc.update("DELETE FROM layout_plan WHERE created_by IN (SELECT user_id FROM app_user WHERE login_id LIKE ?)", "%-" + suffix);
        jdbc.update("DELETE FROM refresh_token WHERE user_id IN (SELECT user_id FROM app_user WHERE login_id LIKE ?)", "%-" + suffix);
        jdbc.update("DELETE FROM app_user WHERE login_id LIKE ?", "%-" + suffix);
    }

    @Test
    void 만들고_고칠_때마다_revision_이_오르고_낡은_revision_으로_고치면_409_다() throws Exception {
        String admin = token("admin", Role.ADMIN);
        String viewer = token("viewer", Role.VIEWER);

        ArrayNode items = MAPPER.createArrayNode();
        items.add(item("o-1", "OPENING", 2.5, 1.5, 0.05, 1.0, 1.0, 0.1, 0.0, "  개구부  "));
        items.add(item("s-1", "SCAFFOLD", 6.0, 4.0, 1.6, 6.0, 1.2, 3.2, 0.3, null));
        String created = mvc.perform(post("/api/layouts").header(HttpHeaders.AUTHORIZATION, admin)
                        .contentType(MediaType.APPLICATION_JSON).content(body("  2층 비계 계획  ", items, null)))
                .andExpect(status().isCreated())
                .andExpect(jsonPath("$.name").value("2층 비계 계획"))
                .andExpect(jsonPath("$.revision").value(1))
                .andExpect(jsonPath("$.layout_version").value("lv-1"))
                .andExpect(jsonPath("$.source").value("PLANNED"))
                .andExpect(jsonPath("$.items.length()").value(2))
                .andExpect(jsonPath("$.items[0].label").value("개구부"))
                .andExpect(jsonPath("$.items[1].label").value((Object) null))
                .andExpect(jsonPath("$.items[1].yaw_rad").value(0.3))
                .andExpect(jsonPath("$.items[1].size[2]").value(3.2))
                .andReturn().getResponse().getContentAsString();
        Number layoutId = JsonPath.read(created, "$.layout_id");

        // 하나 조회는 열람 계정도 된다
        mvc.perform(get("/api/layouts/{id}", layoutId).header(HttpHeaders.AUTHORIZATION, viewer))
                .andExpect(status().isOk())
                .andExpect(jsonPath("$.items[0].item_id").value("o-1"));

        // 시작한 revision 1 로 고치면 2
        ArrayNode fewer = MAPPER.createArrayNode().add(item("e-1", "EQUIPMENT", 10, 2, 1.5, 3, 2, 3, -3.14, "굴착기"));
        mvc.perform(put("/api/layouts/{id}", layoutId).header(HttpHeaders.AUTHORIZATION, admin)
                        .contentType(MediaType.APPLICATION_JSON).content(body("2층 비계 계획(수정)", fewer, 1)))
                .andExpect(status().isOk())
                .andExpect(jsonPath("$.revision").value(2))
                .andExpect(jsonPath("$.layout_version").value("lv-2"))
                .andExpect(jsonPath("$.items.length()").value(1))
                .andExpect(jsonPath("$.items[0].kind").value("EQUIPMENT"));
        // 다른 사람이 이미 고쳤는데 낡은 revision 1 로 고치면 409, 바뀌지 않는다
        mvc.perform(put("/api/layouts/{id}", layoutId).header(HttpHeaders.AUTHORIZATION, admin)
                        .contentType(MediaType.APPLICATION_JSON).content(body("덮어쓰기", items, 1)))
                .andExpect(status().isConflict())
                .andExpect(jsonPath("$.code").value("LAYOUT_REVISION_CONFLICT"));
        mvc.perform(get("/api/layouts/{id}", layoutId).header(HttpHeaders.AUTHORIZATION, viewer))
                .andExpect(jsonPath("$.name").value("2층 비계 계획(수정)"))
                .andExpect(jsonPath("$.revision").value(2))
                .andExpect(jsonPath("$.items.length()").value(1));
        // PUT 도 같은 검사를 하고, 틀리면 바뀌지 않는다
        mvc.perform(put("/api/layouts/{id}", layoutId).header(HttpHeaders.AUTHORIZATION, admin)
                        .contentType(MediaType.APPLICATION_JSON)
                        .content(body("계획", MAPPER.createArrayNode().add(item("a", "FENCE", 1, 1, 0, 1, 1, 1, 0, null)), 2)))
                .andExpect(status().isBadRequest())
                .andExpect(jsonPath("$.code").value("INVALID_LAYOUT"));
        // 지난 revision 의 배치는 이력으로 남는다(계약: 덮어쓰지 않고 새 버전을 더한다)
        mvc.perform(get("/api/layouts/{id}/revisions/{r}", layoutId, 1).header(HttpHeaders.AUTHORIZATION, viewer))
                .andExpect(status().isOk())
                .andExpect(jsonPath("$.layout_version").value("lv-1"))
                .andExpect(jsonPath("$.name").value("2층 비계 계획"))
                .andExpect(jsonPath("$.items.length()").value(2));
        mvc.perform(get("/api/layouts/{id}/revisions/{r}", layoutId, 9).header(HttpHeaders.AUTHORIZATION, viewer))
                .andExpect(status().isNotFound());
        // revision 을 주지 않으면 검사하지 않는다
        mvc.perform(put("/api/layouts/{id}", layoutId).header(HttpHeaders.AUTHORIZATION, admin)
                        .contentType(MediaType.APPLICATION_JSON).content(body("빈 계획", MAPPER.createArrayNode(), null)))
                .andExpect(status().isOk())
                .andExpect(jsonPath("$.revision").value(3))
                .andExpect(jsonPath("$.items.length()").value(0));

        // 목록은 항목 없이 개수만
        String list = mvc.perform(get("/api/layouts").header(HttpHeaders.AUTHORIZATION, admin))
                .andExpect(status().isOk()).andReturn().getResponse().getContentAsString();
        List<Integer> counts = JsonPath.read(list, "$[?(@.layout_id == " + layoutId + ")].item_count");
        assertThat(counts).containsExactly(0);
        List<Object> listedItems = JsonPath.read(list, "$[?(@.layout_id == " + layoutId + ")].items");
        assertThat(listedItems).isEmpty();
    }

    @Test
    void 권한_없는_요청과_없는_레이아웃을_거부한다() throws Exception {
        String admin = token("admin", Role.ADMIN);
        String viewer = token("viewer", Role.VIEWER);
        String ok = body("계획", MAPPER.createArrayNode(), null);

        mvc.perform(get("/api/layouts").header(HttpHeaders.AUTHORIZATION, viewer)).andExpect(status().isForbidden());
        mvc.perform(post("/api/layouts").header(HttpHeaders.AUTHORIZATION, viewer)
                .contentType(MediaType.APPLICATION_JSON).content(ok)).andExpect(status().isForbidden());
        mvc.perform(get("/api/layouts")).andExpect(status().isUnauthorized());
        mvc.perform(get("/api/layouts/{id}", 999_999_999L).header(HttpHeaders.AUTHORIZATION, viewer))
                .andExpect(status().isNotFound())
                .andExpect(jsonPath("$.code").value("LAYOUT_NOT_FOUND"));
        mvc.perform(put("/api/layouts/{id}", 999_999_999L).header(HttpHeaders.AUTHORIZATION, admin)
                        .contentType(MediaType.APPLICATION_JSON).content(ok))
                .andExpect(status().isNotFound())
                .andExpect(jsonPath("$.code").value("LAYOUT_NOT_FOUND"));
        String created = mvc.perform(post("/api/layouts").header(HttpHeaders.AUTHORIZATION, admin)
                        .contentType(MediaType.APPLICATION_JSON).content(ok))
                .andExpect(status().isCreated()).andReturn().getResponse().getContentAsString();
        Number id = JsonPath.read(created, "$.layout_id");
        mvc.perform(put("/api/layouts/{id}", id)
                        .header(HttpHeaders.AUTHORIZATION, viewer).contentType(MediaType.APPLICATION_JSON).content(ok))
                .andExpect(status().isForbidden());
        // 반장도 하나 조회는 되고 목록은 안 된다(12.6: 조회는 열람, 목록은 관리자)
        String foreman = token("foreman", Role.FOREMAN);
        mvc.perform(get("/api/layouts/{id}", id).header(HttpHeaders.AUTHORIZATION, foreman)).andExpect(status().isOk());
        mvc.perform(get("/api/layouts").header(HttpHeaders.AUTHORIZATION, foreman)).andExpect(status().isForbidden());
    }

    @Test
    void 입력_검증_반례를_거부하고_경계값은_받는다() throws Exception {
        String admin = token("admin", Role.ADMIN);
        ArrayNode tooMany = MAPPER.createArrayNode();
        ArrayNode maxItems = MAPPER.createArrayNode();
        for (int i = 0; i < 201; i++) {
            tooMany.add(item("i-" + i, "OPENING", 1, 1, 0, 1, 1, 0.1, 0, null));
            if (i < 200) {
                maxItems.add(item("i-" + i, "OPENING", 1, 1, 0, 1, 1, 0.1, 0, null));
            }
        }
        String[] invalid = {
                body("   ", MAPPER.createArrayNode(), null),
                body("가".repeat(101), MAPPER.createArrayNode(), null),
                body("계획" + (char) 0, MAPPER.createArrayNode(), null),
                "{\"name\":\"계획\"}",
                "{}",
                body("계획", tooMany, null),
                body("계획", MAPPER.createArrayNode().add(item("a", "OPENING", 1, 1, 0, 1, 1, 1, 0, null))
                        .add(item("a", "SCAFFOLD", 2, 2, 0, 1, 1, 1, 0, null)), null),
                body("계획", MAPPER.createArrayNode().add(item("a", "FENCE", 1, 1, 0, 1, 1, 1, 0, null)), null),
                body("계획", MAPPER.createArrayNode().add(item(" ", "OPENING", 1, 1, 0, 1, 1, 1, 0, null)), null),
                body("계획", MAPPER.createArrayNode().add(item("a", "OPENING", 1, 1, 0, 0, 1, 1, 0, null)), null),
                body("계획", MAPPER.createArrayNode().add(item("a", "OPENING", 1, 1, 0, 1, 1, -1, 0, null)), null),
                body("계획", MAPPER.createArrayNode().add(item("a", "OPENING", 1, 1, 0, 1, 1, 1, 3.2, null)), null),
                body("계획", MAPPER.createArrayNode().add(item("a", "OPENING", 1, 1, 0, 1, 1, 1, 3.14159275, null)), null),
                body("계획", MAPPER.createArrayNode().add(item("a", "OPENING", 20_000, 1, 0, 1, 1, 1, 0, null)), null),
                body("계획", MAPPER.createArrayNode().add(item("a", "OPENING", 1, 1, 0, 1, 1, 1, 0, "가".repeat(101))), null),
                body("계획", MAPPER.createArrayNode().add(shortCenter()), null),
                body("계획", MAPPER.createArrayNode().add(noYaw()), null),
                body("계획", MAPPER.createArrayNode(), 0)
        };
        for (String b : invalid) {
            mvc.perform(post("/api/layouts").header(HttpHeaders.AUTHORIZATION, admin)
                            .contentType(MediaType.APPLICATION_JSON).content(b))
                    .andExpect(status().isBadRequest())
                    .andExpect(jsonPath("$.code").value("INVALID_LAYOUT"));
        }
        // 200개·100자·yaw ±π 경계는 받는다
        // yaw 한계는 관제 앱 YAW_LIMIT_RAD(3.1415927) 그대로
        maxItems.set(0, item("i-0", "OPENING", 1, 1, 0, 1, 1, 0.1, 3.1415927, "가".repeat(100)));
        maxItems.set(1, item("i-1", "OPENING", 1, 1, 0, 1, 1, 0.1, -3.1415927, null));
        mvc.perform(post("/api/layouts").header(HttpHeaders.AUTHORIZATION, admin)
                        .contentType(MediaType.APPLICATION_JSON).content(body("가".repeat(100), maxItems, null)))
                .andExpect(status().isCreated())
                .andExpect(jsonPath("$.items.length()").value(200));
    }

    // ---- 도우미

    private static String body(String name, ArrayNode items, Integer revision) {
        ObjectNode b = MAPPER.createObjectNode();
        b.put("name", name);
        b.set("items", items);
        if (revision != null) {
            b.put("revision", revision);
        }
        return MAPPER.writeValueAsString(b);
    }

    private static ObjectNode item(String id, String kind, double cx, double cy, double cz, double sx, double sy, double sz,
            double yaw, String label) {
        ObjectNode o = MAPPER.createObjectNode();
        o.put("item_id", id);
        o.put("kind", kind);
        o.putArray("center").add(cx).add(cy).add(cz);
        o.putArray("size").add(sx).add(sy).add(sz);
        o.put("yaw_rad", yaw);
        if (label != null) {
            o.put("label", label);
        }
        return o;
    }

    private static ObjectNode shortCenter() {
        ObjectNode o = item("a", "OPENING", 1, 1, 0, 1, 1, 1, 0, null);
        o.putArray("center").add(1).add(1);
        return o;
    }

    private static ObjectNode noYaw() {
        ObjectNode o = item("a", "OPENING", 1, 1, 0, 1, 1, 1, 0, null);
        o.remove("yaw_rad");
        return o;
    }

    private String token(String name, Role role) throws Exception {
        String loginId = name + "-" + suffix;
        if (users.findByLoginId(loginId).isEmpty()) {
            users.saveAndFlush(new AppUser(loginId, passwordEncoder.encode(PASSWORD), role, null, Instant.now()));
        }
        String body = mvc.perform(post("/api/auth/login").contentType(MediaType.APPLICATION_JSON)
                        .content("{\"username\":\"" + loginId + "\",\"password\":\"" + PASSWORD + "\"}"))
                .andExpect(status().isOk()).andReturn().getResponse().getContentAsString();
        return "Bearer " + JsonPath.read(body, "$.accessToken");
    }
}
