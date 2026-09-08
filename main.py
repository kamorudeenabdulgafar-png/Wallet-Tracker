#!/usr/bin/env python3
"""
Wallet + Coin Tracker (Solana) — v2
------------------------------------
Everything is controllable from Telegram. No code edits needed to add/remove
wallets. Runs on a schedule via GitHub Actions.

FEATURES
  1. Wallet tracking: alerts on new coin creations + buys/swaps from any
     wallet you're tracking (dev wallets, trader wallets, whatever), plus
     real realized profit/loss per wallet (only for positions we actually
     saw opened — no guessing at cost basis we never observed).
  2. Telegram commands (checked every run):
       /add <address> <label>     -> start tracking a wallet
       /remove <address>          -> stop tracking a wallet
       /list                      -> see everything currently tracked
       /watch <CA>                -> check a coin right now + get rug alerts on it
       /unwatch <CA>              -> stop watching a coin
       /watchlist                 -> see everything currently watched
       /setminliquidity <amount>  -> tune the scanner's liquidity floor
       /setminchange <percent>    -> tune the scanner's momentum threshold
  3. Coin scanner: scans newly graduated pump.fun/PumpSwap pools for
     objective signs of traction and alerts on ones that clear a bar — NOT
     a guarantee, just a filter to point your own research at fewer,
     better candidates. Every flagged coin gets checked again at 1h/4h/24h
     so the scanner's real track record (not just alerts fired) is visible.
  4. Rug alerts: any /watch'd coin gets an urgent alert if its liquidity
     suddenly drops a lot — a classic rug-pull warning sign.
  5. Daily digest: one summary message a day covering wallet activity,
     scanner accuracy, and your watchlist.
  6. Writes docs/data.json each run, which the dashboard (docs/index.html)
     reads to show wallets + recent alerts + scanner hits + watchlist.

SECURITY: no keys are hardcoded. Everything comes from environment variables
(set as GitHub Secrets): HELIUS_API_KEY, TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID
"""

import requests
import time
import json
import os
import sys

# ================= CONFIG =================
HELIUS_API_KEY = os.environ.get("HELIUS_API_KEY")
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")

SOL_MINT = "So11111111111111111111111111111111111111112"
STABLECOIN_MINTS = {
    "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v": "USDC",
    "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB": "USDT",
}

FETCH_LIMIT = 20

WALLETS_FILE = "wallets.json"
SEEN_FILE = "seen_signatures.json"
TG_OFFSET_FILE = "telegram_offset.json"
SCANNER_SEEN_FILE = "scanner_seen.json"
ALERTS_LOG_FILE = "alerts_log.json"
WALLET_STATS_FILE = "wallet_stats.json"
SIGNAL_OUTCOMES_FILE = "signal_outcomes.json"
RECENT_BUYS_FILE = "recent_buys.json"
WATCHED_COINS_FILE = "watched_coins.json"
SETTINGS_FILE = "settings.json"
OUTCOME_HISTORY_FILE = "outcome_history.json"
DASHBOARD_FILE = "docs/data.json"

# How long after a flagged coin do we check back on it ("did it pan out?")
OUTCOME_CHECKPOINTS_MIN = [60, 240, 1440]  # 1h, 4h, 24h
SMART_MONEY_WINDOW_HOURS = 6  # how far back a tracked-wallet buy still counts as "smart money"
RUG_ALERT_DROP_PCT = 50  # liquidity drop (%) since last check that triggers an urgent alert

# Coin scanner thresholds — tune these once we see real output, or override via
# Telegram (/setminliquidity, /setminchange), which take priority when set.
DEFAULT_MIN_LIQUIDITY_USD = 5000   # too little liquidity = can't exit without huge slippage
DEFAULT_MIN_1H_CHANGE_PCT = 15     # must be genuinely accelerating, not just sitting there
SCANNER_MIN_AGE_MIN = 5         # skip brand-new coins with no track record yet
SCANNER_MAX_AGE_MIN = 180        # skip old coins that likely already had their run
MAX_ALERTS_LOGGED = 50
# ============================================

_sol_price_cache = {"price": None, "ts": 0}


def check_config():
    missing = [n for n, v in [
        ("HELIUS_API_KEY", HELIUS_API_KEY),
        ("TELEGRAM_BOT_TOKEN", TELEGRAM_BOT_TOKEN),
        ("TELEGRAM_CHAT_ID", TELEGRAM_CHAT_ID),
    ] if not v]
    if missing:
        print(f"[fatal] Missing environment variables: {', '.join(missing)}")
        sys.exit(1)


# ---------------- generic JSON file helpers ----------------

def load_json(path, default):
    if os.path.exists(path):
        try:
            with open(path) as f:
                return json.load(f)
        except Exception:
            return default
    return default


def save_json(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True) if os.path.dirname(path) else None
    with open(path, "w") as f:
        json.dump(data, f, indent=2)


# ---------------- Telegram ----------------

def send_telegram(message):
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    try:
        requests.post(url, data={
            "chat_id": TELEGRAM_CHAT_ID,
            "text": message,
            "disable_web_page_preview": True,
        }, timeout=15)
    except Exception as e:
        print(f"[error] telegram send failed: {e}")


