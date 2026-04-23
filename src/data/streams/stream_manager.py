"""
src/data/streams/stream_manager.py
Orquestrador de múltiplas conexões WebSocket de klines para o universo completo.

Responsabilidades:
  1. Dividir o universo de símbolos em lotes de MAX_STREAMS_PER_CONNECTION
  2. Instanciar e gerenciar um KlineStream por lote
  3. Após cada kline close, buscar OI+LSR via REST em paralelo
  4. Emitir ClosedCandle (candle + OI + LSR) na output_queue do DataLayer

Fluxo interno:
  [KlineStream_0..N] → _raw_queue (RawCandle)
                              ↓
                      _enrich_worker (busca OI+LSR em paralelo)
                              ↓
                      output_queue (ClosedCandle) → Feature Engine
"""

import asyncio
import math
from typing import Dict, List, Optional, Set

import structlog

from src.data.models import ClosedCandle, RawCandle, RawOI, RawLSR
from src.data.streams.kline_stream import KlineStream, MAX_STREAMS_PER_CONNECTION
from src.data.rest.oi_fetcher import OIFetcher
from src.data.rest.lsr_fetcher import LSRFetcher

logger = structlog.get_logger(__name__)

# Capacidade máxima da fila interna de RawCandles (antes do enriquecimento)
# Com ~400 tokens fechando ao mesmo tempo: 400 eventos em rajada → buffer generoso
_RAW_QUEUE_MAXSIZE = 2000


