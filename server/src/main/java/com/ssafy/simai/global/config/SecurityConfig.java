package com.ssafy.simai.global.config;

import com.ssafy.simai.global.properties.AuthProperties;
import com.ssafy.simai.global.security.AccessTokenIssuer;
import com.ssafy.simai.global.security.SecurityErrorResponder;
import org.springframework.boot.autoconfigure.condition.ConditionalOnWebApplication;
import org.springframework.context.annotation.Bean;
import org.springframework.context.annotation.Configuration;
import org.springframework.http.HttpMethod;
import org.springframework.security.config.Customizer;
import org.springframework.security.config.annotation.method.configuration.EnableMethodSecurity;
import org.springframework.security.config.annotation.web.builders.HttpSecurity;
import org.springframework.security.config.http.SessionCreationPolicy;
import org.springframework.security.oauth2.server.resource.authentication.JwtAuthenticationConverter;
import org.springframework.security.oauth2.server.resource.authentication.JwtGrantedAuthoritiesConverter;
import org.springframework.security.web.SecurityFilterChain;
import org.springframework.web.cors.CorsConfiguration;
import org.springframework.web.cors.CorsConfigurationSource;
import org.springframework.web.cors.UrlBasedCorsConfigurationSource;

/**
 * 공개 경로는 로그인·갱신·로그아웃과 API 문서뿐이고 나머지는 JWT 가 있어야 한다(명세 12.6 권한 열, FR-2).
 * API별 역할은 컨트롤러에 @PreAuthorize 로 붙인다. 열람 API 는 인증만 있으면 된다(역할 3종 모두 열람 가능).
 * 예: 관리자만 → hasRole('ADMIN'), 반장·관리자 → hasAnyRole('FOREMAN', 'ADMIN').
 * 관리 명령은 웹 서버 없이 뜨므로 이 설정을 쓰지 않는다.
 */
@Configuration
@ConditionalOnWebApplication(type = ConditionalOnWebApplication.Type.SERVLET)
@EnableMethodSecurity
public class SecurityConfig {

    @Bean
    SecurityFilterChain securityFilterChain(HttpSecurity http) throws Exception {
        SecurityErrorResponder errors = new SecurityErrorResponder();
        http
                // API 인증은 Authorization 헤더로만 한다. 갱신 토큰 쿠키는 SameSite=Strict·Path=/api/auth 라
                // 다른 사이트에서 갱신·로그아웃을 대신 부를 수 없다
                .csrf(csrf -> csrf.disable())
                .cors(Customizer.withDefaults())
                .sessionManagement(session -> session.sessionCreationPolicy(SessionCreationPolicy.STATELESS))
                .authorizeHttpRequests(auth -> auth
                        // 갱신·로그아웃은 access 토큰이 만료된 뒤에도 불러야 한다. 갱신 토큰 자체가 증명이다
                        .requestMatchers(HttpMethod.POST, "/api/auth/login", "/api/auth/refresh", "/api/auth/logout")
                        .permitAll()
                        // 오류 응답 본문을 만드는 경로. 막으면 400·401 이 빈 본문이 된다
                        .requestMatchers("/error").permitAll()
                        // OpenAPI 문서·Swagger UI(OpenApiConfig). 경로 모양만 보이고 API 호출은 여전히 JWT 가 있어야 한다
                        .requestMatchers(HttpMethod.GET, "/api/docs", "/api/docs/**", "/api/swagger-ui/**").permitAll()
                        .anyRequest().authenticated())
                // 401·403 에도 ProblemDetail(code) 본문을 싣는다. 관제 앱이 만료(갱신 후 재시도)와 다른 오류를 가른다
                .exceptionHandling(exceptions -> exceptions
                        .authenticationEntryPoint(errors)
                        .accessDeniedHandler(errors))
                .oauth2ResourceServer(oauth2 -> oauth2
                        .authenticationEntryPoint(errors)
                        .accessDeniedHandler(errors)
                        .jwt(jwt -> jwt.jwtAuthenticationConverter(roleConverter())));
        return http.build();
    }

    @Bean
    CorsConfigurationSource corsConfigurationSource(AuthProperties properties) {
        UrlBasedCorsConfigurationSource source = new UrlBasedCorsConfigurationSource();
        if (properties.corsAllowedOrigins().isEmpty()) {
            return source;
        }
        CorsConfiguration config = new CorsConfiguration();
        config.setAllowedOrigins(properties.corsAllowedOrigins());
        config.addAllowedMethod(CorsConfiguration.ALL);
        config.addAllowedHeader(CorsConfiguration.ALL);
        // 갱신 토큰 쿠키를 개발 서버 출처에서도 주고받는다. 출처를 정확히 적었을 때만 켤 수 있다
        config.setAllowCredentials(true);
        source.registerCorsConfiguration("/api/**", config);
        return source;
    }

    // role 클레임(ADMIN 등)을 ROLE_ADMIN 권한으로 바꾼다
    private static JwtAuthenticationConverter roleConverter() {
        JwtGrantedAuthoritiesConverter authorities = new JwtGrantedAuthoritiesConverter();
        authorities.setAuthoritiesClaimName(AccessTokenIssuer.ROLE_CLAIM);
        authorities.setAuthorityPrefix("ROLE_");
        JwtAuthenticationConverter converter = new JwtAuthenticationConverter();
        converter.setJwtGrantedAuthoritiesConverter(authorities);
        return converter;
    }
}
