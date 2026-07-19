#!/usr/bin/env python3 -u
"""
Pokemon Card Arbitrage System
Buy from eBay UK → Sell on Cardmarket for profit.

Uses PokéWallet API for Cardmarket prices and eBay Browse API for UK prices.
Runs as a background service on Render (free tier).
"""

import asyncio
import base64
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

AUTO_SCAN_INTERVAL_HOURS = int(os.environ.get("AUTO_SCAN_INTERVAL_HOURS", "6"))
AUTO_SCAN_SET_COUNT = int(os.environ.get("AUTO_SCAN_SET_COUNT", "3"))

WATCHLIST_SETS = [s.strip() for s in os.environ.get("WATCHLIST_SETS", "").split(",") if s.strip()]

STATE_FILE = "arbitrage_state.json"
RESULTS_FILE = "arbitrage_results.json"

REQUEST_DELAY = 2

PORT = int(os.environ.get("PORT", "10000"))


# --- Series grouping by set_code prefix ---

# High-value rarities to show in table view (skip Double Rare and below)
HIGH_RARITIES = {
    "illustration rare", "ir",
    "special illustration rare", "sir",
    "ultra rare", "ur",
    "hyper ultra rare", "hur",
    "hyper rare", "hr",
    "special art rare", "sar",
    "art rare", "ar",
    "secret rare", "sr",
    "full art", "fa",
    "alt art", "aa",
    "gold", "gold rare",
    "trainer gallery", "tg",
    "immersive rare",
    "crown rare",
}

SERIES_PREFIX_MAP = {
    "SV": "Scarlet & Violet",
    "SWSH": "Sword & Shield",
    "SM": "Sun & Moon",
    "XY": "XY",
    "BW": "Black & White",
    "HGSS": "HeartGold & SoulSilver",
    "PL": "Platinum",
    "DP": "Diamond & Pearl",
    "EX": "EX",
    "ME": "Mega Evolution",
    "CL": "Call of Legends",
    "NXD": "Next Destinies",
    "LTR": "Legendary Treasures",
    "GEN": "Generations",
    "DET": "Detective Pikachu",
    "CEL": "Celebrations",
    "PGO": "Pokemon GO",
    "TG": "Trainer Gallery",
}

def _is_high_rarity(rarity: str) -> bool:
    if not rarity:
        return False
    r = rarity.lower().strip()
    if r in HIGH_RARITIES:
        return True
    for hr in HIGH_RARITIES:
        if hr in r or r in hr:
            return True
    return False


