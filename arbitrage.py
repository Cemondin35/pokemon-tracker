#!/usr/bin/env python3 -u
"""
Pokemon Card Arbitrage System
Buy from eBay UK → Sell on Cardmarket for profit.

Uses PokéWallet API for Cardmarket prices and eBay Browse API for UK prices.
Runs as a background service on Render (free tier).
"""

import asyncio
import base64
import html as html_mod
import json
import os
import re
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

os.environ["PYTHONUNBUFFERED"] = "1"
sys.stdout.reconfigure(line_buffering=True)
sys.stderr.reconfigure(line_buffering=True)

import httpx


# --- Load .env file ---

def load_env():
    env_path = Path(__file__).parent / ".env"
    if env_path.exists():
        for line in env_path.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if "=" in line:
                key, _, value = line.partition("=")
                os.environ.setdefault(key.strip(), value.strip())

load_env()


# --- Configuration ---

POKEWALLET_API_BASE = "https://api.pokewallet.io"

TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")

CARDMARKET_COMMISSION_PERCENT = 5.0
CARDMARKET_SHIPPING_COST_EUR = 1.50

GBP_TO_EUR = float(os.environ.get("GBP_TO_EUR", "1.17"))

MIN_PROFIT_EUR = float(os.environ.get("MIN_PROFIT_EUR", "2.0"))
MIN_PROFIT_PERCENT = float(os.environ.get("MIN_PROFIT_PERCENT", "15.0"))

POKEWALLET_API_KEY = os.environ.get("POKEWALLET_API_KEY", "")

EBAY_CLIENT_ID = os.environ.get("EBAY_CLIENT_ID", "")
EBAY_CLIENT_SECRET = os.environ.get("EBAY_CLIENT_SECRET", "")
EBAY_ENVIRONMENT = os.environ.get("EBAY_ENVIRONMENT", "PRODUCTION")

WATCHLIST_SETS = [s.strip() for s in os.environ.get("WATCHLIST_SETS", "").split(",") if s.strip()]

BOT_SETTINGS = {
    "interval_hours": int(os.environ.get("AUTO_SCAN_INTERVAL_HOURS", "6")),
    "max_sets": int(os.environ.get("AUTO_SCAN_SET_COUNT", "5")),
    "series": ["Mega Evolution", "Scarlet & Violet", "Sword & Shield"],
}

STATE_FILE = "arbitrage_state.json"
RESULTS_FILE = "arbitrage_results.json"

REQUEST_DELAY = 2

PORT = int(os.environ.get("PORT", "10000"))


# --- Rarity filter ---

LOW_RARITIES = {
    "common", "uncommon", "rare", "double rare",
    "promo", "normal", "trainer", "energy",
    "c", "u", "r", "rr", "code",
}

SKIP_CARD_NAMES = {"code card", "energy card", "online code"}


def _is_high_rarity(rarity: str) -> bool:
    if not rarity:
        return False
    return rarity.lower().strip() not in LOW_RARITIES


# --- Series grouping by set NAME prefix ---

SERIES_CODE_FALLBACK = {
    "SVP": "Scarlet & Violet", "SV": "Scarlet & Violet",
    "SWSH": "Sword & Shield", "SM": "Sun & Moon",
    "XY": "XY", "BW": "Black & White",
    "HGSS": "HeartGold & SoulSilver", "PL": "Platinum",
    "DP": "Diamond & Pearl", "EX": "EX",
    "CL": "Call of Legends", "NXD": "Next Destinies",
    "LTR": "Legendary Treasures", "GEN": "Generations",
    "DET": "Detective Pikachu", "CEL": "Celebrations",
    "PGO": "Pokemon GO", "TG": "Trainer Gallery",
}


def _get_series_from_set(set_data: dict) -> str:
    """Extract series from set NAME prefix (before colon).
    PokéWallet names: 'ME03: Perfect Order', 'SV10: Destined Rivals', etc.
    """
    name = set_data.get("name", "")
    name_lower = name.lower()

    if ": " in name:
        prefix = name.split(": ")[0].strip()
        alpha = re.sub(r'[^A-Za-z]', '', prefix).upper()

        if alpha in ("ME", "MEP", "MEE", "MED", "MBD", "MBG"):
            return "Mega Evolution"
        if alpha.startswith("SV"):
            return "Scarlet & Violet"
        if alpha.startswith("SWSH"):
            return "Sword & Shield"
        if alpha.startswith("SM"):
            return "Sun & Moon"
        if alpha in ("M", "ML", "MS"):
            return "Pocket Expansion"
        if alpha in ("A", "AA"):
            return "Pocket Expansion"
        if alpha.startswith("X"):
            return "Extended Art"
        if alpha.startswith("CBB") or alpha.startswith("CSV"):
            return "Special Collection"
        if alpha.startswith("PPS"):
            return "Play! Pokemon"
        if alpha == "TP":
            return "Promo"

    if "mega evolution" in name_lower or "mega starter" in name_lower:
        return "Mega Evolution"
    if any(w in name_lower for w in ("mega brave", "mega symphonia", "mega all-stars", "mega dream")):
        return "Mega Evolution"
    if "extended art" in name_lower:
        return "Extended Art"
    if "gem pack" in name_lower:
        return "Special Collection"
    if "promo" in name_lower or "black star" in name_lower:
        return "Promo"
    if "scarlet" in name_lower or "violet" in name_lower:
        return "Scarlet & Violet"

    set_code = set_data.get("set_code", "")
    if set_code:
        code_upper = set_code.upper()
        for pfx, series in sorted(SERIES_CODE_FALLBACK.items(), key=lambda x: -len(x[0])):
            if code_upper.startswith(pfx):
                return series
        if code_upper.startswith("PR"):
            return "Promo"
        if code_upper.startswith("MCD"):
            return "McDonald's"

    return "Diğer"


@dataclass
class CardPrice:
    name: str
    set_name: str
    set_code: str
    card_number: str
    rarity: str = ""
    image_url: str = ""
    ebay_price_gbp: float = 0.0
    ebay_shipping_gbp: float = 0.0
    ebay_total_gbp: float = 0.0
    ebay_url: str = ""
    ebay_condition: str = "Near Mint"
    cardmarket_price_eur: float = 0.0
    cardmarket_trend_eur: float = 0.0
    cardmarket_url: str = ""
    total_cost_eur: float = 0.0
    selling_price_after_fees_eur: float = 0.0
    profit_eur: float = 0.0
    profit_percent: float = 0.0
    is_profitable: bool = False


@dataclass
class SetInfo:
    set_code: str
    name: str
    series: str = ""
    total_cards: int = 0


# --- Date Parser ---

def _parse_date(s: dict) -> str:
    from datetime import datetime
    raw = s.get("releaseDate") or s.get("release_date") or ""
    if not raw:
        return "0000-00-00"
    try:
        if re.match(r"\d{4}[-/]\d{2}[-/]\d{2}", raw):
            return raw[:10].replace("/", "-")
        cleaned = re.sub(r"(\d+)(st|nd|rd|th)", r"\1", raw)
        for fmt in ["%d %B, %Y", "%d %B %Y", "%B %d, %Y", "%B %d %Y"]:
            try:
                dt = datetime.strptime(cleaned.strip(), fmt)
                return dt.strftime("%Y-%m-%d")
            except ValueError:
                continue
    except Exception:
        pass
    return "0000-00-00"


# --- PokéWallet API Client ---

