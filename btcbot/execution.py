"""Order execution via py-clob-client (synchronous, wrapped for async)."""

from __future__ import annotations

import asyncio
import datetime
import logging
import time
from pathlib import Path
from typing import Any

from .config import CONFIG
from .models import Market, OpenPosition, Signal, TradeRecord

log = logging.getLogger(__name__)

_NOT_REDEEMED_LOG = Path("notRedeemed.log")


def _log_unredeemed(msg: str) -> None:
    ts = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with _NOT_REDEEMED_LOG.open("a") as f:
        f.write(f"{ts}  {msg}\n")


def _is_fok_kill(exc: BaseException) -> bool:
    """True when an exception is a Polymarket 'FOK couldn't fill' rejection."""
    return "couldn't be fully filled" in str(exc) or "FOK orders are" in str(exc)


def _build_clob_client() -> Any:
    """Create an authenticated ClobClient. Returns None if no private key."""
    if not CONFIG.private_key:
        return None
    try:
        from py_clob_client.client import ClobClient
        from py_clob_client.clob_types import ApiCreds

        client = ClobClient(
            CONFIG.clob_api_base,
            key=CONFIG.private_key,
            chain_id=137,  # Polygon mainnet
        )
        # Derive or create API credentials
        creds = client.create_or_derive_api_creds()
        client.set_api_creds(creds)
        log.info("CLOB client authenticated")
        return client
    except Exception:
        log.error("Failed to build CLOB client", exc_info=True)
        return None


