#!/usr/bin/env python3
"""
PokéAlert UK — Stock scraper for GitHub Actions
Checks Pokémon TCG product pages across UK retailers and
fires a Discord webhook alert when something restocks.
"""

import json, os, sys, time, random
from datetime import datetime, timezone

import requests
from bs4 import BeautifulSoup

# ── Config ────────────────────────────────────────────────────────────────────

WEBHOOK_URL    = os.environ.get("DISCORD_WEBHOOK", "")
PRODUCTS_FILE  = "products.json"
STATE_FILE     = "state.json"
TIMEOUT        = 20
DELAY          = (2.5, 5.0)

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept":          "text/html,application/xhtml+xml,application/xhtml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-GB,en;q=0.9",
    "Accept-Encoding": "gzip, deflate, br",
    "DNT":             "1",
}

# ── Text signals (generic fallback) ───────────────────────────────────────────

OOS_PHRASES = [
    "out of stock", "sold out", "unavailable", "currently unavailable",
    "not available", "no longer available", "temporarily unavailable",
    "notify me when available", "notify me when in stock",
    "join waitlist", "pre-order only",
]
IN_PHRASES = [
    "add to trolley", "add to basket", "add to cart", "add to bag",
    "buy now", "order now", "in stock",
]

# ── Structured data (schema.org) ──────────────────────────────────────────────

def check_structured_data(soup):
    """
    Parse JSON-LD schema.org markup for availability.
    Most major UK retailers (Smyths, GAME, Very, Tesco etc.) include this —
    it's the most reliable method since it's not affected by JS rendering.
    """
    for script in soup.find_all("script", type="application/ld+json"):
        if not script.string:
            continue
        try:
            data = json.loads(script.string)
            items = data if isinstance(data, list) else [data]
            for item in items:
                offers = item.get("offers", {})
                if isinstance(offers, list):
                    offers = offers[0] if offers else {}
                avail = offers.get("availability", "")
                if not avail:
                    continue
                label = avail.split("/")[-1]   # e.g. "InStock" from full URL
                if any(x in avail for x in ("InStock", "LimitedAvailability", "PreOrder")):
                    return True, f"In stock (schema.org: {label})"
                if any(x in avail for x in ("OutOfStock", "Discontinued", "SoldOut")):
                    return False, f"Out of stock (schema.org: {label})"
        except Exception:
            pass
    return None, None

# ── Generic text fallback ─────────────────────────────────────────────────────

def _generic(soup):
    text = soup.get_text(" ", strip=True).lower()
    for p in OOS_PHRASES:
        if p in text:
            return False, f"Out of stock ('{p}')"
    for p in IN_PHRASES:
        if p in text:
            return True, f"In stock ('{p}')"
    return None, "Status unclear — page may use JS rendering"

# ── Retailer-specific checkers (used as fallback after structured data) ────────

def _check_smyths(soup):
    btn = soup.find("button", class_=lambda c: c and any(
        x in " ".join(c).lower() for x in ("addtocart", "add-to-cart", "add-to-trolley")))
    if btn:
        if btn.get("disabled") or "disabled" in " ".join(btn.get("class", [])).lower():
            return False, "Out of stock (button disabled)"
        return True, "In stock — Add to Trolley available"
    return _generic(soup)


def _check_magic_madhouse(soup):
    # Magic Madhouse shows clear OOS/in-stock text in the page
    text = soup.get_text(" ", strip=True).lower()
    if "out of stock" in text or "sold out" in text:
        return False, "Out of stock"
    if "add to cart" in text or "add to basket" in text or "buy now" in text:
        return True, "In stock"
    # Check for invitation/lottery system
    if "invitation to purchase" in text or "sign up" in text:
        return False, "Out of stock (invitation/lottery only)"
    return _generic(soup)


RETAILER_MAP = {
    "smythstoys.com":     _check_smyths,
    "magicmadhouse.co.uk": _check_magic_madhouse,
}

# ── Main stock detection ───────────────────────────────────────────────────────

def detect_stock(url, html):
    soup = BeautifulSoup(html, "lxml")

    # 1. Try structured data first — most reliable, not affected by JS
    in_stock, reason = check_structured_data(soup)
    if in_stock is not None:
        return in_stock, reason

    # 2. Retailer-specific checker
    for domain, fn in RETAILER_MAP.items():
        if domain in url.lower():
            return fn(soup)

    # 3. Generic text scan
    return _generic(soup)

