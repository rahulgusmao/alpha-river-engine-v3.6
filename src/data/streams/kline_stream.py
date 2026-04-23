"""
src/data/streams/kline_stream.py
WebSocket stream de klines 15m para um lote de até 300 símbolos.

Usa o combined stream da Binance Futures:
  wss://fstream.binance.com/stream?streams=btcusdt@kline_15m/ethusdt@kline_15m/...

Emite RawCandle na output_queue SOMENTE quando a barra está fechada (k.x == true).
O campo V (taker_buy_base_vol) é capturado diretamente do evento — sem aggTrades.

Reconexão automática com backoff exponencial em caso de queda da conexão.
"""

import asyncio
import json
from typing import List, Optional

import aiohttp
import structlog

from src.data.models import RawCandle

logger = structlog.get_logger(__name__)

# Limite de streams por conexão WebSocket (restrição da Binance)
MAX_STREAMS_PER_CONNECTION = 300

# Parâmetros de reconexão
_RECONNECT_DELAY_INITIAL = 1.0   # segundos
_RECONNECT_DELAY_MAX = 60.0      # segundos
_RECONNECT_MULTIPLIER = 2.0

# Heartbeat WebSocket (ping/pong automático via aiohttp)
_WS_HEARTBEAT = 30.0
# Timeout de recebimento — se nenhuma mensagem em N segundos, reconecta
_WS_RECEIVE_TIMEOUT = 60.0