def is_plausible_solana_address(addr):
    if not addr or not (32 <= len(addr) <= 44):
        return False
    alphabet = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"
    return all(c in alphabet for c in addr)


def get_scanner_setting(settings, key, default):
    return settings.get(key, default)


def process_telegram_commands(wallets, watched_coins, settings):
    """Check for new commands since the last run: wallet add/remove/list,
    coin watch/unwatch/watchlist, and scanner threshold overrides."""
    offset_data = load_json(TG_OFFSET_FILE, {"offset": 0})
    offset = offset_data.get("offset", 0)

    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/getUpdates"
    try:
        resp = requests.get(url, params={"offset": offset, "timeout": 0}, timeout=15)
        result = resp.json()
        updates = result.get("result", [])
        print(f"[telegram] fetched {len(updates)} update(s) since offset {offset}")
        if not result.get("ok"):
            print(f"[telegram] getUpdates returned an error: {result}")
    except Exception as e:
        print(f"[error] telegram getUpdates failed: {e}")
        return wallets, watched_coins, settings

    wallets_changed = False
    watched_changed = False
    settings_changed = False

    for update in updates:
        offset = max(offset, update.get("update_id", 0) + 1)
        msg = update.get("message", {})
        text = (msg.get("text") or "").strip()
        print(f"[telegram] saw message: {text!r}")
        if not text.startswith("/"):
            continue

        parts = text.split(maxsplit=2)
        cmd = parts[0].lower()

        if cmd == "/add" and len(parts) >= 2:
            address = parts[1].strip()
            label = parts[2].strip() if len(parts) >= 3 else "Unlabeled wallet"
            if not is_plausible_solana_address(address):
                send_telegram(f"⚠️ That doesn't look like a valid Solana address:\n{address}")
                continue
            if any(w["address"] == address for w in wallets):
                send_telegram(f"Already tracking that wallet ({label}).")
                continue
            wallets.append({"label": label, "address": address})
            wallets_changed = True
            send_telegram(f"✅ Now tracking: {label}\n{address}")

        elif cmd == "/remove" and len(parts) >= 2:
            address = parts[1].strip()
            before = len(wallets)
            wallets = [w for w in wallets if w["address"] != address]
            if len(wallets) < before:
                wallets_changed = True
                send_telegram(f"🗑️ Stopped tracking:\n{address}")
            else:
                send_telegram(f"Couldn't find that address in your tracked list.")

        elif cmd == "/list":
            if not wallets:
                send_telegram("No wallets currently tracked. Use /add <address> <label> to start.")
            else:
                lines = [f"- {w['label']}: {w['address']}" for w in wallets]
                send_telegram("📋 Tracked wallets:\n" + "\n".join(lines))

        elif cmd == "/watch" and len(parts) >= 2:
            ca = parts[1].strip()
            if not is_plausible_solana_address(ca):
                send_telegram(f"⚠️ That doesn't look like a valid token address:\n{ca}")
                continue
            send_telegram(f"🔍 Checking {ca}...")
            name, symbol = get_token_name(ca)
            risk = get_token_risk(ca)
            liquidity = fetch_current_liquidity(ca)

            risk_line = (f"Risk: {risk['level']} ({risk['score']}/100)\n" + "\n".join(f"• {n}" for n in risk["notes"][:3])
                         if risk["score"] is not None else "Risk: unavailable")
            liq_line = f"Liquidity: ~${liquidity:,.0f}" if liquidity else "Liquidity: unavailable"

            send_telegram(
                f"🪙 {name} ({symbol})\nCA: {ca}\n{liq_line}\n{risk_line}\n\n"
                f"👁️ Now watching — you'll get an urgent alert if liquidity drops "
                f"{RUG_ALERT_DROP_PCT}%+ (a rug-pull warning sign).\n"
                f"⚠️ Not financial advice."
            )
            watched_coins[ca] = {
                "name": name, "symbol": symbol,
                "liquidity_at_watch": liquidity, "last_liquidity": liquidity,
                "added_ts": int(time.time()), "rugged": False,
            }
            watched_changed = True

        elif cmd == "/unwatch" and len(parts) >= 2:
            ca = parts[1].strip()
            if ca in watched_coins:
                del watched_coins[ca]
                watched_changed = True
                send_telegram(f"Stopped watching:\n{ca}")
            else:
                send_telegram("Not currently watching that address.")

        elif cmd == "/watchlist":
            if not watched_coins:
                send_telegram("Not watching any coins yet. Use /watch <CA> to start.")
            else:
                lines = [f"- {c['name']} ({c['symbol']}): {ca}" for ca, c in watched_coins.items()]
                send_telegram("👁️ Watched coins:\n" + "\n".join(lines))

        elif cmd == "/setminliquidity" and len(parts) >= 2:
            try:
                value = float(parts[1].strip())
                settings["min_liquidity_usd"] = value
                settings_changed = True
                send_telegram(f"✅ Scanner minimum liquidity set to ${value:,.0f}")
            except ValueError:
                send_telegram("⚠️ Send a number, e.g. /setminliquidity 8000")

        elif cmd == "/setminchange" and len(parts) >= 2:
            try:
                value = float(parts[1].strip())
                settings["min_1h_change_pct"] = value
                settings_changed = True
                send_telegram(f"✅ Scanner minimum 1h change set to {value:+.0f}%")
            except ValueError:
                send_telegram("⚠️ Send a number, e.g. /setminchange 20")

        elif cmd == "/help":
            send_telegram(
                "Wallets:\n"
                "/add <address> <label> — start tracking a wallet\n"
                "/remove <address> — stop tracking a wallet\n"
                "/list — show tracked wallets\n\n"
                "Coins:\n"
                "/watch <CA> — check a coin now + get rug alerts on it\n"
                "/unwatch <CA> — stop watching a coin\n"
                "/watchlist — show watched coins\n\n"
                "Scanner tuning:\n"
                "/setminliquidity <amount> — minimum liquidity to alert on\n"
                "/setminchange <percent> — minimum 1h price change to alert on"
            )

    save_json(TG_OFFSET_FILE, {"offset": offset})
    if wallets_changed:
        save_json(WALLETS_FILE, wallets)
    if watched_changed:
        save_json(WATCHED_COINS_FILE, watched_coins)
    if settings_changed:
        save_json(SETTINGS_FILE, settings)
    return wallets, watched_coins, settings


