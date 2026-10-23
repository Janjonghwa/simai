package com.ssafy.simai.global.config;

import static org.springframework.test.web.servlet.request.MockMvcRequestBuilders.get;
import static org.springframework.test.web.servlet.result.MockMvcResultMatchers.jsonPath;
import static org.springframework.test.web.servlet.result.MockMvcResultMatchers.redirectedUrl;
import static org.springframework.test.web.servlet.result.MockMvcResultMatchers.status;

import org.junit.jupiter.api.Test;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.boot.test.context.SpringBootTest;
import org.springframework.boot.webmvc.test.autoconfigure.AutoConfigureMockMvc;
import org.springframework.test.context.ActiveProfiles;
import org.springframework.test.web.servlet.MockMvc;

@SpringBootTest
@AutoConfigureMockMvc
@ActiveProfiles("test")
class OpenApiDocsIntegrationTest {

    @Autowired
    MockMvc mvc;

    @Test
    void 토큰_없이_OpenAPI_문서를_받고_api_경로와_JWT_방식이_실린다() throws Exception {
        mvc.perform(get("/api/docs/openapi.json"))
                .andExpect(status().isOk())
                .andExpect(jsonPath("$.paths['/api/auth/login']").exists())
                .andExpect(jsonPath("$.components.securitySchemes['" + OpenApiConfig.BEARER + "'].scheme").value("bearer"));
    }

    @Test
    void 문서에는_api_밖의_경로를_싣지_않는다() throws Exception {
        mvc.perform(get("/api/docs/openapi.json"))
                .andExpect(jsonPath("$.paths['/error']").doesNotExist());
    }

    @Test
    void Swagger_UI_는_토큰_없이_열린다() throws Exception {
        // /api/docs 는 화면 파일 위치로 넘긴다. 둘 다 /api 아래라 Nginx 가 그대로 서버로 넘긴다
        mvc.perform(get("/api/docs"))
                .andExpect(status().is3xxRedirection())
                .andExpect(redirectedUrl("/api/swagger-ui/index.html"));
        mvc.perform(get("/api/swagger-ui/index.html"))
                .andExpect(status().isOk());
    }

    @Test
    void 문서_밖의_API_는_여전히_토큰이_있어야_한다() throws Exception {
        mvc.perform(get("/api/events"))
                .andExpect(status().isUnauthorized());
    }
}
