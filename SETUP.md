# Live / Production Setup

This guide walks you through configuring btcbot for live trading on Polymarket with real funds.

> [!CAUTION]
> Live mode places real bets with real money. The strategy has a ~60% expected win rate — roughly 40% of trades lose. Start with a small bankroll you can afford to lose.

## Prerequisites

- Python 3.11+
- btcbot installed (`pip install -e ".[dev]"`)
- A Polygon wallet with USDC
- Small amount of MATIC for the one-time USDC approval transaction (~$0.01)

---

## Step 1 — Create a Polygon Wallet

You need a standard EOA (Externally Owned Account) wallet on Polygon. This is just a regular Ethereum-style wallet — any of these work:

- **MetaMask** — export the private key from Settings → Security → Reveal Private Key
- **New wallet via Python**:
  ```bash
  python3 -c "from eth_account import Account; a = Account.create(); print(f'Address: {a.address}\nPrivate key: {a.key.hex()}')"
  ```
- **Hardware wallet** — export a derived key (less convenient for a bot)

Save the private key (a `0x`-prefixed hex string). You'll need it in Step 4.

> [!WARNING]
> Never share your private key. Never commit it to git. Use environment variables or a `.env` file with restricted permissions (`chmod 600 .env`).

---

## Step 2 — Fund the Wallet

The bot trades in **USDC on Polygon mainnet**. You need to get USDC to your wallet address.