# ---------------- pricing ----------------

def get_sol_price():
    now = time.time()
    if _sol_price_cache["price"] is not None and now - _sol_price_cache["ts"] < 300:
        return _sol_price_cache["price"]
    try:
        resp = requests.get(
            "https://api.coingecko.com/api/v3/simple/price",
            params={"ids": "solana", "vs_currencies": "usd"},
            timeout=10,
        )
        price = resp.json()["solana"]["usd"]
        _sol_price_cache.update(price=price, ts=now)
        return price
    except Exception:
        return None


def usd_value_of(mint, amount):
    if not mint or not amount:
        return None
    if mint == SOL_MINT:
        price = get_sol_price()
        return amount * price if price else None
    if mint in STABLECOIN_MINTS:
        return amount
    return None


# ---------------- wallet transaction tracking ----------------

def fetch_transactions(address):
    url = f"https://api.helius.xyz/v0/addresses/{address}/transactions"
    params = {"api-key": HELIUS_API_KEY, "limit": FETCH_LIMIT}
    resp = requests.get(url, params=params, timeout=15)
    resp.raise_for_status()
    return resp.json()


def get_token_name(mint):
    if mint == SOL_MINT:
        return "SOL", "SOL"
    url = f"https://mainnet.helius-rpc.com/?api-key={HELIUS_API_KEY}"
    payload = {"jsonrpc": "2.0", "id": "lookup", "method": "getAsset", "params": {"id": mint}}
    try:
        resp = requests.post(url, json=payload, timeout=15)
        metadata = resp.json().get("result", {}).get("content", {}).get("metadata", {})
        return metadata.get("name") or "Unknown", metadata.get("symbol") or "?"
    except Exception:
        return "Unknown", "?"


def extract_create_ca(tx):
    for transfer in tx.get("tokenTransfers", []) or []:
        mint = transfer.get("mint")
        if mint:
            return mint
    return None


def extract_swap_details(tx):
    events = tx.get("events", {}) or {}
    swap = events.get("swap") or {}
    token_inputs = swap.get("tokenInputs", []) or []
    token_outputs = swap.get("tokenOutputs", []) or []
    native_input = swap.get("nativeInput")
    native_output = swap.get("nativeOutput")

    paid_mint = paid_amount = bought_mint = bought_amount = None

    if native_input and float(native_input.get("amount", 0)) > 0:
        paid_mint, paid_amount = SOL_MINT, int(native_input.get("amount", 0)) / 1e9
    elif token_inputs:
        paid_mint = token_inputs[0].get("mint")
        paid_amount = token_inputs[0].get("tokenAmount")

    if token_outputs:
        bought_mint = token_outputs[0].get("mint")
        bought_amount = token_outputs[0].get("tokenAmount")
    elif native_output and float(native_output.get("amount", 0)) > 0:
        bought_mint, bought_amount = SOL_MINT, int(native_output.get("amount", 0)) / 1e9

    if not paid_mint and not bought_mint:
        return None
    return {"paid_mint": paid_mint, "paid_amount": paid_amount,
            "bought_mint": bought_mint, "bought_amount": bought_amount}


def build_create_alert(label, address, ca, sig):
    link = f"https://solscan.io/tx/{sig}"
    if ca:
        name, symbol = get_token_name(ca)
        msg = (f"🆕 NEW COIN CREATED\nDev wallet: {label} ({address[:4]}...{address[-4:]})\n"
               f"Token: {name} ({symbol})\nCA: {ca}\n{link}")
    else:
        msg = (f"🆕 NEW COIN CREATED (CA not found — check tx)\n"
               f"Dev wallet: {label} ({address[:4]}...{address[-4:]})\n{link}")
    return msg


