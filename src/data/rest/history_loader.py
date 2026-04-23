"""
src/data/rest/history_loader.py
Warm-up histórico via REST para inicializar as rolling windows do Feature Engine.

Responsabilidades:
  - Descobrir o universo completo de símbolos USDT perpétuos (exchangeInfo)
  - Carregar histórico de klines com o campo V (takerBuyBaseAssetVolume)
  - Carregar histórico de OI via /futures/data/openInterestHist
  - Carregar histórico de LSR via /futures/data/globalLongShortAccountRatio
  - Orquestrar o warm-up completo por símbolo em paralelo

Nota sobre OI/LSR histórico:
  A Binance fornece histórico de OI e LSR alinhado a períodos específicos
  (1m, 3m, 5m, 15m, 30m, 1h, 2h, 4h, 6h, 12h, 1d). Usamos '15m'.
  Limite por request: 500 registros. Máximo histórico disponível: 30 dias.
"""

import asyncio
from typing import Dict, List, Optional

import structlog

from src.data.models import RawCandle, RawOI, RawLSR
from src.data.rest.binance_rest import BinanceRestClient

logger = structlog.get_logger(__name__)

# Limite máximo de klines por request da API Binance
_MAX_KLINES_PER_REQUEST = 1500
# Limite máximo de OI/LSR histórico por request
_MAX_OI_LSR_PER_REQUEST = 500