def _get_series_from_code(set_code: str) -> str:
    if not set_code:
        return "Diğer"
    code_upper = set_code.upper()
    for prefix, series in sorted(SERIES_PREFIX_MAP.items(), key=lambda x: -len(x[0])):
        if code_upper.startswith(prefix):
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
        """
        cm = card_data.get("cardmarket", {})
        if not cm:
            return 0.0, 0.0, ""

        product_url = cm.get("product_url", "")
        prices_list = cm.get("prices", [])

        if isinstance(prices_list, list):
            for p in prices_list:
                vtype = p.get("variant_type", "")
                if vtype in ("normal", "holo", ""):
                    avg = float(p.get("avg", 0) or 0)
                    trend = float(p.get("trend", 0) or 0)
                    low = float(p.get("low", 0) or 0)
                    price = trend or avg or low
                    return price, trend, product_url
            if prices_list:
                p = prices_list[0]
                avg = float(p.get("avg", 0) or 0)
                trend = float(p.get("trend", 0) or 0)
                low = float(p.get("low", 0) or 0)
                price = trend or avg or low
                return price, trend, product_url
        elif isinstance(prices_list, dict):
            avg = float(prices_list.get("avg", 0) or prices_list.get("trendPrice", 0) or prices_list.get("averageSellPrice", 0) or 0)
            trend = float(prices_list.get("trend", 0) or prices_list.get("trendPrice", 0) or 0)
            return avg or trend, trend, product_url

        return 0.0, 0.0, product_url

    @staticmethod
    def extract_card_info(card_data: dict) -> dict:
        """Extract normalized card info from PokéWallet card data."""
        ci = card_data.get("card_info", {})
        card_id = card_data.get("id", "")
        return {
            "id": card_id,
            "name": ci.get("name", card_data.get("name", "Unknown")),
            "set_name": ci.get("set_name", card_data.get("set_name", "")),
            "set_code": ci.get("set_code", card_data.get("set_code", "")),
            "card_number": ci.get("card_number", card_data.get("number", card_data.get("card_number", ""))),
            "rarity": ci.get("rarity", card_data.get("rarity", "")),
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
                "filter": "buyingOptions:{FIXED_PRICE},itemLocationCountry:GB",
                "sort": "newlyListed",
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
                return self._normalize_items(items)
            elif resp.status_code == 429:
                print("[eBay] Rate limited - waiting")
                await asyncio.sleep(5)
                return []
            else:
                print(f"[eBay] Search error: {resp.text[:300]}")
                return []
        except Exception as e:
            print(f"[eBay] Search exception: {e}")
            return []

    def _normalize_items(self, items: list[dict]) -> list[dict]:
        results = []
        for item in items:
            try:
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
                        "title": item.get("title", ""),
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
            set_code = s.get("set_code", s.get("id", ""))
            series = _get_series_from_code(set_code)
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
            if filter_lower in _get_series_from_code(s.get("set_code", s.get("id", ""))).lower()
        ]
        if not filtered:
            filtered = [s for s in sets if filter_lower in (s.get("name") or "").lower()]
        if not filtered:
            return f"❌ '{series_filter}' serisi bulunamadı.", []

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
                if filter_lower in _get_series_from_code(s.get("set_code", s.get("id", ""))).lower()
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

    async def analyze_set_table(self, set_code: str) -> str:
        """Fetch high-rarity cards from a set and return a price comparison table."""
        cards = await self.pokewallet.get_set_cards(set_code)
        if not cards:
            return f"❌ {set_code}: Kart bulunamadı."

        first_info = PokeWalletClient.extract_card_info(cards[0]) if cards else {}
        set_name = first_info.get("set_name", "") or set_code

        ebay_available = bool(EBAY_CLIENT_ID and EBAY_CLIENT_SECRET)

        high_rarity_cards = []
        for card_data in cards:
            info = PokeWalletClient.extract_card_info(card_data)
            rarity = info["rarity"]
            if not _is_high_rarity(rarity):
                continue

            cm_price, cm_trend, cm_url = PokeWalletClient.extract_cardmarket_price(card_data)
            if cm_price < 0.5:
                continue

            high_rarity_cards.append({
                "info": info,
                "cm_price": cm_price,
                "cm_trend": cm_trend,
                "cm_url": cm_url,
                "card_data": card_data,
            })

        if not high_rarity_cards:
            return f"❌ {set_name}: Yüksek rarity kart bulunamadı (IR/SIR/UR/HUR)."

        high_rarity_cards.sort(key=lambda x: x["cm_price"], reverse=True)

        lines = [f"📊 {set_name} — Değerli Kartlar\n"]
        lines.append(f"{'Kart':<20} {'Rarity':<5} {'CM':>7} {'eBay':>7} {'Fark':>8}")
        lines.append("─" * 52)

        for entry in high_rarity_cards[:25]:
            info = entry["info"]
            card_name = info["name"]
            card_number = info["card_number"]
            rarity = info["rarity"]
            cm_price = entry["cm_price"]

            short_rarity = self._short_rarity(rarity)
            display_name = f"{card_name[:17]}" if len(card_name) > 17 else card_name

            ebay_price_eur = 0.0
            ebay_price_gbp = 0.0
            if ebay_available:
                search_query = f"Pokemon {card_name} {card_number} {set_name}"
                ebay_listings = await self.ebay.search_items(search_query, max_results=3)
                await asyncio.sleep(REQUEST_DELAY)

                if ebay_listings:
                    sorted_l = sorted(ebay_listings, key=lambda x: x["total_gbp"])
                    ebay_price_gbp = sorted_l[0]["total_gbp"]
                    ebay_price_eur = ebay_price_gbp * GBP_TO_EUR

            if ebay_price_eur > 0:
                diff = cm_price - ebay_price_eur
                calc = self.calculator.calculate(ebay_price_gbp, cm_price)
                profit = calc["profit_eur"]
                if profit >= MIN_PROFIT_EUR and calc["profit_percent"] >= MIN_PROFIT_PERCENT:
                    icon = "🟢"
                elif profit > 0:
                    icon = "🟡"
                else:
                    icon = "🔴"
                lines.append(
                    f"{icon} {display_name:<18} {short_rarity:<5} "
                    f"€{cm_price:>5.1f}  £{ebay_price_gbp:>5.1f}  "
                    f"€{profit:>+5.1f}"
                )
            else:
                lines.append(
                    f"⚪ {display_name:<18} {short_rarity:<5} "
                    f"€{cm_price:>5.1f}  {'—':>6}  {'—':>6}"
                )

        total_shown = min(len(high_rarity_cards), 25)
        lines.append(f"\n📈 {total_shown} kart gösteriliyor")
        lines.append("🟢 Kârlı | 🟡 Az kârlı | 🔴 Zarar | ⚪ eBay yok")

        if not ebay_available:
            lines.append("\n⚠️ eBay API ayarlanmamış — sadece CM fiyatları gösteriliyor")

        return "\n".join(lines)

    @staticmethod
    def _short_rarity(rarity: str) -> str:
        r = rarity.lower()
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
            return "GOLD"
        if "immersive" in r:
            return "IMR"
        return rarity[:4].upper()

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
        response = (
            "HTTP/1.1 200 OK\r\n"
            "Content-Type: text/plain\r\n\r\n"
            "Pokemon Card Arbitrage Bot - Running\n"
        )
        writer.write(response.encode())
        await writer.drain()
        writer.close()

    server = await asyncio.start_server(handle, "0.0.0.0", PORT)
    print(f"[Health] Listening on port {PORT}")
    await server.serve_forever()


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
                            await engine.telegram.send_text(f"🔍 {set_code} — değerli kartlar taranıyor...", cb_chat_id)
                            table = await engine.analyze_set_table(set_code)
                            await engine.telegram.send_text(table, cb_chat_id)
                    except Exception as e:
                        print(f"[Bot] Callback error: {e}")
                        await engine.telegram.send_text(f"❌ Hata: {e}", cb_chat_id)

                    continue

                msg = update.get("message", {})
                text = msg.get("text", "").strip()
                chat_id = str(msg.get("chat", {}).get("id", ""))

                if chat_id != TELEGRAM_CHAT_ID:
                    continue

                if text.startswith("/series"):
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

                    # PokéWallet /sets
                    try:
                        resp = await cl.get(f"{pw_base}/sets", headers=pw_headers, timeout=15)
                        d.append(f"✅ PW /sets → {resp.status_code}")
                        if resp.status_code == 200:
                            data = resp.json()
                            items = data if isinstance(data, list) else data.get("data", [])
                            d.append(f"  {len(items)} set bulundu")
                            if items:
                                s = items[0]
                                d.append(f"  Keys: {list(s.keys())}")
                    except Exception as e:
                        d.append(f"❌ PW /sets → {e}")

                    # PokéWallet /sets/SV6 (cards with prices)
                    try:
                        resp = await cl.get(f"{pw_base}/sets/SV6", params={"page": 1, "limit": 2}, headers=pw_headers, timeout=15)
                        d.append(f"\n✅ PW /sets/SV6 → {resp.status_code}")
                        if resp.status_code == 200:
                            data = resp.json()
                            d.append(f"  Keys: {list(data.keys())}")
                            cards = data.get("cards", data.get("data", []))
                            if cards:
                                c = cards[0]
                                d.append(f"  Card keys: {list(c.keys())}")
                                cm = c.get("cardmarket", {})
                                d.append(f"  CM: {json.dumps(cm, default=str)[:300]}")
                    except Exception as e:
                        d.append(f"❌ PW /sets/SV6 → {e}")

                    # PokéWallet /search
                    try:
                        resp = await cl.get(f"{pw_base}/search", params={"q": "Charizard"}, headers=pw_headers, timeout=15)
                        d.append(f"\n✅ PW /search?q=Charizard → {resp.status_code}")
                        if resp.status_code == 200:
                            data = resp.json()
                            results = data.get("results", data.get("data", []))
                            d.append(f"  {len(results)} sonuç")
                    except Exception as e:
                        d.append(f"❌ PW /search → {e}")

                    # eBay Browse API
                    if EBAY_CLIENT_ID and EBAY_CLIENT_SECRET:
                        try:
                            token = await engine.ebay._get_token()
                            if token:
                                d.append(f"\n✅ eBay OAuth → Token alındı")
                                items = await engine.ebay.search_items("Pokemon Charizard", max_results=2)
                                d.append(f"✅ eBay Search → {len(items)} sonuç")
                                if items:
                                    d.append(f"  İlk: {items[0].get('title', '?')[:60]} £{items[0].get('total_gbp', 0):.2f}")
                            else:
                                d.append(f"\n❌ eBay OAuth → Token alınamadı")
                        except Exception as e:
                            d.append(f"\n❌ eBay → {e}")
                    else:
                        d.append(f"\n⚠️ eBay API anahtarları ayarlanmamış")

                    await engine.telegram.send_text("🔧 Tanılama Sonuçları:\n\n" + "\n".join(d), chat_id)

                elif text.startswith("/help"):
                    await engine.telegram.send_text(
                        "🃏 Pokemon Arbitrage Bot\n\n"
                        "/series - Serileri butonlarla listele\n"
                        "/sets - Tüm setleri listele\n"
                        "/sets <seri adı> - Serinin setlerini butonlarla göster\n"
                        "/analyze <set_code> - Set analiz et\n"
                        "/check <kart adı> - Tek kart kontrol\n"
                        "/results - En kârlı kartlar\n"
                        "/status - Bot durumu\n"
                        "/diag - Tanılama testi\n"
                        "/help - Bu mesaj\n\n"
                        "Kullanım: /series tıkla → seri seç → set seç → otomatik analiz",
                        chat_id,
                    )

                elif text.startswith("/status"):
                    last_run = engine.state.state.get("last_run", "Hiç")
                    sets_done = len(engine.state.state.get("analyzed_sets", {}))
                    total_profitable = len(engine.state.results.get("profitable_cards", []))
                    ebay_status = "✅ Ayarlandı" if (EBAY_CLIENT_ID and EBAY_CLIENT_SECRET) else "❌ Ayarlanmamış"
                    pw_status = "✅ Ayarlandı" if POKEWALLET_API_KEY else "⚠️ API key yok"
                    await engine.telegram.send_text(
                        f"📊 Bot Durumu\n\n"
                        f"Son tarama: {last_run}\n"
                        f"Analiz edilen set: {sets_done}\n"
                        f"Bulunan kârlı kart: {total_profitable}\n"
                        f"Otomatik tarama: Her {AUTO_SCAN_INTERVAL_HOURS} saatte\n"
                        f"Watchlist: {', '.join(WATCHLIST_SETS) or 'Yok'}\n\n"
                        f"API Durumu:\n"
                        f"PokéWallet: {pw_status}\n"
                        f"eBay Browse API: {ebay_status}",
                        chat_id,
                    )

        except Exception as e:
            print(f"[Bot] Error: {e}")

        await asyncio.sleep(3)


async def auto_scan_loop(engine: ArbitrageEngine):
    print(f"[AutoScan] Her {AUTO_SCAN_INTERVAL_HOURS} saatte otomatik tarama yapılacak")
    await asyncio.sleep(30)

    while True:
        try:
            sets_to_scan = WATCHLIST_SETS.copy()

            if not sets_to_scan:
                all_sets = await engine.pokewallet.get_sets()
                sorted_sets = sorted(all_sets, key=lambda s: _parse_date(s), reverse=True)
                sets_to_scan = [s.get("set_code", s.get("id", "")) for s in sorted_sets[:AUTO_SCAN_SET_COUNT]]

            print(f"[AutoScan] Taranacak setler: {sets_to_scan}")
            for set_code in sets_to_scan:
                if set_code:
                    await engine.analyze_set(set_code)
                    await asyncio.sleep(5)

        except Exception as e:
            print(f"[AutoScan] Error: {e}")

        await asyncio.sleep(AUTO_SCAN_INTERVAL_HOURS * 3600)


async def run_service():
    engine = ArbitrageEngine()
    await engine.start()

    print("🃏 Pokemon Card Arbitrage Bot başlatıldı!")
    print(f"   PokéWallet API: {'✅' if POKEWALLET_API_KEY else '⚠️ API key yok'}")
    print(f"   eBay Browse API: {'✅' if (EBAY_CLIENT_ID and EBAY_CLIENT_SECRET) else '❌ Ayarlanmamış'}")
    print(f"   Otomatik tarama: Her {AUTO_SCAN_INTERVAL_HOURS} saat")
    print(f"   Watchlist: {WATCHLIST_SETS or 'En son setler'}")
    print(f"   Min kâr: €{MIN_PROFIT_EUR} / %{MIN_PROFIT_PERCENT}")
    print()

    if TELEGRAM_TOKEN:
        ebay_status = "✅" if (EBAY_CLIENT_ID and EBAY_CLIENT_SECRET) else "❌ Ayarlanmamış"
        await engine.telegram.send_text(
            "🚀 Pokemon Arbitrage Bot başladı!\n\n"
            f"PokéWallet: ✅\n"
            f"eBay API: {ebay_status}\n"
            f"Otomatik tarama: Her {AUTO_SCAN_INTERVAL_HOURS} saat\n"
            "Komutlar için /help yazın."
        )

    await asyncio.gather(
        health_server(),
        telegram_command_loop(engine),
        auto_scan_loop(engine),
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
