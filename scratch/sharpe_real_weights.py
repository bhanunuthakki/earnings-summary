"""Marginal-Sharpe screen of the 25 eval candidates vs the REAL portfolio.

Portfolio weights + holding prices come from the portfolio-tracker DB
(holdings_snapshots + prices). Candidate prices come from the earnings-summary
10y div-adjusted cache. Correlations align on common dates.
"""
from __future__ import annotations
import sqlite3, json, math, os
from datetime import date, timedelta
from collections import defaultdict

TRK = r"C:\Users\Bhanu\.gemini\antigravity\scratch\portfolio-tracker\portfolio.db"
CACHE = r"C:\Users\Bhanu\.gemini\antigravity\scratch\earnings-summary\data\historical\fmp"
RF = 0.042

EVAL = ["ABNB","BHP","BKNG","CDNS","CGEH","CRWV","DLO","FCX","FIGR","FRVO","LLY","MDB",
        "NBIS","NSP","NTDOY","NTRA","ORCL","SNOW","SNPS","SOFI","TEM","TMO","UBER","V","WGS"]

# expected-return assumptions for non-single-stock sleeves (long-run, annual)
ETF_ER = {"VTI":0.075,"SPY":0.075,"VOO":0.075,"FLKR":0.09,"SGOV":0.042}

# ---------- real weights ----------
conn = sqlite3.connect(TRK); c = conn.cursor()
latest = c.execute("SELECT MAX(snapshot_date) FROM holdings_snapshots").fetchone()[0]
rows = c.execute("""SELECT s.ticker, s.is_cash_equivalent, SUM(h.institution_value)
    FROM holdings_snapshots h JOIN securities s ON h.security_id=s.security_id
    WHERE h.snapshot_date=? GROUP BY s.security_id""", (latest,)).fetchall()
val = defaultdict(float)
for tk, cash, v in rows:
    if v: val[tk or "CASH"] += v
total = sum(val.values())
weights = {t: v/total for t, v in val.items()}

# ---------- holding price series from tracker ----------
def trk_series(ticker):
    q = """SELECT p.date, p.close FROM prices p JOIN securities s ON p.security_id=s.security_id
           WHERE s.ticker=? ORDER BY p.date"""
    out = {}
    for d, cl in c.execute(q, (ticker,)).fetchall():
        if cl: out[d] = float(cl)
    return out

HOLD = [t for t in weights if t not in ("CASH",) and weights[t] > 0.0005]
hold_px = {t: trk_series(t) for t in HOLD}
hold_px = {t: s for t, s in hold_px.items() if len(s) > 50}   # drop money-market stubs (FDRXX etc.)

def to_ret(px):
    ds = sorted(px); out = {}
    for i in range(1, len(ds)):
        a, b = px[ds[i-1]], px[ds[i]]
        if a > 0 and b > 0: out[ds[i]] = math.log(b/a)
    return out

hold_ret = {t: to_ret(s) for t, s in hold_px.items()}
# renormalize weights over the holdings we actually have a price series for
wsum = sum(weights[t] for t in hold_ret)
W = {t: weights[t]/wsum for t in hold_ret}

# ---------- portfolio daily return (current weights held constant) ----------
all_days = sorted(set().union(*[set(r) for r in hold_ret.values()]))
port_ret = {}
for d in all_days:
    num = 0.0; wd = 0.0
    for t in hold_ret:
        if d in hold_ret[t]:
            num += W[t]*hold_ret[t][d]; wd += W[t]
    if wd > 0.80:                       # require >=80% of book to have priced that day
        port_ret[d] = num/wd

def ann_vol(series):
    v = list(series.values())
    if len(v) < 30: return None
    m = sum(v)/len(v); var = sum((x-m)**2 for x in v)/(len(v)-1)
    return math.sqrt(var)*math.sqrt(252)

def corr(a, b):
    common = sorted(set(a) & set(b))
    if len(common) < 60: return None, len(common)
    xa=[a[d] for d in common]; xb=[b[d] for d in common]
    ma=sum(xa)/len(xa); mb=sum(xb)/len(xb)
    cov=sum((xa[i]-ma)*(xb[i]-mb) for i in range(len(xa)))/(len(xa)-1)
    va=sum((x-ma)**2 for x in xa)/(len(xa)-1); vb=sum((x-mb)**2 for x in xb)/(len(xb)-1)
    if va<=0 or vb<=0: return None, len(common)
    return cov/math.sqrt(va*vb), len(common)

port_vol = ann_vol(port_ret)

# ---------- portfolio expected return (blend) ----------
def cache(t, f):
    p=f"{CACHE}/{t}_{f}"; return json.load(open(p)) if os.path.exists(p) else None
