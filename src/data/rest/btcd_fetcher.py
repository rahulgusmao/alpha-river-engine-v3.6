"""
src/data/rest/btcd_fetcher.py
Polling de BTC Dominance via CoinGecko API para uso no Kill Switch.

IMPORTANTE: Este fetcher NÃO usa o BinanceRestClient — a CoinGecko tem
endpoints e limites próprios. Usa sessão aiohttp independente.

CoinGecko Free Tier:
  - Rate limit: ~30 req/min
  - Delay de dados: ~5 minutos (aceitável para kill switch)
  - Endpoint: GET /api/v3/global

Estratégia de polling: a cada 5 minutos (alinhado ao delay da CoinGecko).
"""

import asyncio
import time
from typing import Callable, Coroutine, Optional

import aiohttp
import structlog

from src.data.models import RawBTCD

logger = structlog.get_logger(__name__)

_COINGECKO_URL = "https://api.coingecko.com/api/v3/global"
_POLL_INTERVAL_SEC = 300  # 5 minutos


class BTCDFetcher:
    """
    Busca BTC Dominance da CoinGecko periodicamente e notifica via callback.

    Uso:
        fetcher = BTCDFetcher(on_update=meu_handler)
        task = asyncio.create_task(fetcher.run())
        ...
        fetcher.stop()

    O callback on_update é chamado com o RawBTCD mais recente a cada poll bem-sucedido.
    Acesso síncrono ao último valor: fetcher.latest
    """

    def __init__(
        self,
        on_update: Optional[Callable[[RawBTCD], Coroutine]] = None,
        poll_interval_sec: int = _POLL_INTERVAL_SEC,
    ):
        """
        Args:
            on_update:         coroutine chamada com RawBTCD quando há novo dado
            poll_interval_sec: intervalo de polling em segundos
        """
        self._on_update = on_update
        self._poll_interval = poll_interval_sec
        self._latest: Optional[RawBTCD] = None
        self._running = False

    @property
    def latest(self) -> Optional[RawBTCD]:
        """Último valor de BTC.D disponível. Pode ser None antes do primeiro poll."""
        return self._latest

    async def _fetch_once(self) -> Optional[RawBTCD]:
        """Executa um request à CoinGecko e retorna RawBTCD."""
        try:
            async with aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=15)
            ) as session:
                async with session.get(_COINGECKO_URL) as resp:
                    if resp.status != 200:
                        logger.warning(
                            "btcd_http_error",
                            status=resp.status,
                            url=_COINGECKO_URL,
                        )
                        return None

                    data = await resp.json()
                    btc_pct = (
                        data.get("data", {})
                        .get("market_cap_percentage", {})
                        .get("btc")
                    )
                    if btc_pct is None:
                        logger.warning("btcd_field_missing", data_keys=list(data.keys()))
                        return None

                    return RawBTCD(
                        ts=int(time.time() * 1000),
                        btc_dominance=float(btc_pct),
                    )

        except asyncio.TimeoutError:
            logger.warning("btcd_timeout", url=_COINGECKO_URL)
            return None
        except aiohttp.ClientError as exc:
            logger.warning("btcd_client_error", error=str(exc))
            return None
        except Exception as exc:
            logger.error("btcd_unexpected_error", error=str(exc))
            return None

    async def run(self) -> None:
        """
        Loop principal de polling. Chamar como asyncio.create_task(fetcher.run()).
        Executa indefinidamente até stop() ser chamado.
        """
        self._running = True
        logger.info("btcd_fetcher_started", interval_sec=self._poll_interval)

        while self._running:
            result = await self._fetch_once()

            if result is not None:
                self._latest = result
                logger.debug(
                    "btcd_updated",
                    dominance_pct=round(result.btc_dominance, 2),
                )
                # Notifica o Kill Switch via callback (se configurado)
                if self._on_update is not None:
                    try:
                        await self._on_update(result)
                    except Exception as exc:
                        logger.warning("btcd_callback_error", error=str(exc))

            try:
                await asyncio.sleep(self._poll_interval)
            except asyncio.CancelledError:
                break

        logger.info("btcd_fetcher_stopped")

    def stop(self) -> None:
        """Sinaliza para o loop de polling encerrar na próxima iteração."""
        self._running = False