class PokeWalletClient:
    def __init__(self, client: httpx.AsyncClient):
        self.client = client
        self.base_url = POKEWALLET_API_BASE
        self.headers = {}
        if POKEWALLET_API_KEY:
            self.headers["X-API-Key"] = POKEWALLET_API_KEY
        self._sets_cache: list[dict] = []
        self._sets_cache_time: float = 0

    async def get_sets(self) -> list[dict]:
        if self._sets_cache and (time.time() - self._sets_cache_time) < 300:
            return self._sets_cache

        try:
            resp = await self.client.get(
                f"{self.base_url}/sets", headers=self.headers, timeout=30
            )
            print(f"[PW] GET /sets -> {resp.status_code}")
            resp.raise_for_status()
            data = resp.json()
            items = data if isinstance(data, list) else data.get("data", data.get("sets", []))
            self._sets_cache = items
            self._sets_cache_time = time.time()
            print(f"[PW] Total sets: {len(items)}")
            return items
        except Exception as e:
            print(f"[PW] Error fetching sets: {e}")
            return []

    async def get_set_cards(self, set_code: str) -> list[dict]:
        all_cards = []
        page = 1
        while True:
            try:
                resp = await self.client.get(
                    f"{self.base_url}/sets/{set_code}",
                    params={"page": page, "limit": 50},
                    headers=self.headers,
                    timeout=30,
                )
                print(f"[PW] GET /sets/{set_code}?page={page} -> {resp.status_code}")
                if resp.status_code != 200:
                    break
                data = resp.json()
                cards = data.get("cards", data.get("data", []))
                if isinstance(data, list):
                    cards = data
                if not cards:
                    break
                all_cards.extend(cards)
                total = data.get("total_cards", data.get("total", 0))
                if total and len(all_cards) >= total:
                    break
                if len(cards) < 50:
                    break
                page += 1
                await asyncio.sleep(0.5)
            except Exception as e:
                print(f"[PW] Error fetching cards for {set_code} page {page}: {e}")
                break

        print(f"[PW] Total cards for {set_code}: {len(all_cards)}")
        return all_cards

    async def search_card(self, query: str) -> list[dict]:
        try:
            resp = await self.client.get(
                f"{self.base_url}/search",
                params={"q": query},
                headers=self.headers,
                timeout=30,
            )
            print(f"[PW] GET /search?q={query} -> {resp.status_code}")
            if resp.status_code != 200:
                return []
            data = resp.json()
            results = data.get("results", data.get("data", []))
            if isinstance(data, list):
                results = data
            print(f"[PW] Search '{query}': {len(results)} results")
            return results
        except Exception as e:
            print(f"[PW] Search error: {e}")
            return []

    def get_image_url(self, card_id: str, size: str = "high") -> str:
        return f"{self.base_url}/images/{card_id}?size={size}"

    @staticmethod
    def extract_cardmarket_price(card_data: dict) -> tuple[float, float, str]:
        """Extract Cardmarket avg and trend prices from PokéWallet card data.
        Returns (avg_price, trend_price, product_url).
        Checks multiple possible data structures.
        """
        # Try multiple paths for cardmarket data
        cm = card_data.get("cardmarket") or card_data.get("cm") or {}
        if not cm and "prices" in card_data:
            p = card_data["prices"]
            if isinstance(p, dict) and ("cardmarket" in p or "avg" in p or "trend" in p):
                cm = p.get("cardmarket", p)

        if not cm:
            return 0.0, 0.0, ""

        product_url = cm.get("product_url", cm.get("url", ""))

        # Try .prices (list or dict)
        prices_data = cm.get("prices", cm)

        def _extract(p: dict) -> tuple[float, float]:
            avg = float(p.get("avg", 0) or p.get("averageSellPrice", 0) or p.get("average", 0) or 0)
            trend = float(p.get("trend", 0) or p.get("trendPrice", 0) or 0)
            low = float(p.get("low", 0) or p.get("lowPrice", 0) or 0)
            price = trend or avg or low
            return price, trend

        if isinstance(prices_data, list):
            for p in prices_data:
                vtype = p.get("variant_type", "")
                if vtype in ("normal", "holo", ""):
                    price, trend = _extract(p)
                    if price > 0:
                        return price, trend, product_url
            if prices_data:
                price, trend = _extract(prices_data[0])
                return price, trend, product_url
        elif isinstance(prices_data, dict) and prices_data is not cm:
            price, trend = _extract(prices_data)
            return price, trend, product_url

        # Direct fields on cm itself
        price, trend = _extract(cm)
        if price > 0:
            return price, trend, product_url

        return 0.0, 0.0, product_url

    @staticmethod
    def extract_card_info(card_data: dict) -> dict:
        """Extract normalized card info from PokéWallet card data."""
        ci = card_data.get("card_info", {})
        card_id = card_data.get("id", "")
        return {
            "id": card_id,
            "name": ci.get("name") or card_data.get("name") or "Unknown",
            "set_name": ci.get("set_name", card_data.get("set_name", "")),
            "set_code": ci.get("set_code", card_data.get("set_code", "")),
            "card_number": ci.get("card_number") or card_data.get("number") or card_data.get("card_number") or "",
            "rarity": ci.get("rarity") or card_data.get("rarity") or "",
        }


# --- eBay Browse API Client ---

class EbayBrowseAPI:
    def __init__(self, client: httpx.AsyncClient):
        self.client = client
        self._access_token: str = ""
        self._token_expires: float = 0
        if EBAY_ENVIRONMENT == "SANDBOX":
            self.auth_url = "https://api.sandbox.ebay.com/identity/v1/oauth2/token"
            self.api_url = "https://api.sandbox.ebay.com/buy/browse/v1"
        else:
            self.auth_url = "https://api.ebay.com/identity/v1/oauth2/token"
            self.api_url = "https://api.ebay.com/buy/browse/v1"

    async def _get_token(self) -> str:
        if self._access_token and time.time() < self._token_expires - 60:
            return self._access_token

        if not EBAY_CLIENT_ID or not EBAY_CLIENT_SECRET:
            print("[eBay] No API credentials configured")
            return ""

        credentials = base64.b64encode(
            f"{EBAY_CLIENT_ID}:{EBAY_CLIENT_SECRET}".encode()
        ).decode()

        try:
            resp = await self.client.post(
                self.auth_url,
                headers={
                    "Content-Type": "application/x-www-form-urlencoded",
                    "Authorization": f"Basic {credentials}",
                },
                data={
                    "grant_type": "client_credentials",
                    "scope": "https://api.ebay.com/oauth/api_scope",
                },
                timeout=15,
            )
            print(f"[eBay] OAuth token -> {resp.status_code}")
            if resp.status_code == 200:
                data = resp.json()
                self._access_token = data["access_token"]
                self._token_expires = time.time() + data.get("expires_in", 7200)
                return self._access_token
            else:
                print(f"[eBay] OAuth error: {resp.text[:300]}")
                return ""
        except Exception as e:
            print(f"[eBay] OAuth exception: {e}")
            return ""

    async def search_items(self, query: str, max_results: int = 10) -> list[dict]:
        token = await self._get_token()
        if not token:
            return []

        try:
            params = {
                "q": query,
                "category_ids": "183454",
                "filter": "buyingOptions:{FIXED_PRICE}",
                "sort": "price",
                "limit": str(min(max_results, 50)),
            }
            resp = await self.client.get(
                f"{self.api_url}/item_summary/search",
                params=params,
                headers={
                    "Authorization": f"Bearer {token}",
                    "X-EBAY-C-MARKETPLACE-ID": "EBAY_GB",
                    "X-EBAY-C-ENDUSERCTX": "contextualLocation=country=GB",
                },
                timeout=30,
            )
            print(f"[eBay] Search '{query[:40]}' -> {resp.status_code}")

            if resp.status_code == 200:
                data = resp.json()
                items = data.get("itemSummaries", [])
                total = data.get("total", 0)
                normalized = self._normalize_items(items)
                print(f"[eBay] Found {total} raw, {len(items)} returned, {len(normalized)} after filter")
                return normalized
            elif resp.status_code == 429:
                print("[eBay] Rate limited")
                return []
            else:
                print(f"[eBay] Search error {resp.status_code}: {resp.text[:200]}")
                return []
        except Exception as e:
            print(f"[eBay] Search exception: {e}")
            return []

    EBAY_SKIP_KEYWORDS = {
        "psa ", "cgc ", "bgs ", " graded", " slab",
        "sealed booster", "booster box", " etb ",
        " bulk ", "job lot", " bundle ", "proxy ",
        " custom ", " replica ", "token card",
        " lot of ", " x4 ", " x3 ", " x2 ",
        "playset ", " set of ",
    }

    def _normalize_items(self, items: list[dict]) -> list[dict]:
        results = []
        for item in items:
            try:
                title = item.get("title", "")
                title_lower = title.lower()
                if any(kw in title_lower for kw in self.EBAY_SKIP_KEYWORDS):
                    continue

                price_data = item.get("price", {})
                price_str = price_data.get("value", "0")
                currency = price_data.get("currency", "GBP")
                price = float(price_str)

                shipping = 0.0
                ship_opts = item.get("shippingOptions", [])
                if ship_opts:
                    ship_cost = ship_opts[0].get("shippingCost", {})
                    shipping = float(ship_cost.get("value", "0"))

                image_url = ""
                img = item.get("image", {})
                if img:
                    image_url = img.get("imageUrl", "")
                thumbnails = item.get("thumbnailImages", [])
                if not image_url and thumbnails:
                    image_url = thumbnails[0].get("imageUrl", "")

                condition = item.get("condition", "")
                if isinstance(condition, dict):
                    condition = condition.get("conditionDisplayName", "")

                item_url = item.get("itemWebUrl", item.get("itemHref", ""))

                if price > 0:
                    results.append({
                        "title": title,
                        "price_gbp": price,
                        "shipping_gbp": shipping,
                        "total_gbp": price + shipping,
                        "url": item_url,
                        "image_url": image_url,
                        "condition": condition,
                        "currency": currency,
                        "item_id": item.get("itemId", ""),
                    })
            except Exception:
                continue
        return results


# --- Arbitrage Calculator ---

