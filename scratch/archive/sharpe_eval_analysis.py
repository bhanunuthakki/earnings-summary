"""Portfolio Sharpe + absolute-return screen for the 25 evaluation candidates.

For each candidate computes:
  - annualized volatility (daily log returns, last ~2y)
  - correlation + beta to the equal-weight 11-name portfolio
  - trailing 12m total return (context)
  - analyst-consensus upside (fwd expected-return proxy) + dividend yield
  - asset Sharpe SR_i = (E[r_i]-rf)/sigma_i
  - marginal Sharpe score = SR_i - corr_ip * SR_p   ( >0 => improves portfolio Sharpe )
"""
from __future__ import annotations
import json, math, os
from datetime import date, timedelta

D = "data/historical/fmp"
RF = 0.042  # ~current US risk-free

PORT = ["AMZN","BN","GOOG","MELI","META","NOW","NU","NVO","RBRK","VEEV","WIX"]
EVAL = ["ABNB","BHP","BKNG","CDNS","CGEH","CRWV","DLO","FCX","FIGR","FRVO","LLY","MDB",
        "NBIS","NSP","NTDOY","NTRA","ORCL","SNOW","SNPS","SOFI","TEM","TMO","UBER","V","WGS"]

def load_prices(t):
    """return dict {date_str: adjClose}, most-recent-first source -> sorted ascending."""
    p = f"{D}/{t}_price_chart_10y_div_adj.json"
    if not os.path.exists(p): return {}
    raw = json.load(open(p))
    out = {}
    for r in raw:
        c = r.get("adjClose")
        if c: out[r["date"]] = float(c)
    return out

def load_json(path):
    return json.load(open(path)) if os.path.exists(path) else None

def div_yield(t):
    r = load_json(f"{D}/{t}_financial_ratios_ttm.json")
    if not r: return 0.0
    row = r[0] if isinstance(r, list) and r else (r if isinstance(r, dict) else {})
    for k in ("dividendYielTTM","dividendYieldTTM","dividendYieldPercentageTTM","dividendYield"):
        if k in row and row[k] is not None:
            v = float(row[k])
            return v/100 if v > 1.0 else v   # normalize pct vs ratio
    return 0.0

def analyst_upside(t, cur):
    r = load_json(f"{D}/{t}_price_target_consensus.json")
    if not r or cur is None: return None, None
    row = r[0] if isinstance(r, list) and r else r
    cons = row.get("targetConsensus") or row.get("targetMedian")
    med = row.get("targetMedian")
    up = (cons/cur - 1) if cons else None
    upmed = (med/cur - 1) if med else None
    return up, upmed

def fmp_dcf_upside(t, cur):
    for ep in ("_dcf_levered.json","_dcf_basic.json"):
        r = load_json(f"{D}/{t}{ep}")
        if not r: continue
        row = r[0] if isinstance(r, list) and r else r
        dcf = row.get("dcf") or row.get("equityValuePerShare") or row.get("Levered DCF")
        if dcf and cur:
            try: return float(dcf)/cur - 1
            except Exception: pass
    return None

# ---- build price panel ----
prices = {t: load_prices(t) for t in PORT+EVAL}
cur_price = {t: (prices[t][max(prices[t])] if prices[t] else None) for t in PORT+EVAL}

# common trading dates, restrict to last ~2y for regime relevance
all_dates = sorted(set().union(*[set(prices[t]) for t in PORT+EVAL if prices[t]]))
cutoff = (date.fromisoformat(all_dates[-1]) - timedelta(days=365*2)).isoformat()
win = [d for d in all_dates if d >= cutoff]

def ret_series(t, dates):
    """daily log returns over given date list (skips gaps)."""
    px = prices[t]
    ds = [d for d in dates if d in px]
    out = {}
    for i in range(1, len(ds)):
        p0, p1 = px[ds[i-1]], px[ds[i]]
        if p0 > 0 and p1 > 0:
            out[ds[i]] = math.log(p1/p0)
    return out

rets = {t: ret_series(t, win) for t in PORT+EVAL}

def ann_vol(series):
    v = list(series.values())
    if len(v) < 30: return None
    m = sum(v)/len(v)
    var = sum((x-m)**2 for x in v)/(len(v)-1)
    return math.sqrt(var)*math.sqrt(252)

def corr(a, b):
    common = sorted(set(a) & set(b))
    if len(common) < 30: return None, len(common)
    xa = [a[d] for d in common]; xb = [b[d] for d in common]
    ma, mb = sum(xa)/len(xa), sum(xb)/len(xb)
    cov = sum((xa[i]-ma)*(xb[i]-mb) for i in range(len(xa)))/(len(xa)-1)
    va = sum((x-ma)**2 for x in xa)/(len(xa)-1)
    vb = sum((x-mb)**2 for x in xb)/(len(xb)-1)
    if va<=0 or vb<=0: return None, len(common)
    return cov/math.sqrt(va*vb), len(common)