def build_swap_alert(label, address, details, sig):
    link = f"https://solscan.io/tx/{sig}"
    paid_mint, bought_mint = details["paid_mint"], details["bought_mint"]
    paid_name, paid_symbol = get_token_name(paid_mint) if paid_mint else ("Unknown", "?")
    bought_name, bought_symbol = get_token_name(bought_mint) if bought_mint else ("Unknown", "?")

    paid_amount, bought_amount = details.get("paid_amount"), details.get("bought_amount")
    paid_str = f"{paid_amount:.4f} {paid_symbol}" if paid_amount else paid_symbol
    bought_str = f"{bought_amount:.4f} {bought_symbol}" if bought_amount else bought_symbol

    paid_usd = usd_value_of(paid_mint, paid_amount)
    bought_usd = usd_value_of(bought_mint, bought_amount)
    if paid_usd:
        paid_str += f" (~${paid_usd:,.2f})"
    if bought_usd:
        bought_str += f" (~${bought_usd:,.2f})"

    trade_size = paid_usd or bought_usd
    trade_size_line = (f"Trade size: ~${trade_size:,.2f}\n" if trade_size
                        else "Trade size: unknown\n")

    return (f"💰 SWAP DETECTED\nWallet: {label} ({address[:4]}...{address[-4:]})\n"
            f"Swapped: {paid_str} -> {bought_str}\n{trade_size_line}"
            f"Bought token: {bought_name} ({bought_symbol})\n"
            f"CA: {bought_mint or '(not found)'}\n{link}")


# ---------------- wallet stats (built only from what we've actually observed) ----------------

def classify_swap(details):
    """From the wallet's point of view: did they BUY a token (spent SOL/stable)
    or SELL one (received SOL/stable)?"""
    paid_mint, bought_mint = details["paid_mint"], details["bought_mint"]
    paid_is_base = paid_mint == SOL_MINT or paid_mint in STABLECOIN_MINTS
    bought_is_base = bought_mint == SOL_MINT or bought_mint in STABLECOIN_MINTS
    if paid_is_base and not bought_is_base:
        return "buy", bought_mint
    if bought_is_base and not paid_is_base:
        return "sell", paid_mint
    return None, None  # token-for-token swap, or couldn't tell — skip stats for it


def update_wallet_stats(wallet_stats, address, label, details, paid_usd, bought_usd):
    stats = wallet_stats.setdefault(address, {
        "label": label, "buy_count": 0, "sell_count": 0,
        "total_buy_usd": 0.0, "total_sell_usd": 0.0,
        "tokens_traded": [], "last_active": 0,
        "positions": {}, "realized_pnl_usd": 0.0,
    })
    stats.setdefault("positions", {})
    stats.setdefault("realized_pnl_usd", 0.0)
    stats["label"] = label  # keep label fresh in case it was renamed
    stats["last_active"] = int(time.time())

    direction, token_mint = classify_swap(details)

    if direction == "buy":
        stats["buy_count"] += 1
        if paid_usd:
            stats["total_buy_usd"] += paid_usd
        bought_amount = details.get("bought_amount")
        # Build a real cost basis for this position, so a later sell can
        # compute actual realized profit/loss — not just "they traded".
        if token_mint and bought_amount and paid_usd:
            pos = stats["positions"].setdefault(token_mint, {"qty": 0.0, "cost_usd": 0.0})
            pos["qty"] += bought_amount
            pos["cost_usd"] += paid_usd

    elif direction == "sell":
        stats["sell_count"] += 1
        if bought_usd:
            stats["total_sell_usd"] += bought_usd
        sold_amount = details.get("paid_amount")
        pos = stats["positions"].get(token_mint) if token_mint else None
        # Only compute PnL if we actually saw the buy that opened this
        # position — if the position predates our tracking, we honestly
        # don't know the cost basis, so we skip rather than guess.
        if pos and pos.get("qty", 0) > 0 and sold_amount and bought_usd:
            avg_cost = pos["cost_usd"] / pos["qty"]
            qty_sold = min(sold_amount, pos["qty"])
            cost_of_sold = avg_cost * qty_sold
            stats["realized_pnl_usd"] += bought_usd - cost_of_sold
            pos["qty"] -= qty_sold
            pos["cost_usd"] -= cost_of_sold

    if token_mint and token_mint not in stats["tokens_traded"]:
        stats["tokens_traded"].append(token_mint)
    return wallet_stats


def record_recent_buy(recent_buys, mint, label):
    """Log that a tracked wallet just bought this token — used by the scanner
    to spot real overlap between your tracked wallets and its own hits."""
    entry = recent_buys.setdefault(mint, [])
    entry.append({"label": label, "ts": int(time.time())})
    cutoff = time.time() - SMART_MONEY_WINDOW_HOURS * 3600
    recent_buys[mint] = [e for e in entry if e["ts"] > cutoff]
    # prune mints with no recent activity at all, so the file doesn't grow forever
    return {m: es for m, es in recent_buys.items() if es}


# ---------------- token risk score ----------------