class ArbitrageCalculator:
    @staticmethod
    def calculate(ebay_total_gbp: float, cardmarket_price_eur: float) -> dict:
        total_cost_eur = ebay_total_gbp * GBP_TO_EUR
        cardmarket_fee = cardmarket_price_eur * (CARDMARKET_COMMISSION_PERCENT / 100)
        selling_revenue_eur = cardmarket_price_eur - cardmarket_fee - CARDMARKET_SHIPPING_COST_EUR
        profit_eur = selling_revenue_eur - total_cost_eur
        profit_percent = (profit_eur / total_cost_eur * 100) if total_cost_eur > 0 else 0

        return {
            "total_cost_eur": round(total_cost_eur, 2),
            "selling_price_after_fees_eur": round(selling_revenue_eur, 2),
            "profit_eur": round(profit_eur, 2),
            "profit_percent": round(profit_percent, 1),
            "is_profitable": profit_eur >= MIN_PROFIT_EUR and profit_percent >= MIN_PROFIT_PERCENT,
        }


# --- Telegram Bot ---

class TelegramBot:
    def __init__(self, client: httpx.AsyncClient):
        self.client = client
        self.token = TELEGRAM_TOKEN
        self.chat_id = TELEGRAM_CHAT_ID
        self.last_update_id = 0

    async def send_profitable_card(self, card: CardPrice):
        if not self.token or not self.chat_id:
            return

        text = (
            f"💰 *KÂR FIRSATI!*\n\n"
            f"🃏 *{self._esc(card.name)}*\n"
            f"📦 Set: {self._esc(card.set_name)}\n"
            f"🔢 \\#{card.card_number}\n"
            f"⭐ {self._esc(card.rarity)}\n\n"
            f"🇬🇧 *eBay UK:* £{card.ebay_total_gbp:.2f} \\(€{card.total_cost_eur:.2f}\\)\n"
            f"🇪🇺 *Cardmarket:* €{card.cardmarket_price_eur:.2f}\n"
            f"💵 *Satış sonrası:* €{card.selling_price_after_fees_eur:.2f}\n\n"
            f"✅ *KÂR: €{card.profit_eur:.2f} \\({card.profit_percent:.1f}%\\)*\n\n"
            f"🔗 [eBay'den Al]({card.ebay_url})\n"
            f"🔗 [Cardmarket]({card.cardmarket_url})"
        )

        if card.image_url:
            await self._send_photo(card.image_url, text)
        else:
            await self._send_message(text)

    async def send_set_summary(self, set_name: str, profitable_cards: list[CardPrice]):
        if not self.token or not self.chat_id or not profitable_cards:
            return

        text = (
            f"📊 *SET ANALİZİ TAMAMLANDI*\n\n"
            f"📦 *{self._esc(set_name)}*\n"
            f"💰 Kârlı kart: *{len(profitable_cards)}*\n\n"
        )
        for i, card in enumerate(profitable_cards[:10], 1):
            text += (
                f"{i}\\. {self._esc(card.name)} \\#{card.card_number}\n"
                f"   💵 €{card.profit_eur:.2f} \\({card.profit_percent:.1f}%\\)\n"
            )
        if len(profitable_cards) > 10:
            text += f"\n\\.\\.\\.ve {len(profitable_cards) - 10} kart daha\\!"

        await self._send_message(text)

    async def send_text(self, text: str, chat_id: str = ""):
        await self._send_message(text, parse_mode=None, chat_id=chat_id)

    async def send_html(self, text: str, chat_id: str = ""):
        await self._send_message(text, parse_mode="HTML", chat_id=chat_id)

    async def send_inline_keyboard(self, text: str, buttons: list[list[dict]], chat_id: str = ""):
        if not self.token:
            return
        try:
            await self.client.post(
                f"https://api.telegram.org/bot{self.token}/sendMessage",
                json={
                    "chat_id": chat_id or self.chat_id,
                    "text": text,
                    "reply_markup": {"inline_keyboard": buttons},
                },
                timeout=30,
            )
        except Exception as e:
            print(f"[Telegram] Inline keyboard failed: {e}")

    async def answer_callback(self, callback_id: str, text: str = ""):
        if not self.token:
            return
        try:
            await self.client.post(
                f"https://api.telegram.org/bot{self.token}/answerCallbackQuery",
                json={"callback_query_id": callback_id, "text": text or ""},
                timeout=10,
            )
        except Exception:
            pass

    async def get_updates(self) -> list[dict]:
        if not self.token:
            return []
        try:
            resp = await self.client.get(
                f"https://api.telegram.org/bot{self.token}/getUpdates",
                params={"offset": self.last_update_id + 1, "timeout": 5},
                timeout=15,
            )
            data = resp.json()
            updates = data.get("result", [])
            if updates:
                self.last_update_id = updates[-1]["update_id"]
                for u in updates:
                    if "callback_query" in u:
                        print(f"[Bot] Callback received: {u['callback_query'].get('data', '?')}")
                    elif "message" in u:
                        print(f"[Bot] Message: {u['message'].get('text', '?')[:50]}")
            return updates
        except Exception as e:
            print(f"[Bot] getUpdates error: {e}")
            return []

    async def _send_photo(self, photo_url: str, caption: str):
        try:
            await self.client.post(
                f"https://api.telegram.org/bot{self.token}/sendPhoto",
                json={
                    "chat_id": self.chat_id,
                    "photo": photo_url,
                    "caption": caption,
                    "parse_mode": "MarkdownV2",
                },
                timeout=30,
            )
        except Exception as e:
            print(f"[Telegram] Photo failed ({e}), sending as text")
            await self._send_message(caption)

    async def _send_message(self, text: str, parse_mode: str = "MarkdownV2", chat_id: str = ""):
        if not self.token:
            return
        try:
            payload = {"chat_id": chat_id or self.chat_id, "text": text}
            if parse_mode:
                payload["parse_mode"] = parse_mode
            await self.client.post(
                f"https://api.telegram.org/bot{self.token}/sendMessage",
                json=payload,
                timeout=30,
            )
        except Exception as e:
            print(f"[Telegram] Send failed: {e}")

    @staticmethod
    def _esc(text: str) -> str:
        for ch in r"_*[]()~`>#+-=|{}.!":
            text = text.replace(ch, f"\\{ch}")
        return text


# --- State Manager ---

class StateManager:
    def __init__(self):
        self.state = self._load(STATE_FILE, {"analyzed_sets": {}, "last_run": ""})
        self.results = self._load(RESULTS_FILE, {"profitable_cards": []})

    def _load(self, path: str, default: dict) -> dict:
        try:
            with open(path, "r") as f:
                return json.load(f)
        except (FileNotFoundError, json.JSONDecodeError):
            return default

    def save(self):
        with open(STATE_FILE, "w") as f:
            json.dump(self.state, f, indent=2)
        with open(RESULTS_FILE, "w") as f:
            json.dump(self.results, f, indent=2, default=str)

    def mark_set_analyzed(self, set_code: str, set_name: str, profitable_count: int):
        self.state["analyzed_sets"][set_code] = {
            "name": set_name,
            "profitable_count": profitable_count,
            "last_checked": time.strftime("%Y-%m-%d %H:%M"),
        }
        self.state["last_run"] = time.strftime("%Y-%m-%d %H:%M")
        self.save()

    def add_profitable_card(self, card: CardPrice):
        self.results["profitable_cards"].append({
            "name": card.name,
            "set_name": card.set_name,
            "card_number": card.card_number,
            "rarity": card.rarity,
            "ebay_price_gbp": card.ebay_total_gbp,
            "cardmarket_price_eur": card.cardmarket_price_eur,
            "profit_eur": card.profit_eur,
            "profit_percent": card.profit_percent,
            "ebay_url": card.ebay_url,
            "image_url": card.image_url,
            "found_at": time.strftime("%Y-%m-%d %H:%M"),
        })
        self.save()

    def get_top_results(self, limit: int = 10) -> list[dict]:
        cards = self.results.get("profitable_cards", [])
        return sorted(cards, key=lambda c: c.get("profit_eur", 0), reverse=True)[:limit]

    def is_set_recent(self, set_code: str, hours: int = 24) -> bool:
        info = self.state.get("analyzed_sets", {}).get(set_code)
        if not info:
            return False
        try:
            from datetime import datetime
            checked = datetime.strptime(info["last_checked"], "%Y-%m-%d %H:%M")
            return (datetime.now() - checked).total_seconds() < hours * 3600
        except Exception:
            return False


# --- Arbitrage Engine ---

