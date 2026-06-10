"""Forward growth + quality + valuation snapshot for eval contenders."""
import json, os
from datetime import date
D="data/historical/fmp"
SHORT=["TMO","LLY","BKNG","UBER","V","DLO","ABNB","FCX","BHP","ORCL","SNOW","MDB","NSP","NTRA","SOFI","CDNS","SNPS","TEM"]

def lj(f):
    p=f"{D}/{f}"
    return json.load(open(p)) if os.path.exists(p) else None

def fwd_cagr(t, field):
    """CAGR of avg estimate from the nearest future FY to ~3y out."""
    r=lj(f"{t}_analyst_estimates_annual.json")
    if not r: return None
    rows=[x for x in r if x.get("date")]
    rows.sort(key=lambda x:x["date"])
    today=date.today().isoformat()
    fut=[x for x in rows if x["date"]>=today]
    if len(fut)<2:
        # fall back to last historical + future
        fut=rows[-4:]
    pts=[(x["date"], x.get(field)) for x in fut if x.get(field)]
    pts=[(d,v) for d,v in pts if v and v>0]
    if len(pts)<2: return None
    d0,v0=pts[0]; d1,v1=pts[-1]
    yrs=(date.fromisoformat(d1).year-date.fromisoformat(d0).year)
    if yrs<=0: return None
    return (v1/v0)**(1/yrs)-1

def ratio(t,k):
    r=lj(f"{t}_financial_ratios_ttm.json")
    if not r: return None
    row=r[0] if isinstance(r,list) else r
    return row.get(k)

def scores(t):
    r=lj(f"{t}_financial_scores.json")
    if not r: return (None,None)
    row=r[0] if isinstance(r,list) else r
    return row.get("piotroskiScore"), row.get("altmanZScore")

print(f"{'TKR':6}{'fwdRevCAGR':>11}{'fwdEpsCAGR':>11}{'opMgn':>7}{'netMgn':>7}{'P/E':>7}{'fPEG':>6}{'P/FCF':>7}{'D/E':>6}{'Piotr':>6}{'AltZ':>6}")
print("-"*86)
def f(x,fmt): return fmt.format(x) if x is not None else "  -- "
for t in SHORT:
    rg=fwd_cagr(t,"revenueAvg"); eg=fwd_cagr(t,"epsAvg")
    opm=ratio(t,"operatingProfitMarginTTM"); nm=ratio(t,"netProfitMarginTTM")
    pe=ratio(t,"priceToEarningsRatioTTM"); peg=ratio(t,"forwardPriceToEarningsGrowthRatioTTM")
    pfcf=ratio(t,"priceToFreeCashFlowRatioTTM"); de=ratio(t,"debtToEquityRatioTTM")
    pio,alt=scores(t)
    print(f"{t:6}{f(rg,'{:>11.0%}')}{f(eg,'{:>11.0%}')}{f(opm,'{:>7.0%}')}{f(nm,'{:>7.0%}')}"
          f"{f(pe,'{:>7.1f}')}{f(peg,'{:>6.1f}')}{f(pfcf,'{:>7.1f}')}{f(de,'{:>6.1f}')}{f(pio,'{:>6.0f}')}{f(alt,'{:>6.1f}')}")