def get_token_risk(mint):
    """
    Objective risk checks only — holder concentration + mint/freeze authority.
    Does NOT check liquidity (would need DEX pool data we don't have a free
    source for yet). Never call this "safe" — only ever relative risk.
    """
    url = f"https://mainnet.helius-rpc.com/?api-key={HELIUS_API_KEY}"
    risk_score = 0
    notes = []

    try:
        supply_resp = requests.post(url, json={
            "jsonrpc": "2.0", "id": "supply", "method": "getTokenSupply", "params": [mint],
        }, timeout=15).json()
        supply = float(supply_resp.get("result", {}).get("value", {}).get("amount", 0) or 0)

        holders_resp = requests.post(url, json={
            "jsonrpc": "2.0", "id": "holders", "method": "getTokenLargestAccounts", "params": [mint],
        }, timeout=15).json()
        top_accounts = holders_resp.get("result", {}).get("value", []) or []
        top10_amount = sum(float(a.get("amount", 0)) for a in top_accounts[:10])
        top10_pct = (top10_amount / supply * 100) if supply else None

        if top10_pct is not None:
            if top10_pct > 70:
                risk_score += 40; notes.append(f"top 10 holders own {top10_pct:.0f}% of supply (very concentrated)")
            elif top10_pct > 50:
                risk_score += 25; notes.append(f"top 10 holders own {top10_pct:.0f}% of supply (concentrated)")
            elif top10_pct > 30:
                risk_score += 10; notes.append(f"top 10 holders own {top10_pct:.0f}% of supply")

        mint_info_resp = requests.post(url, json={
            "jsonrpc": "2.0", "id": "mintinfo", "method": "getAccountInfo",
            "params": [mint, {"encoding": "jsonParsed"}],
        }, timeout=15).json()
        parsed = (mint_info_resp.get("result", {}).get("value", {}) or {}).get("data", {}).get("parsed", {}).get("info", {})
        if parsed.get("mintAuthority"):
            risk_score += 30
            notes.append("mint authority is still active (supply can be inflated)")
        if parsed.get("freezeAuthority"):
            risk_score += 20
            notes.append("freeze authority is still active (holder wallets can be frozen)")

    except Exception as e:
        print(f"[risk] lookup failed for {mint}: {e}")
        return {"score": None, "level": "Unknown", "notes": ["Risk data unavailable"]}

    risk_score = min(100, risk_score)
    if risk_score <= 20:
        level = "Low"
    elif risk_score <= 40:
        level = "Moderate"
    elif risk_score <= 60:
        level = "Elevated"
    elif risk_score <= 80:
        level = "High"
    else:
        level = "Extreme"

    return {"score": risk_score, "level": level, "notes": notes or ["No major red flags found"]}


def process_wallet(wallet, seen, alerts_log, wallet_stats, recent_buys):
    label, address = wallet["label"], wallet["address"]
    seen_sigs = set(seen.get(address, []))
    first_run = len(seen_sigs) == 0

    try:
        txs = fetch_transactions(address)
    except Exception as e:
        print(f"[error] fetch failed for {label}: {e}")
        return seen, alerts_log, wallet_stats, recent_buys

    new_txs = [tx for tx in txs if tx.get("signature") not in seen_sigs]

    for tx in reversed(new_txs):
        sig = tx.get("signature", "")
        seen_sigs.add(sig)
        if first_run:
            continue

        tx_type = (tx.get("type") or "").upper()
        desc = tx.get("description", "") or ""
        msg = None

        if tx_type == "CREATE" or "created" in desc.lower():
            msg = build_create_alert(label, address, extract_create_ca(tx), sig)
        elif tx_type in ("SWAP", "TOKEN_SWAP") or "swap" in desc.lower():
            details = extract_swap_details(tx)
            if details:
                msg = build_swap_alert(label, address, details, sig)
                paid_usd = usd_value_of(details["paid_mint"], details.get("paid_amount"))
                bought_usd = usd_value_of(details["bought_mint"], details.get("bought_amount"))
                update_wallet_stats(wallet_stats, address, label, details, paid_usd, bought_usd)
                direction, token_mint = classify_swap(details)
                if direction == "buy" and token_mint:
                    recent_buys = record_recent_buy(recent_buys, token_mint, label)

        if msg:
            print("\n" + "=" * 60 + f"\n{msg}\n" + "=" * 60)
            send_telegram(msg)
            alerts_log.insert(0, {"ts": int(time.time()), "type": "wallet", "message": msg})

    if first_run and new_txs:
        print(f"[{label}] primed with {len(seen_sigs)} past transactions.")

    seen[address] = list(seen_sigs)
    return seen, alerts_log, wallet_stats, recent_buys


# ---------------- coin potential scanner ----------------

def fetch_new_pools(settings):
    """
    Pulls recently created Solana liquidity pools via DexPaprika — genuinely
    free, no signup, no API key, no card, ever (50K requests/month per IP).
    Filtered to PumpSwap, since that's where pump.fun tokens land once they
    graduate off the bonding curve. This means we only see tokens that have
    already graduated (proven enough demand to migrate) — not the earliest
    bonding-curve-stage coins.
    """
    min_liquidity = get_scanner_setting(settings, "min_liquidity_usd", DEFAULT_MIN_LIQUIDITY_USD)
    url = "https://api.dexpaprika.com/networks/solana/pools/search"
    params = {
        "order_by": "created_at",
        "sort": "desc",
        "limit": 50,
        "liquidity_usd_min": min_liquidity,
    }
    try:
        resp = requests.get(url, params=params, timeout=15)
        resp.raise_for_status()
        results = resp.json().get("results", [])
        return [p for p in results if p.get("dex_id") == "pumpswap"]
    except Exception as e:
        print(f"[scanner] fetch failed: {e}")
        return []


