"""runs/*/result.json 을 표로 모은다: python summarize.py > table.md"""
import json

import common as C

rows = []
for f in sorted((C.WORK / "runs").glob("*/result.json")):
    r = json.loads(f.read_text(encoding="utf-8"))
    t, lm = r["times"], r.get("loo_mean") or {}
    rows.append((r["name"], r["faces"], t.get("data_costs_s"), t.get("texrecon_select_s"), t.get("texrecon_final_s"), t.get("bake_total_s"),
                 round((t.get("data_costs_s") or 0) + (t.get("bake_total_s") or 0), 1), lm.get("mae_covered"), lm.get("mae_valid"), lm.get("coverage"),
                 r["ratios_area"]["OBSERVED"], r["ratios_area"]["UNOBSERVED"], round(r["glb_bytes"] / 1e6, 1)))
print("| 설정 | 면 수 | 데이터 비용(s) | 시점 선택(s) | 최종 굽기(s) | 굽기 합(s) | texrecon 합(s) | LOO MAE covered | LOO MAE valid | LOO 덮임 | 관측 면적 비율 | 미관측 | GLB MB |")
print("|---|---|---|---|---|---|---|---|---|---|---|---|---|")
for r in rows:
    print("| " + " | ".join("-" if x is None else str(x) for x in r) + " |")
