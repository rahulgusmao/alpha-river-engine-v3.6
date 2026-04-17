"""
src/data/data_layer.py
Ponto de entrada único do Data Layer — orquestrador de todas as fontes de dados.

Interface pública para o restante da engine:
  - initialize() : descobre universo + warm-up histórico → retorna histórico
  - start()       : inicia streaming ao vivo
  - stop()        : encerra tudo de forma limpa
  - __aiter__()   : async iterator de ClosedCandle (consumido pelo Feature Engine)
  - get_btc_dominance() : último valor de BTC.D (consumido pelo Kill Switch)

Ordem de inicialização obrigatória:
  history = await data_layer.initialize()
  await data_layer.start()
  async for candle in data_layer: ...
"""

import asyncio
from typing import AsyncIterator, Callable, Coroutine, Dict, List, Optional

import structlog

from src.data.models import ClosedCandle, RawBTCD
from src.data.rest.binance_rest import BinanceRestClient
from src.data.rest.history_loader import HistoryLoader
from src.data.rest.oi_fetcher import OIFetcher
from src.data.rest.lsr_fetcher import LSRFetcher
from src.data.rest.btcd_fetcher import BTCDFetcher
from src.data.streams.stream_manager import StreamManager

logger = structlog.get_logger(__name__)

# Tamanho da fila de saída (buffer entre Data Layer e Feature Engine)
_OUTPUT_QUEUE_MAXSIZE = 10_000

# Número de símbolos processados em paralelo durante o warm-up
# (evita sobrecarregar a API com 400 símbolos simultâneos)
_WARMUP_BATCH_SIZE = 20


