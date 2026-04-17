import sqlite3, datetime

DB = r"C:\Users\Rahul\Pine Script v6 + Crypto Quant Trading\alpha-river-engine\alpha_river_snapshot_13_04_2026.db"
con = sqlite3.connect(DB)
con.row_factory = sqlite3.Row
cur = con.cursor()
C = "CLOSED"

def p(title): print(f"\n=== {title} ===")

p("VISAO GERAL")
cur.execute("""SELECT
    COUNT(*) AS total,
    SUM(CASE WHEN pnl_usdt > 0 THEN 1 ELSE 0 END) AS winners,
    SUM(CASE WHEN pnl_usdt <= 0 THEN 1 ELSE 0 END) AS losers,
    ROUND(100.0*SUM(CASE WHEN pnl_usdt>0 THEN 1 ELSE 0 END)/COUNT(*),2) AS wr,
    ROUND(SUM(pnl_usdt),4) AS total_pnl,
    ROUND(AVG(pnl_usdt),5) AS avg_pnl,
    ROUND(AVG(CASE WHEN pnl_usdt>0 THEN pnl_usdt END),5) AS avg_win,
    ROUND(AVG(CASE WHEN pnl_usdt<=0 THEN pnl_usdt END),5) AS avg_loss,
    ROUND(MAX(pnl_usdt),4) AS best,
    ROUND(MIN(pnl_usdt),4) AS worst,
    ROUND(AVG(candles_open),2) AS avg_c
FROM positions WHERE status=?""", (C,))
r = dict(cur.fetchone())
for k,v in r.items(): print(f"  {k}: {v}")
w = abs(r["avg_win"] or 0)
l = abs(r["avg_loss"] or 1)
winners = r["winners"] or 0
losers  = r["losers"] or 1
pf      = round((w * winners) / (l * losers), 4)
payoff  = round(w / l, 4)
wr_dec  = (r["wr"] or 0) / 100
ev      = round(wr_dec * w - (1 - wr_dec) * l, 5)
print(f"  profit_factor: {pf}")
print(f"  payoff_ratio:  {payoff}")
print(f"  expected_value_per_trade: {ev}")

p("CLOSE REASON")
cur.execute("""SELECT close_reason, COUNT(*) as n,
    ROUND(100.0*COUNT(*)/(SELECT COUNT(*) FROM positions WHERE status=?),1) as pct,
    ROUND(SUM(pnl_usdt),4) as pnl, ROUND(AVG(pnl_usdt),5) as avg,
    ROUND(100.0*SUM(CASE WHEN pnl_usdt>0 THEN 1 ELSE 0 END)/COUNT(*),1) as wr
FROM positions WHERE status=? GROUP BY close_reason ORDER BY n DESC""", (C, C))
for r in cur.fetchall():
    reason = str(r["close_reason"])
    print(f"  {reason:12s}: n={r['n']:5d} ({r['pct']:5.1f}%)  pnl={r['pnl']:9.3f}  avg={r['avg']:8.5f}  WR={r['wr']}%")

p("DURACAO (candles_open) PERCENTIS")
cur.execute("SELECT candles_open FROM positions WHERE status=? ORDER BY candles_open", (C,))
vals = [row[0] for row in cur.fetchall()]
n = len(vals)
for pct in [10, 25, 50, 75, 90, 95, 99]:
    idx = max(0, int(n * pct / 100) - 1)
    print(f"  P{pct}: {vals[idx]}")
cur.execute("""SELECT
    ROUND(AVG(CASE WHEN pnl_usdt>0 THEN candles_open END),2) as win_c,
    ROUND(AVG(CASE WHEN pnl_usdt<=0 THEN candles_open END),2) as loss_c
FROM positions WHERE status=?""", (C,))
r = dict(cur.fetchone())
print(f"  avg_winner_candles: {r['win_c']}")
print(f"  avg_loser_candles:  {r['loss_c']}")