def analyst_up(t, cur):
    r=cache(t,"price_target_consensus.json")
    if not r or not cur: return None
    row=r[0] if isinstance(r,list) and r else r
    cons=row.get("targetConsensus") or row.get("targetMedian")
    return (cons/cur-1) if cons else None
def divy(t):
    r=cache(t,"financial_ratios_ttm.json")
    if not r: return 0.0
    row=r[0] if isinstance(r,list) and r else r
    for k in ("dividendYieldTTM","dividendYielTTM","dividendYield"):
        if row.get(k) is not None:
            v=float(row[k]); return v/100 if v>1 else v
    return 0.0
def cur_price_cache(t):
    r=cache(t,"price_chart_10y_div_adj.json")
    return float(r[0]["adjClose"]) if r else None

# expected return per holding (single stocks: analyst upside+div, capped; sleeves: assumption)
er_parts=[]
for t in hold_ret:
    if t in ETF_ER:
        er=ETF_ER[t]
    else:
        cur=hold_px[t][max(hold_px[t])]
        up=analyst_up(t, cur)
        er = (up + divy(t)) if up is not None else 0.10
        er = min(er, 0.45)            # cap absurd analyst upside
    er_parts.append(W[t]*er)
port_er = sum(er_parts)
port_sharpe = (port_er-RF)/port_vol

print(f"# REAL portfolio  (snapshot {latest}, ${total:,.0f})")
print(f"  holdings used : {', '.join(f'{t} {W[t]*100:.1f}%' for t in sorted(W, key=lambda x:-W[x]))}")
print(f"  ann vol       : {port_vol:.1%}")
print(f"  E[r] (blend)  : {port_er:.1%}   Sharpe: {port_sharpe:.2f}")
print(f"  window        : {min(port_ret)} -> {max(port_ret)} ({len(port_ret)} days)\n")

# ---------- candidate screen ----------
def cand_ret(t):
    r=cache(t,"price_chart_10y_div_adj.json")
    if not r: return {}
    px={x["date"]:float(x["adjClose"]) for x in r if x.get("adjClose")}
    # restrict to portfolio window
    lo=min(port_ret)
    px={d:v for d,v in px.items() if d>=lo}
    return to_ret(px)

rows_out=[]
for t in EVAL:
    cr=cand_ret(t)
    vol=ann_vol(cr)
    cc,n=corr(cr, port_ret)
    beta = cc*(vol/port_vol) if (cc is not None and vol and port_vol) else None
    cur=cur_price_cache(t)
    up=analyst_up(t,cur); dy=divy(t)
    er=(up+dy) if up is not None else None
    sr_i=(er-RF)/vol if (er is not None and vol) else None
    marg=(sr_i-cc*port_sharpe) if (sr_i is not None and cc is not None) else None
    # marginal risk contribution at small add (proportional to corr*vol)
    mrisk = cc*vol if (cc is not None and vol) else None
    rows_out.append(dict(t=t,vol=vol,cc=cc,beta=beta,up=up,er=er,sr=sr_i,marg=marg,mrisk=mrisk,n=n))

rows_out.sort(key=lambda r:(r["marg"] is not None, r["marg"] if r["marg"] is not None else -9), reverse=True)
def f(x,fmt): return fmt.format(x) if x is not None else "  -- "
hdr=f"{'TKR':6}{'vol':>7}{'corr→port':>10}{'beta':>6}{'analyst↑':>9}{'E[r]':>7}{'SR_i':>6}{'margSharpe':>11}{'days':>6}"
print(hdr); print('-'*len(hdr))
for r in rows_out:
    print(f"{r['t']:6}{f(r['vol'],'{:>7.0%}')}{f(r['cc'],'{:>10.2f}')}{f(r['beta'],'{:>6.2f}')}"
          f"{f(r['up'],'{:>9.0%}')}{f(r['er'],'{:>7.0%}')}{f(r['sr'],'{:>6.2f}')}{f(r['marg'],'{:>11.2f}')}{r['n']:>6}")

# per-holding correlation for the 5 finalists (what each diversifies)
print("\n# Finalist correlation to each MAJOR holding (weight-sorted)")
majors=[t for t in sorted(W,key=lambda x:-W[x]) if W[t]>=0.02]
for t in ["TMO","BKNG","LLY","UBER","V","FCX","DLO"]:
    cr=cand_ret(t)
    cs=[]
    for h in majors:
        cc,_=corr(cr, hold_ret[h])
        if cc is not None: cs.append(f"{h}({W[h]*100:.0f}%):{cc:+.2f}")
    print(f"  {t:5} -> "+"  ".join(cs))