class ArbitrageEngine:
    def __init__(self):
        self.client: Optional[httpx.AsyncClient] = None
        self.pokewallet: Optional[PokeWalletClient] = None
        self.ebay: Optional[EbayBrowseAPI] = None
        self.telegram: Optional[TelegramBot] = None
        self.state: Optional[StateManager] = None
        self.calculator = ArbitrageCalculator()
        self._running_task: Optional[asyncio.Task] = None
        self._task_label: str = ""

    async def start(self):
        self.client = httpx.AsyncClient(follow_redirects=True, timeout=30)
        self.pokewallet = PokeWalletClient(self.client)
        self.ebay = EbayBrowseAPI(self.client)
        self.telegram = TelegramBot(self.client)
        self.state = StateManager()

    async def stop(self):
        if self.client:
            await self.client.aclose()

    def _group_series(self, sets: list[dict]) -> list[tuple[str, dict]]:
        series_map = {}
        for s in sets:
            series = _get_series_from_set(s)
            if series not in series_map:
                series_map[series] = {"sets": [], "latest_date": "0000-00-00"}
            series_map[series]["sets"].append(s)
            d = _parse_date(s)
            if d > series_map[series]["latest_date"]:
                series_map[series]["latest_date"] = d
        return sorted(series_map.items(), key=lambda x: x[1]["latest_date"], reverse=True)

    async def list_series_buttons(self) -> tuple[str, list[list[dict]]]:
        sets = await self.pokewallet.get_sets()
        if not sets:
            return "❌ Seri listesi alınamadı.", []

        sorted_series = self._group_series(sets)

        buttons = []
        for name, info in sorted_series[:20]:
            count = len(info["sets"])
            btn_text = f"{name} ({count})"
            cb_data = f"series:{name[:50]}"
            buttons.append([{"text": btn_text, "callback_data": cb_data}])

        return "📚 Bir seri seçin:", buttons

    async def list_sets_buttons(self, series_filter: str) -> tuple[str, list[list[dict]]]:
        sets = await self.pokewallet.get_sets()
        if not sets:
            return "❌ Set listesi alınamadı.", []

        filter_lower = series_filter.lower()
        filtered = [
            s for s in sets
            if filter_lower in _get_series_from_set(s).lower()
        ]
        if not filtered:
            filtered = [s for s in sets if filter_lower in (s.get("name") or "").lower()]
        if not filtered:
            return f"❌ '{series_filter}' serisi bulunamadı.", []

        skip_words = {"energies", "energy", "deck", "sleeves", "box", "tin", "collection"}
        filtered = [
            s for s in filtered
            if not any(w in (s.get("name") or "").lower() for w in skip_words)
        ]

        sets_sorted = sorted(filtered, key=lambda s: _parse_date(s), reverse=True)

        buttons = []
        for s in sets_sorted[:30]:
            name = s.get("name", "?")
            set_code = s.get("set_code", s.get("id", ""))
            total = s.get("card_count", s.get("total", "?"))
            btn_text = f"{name} ({total} kart)"
            cb_data = f"analyze:{set_code}"
            buttons.append([{"text": btn_text, "callback_data": cb_data}])

        return f"📦 {series_filter} ({len(sets_sorted)} set):", buttons

    async def list_sets(self, series_filter: str = "") -> str:
        sets = await self.pokewallet.get_sets()
        if not sets:
            return "❌ Set listesi alınamadı."

        if series_filter:
            filter_lower = series_filter.lower()
            filtered = [
                s for s in sets
                if filter_lower in _get_series_from_set(s).lower()
            ]
            if not filtered:
                filtered = [s for s in sets if filter_lower in (s.get("name") or "").lower()]
            if not filtered:
                return f"❌ '{series_filter}' serisi bulunamadı. /series ile serileri listele."
            sets = filtered

        sets_sorted = sorted(sets, key=lambda s: _parse_date(s), reverse=True)
        title = f"📦 {series_filter}" if series_filter else "📦 Tüm setler"
        lines = [f"{title} ({len(sets)} set, en yeniden eskiye):\n"]
        for i, s in enumerate(sets_sorted[:25], 1):
            name = s.get("name", "?")
            set_code = s.get("set_code", s.get("id", ""))
            total = s.get("card_count", s.get("total", "?"))
            date = s.get("release_date") or s.get("releaseDate") or ""
            lines.append(f"{i}. [{set_code}] {name} ({total} kart) {date}")
        if len(sets) > 25:
            lines.append(f"\n...ve {len(sets) - 25} set daha")
        lines.append(f"\nBir seti analiz etmek için:\n/analyze <set_code>")
        return "\n".join(lines)

    async def _fetch_card_cm_price(self, card_data: dict, sem: asyncio.Semaphore):
        """Fetch CM price for a single card via /cards/:id with semaphore."""
        card_id = card_data.get("id", "")
        if not card_id:
            return 0.0, 0.0, ""
        async with sem:
            try:
                resp = await self.client.get(
                    f"{self.pokewallet.base_url}/cards/{card_id}",
                    headers=self.pokewallet.headers, timeout=15,
                )
                if resp.status_code == 200:
                    detail = resp.json()
                    return PokeWalletClient.extract_cardmarket_price(detail)
                await asyncio.sleep(0.5)
            except Exception:
                pass
        return 0.0, 0.0, ""

    async def _fetch_ebay_price(self, query: str, cm_price: float, sem: asyncio.Semaphore):
        """Fetch eBay price with semaphore. Validates against CM price to skip wrong matches."""
        async with sem:
            try:
                await asyncio.sleep(0.5)
                listings = await self.ebay.search_items(query, max_results=10)
                if not listings:
                    return 0.0
                cm_in_gbp = cm_price / GBP_TO_EUR if cm_price > 0 else 0
                valid = []
                for li in listings:
                    price = li["total_gbp"]
                    if cm_in_gbp > 5 and price < cm_in_gbp * 0.15:
                        continue
                    valid.append(li)
                if valid:
                    valid.sort(key=lambda x: x["total_gbp"])
                    top3 = valid[:3]
                    return sum(l["total_gbp"] for l in top3) / len(top3)
                elif listings:
                    return listings[0]["total_gbp"]
            except Exception:
                pass
        return 0.0

    def cancel_running_task(self) -> bool:
        if self._running_task and not self._running_task.done():
            self._running_task.cancel()
            label = self._task_label
            self._running_task = None
            self._task_label = ""
            return True
        return False

    async def analyze_set_table(self, set_code: str, chat_id: str = "") -> list:
        """Fetch high-rarity cards from a set and return a price comparison table."""
        cards = await self.pokewallet.get_set_cards(set_code)
        if not cards:
            return [f"❌ {set_code}: Kart bulunamadı."]

        sample = cards[0]
        cm = sample.get("cardmarket", sample.get("cm", {}))
        cm_prices = cm.get("prices", []) if isinstance(cm, dict) else []
        set_has_prices = bool(cm_prices)
        print(f"[Table] {set_code}: set_has_prices={set_has_prices}, total_cards={len(cards)}")

        first_info = PokeWalletClient.extract_card_info(cards[0])
        set_name = first_info.get("set_name", "") or set_code

        is_promo = "promo" in set_name.lower() or "promo" in set_code.lower()
        ebay_available = bool(EBAY_CLIENT_ID and EBAY_CLIENT_SECRET)

        filtered = []
        all_rarities = set()
        for card_data in cards:
            info = PokeWalletClient.extract_card_info(card_data)
            name_lower = (info["name"] or "").lower()
            if any(skip in name_lower for skip in SKIP_CARD_NAMES):
                continue
            rarity = info["rarity"]
            if rarity:
                all_rarities.add(rarity)
            if not is_promo and not _is_high_rarity(rarity):
                continue
            filtered.append((card_data, info))

        print(f"[Table] {set_name}: {len(filtered)} cards after filter, rarities: {all_rarities}")

        if not filtered:
            rarity_list = ", ".join(sorted(all_rarities)) if all_rarities else "bilinmiyor"
            return [f"❌ {set_name}: Değerli kart bulunamadı.\nMevcut rarity'ler: {rarity_list}"]

        # Phase 1: fetch CM prices in parallel (5 concurrent)
        pw_sem = asyncio.Semaphore(5)
        if not set_has_prices:
            cm_tasks = [self._fetch_card_cm_price(cd, pw_sem) for cd, _ in filtered]
            cm_results = await asyncio.gather(*cm_tasks)
        else:
            cm_results = [PokeWalletClient.extract_cardmarket_price(cd) for cd, _ in filtered]

        high_rarity_cards = []
        for i, (card_data, info) in enumerate(filtered):
            cm_price, cm_trend, cm_url = cm_results[i]
            high_rarity_cards.append({
                "info": info, "cm_price": cm_price,
                "cm_trend": cm_trend, "cm_url": cm_url,
            })

        high_rarity_cards.sort(key=lambda x: x["cm_price"], reverse=True)

        if chat_id:
            await self.telegram.send_text(
                f"📊 {set_name}: {len(high_rarity_cards)} kart bulundu, eBay fiyatları alınıyor...",
                chat_id,
            )

        # Phase 2: fetch eBay prices in parallel (3 concurrent)
        ebay_sem = asyncio.Semaphore(3)
        rows = []
        if ebay_available:
            ebay_queries = []
            for entry in high_rarity_cards:
                info = entry["info"]
                cn = info["name"] or "?"
                cnum = info["card_number"] or ""
                num_only = cnum.split("/")[0].strip() if "/" in cnum else cnum.strip()
                num_only = re.sub(r'[^0-9]', '', num_only)
                q = f"{cn} {num_only} pokemon card" if num_only else f"{cn} pokemon card"
                ebay_queries.append(q)
            ebay_tasks = [
                self._fetch_ebay_price(q, high_rarity_cards[i]["cm_price"], ebay_sem)
                for i, q in enumerate(ebay_queries)
            ]
            ebay_results = await asyncio.gather(*ebay_tasks)
        else:
            ebay_results = [0.0] * len(high_rarity_cards)

        for i, entry in enumerate(high_rarity_cards):
            info = entry["info"]
            card_name = info["name"] or "?"
            card_number = info["card_number"] or ""
            rarity = info["rarity"]
            cm_price = entry["cm_price"]

            short_rarity = self._short_rarity(rarity or "")
            num_part = card_number.split("/")[0].strip() if "/" in card_number else card_number.strip()
            num_part = re.sub(r'[^0-9]', '', num_part)
            clean_name = re.sub(r'\s*[-–]\s*\d+/\d+.*$', '', card_name).strip()
            clean_name = re.sub(r'\s*[-–]\s*\d+\s*$', '', clean_name).strip()
            if num_part and num_part not in clean_name:
                display_name = f"{clean_name} {num_part}"
            else:
                display_name = clean_name

            ebay_price_gbp = ebay_results[i]
            ebay_price_eur = ebay_price_gbp * GBP_TO_EUR

            cm_str = f"{cm_price:.1f}" if cm_price > 0 and cm_price < 10 else (f"{cm_price:.0f}" if cm_price > 0 else "-")
            ebay_str = f"{ebay_price_eur:.1f}" if ebay_price_eur > 0 and ebay_price_eur < 10 else (f"{ebay_price_eur:.0f}" if ebay_price_eur > 0 else "-")

            if ebay_price_eur > 0 and cm_price > 0:
                calc = self.calculator.calculate(ebay_price_gbp, cm_price)
                profit = calc["profit_eur"]
                profit_str = f"{profit:.0f}"
                profitable = profit > 0
            else:
                profit_str = "-"
                profitable = None

            rows.append((display_name, short_rarity, cm_str, ebay_str, profit_str, profitable))

        NW = 15
        RW = 3
        def _fmt_row(name, rar, cm_s, eb_s, pr_s):
            esc = html_mod.escape(name)
            if len(esc) > NW:
                esc = esc[:NW]
            r = rar[:RW]
            return f"{esc:<{NW}}|{r:>{RW}}|{cm_s:>5}|{eb_s:>5}|{pr_s:>5}"

        hdr = f"{'Kart':<{NW}}|{'R':>{RW}}|{'CM':>5}|{'eB':>5}|{'Kar':>5}"
        sep = "-" * NW + "+" + "-" * RW + "+" + "-" * 5 + "+" + "-" * 5 + "+" + "-" * 5

        green_rows = [r for r in rows if r[5] is True]
        red_rows = [r for r in rows if r[5] is False]
        nodata_rows = [r for r in rows if r[5] is None]

        messages = []

        def _build_table(section_rows, max_msg=3600):
            lines = []
            cur_len = 0
            chunks = []
            for name, rar, cm_s, eb_s, pr_s, _ in section_rows:
                line = _fmt_row(name, rar, cm_s, eb_s, pr_s)
                if cur_len + len(line) + len(hdr) + len(sep) + 30 > max_msg and lines:
                    chunks.append("<pre>" + "\n".join([hdr, sep] + lines) + "</pre>")
                    lines = []
                    cur_len = 0
                lines.append(line)
                cur_len += len(line) + 1
            if lines:
                chunks.append("<pre>" + "\n".join([hdr, sep] + lines) + "</pre>")
            return chunks

        title = f"📊 <b>{html_mod.escape(set_name)}</b>\n\n"

        if green_rows:
            green_tables = _build_table(green_rows)
            first = title + f"🟢 <b>KARLI ({len(green_rows)})</b>\n" + green_tables[0]
            messages.append(first)
            messages.extend(green_tables[1:])
            title = ""

        if red_rows:
            red_tables = _build_table(red_rows)
            prefix = title or ""
            first = prefix + f"🔴 Zararli ({len(red_rows)})\n" + red_tables[0]
            messages.append(first)
            messages.extend(red_tables[1:])
            title = ""

        if nodata_rows:
            nd_tables = _build_table(nodata_rows)
            prefix = title or ""
            first = prefix + f"⚪ eBay yok ({len(nodata_rows)})\n" + nd_tables[0]
            messages.append(first)
            messages.extend(nd_tables[1:])

        if not messages:
            messages = [f"❌ {set_name}: Sonuc bulunamadi."]

        total_shown = len(rows)
        footer = f"\n\n📈 {total_shown} kart | Fiyatlar EUR"
        footer += f"\nCM: %5 + €1.50 kesinti | GBP→EUR x{GBP_TO_EUR}"
        if not ebay_available:
            footer += "\n⚠️ eBay API ayarlanmamis"
        messages[-1] += footer

        return messages

    @staticmethod
    def _short_rarity(rarity: str) -> str:
        r = rarity.lower().strip()
        if "special illustration" in r or r == "sir":
            return "SIR"
        if "illustration" in r or r == "ir":
            return "IR"
        if "hyper ultra" in r or r == "hur":
            return "HUR"
        if "hyper" in r or r == "hr":
            return "HR"
        if "ultra" in r or r == "ur":
            return "UR"
        if "special art" in r or r == "sar":
            return "SAR"
        if "art rare" in r or r == "ar":
            return "AR"
        if "secret" in r or r == "sr":
            return "SR"
        if "full art" in r or r == "fa":
            return "FA"
        if "crown" in r:
            return "CR"
        if "gold" in r:
            return "GLD"
        if "immersive" in r:
            return "IMR"
        if "shiny" in r or r == "shiny rare":
            return "SHN"
        return rarity[:3].upper()

    async def analyze_set(self, set_code: str, force: bool = False) -> list[CardPrice]:
        if not force and self.state.is_set_recent(set_code):
            return []

        cards = await self.pokewallet.get_set_cards(set_code)
        if not cards:
            print(f"[Analyze] No cards returned for {set_code}")
            return []

        first_info = PokeWalletClient.extract_card_info(cards[0]) if cards else {}
        set_name = first_info.get("set_name", "") or set_code
        profitable = []

        total_cards = len(cards)
        cards_with_cm_price = 0
        cards_checked_ebay = 0
        cards_found_ebay = 0
        ebay_available = bool(EBAY_CLIENT_ID and EBAY_CLIENT_SECRET)

        print(f"[Analyze] {set_name} ({set_code}): {total_cards} cards, eBay API: {'yes' if ebay_available else 'no credentials'}")

        if cards:
            sample = cards[0]
            print(f"[Analyze] Sample card keys: {list(sample.keys())}")
            cm_sample = sample.get("cardmarket", {})
            print(f"[Analyze] Sample cardmarket: {json.dumps(cm_sample, default=str)[:400]}")

        for card_data in cards:
            info = PokeWalletClient.extract_card_info(card_data)
            card_name = info["name"]
            card_number = info["card_number"]
            card_id = info["id"]
            rarity = info["rarity"]

            image_url = self.pokewallet.get_image_url(card_id) if card_id else ""

            cardmarket_price, cardmarket_trend, cardmarket_url = PokeWalletClient.extract_cardmarket_price(card_data)

            if cardmarket_price < 1.0:
                continue

            cards_with_cm_price += 1

            if not ebay_available:
                continue

            search_query = f"Pokemon {card_name} {card_number} {set_name}"
            ebay_listings = await self.ebay.search_items(search_query, max_results=5)
            cards_checked_ebay += 1
            await asyncio.sleep(REQUEST_DELAY)

            if not ebay_listings:
                continue

            cards_found_ebay += 1

            sorted_listings = sorted(ebay_listings, key=lambda x: x["total_gbp"])
            cheapest_3 = sorted_listings[:3]
            avg_total_gbp = sum(l["total_gbp"] for l in cheapest_3) / len(cheapest_3)
            cheapest = cheapest_3[0]
            calc = self.calculator.calculate(avg_total_gbp, cardmarket_price)

            print(f"  [{card_name} #{card_number}] CM: €{cardmarket_price:.2f} | eBay avg: £{avg_total_gbp:.2f} (€{calc['total_cost_eur']:.2f}) | Kâr: €{calc['profit_eur']:.2f} ({calc['profit_percent']:.1f}%)")

            if calc["is_profitable"]:
                card_price = CardPrice(
                    name=card_name,
                    set_name=set_name,
                    set_code=set_code,
                    card_number=str(card_number),
                    rarity=rarity,
                    image_url=image_url or cheapest.get("image_url", ""),
                    ebay_price_gbp=cheapest["price_gbp"],
                    ebay_shipping_gbp=cheapest["shipping_gbp"],
                    ebay_total_gbp=cheapest["total_gbp"],
                    ebay_url=cheapest["url"],
                    ebay_condition=cheapest.get("condition", ""),
                    cardmarket_price_eur=cardmarket_price,
                    cardmarket_trend_eur=cardmarket_trend,
                    cardmarket_url=cardmarket_url or f"https://www.cardmarket.com/en/Pokemon/Products/Search?searchString={card_name.replace(' ', '+')}",
                    total_cost_eur=calc["total_cost_eur"],
                    selling_price_after_fees_eur=calc["selling_price_after_fees_eur"],
                    profit_eur=calc["profit_eur"],
                    profit_percent=calc["profit_percent"],
                    is_profitable=True,
                )
                profitable.append(card_price)
                self.state.add_profitable_card(card_price)
                await self.telegram.send_profitable_card(card_price)
                print(f"  ✅ {card_name} #{card_number} | Kâr: €{calc['profit_eur']:.2f}")

        summary = (
            f"[✓] {set_name}: {len(profitable)} kârlı kart | "
            f"Toplam: {total_cards}, CM≥€1: {cards_with_cm_price}, "
            f"eBay sorgu: {cards_checked_ebay}, eBay buldu: {cards_found_ebay}"
        )
        print(summary)

        if not profitable and cards_with_cm_price > 0:
            diag = f"📊 Analiz detayı:\n"
            diag += f"Toplam kart: {total_cards}\n"
            diag += f"CM fiyatı ≥€1: {cards_with_cm_price}\n"
            diag += f"eBay'de arandı: {cards_checked_ebay}\n"
            diag += f"eBay'de bulundu: {cards_found_ebay}\n"
            if not ebay_available:
                diag += "\n⚠️ eBay API anahtarları ayarlanmamış!\nEBAY_CLIENT_ID ve EBAY_CLIENT_SECRET gerekli."
            elif cards_found_ebay == 0 and cards_checked_ebay > 0:
                diag += "\n⚠️ eBay'de sonuç bulunamadı"
            elif cards_found_ebay > 0:
                diag += "\neBay fiyatları kâr eşiğini karşılamıyor"
            await self.telegram.send_text(diag)

        self.state.mark_set_analyzed(set_code, set_name, len(profitable))
        await self.telegram.send_set_summary(set_name, profitable)
        return profitable

    async def quick_check(self, card_name: str) -> str:
        results = await self.pokewallet.search_card(card_name)
        if not results:
            return f"❌ '{card_name}' bulunamadı."

        card_data = results[0]
        info = PokeWalletClient.extract_card_info(card_data)
        name = info["name"]
        card_number = info["card_number"]
        set_name = info["set_name"]
        card_id = info["id"]

        image_url = self.pokewallet.get_image_url(card_id) if card_id else ""
        cardmarket_price, _, cardmarket_url = PokeWalletClient.extract_cardmarket_price(card_data)

        if cardmarket_price < 0.5:
            return f"❌ {name}: Cardmarket fiyatı çok düşük (€{cardmarket_price:.2f})"

        if not EBAY_CLIENT_ID or not EBAY_CLIENT_SECRET:
            return (
                f"🃏 {name} #{card_number} ({set_name})\n"
                f"🇪🇺 Cardmarket: €{cardmarket_price:.2f}\n\n"
                f"⚠️ eBay API anahtarları ayarlanmamış.\n"
                f"EBAY_CLIENT_ID ve EBAY_CLIENT_SECRET gerekli."
            )

        search_query = f"Pokemon {name} {card_number}"
        ebay_listings = await self.ebay.search_items(search_query, max_results=5)

        if not ebay_listings:
            return f"❌ {name}: eBay UK'de bulunamadı."

        sorted_listings = sorted(ebay_listings, key=lambda x: x["total_gbp"])
        cheapest_3 = sorted_listings[:3]
        avg_total_gbp = sum(l["total_gbp"] for l in cheapest_3) / len(cheapest_3)
        cheapest = cheapest_3[0]
        calc = self.calculator.calculate(avg_total_gbp, cardmarket_price)

        status = "✅ KÂRLI" if calc["is_profitable"] else "❌ Kârsız"
        prices_info = " / ".join([f"£{l['total_gbp']:.2f}" for l in cheapest_3])
        return (
            f"{status}\n\n"
            f"🃏 {name} #{card_number} ({set_name})\n"
            f"🇬🇧 eBay UK: {prices_info}\n"
            f"🇬🇧 Ortalama: £{avg_total_gbp:.2f} (€{calc['total_cost_eur']:.2f})\n"
            f"🇪🇺 Cardmarket: €{cardmarket_price:.2f}\n"
            f"💵 Satış sonrası: €{calc['selling_price_after_fees_eur']:.2f}\n"
            f"💰 Kâr: €{calc['profit_eur']:.2f} ({calc['profit_percent']:.1f}%)\n"
            f"🔗 {cheapest['url']}"
        )


