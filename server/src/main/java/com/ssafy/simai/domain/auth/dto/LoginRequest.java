package com.ssafy.simai.domain.auth.dto;

import jakarta.validation.constraints.NotBlank;

/** username 은 app_user.login_id 다. 관제 앱 로그인 폼 이름을 따른다. */
public record LoginRequest(@NotBlank String username, @NotBlank String password) {
}