def extract_token_mint(pool):
    """Pool has two sides (token + SOL) — return whichever isn't SOL."""
    for t in pool.get("tokens", []) or []:
        if t.get("id") != SOL_MINT:
            return t.get("id")
    return None


def score_pool(pool, settings):
    """
    Returns (passes, why_lines, liquidity, volume_24h, change_1h). DexPaprika
    already computes price-change percentages for us, so no manual
    snapshot-diffing needed — it's a real momentum reading straight from the
    source.
    """
    min_liquidity = get_scanner_setting(settings, "min_liquidity_usd", DEFAULT_MIN_LIQUIDITY_USD)
    min_change = get_scanner_setting(settings, "min_1h_change_pct", DEFAULT_MIN_1H_CHANGE_PCT)

    liquidity = pool.get("liquidity_usd", 0) or 0
    volume_24h = pool.get("volume_usd_24h", 0) or 0
    change_1h = pool.get("price_change_percentage_1h", 0) or 0

    created_at = pool.get("created_at")
    age_min = None
    if created_at:
        try:
            created_ts = time.mktime(time.strptime(created_at, "%Y-%m-%dT%H:%M:%SZ"))
            age_min = (time.time() - created_ts) / 60
        except Exception:
            age_min = None

    if age_min is None or not (SCANNER_MIN_AGE_MIN <= age_min <= SCANNER_MAX_AGE_MIN):
        return False, [], liquidity, volume_24h, change_1h
    if liquidity < min_liquidity:
        return False, [], liquidity, volume_24h, change_1h
    if change_1h < min_change:
        return False, [], liquidity, volume_24h, change_1h

    why_lines = [
        f"Liquidity ~${liquidity:,.0f}, {age_min:.0f} min since pool creation",
        f"Up {change_1h:+.0f}% in the last hour, ~${volume_24h:,.0f} in 24h volume",
    ]
    return True, why_lines, liquidity, volume_24h, change_1h


def signal_state(score):
    if score < 40: return "AVOID"
    if score < 60: return "WEAK"
    if score < 70: return "WATCH"
    if score < 80: return "INTERESTING"
    if score < 90: return "STRONG"
    return "EXCEPTIONAL"


def compute_opportunity_score(change_1h, risk, liquidity, smart_money_labels):
    """
    Combines only signals we can actually back with real data:
      - Momentum: real 1h price change from DexPaprika
      - Safety: inverse of the risk score (holder concentration + authorities)
      - Liquidity: can you actually get in/out without huge slippage?
      - Smart money: did any of YOUR tracked wallets buy this recently?
    Weights are rough and meant to be tuned once you've seen real output.
    """
    momentum_score = max(0, min(100, change_1h * 1.5))
    safety_score = (100 - risk["score"]) if risk.get("score") is not None else 50
    liquidity_score = max(0, min(100, liquidity / 20000 * 100))
    smart_money_score = 100 if smart_money_labels else 0

    weights = {"momentum": 0.30, "safety": 0.30, "liquidity": 0.15, "smart_money": 0.25}
    total = (momentum_score * weights["momentum"] + safety_score * weights["safety"] +
             liquidity_score * weights["liquidity"] + smart_money_score * weights["smart_money"])
    total = round(total)

    breakdown = {
        "MOMENTUM": round(momentum_score),
        "SAFETY": round(safety_score),
        "LIQUIDITY": round(liquidity_score),
        "SMART MONEY": round(smart_money_score),
    }
    return total, breakdown


def check_smart_money(mint, recent_buys):
    entries = recent_buys.get(mint, [])
    return [e["label"] for e in entries]


def check_watched_coins_for_rugs(watched_coins, alerts_log):
    """For every coin someone /watch'd, check its current liquidity against
    the last known value — a sudden large drop is a classic rug-pull sign."""
    for ca, coin in watched_coins.items():
        if coin.get("rugged"):
            continue  # already alerted on this one, don't spam
        current = fetch_current_liquidity(ca)
        if current is None:
            continue
        last = coin.get("last_liquidity") or coin.get("liquidity_at_watch")
        if last and last > 0:
            drop_pct = (1 - current / last) * 100
            if drop_pct >= RUG_ALERT_DROP_PCT:
                msg = (
                    f"🚨 POSSIBLE RUG — {coin['name']} ({coin['symbol']})\n"
                    f"Liquidity dropped {drop_pct:.0f}% (${last:,.0f} -> ${current:,.0f})\n"
                    f"CA: {ca}\n"
                    f"Sudden, large liquidity removal is a classic rug-pull sign — "
                    f"treat this as a serious warning, not a certainty."
                )
                send_telegram(msg)
                alerts_log.insert(0, {"ts": int(time.time()), "type": "rug", "message": msg})
                coin["rugged"] = True
        coin["last_liquidity"] = current
    return watched_coins, alerts_log