def trailing_return(t, days=365):
    px = prices[t]
    if not px: return None
    ds = sorted(px)
    last = ds[-1]
    target = (date.fromisoformat(last) - timedelta(days=days)).isoformat()
    past = [d for d in ds if d <= target]
    if not past: return None
    return px[last]/px[past[-1]] - 1

# ---- equal-weight portfolio daily return series ----
port_dates = sorted(set().union(*[set(rets[t]) for t in PORT]))
port_ret = {}
for d in port_dates:
    vals = [rets[t][d] for t in PORT if d in rets[t]]
    if len(vals) >= 6:   # need a quorum of holdings
        port_ret[d] = sum(vals)/len(vals)

port_vol = ann_vol(port_ret)
# portfolio expected return = equal-weight blend of holdings' analyst upside + div yield
port_er_parts = []
for t in PORT:
    up,_ = analyst_upside(t, cur_price[t])
    if up is not None:
        port_er_parts.append(up + div_yield(t))
port_er = sum(port_er_parts)/len(port_er_parts) if port_er_parts else None
port_sharpe = (port_er - RF)/port_vol if (port_er is not None and port_vol) else None

print(f"# Equal-weight 11-name portfolio")
print(f"  ann vol      : {port_vol:.1%}")
print(f"  E[r] (analyst+div, 12m): {port_er:.1%}")
print(f"  Sharpe (E[r]-rf)/vol   : {port_sharpe:.2f}")
print(f"  return window: {win[0]} -> {win[-1]}  ({len(win)} days)")
print()

# ---- per-candidate table ----
rows = []
for t in EVAL:
    cur = cur_price[t]
    vol = ann_vol(rets[t])
    c, n = corr(rets[t], port_ret)
    beta = None
    if c is not None and port_vol and vol:
        beta = c * (vol/port_vol)
    up, upmed = analyst_upside(t, cur)
    dy = div_yield(t)
    er = (up + dy) if up is not None else None
    sr_i = (er - RF)/vol if (er is not None and vol) else None
    marg = (sr_i - c*port_sharpe) if (sr_i is not None and c is not None and port_sharpe is not None) else None
    tr1y = trailing_return(t, 365)
    fdcf = fmp_dcf_upside(t, cur)
    rows.append(dict(t=t, cur=cur, vol=vol, corr=c, beta=beta, n=n, up=up, upmed=upmed,
                     dy=dy, er=er, sr_i=sr_i, marg=marg, tr1y=tr1y, fdcf=fdcf))

def f(x, fmt):
    return fmt.format(x) if x is not None else "  -- "

# sort by marginal Sharpe score desc
rows.sort(key=lambda r: (r["marg"] is not None, r["marg"] if r["marg"] is not None else -9))
rows.reverse()

hdr = f"{'TKR':6}{'price':>8}{'vol':>7}{'corr':>6}{'beta':>6}{'1yRet':>8}{'analyst↑':>9}{'medTgt↑':>9}{'divY':>6}{'E[r]':>7}{'SR_i':>6}{'margSharpe':>11}{'fmpDCF↑':>9}{'days':>5}"
print(hdr); print("-"*len(hdr))
for r in rows:
    print(f"{r['t']:6}{f(r['cur'],'{:>8.1f}')}{f(r['vol'],'{:>7.0%}')}{f(r['corr'],'{:>6.2f}')}"
          f"{f(r['beta'],'{:>6.2f}')}{f(r['tr1y'],'{:>8.0%}')}{f(r['up'],'{:>9.0%}')}{f(r['upmed'],'{:>9.0%}')}"
          f"{f(r['dy'],'{:>6.1%}')}{f(r['er'],'{:>7.0%}')}{f(r['sr_i'],'{:>6.2f}')}{f(r['marg'],'{:>11.2f}')}"
          f"{f(r['fdcf'],'{:>9.0%}')}{r['n']:>5}")

# correlation of each candidate to EACH holding (to see what it diversifies)
print("\n# Avg correlation to portfolio holdings + min/max pair")
for r in sorted(rows, key=lambda x: (x['corr'] is None, x['corr'] if x['corr'] is not None else 9)):
    t = r['t']
    cs = []
    for h in PORT:
        cc,_ = corr(rets[t], rets[h])
        if cc is not None: cs.append((cc,h))
    if cs:
        cs.sort()
        avg = sum(c for c,_ in cs)/len(cs)
        print(f"  {t:6} avg={avg:+.2f}  min={cs[0][0]:+.2f}({cs[0][1]})  max={cs[-1][0]:+.2f}({cs[-1][1]})")