class KlineStream:
    """
    Mantém uma conexão WebSocket para um lote de símbolos.

    Cada instância gerencia exatamente uma conexão WS (até 300 streams).
    Para universos maiores, o StreamManager instancia múltiplos KlineStreams.

    Thread-safety: não thread-safe — uso exclusivo via asyncio.
    """

    def __init__(
        self,
        symbols: List[str],
        output_queue: asyncio.Queue,
        ws_base_url: str,
        interval: str = "15m",
        stream_id: int = 0,
    ):
        """
        Args:
            symbols:      lista de símbolos (ex: ['BTCUSDT', 'ETHUSDT'])
            output_queue: fila de saída onde os RawCandle fechados são depositados
            ws_base_url:  URL base do WebSocket (ex: 'wss://fstream.binance.com')
            interval:     intervalo do kline (ex: '15m')
            stream_id:    identificador da conexão (para logging)
        """
        if len(symbols) > MAX_STREAMS_PER_CONNECTION:
            raise ValueError(
                f"KlineStream suporta no máximo {MAX_STREAMS_PER_CONNECTION} símbolos. "
                f"Recebidos: {len(symbols)}. Use StreamManager para dividir o universo."
            )

        self._symbols = symbols
        self._output_queue = output_queue
        self._ws_base_url = ws_base_url.rstrip("/")
        self._interval = interval
        self._stream_id = stream_id
        self._running = False
        self._reconnect_count = 0

    # ──────────────────────────── URL ──────────────────────────────────────

    def _build_url(self) -> str:
        """
        Monta a URL do combined stream com todos os símbolos do lote.
        Formato: wss://host/stream?streams=sym1@kline_15m/sym2@kline_15m/...
        """
        stream_names = "/".join(
            f"{sym.lower()}@kline_{self._interval}" for sym in self._symbols
        )
        return f"{self._ws_base_url}/stream?streams={stream_names}"

    # ──────────────────────────── PARSING ──────────────────────────────────

    def _parse_event(self, raw: dict) -> Optional[RawCandle]:
        """
        Parseia um evento do combined stream.

        Estrutura do evento (combined stream):
          { "stream": "btcusdt@kline_15m", "data": { "e": "kline", "k": {...} } }

        Retorna RawCandle apenas quando k.x == True (barra fechada).
        Retorna None para barras ainda abertas (descartadas silenciosamente).
        """
        try:
            k = raw["data"]["k"]

            # Ignora eventos de barras ainda abertas (alta frequência, sem valor)
            if not k.get("x", False):
                return None

            return RawCandle(
                symbol=k["s"],
                ts_open=int(k["t"]),
                ts_close=int(k["T"]),
                open=float(k["o"]),
                high=float(k["h"]),
                low=float(k["l"]),
                close=float(k["c"]),
                volume=float(k["v"]),
                taker_buy_base_vol=float(k["V"]),   # campo V = CVD source
                num_trades=int(k["n"]),
                interval=k["i"],
            )

        except (KeyError, ValueError, TypeError) as exc:
            # Log de warning apenas — não levanta exceção para não quebrar o stream
            logger.warning(
                "kline_parse_error",
                stream_id=self._stream_id,
                error=str(exc),
                raw_keys=list(raw.keys()) if isinstance(raw, dict) else "?",
            )
            return None

    # ──────────────────────────── CONEXÃO ──────────────────────────────────

    async def _connect_and_listen(self) -> None:
        """
        Abre e mantém uma sessão WebSocket até desconexão ou erro.
        Levanta exceção em caso de falha de rede para que o loop de
        reconexão externo possa tratar.
        """
        url = self._build_url()
        logger.info(
            "ws_connecting",
            stream_id=self._stream_id,
            symbols=len(self._symbols),
            first_symbol=self._symbols[0] if self._symbols else "?",
        )

        async with aiohttp.ClientSession() as session:
            async with session.ws_connect(
                url,
                heartbeat=_WS_HEARTBEAT,
                receive_timeout=_WS_RECEIVE_TIMEOUT,
            ) as ws:
                # Conexão estabelecida — reset do contador de reconexão
                self._reconnect_count = 0
                logger.info(
                    "ws_connected",
                    stream_id=self._stream_id,
                    symbols=len(self._symbols),
                )

                async for msg in ws:
                    if msg.type == aiohttp.WSMsgType.TEXT:
                        try:
                            raw = json.loads(msg.data)
                        except json.JSONDecodeError as exc:
                            logger.warning(
                                "ws_json_error",
                                stream_id=self._stream_id,
                                error=str(exc),
                            )
                            continue

                        candle = self._parse_event(raw)
                        if candle is not None:
                            # Deposita na fila de saída (não bloqueia se cheia — put_nowait)
                            try:
                                self._output_queue.put_nowait(candle)
                            except asyncio.QueueFull:
                                logger.warning(
                                    "output_queue_full",
                                    stream_id=self._stream_id,
                                    symbol=candle.symbol,
                                )
                                # Bloqueia até conseguir — backpressure intencional
                                await self._output_queue.put(candle)

                    elif msg.type == aiohttp.WSMsgType.PING:
                        # aiohttp responde PONG automaticamente via heartbeat
                        pass

                    elif msg.type == aiohttp.WSMsgType.ERROR:
                        logger.error(
                            "ws_message_error",
                            stream_id=self._stream_id,
                            error=str(ws.exception()),
                        )
                        break

                    elif msg.type in (
                        aiohttp.WSMsgType.CLOSE,
                        aiohttp.WSMsgType.CLOSING,
                    ):
                        logger.warning(
                            "ws_closed_by_server",
                            stream_id=self._stream_id,
                            close_code=ws.close_code,
                        )
                        break

    # ──────────────────────────── LOOP PRINCIPAL ───────────────────────────

    async def run(self) -> None:
        """
        Loop principal com reconexão automática e backoff exponencial.
        Chamar como: asyncio.create_task(stream.run())

        Encerra quando stop() é chamado ou a task é cancelada.
        """
        self._running = True
        delay = _RECONNECT_DELAY_INITIAL

        while self._running:
            try:
                await self._connect_and_listen()

            except asyncio.CancelledError:
                logger.info("ws_cancelled", stream_id=self._stream_id)
                break

            except (aiohttp.ClientError, asyncio.TimeoutError, OSError,
                    ConnectionError) as exc:
                self._reconnect_count += 1
                logger.warning(
                    "ws_disconnected",
                    stream_id=self._stream_id,
                    error=str(exc),
                    reconnect_attempt=self._reconnect_count,
                    next_retry_sec=delay,
                )

            except Exception as exc:
                # Erro inesperado (bug de programação) — loga com stack trace e re-levanta
                # para que o erro não fique mascarado como desconexão de rede.
                logger.error(
                    "ws_unexpected_error",
                    stream_id=self._stream_id,
                    error=str(exc),
                    reconnect_attempt=self._reconnect_count,
                    exc_info=True,
                )
                raise

            if not self._running:
                break

            # Backoff exponencial: 1s → 2s → 4s → ... → 60s
            await asyncio.sleep(delay)
            delay = min(delay * _RECONNECT_MULTIPLIER, _RECONNECT_DELAY_MAX)

        logger.info("ws_stream_stopped", stream_id=self._stream_id)

    def stop(self) -> None:
        """Sinaliza para o loop principal encerrar após a próxima reconexão."""
        self._running = False