def run_coin_scanner(scanner_seen, alerts_log, signal_outcomes, recent_buys, settings):
    pools = fetch_new_pools(settings)
    for pool in pools:
        mint = extract_token_mint(pool)
        if not mint or mint in signal_outcomes:
            continue  # no token found, or we've already flagged this one before

        passes, why_lines, liquidity, volume_24h, change_1h = score_pool(pool, settings)
        if not passes:
            continue

        name, symbol = get_token_name(mint)
        risk = get_token_risk(mint)
        smart_money_labels = check_smart_money(mint, recent_buys)

        opp_score, breakdown = compute_opportunity_score(change_1h, risk, liquidity, smart_money_labels)
        state = signal_state(opp_score)

        if smart_money_labels:
            why_lines.append(f"Bought recently by wallets you track: {', '.join(smart_money_labels)}")

        why_block = "\n".join(f"• {line}" for line in why_lines)
        if risk["score"] is not None:
            risk_block = "\n".join(f"• {note}" for note in risk["notes"][:3])
            risk_header = f"RISKS ({risk['level']}, {risk['score']}/100):"
        else:
            risk_block = "• Risk data unavailable"
            risk_header = "RISKS:"
        breakdown_block = "\n".join(f"{k:<12} {v}" for k, v in breakdown.items())

        msg = (f"📈 COIN WORTH LOOKING AT — {state}\n"
               f"{name} ({symbol})\n"
               f"Opportunity: {opp_score}/100\n\n"
               f"{breakdown_block}\n\n"
               f"WHY:\n{why_block}\n\n"
               f"{risk_header}\n{risk_block}\n\n"
               f"CA: {mint}\n"
               f"https://dexscreener.com/solana/{mint}\n"
               f"⚠️ Not financial advice — this only means it clears basic "
               f"traction filters. Do your own check before anything else.")
        print("\n" + "=" * 60 + f"\n{msg}\n" + "=" * 60)
        send_telegram(msg)
        alerts_log.insert(0, {"ts": int(time.time()), "type": "scanner", "message": msg})

        signal_outcomes[mint] = {
            "name": name, "symbol": symbol,
            "flagged_ts": int(time.time()),
            "liquidity_at_flag": liquidity,
            "checkpoints_done": [],
        }

    return scanner_seen, alerts_log, signal_outcomes


def fetch_current_liquidity(mint):
    """Look up a token's current best-pool liquidity, for outcome check-backs."""
    url = "https://api.dexpaprika.com/networks/solana/pools/search"
    params = {"token_address": mint, "order_by": "liquidity_usd", "sort": "desc", "limit": 1}
    try:
        resp = requests.get(url, params=params, timeout=15)
        resp.raise_for_status()
        results = resp.json().get("results", [])
        if results:
            return results[0].get("liquidity_usd")
    except Exception as e:
        print(f"[scanner] outcome liquidity lookup failed for {mint}: {e}")
    return None


def update_outcome_history(outcome_history, checkpoint_label, change_pct):
    """Tally results per checkpoint so we can report the scanner's real
    track record, not just fire alerts and never look back."""
    bucket = outcome_history.setdefault(checkpoint_label, {"count": 0, "up_count": 0, "total_change_pct": 0.0})
    bucket["count"] += 1
    if change_pct > 0:
        bucket["up_count"] += 1
    bucket["total_change_pct"] += change_pct
    return outcome_history


def summarize_outcome_history(outcome_history):
    summary = {}
    for label, bucket in outcome_history.items():
        if bucket["count"] > 0:
            summary[label] = {
                "count": bucket["count"],
                "up_pct": round(bucket["up_count"] / bucket["count"] * 100),
                "avg_change_pct": round(bucket["total_change_pct"] / bucket["count"], 1),
            }
    return summary


def check_signal_outcomes(signal_outcomes, alerts_log, outcome_history):
    """For coins we've flagged before, check back at fixed checkpoints
    (1h/4h/24h) and report whether it actually grew — this is what makes the
    scanner accountable instead of just firing alerts and never following up."""
    now = time.time()
    for mint, record in signal_outcomes.items():
        elapsed_min = (now - record["flagged_ts"]) / 60

        for checkpoint in OUTCOME_CHECKPOINTS_MIN:
            label = f"{checkpoint}m"
            if elapsed_min >= checkpoint and label not in record["checkpoints_done"]:
                record["checkpoints_done"].append(label)
                current = fetch_current_liquidity(mint)
                if current and record.get("liquidity_at_flag"):
                    change_pct = (current / record["liquidity_at_flag"] - 1) * 100
                    hours = checkpoint / 60
                    outcome_msg = (
                        f"📊 SIGNAL REVIEW ({hours:.0f}h later)\n"
                        f"{record['name']} ({record['symbol']})\n"
                        f"Liquidity at flag: ~${record['liquidity_at_flag']:,.0f}\n"
                        f"Liquidity now: ~${current:,.0f}\n"
                        f"Change: {change_pct:+.0f}%"
                    )
                    send_telegram(outcome_msg)
                    alerts_log.insert(0, {"ts": int(time.time()), "type": "outcome", "message": outcome_msg})
                    outcome_history = update_outcome_history(outcome_history, label, change_pct)

    # drop records once fully checked and old, so the file doesn't grow forever
    signal_outcomes = {
        m: r for m, r in signal_outcomes.items()
        if len(r["checkpoints_done"]) < len(OUTCOME_CHECKPOINTS_MIN)
        or (now - r["flagged_ts"]) < 7 * 86400
    }
    return signal_outcomes, alerts_log, outcome_history