# ── HTTP ──────────────────────────────────────────────────────────────────────

def fetch(url):
    try:
        r = requests.get(url, headers=HEADERS, timeout=TIMEOUT, allow_redirects=True)
        return (r.text, None) if r.status_code == 200 else (None, f"HTTP {r.status_code}")
    except requests.exceptions.Timeout:
        return None, "Timeout"
    except requests.exceptions.RequestException as e:
        return None, str(e)

# ── Discord ───────────────────────────────────────────────────────────────────

def _post(payload):
    if not WEBHOOK_URL:
        print("[Discord] No webhook URL — skipping.")
        return
    try:
        requests.post(WEBHOOK_URL, json=payload, timeout=10).raise_for_status()
    except Exception as e:
        print(f"[Discord] Error: {e}")


def send_restock(product, reason):
    name, retailer = product["name"], product.get("retailer", "Unknown")
    price, url     = product.get("price", ""), product["url"]

    fields = [
        {"name": "🏪 Retailer", "value": retailer, "inline": True},
        {"name": "📦 Status",   "value": reason,   "inline": True},
    ]
    if price:
        fields.append({"name": "💰 Price", "value": price, "inline": True})
    fields.append({"name": "🔗 Link", "value": f"[Buy Now!]({url})", "inline": False})

    _post({
        "content": "@everyone 🔴 **RESTOCK ALERT!**",
        "embeds": [{
            "title":       "🚨 RESTOCK DETECTED!",
            "description": f"**{name}** is back in stock at **{retailer}**!",
            "color":       0x22C55E,
            "fields":      fields,
            "timestamp":   datetime.now(timezone.utc).isoformat(),
            "footer":      {"text": "PokéAlert UK"},
        }],
    })
    print(f"[Discord] ✅ Alert sent for: {name}")


def send_summary(results):
    if not os.environ.get("DISCORD_SEND_SUMMARY"):
        return
    icon = {True: "✅", False: "❌", None: "❓"}
    lines = [f"{icon.get(r['in_stock'], '❓')} **{r['name']}** — {r['reason']}" for r in results]
    _post({
        "embeds": [{
            "title":       "📋 Stock Check Summary",
            "description": "\n".join(lines) or "No products checked.",
            "color":       0x6B7280,
            "timestamp":   datetime.now(timezone.utc).isoformat(),
            "footer":      {"text": "PokéAlert UK"},
        }]
    })

# ── Helpers ───────────────────────────────────────────────────────────────────

def load_json(path, default):
    if os.path.exists(path):
        try:
            with open(path) as f:
                return json.load(f)
        except Exception as e:
            print(f"[Warning] Could not read {path}: {e}")
    return default


def save_json(path, data):
    with open(path, "w") as f:
        json.dump(data, f, indent=2)

# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    products = load_json(PRODUCTS_FILE, [])
    if not products:
        print("No products in products.json — nothing to do.")
        sys.exit(0)

    state     = load_json(STATE_FILE, {})
    is_first  = not bool(state)
    new_state = {}
    results   = []

    if is_first:
        print("[Info] First run — building baseline. No alerts will fire yet.")

    for product in products:
        name = product.get("name", "Unnamed")
        url  = product.get("url", "")
        if not url:
            print(f"[Skip] {name} — no URL configured")
            continue

        print(f"[Check] {name}")
        html, err = fetch(url)
        now = datetime.now(timezone.utc).isoformat()

        if err:
            print(f"  ✗ {err}")
            new_state[name] = {**state.get(name, {"in_stock": None}), "last_checked": now, "error": err}
            results.append({"name": name, "in_stock": None, "reason": err})
            time.sleep(random.uniform(*DELAY))
            continue

        in_stock, reason = detect_stock(url, html)
        print(f"  → {reason}")

        prev_in_stock = state.get(name, {}).get("in_stock")
        new_state[name] = {"in_stock": in_stock, "reason": reason, "last_checked": now, "error": None}

        if in_stock is True and prev_in_stock is not True and not is_first:
            print(f"  🚨 RESTOCK!")
            send_restock(product, reason)

        results.append({"name": name, "in_stock": in_stock, "reason": reason})
        time.sleep(random.uniform(*DELAY))

    save_json(STATE_FILE, new_state)
    print(f"\n[Done] Checked {len(results)} product(s).")
    send_summary(results)


if __name__ == "__main__":
    main()
