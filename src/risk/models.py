"""
src/risk/models.py
Modelos de dados do Risk Manager.

Contratos de interface entre:
  - SignalEngine → RiskManager  : Signal
  - RiskManager → ExecutionEngine: OrderSpec
  - ExecutionEngine → StateManager: Position
"""

import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import TYPE_CHECKING, Optional

if TYPE_CHECKING:
    from src.signals.models import Signal


class CloseReason(str, Enum):
    """
    Razão de encerramento de uma posição.
    Gravada no trade log para análise de performance posterior.
    """
    SL          = "SL"           # stop loss atingido (inicial ou decay)
    DECAY_SL    = "DECAY_SL"     # v3.3: SL Time-Decay atingido (fase 3, mult 0.8×ATR)
    TRAILING    = "TRAILING"     # trailing stop atingido
    FLOW_CB     = "FLOW_CB"      # v3.3: Flow Circuit Breaker (CVD_n < -0.9 por 4c + close < entry)
    BREAKEVEN   = "BREAKEVEN"    # v3.4: Breakeven Stop (c8 sem trailing + close <= entry)
    MAX_HOLD    = "MAX_HOLD"     # máximo de candles atingido (20c = 5h em v3.3)
    KILL_SWITCH = "KILL_SWITCH"  # kill switch ativado (BTC.D ou drawdown)
    MANUAL      = "MANUAL"       # encerramento manual


@dataclass
class OrderSpec:
    """
    Especificação de ordem de entrada — contrato entre RiskManager e ExecutionEngine.

    O ExecutionEngine usa este objeto para colocar a ordem de entrada (MARKET)
    e configurar o SL inicial (STOP_MARKET). O trailing e o max_hold são
    gerenciados posteriormente pelo position monitor.
    """

    position_id:      str    = field(default_factory=lambda: str(uuid.uuid4()))
    symbol:           str    = ""
    side:             str    = "BUY"           # apenas LONG nesta fase
    qty:              float  = 0.0             # quantidade em base asset
    entry_price:      float  = 0.0             # preço de referência (close da barra)
    sl_price:         float  = 0.0             # SL inicial = entry - 2×ATR
    entry_atr:        float  = 0.0             # v3.3: ATR fixo no momento da entrada (para SL Decay)
    max_hold_candles: int    = 20              # v3.3: 20 candles × 15m = 5h (ótimo empírico)
    tier:             int    = 1
    score:            float  = 0.0
    oi_regime:        str    = "NEUTRO"
    signal:           "Signal" = None          # referência ao Signal de origem

    def __repr__(self) -> str:
        return (
            f"OrderSpec({self.symbol} | {self.side} {self.qty:.6f} @ {self.entry_price:.4f} | "
            f"SL={self.sl_price:.4f} | T{self.tier} score={self.score:.3f} | "
            f"oi={self.oi_regime})"
        )


@dataclass
class Position:
    """
    Posição aberta — estado mantido pelo StateManager durante o ciclo de vida do trade.

    Atualizado a cada candle fechado pelo position monitor do ExecutionEngine.
    Gravado em SQLite para sobreviver a reinícios.
    """

    position_id:      str
    symbol:           str
    entry_price:      float
    qty:              float
    sl_price:         float          # SL atual (atualizado pelo trailing ou decay)
    peak:             float          # maior close desde a entrada
    candles_open:     int    = 0
    tier:             int    = 1
    score:            float  = 0.0
    oi_regime:        str    = "NEUTRO"
    opened_at:        int    = 0     # epoch ms
    trailing_active:  bool   = False
    status:           str    = "OPEN"

    # ── v3.3 — campos adicionais ───────────────────────────────────────────────
    # ATR no momento da entrada (fixo; usado pelo SL Time-Decay para calcular
    # o SL das fases 2 e 3 sem depender do ATR corrente, que pode mudar).
    entry_atr:        float  = 0.0

    # Contador de candles consecutivos com CVD_n < flow_cb_cvd_threshold.
    # Resetado para 0 quando CVD_n >= threshold. Usado pelo FlowCircuitBreaker.
    cvd_neg_streak:   int    = 0

    # v3.4: True quando o SL já foi movido para entry_price pelo BreakevenStop.
    # Flag idempotente — evita recalcular e logar a cada candle subsequente.
    breakeven_active: bool   = False

    # Preenchidos no encerramento
    close_reason:     Optional[CloseReason] = None
    close_price:      Optional[float]       = None
    pnl_usdt:         Optional[float]       = None
    pnl_pct:          Optional[float]       = None

    @classmethod
    def from_order_spec(cls, spec: OrderSpec, ts_open: int) -> "Position":
        """Cria uma posição aberta a partir de um OrderSpec preenchido."""
        return cls(
            position_id=spec.position_id,
            symbol=spec.symbol,
            entry_price=spec.entry_price,
            qty=spec.qty,
            sl_price=spec.sl_price,
            peak=spec.entry_price,   # peak inicial = entry
            candles_open=0,
            tier=spec.tier,
            score=spec.score,
            oi_regime=spec.oi_regime,
            opened_at=ts_open,
            entry_atr=spec.entry_atr,  # v3.3: propaga ATR de entrada para o Position
        )

    def compute_pnl(self, exit_price: float) -> tuple[float, float]:
        """
        Calcula PnL ao encerrar a posição.

        Returns:
            (pnl_usdt, pnl_pct)
        """
        pnl_usdt = (exit_price - self.entry_price) * self.qty
        pnl_pct  = (exit_price - self.entry_price) / self.entry_price
        return round(pnl_usdt, 6), round(pnl_pct, 6)

    def __repr__(self) -> str:
        return (
            f"Position({self.symbol} | entry={self.entry_price:.4f} "
            f"sl={self.sl_price:.4f} peak={self.peak:.4f} | "
            f"{self.candles_open}c open | trailing={self.trailing_active})"
        )


@dataclass
class CloseInstruction:
    """
    Instrução de encerramento emitida pelo RiskManager ao ExecutionEngine.
    Produzida pelo position monitor a cada candle fechado.
    """
    position_id: str
    symbol:      str
    reason:      CloseReason
    close_price: Optional[float] = None   # None = usar preço de mercado atual
