"""
src/risk/decay_sl.py
SL Time-Decay — Alpha River v3.3

Motivação:
  O diagnóstico de 2026-04-04 revelou que o "MaxHold wall" (candle 24) é o maior
  drain de PnL: trades que chegam a esse ponto têm WR=5.3% (vs 89.6% nas candles
  anteriores). A causa é que esses trades ficam presos em posições perdedoras sem
  sinal de reversão e o SL fixo não os fecha a tempo.

  A solução é um SL que se aperta progressivamente quanto mais tempo a posição
  fica aberta, usando o ATR fixado na entrada (entry_atr) — não o ATR corrente.
  Isso evita que um spike de volatilidade pós-entrada alargue o SL num momento
  ruim.

Fases do Decay:
  Fase 1: candles 0–4   → mult 2.0 × entry_atr  (SL amplo, tolerância de ruído inicial)
  Fase 2: candles 5–16  → mult 1.7 × entry_atr  (proteção moderada)
  Fase 3: candles 17+   → mult 0.8 × entry_atr  (aperta SL, força saída antes do MaxHold wall)

Resultado empírico (backtest Fase 1, Jan-Fev 2026):
  PF: 0.883 → 0.969 (OOS Mar 2026 com MH20+BL20)
  WR: 74.0% in-sample / 73.1% OOS
  Maior impacto: redução de MaxHold exits de WR=5.3%
"""

import logging
from dataclasses import dataclass

logger = logging.getLogger(__name__)


@dataclass
class DecaySLConfig:
    """Configuração do SL Time-Decay. Lida de risk config no engine.yaml."""
    phase1_candles:    int   = 4     # 0–N → fase 1
    phase2_candles:    int   = 16    # N+1–M → fase 2; M+1+ → fase 3
    mult_phase1:       float = 2.0
    mult_phase2:       float = 1.7
    mult_phase3:       float = 0.8

    @classmethod
    def from_config(cls, risk_cfg: dict) -> "DecaySLConfig":
        return cls(
            phase1_candles=risk_cfg.get("sl_decay_phase1_candles", 4),
            phase2_candles=risk_cfg.get("sl_decay_phase2_candles", 16),
            mult_phase1=risk_cfg.get("sl_decay_mult_phase1", 2.0),
            mult_phase2=risk_cfg.get("sl_decay_mult_phase2", 1.7),
            mult_phase3=risk_cfg.get("sl_decay_mult_phase3", 0.8),
        )


class DecaySLChecker:
    """
    Calcula e verifica o SL Time-Decay para uma posição aberta.

    Uso pelo RiskManager a cada candle fechado:

        checker = DecaySLChecker(config)
        sl_price, reason = checker.compute_sl(position)
        if current_close <= sl_price and reason == CloseReason.DECAY_SL:
            # fechar posição
    """

    def __init__(self, config: DecaySLConfig) -> None:
        self.cfg = config

    def _multiplier(self, candles_open: int) -> tuple[float, int]:
        """
        Retorna (multiplicador ATR, fase) para o número de candles abertos.
        """
        if candles_open <= self.cfg.phase1_candles:
            return self.cfg.mult_phase1, 1
        elif candles_open <= self.cfg.phase2_candles:
            return self.cfg.mult_phase2, 2
        else:
            return self.cfg.mult_phase3, 3

    def compute_sl(self, entry_price: float, entry_atr: float, candles_open: int) -> float:
        """
        Retorna o preço de SL aplicável para a fase atual.

        Args:
            entry_price:  Preço de entrada da posição.
            entry_atr:    ATR(14) no candle de entrada (fixo durante toda a vida da posição).
            candles_open: Número de candles que a posição está aberta.

        Returns:
            sl_price (float): Preço de SL para o candle atual.

        Note:
            Se entry_atr == 0.0, o SL não pode ser calculado e retorna 0.0.
            O RiskManager deve ignorar o resultado nesses casos (fallback para SL estático).
        """
        if entry_atr <= 0.0:
            logger.warning(
                "DecaySL: entry_atr=0.0 — impossível calcular SL decay. "
                "Verifique se entry_atr foi populado no OrderSpec."
            )
            return 0.0

        mult, phase = self._multiplier(candles_open)
        sl = entry_price - mult * entry_atr

        logger.debug(
            "DecaySL | candles=%d phase=%d mult=%.1f entry=%.4f atr=%.4f sl=%.4f",
            candles_open, phase, mult, entry_price, entry_atr, sl,
        )
        return sl

    def check(
        self,
        entry_price: float,
        entry_atr:   float,
        candles_open: int,
        current_close: float,
        current_sl_price: float,
    ) -> tuple[float, bool]:
        """
        Verifica se o SL Time-Decay foi atingido e retorna o novo SL.

        O SL Decay só é aplicado se for MAIS RESTRITIVO (mais alto) que o SL
        atual. Isso garante que o Decay nunca "afrouxe" um trailing stop já ativado.

        Args:
            entry_price:      Preço de entrada.
            entry_atr:        ATR fixo na entrada.
            candles_open:     Candles abertos.
            current_close:    Close do candle atual.
            current_sl_price: SL atual da posição (pode ser trailing ou estático).

        Returns:
            (effective_sl, triggered):
                effective_sl: O SL mais restritivo (max entre decay_sl e current_sl).
                triggered:    True se current_close <= effective_sl.
        """
        decay_sl = self.compute_sl(entry_price, entry_atr, candles_open)

        # Aplica apenas se decay_sl for mais restritivo (maior) que o SL atual
        effective_sl = max(decay_sl, current_sl_price) if decay_sl > 0 else current_sl_price

        triggered = (current_close <= effective_sl) and (effective_sl > 0)

        if triggered:
            logger.info(
                "DecaySL TRIGGERED | close=%.4f <= sl=%.4f (decay_sl=%.4f current_sl=%.4f) | "
                "candles=%d entry=%.4f atr=%.4f",
                current_close, effective_sl, decay_sl, current_sl_price,
                candles_open, entry_price, entry_atr,
            )

        return effective_sl, triggered