# --- Background Service ---

async def health_server():
    async def handle(reader, writer):
        await reader.read(1024)
        uptime = int(time.time() - _start_time)
        response = (
            "HTTP/1.1 200 OK\r\n"
            "Content-Type: text/plain\r\n\r\n"
            f"Pokemon Card Arbitrage Bot - Running ({uptime}s)\n"
        )
        writer.write(response.encode())
        await writer.drain()
        writer.close()

    server = await asyncio.start_server(handle, "0.0.0.0", PORT)
    print(f"[Health] Listening on port {PORT}")
    await server.serve_forever()


_start_time = time.time()


async def keep_alive_loop():
    """Ping own health endpoint every 10 min to prevent Render free tier sleep."""
    render_url = os.environ.get("RENDER_EXTERNAL_URL", "")
    if not render_url:
        print("[KeepAlive] RENDER_EXTERNAL_URL not set, skipping keep-alive")
        return
    print(f"[KeepAlive] Pinging {render_url} every 10 min")
    async with httpx.AsyncClient() as client:
        while True:
            await asyncio.sleep(600)
            try:
                resp = await client.get(render_url, timeout=10)
                print(f"[KeepAlive] Ping -> {resp.status_code}")
            except Exception as e:
                print(f"[KeepAlive] Ping failed: {e}")


