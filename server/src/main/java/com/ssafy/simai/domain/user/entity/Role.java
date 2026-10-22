package com.ssafy.simai.domain.user.entity;

/** 계정 역할(명세 14장): 관리자, 반장, 열람. app_user.role 의 CHECK 값과 같다. */
public enum Role {
    ADMIN,
    FOREMAN,
    VIEWER
}
