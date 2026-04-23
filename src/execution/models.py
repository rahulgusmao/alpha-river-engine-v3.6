"""
src/execution/models.py
Modelos de dados do Execution Engine.

Contratos de interface:
  - ExecutionEngine → StateManager  : ExecReport (abertura)
  - ExecutionEngine → StateManager  : ClosedExecReport (encerramento)
  - ExecutionEngine → Monitoring    : ambos os tipos (para alertas Telegram)
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from src.risk.models import Position


@dataclass
class FillResult:
    """
    Resultado de preenchimento de uma ordem executada.
    Extraído da resposta da API Binance Futures.
    """

    order_id:         int
    symbol:           str
    side:             str    # "BUY" | "SELL"
    fill_price:       float  # preço médio de execução (avgPrice)
    fill_qty:         float  # quantidade preenchida (executedQty)
    commission_usdt:  float  # comissão estimada (fill_price × fill_qty × taker_fee)
    ts_ms:            int    # timestamp epoch ms da execução

    def __repr__(self) -> str:
        return (
            f"Fill({self.symbol} {self.side} {self.fill_qty:.6f} @ {self.fill_price:.4f} "
            f"fee≈{self.commission_usdt:.4f} USDT)"
        )


@dataclass
class ExecReport:
    """
    Relatório de abertura de posição.
    Produzido após ordem MARKET de entrada + STOP_MARKET de SL colocados com sucesso.
    Entregue ao StateManager e ao PositionMonitor.

    sl_price: SL real ajustado ao fill (entry_fill.fill_price - atr_offset).
              Necessário para o StateManager persistir o sl_price correto no SQLite
              e recuperá-lo fielmente após um restart.
    """

    position_id:   str
    symbol:        str
    entry_fill:    FillResult
    sl_order_id:   int    # order_id Binance do STOP_MARKET (para cancelamento futuro)
    leverage:      int
    sl_price:      float  # SL ajustado ao fill real (entry_fill.fill_price - 2×ATR)

    # Metadados do sinal — necessários para o StateManager persistir corretamente
    # no SQLite. Sem eles, todos os trades ficam com score=0/tier=1/oi=NEUTRO no DB.
    tier:       int   = 1
    score:      float = 0.0
    oi_regime:  str   = "NEUTRO"
    entry_atr:  float = 0.0   # v3.3: ATR fixo na entrada — necessário para DecaySL

    # v3.7: instrumentação de diagnóstico (P65 — AF Parte XXVII)
    # Populados pelo ExecutionEngine a partir do Signal/FeatureSet no momento da entrada.
    entry_path:      str   | None = None   # 'score' | 'rsi_secondary' | 'zvol_secondary'
    entry_zvol:      float | None = None   # Z-Score de volume (zvol_raw)
    entry_cvd_z:     float | None = None   # CVD normalizado (cvd_n)
    entry_lsr_z:     float | None = None   # LSR normalizado (lsr_n)
    entry_adx:       float | None = None   # ADX(14) no candle de entrada
    entry_kalman_r2: float | None = None   # R² rolling Kalman

    def __repr__(self) -> str:
        return (
            f"ExecReport({self.symbol} | pid={self.position_id[:8]} | "
            f"entry={self.entry_fill.fill_price:.4f} | sl={self.sl_price:.4f} | "
            f"sl_order={self.sl_order_id} | {self.leverage}x | "
            f"T{self.tier} score={self.score:.3f} oi={self.oi_regime} "
            f"path={self.entry_path})"
        )


@dataclass
class PositionStateUpdate:
    """
    Snapshot de estado incremental de uma posição aberta.

    Emitido pelo ExecutionEngine após cada candle que modifica peak,
    sl_price, candles_open ou trailing_active via PositionMonitor.on_candle().
    Consumido pelo StateManager para persistir o estado no SQLite — garante
    que um restart recupere candles_open e trailing_active corretos.

    Não representa abertura nem fechamento — apenas atualização de estado.
    """
    position: "Position"


@dataclass
class ClosedExecReport:
    """
    Relatório de encerramento de posição.
    Produzido quando o ExecutionEngine fecha a posição por qualquer razão.

    v3.7: adicionados gross_pnl e commission para persistência separada no DB,
    permitindo auditoria de edge bruto vs. custo de transação em diagnósticos.
    """

    position_id:  str
    symbol:       str
    close_fill:   FillResult
    close_reason: str    # CloseReason.value — string para serialização
    pnl_usdt:     float  # PnL líquido em USDT (gross_pnl - commission)
    pnl_pct:      float  # PnL percentual (usando fills reais)

    # v3.7: decomposição do PnL para diagnóstico de edge bruto
    gross_pnl:   float | None = None  # PnL antes da comissão
    commission:  float | None = None  # comissão bilateral estimada

    def __repr__(self) -> str:
        sign = "+" if self.pnl_usdt >= 0 else ""
        gross_str = f" gross={'+' if (self.gross_pnl or 0) >= 0 else ''}{self.gross_pnl:.4f}" if self.gross_pnl is not None else ""
        return (
            f"ClosedReport({self.symbol} | {self.close_reason} | "
            f"pid={self.position_id[:8]} | "
            f"{sign}{self.pnl_usdt:.4f} USDT ({sign}{self.pnl_pct * 100:.2f}%)"
            f"{gross_str})"
        )