async def telegram_command_loop(engine: ArbitrageEngine):
    print("[Bot] Telegram komut dinleme başladı...")

    while True:
        try:
            updates = await engine.telegram.get_updates()
            for update in updates:
                cb = update.get("callback_query")
                if cb:
                    cb_id = cb.get("id", "")
                    cb_data = cb.get("data", "")
                    cb_chat_id = str(cb.get("message", {}).get("chat", {}).get("id", ""))
                    print(f"[Bot] Processing callback: data='{cb_data}' chat={cb_chat_id}")

                    if cb_chat_id != TELEGRAM_CHAT_ID:
                        await engine.telegram.answer_callback(cb_id)
                        continue

                    try:
                        await engine.telegram.answer_callback(cb_id, "⏳ İşleniyor...")

                        if cb_data.startswith("series:"):
                            series_name = cb_data[7:]
                            print(f"[Bot] Loading sets for series: '{series_name}'")
                            text, buttons = await engine.list_sets_buttons(series_name)
                            if buttons:
                                await engine.telegram.send_inline_keyboard(text, buttons, cb_chat_id)
                            else:
                                await engine.telegram.send_text(text, cb_chat_id)

                        elif cb_data.startswith("analyze:"):
                            set_code = cb_data[8:]
                            if engine._running_task and not engine._running_task.done():
                                await engine.telegram.send_text(
                                    f"⏳ Zaten {engine._task_label} taranıyor. Durdurmak için /stop yaz.",
                                    cb_chat_id,
                                )
                                continue
                            await engine.telegram.send_text(f"🔍 {set_code} — değerli kartlar taranıyor...", cb_chat_id)

                            async def _run_analysis(sc=set_code, cid=cb_chat_id):
                                try:
                                    messages = await engine.analyze_set_table(sc, chat_id=cid)
                                    if isinstance(messages, str):
                                        messages = [messages]
                                    for msg in messages:
                                        await engine.telegram.send_html(msg, cid)
                                except asyncio.CancelledError:
                                    await engine.telegram.send_text(f"🛑 {sc} — tarama durduruldu.", cid)
                                except Exception as e:
                                    await engine.telegram.send_text(f"❌ {sc} — Hata: {e}", cid)
                                finally:
                                    engine._running_task = None
                                    engine._task_label = ""

                            engine._task_label = set_code
                            engine._running_task = asyncio.create_task(_run_analysis())
                    except Exception as e:
                        print(f"[Bot] Callback error: {e}")
                        await engine.telegram.send_text(f"❌ Hata: {e}", cb_chat_id)

                    continue

                msg = update.get("message", {})
                text = msg.get("text", "").strip()
                chat_id = str(msg.get("chat", {}).get("id", ""))

                if chat_id != TELEGRAM_CHAT_ID:
                    continue

                if text.startswith("/stop"):
                    if engine.cancel_running_task():
                        await engine.telegram.send_text("🛑 Tarama durduruldu.", chat_id)
                    else:
                        await engine.telegram.send_text("ℹ️ Şu an çalışan tarama yok.", chat_id)

                elif text.startswith("/series"):
                    title, buttons = await engine.list_series_buttons()
                    if buttons:
                        await engine.telegram.send_inline_keyboard(title, buttons, chat_id)
                    else:
                        await engine.telegram.send_text(title, chat_id)

                elif text.startswith("/sets"):
                    series_filter = text.replace("/sets", "").strip()
                    if series_filter:
                        title, buttons = await engine.list_sets_buttons(series_filter)
                        if buttons:
                            await engine.telegram.send_inline_keyboard(title, buttons, chat_id)
                        else:
                            await engine.telegram.send_text(title, chat_id)
                    else:
                        result = await engine.list_sets()
                        await engine.telegram.send_text(result, chat_id)

                elif text.startswith("/analyze"):
                    parts = text.split()
                    if len(parts) < 2:
                        await engine.telegram.send_text("Kullanım: /analyze <set_code>", chat_id)
                    else:
                        set_code = parts[1]
                        await engine.telegram.send_text(f"🔍 {set_code} analiz ediliyor...", chat_id)
                        profitable = await engine.analyze_set(set_code, force=True)
                        if not profitable:
                            await engine.telegram.send_text(f"❌ {set_code}: Kârlı kart bulunamadı.", chat_id)

                elif text.startswith("/check"):
                    card_name = text.replace("/check", "").strip()
                    if not card_name:
                        await engine.telegram.send_text(
                            "Kullanım: /check <kart adı>\n"
                            "Örnek: /check Charizard ex\n\n"
                            "Set analizi için /series butonlarını kullanın.",
                            chat_id,
                        )
                    else:
                        await engine.telegram.send_text(f"🔍 '{card_name}' aranıyor...", chat_id)
                        result = await engine.quick_check(card_name)
                        await engine.telegram.send_text(result, chat_id)

                elif text.startswith("/results"):
                    top = engine.state.get_top_results(10)
                    if not top:
                        await engine.telegram.send_text("Henüz kârlı kart bulunamadı.", chat_id)
                    else:
                        lines = ["💰 EN KÂRLI KARTLAR:\n"]
                        for i, c in enumerate(top, 1):
                            lines.append(
                                f"{i}. {c['name']} ({c['set_name']})\n"
                                f"   💵 €{c['profit_eur']:.2f} kâr ({c['profit_percent']:.1f}%)"
                            )
                        await engine.telegram.send_text("\n".join(lines), chat_id)

                elif text.startswith("/diag"):
                    await engine.telegram.send_text("🔧 Tanılama çalışıyor...", chat_id)
                    cl = engine.client
                    pw_headers = engine.pokewallet.headers
                    pw_base = engine.pokewallet.base_url

                    d = []

                    # PokéWallet /sets — show unique set_code prefixes
                    all_sets_data = []
                    try:
                        resp = await cl.get(f"{pw_base}/sets", headers=pw_headers, timeout=15)
                        d.append(f"✅ PW /sets → {resp.status_code}")
                        if resp.status_code == 200:
                            data = resp.json()
                            all_sets_data = data if isinstance(data, list) else data.get("data", [])
                            d.append(f"  {len(all_sets_data)} set bulundu")
                            if all_sets_data:
                                d.append(f"  Keys: {list(all_sets_data[0].keys())}")
                    except Exception as e:
                        d.append(f"❌ PW /sets → {e}")
                    await engine.telegram.send_text("🔧 1/4 Sets\n\n" + "\n".join(d), chat_id)

                    # Show all set codes grouped
                    if all_sets_data:
                        code_lines = []
                        sorted_by_date = sorted(all_sets_data, key=lambda s: _parse_date(s), reverse=True)
                        for s in sorted_by_date[:40]:
                            code = s.get("set_code", s.get("id", "?"))
                            name = s.get("name", "?")
                            series = _get_series_from_set(s)
                            code_lines.append(f"[{code}] {name} → {series}")
                        await engine.telegram.send_text(
                            f"🔧 2/4 Set Kodları (ilk 40):\n\n" + "\n".join(code_lines),
                            chat_id,
                        )

                    # PokéWallet card data structure
                    d2 = []
                    # Pick real set codes from the sets list
                    test_codes = []
                    if all_sets_data:
                        for s in all_sets_data[:100]:
                            sc = s.get("set_code", "")
                            nm = s.get("name", "")
                            if "SV" in nm[:4] and len(test_codes) < 1:
                                test_codes.append(sc)
                            elif "ME01" in nm[:5] and len(test_codes) < 2:
                                test_codes.append(sc)
                            elif "Genetic" in nm and len(test_codes) < 3:
                                test_codes.append(sc)
                        if not test_codes:
                            test_codes = [all_sets_data[0].get("set_code", "")]

                    for test_code in test_codes[:3]:
                        try:
                            resp = await cl.get(f"{pw_base}/sets/{test_code}", params={"page": 1, "limit": 1}, headers=pw_headers, timeout=15)
                            d2.append(f"/sets/{test_code} → {resp.status_code}")
                            if resp.status_code == 200:
                                data = resp.json()
                                d2.append(f"  Top keys: {list(data.keys())}")
                                cards = data.get("cards", data.get("data", []))
                                if isinstance(data, list):
                                    cards = data
                                if cards:
                                    c = cards[0]
                                    d2.append(f"  Card keys: {list(c.keys())}")
                                    cm = c.get("cardmarket", c.get("cm", {}))
                                    d2.append(f"  CM data: {json.dumps(cm, default=str)[:400]}")
                                    if not cm:
                                        d2.append(f"  Full card: {json.dumps(c, default=str)[:600]}")
                        except Exception as e:
                            d2.append(f"/sets/{test_code} → ERR {e}")
                    await engine.telegram.send_text("🔧 3/4 Kart Yapısı\n\n" + "\n".join(d2), chat_id)

                    # eBay Browse API
                    d3 = []
                    if EBAY_CLIENT_ID and EBAY_CLIENT_SECRET:
                        try:
                            token = await engine.ebay._get_token()
                            if token:
                                d3.append(f"✅ eBay OAuth → Token alındı")
                                items = await engine.ebay.search_items("Pokemon Charizard", max_results=2)
                                d3.append(f"✅ eBay Search → {len(items)} sonuç")
                                if items:
                                    d3.append(f"  İlk: {items[0].get('title', '?')[:60]} £{items[0].get('total_gbp', 0):.2f}")
                            else:
                                d3.append(f"❌ eBay OAuth → Token alınamadı")
                        except Exception as e:
                            d3.append(f"❌ eBay → {e}")
                    else:
                        d3.append(f"⚠️ eBay API anahtarları ayarlanmamış")
                    await engine.telegram.send_text("🔧 4/4 eBay API\n\n" + "\n".join(d3), chat_id)

                elif text.startswith("/settings"):
                    parts = text.split(maxsplit=2)
                    if len(parts) < 2:
                        series_list = ", ".join(BOT_SETTINGS['series']) if BOT_SETTINGS['series'] else "Tum seriler"
                        await engine.telegram.send_text(
                            f"⚙️ Ayarlar\n\n"
                            f"Otomatik tarama: Her {BOT_SETTINGS['interval_hours']} saat\n"
                            f"Taranacak seriler: {series_list}\n"
                            f"Her seride max set: {BOT_SETTINGS['max_sets']}\n\n"
                            f"Degistirmek icin:\n"
                            f"/settings interval <saat>\n"
                            f"/settings maxsets <sayi>\n"
                            f"/settings series ekle <seri>\n"
                            f"/settings series sil <seri>\n"
                            f"/settings series liste",
                            chat_id,
                        )
                    elif parts[1] == "interval" and len(parts) > 2:
                        try:
                            val = int(parts[2])
                            if 1 <= val <= 48:
                                BOT_SETTINGS['interval_hours'] = val
                                await engine.telegram.send_text(f"✅ Tarama araligi: {val} saat", chat_id)
                            else:
                                await engine.telegram.send_text("❌ 1-48 arasi olmal.", chat_id)
                        except ValueError:
                            await engine.telegram.send_text("❌ Sayi gir: /settings interval 6", chat_id)
                    elif parts[1] == "maxsets" and len(parts) > 2:
                        try:
                            val = int(parts[2])
                            if 1 <= val <= 20:
                                BOT_SETTINGS['max_sets'] = val
                                await engine.telegram.send_text(f"✅ Her seride max {val} set taranacak", chat_id)
                            else:
                                await engine.telegram.send_text("❌ 1-20 arasi olmali.", chat_id)
                        except ValueError:
                            await engine.telegram.send_text("❌ Sayi gir: /settings maxsets 5", chat_id)
                    elif parts[1] == "series" and len(parts) > 2:
                        sub = parts[2].strip()
                        if sub == "liste":
                            if BOT_SETTINGS['series']:
                                lines = [f"{i+1}. {s}" for i, s in enumerate(BOT_SETTINGS['series'])]
                                await engine.telegram.send_text("📋 Taranacak seriler:\n" + "\n".join(lines), chat_id)
                            else:
                                await engine.telegram.send_text("📋 Seri filtresi yok, tum setler taranir.", chat_id)
                        elif sub.startswith("ekle "):
                            name = sub[5:].strip()
                            if name and name not in BOT_SETTINGS['series']:
                                BOT_SETTINGS['series'].append(name)
                                await engine.telegram.send_text(f"✅ '{name}' eklendi. Seriler: {', '.join(BOT_SETTINGS['series'])}", chat_id)
                            else:
                                await engine.telegram.send_text(f"⚠️ '{name}' zaten listede.", chat_id)
                        elif sub.startswith("sil "):
                            name = sub[4:].strip()
                            if name in BOT_SETTINGS['series']:
                                BOT_SETTINGS['series'].remove(name)
                                await engine.telegram.send_text(f"✅ '{name}' silindi. Seriler: {', '.join(BOT_SETTINGS['series']) or 'Bos'}", chat_id)
                            else:
                                await engine.telegram.send_text(f"⚠️ '{name}' listede yok.", chat_id)
                        else:
                            await engine.telegram.send_text("Kullanim: /settings series liste|ekle <ad>|sil <ad>", chat_id)
                    else:
                        await engine.telegram.send_text("⚙️ /settings yazarak ayarlari gor.", chat_id)

                elif text.startswith("/help"):
                    await engine.telegram.send_text(
                        "🃏 Pokemon Arbitrage Bot\n\n"
                        "/series - Serileri butonlarla listele\n"
                        "/sets - Tum setleri listele\n"
                        "/sets <seri adi> - Serinin setlerini goster\n"
                        "/analyze <set_code> - Set analiz et\n"
                        "/check <kart adi> - Tek kart kontrol\n"
                        "/results - En karli kartlar\n"
                        "/stop - Calisan taramayi durdur\n"
                        "/settings - Ayarlar (interval, seri)\n"
                        "/status - Bot durumu\n"
                        "/diag - Tanilama testi\n"
                        "/help - Bu mesaj\n\n"
                        "Kullanim: /series tikla > seri sec > set sec > otomatik analiz",
                        chat_id,
                    )

                elif text.startswith("/status"):
                    last_run = engine.state.state.get("last_run", "Hic")
                    sets_done = len(engine.state.state.get("analyzed_sets", {}))
                    total_profitable = len(engine.state.results.get("profitable_cards", []))
                    ebay_status = "✅" if (EBAY_CLIENT_ID and EBAY_CLIENT_SECRET) else "❌"
                    pw_status = "✅" if POKEWALLET_API_KEY else "⚠️"
                    series_str = ", ".join(BOT_SETTINGS['series']) if BOT_SETTINGS['series'] else "Tum seriler"
                    task_str = f"⏳ {engine._task_label}" if engine._running_task and not engine._running_task.done() else "Bos"
                    await engine.telegram.send_text(
                        f"📊 Bot Durumu\n\n"
                        f"Son tarama: {last_run}\n"
                        f"Analiz edilen set: {sets_done}\n"
                        f"Karli kart: {total_profitable}\n"
                        f"Calisan gorev: {task_str}\n\n"
                        f"Otomatik tarama: Her {BOT_SETTINGS['interval_hours']} saat\n"
                        f"Seriler: {series_str}\n"
                        f"Her seride max: {BOT_SETTINGS['max_sets']} set\n\n"
                        f"PokéWallet: {pw_status} | eBay: {ebay_status}",
                        chat_id,
                    )

        except Exception as e:
            print(f"[Bot] Error: {e}")

        await asyncio.sleep(3)


