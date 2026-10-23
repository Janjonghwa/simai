package com.ssafy.simai.global.config;

import io.swagger.v3.oas.models.Components;
import io.swagger.v3.oas.models.OpenAPI;
import io.swagger.v3.oas.models.info.Info;
import io.swagger.v3.oas.models.security.SecurityRequirement;
import io.swagger.v3.oas.models.security.SecurityScheme;
import org.springframework.context.annotation.Bean;
import org.springframework.context.annotation.Configuration;

/**
 * 명세 12.6 의 경로는 기본안이고 확정본은 이 문서다. 문서는 /api/docs/openapi.json, 화면은 /api/docs(→ /api/swagger-ui/index.html, application.yaml).
 * 모든 API 에 JWT 를 기본으로 걸어 Swagger UI 의 Authorize 로 시험할 수 있게 한다. 공개 API(로그인 등)도 표시만 붙고 동작은 같다.
 */
@Configuration
public class OpenApiConfig {

    static final String BEARER = "bearer-jwt";

    @Bean
    OpenAPI simaiOpenApi() {
        return new OpenAPI()
                .info(new Info().title("simAI API").version("v1"))
                .components(new Components().addSecuritySchemes(BEARER, new SecurityScheme()
                        .type(SecurityScheme.Type.HTTP)
                        .scheme("bearer")
                        .bearerFormat("JWT")))
                .addSecurityItem(new SecurityRequirement().addList(BEARER));
    }
}