class Executor:
    """Places orders on Polymarket's CLOB exchange."""

    def __init__(self) -> None:
        self._client = _build_clob_client()

    @property
    def is_ready(self) -> bool:
        return self._client is not None

    async def place_trade(
        self,
        market: Market,
        signal: Signal,
        amount_usd: float,
    ) -> TradeRecord | None:
        """Place a market buy order for the signal direction.

        Uses Fill-or-Kill to ensure instant execution. Falls back to an
        aggressive limit order if FOK fails.
        """
        if not self._client:
            log.error("CLOB client not initialised — cannot trade")
            return None

        token_id = market.token_id_for(signal.direction)

        try:
            resp = await asyncio.to_thread(
                self._fok_buy, token_id, amount_usd
            )
            if resp:
                filled_size = float(resp["filled_size"])
                fill_price = float(resp["fill_price"])
                real_amount_usd = filled_size * fill_price
                return TradeRecord(
                    market_slug=market.slug,
                    trade_type="ENTRY",
                    direction=signal.direction,
                    token_id=token_id,
                    side="BUY",
                    amount_usd=real_amount_usd,
                    fill_price=fill_price,
                    token_quantity=filled_size,
                    signal_strength=signal.strength,
                    signal_edge=signal.edge,
                    order_id=resp["orderID"],
                )
        except Exception as e:
            # FOK rejection (couldn't fully fill) is expected — log quietly
            # and fall back to a limit order. Anything else is real.
            if _is_fok_kill(e):
                log.info("FOK unfilled — falling back to limit order")
            else:
                log.warning("FOK order failed unexpectedly", exc_info=True)

        # Fallback: aggressive limit order. _limit_buy returns real fill info
        # (or None if nothing filled) so we never record phantom trades.
        try:
            resp = await asyncio.to_thread(
                self._limit_buy, token_id, amount_usd, signal.poly_implied_prob
            )
            if resp:
                filled_size = float(resp["filled_size"])
                fill_price = float(resp["fill_price"])
                real_amount_usd = filled_size * fill_price
                return TradeRecord(
                    market_slug=market.slug,
                    trade_type="ENTRY",
                    direction=signal.direction,
                    token_id=token_id,
                    side="BUY",
                    amount_usd=real_amount_usd,
                    fill_price=fill_price,
                    token_quantity=filled_size,
                    signal_strength=signal.strength,
                    signal_edge=signal.edge,
                    order_id=resp["orderID"],
                )
        except Exception:
            log.error("Limit order also failed", exc_info=True)

        return None

    async def place_hedge(
        self,
        market: Market,
        position: OpenPosition,
        estimated_price: float | None = None,
    ) -> TradeRecord | None:
        """Buy the opposite side to hedge a losing position.

        Tries FOK first for instant fill; if it can't be fully filled, falls
        back to an aggressive GTC limit at ``est_price + limit_slippage``.
        Only returns a TradeRecord if real shares were acquired.
        """
        if not self._client:
            return None

        opposite_dir = "DOWN" if position.direction == "UP" else "UP"
        opposite_token = market.token_id_for(opposite_dir)
        est_price = estimated_price if estimated_price and estimated_price > 0 else 0.50
        hedge_cost = position.token_quantity * est_price

        def _build_hedge_record(resp: dict) -> TradeRecord:
            filled_size = float(resp["filled_size"])
            fill_price = float(resp["fill_price"])
            return TradeRecord(
                market_slug=market.slug,
                trade_type="HEDGE",
                direction=opposite_dir,
                token_id=opposite_token,
                side="BUY",
                amount_usd=filled_size * fill_price,
                fill_price=fill_price,
                token_quantity=filled_size,
                signal_strength=0.0,
                signal_edge=0.0,
                order_id=resp["orderID"],
            )

        try:
            resp = await asyncio.to_thread(
                self._fok_buy, opposite_token, hedge_cost
            )
            if resp:
                return _build_hedge_record(resp)
        except Exception as e:
            if _is_fok_kill(e):
                log.info("Hedge FOK unfilled — falling back to limit order")
            else:
                log.warning("Hedge FOK failed unexpectedly", exc_info=True)

        # Fallback: aggressive limit order so the position doesn't stay
        # unhedged on a thin book.
        try:
            resp = await asyncio.to_thread(
                self._limit_buy, opposite_token, hedge_cost, est_price
            )
            if resp:
                return _build_hedge_record(resp)
        except Exception:
            log.error("Hedge limit order also failed", exc_info=True)

        log.warning("Hedge failed for %s — position remains unprotected", market.slug)
        return None

    async def redeem(self, condition_id: str) -> str | None:
        """Redeem winning conditional tokens for USDC on-chain.

        Retries with backoff since the on-chain oracle may not have resolved
        the market yet when the bot detects the outcome.
        """
        delays = [120, 150, 180, 120, 150, 180, 120, 150, 180]
        for delay in delays:
            await asyncio.sleep(delay)
            try:
                tx_hash = await asyncio.to_thread(self._redeem_sync, condition_id)
                log.info("Redeemed %s — tx: %s", condition_id[:18], tx_hash)
                return tx_hash
            except Exception as e:
                if "not received yet" in str(e):
                    log.info("Oracle not ready for %s — retrying in %ds", condition_id[:18], delay)
                else:
                    log.warning("Redemption failed for %s", condition_id[:18], exc_info=True)
                    return None
        msg = f"{condition_id} — gave up after all retries"
        log.warning("Redemption gave up for %s after all retries", condition_id[:18])
        _log_unredeemed(msg)
        return None

    def _redeem_sync(self, condition_id: str) -> str:
        """Synchronous on-chain redemption (runs in thread)."""
        from web3 import Web3
        from web3.middleware import ExtraDataToPOAMiddleware

        w3 = Web3(Web3.HTTPProvider("https://polygon-bor-rpc.publicnode.com"))
        w3.middleware_onion.inject(ExtraDataToPOAMiddleware, layer=0)
        account = w3.eth.account.from_key(CONFIG.private_key)

        USDC = "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174"
        CTF = "0x4D97DCd97eC945f40cF65F87097ACe5EA0476045"

        abi = [{
            "inputs": [
                {"name": "collateralToken", "type": "address"},
                {"name": "parentCollectionId", "type": "bytes32"},
                {"name": "conditionId", "type": "bytes32"},
                {"name": "indexSets", "type": "uint256[]"},
            ],
            "name": "redeemPositions",
            "outputs": [],
            "type": "function",
        }]

        ctf = w3.eth.contract(address=CTF, abi=abi)
        tx = ctf.functions.redeemPositions(
            USDC,
            b"\x00" * 32,
            bytes.fromhex(condition_id[2:]) if condition_id.startswith("0x") else bytes.fromhex(condition_id),
            [1, 2],
        ).build_transaction({"from": account.address, "nonce": w3.eth.get_transaction_count(account.address)})

        signed = account.sign_transaction(tx)
        tx_hash = w3.eth.send_raw_transaction(signed.raw_transaction)
        w3.eth.wait_for_transaction_receipt(tx_hash)
        return tx_hash.hex()

    def _fok_buy(self, token_id: str, amount_usd: float) -> dict | None:
        """Synchronous FOK market buy (runs in thread).

        Posts a Fill-or-Kill market order, then queries ``get_order`` for
        the authoritative ``size_matched`` and ``price``. Returns a dict with
        ``orderID``, ``filled_size``, ``fill_price``, ``status`` — or ``None``
        if the FOK was rejected. A rejected FOK raises ``PolyApiException``
        from the underlying client, which propagates to the caller.
        """
        from py_clob_client.clob_types import MarketOrderArgs, OrderType

        args = MarketOrderArgs(
            token_id=token_id,
            amount=amount_usd,
            side="BUY",
        )
        signed = self._client.create_market_order(args)
        resp = self._client.post_order(signed, OrderType.FOK)
        if not resp or not resp.get("orderID"):
            return None

        order_id = resp["orderID"]
        filled, real_price, status = self._query_fill(order_id)
        if filled <= 0:
            # FOK succeeded per the API but get_order reported no fill —
            # treat as no trade to avoid recording a phantom.
            log.warning("FOK %s reported success but size_matched=0", order_id[:20])
            return None

        return {
            "orderID": order_id,
            "filled_size": filled,
            "fill_price": real_price,
            "status": status,
        }

    def _query_fill(self, order_id: str) -> tuple[float, float, str]:
        """Return (filled_size, fill_price, status) for ``order_id``.

        Queries ``get_order`` once. Returns zeros with status ``UNKNOWN`` if
        the lookup fails — callers must handle the zero case.
        """
        try:
            order = self._client.get_order(order_id)
        except Exception:
            log.warning("get_order failed for %s", order_id, exc_info=True)
            return 0.0, 0.0, "UNKNOWN"
        if not order:
            return 0.0, 0.0, "UNKNOWN"
        filled = float(order.get("size_matched") or 0)
        price = float(order.get("price") or 0)
        status = order.get("status") or "UNKNOWN"
        return filled, price, status

    def _limit_buy(
        self, token_id: str, amount_usd: float, mid_price: float
    ) -> dict | None:
        """Synchronous aggressive limit buy (runs in thread).

        Posts a GTC limit order, then polls ``get_order`` to read the real
        ``size_matched``. If the order partially filled, the remainder is
        canceled. If nothing filled, the order is canceled and ``None`` is
        returned so the caller does not record a phantom trade.

        Returns a dict with keys ``orderID``, ``filled_size``, ``fill_price``,
        and ``status`` reflecting the real on-exchange state.
        """
        from py_clob_client.clob_types import OrderArgs, OrderType

        price = min(mid_price + CONFIG.limit_slippage, 0.99)
        if price <= 0:
            return None
        size = amount_usd / price
        if size <= 0:
            return None

        args = OrderArgs(
            token_id=token_id,
            price=price,
            size=size,
            side="BUY",
        )
        signed = self._client.create_order(args)
        resp = self._client.post_order(signed, OrderType.GTC)
        if not resp or not resp.get("orderID"):
            return None

        order_id = resp["orderID"]

        filled = 0.0
        real_price = price
        status = "LIVE"
        for _ in range(3):
            time.sleep(1.0)
            filled, queried_price, status = self._query_fill(order_id)
            if queried_price > 0:
                real_price = queried_price
            if status == "MATCHED" or filled > 0:
                break

        if filled <= 0:
            try:
                self._client.cancel(order_id)
            except Exception:
                log.warning("Failed to cancel unfilled order %s", order_id, exc_info=True)
            log.info("Limit order %s did not fill — canceled", order_id[:20])
            return None

        if status != "MATCHED":
            try:
                self._client.cancel(order_id)
            except Exception:
                log.warning("Failed to cancel partial order %s", order_id, exc_info=True)
            log.info("Limit order %s partially filled (%.4f/%.4f)", order_id[:20], filled, size)

        return {
            "orderID": order_id,
            "filled_size": filled,
            "fill_price": real_price,
            "status": status,
        }