async def auto_scan_loop(engine: ArbitrageEngine):
    print(f"[AutoScan] Her {BOT_SETTINGS['interval_hours']} saatte otomatik tarama")
    print(f"[AutoScan] Seriler: {BOT_SETTINGS['series']}")
    await asyncio.sleep(30)

    while True:
        try:
            sets_to_scan = WATCHLIST_SETS.copy()

            if not sets_to_scan:
                all_sets = await engine.pokewallet.get_sets()
                if BOT_SETTINGS['series']:
                    filtered = []
                    for s in all_sets:
                        series = _get_series_from_set(s)
                        if any(target.lower() in series.lower() for target in BOT_SETTINGS['series']):
                            filtered.append(s)
                    sorted_sets = sorted(filtered, key=lambda s: _parse_date(s), reverse=True)
                else:
                    sorted_sets = sorted(all_sets, key=lambda s: _parse_date(s), reverse=True)
                sets_to_scan = [
                    s.get("set_code", s.get("id", ""))
                    for s in sorted_sets[:BOT_SETTINGS['max_sets']]
                ]

            print(f"[AutoScan] Taranacak setler: {sets_to_scan}")
            for set_code in sets_to_scan:
                if set_code:
                    await engine.analyze_set(set_code)
                    await asyncio.sleep(5)

        except Exception as e:
            print(f"[AutoScan] Error: {e}")

        await asyncio.sleep(BOT_SETTINGS['interval_hours'] * 3600)


