package com.ssafy.simai.domain.user.entity;

import jakarta.persistence.Column;
import jakarta.persistence.Entity;
import jakarta.persistence.EnumType;
import jakarta.persistence.Enumerated;
import jakarta.persistence.GeneratedValue;
import jakarta.persistence.GenerationType;
import jakarta.persistence.Id;
import jakarta.persistence.Table;
import java.time.Instant;
import lombok.AccessLevel;
import lombok.Getter;
import lombok.NoArgsConstructor;

/** 관제 화면·PWA 계정(app_user). 지우지 않고 disabled_at 으로 막는다. */
@Entity
@Table(name = "app_user")
@Getter
@NoArgsConstructor(access = AccessLevel.PROTECTED)
public class AppUser {

    @Id
    @GeneratedValue(strategy = GenerationType.IDENTITY)
    @Column(name = "user_id")
    private Long id;

    @Column(name = "login_id", nullable = false, unique = true)
    private String loginId;

    @Column(name = "password_hash", nullable = false)
    private String passwordHash;

    @Enumerated(EnumType.STRING)
    @Column(nullable = false)
    private Role role;

    @Column(name = "display_label")
    private String displayLabel;

    @Column(name = "created_at", nullable = false)
    private Instant createdAt;

    @Column(name = "disabled_at")
    private Instant disabledAt;

    public AppUser(String loginId, String passwordHash, Role role, String displayLabel, Instant createdAt) {
        this.loginId = loginId;
        this.passwordHash = passwordHash;
        this.role = role;
        this.displayLabel = displayLabel;
        this.createdAt = createdAt;
    }

    public boolean isDisabled() {
        return disabledAt != null;
    }

    public void changePassword(String passwordHash) {
        this.passwordHash = passwordHash;
    }

    public void disable(Instant at) {
        this.disabledAt = at;
    }
}
