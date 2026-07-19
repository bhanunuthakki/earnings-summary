"""Fold the NVO patent-cliff / what's-priced-in / trap-vs-turn signposts into
the thesis JSON. Idempotent-ish: safe to run once on the backed-up original."""
import json, pathlib

P = pathlib.Path(r"C:\Users\Bhanu\.gemini\antigravity\scratch\earnings-summary\micro_thesis\holdings\NVO.json")
t = json.loads(P.read_text(encoding="utf-8"))

t["last_updated"] = "2026-06-08"

# 1) Thesis statement — append the "what's priced in" / bifurcated-cliff / role framing
priced_in = (
    " WHAT'S PRICED IN (2026-06): at ~10x and -40-50% off the $81 high with FY26 guided to an "
    "outright sales+profit DECLINE, NVO is priced as a challenged loser - so much of the patent + "
    "competition fear is already discounted (a CONTRARIAN value setup, not a momentum name). The "
    "patent cliff is BIFURCATED and NOT the near-term risk: the high-margin US pool is protected to "
    "~2031 patent / ~2032 practical generics (EU ~2031-33), while the 2026 EM LOEs (CN/IN/BR/CA) are "
    "low-margin with a slow generic ramp. The live, less-priced variable is the COMPETITIVE trajectory "
    "vs Lilly - and the bar is low (NVO need only stop bleeding US share). Trap-vs-turn turns on the "
    "2027 guide returning to growth + US share stabilization + the CagriSema/amycretin pipeline. ROLE: "
    "the cheap, asymmetric leg of a long-GLP-1 book - hold/don't capitulate at the trough; diversify "
    "the THEME (Roche/AZN/Amgen for pipeline breadth, or LLY on a pullback) rather than double the "
    "obesity bet."
)
if "WHAT'S PRICED IN" not in t["thesis"]:
    t["thesis"] = t["thesis"].rstrip() + priced_in

# 2) Key driver — name the swing
t["key_driver"] = (
    "Whether 2026 is the price-driven trough: the 2027 CER guide returning to growth + US share "
    "stabilization vs Lilly + volume (oral Wegovy / amycretin) clearing the falling net-price curve"
)

kpis = t["tier_1_kpis"]

# 3) Refine the patent entry to encode the bifurcation
for k in kpis:
    if k.get("name", "").startswith("Patent extension wins"):
        k["name"] = "Patent cliff — BIFURCATED (US/EU distant & high-margin vs 2026 EM LOE low-margin)"
        k["current"] = (
            "High-margin US protected to ~2031 patent / ~2032 practical generics; EU ~2031-33. "
            "2026 LOEs (CN/IN/BR/CA) are LOW-margin + slow ramp (no China generics ~1mo post-expiry). "
            "Material US cliff is ~6yrs out and largely priced at ~10x."
        )
        k["break_condition"] = (
            "A 2026 EM LOE is immaterial (low-margin); the real watch is a US/EU challenge that "
            "PULLS FORWARD the ~2031-32 cliff, OR EM erosion proving faster/larger than the low-margin assumption"
        )
        k["status"] = "Green-ish (mostly priced; distant)"

# 4) Refine CagriSema entry with current milestones
for k in kpis:
    if k.get("name", "").startswith("CagriSema regulatory"):
        k["current"] = (
            "NDA filed; PDUFA ~Oct 2026. REDEFINE 1 weight loss 22.7%; head-to-head vs Zepbound "
            "20.4% vs ~24% (missed clean superiority). Higher-dose Ph3 H2'26; REDEFINE-11 data H1'27."
        )

# 5) Insert the 2027-guide signpost as the SECOND tier-1 (high in the anchor)
guide_2027 = {
    "name": "2027 CER guide — return to growth (THE trap-vs-turn signpost)",
    "current": "FY26 guided to DECLINE -4% to -12% CER (a 'reset year'); the 2027 outlook is the key tell on whether the trough is single-cycle pricing or a structural slide",
    "prior": None, "yoy": None, "status": "Yellow",
    "break_condition": "2027 guided to another decline / fails to return to growth -> confirms a structural slide (value trap), not a single-cycle reset",
    "source": "FY26 results + 2027 guidance",
}
if not any("2027 CER guide" in k.get("name", "") for k in kpis):
    kpis.insert(1, guide_2027)