class HistoryLoader:
    """
    Carrega dados históricos via REST para aquecer as rolling windows.
    Também responsável por descobrir o universo de símbolos elegíveis.
    """

    def __init__(self, client: BinanceRestClient, interval: str = "15m"):
        self._client = client
        self._interval = interval

    # ──────────────────────────── UNIVERSO ─────────────────────────────────

    async def fetch_universe(self, quote_asset: str = "USDT") -> List[str]:
        """
        Retorna todos os símbolos de futuros perpétuos em USDT com status TRADING.
        Usa /fapi/v1/exchangeInfo (peso: 40).

        Returns:
            Lista de símbolos ordenada alfabeticamente, ex: ['AAVEUSDT', 'ADAUSDT', ...]
        """
        data = await self._client.get("/fapi/v1/exchangeInfo", weight=40)
        if not data:
            logger.error("fetch_universe_failed", reason="API retornou None")
            return []

        symbols = sorted([
            s["symbol"]
            for s in data.get("symbols", [])
            if (
                s.get("quoteAsset") == quote_asset
                and s.get("contractType") == "PERPETUAL"
                and s.get("status") == "TRADING"
            )
        ])

        logger.info("universe_fetched", count=len(symbols), quote_asset=quote_asset)
        return symbols

    # ──────────────────────────── KLINES ───────────────────────────────────

    async def load_klines(self, symbol: str, limit: int = 700) -> List[RawCandle]:
        """
        Carrega os últimos `limit` candles fechados de um símbolo via REST.
        Pagina automaticamente se limit > 1500.

        Returns:
            Lista de RawCandle ordenada do mais antigo ao mais recente.
        """
        if limit <= _MAX_KLINES_PER_REQUEST:
            data = await self._client.get(
                "/fapi/v1/klines",
                params={"symbol": symbol, "interval": self._interval, "limit": limit},
                weight=2,
            )
            if not data:
                logger.warning("klines_empty", symbol=symbol)
                return []
            return [self._parse_kline(symbol, k) for k in data]

        # Paginação retrógrada para limits > 1500
        return await self._load_klines_paginated(symbol, limit)

    async def _load_klines_paginated(self, symbol: str, total: int) -> List[RawCandle]:
        """Carrega klines em múltiplas páginas, trabalhando de trás para frente."""
        all_candles: List[RawCandle] = []
        end_time: Optional[int] = None
        remaining = total

        while remaining > 0:
            batch = min(remaining, _MAX_KLINES_PER_REQUEST)
            params: Dict = {"symbol": symbol, "interval": self._interval, "limit": batch}
            if end_time is not None:
                params["endTime"] = end_time

            data = await self._client.get("/fapi/v1/klines", params=params, weight=2)
            if not data:
                break

            parsed = [self._parse_kline(symbol, k) for k in data]
            all_candles = parsed + all_candles  # mais antigos na frente
            end_time = int(data[0][0]) - 1       # ts_open do mais antigo menos 1ms
            remaining -= len(parsed)

            if len(parsed) < batch:
                break  # chegou no início do histórico disponível

        return all_candles

    def _parse_kline(self, symbol: str, k: list) -> RawCandle:
        """
        Converte a lista de campos da API REST de klines em RawCandle.

        Índices da API Binance Futures klines:
          0=ts_open, 1=open, 2=high, 3=low, 4=close, 5=volume,
          6=ts_close, 7=quote_vol, 8=num_trades,
          9=taker_buy_base_vol, 10=taker_buy_quote_vol, 11=ignore
        """
        return RawCandle(
            symbol=symbol,
            ts_open=int(k[0]),
            ts_close=int(k[6]),
            open=float(k[1]),
            high=float(k[2]),
            low=float(k[3]),
            close=float(k[4]),
            volume=float(k[5]),
            taker_buy_base_vol=float(k[9]),   # campo V — fonte do CVD
            num_trades=int(k[8]),
            interval=self._interval,
        )

    # ──────────────────────────── OI HISTÓRICO ─────────────────────────────

    async def load_oi_history(self, symbol: str, limit: int = 700) -> List[RawOI]:
        """
        Carrega histórico de Open Interest via /futures/data/openInterestHist.
        Período alinhado ao intervalo do kline ('15m').
        Binance limita a 500 registros por request e ~30 dias de histórico.

        Returns:
            Lista de RawOI do mais antigo ao mais recente.
        """
        all_oi: List[RawOI] = []
        end_time: Optional[int] = None
        remaining = limit

        while remaining > 0:
            batch = min(remaining, _MAX_OI_LSR_PER_REQUEST)
            params: Dict = {
                "symbol": symbol,
                "period": self._interval,
                "limit": batch,
            }
            if end_time is not None:
                params["endTime"] = end_time

            data = await self._client.get(
                "/futures/data/openInterestHist", params=params, weight=1
            )
            if not data or not isinstance(data, list):
                break

            parsed = [
                RawOI(
                    symbol=item["symbol"],
                    ts=int(item["timestamp"]),
                    open_interest=float(item["sumOpenInterest"]),
                )
                for item in data
            ]
            all_oi = parsed + all_oi
            end_time = int(data[0]["timestamp"]) - 1
            remaining -= len(parsed)

            if len(parsed) < batch:
                break

        return all_oi

    # ──────────────────────────── LSR HISTÓRICO ────────────────────────────

    async def load_lsr_history(self, symbol: str, limit: int = 700) -> List[RawLSR]:
        """
        Carrega histórico de Long/Short Ratio via /futures/data/globalLongShortAccountRatio.
        Retorna razão de contas globais (não apenas top traders).

        Returns:
            Lista de RawLSR do mais antigo ao mais recente.
        """
        all_lsr: List[RawLSR] = []
        end_time: Optional[int] = None
        remaining = limit

        while remaining > 0:
            batch = min(remaining, _MAX_OI_LSR_PER_REQUEST)
            params: Dict = {
                "symbol": symbol,
                "period": self._interval,
                "limit": batch,
            }
            if end_time is not None:
                params["endTime"] = end_time

            data = await self._client.get(
                "/futures/data/globalLongShortAccountRatio", params=params, weight=1
            )
            if not data or not isinstance(data, list):
                break

            parsed = [
                RawLSR(
                    symbol=item["symbol"],
                    ts=int(item["timestamp"]),
                    long_short_ratio=float(item["longShortRatio"]),
                )
                for item in data
            ]
            all_lsr = parsed + all_lsr
            end_time = int(data[0]["timestamp"]) - 1
            remaining -= len(parsed)

            if len(parsed) < batch:
                break

        return all_lsr

    # ──────────────────────────── WARM-UP COMPLETO ─────────────────────────

    async def warm_up_symbol(self, symbol: str, candles: int = 700) -> Dict:
        """
        Carrega histórico completo (klines + OI + LSR) para um símbolo em paralelo.

        Returns:
            {
                'klines': List[RawCandle],
                'oi':     List[RawOI],
                'lsr':    List[RawLSR],
            }
        """
        klines_task = asyncio.create_task(self.load_klines(symbol, candles))
        oi_task = asyncio.create_task(self.load_oi_history(symbol, candles))
        lsr_task = asyncio.create_task(self.load_lsr_history(symbol, candles))

        klines, oi, lsr = await asyncio.gather(
            klines_task, oi_task, lsr_task, return_exceptions=True
        )

        # Trata erros individuais sem deixar o warm-up explodir
        result: Dict = {}
        result["klines"] = klines if not isinstance(klines, Exception) else []
        result["oi"] = oi if not isinstance(oi, Exception) else []
        result["lsr"] = lsr if not isinstance(lsr, Exception) else []

        logger.debug(
            "warm_up_symbol_done",
            symbol=symbol,
            klines=len(result["klines"]),
            oi=len(result["oi"]),
            lsr=len(result["lsr"]),
        )
        return result
