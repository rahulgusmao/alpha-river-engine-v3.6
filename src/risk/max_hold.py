"""
src/risk/max_hold.py
Controle de tempo máximo de posição aberta — v3.6.

Regra:
    Se candles_open >= max_hold_candles (padrão: 8) → fechar posição.
    8 candles × 15 min/candle = 2 horas.

Motivação (v3.6 — 2026-04-13):
  Análise do snapshot live (7.142 trades, 11-13/Abr/2026) comprovou que
  segurar posições além de 8 candles é exponencialmente prejudicial:
  - Trades com candles 5-8 sem trailing: avg_loss=-1.12%, P&L=-1.724 USDT
  - A relação perda×tempo é não-linear: cada candle adicional além do c4
    incrementa o loss esperado sem ganho proporcional de win rate.
  Combinado com o Breakeven Stop no c4 (que move SL para entry e aplica
  cap de loss de 0.50%), o MaxHold no c8 é a última linha de defesa:
  fecha qualquer posição que sobreviveu ao BE sem trailing.

  Histórico:
    v3.2: max_hold_candles = 24 (original)
    v3.3: max_hold_candles = 20 (ótimo empírico backtest Jan-Fev 2026)
    v3.6: max_hold_candles = 8  (evidência live Abr/2026 — exponential decay)

Uso: chamado a cada candle fechado pelo position monitor do ExecutionEngine,
APÓS todas as verificações de saída (DecaySL, Breakeven, Trailing).
É o incrementador do contador candles_open — deve ser o último a rodar.
"""

from src.risk.models import CloseReason, Position
from typing import Optional


class MaxHoldChecker:
    """
    Incrementa o contador de candles e verifica o limite máximo de exposição.
    Stateless.
    """

    def __init__(self, max_hold_candles: int = 8):
        """
        Args:
            max_hold_candles: máximo de candles antes do encerramento forçado.
                              v3.6: padrão 8 (era 20 em v3.3, 24 em v3.2).
        """
        self._max = max_hold_candles

    def check(self, position: Position) -> Optional[CloseReason]:
        """
        Incrementa candles_open e verifica o limite.
        Modifica position.candles_open in-place.

        IMPORTANTE: este método deve ser chamado POR ÚLTIMO em evaluate_positions(),
        depois de DecaySL, Breakeven e Trailing. O incremento de candles_open aqui
        é o que alimenta as verificações do próximo candle — incluindo o trigger
        do BreakevenStop (candles_open >= breakeven_trigger_candles).

        Returns:
            CloseReason.MAX_HOLD se o limite foi atingido.
            None caso contrário.
        """
        position.candles_open += 1
        if position.candles_open >= self._max:
            return CloseReason.MAX_HOLD
        return None
