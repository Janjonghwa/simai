package com.ssafy.simai.domain.machine;

/**
 * 사람 계정이 아닌 호출 주체(명세 14). 토큰은 주체마다 따로 발급하고 원문은 발급 때 한 번만 보여 준다(DB 에는 SHA-256 만).
 * DEVICE: 현장 노트북·워치(device, /api/devices/**·GET /api/endpoints/tier0). WORKER: RunPod 분석 워커(worker, /api/worker/**).
 * Tier 0 토큰은 사이트에 하나라 환경 변수 해시로 둔다(S15P31A101-103, Tier0Properties).
 */
public enum MachineKind {
    DEVICE("device", "device_id", "ROLE_DEVICE"),
    WORKER("worker", "worker_id", "ROLE_WORKER");

    private final String table;
    private final String idColumn;
    private final String authority;

    MachineKind(String table, String idColumn, String authority) {
        this.table = table;
        this.idColumn = idColumn;
        this.authority = authority;
    }

    String table() {
        return table;
    }

    String idColumn() {
        return idColumn;
    }

    public String authority() {
        return authority;
    }
}
