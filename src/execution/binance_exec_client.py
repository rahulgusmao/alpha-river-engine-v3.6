"""
src/execution/binance_exec_client.py
Cliente REST para execução de ordens no Binance Futures Testnet.

Endpoint base padrão: https://testnet.binancefuture.com
Autenticação:         HMAC-SHA256 sobre query string + timestamp obrigatório

Endpoints utilizados:
  POST   /fapi/v1/leverage        — define alavancagem por símbolo (uma vez)
  POST   /fapi/v1/marginType      — define ISOLATED por símbolo (uma vez)
  GET    /fapi/v1/exchangeInfo    — lot size filters (stepSize por símbolo)
  POST   /fapi/v1/order           — place MARKET entry ou STOP_MARKET SL
  DELETE /fapi/v1/order           — cancela ordem (SL quando fechamos via trailing)
  GET    /fapi/v2/account         — saldo USDT disponível

Obs sobre testnet:
  - Market data (WebSocket) continua via produção (wss://fstream.binance.com)
  - Apenas as chamadas de execução vão para testnet.binancefuture.com
  - Credenciais do testnet são separadas das de produção — nunca misturar
"""

import asyncio
import hashlib
import hmac
import math
import os
import time
from typing import Optional
from urllib.parse import urlencode

import aiohttp
import structlog

from src.execution.models import FillResult

log = structlog.get_logger(__name__)

_TAKER_FEE  = 0.0004   # 0.04% — taxa taker padrão Binance Futures testnet