# ---------------- daily digest ----------------

def maybe_send_daily_digest(wallet_stats, outcome_history, watched_coins, digest_state):
    """Sends one summary message per day (around 08:00 UTC) instead of only
    reactive alerts — a single glance at how things are going."""
    today = time.strftime("%Y-%m-%d", time.gmtime())
    current_hour = int(time.strftime("%H", time.gmtime()))
    if digest_state.get("last_sent_date") == today or current_hour != 8:
        return digest_state

    lines = ["🗞️ DAILY DIGEST"]
    cutoff = time.time() - 86400
    active = [s for s in wallet_stats.values() if s.get("last_active", 0) > cutoff]
    lines.append(f"\nWallets active in last 24h: {len(active)}")
    for s in active:
        pnl = s.get("realized_pnl_usd", 0.0)
        lines.append(f"- {s['label']}: {s['buy_count']} buys, {s['sell_count']} sells, realized PnL ~${pnl:,.2f}")

    summary = summarize_outcome_history(outcome_history)
    if summary:
        lines.append("\nScanner accuracy so far:")
        for label, s in summary.items():
            lines.append(f"- {label}: {s['count']} flagged, {s['up_pct']}% were up, avg change {s['avg_change_pct']:+.1f}%")

    if watched_coins:
        lines.append(f"\nWatching {len(watched_coins)} coin(s) for rug alerts.")

    send_telegram("\n".join(lines))
    digest_state["last_sent_date"] = today
    return digest_state


# ---------------- dashboard export ----------------

def write_dashboard(wallets, alerts_log, wallet_stats, watched_coins, outcome_history, settings):
    data = {
        "last_updated": int(time.time()),
        "wallets": wallets,
        "wallet_stats": wallet_stats,
        "watched_coins": watched_coins,
        "scanner_accuracy": summarize_outcome_history(outcome_history),
        "settings": settings,
        "recent_alerts": alerts_log[:MAX_ALERTS_LOGGED],
    }
    save_json(DASHBOARD_FILE, data)


# ---------------- main ----------------

def main():
    check_config()

    default_wallets = [
        {"label": "Dev wallet 1", "address": "5CEbueQnq1Ym2uSSx2xXds3jQAqT1BDnkA59RZobSPAG"},
    ]
    wallets = load_json(WALLETS_FILE, default_wallets)
    seen = load_json(SEEN_FILE, {})
    scanner_seen = load_json(SCANNER_SEEN_FILE, {})
    alerts_log = load_json(ALERTS_LOG_FILE, [])
    wallet_stats = load_json(WALLET_STATS_FILE, {})
    signal_outcomes = load_json(SIGNAL_OUTCOMES_FILE, {})
    recent_buys = load_json(RECENT_BUYS_FILE, {})
    watched_coins = load_json(WATCHED_COINS_FILE, {})
    settings = load_json(SETTINGS_FILE, {})
    outcome_history = load_json(OUTCOME_HISTORY_FILE, {})
    digest_state = load_json("digest_state.json", {})

    wallets, watched_coins, settings = process_telegram_commands(wallets, watched_coins, settings)
    save_json(WALLETS_FILE, wallets)  # always persist, even if nothing changed this run
    save_json(WATCHED_COINS_FILE, watched_coins)
    save_json(SETTINGS_FILE, settings)

    print(f"Tracking {len(wallets)} wallet(s), watching {len(watched_coins)} coin(s):")
    for w in wallets:
        print(f"  - {w['label']}: {w['address']}")

    for wallet in wallets:
        seen, alerts_log, wallet_stats, recent_buys = process_wallet(
            wallet, seen, alerts_log, wallet_stats, recent_buys)
        save_json(SEEN_FILE, seen)
    save_json(WALLET_STATS_FILE, wallet_stats)
    save_json(RECENT_BUYS_FILE, recent_buys)

    watched_coins, alerts_log = check_watched_coins_for_rugs(watched_coins, alerts_log)
    save_json(WATCHED_COINS_FILE, watched_coins)

    scanner_seen, alerts_log, signal_outcomes = run_coin_scanner(
        scanner_seen, alerts_log, signal_outcomes, recent_buys, settings)
    signal_outcomes, alerts_log, outcome_history = check_signal_outcomes(
        signal_outcomes, alerts_log, outcome_history)
    save_json(SCANNER_SEEN_FILE, scanner_seen)
    save_json(SIGNAL_OUTCOMES_FILE, signal_outcomes)
    save_json(OUTCOME_HISTORY_FILE, outcome_history)

    digest_state = maybe_send_daily_digest(wallet_stats, outcome_history, watched_coins, digest_state)
    save_json("digest_state.json", digest_state)

    alerts_log = alerts_log[:MAX_ALERTS_LOGGED]
    save_json(ALERTS_LOG_FILE, alerts_log)

    write_dashboard(wallets, alerts_log, wallet_stats, watched_coins, outcome_history, settings)


if __name__ == "__main__":
    main()