p("POR TIER")
cur.execute("""SELECT tier, COUNT(*) as n,
    ROUND(SUM(pnl_usdt),4) as pnl, ROUND(AVG(pnl_usdt),5) as avg,
    ROUND(100.0*SUM(CASE WHEN pnl_usdt>0 THEN 1 ELSE 0 END)/COUNT(*),1) as wr
FROM positions WHERE status=? GROUP BY tier ORDER BY tier""", (C,))
for r in cur.fetchall():
    print(f"  Tier {r['tier']}: n={r['n']:4d}  pnl={r['pnl']:9.3f}  avg={r['avg']:8.5f}  WR={r['wr']}%")

p("POR OI REGIME")
cur.execute("""SELECT oi_regime, COUNT(*) as n,
    ROUND(SUM(pnl_usdt),4) as pnl, ROUND(AVG(pnl_usdt),5) as avg,
    ROUND(100.0*SUM(CASE WHEN pnl_usdt>0 THEN 1 ELSE 0 END)/COUNT(*),1) as wr
FROM positions WHERE status=? GROUP BY oi_regime ORDER BY n DESC""", (C,))
for r in cur.fetchall():
    regime = str(r["oi_regime"])
    print(f"  {regime:12s}: n={r['n']:4d}  pnl={r['pnl']:9.3f}  avg={r['avg']:8.5f}  WR={r['wr']}%")

p("TOP 10 SIMBOLOS (melhor PnL)")
cur.execute("""SELECT symbol, COUNT(*) as n, ROUND(SUM(pnl_usdt),4) as pnl,
    ROUND(AVG(pnl_usdt),5) as avg,
    ROUND(100.0*SUM(CASE WHEN pnl_usdt>0 THEN 1 ELSE 0 END)/COUNT(*),1) as wr
FROM positions WHERE status=? GROUP BY symbol ORDER BY pnl DESC LIMIT 10""", (C,))
for r in cur.fetchall():
    print(f"  {r['symbol']:16s}: n={r['n']:3d}  pnl={r['pnl']:8.3f}  avg={r['avg']:8.5f}  WR={r['wr']}%")

p("BOTTOM 10 SIMBOLOS (pior PnL)")
cur.execute("""SELECT symbol, COUNT(*) as n, ROUND(SUM(pnl_usdt),4) as pnl,
    ROUND(AVG(pnl_usdt),5) as avg,
    ROUND(100.0*SUM(CASE WHEN pnl_usdt>0 THEN 1 ELSE 0 END)/COUNT(*),1) as wr
FROM positions WHERE status=? GROUP BY symbol ORDER BY pnl ASC LIMIT 10""", (C,))
for r in cur.fetchall():
    print(f"  {r['symbol']:16s}: n={r['n']:3d}  pnl={r['pnl']:8.3f}  avg={r['avg']:8.5f}  WR={r['wr']}%")

p("PnL POR DIA")
cur.execute("""SELECT
    DATE(DATETIME(closed_at/1000, 'unixepoch')) as dia,
    COUNT(*) as n,
    ROUND(SUM(pnl_usdt),4) as pnl,
    ROUND(100.0*SUM(CASE WHEN pnl_usdt>0 THEN 1 ELSE 0 END)/COUNT(*),1) as wr
FROM positions WHERE status=? AND closed_at IS NOT NULL
GROUP BY dia ORDER BY dia""", (C,))
cum = 0.0
for r in cur.fetchall():
    cum += (r["pnl"] or 0)
    sign = "+" if (r["pnl"] or 0) >= 0 else ""
    print(f"  {r['dia']}  n={r['n']:4d}  {sign}{r['pnl']:7.3f}  WR={r['wr']:5.1f}%  cum={cum:9.3f}")

p("POSICOES ABERTAS")
cur.execute("SELECT COUNT(*) as n FROM positions WHERE status='OPEN'")
print(f"  Total abertas: {cur.fetchone()['n']}")

con.close()
print("\n[fim]")