class StreamManager:
    """
    Gerencia o ciclo completo: WebSocket → enriquecimento REST → ClosedCandle.

    Deve ser instanciado e controlado pelo DataLayer.
    """

    def __init__(
        self,
        symbols: List[str],
        output_queue: asyncio.Queue,
        oi_fetcher: OIFetcher,
        lsr_fetcher: LSRFetcher,
        ws_base_url: str,
        interval: str = "15m",
    ):
        """
        Args:
            symbols:      universo completo de símbolos a monitorar
            output_queue: fila de saída para o Feature Engine (ClosedCandle)
            oi_fetcher:   instância de OIFetcher (compartilhada com DataLayer)
            lsr_fetcher:  instância de LSRFetcher (compartilhada com DataLayer)
            ws_base_url:  URL base WebSocket (ex: 'wss://fstream.binance.com')
            interval:     intervalo dos klines (ex: '15m')
        """
        self._symbols = symbols
        self._output_queue = output_queue
        self._oi_fetcher = oi_fetcher
        self._lsr_fetcher = lsr_fetcher
        self._ws_base_url = ws_base_url
        self._interval = interval

        # Fila interna: RawCandle antes do enriquecimento OI/LSR
        self._raw_queue: asyncio.Queue = asyncio.Queue(maxsize=_RAW_QUEUE_MAXSIZE)

        self._streams: List[KlineStream] = []
        self._tasks: List[asyncio.Task] = []
        self._running = False

    # ──────────────────────────── SPLIT ────────────────────────────────────

    def _split_symbols(self) -> List[List[str]]:
        """
        Divide o universo em lotes equilibrados de no máximo MAX_STREAMS_PER_CONNECTION.

        Exemplo: 400 símbolos → 2 lotes de 200 (balanceado, não 300+100).
        """
        total = len(self._symbols)
        n_connections = math.ceil(total / MAX_STREAMS_PER_CONNECTION)
        batch_size = math.ceil(total / n_connections)
        return [
            self._symbols[i : i + batch_size]
            for i in range(0, total, batch_size)
        ]

    # ──────────────────────────── ENRIQUECIMENTO ───────────────────────────

    async def _enrich_candle(self, candle: RawCandle) -> None:
        """
        Busca OI e LSR em paralelo para um único candle fechado.
        Produz ClosedCandle na output_queue independente de falhas.
        OI/LSR None é tratado downstream pelo Feature Engine.
        """
        symbol = candle.symbol

        # Fetch paralelo de OI e LSR — a latência é dominada pelo mais lento dos dois
        oi_task = asyncio.create_task(self._oi_fetcher.fetch_one(symbol))
        lsr_task = asyncio.create_task(self._lsr_fetcher.fetch_one(symbol))

        results = await asyncio.gather(oi_task, lsr_task, return_exceptions=True)

        oi: Optional[RawOI] = results[0] if not isinstance(results[0], Exception) else None
        lsr: Optional[RawLSR] = results[1] if not isinstance(results[1], Exception) else None

        if isinstance(results[0], Exception):
            logger.warning("oi_enrich_error", symbol=symbol, error=str(results[0]))
        if isinstance(results[1], Exception):
            logger.warning("lsr_enrich_error", symbol=symbol, error=str(results[1]))

        closed = ClosedCandle(candle=candle, oi=oi, lsr=lsr)
        await self._output_queue.put(closed)

        logger.debug(
            "closed_candle_produced",
            symbol=symbol,
            close=candle.close,
            cvd_delta=round(candle.cvd_delta, 4),
            oi_ok=oi is not None,
            lsr_ok=lsr is not None,
        )

    # Máximo de tasks de enriquecimento OI+LSR pendentes simultaneamente.
    # ~400 tokens por boundary 15m × até 3 boundaries atrasados = 1200.
    # Se exceder, aguarda tasks existentes finalizarem antes de criar novas.
    _MAX_PENDING_TASKS = 1000

    async def _enrich_worker(self) -> None:
        """
        Worker que consome RawCandles da fila interna e dispara enriquecimento.

        Design: não processa um por um sequencialmente — drena toda a fila
        disponível de uma vez e dispara tasks concorrentes. Isso é crítico
        pois ~400 tokens fecham ao mesmo tempo no boundary do 15m.

        As tasks de enriquecimento ficam em `_pending` e são monitoradas
        para evitar acúmulo indefinido em caso de falha sustentada da API REST.
        Quando `_MAX_PENDING_TASKS` é atingido, aguarda metade das tasks
        finalizarem antes de aceitar novos candles.
        """
        pending: Set[asyncio.Task] = set()

        while self._running:
            try:
                # Se o backlog está no limite, aguarda metade finalizar antes de prosseguir
                if len(pending) >= self._MAX_PENDING_TASKS:
                    logger.warning(
                        "enrich_worker_backlog_cap",
                        pending_tasks=len(pending),
                        cap=self._MAX_PENDING_TASKS,
                        hint="Aguardando tasks finalizarem antes de aceitar novos candles",
                    )
                    # Aguarda até que metade das tasks conclua
                    done, pending = await asyncio.wait(
                        pending, return_when=asyncio.FIRST_COMPLETED
                    )
                    # Drena o restante concluído sem bloquear
                    while True:
                        done2, remaining = await asyncio.wait(pending, timeout=0)
                        pending = remaining
                        if not done2:
                            break

                # Aguarda o próximo candle com timeout para checar _running
                try:
                    candle = await asyncio.wait_for(
                        self._raw_queue.get(), timeout=2.0
                    )
                    task = asyncio.create_task(self._enrich_candle(candle))
                    pending.add(task)
                    task.add_done_callback(pending.discard)
                except asyncio.TimeoutError:
                    pass  # sem candle — continua o loop

                # Drena o restante da fila (rajada de 15m boundary)
                while not self._raw_queue.empty() and len(pending) < self._MAX_PENDING_TASKS:
                    try:
                        candle = self._raw_queue.get_nowait()
                        task = asyncio.create_task(self._enrich_candle(candle))
                        pending.add(task)
                        task.add_done_callback(pending.discard)
                    except asyncio.QueueEmpty:
                        break

                # Aviso leve se backlog estiver crescendo (antes do hard cap)
                if len(pending) > 200:
                    logger.warning("enrich_worker_backlog", pending_tasks=len(pending))

            except asyncio.CancelledError:
                break
            except Exception as exc:
                logger.error("enrich_worker_unexpected_error", error=str(exc), exc_info=True)

        # Aguarda todas as tasks de enriquecimento em andamento finalizarem
        if pending:
            logger.info("enrich_worker_draining", pending=len(pending))
            await asyncio.gather(*pending, return_exceptions=True)

        logger.info("enrich_worker_stopped")

    # ──────────────────────────── LIFECYCLE ────────────────────────────────

    async def start(self) -> None:
        """
        Inicializa todos os KlineStreams e o worker de enriquecimento.
        Deve ser chamado uma única vez pelo DataLayer.
        """
        self._running = True
        batches = self._split_symbols()

        logger.info(
            "stream_manager_starting",
            total_symbols=len(self._symbols),
            ws_connections=len(batches),
            symbols_per_conn=[len(b) for b in batches],
        )

        # Cria e inicia um KlineStream por lote
        for idx, batch in enumerate(batches):
            stream = KlineStream(
                symbols=batch,
                output_queue=self._raw_queue,
                ws_base_url=self._ws_base_url,
                interval=self._interval,
                stream_id=idx,
            )
            self._streams.append(stream)
            task = asyncio.create_task(
                stream.run(), name=f"kline_stream_{idx}"
            )
            self._tasks.append(task)

        # Worker de enriquecimento OI+LSR
        enrich_task = asyncio.create_task(
            self._enrich_worker(), name="enrich_worker"
        )
        self._tasks.append(enrich_task)

        logger.info(
            "stream_manager_started",
            streams=len(self._streams),
            total_tasks=len(self._tasks),
        )

    async def stop(self) -> None:
        """Para todos os streams e aguarda tasks pendentes finalizarem."""
        self._running = False

        # Sinaliza cada stream para parar
        for stream in self._streams:
            stream.stop()

        # Cancela e aguarda todas as tasks
        for task in self._tasks:
            if not task.done():
                task.cancel()

        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)

        logger.info("stream_manager_stopped")
