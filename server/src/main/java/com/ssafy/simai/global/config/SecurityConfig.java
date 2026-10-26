package com.ssafy.simai.global.config;

import com.ssafy.simai.domain.machine.MachineKind;
import com.ssafy.simai.domain.machine.MachineTokenService;
import com.ssafy.simai.domain.realtime.RealtimeTopics;
import com.ssafy.simai.domain.tier0.Tier0Paths;
import com.ssafy.simai.global.properties.AuthProperties;
import com.ssafy.simai.global.properties.Tier0Properties;
import com.ssafy.simai.global.security.AccessTokenIssuer;
import com.ssafy.simai.global.security.MachineTokenAuthenticationFilter;
import com.ssafy.simai.global.security.SecurityErrorResponder;
import com.ssafy.simai.global.security.Tier0TokenAuthenticationFilter;
import org.springframework.boot.autoconfigure.condition.ConditionalOnWebApplication;
import org.springframework.context.annotation.Bean;
import org.springframework.context.annotation.Configuration;
import org.springframework.core.annotation.Order;
import org.springframework.http.HttpMethod;
import org.springframework.security.config.Customizer;
import org.springframework.security.config.annotation.method.configuration.EnableMethodSecurity;
import org.springframework.security.config.annotation.web.builders.HttpSecurity;
import org.springframework.security.config.http.SessionCreationPolicy;
import org.springframework.security.oauth2.server.resource.authentication.JwtAuthenticationConverter;
import org.springframework.security.oauth2.server.resource.authentication.JwtGrantedAuthoritiesConverter;
import org.springframework.security.web.SecurityFilterChain;
import org.springframework.security.web.authentication.AnonymousAuthenticationFilter;
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

    /**
     * I1(Tier 0 → EC2 WebSocket, 명세 12.2·12.3)은 사용자 JWT 가 아니라 Tier 0 토큰으로 인증한다.
     * 업그레이드 요청 하나만 거치므로 이 체인이 사용자 체인보다 먼저 경로를 가져간다.
     */
    @Bean
    @Order(1)
    SecurityFilterChain tier0FilterChain(HttpSecurity http, Tier0Properties tier0) throws Exception {
        SecurityErrorResponder errors = new SecurityErrorResponder();
        http
                .securityMatcher(Tier0Paths.I1)
                .csrf(csrf -> csrf.disable())
                .sessionManagement(session -> session.sessionCreationPolicy(SessionCreationPolicy.STATELESS))
                .addFilterBefore(new Tier0TokenAuthenticationFilter(tier0), AnonymousAuthenticationFilter.class)
                .authorizeHttpRequests(auth -> auth.anyRequest().hasAuthority(Tier0TokenAuthenticationFilter.ROLE))
                .exceptionHandling(exceptions -> exceptions
                        .authenticationEntryPoint(errors)
                        .accessDeniedHandler(errors));
        return http.build();
    }

    /**
     * 현장 노트북(하트비트, Tier 0 엔드포인트 조회). 기기 토큰(명세 14·12.6, S15P31A101-222).
     * 사용자 JWT 체인과 나눈다. 같은 체인이면 JWT 리소스 서버가 기기 토큰을 JWT 로 읽다가 거부한다.
     */
    @Bean
    @Order(2)
    SecurityFilterChain deviceFilterChain(HttpSecurity http, MachineTokenService tokens) throws Exception {
        // 엔드포인트는 조회(GET)만 기기 토큰이다. 등록은 Tier 0 가 I1 tier0_endpoint 로 한다(12.6, FR-138)
        return machineChain(http, MachineKind.DEVICE, tokens, matchers -> matchers
                .requestMatchers("/api/devices/**")
                .requestMatchers(HttpMethod.GET, "/api/endpoints/tier0"));
    }

    /** RunPod 분석 워커(I5 작업 대여·결과, 워커 객체 서명). 워커 토큰(명세 12.5, S15P31A101-222) */
    @Bean
    @Order(3)
    SecurityFilterChain workerFilterChain(HttpSecurity http, MachineTokenService tokens) throws Exception {
        return machineChain(http, MachineKind.WORKER, tokens, matchers -> matchers.requestMatchers("/api/worker/**"));
    }

    private static SecurityFilterChain machineChain(HttpSecurity http, MachineKind kind, MachineTokenService tokens,
            Customizer<HttpSecurity.RequestMatcherConfigurer> paths) throws Exception {
        SecurityErrorResponder errors = new SecurityErrorResponder();
        http
                .securityMatchers(paths)
                .csrf(csrf -> csrf.disable())
                .sessionManagement(session -> session.sessionCreationPolicy(SessionCreationPolicy.STATELESS))
                .addFilterBefore(new MachineTokenAuthenticationFilter(kind, tokens), AnonymousAuthenticationFilter.class)
                .authorizeHttpRequests(auth -> auth.anyRequest().hasAuthority(kind.authority()))
                .exceptionHandling(exceptions -> exceptions
                        .authenticationEntryPoint(errors)
                        .accessDeniedHandler(errors));
        return http.build();
    }

    @Bean
    @Order(4)
    SecurityFilterChain securityFilterChain(HttpSecurity http, JwtAuthenticationConverter roleConverter) throws Exception {
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
                        // 관제 앱 STOMP 업그레이드. 브라우저는 여기에 헤더를 싣지 못해 CONNECT 프레임에서 JWT 를 본다(StompAuthInterceptor)
                        .requestMatchers(HttpMethod.GET, RealtimeTopics.ENDPOINT).permitAll()
                        // Tier 0 가 구성 묶음 파일을 헤더 없이 내려받는다. 주소의 서명(sig)을 컨트롤러가 검사한다(BundleUrlSigner)
                        .requestMatchers(HttpMethod.GET, "/api/config-bundles/*/files/*").permitAll()
                        .anyRequest().authenticated())
                // 401·403 에도 ProblemDetail(code) 본문을 싣는다. 관제 앱이 만료(갱신 후 재시도)와 다른 오류를 가른다
                .exceptionHandling(exceptions -> exceptions
                        .authenticationEntryPoint(errors)
                        .accessDeniedHandler(errors))
                .oauth2ResourceServer(oauth2 -> oauth2
                        .authenticationEntryPoint(errors)
                        .accessDeniedHandler(errors)
                        .jwt(jwt -> jwt.jwtAuthenticationConverter(roleConverter)));
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

    /** role 클레임(ADMIN 등)을 ROLE_ADMIN 권한으로 바꾼다. REST 와 STOMP CONNECT(StompConfig)가 같이 쓴다 */
    @Bean
    JwtAuthenticationConverter roleConverter() {
        JwtGrantedAuthoritiesConverter authorities = new JwtGrantedAuthoritiesConverter();
        authorities.setAuthoritiesClaimName(AccessTokenIssuer.ROLE_CLAIM);
        authorities.setAuthorityPrefix("ROLE_");
        JwtAuthenticationConverter converter = new JwtAuthenticationConverter();
        converter.setJwtGrantedAuthoritiesConverter(authorities);
        return converter;
    }
}
