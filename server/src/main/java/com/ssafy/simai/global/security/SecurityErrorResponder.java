package com.ssafy.simai.global.security;

import com.ssafy.simai.global.exception.ErrorCode;
import jakarta.servlet.ServletException;
import jakarta.servlet.http.HttpServletRequest;
import jakarta.servlet.http.HttpServletResponse;
import java.io.IOException;
import java.nio.charset.StandardCharsets;
import org.springframework.http.MediaType;
import org.springframework.security.access.AccessDeniedException;
import org.springframework.security.core.AuthenticationException;
import org.springframework.security.oauth2.server.resource.web.BearerTokenAuthenticationEntryPoint;
import org.springframework.security.oauth2.server.resource.web.access.BearerTokenAccessDeniedHandler;
import org.springframework.security.web.AuthenticationEntryPoint;
import org.springframework.security.web.access.AccessDeniedHandler;

/**
 * 보안 필터가 거부한 401·403 에도 컨트롤러 오류와 같은 ProblemDetail 본문(detail, code)을 싣는다.
 * 상태와 WWW-Authenticate 헤더(RFC 6750)는 Spring 기본 처리기가 정한다.
 */
public class SecurityErrorResponder implements AuthenticationEntryPoint, AccessDeniedHandler {

    private final AuthenticationEntryPoint bearerEntryPoint = new BearerTokenAuthenticationEntryPoint();
    private final AccessDeniedHandler bearerDeniedHandler = new BearerTokenAccessDeniedHandler();

    @Override
    public void commence(HttpServletRequest request, HttpServletResponse response, AuthenticationException e)
            throws IOException, ServletException {
        bearerEntryPoint.commence(request, response, e);
        write(response, ErrorCode.Auth.UNAUTHORIZED);
    }

    @Override
    public void handle(HttpServletRequest request, HttpServletResponse response, AccessDeniedException e)
            throws IOException, ServletException {
        bearerDeniedHandler.handle(request, response, e);
        write(response, ErrorCode.Auth.FORBIDDEN);
    }

    // 문구가 상수뿐이라 JSON 을 직접 쓴다(요청 값을 넣지 않는다)
    private static void write(HttpServletResponse response, ErrorCode errorCode) throws IOException {
        response.setStatus(errorCode.status().value());
        response.setContentType(MediaType.APPLICATION_PROBLEM_JSON_VALUE);
        response.setCharacterEncoding(StandardCharsets.UTF_8.name());
        response.getWriter().write("{\"type\":\"about:blank\",\"title\":\"" + errorCode.status().getReasonPhrase()
                + "\",\"status\":" + errorCode.status().value()
                + ",\"detail\":\"" + errorCode.message()
                + "\",\"code\":\"" + errorCode.name() + "\"}");
    }
}
