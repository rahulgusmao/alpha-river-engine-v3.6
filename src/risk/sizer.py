"""
src/risk/sizer.py
Cálculo de tamanho de posição — configuração canônica v3.2.

Fórmula:
    base_usdt     = portfolio_value × risk_per_trade_pct
    position_usdt = base_usdt × delev_multiplier
    qty           = position_usdt / entry_price

Ajuste por regime OI (configuração canônica):
    DELEV       → multiplier = 0.5   (risco reduzido pela metade)
    demais       → multiplier = 1.0

Filtro de notional mínimo:
    Se position_usdt < min_notional_usdt → retorna (0.0, 0.0)
    Previne que a engine tente colocar ordens que vão falhar com -4164.
    Mínimo Binance Futures: 5 USDT para a maioria dos pares.

Nota sobre alavancagem:
    qty retornado é em base asset. A alavancagem (ex: 3×) é aplicada
    pelo ExecutionEngine ao configurar a margem na Binance Futures.
    Aqui calculamos apenas a exposição NOCIONAL desejada.
"""

import structlog

from src.features.models import OIRegime

log = structlog.get_logger(__name__)

_EPSILON = 1e-10

# Mínimo nocional Binance Futures para a maioria dos pares (USDT-M perpétuos).
# Alguns pares exigem 20 USDT — usar 5 como floor conservador.
# Fonte: Binance filter MIN_NOTIONAL / NOTIONAL (code -4164).
_BINANCE_MIN_NOTIONAL = 5.0


class Sizer:
    """
    Calcula o tamanho da posição dado o capital disponível e o regime OI.
    Stateless — pode ser chamado para qualquer sinal.
    """

    def __init__(
        self,
        risk_per_trade_pct: float = 0.01,
        delev_multiplier:   float = 0.5,
        min_notional_usdt:  float = _BINANCE_MIN_NOTIONAL,
    ):
        """
        Args:
            risk_per_trade_pct: percentual do portfólio arriscado por trade (default: 1%)
            delev_multiplier:   fator de redução em regime DELEV (default: 0.5×)
            min_notional_usdt:  notional mínimo em USDT para colocar ordem (default: 5.0)
                                Previne erros -4164 da Binance Futures API.
        """
        self._risk_pct       = risk_per_trade_pct
        self._delev_mult     = delev_multiplier
        self._min_notional   = min_notional_usdt

    def compute(
        self,
        portfolio_value: float,
        entry_price:     float,
        oi_regime:       str,
    ) -> tuple[float, float]:
        """
        Calcula o tamanho da posição em base asset e o valor nocional em USDT.

        Retorna (0.0, 0.0) se o notional calculado for menor que min_notional_usdt,
        bloqueando a ordem antes de chegar na Binance (evita cascata de erros -4164).

        Args:
            portfolio_value: capital disponível em USDT (availableBalance)
            entry_price:     preço de entrada (close do candle de gatilho)
            oi_regime:       regime OI do sinal (string, ex: 'DELEV')

        Returns:
            (qty, position_usdt) ou (0.0, 0.0) se abaixo do notional mínimo.
            qty em base asset (ex: contratos BTC)
            position_usdt = exposição nocional em USDT
        """
        base_usdt = portfolio_value * self._risk_pct

        # Reduz 50% a posição em regime DELEV
        if oi_regime == OIRegime.DELEV.value or oi_regime == OIRegime.DELEV:
            position_usdt = base_usdt * self._delev_mult
        else:
            position_usdt = base_usdt

        # Filtro de notional mínimo: bloqueia antes de chamar a API
        if position_usdt < self._min_notional:
            log.debug(
                "sizer_below_min_notional",
                position_usdt=round(position_usdt, 4),
                min_notional=self._min_notional,
                portfolio_value=round(portfolio_value, 2),
                risk_pct=self._risk_pct,
            )
            return 0.0, 0.0

        qty = position_usdt / (entry_price + _EPSILON)
        return float(qty), float(position_usdt)
