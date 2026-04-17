#!/usr/bin/env python3
"""
diagnostic.py — Alpha River Engine: Script de Diagnóstico Automatizado
=======================================================================
AF Parte XXVII, seção 27.6.2 — P65

Uso:
    python diagnostic.py <snapshot.db>
    python diagnostic.py <snapshot.db> --json   # saída JSON para rastreamento temporal
    python diagnostic.py <snapshot.db> --prev <previous.db>  # comparação com snapshot anterior

Módulos executados:
    1. Health Check     — schema, INCs, contagem, período
    2. Edge Detection   — PnL bruto vs. líquido, PF, corr(score, PnL)
    3. Regime Decomp.   — trail%/PnL por hora, detecção de regime favorável
    4. Entry Quality    — MFE, distribuição de retorno máximo (requer peak_price)
    5. Symbol Attribution — top/bottom 10, concentração, outliers
"""

import argparse
import json
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path


# ══════════════════════════════════════════════════════════════════════════════
# Utilitários
# ══════════════════════════════════════════════════════════════════════════════

def connect(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    return conn


def fmt_pnl(v: float | None) -> str:
    if v is None:
        return "N/A"
    sign = "+" if v >= 0 else ""
    return f"{sign}${v:,.2f}"


def fmt_pct(v: float | None) -> str:
    if v is None:
        return "N/A"
    sign = "+" if v >= 0 else ""
    return f"{sign}{v:.1f}%"


def col_exists(conn: sqlite3.Connection, col: str) -> bool:
    cur = conn.execute("PRAGMA table_info(positions)")
    return any(row["name"] == col for row in cur.fetchall())


def has_v37(conn: sqlite3.Connection) -> bool:
    """Verifica se o schema v3.7 (instrumentação) está presente."""
    return col_exists(conn, "gross_pnl")


# ══════════════════════════════════════════════════════════════════════════════
# Módulo 1 — Health Check
# ══════════════════════════════════════════════════════════════════════════════

def module_1_health(conn: sqlite3.Connection) -> dict:
    out = {}

    # Schema version
    out["schema_v37"] = has_v37(conn)
    out["schema_peak_price"] = col_exists(conn, "peak_price")
    out["schema_entry_path"] = col_exists(conn, "entry_path")

    # Status distribution
    cur = conn.execute("SELECT status, COUNT(*) FROM positions GROUP BY status")
    status = {r[0]: r[1] for r in cur.fetchall()}
    out["closed"] = status.get("CLOSED", 0)
    out["open"]   = status.get("OPEN", 0)

    # Período
    cur = conn.execute("SELECT MIN(opened_at), MAX(closed_at) FROM positions WHERE status='CLOSED' AND closed_at IS NOT NULL")
    ts_min, ts_max = cur.fetchone()
    if ts_min and ts_max:
        t0 = datetime.fromtimestamp(ts_min / 1000, tz=timezone.utc)
        t1 = datetime.fromtimestamp(ts_max / 1000, tz=timezone.utc)
        span_h = (ts_max - ts_min) / 1000 / 3600
        out["period_start"] = t0.strftime("%Y-%m-%d %H:%M UTC")
        out["period_end"]   = t1.strftime("%Y-%m-%d %H:%M UTC")
        out["span_hours"]   = round(span_h, 1)
        out["trades_per_hour"] = round(out["closed"] / max(span_h, 0.001), 1)
    else:
        out["period_start"] = out["period_end"] = "N/A"
        out["span_hours"] = out["trades_per_hour"] = 0

    # INC verifications
    cur = conn.execute("SELECT COUNT(*) FROM positions WHERE close_reason='SL' AND breakeven_active=1")
    out["inc2_sl_with_be"] = cur.fetchone()[0]

    cur = conn.execute("SELECT COUNT(*) FROM positions WHERE close_reason='BREAKEVEN' AND (breakeven_active=0 OR breakeven_active IS NULL)")
    out["inc3_be_without_flag"] = cur.fetchone()[0]

    cur = conn.execute("SELECT COUNT(*) FROM positions WHERE status='CLOSED' AND candles_open > 8")
    out["inc5_over_mh8"] = cur.fetchone()[0]

    cur = conn.execute("SELECT MAX(cvd_neg_streak) FROM positions")
    out["inc6_max_cvd_streak"] = cur.fetchone()[0] or 0

    # Scores negativos (bypass sem registro)
    cur = conn.execute("SELECT COUNT(*) FROM positions WHERE score < 0")
    out["negative_scores"] = cur.fetchone()[0]

    cur = conn.execute("SELECT COUNT(*) FROM positions WHERE score < 0.40")
    out["below_threshold"] = cur.fetchone()[0]

    return out


# ══════════════════════════════════════════════════════════════════════════════
# Módulo 2 — Edge Detection
# ══════════════════════════════════════════════════════════════════════════════

def module_2_edge(conn: sqlite3.Connection) -> dict:
    out = {}
    v37 = has_v37(conn)

    # PnL total líquido
    cur = conn.execute("SELECT SUM(pnl_usdt), COUNT(*), AVG(pnl_usdt) FROM positions WHERE status='CLOSED' AND pnl_usdt IS NOT NULL")
    net_sum, n, net_avg = cur.fetchone()
    out["net_pnl"] = round(net_sum or 0, 2)
    out["n_trades"] = n or 0
    out["net_avg_per_trade"] = round(net_avg or 0, 4)

    # PnL bruto (se v3.7) ou estimado
    if v37:
        cur = conn.execute("SELECT SUM(gross_pnl), SUM(commission) FROM positions WHERE status='CLOSED' AND gross_pnl IS NOT NULL")
        gross_sum, comm_sum = cur.fetchone()
        out["gross_pnl"] = round(gross_sum or 0, 2)
        out["commission_total"] = round(comm_sum or 0, 2)
        out["v37_instrumented"] = True
    else:
        # Fallback: estimar com commission_factor=0.0008
        cur = conn.execute("SELECT SUM(pnl_usdt + entry_price * qty * 0.0008), SUM(entry_price * qty * 0.0008) FROM positions WHERE status='CLOSED' AND pnl_usdt IS NOT NULL")
        gross_est, comm_est = cur.fetchone()
        out["gross_pnl"] = round(gross_est or 0, 2)
        out["commission_total"] = round(comm_est or 0, 2)
        out["v37_instrumented"] = False

    # Profit Factor líquido
    cur = conn.execute("SELECT COALESCE(SUM(pnl_usdt),0) FROM positions WHERE status='CLOSED' AND pnl_usdt > 0")
    gains = cur.fetchone()[0]
    cur = conn.execute("SELECT COALESCE(SUM(pnl_usdt),0) FROM positions WHERE status='CLOSED' AND pnl_usdt < 0")
    losses = cur.fetchone()[0]
    out["pf_net"] = round(gains / abs(losses), 4) if losses else None
    out["gains"] = round(gains, 2)
    out["losses"] = round(losses, 2)

    # Profit Factor bruto
    if v37:
        cur = conn.execute("SELECT COALESCE(SUM(gross_pnl),0) FROM positions WHERE status='CLOSED' AND gross_pnl > 0")
        gg = cur.fetchone()[0]
        cur = conn.execute("SELECT COALESCE(SUM(gross_pnl),0) FROM positions WHERE status='CLOSED' AND gross_pnl < 0")
        gl = cur.fetchone()[0]
    else:
        gg = gains + (out["commission_total"] * gains / max(abs(gains) + abs(losses), 1))
        gl = losses + (out["commission_total"] * losses / max(abs(gains) + abs(losses), 1))
    out["pf_gross"] = round(gg / abs(gl), 4) if gl else None

    # Win rate
    cur = conn.execute("SELECT COUNT(*) FROM positions WHERE status='CLOSED' AND pnl_usdt > 0")
    out["wins"] = cur.fetchone()[0]
    out["win_rate"] = round(out["wins"] / max(out["n_trades"], 1) * 100, 1)

    # Close reason distribution
    cur = conn.execute("""
        SELECT close_reason, COUNT(*) as n,
               ROUND(SUM(pnl_usdt),2) as sum_pnl,
               ROUND(CAST(COUNT(*) AS REAL) / ? * 100, 1) as pct
        FROM positions WHERE status='CLOSED'
        GROUP BY close_reason ORDER BY n DESC
    """, (max(out["n_trades"], 1),))
    out["close_reasons"] = [dict(r) for r in cur.fetchall()]

    # Correlação score vs PnL (Pearson)
    cur = conn.execute("SELECT score, pnl_usdt FROM positions WHERE status='CLOSED' AND score IS NOT NULL AND pnl_usdt IS NOT NULL")
    rows = cur.fetchall()
    if len(rows) >= 20:
        scores = [r[0] for r in rows]
        pnls   = [r[1] for r in rows]
        n = len(scores)
        ms = sum(scores) / n
        mp = sum(pnls) / n
        cov  = sum((s - ms) * (p - mp) for s, p in zip(scores, pnls)) / n
        vs   = sum((s - ms) ** 2 for s in scores) / n
        vp   = sum((p - mp) ** 2 for p in pnls) / n
        corr = cov / (vs * vp) ** 0.5 if vs > 0 and vp > 0 else 0
        out["score_pnl_corr"] = round(corr, 4)
    else:
        out["score_pnl_corr"] = None

    # Score quartiles
    cur = conn.execute("""
        SELECT
            ROUND(AVG(CASE WHEN score_rank <= 0.25 THEN pnl_usdt END), 4) as q1_avg,
            ROUND(AVG(CASE WHEN score_rank >= 0.75 THEN pnl_usdt END), 4) as q4_avg
        FROM (
            SELECT pnl_usdt, PERCENT_RANK() OVER (ORDER BY score) as score_rank
            FROM positions WHERE status='CLOSED' AND score IS NOT NULL AND pnl_usdt IS NOT NULL
        )
    """)
    r = cur.fetchone()
    out["score_q1_avg_pnl"] = r[0]  # bottom 25% de scores
    out["score_q4_avg_pnl"] = r[1]  # top 25% de scores
    if r[0] is not None and r[1] is not None:
        out["score_quartile_delta"] = round(r[1] - r[0], 4)
    else:
        out["score_quartile_delta"] = None

    # Entry path distribution (se v3.7)
    if v37 and col_exists(conn, "entry_path"):
        cur = conn.execute("""
            SELECT entry_path, COUNT(*) as n,
                   ROUND(AVG(pnl_usdt), 4) as avg_pnl,
                   ROUND(SUM(pnl_usdt), 2) as sum_pnl
            FROM positions WHERE status='CLOSED'
            GROUP BY entry_path ORDER BY n DESC
        """)
        out["entry_paths"] = [dict(r) for r in cur.fetchall()]

    return out


# ══════════════════════════════════════════════════════════════════════════════
# Módulo 3 — Regime Decomposition
# ══════════════════════════════════════════════════════════════════════════════

def module_3_regime(conn: sqlite3.Connection) -> dict:
    out = {}

    # Trail% e PnL por hora
    cur = conn.execute("""
        SELECT
            strftime('%Y-%m-%d %H:00', closed_at/1000, 'unixepoch') as hora,
            COUNT(*) as n,
            ROUND(SUM(pnl_usdt), 2) as pnl,
            ROUND(AVG(CASE WHEN close_reason='TRAILING' THEN 1.0 ELSE 0 END)*100, 1) as trail_pct,
            SUM(CASE WHEN close_reason='BREAKEVEN' THEN 1 ELSE 0 END) as be_n
        FROM positions WHERE status='CLOSED' AND closed_at IS NOT NULL
        GROUP BY hora ORDER BY hora
    """)
    hourly = [dict(r) for r in cur.fetchall()]
    out["hourly"] = hourly

    # Classificar horas por regime
    favorable   = [h for h in hourly if h["trail_pct"] >= 65]
    neutral     = [h for h in hourly if 40 <= h["trail_pct"] < 65]
    unfavorable = [h for h in hourly if h["trail_pct"] < 40]

    out["regime_favorable_hours"]   = len(favorable)
    out["regime_neutral_hours"]     = len(neutral)
    out["regime_unfavorable_hours"] = len(unfavorable)
    out["regime_favorable_pnl"]     = round(sum(h["pnl"] for h in favorable), 2)
    out["regime_unfavorable_pnl"]   = round(sum(h["pnl"] for h in unfavorable), 2)

    # Trail% range
    trail_pcts = [h["trail_pct"] for h in hourly]
    if trail_pcts:
        out["trail_pct_min"]  = min(trail_pcts)
        out["trail_pct_max"]  = max(trail_pcts)
        out["trail_pct_mean"] = round(sum(trail_pcts) / len(trail_pcts), 1)
        mean = out["trail_pct_mean"]
        out["trail_pct_std"]  = round((sum((x - mean)**2 for x in trail_pcts) / len(trail_pcts))**0.5, 1)

    # OI Regime como qualificador
    cur = conn.execute("""
        SELECT oi_regime, COUNT(*) as n,
               ROUND(AVG(pnl_usdt), 4) as avg_pnl,
               ROUND(SUM(pnl_usdt), 2) as sum_pnl,
               ROUND(AVG(CASE WHEN close_reason='TRAILING' THEN 1.0 ELSE 0 END)*100, 1) as trail_pct
        FROM positions WHERE status='CLOSED'
        GROUP BY oi_regime ORDER BY avg_pnl DESC
    """)
    out["oi_regime"] = [dict(r) for r in cur.fetchall()]

    # Tier distribution
    cur = conn.execute("""
        SELECT tier, COUNT(*) as n,
               ROUND(AVG(pnl_usdt), 4) as avg_pnl,
               ROUND(SUM(pnl_usdt), 2) as sum_pnl
        FROM positions WHERE status='CLOSED'
        GROUP BY tier ORDER BY tier
    """)
    out["tiers"] = [dict(r) for r in cur.fetchall()]

    return out


# ══════════════════════════════════════════════════════════════════════════════
# Módulo 4 — Entry Quality Audit
# ══════════════════════════════════════════════════════════════════════════════

def module_4_entry_quality(conn: sqlite3.Connection) -> dict:
    out = {}
    v37_peak = col_exists(conn, "peak_price")

    # MFE (Max Favorable Excursion) — requer peak_price
    if v37_peak:
        cur = conn.execute("""
            SELECT
                ROUND(AVG((peak_price - entry_price) / entry_price * 100), 4) as mfe_avg_pct,
                ROUND(MEDIAN((peak_price - entry_price) / entry_price * 100), 4) as mfe_med_pct,
                ROUND(AVG(pnl_pct * 100), 4) as actual_avg_pct,
                COUNT(*) as n
            FROM positions WHERE status='CLOSED' AND peak_price IS NOT NULL AND pnl_usdt IS NOT NULL
        """)
        # SQLite não tem MEDIAN — fallback manual
        cur = conn.execute("""
            SELECT
                ROUND(AVG((peak_price - entry_price) / entry_price * 100), 4) as mfe_avg_pct,
                ROUND(AVG(pnl_pct * 100), 4) as actual_avg_pct,
                COUNT(*) as n,
                COUNT(CASE WHEN (peak_price - entry_price) / entry_price > 0.002 THEN 1 END) as mfe_over_02pct,
                COUNT(CASE WHEN pnl_usdt > 0 THEN 1 END) as winners
            FROM positions WHERE status='CLOSED' AND peak_price IS NOT NULL AND pnl_usdt IS NOT NULL
        """)
        r = cur.fetchone()
        if r and r["n"]:
            out["mfe_avg_pct"]   = r["mfe_avg_pct"]
            out["actual_avg_pct"] = r["actual_avg_pct"]
            out["mfe_over_02pct_count"] = r["mfe_over_02pct"]
            out["mfe_over_02pct_pct"]   = round(r["mfe_over_02pct"] / r["n"] * 100, 1)
            out["peak_price_available"] = True
            # Se MFE mediano > 0 mas PnL negativo → problema de saída
            # Se MFE mediano ≈ 0 → problema de entrada
            if out["mfe_avg_pct"] is not None and out["actual_avg_pct"] is not None:
                out["entry_exit_diagnosis"] = (
                    "exit_problem" if out["mfe_avg_pct"] > 0.3 and out["actual_avg_pct"] < 0
                    else "entry_problem" if out["mfe_avg_pct"] < 0.1
                    else "mixed"
                )
        else:
            out["peak_price_available"] = False
            out["entry_exit_diagnosis"] = "insufficient_data"
    else:
        out["peak_price_available"] = False
        out["entry_exit_diagnosis"] = "no_peak_price_col_upgrade_to_v37"

    # Candles distribution
    cur = conn.execute("""
        SELECT candles_open, COUNT(*) as n, ROUND(AVG(pnl_usdt), 4) as avg_pnl
        FROM positions WHERE status='CLOSED'
        GROUP BY candles_open ORDER BY candles_open
    """)
    out["candles_distribution"] = [dict(r) for r in cur.fetchall()]

    # Trailing winners por candle
    cur = conn.execute("""
        SELECT candles_open, COUNT(*) as n
        FROM positions WHERE status='CLOSED' AND close_reason='TRAILING'
        GROUP BY candles_open ORDER BY candles_open
    """)
    trailing_rows = cur.fetchall()
    total_trail = sum(r["n"] for r in trailing_rows)
    cum = 0
    trail_cumulative = []
    for r in trailing_rows:
        cum += r["n"]
        trail_cumulative.append({
            "candle": r["candles_open"],
            "n": r["n"],
            "pct": round(r["n"] / max(total_trail, 1) * 100, 1),
            "cum_pct": round(cum / max(total_trail, 1) * 100, 1),
        })
    out["trailing_by_candle"] = trail_cumulative
    if trail_cumulative:
        c4_cum = next((t["cum_pct"] for t in trail_cumulative if t["candle"] == 4), 0)
        out["trailing_c4_cumulative_pct"] = c4_cum  # AF pressuponha 100%

    # BE loss cap enforcement
    cur = conn.execute("""
        SELECT
            COUNT(*) as total_be,
            SUM(CASE WHEN pnl_pct < -0.005 THEN 1 ELSE 0 END) as over_cap,
            SUM(CASE WHEN pnl_pct < -0.01  THEN 1 ELSE 0 END) as over_1pct,
            ROUND(MIN(pnl_pct)*100, 3) as worst_pct,
            ROUND(AVG(pnl_pct)*100, 3) as avg_pct
        FROM positions WHERE close_reason='BREAKEVEN'
    """)
    r = cur.fetchone()
    if r and r["total_be"]:
        out["be_total"]    = r["total_be"]
        out["be_over_cap_n"]   = r["over_cap"]
        out["be_over_cap_pct"] = round(r["over_cap"] / r["total_be"] * 100, 1)
        out["be_over_1pct_n"]  = r["over_1pct"]
        out["be_worst_pct"]    = r["worst_pct"]
        out["be_avg_pct"]      = r["avg_pct"]

    return out


# ══════════════════════════════════════════════════════════════════════════════
# Módulo 5 — Symbol Attribution
# ══════════════════════════════════════════════════════════════════════════════

def module_5_symbols(conn: sqlite3.Connection) -> dict:
    out = {}

    # Total symbols
    cur = conn.execute("SELECT COUNT(DISTINCT symbol) FROM positions WHERE status='CLOSED'")
    out["total_symbols"] = cur.fetchone()[0]

    cur = conn.execute("SELECT COUNT(DISTINCT symbol) FROM positions WHERE status='CLOSED' AND pnl_usdt > 0")
    out["positive_symbols"] = cur.fetchone()[0]
    out["negative_symbols"] = out["total_symbols"] - out["positive_symbols"]
    out["positive_pct"] = round(out["positive_symbols"] / max(out["total_symbols"], 1) * 100, 1)

    # Top 10 winners
    cur = conn.execute("""
        SELECT symbol, COUNT(*) as n, ROUND(SUM(pnl_usdt), 2) as total_pnl, ROUND(AVG(pnl_usdt), 4) as avg_pnl
        FROM positions WHERE status='CLOSED'
        GROUP BY symbol ORDER BY total_pnl DESC LIMIT 10
    """)
    out["top10_winners"] = [dict(r) for r in cur.fetchall()]

    # Bottom 10 losers
    cur = conn.execute("""
        SELECT symbol, COUNT(*) as n, ROUND(SUM(pnl_usdt), 2) as total_pnl, ROUND(AVG(pnl_usdt), 4) as avg_pnl
        FROM positions WHERE status='CLOSED'
        GROUP BY symbol ORDER BY total_pnl ASC LIMIT 10
    """)
    out["bottom10_losers"] = [dict(r) for r in cur.fetchall()]

    # Concentração do edge
    cur = conn.execute("""
        SELECT SUM(total_pnl) FROM (
            SELECT SUM(pnl_usdt) as total_pnl
            FROM positions WHERE status='CLOSED' AND pnl_usdt > 0
            GROUP BY symbol ORDER BY total_pnl DESC LIMIT 10
        )
    """)
    top10_gains = cur.fetchone()[0] or 0
    cur = conn.execute("SELECT SUM(pnl_usdt) FROM positions WHERE status='CLOSED' AND pnl_usdt > 0")
    total_gains = cur.fetchone()[0] or 0
    out["top10_concentration_pct"] = round(top10_gains / max(total_gains, 0.01) * 100, 1)

    # BE outliers (loss > $3) — proxy de microcaps com spread alto
    cur = conn.execute("""
        SELECT symbol, ROUND(pnl_usdt, 4) as pnl, candles_open,
               ROUND((entry_price - close_price) / entry_price * 100, 3) as slip_pct
        FROM positions WHERE close_reason='BREAKEVEN' AND pnl_usdt < -3.0
        ORDER BY pnl_usdt ASC LIMIT 15
    """)
    out["be_outliers_over_3usd"] = [dict(r) for r in cur.fetchall()]

    # ATR/price proxy (entry_atr / entry_price) para identificar microcaps
    cur = conn.execute("""
        SELECT symbol,
               ROUND(AVG(entry_atr / entry_price * 100), 3) as atr_price_pct,
               ROUND(AVG(pnl_usdt), 4) as avg_pnl,
               COUNT(*) as n
        FROM positions WHERE status='CLOSED' AND entry_atr > 0 AND entry_price > 0
        GROUP BY symbol HAVING COUNT(*) >= 3
        ORDER BY atr_price_pct DESC LIMIT 15
    """)
    out["highest_atr_price_symbols"] = [dict(r) for r in cur.fetchall()]

    return out


# ══════════════════════════════════════════════════════════════════════════════
# Formatação de saída
# ══════════════════════════════════════════════════════════════════════════════

def print_report(db_path: str, m1: dict, m2: dict, m3: dict, m4: dict, m5: dict) -> None:
    sep = "═" * 72
    sec = "─" * 72

    print(f"\n{sep}")
    print(f"  ALPHA RIVER ENGINE — DIAGNOSTIC REPORT")
    print(f"  {db_path}")
    print(f"  Gerado em: {datetime.now(tz=timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}")
    print(sep)

    # ── Módulo 1
    print(f"\n{'── MÓDULO 1: HEALTH CHECK ':{'─'}<72}")
    status_ok = "✅" if m1["inc2_sl_with_be"] == 0 else "❌"
    print(f"  INC-2 [SL+BE=1]:          {m1['inc2_sl_with_be']:>5d}  {status_ok}")
    status_ok = "✅" if m1["inc3_be_without_flag"] == 0 else "❌"
    print(f"  INC-3 [BE+flag=0]:        {m1['inc3_be_without_flag']:>5d}  {status_ok}")
    status_ok = "✅" if m1["inc5_over_mh8"] == 0 else "❌"
    print(f"  INC-5 [candles>8]:        {m1['inc5_over_mh8']:>5d}  {status_ok}")
    status_ok = "✅" if m1["inc6_max_cvd_streak"] == 0 else "❌"
    print(f"  INC-6 [cvd_streak max]:   {m1['inc6_max_cvd_streak']:>5d}  {status_ok}")
    print(f"  Schema v3.7:              {'✅' if m1['schema_v37'] else '⚠️  não instalado — rode migration'}")
    print(f"  Trades CLOSED / OPEN:     {m1['closed']:>5d} / {m1['open']}")
    print(f"  Período:                  {m1['period_start']} → {m1['period_end']}")
    print(f"  Span / Trades/hora:       {m1['span_hours']}h / {m1['trades_per_hour']}/h")
    if m1["negative_scores"]:
        print(f"  ⚠️  Scores negativos:     {m1['negative_scores']} trades (bypass sem registro)")
    if m1["below_threshold"]:
        print(f"  ⚠️  Score < 0.40:         {m1['below_threshold']} trades (secondary trigger?)")

    # ── Módulo 2
    print(f"\n{'── MÓDULO 2: EDGE DETECTION ':{'─'}<72}")
    pf_net_str = f"{m2['pf_net']:.4f}" if m2["pf_net"] else "N/A"
    pf_gross_str = f"{m2['pf_gross']:.4f}" if m2["pf_gross"] else "N/A"
    pf_ok = "✅" if (m2["pf_gross"] or 0) > 1.0 else "❌ SEM EDGE BRUTO"
    print(f"  PnL Líquido Total:        {fmt_pnl(m2['net_pnl'])}")
    print(f"  PnL Bruto Total:          {fmt_pnl(m2['gross_pnl'])}  {pf_ok}")
    print(f"  Comissão Total:           {fmt_pnl(m2['commission_total'])}")
    print(f"  PF Líquido / Bruto:       {pf_net_str} / {pf_gross_str}")
    print(f"  Win Rate:                 {m2['win_rate']:.1f}%  ({m2['wins']}/{m2['n_trades']})")
    corr = m2.get("score_pnl_corr")
    corr_str = f"{corr:.4f}" if corr is not None else "N/A"
    corr_ok = "✅" if (corr or 0) > 0.10 else ("⚠️ fraco" if (corr or 0) > 0.05 else "❌ NULO")
    print(f"  corr(score, PnL):         {corr_str}  {corr_ok}")
    delta = m2.get("score_quartile_delta")
    delta_str = f"${delta:.4f}" if delta is not None else "N/A"
    print(f"  Score Q4 vs Q1 (avg PnL): {delta_str}")
    print(f"  {'─'*30}")
    for r in m2["close_reasons"]:
        print(f"  {r['close_reason']:15s}: {r['n']:>5d} ({r['pct']:>5.1f}%)  PnL={fmt_pnl(r['sum_pnl'])}")
    if "entry_paths" in m2:
        print(f"  {'─'*30}")
        for r in m2.get("entry_paths", []):
            path = r["entry_path"] or "NULL"
            print(f"  path={path:15s}: {r['n']:>5d}  avg={fmt_pnl(r['avg_pnl'])}  sum={fmt_pnl(r['sum_pnl'])}")

    # ── Módulo 3
    print(f"\n{'── MÓDULO 3: REGIME DECOMPOSITION ':{'─'}<72}")
    print(f"  Trail% range:             {m3.get('trail_pct_min',0):.1f}% — {m3.get('trail_pct_max',0):.1f}%  mean={m3.get('trail_pct_mean',0):.1f}%  std={m3.get('trail_pct_std',0):.1f}%")
    print(f"  Horas favoráveis (≥65%):  {m3['regime_favorable_hours']}  PnL={fmt_pnl(m3['regime_favorable_pnl'])}")
    print(f"  Horas desfavoráveis(<40%): {m3['regime_unfavorable_hours']}  PnL={fmt_pnl(m3['regime_unfavorable_pnl'])}")
    print(f"  {'─'*30}")
    for r in m3["oi_regime"]:
        print(f"  OI {r['oi_regime']:15s}: n={r['n']:>4d}  avg={fmt_pnl(r['avg_pnl'])}  trail%={r['trail_pct']:.1f}%")
    print(f"  {'─'*30}")
    for r in m3["tiers"]:
        print(f"  Tier {r['tier']}:                n={r['n']:>5d}  avg={fmt_pnl(r['avg_pnl'])}  sum={fmt_pnl(r['sum_pnl'])}")

    # ── Módulo 4
    print(f"\n{'── MÓDULO 4: ENTRY QUALITY AUDIT ':{'─'}<72}")
    if m4.get("peak_price_available"):
        mfe = m4.get("mfe_avg_pct", 0) or 0
        diag = m4.get("entry_exit_diagnosis", "?")
        diag_str = {"entry_problem": "❌ PROBLEMA DE ENTRADA", "exit_problem": "⚠️ PROBLEMA DE SAÍDA", "mixed": "⚠️ MISTO"}.get(diag, diag)
        print(f"  MFE médio:                {mfe:.4f}%  → {diag_str}")
        print(f"  MFE > +0.2% (trades):     {m4.get('mfe_over_02pct_count',0)} ({m4.get('mfe_over_02pct_pct',0):.1f}%)")
    else:
        print(f"  ⚠️  peak_price não disponível — {m4.get('entry_exit_diagnosis','')}")
    c4_cum = m4.get("trailing_c4_cumulative_pct")
    if c4_cum is not None:
        c4_ok = "✅" if c4_cum >= 95 else "⚠️ abaixo de 95%"
        print(f"  TRAILING acumulado c1-c4: {c4_cum:.1f}%  {c4_ok}  (AF v3.6 pressupunha 100%)")
    if "be_total" in m4:
        cap_ok = "✅" if m4["be_over_cap_pct"] < 20 else "❌ CAP INEFICAZ"
        print(f"  BE violando cap 0.50%:    {m4['be_over_cap_n']} ({m4['be_over_cap_pct']:.1f}%)  {cap_ok}")
        print(f"  BE pior perda / média:    {m4['be_worst_pct']:.3f}% / {m4['be_avg_pct']:.3f}%")

    # ── Módulo 5
    print(f"\n{'── MÓDULO 5: SYMBOL ATTRIBUTION ':{'─'}<72}")
    print(f"  Símbolos operados:        {m5['total_symbols']}")
    print(f"  Positivos / Negativos:    {m5['positive_symbols']} ({m5['positive_pct']:.1f}%) / {m5['negative_symbols']}")
    print(f"  Concentração top-10:      {m5['top10_concentration_pct']:.1f}% dos gains totais")
    print(f"  {'─'*30}")
    print("  TOP 10 WINNERS:")
    for r in m5["top10_winners"][:5]:
        print(f"    {r['symbol']:20s}: {r['n']:>3d} trades  PnL={fmt_pnl(r['total_pnl'])}  avg={fmt_pnl(r['avg_pnl'])}")
    print("  BOTTOM 10 LOSERS:")
    for r in m5["bottom10_losers"][:5]:
        print(f"    {r['symbol']:20s}: {r['n']:>3d} trades  PnL={fmt_pnl(r['total_pnl'])}  avg={fmt_pnl(r['avg_pnl'])}")
    if m5["be_outliers_over_3usd"]:
        print(f"  ⚠️  BE outliers (loss>$3): {len(m5['be_outliers_over_3usd'])} símbolos — candidatos ao filtro H-C")
        for r in m5["be_outliers_over_3usd"][:5]:
            print(f"    {r['symbol']:20s}: PnL={r['pnl']:.4f}  slip={r['slip_pct']:.3f}%")

    print(f"\n{sep}\n")


# ══════════════════════════════════════════════════════════════════════════════
# Main
# ══════════════════════════════════════════════════════════════════════════════

def main():
    parser = argparse.ArgumentParser(description="Alpha River Engine — Diagnostic Script (AF Parte XXVII)")
    parser.add_argument("db", help="Caminho para o snapshot .db")
    parser.add_argument("--json", action="store_true", help="Saída em JSON para rastreamento temporal")
    parser.add_argument("--prev", help="Snapshot anterior para comparação")
    args = parser.parse_args()

    db_path = args.db
    if not Path(db_path).exists():
        print(f"ERRO: arquivo não encontrado: {db_path}", file=sys.stderr)
        sys.exit(1)

    conn = connect(db_path)

    m1 = module_1_health(conn)
    m2 = module_2_edge(conn)
    m3 = module_3_regime(conn)
    m4 = module_4_entry_quality(conn)
    m5 = module_5_symbols(conn)

    conn.close()

    if args.json:
        result = {
            "db": db_path,
            "generated_at": datetime.now(tz=timezone.utc).isoformat(),
            "health": m1,
            "edge": m2,
            "regime": m3,
            "entry_quality": m4,
            "symbols": m5,
        }
        print(json.dumps(result, indent=2, default=str))
    else:
        print_report(db_path, m1, m2, m3, m4, m5)


if __name__ == "__main__":
    main()
