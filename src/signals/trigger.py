"""
src/signals/trigger.py
Verificação de gatilho de entrada — configuração canônica v3.2.

Dois gatilhos independentes (OR):
────────────────────────────────────────────────────────────
PRIMÁRIO   : score >= threshold (padrão: 0.40)
             Sinal institucional forte capturado pelo modelo.

SECUNDÁRIO : RSI(14) < rsi_threshold (padrão: 30)
             AND zvol_raw > zvol_threshold (padrão: 1.5σ)

             Oversold com volume anômalo = potencial de absorção institucional
             ou exaustão de venda com capitulação do varejo.
             Captura oportunidades de reversão não capturadas pelo score.
────────────────────────────────────────────────────────────

Retorna (triggered: bool, trigger_type: TriggerType | None).
trigger_type é PRIMARY se ambos forem verdadeiros (PRIMARY tem precedência).
"""

from typing import Optional, Tuple

from src.features.models import FeatureSet
from src.signals.models import TriggerType

# Valores padrão — sobrescritos pelo config em SignalEngine
_DEFAULT_SCORE_THRESHOLD = 0.40
_DEFAULT_RSI_THRESHOLD   = 30.0
_DEFAULT_ZVOL_THRESHOLD  = 1.5


class Trigger:
    """
    Avalia as condições de entrada para um FeatureSet pontuado.
    Stateless — pode ser reutilizado para qualquer símbolo.
    """

    def __init__(
        self,
        score_threshold: float = _DEFAULT_SCORE_THRESHOLD,
        rsi_threshold:   float = _DEFAULT_RSI_THRESHOLD,
        zvol_threshold:  float = _DEFAULT_ZVOL_THRESHOLD,
    ):
        """
        Args:
            score_threshold: score mínimo para gatilho primário (default: 0.40)
            rsi_threshold:   RSI máximo para gatilho secundário (default: 30)
            zvol_threshold:  zvol_raw mínimo em σ para gatilho secundário (default: 1.5)
        """
        self._score_thr = score_threshold
        self._rsi_thr   = rsi_threshold
        self._zvol_thr  = zvol_threshold

    def check(self, fs: FeatureSet) -> Tuple[bool, Optional[TriggerType]]:
        """
        Verifica se o FeatureSet satisfaz algum dos gatilhos de entrada.

        Requer que fs.score já tenha sido preenchido pelo Scorer.

        Returns:
            (triggered, trigger_type)
            triggered=False → (False, None)
            triggered=True  → (True, TriggerType.PRIMARY | TriggerType.SECONDARY)

        Precedência: PRIMARY > SECONDARY (se ambos verdadeiros, retorna PRIMARY).
        """
        primary   = self._check_primary(fs)
        secondary = self._check_secondary(fs)

        if primary:
            return True, TriggerType.PRIMARY
        if secondary:
            return True, TriggerType.SECONDARY
        return False, None

    # ──────────────────────────── PRIMÁRIO ────────────────────────────────

    def _check_primary(self, fs: FeatureSet) -> bool:
        """score >= threshold."""
        return fs.score >= self._score_thr

    # ──────────────────────────── SECUNDÁRIO ──────────────────────────────

    def _check_secondary(self, fs: FeatureSet) -> bool:
        """
        RSI < rsi_threshold AND zvol_raw > zvol_threshold.

        Guards:
          - rsi must not be None (feature ready check já feito pelo FeatureEngine,
            mas defensive programming é barato)
          - zvol_raw já é um float (nunca None — inicializado a 0.0 no FeatureSet)
        """
        if fs.rsi is None:
            return False
        return fs.rsi < self._rsi_thr and fs.zvol_raw > self._zvol_thr
