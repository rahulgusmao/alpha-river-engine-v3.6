"""
src/features/calculators/rsi.py
RSI(14) usando Wilder's Smoothing (método original de Welles Wilder).

Fórmula:
    gain = max(close - prev_close, 0)
    loss = max(prev_close - close, 0)

    Inicialização (primeiros 14 candles):
        avg_gain = SMA(gains, 14)
        avg_loss = SMA(losses, 14)

    Subsequentes (Wilder's smoothing, α = 1/14):
        avg_gain = (prev_avg_gain × 13 + gain) / 14
        avg_loss = (prev_avg_loss × 13 + loss) / 14

    RS  = avg_gain / avg_loss
    RSI = 100 - 100 / (1 + RS)

Casos especiais:
    avg_loss = 0 → RSI = 100 (todos os movimentos foram ganhos)
    avg_gain = 0 → RSI = 0   (todos os movimentos foram perdas)
"""

from dataclasses import dataclass, field
from typing import Dict, List, Optional

_PERIOD = 14
_EPSILON = 1e-10


@dataclass
class _RSIState:
    """Estado do RSI por símbolo."""
    gains_init:  List[float] = field(default_factory=list)
    losses_init: List[float] = field(default_factory=list)
    avg_gain:    float = 0.0
    avg_loss:    float = 0.0
    prev_close:  float = 0.0
    has_prev:    bool  = False
    ready:       bool  = False


class RSICalculator:
    """
    Calcula RSI(14) incrementalmente com Wilder's Smoothing.
    Mantém estado por símbolo para processamento de streaming.
    """

    def __init__(self, period: int = _PERIOD):
        self._period = period
        self._states: Dict[str, _RSIState] = {}

    # ──────────────────────────── INICIALIZAÇÃO ────────────────────────────

    def initialize(self, symbol: str, closes: List[float]) -> None:
        """
        Inicializa processando histórico sequencialmente.
        Deve ser chamado com ao menos (period + 1) candles para gerar estado válido.
        """
        self._states[symbol] = _RSIState()
        for c in closes:
            self._update_state(symbol, c)

    # ──────────────────────────── UPDATE ───────────────────────────────────

    def update(self, symbol: str, close: float) -> Optional[float]:
        """
        Atualiza com o close do novo candle e retorna RSI.

        Returns:
            RSI em [0, 100], ou None se ainda não há dados suficientes.
        """
        if symbol not in self._states:
            self._states[symbol] = _RSIState()

        return self._update_state(symbol, close)

    # ──────────────────────────── LÓGICA INTERNA ───────────────────────────

    def _update_state(self, symbol: str, close: float) -> Optional[float]:
        st = self._states[symbol]

        if not st.has_prev:
            st.prev_close = close
            st.has_prev   = True
            return None

        # Gain e loss do período atual
        delta = close - st.prev_close
        gain  = max(delta,  0.0)
        loss  = max(-delta, 0.0)
        st.prev_close = close

        if not st.ready:
            # ── Fase de inicialização: acumula primeiros `period` valores ─
            st.gains_init.append(gain)
            st.losses_init.append(loss)

            if len(st.gains_init) == self._period:
                st.avg_gain = sum(st.gains_init)  / self._period
                st.avg_loss = sum(st.losses_init) / self._period
                st.ready    = True
            return None

        # ── Fase estável: Wilder's smoothing ─────────────────────────────
        st.avg_gain = (st.avg_gain * (self._period - 1) + gain) / self._period
        st.avg_loss = (st.avg_loss * (self._period - 1) + loss) / self._period

        if st.avg_loss < _EPSILON and st.avg_gain < _EPSILON:
            return 50.0  # sem movimento → neutro
        if st.avg_loss < _EPSILON:
            return 100.0
        if st.avg_gain < _EPSILON:
            return 0.0

        rs  = st.avg_gain / st.avg_loss
        rsi = 100.0 - (100.0 / (1.0 + rs))
        return float(rsi)
