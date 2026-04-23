"""
src/signals/trigger.py
Verificação de gatilho de entrada — configuração canônica v4.0.

Dois gatilhos independentes (OR):
────────────────────────────────────────────────────────────
PRIMÁRIO   : score >= threshold (padrão: 0.40)
             Sinal institucional forte capturado pelo modelo ponderado
             (zvol + cvd + lsr + adx, com Kalman dinâmico em Tier3).

SECUNDÁRIO : RSI(14) < rsi_threshold (padrão: 30)
             AND zvol_raw > zvol_threshold (padrão: 1.5σ)

             Co-gatilho de pânico de venda: RSI em sobrevenda extrema +
             volume anômalo = potencial capitulação do varejo absorvida
             pelo Smart Money. Captura reversões não cobertas pelo score.
────────────────────────────────────────────────────────────

DOUTRINA DO RSI (MASTER_KNOWLEDGE_BASE + ANALYTICAL_FRAMEWORK):
────────────────────────────────────────────────────────────
O RSI isolado é uma ARMADILHA DE LIQUIDEZ, não um sinal preditivo.

O Smart Money mantém RSI em extremos (sobrecompra/sobrevenda) por longos
períodos nos TFs maiores deliberadamente — isso induz o varejo a abrir
posições contrárias que serão usadas como liquidez.

Hierarquia de precedência de sinais (MASTER_KNOWLEDGE_BASE seção 4.A):
  1. GATILHO:    OI_slope + z_vol     → injeção de capital detectada
  2. CONFIRMAÇÃO: CVD_slope           → agressão taker confirma direção
  3. CONTEXTO:    LSR_slope + Funding → amplifica ou atenua sizing
  4. AUXILIAR:    RSI                 → NUNCA como bloqueio em tendência forte
                                        APENAS como co-gatilho de pânico

Implicações para este módulo:
  - RSI isolado (sem zvol anômalo) NÃO dispara entrada.
  - RSI + zvol > 1.5σ = "pânico de venda com volume institucional" → válido.
  - RSI esticado em tendência forte com score >= threshold → PRIMARY vence
    (não é secundário nesses casos — nem precisaria disparar).
  - RSI NUNCA bloqueia entrada quando score >= threshold.
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

    v4.0: RSI mantido como co-gatilho SECUNDÁRIO (RSI + zvol).
    RSI isolado não dispara entrada. RSI não bloqueia entrada quando score>=threshold.
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
                             Nunca usar como gatilho isolado — exige zvol confirmando.
            zvol_threshold:  zvol_raw mínimo em σ para gatilho secundário (default: 1.5)
                             Obrigatório acompanhar RSI — distingue pânico de tendência exausta.
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
        """
        score >= threshold.
        Captura o alinhamento institucional completo (zvol + cvd + lsr + adx ± kal).
        """
        return fs.score >= self._score_thr

    # ──────────────────────────── SECUNDÁRIO ──────────────────────────────

    def _check_secondary(self, fs: FeatureSet) -> bool:
        """
        RSI < rsi_threshold AND zvol_raw > zvol_threshold.

        Co-gatilho de pânico de venda (MASTER_KNOWLEDGE_BASE / AF):
          - RSI em sobrevenda extrema isolado = faca caindo (ARMADILHA).
          - RSI + zvol anômalo = capitulação do varejo com volume institucional (EDGE).

        Guards:
          - rsi must not be None (defensive programming)
          - zvol_raw já é float (nunca None — inicializado a 0.0 no FeatureSet)
        """
        if fs.rsi is None:
            return False
        return fs.rsi < self._rsi_thr and fs.zvol_raw > self._zvol_thr
