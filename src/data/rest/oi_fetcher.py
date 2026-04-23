"""
src/data/rest/oi_fetcher.py
Fetch de Open Interest atual para símbolos individuais ou em lote.

Executado pelo StreamManager imediatamente após o fechamento de cada barra (x=true).
A concorrência é controlada pelo semáforo do BinanceRestClient (padrão: 50).
"""

import asyncio
from typing import Dict, List, Optional

import structlog

from src.data.models import RawOI
from src.data.rest.binance_rest import BinanceRestClient

logger = structlog.get_logger(__name__)


class OIFetcher:
    """
    Busca Open Interest atual (spot, não histórico) via /fapi/v1/openInterest.
    Peso do endpoint: 1 por símbolo.
    """

    def __init__(self, client: BinanceRestClient):
        self._client = client

    async def fetch_one(self, symbol: str) -> Optional[RawOI]:
        """
        Busca OI atual para um único símbolo.

        Returns:
            RawOI preenchido, ou None se o request falhar.
        """
        data = await self._client.get(
            "/fapi/v1/openInterest",
            params={"symbol": symbol},
            weight=1,
        )
        if not data:
            logger.warning("oi_fetch_none", symbol=symbol)
            return None

        try:
            return RawOI(
                symbol=data["symbol"],
                ts=int(data["time"]),
                open_interest=float(data["openInterest"]),
            )
        except (KeyError, ValueError, TypeError) as exc:
            logger.warning("oi_parse_error", symbol=symbol, error=str(exc))
            return None

    async def fetch_batch(self, symbols: List[str]) -> Dict[str, Optional[RawOI]]:
        """
        Busca OI para um conjunto de símbolos em paralelo.
        O semáforo do BinanceRestClient garante que não estouraremos o rate limit.

        Returns:
            Dict {symbol: RawOI | None} — símbolos com falha retornam None.
        """
        tasks = {sym: asyncio.create_task(self.fetch_one(sym)) for sym in symbols}
        results = await asyncio.gather(*tasks.values(), return_exceptions=True)

        output: Dict[str, Optional[RawOI]] = {}
        for symbol, result in zip(tasks.keys(), results):
            if isinstance(result, Exception):
                logger.warning("oi_batch_error", symbol=symbol, error=str(result))
                output[symbol] = None
            else:
                output[symbol] = result

        return output
