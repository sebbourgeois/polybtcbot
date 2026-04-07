"""Redeem winning conditional tokens from resolved Polymarket markets."""

import sqlite3
import sys

from dotenv import load_dotenv
load_dotenv()

import os
from web3 import Web3
from web3.middleware import ExtraDataToPOAMiddleware

PRIVATE_KEY = os.environ["BOT_PRIVATE_KEY"]
DB_PATH = os.environ.get("BOT_DB_PATH", "./btcbot.db")

# Polygon mainnet
w3 = Web3(Web3.HTTPProvider("https://polygon-bor-rpc.publicnode.com"))
w3.middleware_onion.inject(ExtraDataToPOAMiddleware, layer=0)
account = w3.eth.account.from_key(PRIVATE_KEY)

USDC = "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174"
CTF = "0x4D97DCd97eC945f40cF65F87097ACe5EA0476045"

# CTF redeemPositions ABI
CTF_ABI = [
    {
        "inputs": [
            {"name": "collateralToken", "type": "address"},
            {"name": "parentCollectionId", "type": "bytes32"},
            {"name": "conditionId", "type": "bytes32"},
            {"name": "indexSets", "type": "uint256[]"},
        ],
        "name": "redeemPositions",
        "outputs": [],
        "type": "function",
    }
]

ctf = w3.eth.contract(address=CTF, abi=CTF_ABI)
nonce = w3.eth.get_transaction_count(account.address)


def find_redeemable_markets() -> list[tuple[str, str]]:
    """Return (slug, condition_id) for won live markets."""
    conn = sqlite3.connect(DB_PATH)
    rows = conn.execute(
        """
        SELECT DISTINCT t.market_slug, m.condition_id, t.direction, m.outcome
        FROM trades t
        JOIN markets m ON t.market_slug = m.slug
        WHERE t.trade_type = 'ENTRY'
          AND t.is_paper = 0
          AND m.outcome IS NOT NULL
          AND t.direction = m.outcome
        """,
    ).fetchall()
    conn.close()
    return [(r[0], r[1]) for r in rows]


def redeem(slug: str, condition_id: str) -> str:
    global nonce
    # Binary market: indexSets [1, 2] covers both outcome slots.
    # The contract only pays out for the winning side.
    tx = ctf.functions.redeemPositions(
        USDC,
        b"\x00" * 32,  # parentCollectionId (top-level)
        bytes.fromhex(condition_id[2:]),  # strip 0x prefix
        [1, 2],
    ).build_transaction({"from": account.address, "nonce": nonce})

    signed = account.sign_transaction(tx)
    tx_hash = w3.eth.send_raw_transaction(signed.raw_transaction)
    w3.eth.wait_for_transaction_receipt(tx_hash)
    nonce += 1
    return tx_hash.hex()


def main():
    markets = find_redeemable_markets()
    if not markets:
        print("No redeemable markets found.")
        sys.exit(0)

    print(f"Found {len(markets)} winning market(s) to redeem:\n")
    for slug, cid in markets:
        print(f"  {slug}  {cid[:18]}...")

    print()
    for slug, cid in markets:
        print(f"Redeeming {slug}...")
        try:
            tx_hash = redeem(slug, cid)
            print(f"  tx: {tx_hash}")
        except Exception as e:
            print(f"  FAILED: {e}")

    print("\nDone.")


if __name__ == "__main__":
    main()