class BinanceExecClient:
    """
    Cliente assíncrono para Binance Futures Testnet.

    Gerencia:
      - Autenticação HMAC-SHA256
      - Precisão de quantidade via LOT_SIZE (stepSize)
      - Setup por símbolo (leverage + margin type), idempotente
      - Colocação e cancelamento de ordens
      - Consulta de saldo

    Uso:
        client = BinanceExecClient(api_key, api_secret)
        await client.setup_symbol("BTCUSDT", leverage=3)
        fill = await client.place_market_order("BTCUSDT", "BUY", 0.001)
        sl_id = await client.place_stop_market("BTCUSDT", "SELL", 49000.0, 0.001)
    """

    def __init__(
        self,
        api_key:    str,
        api_secret: str,
        base_url:   str = "https://testnet.binancefuture.com",
    ):
        self._api_key    = api_key
        self._api_secret = api_secret
        self._base_url   = base_url.rstrip("/")
        self._session:    Optional[aiohttp.ClientSession] = None

        # Cache de stepSize por símbolo: {symbol: float}
        self._step_size:  dict[str, float] = {}
        # Símbolos já configurados (leverage + margin type): set
        self._configured: set[str] = set()
        # Lock para setup idempotente
        self._setup_lock = asyncio.Lock()

    # ── Session ──────────────────────────────────────────────────────────────

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            connector = aiohttp.TCPConnector(limit=20, ssl=True)
            self._session = aiohttp.ClientSession(
                headers={
                    "X-MBX-APIKEY":  self._api_key,
                    "Content-Type":  "application/x-www-form-urlencoded",
                },
                connector=connector,
            )
        return self._session

    # ── Autenticação ─────────────────────────────────────────────────────────

    def _sign(self, params: dict) -> str:
        """
        Injeta timestamp, constrói query string e adiciona assinatura HMAC-SHA256.
        Retorna a query string completa (params + signature) pronta para uso.
        """
        params["timestamp"] = int(time.time() * 1000)
        query = urlencode(params)
        signature = hmac.new(
            self._api_secret.encode("utf-8"),
            query.encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()
        return f"{query}&signature={signature}"

    # ── HTTP ─────────────────────────────────────────────────────────────────

    async def _request(
        self,
        method: str,
        path:   str,
        params: dict,
        signed: bool = True,
    ) -> dict:
        """
        Executa requisição com retry (3×, backoff 1s/2s/4s).
        Lança RuntimeError se todas as tentativas falharem.
        """
        session    = await self._get_session()
        signed_qs  = self._sign(params.copy()) if signed else urlencode(params)
        url        = f"{self._base_url}{path}"

        for attempt in range(3):
            try:
                if method == "GET":
                    async with session.get(f"{url}?{signed_qs}", timeout=aiohttp.ClientTimeout(total=10)) as resp:
                        data = await resp.json()
                        if resp.status not in (200, 201):
                            raise RuntimeError(
                                f"HTTP {resp.status} [{method} {path}]: "
                                f"code={data.get('code')} msg={data.get('msg', data)}"
                            )
                        return data

                elif method == "POST":
                    async with session.post(url, data=signed_qs, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                        data = await resp.json()
                        if resp.status not in (200, 201):
                            raise RuntimeError(
                                f"HTTP {resp.status} [{method} {path}]: "
                                f"code={data.get('code')} msg={data.get('msg', data)}"
                            )
                        return data

                elif method == "DELETE":
                    async with session.delete(f"{url}?{signed_qs}", timeout=aiohttp.ClientTimeout(total=10)) as resp:
                        data = await resp.json()
                        if resp.status not in (200, 201):
                            raise RuntimeError(
                                f"HTTP {resp.status} [{method} {path}]: "
                                f"code={data.get('code')} msg={data.get('msg', data)}"
                            )
                        return data

                else:
                    raise ValueError(f"Método HTTP não suportado: {method}")

            except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
                log.warning(
                    "exec_client_network_error",
                    attempt=attempt + 1, method=method, path=path, error=str(exc),
                )
                if attempt < 2:
                    await asyncio.sleep(2 ** attempt)

        raise RuntimeError(f"exec_client: falhou após 3 tentativas [{method} {path}]")

    # ── Lot Size ─────────────────────────────────────────────────────────────

    async def fetch_lot_sizes(self, symbols: list[str]) -> None:
        """
        Busca e cacheia o stepSize (precisão de quantidade) para cada símbolo.
        Deve ser chamado uma vez durante a inicialização da ExecutionEngine.
        """
        data = await self._request("GET", "/fapi/v1/exchangeInfo", {}, signed=False)
        target = set(symbols)
        loaded = 0
        for s in data.get("symbols", []):
            if s["symbol"] not in target:
                continue
            for f in s.get("filters", []):
                if f["filterType"] == "LOT_SIZE":
                    self._step_size[s["symbol"]] = float(f["stepSize"])
                    loaded += 1
                    break
        log.info("lot_sizes_loaded", loaded=loaded, requested=len(symbols))

    def _round_qty(self, symbol: str, qty: float) -> float:
        """
        Arredonda qty para baixo (floor) ao múltiplo do stepSize do símbolo.
        Usar floor em vez de round para não exceder o capital disponível.
        """
        step = self._step_size.get(symbol, 0.001)
        if step <= 0:
            step = 0.001
        rounded = math.floor(qty / step) * step
        step_str = f"{step:.10f}".rstrip("0")
        if "." in step_str:
            decimals = len(step_str.split(".")[1])
        else:
            decimals = 0
        return round(rounded, max(decimals, 0))

    def _format_qty(self, symbol: str, qty: float) -> str:
        """
        Formata qty como string para a API Binance — sem trailing zeros ou '.0'.
        Símbolos com step inteiro (ex: step=1) exigem '16082', não '16082.0'.
        """
        step = self._step_size.get(symbol, 0.001)
        if step <= 0:
            step = 0.001
        step_str = f"{step:.10f}".rstrip("0")
        if "." in step_str:
            decimals = len(step_str.split(".")[1])
        else:
            decimals = 0
        if decimals == 0:
            return str(int(qty))
        return f"{qty:.{decimals}f}"

    # ── Setup por Símbolo ────────────────────────────────────────────────────

    async def setup_symbol(
        self,
        symbol:      str,
        leverage:    int  = 3,
        margin_type: str  = "ISOLATED",
    ) -> None:
        """
        Configura leverage e tipo de margem para um símbolo antes da primeira ordem.

        Idempotente: verifica cache antes de chamar a API.
        Ignora erro -4046 da Binance ("No need to change margin type").

        Args:
            symbol:      par a configurar (ex: "BTCUSDT")
            leverage:    alavancagem desejada (1–125 dependendo do símbolo)
            margin_type: "ISOLATED" ou "CROSSED" — padrão é ISOLATED
        """
        if symbol in self._configured:
            return

        async with self._setup_lock:
            if symbol in self._configured:
                return

            # 1. Margin type
            try:
                await self._request(
                    "POST", "/fapi/v1/marginType",
                    {"symbol": symbol, "marginType": margin_type},
                )
            except RuntimeError as exc:
                err = str(exc)
                # -4046 = "No need to change margin type." — já está configurado
                if "-4046" in err or "already" in err.lower():
                    log.debug("margin_type_already_set", symbol=symbol, margin_type=margin_type)
                else:
                    raise

            # 2. Leverage
            await self._request(
                "POST", "/fapi/v1/leverage",
                {"symbol": symbol, "leverage": leverage},
            )

            self._configured.add(symbol)
            log.info(
                "symbol_configured",
                symbol=symbol, leverage=leverage, margin_type=margin_type,
            )

    # ── Ordens ───────────────────────────────────────────────────────────────

    async def place_market_order(
        self,
        symbol: str,
        side:   str,   # "BUY" | "SELL"
        qty:    float,
    ) -> FillResult:
        """
        Coloca uma ordem MARKET.
        Bloqueia até preenchimento (MARKET é síncrono na Binance Futures).

        Returns:
            FillResult com preço médio real de execução.
        """
        qty_r = self._round_qty(symbol, qty)
        if qty_r <= 0:
            raise ValueError(
                f"place_market_order: qty arredondado é zero "
                f"(qty={qty}, step={self._step_size.get(symbol, 'N/A')})"
            )

        params = {
            "symbol":         symbol,
            "side":           side,
            "type":           "MARKET",
            "quantity":       self._format_qty(symbol, qty_r),
            "newOrderRespType": "RESULT",   # garante avgPrice preenchido na resposta
        }
        data = await self._request("POST", "/fapi/v1/order", params)

        # Com newOrderRespType=RESULT, avgPrice sempre vem preenchido para MARKET
        avg_price  = float(data.get("avgPrice") or data.get("price") or 0)
        fill_qty   = float(data.get("executedQty", qty_r))
        commission = avg_price * fill_qty * _TAKER_FEE
        ts_ms      = int(data.get("updateTime", time.time() * 1000))
        order_id   = int(data["orderId"])

        result = FillResult(
            order_id=order_id, symbol=symbol, side=side,
            fill_price=avg_price, fill_qty=fill_qty,
            commission_usdt=commission, ts_ms=ts_ms,
        )
        log.info(
            "market_order_filled",
            symbol=symbol, side=side,
            qty=fill_qty, price=avg_price,
            commission=round(commission, 4),
            order_id=order_id,
        )
        return result

    async def place_stop_market(
        self,
        symbol:      str,
        side:        str,    # "SELL" para SL de LONG
        stop_price:  float,
        qty:         float,
        reduce_only: bool = True,
    ) -> int:
        """
        Coloca uma ordem STOP_MARKET para stop loss.

        Args:
            stop_price:  preço de disparo (em MARK_PRICE para evitar stop hunting)
            reduce_only: True = apenas fecha posição existente (não abre nova)

        Returns:
            order_id Binance do SL (usado para cancelamento posterior).
        """
        qty_r = self._round_qty(symbol, qty)
        if qty_r <= 0:
            raise ValueError(
                f"place_stop_market: qty arredondado é zero "
                f"(qty={qty}, step={self._step_size.get(symbol, 'N/A')})"
            )

        # Precisão do preço: 8 casas decimais cobre todos os pares
        params = {
            "symbol":      symbol,
            "side":        side,
            "type":        "STOP_MARKET",
            "stopPrice":   f"{stop_price:.4f}",
            "quantity":    self._format_qty(symbol, qty_r),
            "reduceOnly":  "true" if reduce_only else "false",
            "workingType": "MARK_PRICE",  # dispara no mark price (mais seguro que last)
        }
        data     = await self._request("POST", "/fapi/v1/order", params)
        order_id = int(data["orderId"])

        log.info(
            "sl_order_placed",
            symbol=symbol, stop_price=stop_price,
            qty=qty_r, order_id=order_id,
            working_type="MARK_PRICE",
        )
        return order_id

    async def cancel_order(self, symbol: str, order_id: int) -> bool:
        """
        Cancela uma ordem existente.
        Usado para cancelar o SL quando a posição é encerrada via trailing ou max_hold.

        Returns:
            True se cancelada com sucesso; False se já não existia (seguro ignorar).
        """
        try:
            await self._request(
                "DELETE", "/fapi/v1/order",
                {"symbol": symbol, "orderId": order_id},
            )
            log.info("order_cancelled", symbol=symbol, order_id=order_id)
            return True
        except RuntimeError as exc:
            err = str(exc)
            # -2011 = "Unknown order sent." — ordem já foi preenchida ou cancelada
            if "-2011" in err or "Unknown order" in err:
                log.debug(
                    "cancel_order_already_gone",
                    symbol=symbol, order_id=order_id,
                )
                return False
            raise

    # ── Conta ─────────────────────────────────────────────────────────────────

    async def get_usdt_balance(self) -> float:
        """
        Retorna o saldo DISPONÍVEL em USDT (availableBalance).

        Usado para: sizing de novas posições (capital real disponível para alocar).
        NÃO usar para drawdown tracking — ver get_account_summary().

        availableBalance = walletBalance - initialMargin das posições abertas.
        Quando há muitas posições abertas, este valor pode ser muito menor que
        o capital total do portfólio, distorcendo o Kill Switch se usado como base.
        """
        available, _ = await self.get_account_summary()
        return available

    async def get_account_summary(self) -> tuple[float, float]:
        """
        Retorna (available_balance, wallet_balance) em USDT.

        available_balance: capital livre para novas posições (sizing)
        wallet_balance:    capital total realizado = depósitos + PnL realizado
                           (exclui unrealized PnL, inclui margem bloqueada)
                           Usar como base do Kill Switch / drawdown tracking.

        Por que wallet_balance para o Kill Switch:
          Com 347 posições abertas, available pode ser ~335 USDT enquanto
          wallet é ~4.997 USDT. O KS precisa enxergar o capital total para
          calcular drawdown corretamente. unrealized PnL (margin_balance) é
          volátil demais para ser base de drawdown — wallet é mais estável.
        """
        data = await self._request("GET", "/fapi/v2/account", {})
        for asset in data.get("assets", []):
            if asset["asset"] == "USDT":
                available = float(asset.get("availableBalance", 0))
                wallet    = float(asset.get("walletBalance",    0))
                unrealized = float(asset.get("unrealizedProfit", 0))
                log.debug(
                    "usdt_balance_detail",
                    wallet_balance=round(wallet, 2),
                    available_balance=round(available, 2),
                    unrealized_pnl=round(unrealized, 2),
                    margin_in_use=round(wallet - available, 2),
                )
                return available, wallet
        log.warning("usdt_balance_not_found_in_account")
        return 0.0, 0.0

    async def get_open_positions(self) -> list[dict]:
        """
        Retorna todas as posições abertas com exposição != 0.

        Campos relevantes por posição:
          symbol, positionAmt, entryPrice, unrealizedProfit,
          isolatedMargin, leverage, markPrice

        Usado pelo snapshot de diagnóstico — não pelo loop principal.
        """
        data = await self._request("GET", "/fapi/v2/account", {})
        positions = [
            p for p in data.get("positions", [])
            if float(p.get("positionAmt", 0)) != 0
        ]
        log.info(
            "open_positions_fetched",
            count=len(positions),
        )
        return positions

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    async def close(self) -> None:
        """Fecha a sessão HTTP. Chamar no shutdown da engine."""
        if self._session and not self._session.closed:
            await self._session.close()
            log.debug("exec_client_session_closed")
