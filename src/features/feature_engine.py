"""
src/features/feature_engine.py
Orquestrador do Feature Engine — recebe ClosedCandle, produz FeatureSet.

Responsabilidades:
  - Inicializar todos os calculators com histórico de warm-up
  - Classificar tokens em tiers (Tier1/2/3)
  - Para cada ClosedCandle ao vivo, chamar todos os calculators e montar FeatureSet
  - Expor a output_queue de FeatureSet para o Signal Engine

Classificação de Tier (simples, evoluir em paper trading):
  Tier1: tokens da lista hardcoded de blue chips (BTC, ETH, top por OI)
  Tier2: tokens com histórico ≥ 500c e que não estão na lista Tier1
  Tier3: todos os demais tokens com histórico ≥ 500c

Nota sobre lsr_n em candles sem LSR:
  Se lsr for None (fetch falhou), usamos o último valor válido do símbolo.
  Se não há nenhum valor histórico, lsr_n = 0.5 (neutro no min-max).
"""

import asyncio
from typing import Dict, List, Optional

import structlog

from src.data.models import ClosedCandle
from src.features.models import FeatureSet, OIRegime
from src.features.calculators.zvol      import ZVolCalculator
from src.features.calculators.cvd       import CVDCalculator
from src.features.calculators.lsr       import LSRCalculator
from src.features.calculators.adx       import ADXCalculator
from src.features.calculators.rsi       import RSICalculator
from src.features.calculators.kalman    import KalmanCalculator
from src.features.calculators.oi_regime import OIRegimeClassifier

logger = structlog.get_logger(__name__)

# ── Tier1: tokens considerados blue chips (OI alto, histórico longo) ──────
_TIER1_TOKENS = {
    "BTCUSDT", "ETHUSDT", "BNBUSDT", "SOLUSDT", "XRPUSDT",
    "ADAUSDT", "DOGEUSDT", "AVAXUSDT", "LINKUSDT", "DOTUSDT",
}

# Mínimo de candles históricos para Tier2/3 (critério Kalman R²)
_MIN_HISTORY_FOR_TIER = 500


