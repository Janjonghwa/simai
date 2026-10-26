package com.ssafy.simai.domain.config;

import java.security.MessageDigest;
import java.security.NoSuchAlgorithmException;
import java.util.HexFormat;
import java.util.List;

/** 구성 묶음 파일 이름과 해시 형식(계약 sha256: "sha256:<hex 64>") */
public final class ConfigFiles {

    public static final String MANIFEST = "manifest";
    /** 계약 config_bundle.payload.components 의 키 */
    public static final List<String> COMPONENTS = List.of("zones", "rules", "calibration", "operational_geometry");

    private ConfigFiles() {
    }

    public static boolean known(String name) {
        return MANIFEST.equals(name) || COMPONENTS.contains(name);
    }

    public static String sha256(byte[] content) {
        try {
            return "sha256:" + HexFormat.of().formatHex(MessageDigest.getInstance("SHA-256").digest(content));
        } catch (NoSuchAlgorithmException e) {
            throw new IllegalStateException(e);
        }
    }
}
