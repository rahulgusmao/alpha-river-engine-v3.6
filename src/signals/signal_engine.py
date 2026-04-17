"""
src/signals/signal_engine.py
Orquestrador do Signal Engine — recebe FeatureSet, emite Signal.

Pipeline por FeatureSet:
  1. Scorer.compute(fs)          → preenche fs.score
  2. Trigger.check(fs)           → verifica gatilhos PRIMARY / SECONDARY
  3. Se triggered → monta Signal e coloca na output_queue

Interface pública:
  - process(fs)       : processa um FeatureSet e retorna Signal ou None
  - run(input_queue)  : loop assíncrono para uso via asyncio.create_task
  - output_queue      : fila de Signal para o Risk Manager
"""

import asyncio
from typing import Optional

import structlog

from src.features.models import FeatureSet
from src.signals.models  import Signal, TriggerType
from src.signals.scorer  import Scorer
from src.signals.trigger import Trigger

logger = structlog.get_logger(__name__)


class SignalEngine:
    """
    Aplica o score e avalia gatilhos para cada FeatureSet recebido.

    Stateless em relação a símbolos — toda a memória está no FeatureEngine.
    Um Signal emitido representa uma oportunidade confirmada de entrada LONG.
    """

    def __init__(self, config: dict):
        cfg = config.get("signal", {})

        self._scorer  = Scorer()
        self._trigger = Trigger(
            score_threshold=cfg.get("score_threshold", 0.40),
            rsi_threshold=cfg.get("rsi_secondary_threshold", 30.0),
            zvol_threshold=cfg.get("zvol_secondary_threshold", 1.5),
        )

        # Fila de saída para o Risk Manager
        self._output_queue: asyncio.Queue = asyncio.Queue(maxsize=5_000)

        # Contadores para monitoramento
        self._total_processed = 0
        self._total_signals   = 0

    # ──────────────────────────── PROCESSAMENTO ────────────────────────────

    def process(self, fs: FeatureSet) -> Optional[Signal]:
        """
        Processa um FeatureSet e retorna um Signal se as condições de entrada
        forem satisfeitas.

        Args:
            fs: FeatureSet com todas as features obrigatórias preenchidas
                (is_ready() == True garantido pelo FeatureEngine).

        Returns:
            Signal se triggered, None caso contrário.
        """
        self._total_processed += 1

        # 1. Computa score (atualiza fs.score in-place)
        self._scorer.compute(fs)

        # 2. Verifica gatilhos
        triggered, trigger_type = self._trigger.check(fs)

        if not triggered:
            return None

        # 3. Monta o Signal
        self._total_signals += 1

        signal = Signal(
            symbol=fs.symbol,
            ts_close=fs.ts_close,
            tier=fs.tier,
            score=fs.score,
            direction="LONG",
            entry_price=fs.close,
            atr14=fs.atr14,
            oi_regime=fs.oi_regime.value,
            oi_mult=fs.oi_mult,
            trigger_type=trigger_type,
            feature_set=fs,
        )

        logger.info(
            "signal_generated",
            symbol=signal.symbol,
            tier=signal.tier,
            score=round(signal.score, 4),
            trigger=trigger_type.value,
            entry=round(signal.entry_price, 4),
            atr=round(signal.atr14, 4),
            oi_regime=signal.oi_regime,
            rsi=round(fs.rsi, 1) if fs.rsi else None,
            zvol_raw=round(fs.zvol_raw, 2),
            signals_total=self._total_signals,
        )

        return signal

    # ──────────────────────────── ASYNC LOOP ───────────────────────────────

    async def run(self, input_queue: asyncio.Queue) -> None:
        """
        Loop assíncrono: consome FeatureSet da fila do FeatureEngine,
        processa e coloca Signal na output_queue.

        Uso: asyncio.create_task(signal_engine.run(feature_engine.output_queue))
        """
        logger.info("signal_engine_loop_started")

        while True:
            try:
                fs = await input_queue.get()
                signal = self.process(fs)
                if signal is not None:
                    await self._output_queue.put(signal)
            except asyncio.CancelledError:
                break
            except Exception as exc:
                logger.error(
                    "signal_engine_error",
                    symbol=getattr(fs, "symbol", "?"),
                    error=str(exc),
                )

        logger.info(
            "signal_engine_loop_stopped",
            total_processed=self._total_processed,
            total_signals=self._total_signals,
        )

    # ──────────────────────────── INTERFACE ────────────────────────────────

    @property
    def output_queue(self) -> asyncio.Queue:
        """Fila de Signal para o Risk Manager."""
        return self._output_queue

    @property
    def signal_rate(self) -> float:
        """Taxa de sinais emitidos sobre total processado (0–1)."""
        if self._total_processed == 0:
            return 0.0
        return self._total_signals / self._total_processed
