"""Redeem winning Polymarket positions on-chain.

Fetches live positions from ``data-api.polymarket.com`` (source of truth,
independent of the bot's local DB) and calls ``redeemPositions`` for every
resolved market where the wallet still holds winning shares.

Default is **apply** — transactions will be sent. Pass ``--dry-run`` to
preview without touching the chain.

Safe to run repeatedly: on-chain redemption is idempotent (shares are
burned when redeemed, so a re-run finds nothing).
"""

from __future__ import annotations

import argparse
import os
import sqlite3
import sys
import time

import requests
from dotenv import load_dotenv
from eth_account import Account
from web3 import Web3
from web3.middleware import ExtraDataToPOAMiddleware

load_dotenv()

PRIVATE_KEY = os.environ["BOT_PRIVATE_KEY"]
DB_PATH = os.environ.get("BOT_DB_PATH", "./btcbot.db")

POLYGON_RPC = "https://polygon-bor-rpc.publicnode.com"
USDC = "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174"
CTF = "0x4D97DCd97eC945f40cF65F87097ACe5EA0476045"
DATA_API = "https://data-api.polymarket.com"

CTF_ABI = [{
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


def fetch_positions(wallet: str) -> list[dict]:
    """Fetch all non-dust positions for the wallet from Polymarket's data API."""
    r = requests.get(
        f"{DATA_API}/positions",
        params={"user": wallet, "sizeThreshold": 0.1},
        timeout=30,
    )
    r.raise_for_status()
    data = r.json()
    if not isinstance(data, list):
        raise RuntimeError(f"Unexpected data-api response: {data}")
    return data


def group_by_condition(positions: list[dict]) -> dict[str, dict]:
    """Collapse positions by condition_id. Returns conditions with at least
    one redeemable side that has non-zero current value."""
    conditions: dict[str, dict] = {}
    for p in positions:
        if not p.get("redeemable"):
            continue
        cid = p.get("conditionId")
        if not cid:
            continue
        size = float(p.get("size") or 0)
        if size <= 0:
            continue
        entry = conditions.setdefault(cid, {
            "conditionId": cid,
            "slug": p.get("slug") or "",
            "title": p.get("title") or "",
            "sides": [],
        })
        entry["sides"].append({
            "outcome": p.get("outcome"),
            "size": size,
            "current_value": float(p.get("currentValue") or 0),
        })

    return {
        cid: c for cid, c in conditions.items()
        if any(s["current_value"] > 0 for s in c["sides"])
    }


def redeem_condition(w3: Web3, account, condition_id: str, nonce: int) -> str:
    ctf = w3.eth.contract(address=CTF, abi=CTF_ABI)
    cid_hex = condition_id[2:] if condition_id.startswith("0x") else condition_id
    cid_bytes = bytes.fromhex(cid_hex)

    tx = ctf.functions.redeemPositions(
        USDC,
        b"\x00" * 32,
        cid_bytes,
        [1, 2],
    ).build_transaction({"from": account.address, "nonce": nonce})
    signed = account.sign_transaction(tx)
    tx_hash = w3.eth.send_raw_transaction(signed.raw_transaction)
    w3.eth.wait_for_transaction_receipt(tx_hash)
    return tx_hash.hex()


def _is_nonce_error(exc: BaseException) -> bool:
    s = str(exc).lower()
    return "nonce too low" in s or "nonce too high" in s or "replacement transaction" in s


def mark_redeemed_in_db(slug: str) -> None:
    if not os.path.exists(DB_PATH):
        return
    try:
        conn = sqlite3.connect(DB_PATH)
        cur = conn.execute(
            "UPDATE market_results SET redeemed_at = ? WHERE market_slug = ?",
            (int(time.time()), slug),
        )
        conn.commit()
        if cur.rowcount == 0:
            # No row to update — DB doesn't know about this market, fine
            pass
        conn.close()
    except Exception as e:
        print(f"  [warn] could not update DB for {slug}: {e}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true",
                        help="Preview only — do not send redemption transactions")
    args = parser.parse_args()

    account = Account.from_key(PRIVATE_KEY)
    wallet = account.address
    print(f"Wallet: {wallet}")
    print(f"Mode:   {'DRY RUN' if args.dry_run else 'APPLY'}")
    print()

    print("Fetching positions from Polymarket data-api…")
    try:
        positions = fetch_positions(wallet)
    except Exception as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 1
    print(f"  got {len(positions)} positions")
    print()

    winners = group_by_condition(positions)
    if not winners:
        print("No redeemable winning positions.")
        return 0

    total = 0.0
    print(f"Found {len(winners)} market(s) with winning positions:")
    hdr = f"  {'slug':<32} {'value':>9}  {'sides (size × side = value)':<60}"
    print(hdr)
    print("  " + "-" * (len(hdr) - 2))
    for cid, c in sorted(winners.items(), key=lambda x: -sum(s["current_value"] for s in x[1]["sides"])):
        value = sum(s["current_value"] for s in c["sides"])
        total += value
        sides_str = "  ".join(
            f"{s['size']:.2f} {s['outcome']}=${s['current_value']:.2f}"
            for s in c["sides"]
        )
        print(f"  {c['slug']:<32} ${value:>8.2f}  {sides_str}")
    print("  " + "-" * (len(hdr) - 2))
    print(f"  {'Total':<32} ${total:>8.2f}")
    print()

    if args.dry_run:
        print("Dry run — no transactions sent. Re-run without --dry-run to redeem.")
        return 0

    w3 = Web3(Web3.HTTPProvider(POLYGON_RPC))
    w3.middleware_onion.inject(ExtraDataToPOAMiddleware, layer=0)

    # Seed nonce once from the pending block and increment locally; public
    # RPCs can be slow to reflect just-mined txs in the latest block tag.
    nonce = w3.eth.get_transaction_count(account.address, "pending")

    ok = 0
    failed = 0
    for cid, c in winners.items():
        print(f"Redeeming {c['slug']}… (nonce={nonce})")
        attempt = 0
        while True:
            attempt += 1
            try:
                tx_hash = redeem_condition(w3, account, cid, nonce)
                mark_redeemed_in_db(c["slug"])
                print(f"  tx: 0x{tx_hash}")
                nonce += 1
                ok += 1
                break
            except Exception as e:
                if _is_nonce_error(e) and attempt <= 3:
                    refreshed = w3.eth.get_transaction_count(account.address, "pending")
                    print(f"  nonce error — refreshing from RPC: {nonce} → {refreshed}, retrying")
                    nonce = refreshed
                    time.sleep(1.5)
                    continue
                print(f"  FAILED: {e}")
                failed += 1
                # Resync nonce so subsequent markets aren't stuck on a stale one
                try:
                    nonce = w3.eth.get_transaction_count(account.address, "pending")
                except Exception:
                    pass
                break

    print()
    print(f"Done. {ok} redeemed, {failed} failed.")
    return 0 if failed == 0 else 2


if __name__ == "__main__":
    sys.exit(main())
