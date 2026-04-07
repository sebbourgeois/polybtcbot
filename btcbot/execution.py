"""Order execution via py-clob-client (synchronous, wrapped for async)."""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

from .config import CONFIG
from .models import Market, OpenPosition, Signal, TradeRecord

log = logging.getLogger(__name__)


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
            if resp and resp.get("orderID"):
                fill_price = float(resp.get("averagePrice", signal.poly_implied_prob))
                qty = amount_usd / fill_price if fill_price > 0 else 0
                return TradeRecord(
                    market_slug=market.slug,
                    trade_type="ENTRY",
                    direction=signal.direction,
                    token_id=token_id,
                    side="BUY",
                    amount_usd=amount_usd,
                    fill_price=fill_price,
                    token_quantity=qty,
                    signal_strength=signal.strength,
                    signal_edge=signal.edge,
                    order_id=resp.get("orderID", ""),
                )
        except Exception:
            log.warning("FOK order failed", exc_info=True)

        # Fallback: aggressive limit order
        try:
            resp = await asyncio.to_thread(
                self._limit_buy, token_id, amount_usd, signal.poly_implied_prob
            )
            if resp and resp.get("orderID"):
                # Wait briefly for fill
                await asyncio.sleep(2.0)
                fill_price = signal.poly_implied_prob + CONFIG.limit_slippage
                qty = amount_usd / fill_price if fill_price > 0 else 0
                return TradeRecord(
                    market_slug=market.slug,
                    trade_type="ENTRY",
                    direction=signal.direction,
                    token_id=token_id,
                    side="BUY",
                    amount_usd=amount_usd,
                    fill_price=fill_price,
                    token_quantity=qty,
                    signal_strength=signal.strength,
                    signal_edge=signal.edge,
                    order_id=resp.get("orderID", ""),
                )
        except Exception:
            log.error("Limit order also failed", exc_info=True)

        return None

    async def place_hedge(
        self,
        market: Market,
        position: OpenPosition,
    ) -> TradeRecord | None:
        """Buy the opposite side to hedge a losing position."""
        if not self._client:
            return None

        opposite_dir = "DOWN" if position.direction == "UP" else "UP"
        opposite_token = market.token_id_for(opposite_dir)
        # Buy enough opposite tokens to cover our position
        hedge_cost = position.token_quantity * 0.50  # approximate at ~$0.50 each

        try:
            resp = await asyncio.to_thread(
                self._fok_buy, opposite_token, hedge_cost
            )
            if resp and resp.get("orderID"):
                fill_price = float(resp.get("averagePrice", 0.50))
                qty = hedge_cost / fill_price if fill_price > 0 else 0
                return TradeRecord(
                    market_slug=market.slug,
                    trade_type="HEDGE",
                    direction=opposite_dir,
                    token_id=opposite_token,
                    side="BUY",
                    amount_usd=hedge_cost,
                    fill_price=fill_price,
                    token_quantity=qty,
                    signal_strength=0.0,
                    signal_edge=0.0,
                    order_id=resp.get("orderID", ""),
                )
        except Exception:
            log.error("Hedge order failed", exc_info=True)

        return None

    async def redeem(self, condition_id: str, max_retries: int = 5) -> str | None:
        """Redeem winning conditional tokens for USDC on-chain.

        Retries with backoff since the on-chain oracle may not have resolved
        the market yet when the bot detects the outcome.
        """
        for attempt in range(max_retries):
            try:
                tx_hash = await asyncio.to_thread(self._redeem_sync, condition_id)
                log.info("Redeemed %s — tx: %s", condition_id[:18], tx_hash)
                return tx_hash
            except Exception as e:
                if "not received yet" in str(e) and attempt < max_retries - 1:
                    delay = 30 * (attempt + 1)
                    log.info("Oracle not ready for %s — retrying in %ds", condition_id[:18], delay)
                    await asyncio.sleep(delay)
                else:
                    log.warning("Redemption failed for %s", condition_id[:18], exc_info=True)
                    return None
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
        """Synchronous FOK market buy (runs in thread)."""
        from py_clob_client.clob_types import MarketOrderArgs, OrderType

        args = MarketOrderArgs(
            token_id=token_id,
            amount=amount_usd,
            side="BUY",
        )
        signed = self._client.create_market_order(args)
        return self._client.post_order(signed, OrderType.FOK)

    def _limit_buy(
        self, token_id: str, amount_usd: float, mid_price: float
    ) -> dict | None:
        """Synchronous aggressive limit buy (runs in thread)."""
        from py_clob_client.clob_types import OrderArgs, OrderType

        price = min(mid_price + CONFIG.limit_slippage, 0.99)
        size = amount_usd / price if price > 0 else 0

        args = OrderArgs(
            token_id=token_id,
            price=price,
            size=size,
            side="BUY",
        )
        signed = self._client.create_order(args)
        return self._client.post_order(signed, OrderType.GTC)
