"""
src/risk/flow_cb.py
Flow Circuit Breaker — Alpha River v3.3

Motivação:
  CVD_n (Cumulative Volume Delta normalizado) negativo persistente indica pressão
  vendedora acumulada sem reversão. Quando essa pressão persiste por 4 candles
  consecutivos E o preço atual está abaixo do entry_price, a posição está
  deteriorando — não há sinal de recuperação e segurar aumenta a probabilidade
  de atingir o MaxHold wall com WR=5.3%.

  O Flow CB força a saída preemptiva dessa situação, preservando capital para
  entradas com setup favorável.

Regra de disparo (conjuntiva — AMBAS condições devem ser verdadeiras):
  1. CVD_n < threshold (padrão: -0.9) por N candles CONSECUTIVOS (padrão: 4)
  2. close_price < entry_price (posição no prejuízo)

Resultado empírico:
  Contribui para a melhoria do W/L Ratio de 0.310 → 0.356 (OOS Mar 2026).
  Fechamentos FLOW_CB têm WR inferior ao SL_DECAY mas superior ao MAX_HOLD.
"""

import logging
from dataclasses import dataclass

logger = logging.getLogger(__name__)


@dataclass
class FlowCBConfig:
    """Configuração do Flow Circuit Breaker. Lida de risk config no engine.yaml."""
    cvd_threshold:  float = -0.9   # CVD_n abaixo deste valor conta como "negativo"
    streak_candles: int   = 4      # candles consecutivos necessários para disparar

    @classmethod
    def from_config(cls, risk_cfg: dict) -> "FlowCBConfig":
        return cls(
            cvd_threshold=risk_cfg.get("flow_cb_cvd_threshold", -0.9),
            streak_candles=risk_cfg.get("flow_cb_streak_candles", 4),
        )


class FlowCircuitBreaker:
    """
    Verifica e atualiza o estado do Flow Circuit Breaker para uma posição.

    O estado (cvd_neg_streak) é mantido NO objeto Position, não aqui.
    Este módulo é stateless — recebe o estado, retorna novo estado + decisão.

    Uso pelo RiskManager a cada candle fechado:

        fcb = FlowCircuitBreaker(config)
        new_streak, triggered = fcb.check(
            cvd_n=features.cvd_n,
            close_price=candle.close,
            entry_price=position.entry_price,
            current_streak=position.cvd_neg_streak,
        )
        position.cvd_neg_streak = new_streak
        if triggered:
            # fechar com FLOW_CB
    """

    def __init__(self, config: FlowCBConfig) -> None:
        self.cfg = config

    def update_streak(self, cvd_n: float, current_streak: int) -> int:
        """
        Atualiza o contador de streak CVD negativo.

        Args:
            cvd_n:           CVD normalizado do candle atual (range tipicamente -1 a 1).
            current_streak:  Streak atual da posição (position.cvd_neg_streak).

        Returns:
            new_streak (int): Novo valor do streak (0 se CVD >= threshold).
        """
        if cvd_n < self.cfg.cvd_threshold:
            return current_streak + 1
        return 0

    def check(
        self,
        cvd_n:          float,
        close_price:    float,
        entry_price:    float,
        current_streak: int,
    ) -> tuple[int, bool]:
        """
        Verifica se o Flow Circuit Breaker deve ser acionado.

        Args:
            cvd_n:          CVD normalizado do candle atual.
            close_price:    Preço de fechamento do candle atual.
            entry_price:    Preço de entrada da posição.
            current_streak: Streak CVD negativo acumulado antes deste candle.

        Returns:
            (new_streak, triggered):
                new_streak: Streak atualizado para salvar em position.cvd_neg_streak.
                triggered:  True se ambas as condições forem satisfeitas.
        """
        new_streak = self.update_streak(cvd_n, current_streak)
        position_losing = close_price < entry_price

        triggered = (new_streak >= self.cfg.streak_candles) and position_losing

        if triggered:
            logger.info(
                "FlowCB TRIGGERED | cvd_n=%.3f streak=%d/%d close=%.4f < entry=%.4f",
                cvd_n, new_streak, self.cfg.streak_candles, close_price, entry_price,
            )
        else:
            logger.debug(
                "FlowCB | cvd_n=%.3f streak=%d/%d losing=%s triggered=False",
                cvd_n, new_streak, self.cfg.streak_candles, position_losing,
            )

        return new_streak, triggered