**Option A — Bridge from Ethereum/another chain:**
- Use [Polygon Bridge](https://portal.polygon.technology/bridge) or any cross-chain bridge (Jumper, Stargate, etc.)
- Bridge USDC from Ethereum/Arbitrum/Base to Polygon

**Option B — Buy directly on Polygon:**
- Use a CEX (Coinbase, Binance) that supports Polygon USDC withdrawals
- Withdraw USDC to your wallet address on the Polygon network

**Option C — Swap on Polygon:**
- Send MATIC to your wallet, then swap for USDC on [Uniswap](https://app.uniswap.org) or [QuickSwap](https://quickswap.exchange)

**How much?** The default bankroll is $100. Start small — you can always add more later.

> [!NOTE]
> You also need a tiny amount of MATIC (< $0.01) for the one-time USDC approval transaction in Step 3. Most bridges or CEX withdrawals include enough MATIC for gas.

---

## Step 3 — Approve USDC for Polymarket

Before the bot can place orders, you must approve Polymarket's Exchange contract to spend your USDC. This is a standard ERC-20 approval — a one-time on-chain transaction.

**Contract addresses (Polygon mainnet):**

| Contract | Address |
|---|---|
| USDC | `0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174` |
| Exchange | `0x4bFb41d5B3570DeFd03C39a9A4D8dE6Bd8B8982E` |
| Neg Risk Exchange | `0xC5d563A36AE78145C45a50134d48A1215220f80a` |
| Conditional Tokens (CTF) | `0x4D97DCd97eC945f40cF65F87097ACe5EA0476045` |

**Approve via Python (recommended):**

```python
from web3 import Web3

# Connect to Polygon
w3 = Web3(Web3.HTTPProvider("https://polygon-rpc.com"))

PRIVATE_KEY = "0xYOUR_PRIVATE_KEY"
account = w3.eth.account.from_key(PRIVATE_KEY)

USDC = "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174"
EXCHANGE = "0x4bFb41d5B3570DeFd03C39a9A4D8dE6Bd8B8982E"
NEG_RISK_EXCHANGE = "0xC5d563A36AE78145C45a50134d48A1215220f80a"
CTF = "0x4D97DCd97eC945f40cF65F87097ACe5EA0476045"
MAX_APPROVAL = 2**256 - 1

# ERC-20 approve ABI (minimal)
abi = [{"inputs":[{"name":"spender","type":"address"},{"name":"amount","type":"uint256"}],
        "name":"approve","outputs":[{"name":"","type":"bool"}],"type":"function"}]

# ERC-1155 setApprovalForAll ABI (minimal)
abi_1155 = [{"inputs":[{"name":"operator","type":"address"},{"name":"approved","type":"bool"}],
             "name":"setApprovalForAll","outputs":[],"type":"function"}]

usdc = w3.eth.contract(address=USDC, abi=abi)
ctf  = w3.eth.contract(address=CTF, abi=abi_1155)

def send_tx(tx):
    tx["nonce"] = w3.eth.get_transaction_count(account.address)
    tx["gas"] = 100_000
    tx["gasPrice"] = w3.eth.gas_price
    signed = account.sign_transaction(tx)
    return w3.eth.send_raw_transaction(signed.raw_transaction).hex()

# 1. Approve USDC on Exchange
print("Approving USDC on Exchange...")
tx = usdc.functions.approve(EXCHANGE, MAX_APPROVAL).build_transaction({"from": account.address})
print(f"  tx: {send_tx(tx)}")

# 2. Approve USDC on Neg Risk Exchange
print("Approving USDC on Neg Risk Exchange...")
tx = usdc.functions.approve(NEG_RISK_EXCHANGE, MAX_APPROVAL).build_transaction({"from": account.address})
print(f"  tx: {send_tx(tx)}")

# 3. Approve CTF tokens on Exchange (for selling/merging)
print("Approving CTF on Exchange...")
tx = ctf.functions.setApprovalForAll(EXCHANGE, True).build_transaction({"from": account.address})
print(f"  tx: {send_tx(tx)}")

# 4. Approve CTF tokens on Neg Risk Exchange
print("Approving CTF on Neg Risk Exchange...")
tx = ctf.functions.setApprovalForAll(NEG_RISK_EXCHANGE, True).build_transaction({"from": account.address})
print(f"  tx: {send_tx(tx)}")

print("Done! All approvals submitted.")
```

**Approve via PolygonScan (alternative):**
1. Go to [USDC on PolygonScan](https://polygonscan.com/token/0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174#writeProxyContract)
2. Connect your wallet
3. Call `approve(spender, amount)`:
   - spender: `0x4bFb41d5B3570DeFd03C39a9A4D8dE6Bd8B8982E`
   - amount: `115792089237316195423570985008687907853269984665640564039457584007913129639935` (max uint256)
4. Repeat for the Neg Risk Exchange: `0xC5d563A36AE78145C45a50134d48A1215220f80a`

---

## Step 4 — Configure the Bot

Create your `.env` file:

```bash
cp .env.example .env
chmod 600 .env
```

Edit `.env` with your settings:

```bash
# ── REQUIRED for live trading ────────────────────────────────────────
BOT_PRIVATE_KEY=0xYOUR_PRIVATE_KEY_HERE
BOT_PAPER_MODE=false

# ── Bankroll ─────────────────────────────────────────────────────────
# Start small. This is the total capital the bot sizes positions against.
BOT_BANKROLL=100.0

# ── Risk (conservative defaults — adjust after observing paper results)
BOT_MAX_POSITION_USD=10.0        # Max $10 per trade (start low)
BOT_MAX_DAILY_LOSS_USD=30.0      # Stop after $30 daily loss
BOT_MAX_CONSECUTIVE_LOSSES=5     # Pause 15min after 5 straight losses
BOT_HEDGE_TRIGGER=0.15           # Hedge when token drops 15%

# ── Signal thresholds ────────────────────────────────────────────────
BOT_MIN_EDGE=0.06                # Require 6% edge (slightly stricter than default)
BOT_MIN_SIGNAL_STRENGTH=0.35     # Slightly higher confidence threshold

# ── Dashboard ────────────────────────────────────────────────────────
BOT_HOST=0.0.0.0
BOT_PORT=8500
```

> [!TIP]
> Start with conservative settings (`MAX_POSITION_USD=10`, `MIN_EDGE=0.06`) and widen them only after reviewing your results over a few days.

---

## Step 5 — Verify Setup

Before going live, verify your credentials work:

```bash
source .venv/bin/activate

python3 -c "
from py_clob_client.client import ClobClient
client = ClobClient('https://clob.polymarket.com', key='YOUR_PRIVATE_KEY', chain_id=137)
creds = client.create_or_derive_api_creds()
client.set_api_creds(creds)
print('API Key:', creds.api_key[:8] + '...')
print('Auth OK')
"
```

If this prints `Auth OK`, your wallet is properly set up.

---

## Step 6 — Run the Bot

```bash
# Initialise the database (if not already done)
btcbot initdb

# Start with dashboard
btcbot serve --live -v
```

Open [http://localhost:8500](http://localhost:8500) to monitor in real-time.

**Headless mode** (no dashboard, e.g. on a VPS):

```bash
btcbot run --live -v
```

---

## Running in Production

### Systemd Service (Linux)

Create `/etc/systemd/system/btcbot.service`:

```ini
[Unit]
Description=btcbot - Polymarket BTC trading bot
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=YOUR_USER
WorkingDirectory=/path/to/experiment-001
EnvironmentFile=/path/to/experiment-001/.env
ExecStart=/path/to/experiment-001/.venv/bin/btcbot serve --live
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl daemon-reload
sudo systemctl enable btcbot
sudo systemctl start btcbot

# View logs
journalctl -u btcbot -f
```

### Docker (alternative)

```dockerfile
FROM python:3.12-slim
WORKDIR /app
COPY . .
RUN pip install --no-cache-dir .
EXPOSE 8500
CMD ["btcbot", "serve", "--live"]
```

```bash
docker build -t btcbot .
docker run -d --name btcbot --env-file .env -p 8500:8500 btcbot
```

### Remote Access

To access the dashboard from another machine, either:
- **SSH tunnel**: `ssh -L 8500:localhost:8500 user@your-server`
- **Reverse proxy** (nginx/caddy) with authentication in front of it

> [!WARNING]
> Do NOT expose the dashboard to the public internet without authentication. It shows your trading activity and P&L.

---

## Monitoring

### Dashboard

The web dashboard at `http://localhost:8500` shows everything live:
- BTC price and momentum
- Active market and time remaining
- Current signal and open position
- P&L stats, trade log, hourly chart

### CLI

```bash
btcbot status       # Quick P&L summary
btcbot history      # Daily breakdown
```

### Logs

With `-v` (verbose), the bot logs every signal evaluation, trade, and resolution:

```
11:00:04 INFO  btcbot.engine: New market: btc-updown-5m-1775466300 (ends in 295s)
11:02:15 INFO  btcbot.engine: TRADE UP btc-updown-5m-1775466300 @ $0.480 ($15.00) — edge=0.082
11:05:01 INFO  btcbot.engine: Resolved btc-updown-5m-1775466300: WIN — PnL=$16.25
```

### Alerts

The bot doesn't have built-in alerts. For notifications, monitor the JSON API:

```bash
# Poll every 5 minutes, alert on big losses
curl -s http://localhost:8500/api/live | jq '.daily_pnl'
```

---

## Tuning

### Signal Sensitivity

| Parameter | More trades | Fewer trades |
|---|---|---|
| `BOT_MIN_EDGE` | Lower (0.03) | Higher (0.08) |
| `BOT_MIN_SIGNAL_STRENGTH` | Lower (0.20) | Higher (0.50) |
| `BOT_WARMUP_SEC` | Shorter (15) | Longer (45) |
| `BOT_COOLDOWN_SEC` | Shorter (30) | Longer (90) |

### Position Sizing

| Parameter | More aggressive | More conservative |
|---|---|---|
| `BOT_BANKROLL` | Higher | Lower |
| `BOT_MAX_POSITION_USD` | Higher (50) | Lower (5) |
| `BOT_MIN_POSITION_USD` | Higher (5) | Lower (1) |

### Risk

| Parameter | More risk | Less risk |
|---|---|---|
| `BOT_MAX_DAILY_LOSS_USD` | Higher (100) | Lower (20) |
| `BOT_MAX_CONSECUTIVE_LOSSES` | Higher (10) | Lower (3) |
| `BOT_MAX_PRICE_TO_PAY` | Higher (0.75) | Lower (0.55) |
| `BOT_HEDGE_TRIGGER` | Higher (0.25, hedge later) | Lower (0.10, hedge sooner) |

### BTC Volatility

`BOT_BTC_5M_VOLATILITY` (default: $30) sets the expected 5-minute BTC price swing. This normalizes the signal model:
- If BTC is volatile (big swings), increase to 40–60
- If BTC is calm (tight range), decrease to 15–20
- Wrong values lead to over/under-confident signals

---

## Troubleshooting

**"CLOB client not initialised"**
- `BOT_PRIVATE_KEY` is empty or invalid. Check your `.env` file.

**"FOK order failed"**
- Not enough liquidity at the requested price. The bot retries with a limit order.
- If both fail, the market may have very thin liquidity. This is normal — the bot skips and waits for the next window.

**"Daily loss limit hit"**
- The bot has lost more than `BOT_MAX_DAILY_LOSS_USD` today and stopped trading. It resets at midnight.

**"5 consecutive losses — pausing for 15m"**
- Normal risk management. The bot resumes automatically after 15 minutes.

**No trades being placed**
- Check that the signal is firing: run with `-v` and look for `edge=` in the logs.
- The bot only trades during the T+30s → T+240s window of each 5-minute market.
- If edges are small (< 5%), the market is efficiently priced and there's no opportunity.

**Dashboard shows $0 P&L after restart**
- This was fixed — all stats now read from the database, not in-memory counters. Make sure you're running the latest code.
