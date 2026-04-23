"""
snapshot_positions.py
Consulta todas as posições abertas no Demo/Testnet e exibe um resumo.

Uso:
    python snapshot_positions.py

Requer as mesmas variáveis de ambiente da engine:
    BINANCE_TESTNET_API_KEY
    BINANCE_TESTNET_API_SECRET

Saída:
    - Tabela de posições ordenadas por unrealizedProfit (pior primeiro)
    - Resumo agregado: total de posições, PnL total, margem em uso
"""

import asyncio
import os
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(Path(__file__).parent / ".env")

from src.execution.binance_exec_client import BinanceExecClient


async def main() -> None:
    api_key    = os.environ.get("BINANCE_TESTNET_API_KEY", "")
    api_secret = os.environ.get("BINANCE_TESTNET_API_SECRET", "")
    base_url   = "https://testnet.binancefuture.com"

    if not api_key or not api_secret:
        print("❌  Credenciais não encontradas. Defina BINANCE_TESTNET_API_KEY e BINANCE_TESTNET_API_SECRET.")
        return

    client = BinanceExecClient(api_key, api_secret, base_url)

    try:
        # ── Saldo da conta ───────────────────────────────────────────────────
        available, wallet = await client.get_account_summary()

        # ── Posições abertas ─────────────────────────────────────────────────
        positions = await client.get_open_positions()

        if not positions:
            print("Nenhuma posição aberta encontrada.")
            return

        # ── Parsing e ordenação ──────────────────────────────────────────────
        parsed = []
        for p in positions:
            symbol        = p.get("symbol", "?")
            qty           = float(p.get("positionAmt", 0))
            entry_price   = float(p.get("entryPrice", 0))
            mark_price    = float(p.get("markPrice", 0))
            unrealized    = float(p.get("unrealizedProfit", 0))
            margin        = float(p.get("isolatedMargin", 0))
            leverage      = int(float(p.get("leverage", 1)))
            notional      = abs(qty) * mark_price
            pnl_pct       = (unrealized / margin * 100) if margin > 0 else 0.0
            side          = "LONG" if qty > 0 else "SHORT"

            parsed.append({
                "symbol":      symbol,
                "side":        side,
                "qty":         qty,
                "entry":       entry_price,
                "mark":        mark_price,
                "unrealized":  unrealized,
                "margin":      margin,
                "notional":    notional,
                "leverage":    leverage,
                "pnl_pct":     pnl_pct,
            })

        # Ordena: pior PnL primeiro
        parsed.sort(key=lambda x: x["unrealized"])

        # ── Totais ───────────────────────────────────────────────────────────
        total_unrealized = sum(p["unrealized"] for p in parsed)
        total_margin     = sum(p["margin"]     for p in parsed)
        total_notional   = sum(p["notional"]   for p in parsed)
        n_loss           = sum(1 for p in parsed if p["unrealized"] < 0)
        n_gain           = sum(1 for p in parsed if p["unrealized"] >= 0)

        # ── Exibição ─────────────────────────────────────────────────────────
        SEP  = "─" * 100
        SEP2 = "═" * 100

        print(f"\n{SEP2}")
        print(f"  SNAPSHOT DE POSIÇÕES — {len(parsed)} abertas")
        print(f"{SEP2}")
        print(f"  Wallet Balance:   {wallet:>12.2f} USDT   (capital realizado total)")
        print(f"  Available:        {available:>12.2f} USDT   (livre para novas posições)")
        print(f"  Capital em margem:{total_margin:>12.2f} USDT   ({total_margin/wallet*100:.1f}% do wallet)")
        print(f"  Unrealized PnL:   {total_unrealized:>+12.2f} USDT")
        print(f"  Drawdown atual:   {abs(total_unrealized)/wallet*100:>11.1f}%   (vs wallet balance)")
        print(f"  Em loss:          {n_loss:>12}        Em gain: {n_gain}")
        print(f"{SEP2}\n")

        # Top 10 piores
        print(f"  {'SÍMBOLO':<18} {'LADO':<6} {'QTY':>12} {'ENTRY':>12} {'MARK':>12} {'UNREALIZED':>12} {'MARGEM':>10} {'PnL%':>7}")
        print(SEP)
        for p in parsed[:20]:
            pnl_str = f"{p['unrealized']:>+10.4f}"
            flag    = "🔴" if p["unrealized"] < 0 else "🟢"
            print(
                f"  {flag} {p['symbol']:<16} {p['side']:<6} "
                f"{p['qty']:>12.4f} {p['entry']:>12.4f} {p['mark']:>12.4f} "
                f"{pnl_str} USDT  {p['margin']:>8.2f} USDT  {p['pnl_pct']:>+6.1f}%"
            )

        if len(parsed) > 20:
            print(f"\n  ... e mais {len(parsed) - 20} posições (apenas Top 20 piores exibidas)")

        # Top 5 melhores (se houver gain)
        gainers = [p for p in parsed if p["unrealized"] > 0]
        if gainers:
            print(f"\n  TOP {min(5, len(gainers))} MELHORES:")
            print(SEP)
            for p in sorted(gainers, key=lambda x: -x["unrealized"])[:5]:
                print(
                    f"  🟢 {p['symbol']:<16} {p['side']:<6} "
                    f"unrealized={p['unrealized']:>+10.4f} USDT  "
                    f"pnl_pct={p['pnl_pct']:>+6.1f}%"
                )

        print(f"\n{SEP2}\n")

    finally:
        await client.close()


if __name__ == "__main__":
    asyncio.run(main())