class DataLayer:
    """
    Orquestrador do Data Layer do Alpha River Engine.

    Uso típico:

        config = yaml.safe_load(open('config/engine.yaml'))
        data_layer = DataLayer(config)

        # Fase 1: warm-up (bloqueante — aguarda todo o histórico)
        history = await data_layer.initialize()

        # Fase 2: streaming ao vivo
        await data_layer.start()

        # Consumo pelo Feature Engine
        async for closed_candle in data_layer:
            await feature_engine.process(closed_candle)

        # Encerramento
        await data_layer.stop()
    """

    def __init__(self, config: dict):
        self._cfg = config
        self._symbols: List[str] = []
        self._history: Dict[str, dict] = {}

        # Fila de saída: ClosedCandle para o Feature Engine
        self._output_queue: asyncio.Queue = asyncio.Queue(maxsize=_OUTPUT_QUEUE_MAXSIZE)

        # Componentes (inicializados em initialize/start)
        self._stream_manager: Optional[StreamManager] = None
        self._btcd_fetcher: Optional[BTCDFetcher] = None
        self._btcd_task: Optional[asyncio.Task] = None

        # Cliente REST compartilhado por todos os fetchers (um pool de conexões TCP)
        self._market_client = BinanceRestClient(
            base_url=config["network"]["market_data_rest_url"],
            semaphore_limit=config["data"].get("oi_lsr_semaphore", 50),
        )

    # ─────────────────────────── INICIALIZAÇÃO ─────────────────────────────

    async def initialize(self) -> Dict[str, dict]:
        """
        Fase 1: descoberta do universo + warm-up histórico via REST.

        Processa símbolos em lotes de _WARMUP_BATCH_SIZE para não sobrecarregar
        a API com 400+ requests simultâneos durante a fase de warm-up.

        Returns:
            Dict {symbol: {'klines': [...], 'oi': [...], 'lsr': [...]}}
            Passado diretamente para o Feature Engine na inicialização.

        Raises:
            RuntimeError: se o universo de símbolos estiver vazio.
        """
        loader = HistoryLoader(
            client=self._market_client,
            interval=self._cfg["data"]["kline_interval"],
        )

        # 1. Descoberta do universo
        self._symbols = await loader.fetch_universe(
            quote_asset=self._cfg["universe"]["quote_asset"]
        )
        if not self._symbols:
            raise RuntimeError(
                "Universo de símbolos vazio. "
                "Verifique a conexão com a Binance API e as configurações de universe."
            )

        logger.info(
            "initialize_started",
            symbols=len(self._symbols),
            history_candles=self._cfg["data"]["history_candles"],
        )

        # 2. Warm-up em lotes paralelos
        history_candles = self._cfg["data"]["history_candles"]
        total = len(self._symbols)
        loaded = 0

        for i in range(0, total, _WARMUP_BATCH_SIZE):
            batch = self._symbols[i : i + _WARMUP_BATCH_SIZE]

            # Lança warm-up dos símbolos do lote em paralelo
            tasks = {
                sym: asyncio.create_task(loader.warm_up_symbol(sym, history_candles))
                for sym in batch
            }
            results = await asyncio.gather(*tasks.values(), return_exceptions=True)

            for sym, result in zip(tasks.keys(), results):
                if isinstance(result, Exception):
                    logger.warning(
                        "warm_up_symbol_failed",
                        symbol=sym,
                        error=str(result),
                    )
                    # Símbolo com falha não entra no histórico — será tratado pelo Feature Engine
                else:
                    self._history[sym] = result

            loaded += len(batch)
            logger.info(
                "warm_up_progress",
                progress=f"{loaded}/{total}",
                loaded_symbols=len(self._history),
            )

        logger.info(
            "initialize_complete",
            total_symbols=total,
            loaded_symbols=len(self._history),
            failed_symbols=total - len(self._history),
        )

        return self._history

    # ─────────────────────────── STREAMING ─────────────────────────────────

    async def start(
        self,
        on_btcd_update: Optional[Callable[[RawBTCD], Coroutine]] = None,
    ) -> None:
        """
        Fase 2: inicia WebSocket streaming ao vivo.
        Deve ser chamado APÓS initialize().

        Args:
            on_btcd_update: coroutine opcional chamada a cada novo valor de BTC.D.
                            Usado pelo RiskManager para alimentar o Kill Switch.
                            Se None, o Kill Switch de BTC.D fica inativo.

        Raises:
            RuntimeError: se initialize() não foi chamado antes.
        """
        if not self._symbols:
            raise RuntimeError(
                "DataLayer.initialize() deve ser chamado antes de start()."
            )

        # Fetchers REST (compartilham o mesmo BinanceRestClient)
        oi_fetcher = OIFetcher(client=self._market_client)
        lsr_fetcher = LSRFetcher(
            client=self._market_client,
            period=self._cfg["data"]["kline_interval"],
        )

        # StreamManager: WebSocket + enriquecimento OI/LSR
        self._stream_manager = StreamManager(
            symbols=self._symbols,
            output_queue=self._output_queue,
            oi_fetcher=oi_fetcher,
            lsr_fetcher=lsr_fetcher,
            ws_base_url=self._cfg["network"]["market_data_ws_url"],
            interval=self._cfg["data"]["kline_interval"],
        )
        await self._stream_manager.start()

        # BTCDFetcher: polling CoinGecko para Kill Switch.
        # on_btcd_update conecta o poller diretamente ao RiskManager —
        # sem esse callback o Kill Switch de BTC.D nunca recebe dados.
        self._btcd_fetcher = BTCDFetcher(
            on_update=on_btcd_update,
            poll_interval_sec=self._cfg["data"].get("btcd_poll_interval_sec", 300),
        )
        self._btcd_task = asyncio.create_task(
            self._btcd_fetcher.run(), name="btcd_poller"
        )

        logger.info("data_layer_started", symbols=len(self._symbols))

    # ─────────────────────────── ENCERRAMENTO ──────────────────────────────

    async def stop(self) -> None:
        """
        Encerra todos os streams e libera recursos.
        Seguro para chamar múltiplas vezes.
        """
        if self._stream_manager:
            await self._stream_manager.stop()
            self._stream_manager = None

        if self._btcd_fetcher:
            self._btcd_fetcher.stop()

        if self._btcd_task and not self._btcd_task.done():
            self._btcd_task.cancel()
            try:
                await self._btcd_task
            except asyncio.CancelledError:
                pass

        await self._market_client.close()
        logger.info("data_layer_stopped")

    # ─────────────────────────── INTERFACE PÚBLICA ─────────────────────────

    def get_btc_dominance(self) -> Optional[float]:
        """
        Retorna o último valor de BTC Dominance disponível (0–100).
        None antes do primeiro poll bem-sucedido (~5s após start()).
        Usado pelo Kill Switch no Risk Manager.
        """
        if self._btcd_fetcher and self._btcd_fetcher.latest:
            return self._btcd_fetcher.latest.btc_dominance
        return None

    @property
    def symbols(self) -> List[str]:
        """Lista de símbolos do universo descoberto em initialize()."""
        return list(self._symbols)

    @property
    def history(self) -> Dict[str, dict]:
        """Histórico carregado durante initialize()."""
        return self._history

    def __aiter__(self) -> AsyncIterator[ClosedCandle]:
        """
        Permite consumir ClosedCandles com `async for candle in data_layer`.
        Bloqueia até o próximo candle estar disponível na fila.
        """
        return self._aiter_impl()

    async def _aiter_impl(self) -> AsyncIterator[ClosedCandle]:
        """Implementação do async iterator."""
        while True:
            try:
                candle = await self._output_queue.get()
                yield candle
            except asyncio.CancelledError:
                break