async def run_service():
    engine = ArbitrageEngine()
    await engine.start()

    print("🃏 Pokemon Card Arbitrage Bot başlatıldı!")
    print(f"   PokéWallet API: {'✅' if POKEWALLET_API_KEY else '⚠️ API key yok'}")
    print(f"   eBay Browse API: {'✅' if (EBAY_CLIENT_ID and EBAY_CLIENT_SECRET) else '❌ Ayarlanmamış'}")
    print(f"   Otomatik tarama: Her {BOT_SETTINGS['interval_hours']} saat")
    print(f"   Watchlist: {WATCHLIST_SETS or 'En son setler'}")
    print(f"   Min kâr: €{MIN_PROFIT_EUR} / %{MIN_PROFIT_PERCENT}")
    print()

    if TELEGRAM_TOKEN:
        ebay_status = "✅" if (EBAY_CLIENT_ID and EBAY_CLIENT_SECRET) else "❌ Ayarlanmamış"
        await engine.telegram.send_text(
            "🚀 Pokemon Arbitrage Bot başladı!\n\n"
            f"PokéWallet: ✅\n"
            f"eBay API: {ebay_status}\n"
            f"Otomatik tarama: Her {BOT_SETTINGS['interval_hours']} saat\n"
            "Komutlar için /help yazın."
        )

    await asyncio.gather(
        health_server(),
        telegram_command_loop(engine),
        auto_scan_loop(engine),
        keep_alive_loop(),
    )


# --- CLI Mode ---

async def run_cli():
    engine = ArbitrageEngine()
    await engine.start()

    try:
        command = sys.argv[1].lower()

        if command == "sets":
            print(await engine.list_sets())

        elif command == "analyze":
            if len(sys.argv) < 3:
                print("Kullanım: python arbitrage.py analyze <set_code> [--force]")
                return
            set_code = sys.argv[2]
            force = "--force" in sys.argv
            result = await engine.analyze_set(set_code, force=force)
            if not result:
                print(f"❌ '{set_code}': Kârlı kart bulunamadı (veya yakın zamanda taranmış).")

        elif command == "check":
            if len(sys.argv) < 3:
                print("Kullanım: python arbitrage.py check <kart_adı>")
                return
            card_name = " ".join(sys.argv[2:])
            print(await engine.quick_check(card_name))

        elif command == "scan":
            limit = int(sys.argv[2]) if len(sys.argv) > 2 else 3
            sets = await engine.pokewallet.get_sets()
            sorted_sets = sorted(sets, key=lambda s: _parse_date(s), reverse=True)
            for s in sorted_sets[:limit]:
                set_code = s.get("set_code", s.get("id", ""))
                if set_code:
                    await engine.analyze_set(set_code, force=True)

        elif command == "results":
            top = engine.state.get_top_results(20)
            if not top:
                print("Henüz kârlı kart bulunamadı.")
            else:
                print(f"\n💰 EN KÂRLI KARTLAR ({len(top)} adet):\n")
                for i, c in enumerate(top, 1):
                    print(f"  {i:2d}. {c['name']} #{c.get('card_number', '?')} ({c['set_name']})")
                    print(f"      eBay: £{c['ebay_price_gbp']:.2f} → CM: €{c['cardmarket_price_eur']:.2f}")
                    print(f"      Kâr: €{c['profit_eur']:.2f} ({c['profit_percent']:.1f}%)")
                    print()
        else:
            print_help()
    finally:
        await engine.stop()


def print_help():
    print("""
🃏 Pokemon Card Arbitrage System
   eBay UK → Cardmarket Kâr Sistemi

MODLAR:
  python arbitrage.py serve          Arka plan servisi başlat (Render)
  python arbitrage.py sets           Setleri listele
  python arbitrage.py analyze <code> Set analiz et
  python arbitrage.py check <ad>     Tek kart kontrol
  python arbitrage.py scan [N]       Son N seti tara
  python arbitrage.py results        Kârlı kartları göster

ÖRNEKLER:
  python arbitrage.py serve
  python arbitrage.py analyze SV6 --force
  python arbitrage.py check "Charizard ex"
  python arbitrage.py scan 3
""")


if __name__ == "__main__":
    if len(sys.argv) < 2 or sys.argv[1].lower() == "serve":
        asyncio.run(run_service())
    else:
        asyncio.run(run_cli())