class FeatureEngine:
    """
    Processa ClosedCandle e produz FeatureSet com todas as features normalizadas.

    Uso:
        engine = FeatureEngine(config)
        engine.initialize(history)           # warm-up
        await engine.start()                 # inicia loop de consumo

        # Ou processar diretamente:
        feature_set = engine.process(closed_candle)
    """

    def __init__(self, config: dict):
        cfg_sig = config.get("signal", {})

        # Calculators (um por feature, stateful por símbolo)
        self._zvol   = ZVolCalculator(window=100, min_periods=20)
        self._cvd    = CVDCalculator(window=100,  min_periods=20)
        self._lsr    = LSRCalculator(window=100,  min_periods=20)
        self._adx    = ADXCalculator(period=14)
        self._rsi    = RSICalculator(period=14)
        self._kalman = KalmanCalculator(
            r2_window=cfg_sig.get("kalman_r2_window", 500),
            r2_threshold=cfg_sig.get("kalman_r2_threshold", 0.30),
        )
        self._oi_regime = OIRegimeClassifier(threshold_pct=0.01)

        # Último LSR válido por símbolo (fallback quando fetch falha)
        self._last_lsr: Dict[str, float] = {}

        # Tier de cada símbolo (computado no initialize)
        self._tiers: Dict[str, int] = {}

        # v3.3: cache do último FeatureSet processado por símbolo.
        # Usado pelo PositionMonitor para acessar cvd_n sem alterar a topologia de filas.
        # Atualizado em process() a cada candle. Thread-safe para leitura (asyncio single-thread).
        self._last_features: Dict[str, "FeatureSet"] = {}

        # Fila de saída para o Signal Engine
        self._output_queue: asyncio.Queue = asyncio.Queue(maxsize=20_000)

    # ──────────────────────────── INICIALIZAÇÃO ────────────────────────────

    def initialize(self, history: Dict[str, dict]) -> None:
        """
        Aquece todos os calculators com histórico de candles.

        Args:
            history: {symbol: {'klines': [RawCandle], 'oi': [RawOI], 'lsr': [RawLSR]}}
                     como retornado pelo DataLayer.initialize()
        """
        logger.info("feature_engine_warmup_starting", symbols=len(history))

        for symbol, hist in history.items():
            klines = hist.get("klines", [])
            oi_list = hist.get("oi", [])
            lsr_list = hist.get("lsr", [])

            if len(klines) < 2:
                logger.debug("skip_symbol_insufficient_history", symbol=symbol, candles=len(klines))
                continue

            # Extrai listas de valores brutos dos klines
            closes  = [c.close               for c in klines]
            highs   = [c.high                for c in klines]
            lows    = [c.low                 for c in klines]
            volumes = [c.volume              for c in klines]
            tbvs    = [c.taker_buy_base_vol  for c in klines]
            cvd_deltas = [c.cvd_delta        for c in klines]

            # Inicializa cada calculator
            self._zvol.initialize(symbol, volumes, closes)
            self._cvd.initialize(symbol, cvd_deltas)
            self._adx.initialize(symbol, highs, lows, closes)
            self._rsi.initialize(symbol, closes)
            self._kalman.initialize(symbol, closes)

            # LSR: usa valores históricos se disponíveis
            if lsr_list:
                lsr_values = [r.long_short_ratio for r in lsr_list]
                self._lsr.initialize(symbol, lsr_values)
                self._last_lsr[symbol] = lsr_values[-1]

            # OI Regime: inicializa com último par (oi, close) do histórico
            oi_values = [r.open_interest for r in oi_list] if oi_list else []
            self._oi_regime.initialize(symbol, oi_values, closes)

            # Classifica o tier do símbolo
            self._tiers[symbol] = self._classify_tier(symbol, len(klines))

        logger.info(
            "feature_engine_warmup_complete",
            symbols_ready=len(self._tiers),
            tier1=sum(1 for t in self._tiers.values() if t == 1),
            tier2=sum(1 for t in self._tiers.values() if t == 2),
            tier3=sum(1 for t in self._tiers.values() if t == 3),
        )

    # ──────────────────────────── PROCESSAMENTO ────────────────────────────

    def process(self, closed_candle: ClosedCandle) -> Optional[FeatureSet]:
        """
        Processa um ClosedCandle ao vivo e retorna o FeatureSet correspondente.

        Args:
            closed_candle: evento produzido pelo DataLayer (candle + OI + LSR)

        Returns:
            FeatureSet pronto para o SignalEngine, ou None se:
              - símbolo não tem histórico inicializado (novo token)
              - calculators retornam None (janela ainda insuficiente)
        """
        candle = closed_candle.candle
        symbol = candle.symbol

        # Símbolo novo sem warm-up — inicializa sob demanda (vai precisar de mais candles)
        if symbol not in self._tiers:
            self._initialize_new_symbol(symbol, candle)

        tier = self._tiers.get(symbol, 3)

        # ── Z-Vol ─────────────────────────────────────────────────────────
        zvol_n, zvol_raw = self._zvol.update(symbol, candle.volume, candle.close)

        # ── CVD ───────────────────────────────────────────────────────────
        cvd_n = self._cvd.update(symbol, candle.taker_buy_base_vol, candle.volume)

        # ── LSR ───────────────────────────────────────────────────────────
        lsr_value: Optional[float] = None
        if closed_candle.lsr is not None:
            lsr_value = closed_candle.lsr.long_short_ratio
            self._last_lsr[symbol] = lsr_value
        elif symbol in self._last_lsr:
            lsr_value = self._last_lsr[symbol]   # fallback: último valor válido

        lsr_n = self._lsr.update(symbol, lsr_value) if lsr_value is not None else None

        # ── ADX + ATR ─────────────────────────────────────────────────────
        adx_n, atr14 = self._adx.update(symbol, candle.high, candle.low, candle.close)

        # ── RSI ───────────────────────────────────────────────────────────
        rsi = self._rsi.update(symbol, candle.close)

        # ── Kalman (apenas computado, sempre; ativação por R²) ───────────
        kal, kal_active, kal_r2 = self._kalman.update(symbol, candle.close)

        # ── OI Regime ─────────────────────────────────────────────────────
        oi_value = closed_candle.oi.open_interest if closed_candle.oi else None
        oi_regime, oi_mult = self._oi_regime.update(symbol, oi_value, candle.close)

        # ── Verifica se features obrigatórias estão prontas ───────────────
        if any(v is None for v in [zvol_n, cvd_n, lsr_n, adx_n, atr14, rsi]):
            logger.debug(
                "features_not_ready",
                symbol=symbol,
                zvol_ok=zvol_n is not None,
                cvd_ok=cvd_n is not None,
                lsr_ok=lsr_n is not None,
                adx_ok=adx_n is not None,
                atr_ok=atr14 is not None,
                rsi_ok=rsi is not None,
            )
            return None

        fs = FeatureSet(
            symbol=symbol,
            ts_close=candle.ts_close,
            tier=tier,
            zvol_n=zvol_n,
            zvol_raw=zvol_raw,
            cvd_n=cvd_n,
            lsr_n=lsr_n,
            adx_n=adx_n,
            kal=kal,
            kal_active=kal_active,
            kal_r2=kal_r2,
            atr14=atr14,
            rsi=rsi,
            close=candle.close,
            oi_regime=oi_regime,
            oi_mult=oi_mult,
        )
        # v3.3: cache para PositionMonitor (FlowCB precisa de cvd_n)
        self._last_features[symbol] = fs
        return fs

    # ──────────────────────────── TIER ─────────────────────────────────────

    def _classify_tier(self, symbol: str, history_len: int) -> int:
        """
        Classifica o tier do símbolo com base na lista hardcoded e no histórico.

        Tier1: blue chips (lista _TIER1_TOKENS)
        Tier2: tokens com ≥ MIN_HISTORY_FOR_TIER candles e não em Tier1
        Tier3: demais tokens com ≥ MIN_HISTORY_FOR_TIER candles
        """
        if symbol in _TIER1_TOKENS:
            return 1
        if history_len >= _MIN_HISTORY_FOR_TIER:
            return 2   # mid-caps com histórico suficiente
        return 3       # demais (mas ainda elegíveis com histórico >= 500c para Kalman)

    def _initialize_new_symbol(self, symbol: str, candle) -> None:
        """Registra símbolo novo sem histórico (será promovido com o tempo)."""
        self._tiers[symbol] = 3
        logger.debug("new_symbol_registered", symbol=symbol)

    # ──────────────────────────── ASYNC LOOP ───────────────────────────────

    async def run(self, input_queue: asyncio.Queue) -> None:
        """
        Loop assíncrono: consome ClosedCandle da fila do DataLayer,
        processa e coloca FeatureSet na output_queue.

        Uso: asyncio.create_task(feature_engine.run(data_layer._output_queue))
        """
        logger.info("feature_engine_loop_started")
        while True:
            try:
                closed_candle = await input_queue.get()
                feature_set = self.process(closed_candle)
                if feature_set is not None:
                    await self._output_queue.put(feature_set)
            except asyncio.CancelledError:
                break
            except Exception as exc:
                logger.error(
                    "feature_engine_error",
                    symbol=getattr(closed_candle, "symbol", "?"),
                    error=str(exc),
                )
        logger.info("feature_engine_loop_stopped")

    @property
    def output_queue(self) -> asyncio.Queue:
        """Fila de FeatureSet para o Signal Engine."""
        return self._output_queue

    def get_features(self, symbol: str) -> Optional[FeatureSet]:
        """
        v3.3: Retorna o último FeatureSet calculado para o símbolo.
        Usado pelo PositionMonitor para acessar cvd_n no FlowCB.
        Retorna None se o símbolo ainda não foi processado.
        """
        return self._last_features.get(symbol)