# 6) Append the amycretin/AMAZE next-gen signpost
amycretin = {
    "name": "Amycretin / zenagamtide Phase 3 (AMAZE) — next-gen GLP-1/amylin",
    "current": "SC + oral amycretin (rebranded zenagamtide) entered Phase 3 AMAZE Q1'26; the long-term franchise-defense asset vs Lilly retatrutide/orforglipron",
    "prior": None, "yoy": None, "status": "Yellow",
    "break_condition": "AMAZE delays, or efficacy/tolerability disappoints vs Lilly's next-gen -> erodes the pipeline pillar of the hold",
    "source": "Novo pipeline updates + ADA / clinical readouts",
}
if not any("AMAZE" in k.get("name", "") for k in kpis):
    kpis.append(amycretin)

# 7) Enrich competitive watchlist with current pipeline status
t["competitive_watchlist"] = [
    "Eli Lilly — the share winner: Foundayo (oral orforglipron) approved Apr'26; retatrutide TRIUMPH-1 28.3%; Zepbound outgrowing Wegovy",
    "Roche — deepest non-leader pipeline: CT-388/enicepatide dual GIP/GLP-1 (Ph2 22.5% placebo-adj @48wk) + petrelintide (amylin, via Zealand)",
    "AstraZeneca — elecoglipron (oral GLP-1, Ph3 H2'26) + AZD6234 (amylin) + AZD9550; CSPC deal",
    "Pfizer — acquired Metsera ($4.9B, beat Novo): MET-097i monthly GLP-1 + MET-233i amylin + orals",
    "Amgen — MariTide (monthly GLP-1/GIP, Ph3 MARITIME ongoing; Ph2 ~16-20% but GI tolerability/discontinuation risk)",
]

# 8) Add the durable valuation / signposts record to nuance
t["nuance"]["valuation_whats_priced_in"] = (
    "At ~$43 / ~10x earnings, down ~40% over 12mo (~50% from the $81 high) with FY26 guided to an "
    "outright sales+profit DECLINE (-4% to -12% CER), NVO is priced as a challenged loser, not a "
    "compounder - so much of the patent + competition fear is already discounted (target dispersion "
    "$40-175 = genuine debate; Goldman $41 bear vs Cantor $160 bull). Patent cliff is bifurcated: the "
    "material US pool is protected to ~2031 patent / ~2032 practical generics (EU ~2031-33), so at 10x "
    "you barely pay for post-2031 - the cliff is well-discounted; the 2026 EM LOEs (CN/IN/BR/CA) are "
    "low-margin with a slow generic ramp. The genuinely-unresolved variable is the competitive / "
    "deceleration trajectory: is 2026 THE trough (stabilize -> re-rate) or the start of a structural "
    "slide (value trap)? The bar is LOW - NVO need only stop bleeding US share. PORTFOLIO ROLE: the "
    "cheap, asymmetric leg of a long-GLP-1-theme book - hold / don't capitulate to chase Lilly at its "
    "high; diversify the THEME (Roche/AZN/Amgen for pipeline breadth, or LLY on a pullback) rather "
    "than concentrate the obesity bet."
)
t["nuance"]["signposts_trap_vs_turn"] = [
    "2027 CER guide returns to growth (the single most important tell)",
    "US Rx-share stabilizing vs Lilly (stop the bleed)",
    "CagriSema PDUFA ~Oct'26 + launch trajectory",
    "Oral Wegovy/Ozempic US ramp (oral Ozempic T2D cleared; 25mg decision end-2026)",
    "Amycretin/zenagamtide AMAZE Phase 3 progress",
    "Realized net-price / margin trend under IRA (~70% off list 2027) + MFN",
]

P.write_text(json.dumps(t, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")

# Validate + estimate the thesis-anchor size (cap is 3500)
reloaded = json.loads(P.read_text(encoding="utf-8"))
anchor_est = len(reloaded["thesis"]) + len(reloaded["key_driver"]) + sum(
    len(k.get("name","")) + len(k.get("break_condition","")) + 25 for k in reloaded["tier_1_kpis"]
) + sum(len(r.get("narrative","")) for r in reloaded.get("business_model_rules", [])) + 200
print("OK — valid JSON written.")
print(f"thesis chars: {len(reloaded['thesis'])}")
print(f"tier_1_kpis: {len(reloaded['tier_1_kpis'])}")
print(f"approx thesis-anchor chars: {anchor_est} (cap 3500 -> {'TRUNCATES tail' if anchor_est>3500 else 'fits'})")
