#!/usr/bin/env python3 -u
"""
Pokemon Card Arbitrage System
Buy from eBay UK → Sell on Cardmarket for profit.

Runs as a background service on Render (free tier):
- Automatically scans sets every few hours
- Responds to Telegram bot commands (/sets, /analyze, /check, /results)
- Sends photo notifications for profitable cards
"""

import asyncio
import json
import os
import re
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

# Force unbuffered output for Render logs
os.environ["PYTHONUNBUFFERED"] = "1"
sys.stdout.reconfigure(line_buffering=True)
sys.stderr.reconfigure(line_buffering=True)

import httpx
from bs4 import BeautifulSoup


# --- Load .env file ---

def load_env():
    """Load variables from .env file if it exists."""
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
EBAY_UK_BASE = "https://www.ebay.co.uk"

TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")

# Cardmarket fees: 5% commission + ~1.5€ shipping average
CARDMARKET_COMMISSION_PERCENT = 5.0
CARDMARKET_SHIPPING_COST_EUR = 1.50

# GBP to EUR conversion rate
GBP_TO_EUR = float(os.environ.get("GBP_TO_EUR", "1.17"))

# Minimum profit thresholds
MIN_PROFIT_EUR = float(os.environ.get("MIN_PROFIT_EUR", "2.0"))
MIN_PROFIT_PERCENT = float(os.environ.get("MIN_PROFIT_PERCENT", "15.0"))

# PokéWallet API key (free tier: 100 req/hour, 1000/day)
POKEWALLET_API_KEY = os.environ.get("POKEWALLET_API_KEY", "")

# Auto-scan settings
AUTO_SCAN_INTERVAL_HOURS = int(os.environ.get("AUTO_SCAN_INTERVAL_HOURS", "6"))
AUTO_SCAN_SET_COUNT = int(os.environ.get("AUTO_SCAN_SET_COUNT", "3"))

# Watchlist: comma-separated set IDs to auto-scan
WATCHLIST_SETS = [s.strip() for s in os.environ.get("WATCHLIST_SETS", "").split(",") if s.strip()]

# State files
STATE_FILE = "arbitrage_state.json"
RESULTS_FILE = "arbitrage_results.json"

# Rate limiting
REQUEST_DELAY = 2

# Health check port (Render needs this)
PORT = int(os.environ.get("PORT", "10000"))


@dataclass
class CardPrice:
    name: str
    set_name: str
    set_id: str
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
    id: str
    name: str
    series: str = ""
    total_cards: int = 0


# --- PokéWallet API Client (Cardmarket Prices) ---

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
        # Return cached data if fresh (5 minutes)
        if self._sets_cache and (time.time() - self._sets_cache_time) < 300:
            return self._sets_cache

        # Use pokemontcg.io as primary — it has series, proper IDs, releaseDate
        try:
            page = 1
            all_sets = []
            while True:
                resp = await self.client.get(
                    f"https://api.pokemontcg.io/v2/sets?orderBy=-releaseDate&page={page}&pageSize=250",
                    headers={}, timeout=30,
                )
                print(f"[ptcg] GET /sets page={page} -> {resp.status_code}")
                if resp.status_code != 200:
                    break
                data = resp.json()
                items = data.get("data", [])
                if not items:
                    break
                all_sets.extend(items)
                if len(items) < 250:
                    break
                page += 1
            if all_sets:
                print(f"[ptcg] Total sets: {len(all_sets)}")
                self._sets_cache = all_sets
                self._sets_cache_time = time.time()
                return all_sets
        except Exception as e:
            print(f"[ptcg] Sets error: {e}")

        # Fallback to PokéWallet
        try:
            resp = await self.client.get(
                f"{self.base_url}/sets", headers=self.headers, timeout=30
            )
            print(f"[PokeWallet] GET /sets -> {resp.status_code}")
            resp.raise_for_status()
            data = resp.json()
            items = data if isinstance(data, list) else data.get("data", data.get("sets", []))
            self._sets_cache = items
            self._sets_cache_time = time.time()
            return items
        except Exception as e:
            print(f"[PokeWallet] Error fetching sets: {e}")
            return []

    async def get_set_cards(self, set_id: str, set_name: str = "") -> list[dict]:
        # pokemontcg.io first (since our sets listing uses ptcg IDs)
        all_cards = []
        page = 1
        try:
            while True:
                url = f"https://api.pokemontcg.io/v2/cards?q=set.id:{set_id}&select=name,number,rarity,images,set,cardmarket&pageSize=250&page={page}"
                resp = await self.client.get(url, headers={}, timeout=30)
                print(f"[ptcg] cards set.id:{set_id} page={page} -> {resp.status_code}")
                if resp.status_code == 200:
                    items = resp.json().get("data", [])
                    if items:
                        all_cards.extend(items)
                        if len(items) < 250:
                            break
                        page += 1
                        continue
                break
        except Exception as e:
            print(f"[ptcg] Cards error: {e}")

        if all_cards:
            print(f"[ptcg] Total cards for {set_id}: {len(all_cards)}")
            return all_cards

        # Fallback: search by set name
        if set_name:
            try:
                url = f"https://api.pokemontcg.io/v2/cards?q=set.name:\"{set_name}\"&select=name,number,rarity,images,set,cardmarket&pageSize=250"
                resp = await self.client.get(url, headers={}, timeout=30)
                print(f"[ptcg] cards by name '{set_name}' -> {resp.status_code}")
                if resp.status_code == 200:
                    items = resp.json().get("data", [])
                    if items:
                        print(f"[ptcg] Found {len(items)} cards by name")
                        return items
            except Exception as e:
                print(f"[ptcg] Name search error: {e}")

        # Last fallback: PokéWallet
        for url in [
            f"{self.base_url}/cards?q=set.id:{set_id}",
            f"{self.base_url}/sets/{set_id}/cards",
        ]:
            try:
                resp = await self.client.get(url, headers=self.headers, timeout=30)
                print(f"[PW] {url.replace(self.base_url, '')} -> {resp.status_code}")
                if resp.status_code == 200:
                    data = resp.json()
                    items = data if isinstance(data, list) else data.get("data", data.get("cards", []))
                    if items:
                        return items
            except Exception as e:
                print(f"[PW] Error: {e}")

        print(f"[API] No cards found for set {set_id}")
        return []

    async def search_card(self, name: str, set_name: str = "") -> list[dict]:
        # pokemontcg.io first (better data, free, no key)
        try:
            url = f"https://api.pokemontcg.io/v2/cards?q=name:\"{name}\"&select=name,number,rarity,images,set,cardmarket&pageSize=10"
            resp = await self.client.get(url, headers={}, timeout=30)
            print(f"[ptcg] Search '{name}' -> {resp.status_code}")
            if resp.status_code == 200:
                items = resp.json().get("data", [])
                if items:
                    print(f"[ptcg] Found {len(items)} results")
                    return items
        except Exception as e:
            print(f"[ptcg] Search error: {e}")

        # Fallback: PokéWallet
        for url in [
            f"{self.base_url}/cards?q=name:{name}",
            f"{self.base_url}/cards?q={name}",
        ]:
            try:
                resp = await self.client.get(url, headers=self.headers, timeout=30)
                print(f"[PW] Search '{name}' -> {resp.status_code}")
                if resp.status_code == 200:
                    data = resp.json()
                    items = data if isinstance(data, list) else data.get("data", data.get("cards", []))
                    if items:
                        return items
            except Exception as e:
                print(f"[PW] Search error: {e}")
        return []


# --- eBay UK Scraper ---

class EbayUKScraper:
    def __init__(self, client: httpx.AsyncClient):
        self.client = client
        self.headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            "Accept-Language": "en-GB,en;q=0.9",
        }

    async def search_sold_listings(self, query: str, max_results: int = 10) -> list[dict]:
        params = {
            "_nkw": query,
            "_sacat": "183454",
            "LH_Sold": "1",
            "LH_Complete": "1",
            "_sop": "15",
            "LH_PrefLoc": "1",
        }
        return await self._search(params, max_results)

    async def search_buy_it_now(self, query: str, max_results: int = 10) -> list[dict]:
        params = {
            "_nkw": query,
            "_sacat": "183454",
            "LH_BIN": "1",
            "_sop": "15",
            "LH_PrefLoc": "1",
        }
        return await self._search(params, max_results)

    async def _search(self, params: dict, max_results: int) -> list[dict]:
        try:
            resp = await self.client.get(
                f"{EBAY_UK_BASE}/sch/i.html",
                params=params,
                headers=self.headers,
                timeout=30,
            )
            resp.raise_for_status()
            return self._parse_listings(resp.text, max_results)
        except Exception as e:
            print(f"[eBay UK] Error searching: {e}")
            return []

    def _parse_listings(self, html: str, max_results: int) -> list[dict]:
        soup = BeautifulSoup(html, "lxml")
        listings = []

        for item in soup.select(".s-item")[:max_results + 1]:
            try:
                title_el = item.select_one(".s-item__title")
                if not title_el:
                    continue
                title = title_el.get_text(strip=True)
                if title.lower() == "shop on ebay":
                    continue

                price_el = item.select_one(".s-item__price")
                price_text = price_el.get_text(strip=True) if price_el else "0"
                price = self._parse_price(price_text)

                shipping_el = item.select_one(".s-item__shipping, .s-item__freeXDays")
                shipping_text = shipping_el.get_text(strip=True) if shipping_el else "0"
                shipping = self._parse_price(shipping_text) if "free" not in shipping_text.lower() else 0.0

                link_el = item.select_one(".s-item__link")
                url = link_el["href"] if link_el else ""

                img_el = item.select_one(".s-item__image-wrapper img")
                image_url = ""
                if img_el:
                    image_url = img_el.get("src", "") or img_el.get("data-src", "")

                condition_el = item.select_one(".SECONDARY_INFO")
                condition = condition_el.get_text(strip=True) if condition_el else ""

                if price > 0:
                    listings.append({
                        "title": title,
                        "price_gbp": price,
                        "shipping_gbp": shipping,
                        "total_gbp": price + shipping,
                        "url": url,
                        "image_url": image_url,
                        "condition": condition,
                    })
            except Exception:
                continue

        return listings[:max_results]

    def _parse_price(self, text: str) -> float:
        match = re.search(r"£([\d,]+\.?\d*)", text)
        if match:
            return float(match.group(1).replace(",", ""))
        return 0.0


# --- Date Parser ---

def _parse_date(s: dict) -> str:
    """Parse various date formats into sortable 'YYYY-MM-DD'."""
    from datetime import datetime
    raw = s.get("releaseDate") or s.get("release_date") or ""
    if not raw:
        return "0000-00-00"
    try:
        if re.match(r"\d{4}[-/]\d{2}[-/]\d{2}", raw):
            return raw[:10].replace("/", "-")
        cleaned = re.sub(r"(\d+)(st|nd|rd|th)", r"\1", raw)
        dt = datetime.strptime(cleaned.strip(), "%d %B, %Y")
        return dt.strftime("%Y-%m-%d")
    except Exception:
        return "0000-00-00"


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


# --- Telegram Bot (send + receive commands) ---

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
        """Poll for new Telegram messages and callback queries."""
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

    def mark_set_analyzed(self, set_id: str, set_name: str, profitable_count: int):
        self.state["analyzed_sets"][set_id] = {
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

    def is_set_recent(self, set_id: str, hours: int = 24) -> bool:
        info = self.state.get("analyzed_sets", {}).get(set_id)
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
        self.ebay: Optional[EbayUKScraper] = None
        self.telegram: Optional[TelegramBot] = None
        self.state: Optional[StateManager] = None
        self.calculator = ArbitrageCalculator()

    async def start(self):
        self.client = httpx.AsyncClient(follow_redirects=True, timeout=30)
        self.pokewallet = PokeWalletClient(self.client)
        self.ebay = EbayUKScraper(self.client)
        self.telegram = TelegramBot(self.client)
        self.state = StateManager()

    async def stop(self):
        if self.client:
            await self.client.aclose()

    def _group_series(self, sets: list[dict]) -> list[tuple[str, dict]]:
        """Group sets by series, sorted newest first."""
        series_map = {}
        for s in sets:
            series = s.get("series") or s.get("set_series") or "Diğer"
            if series not in series_map:
                series_map[series] = {"sets": [], "latest_date": "0000-00-00"}
            series_map[series]["sets"].append(s)
            d = _parse_date(s)
            if d > series_map[series]["latest_date"]:
                series_map[series]["latest_date"] = d
        return sorted(series_map.items(), key=lambda x: x[1]["latest_date"], reverse=True)

    async def list_series(self) -> str:
        """List all series/eras (newest first)."""
        sets = await self.pokewallet.get_sets()
        if not sets:
            return "❌ Seri listesi alınamadı."

        sorted_series = self._group_series(sets)

        lines = ["📚 SERİLER (en yeniden eskiye):\n"]
        for i, (name, info) in enumerate(sorted_series, 1):
            count = len(info["sets"])
            lines.append(f"{i}. {name} ({count} set)")
        lines.append(f"\nBir serinin setlerini görmek için:\n/sets <seri adı>\nÖrn: /sets Scarlet & Violet")
        return "\n".join(lines)

    async def list_series_buttons(self) -> tuple[str, list[list[dict]]]:
        """Return series as inline keyboard buttons."""
        sets = await self.pokewallet.get_sets()
        if not sets:
            return "❌ Seri listesi alınamadı.", []

        sorted_series = self._group_series(sets)

        buttons = []
        for name, info in sorted_series[:20]:
            count = len(info["sets"])
            btn_text = f"{name} ({count})"
            # Truncate callback data to 64 bytes (Telegram limit)
            cb_data = f"series:{name[:50]}"
            buttons.append([{"text": btn_text, "callback_data": cb_data}])

        return "📚 Bir seri seçin:", buttons

    async def list_sets_buttons(self, series_filter: str) -> tuple[str, list[list[dict]]]:
        """Return sets in a series as inline keyboard buttons."""
        sets = await self.pokewallet.get_sets()
        if not sets:
            return "❌ Set listesi alınamadı.", []

        filter_lower = series_filter.lower()
        filtered = [s for s in sets if filter_lower in (s.get("series") or s.get("set_series") or "").lower()]
        if not filtered:
            filtered = [s for s in sets if filter_lower in (s.get("name") or "").lower()]
        if not filtered:
            return f"❌ '{series_filter}' serisi bulunamadı.", []

        sets_sorted = sorted(filtered, key=lambda s: _parse_date(s), reverse=True)

        buttons = []
        for s in sets_sorted[:30]:
            name = s.get("name", "?")
            sid = s.get("id", s.get("set_id", ""))
            total = s.get("total", s.get("totalCards", "?"))
            btn_text = f"{name} ({total} kart)"
            cb_data = f"analyze:{sid}"
            buttons.append([{"text": btn_text, "callback_data": cb_data}])

        return f"📦 {series_filter} setleri ({len(sets_sorted)} set):", buttons

    async def list_sets(self, series_filter: str = "") -> str:
        """List sets, optionally filtered by series."""
        sets = await self.pokewallet.get_sets()
        if not sets:
            return "❌ Set listesi alınamadı."

        # Filter by series if provided
        if series_filter:
            filter_lower = series_filter.lower()
            filtered = [s for s in sets if filter_lower in (s.get("series") or s.get("set_series") or "").lower()]
            if not filtered:
                # Try matching set name too
                filtered = [s for s in sets if filter_lower in (s.get("name") or "").lower()]
            if not filtered:
                return f"❌ '{series_filter}' serisi bulunamadı. /series ile serileri listele."
            sets = filtered

        # En yeni setler önce
        sets_sorted = sorted(sets, key=lambda s: _parse_date(s), reverse=True)
        title = f"📦 {series_filter}" if series_filter else "📦 Tüm setler"
        lines = [f"{title} ({len(sets)} set, en yeniden eskiye):\n"]
        for i, s in enumerate(sets_sorted[:25], 1):
            name = s.get("name", "?")
            sid = s.get("id", s.get("set_id", ""))
            total = s.get("total", s.get("totalCards", "?"))
            date = s.get("releaseDate") or s.get("release_date") or ""
            lines.append(f"{i}. [{sid}] {name} ({total} kart) {date}")
        if len(sets) > 25:
            lines.append(f"\n...ve {len(sets) - 25} set daha")
        lines.append(f"\nBir seti analiz etmek için:\n/analyze <set_id>")
        return "\n".join(lines)

    async def analyze_set(self, set_id: str, force: bool = False) -> list[CardPrice]:
        if not force and self.state.is_set_recent(set_id):
            return []

        # Try to get set name from the sets list for better fallback search
        set_name_hint = ""
        all_sets = await self.pokewallet.get_sets()
        for s in all_sets:
            sid = s.get("id", s.get("set_id", ""))
            if str(sid) == str(set_id):
                set_name_hint = s.get("name", "")
                break

        cards = await self.pokewallet.get_set_cards(set_id, set_name=set_name_hint)
        if not cards:
            return []

        set_name = cards[0].get("set", {}).get("name", set_name_hint or set_id) if cards else set_id
        profitable = []

        for card_data in cards:
            card_name = card_data.get("name", "Unknown")
            card_number = card_data.get("number", card_data.get("card_number", ""))
            rarity = card_data.get("rarity", "")
            images = card_data.get("images", {})
            image_url = card_data.get("image") or images.get("small") or images.get("large") or ""

            # Extract Cardmarket price (handles both PokéWallet and pokemontcg.io formats)
            cardmarket_data = card_data.get("cardmarket", {})
            prices = cardmarket_data.get("prices", card_data.get("prices", {}))
            cardmarket_price = 0.0
            if isinstance(prices, dict):
                cm = prices.get("cardmarket", prices)
                cardmarket_price = float(
                    cm.get("trendPrice") or cm.get("averageSellPrice") or
                    cm.get("trend") or cm.get("price") or 0
                )

            if cardmarket_price < 1.0:
                continue

            search_query = f"Pokemon {card_name} {card_number} {set_name}"
            ebay_listings = await self.ebay.search_sold_listings(search_query, max_results=5)
            await asyncio.sleep(REQUEST_DELAY)

            if not ebay_listings:
                continue

            # Son 3 satışın ortalamasını al (daha gerçekçi fiyat)
            sorted_listings = sorted(ebay_listings, key=lambda x: x["total_gbp"])
            last_3 = sorted_listings[:3]
            avg_total_gbp = sum(l["total_gbp"] for l in last_3) / len(last_3)
            cheapest = last_3[0]  # link ve resim için en ucuzunu kullan
            calc = self.calculator.calculate(avg_total_gbp, cardmarket_price)

            if calc["is_profitable"]:
                card_price = CardPrice(
                    name=card_name,
                    set_name=set_name,
                    set_id=set_id,
                    card_number=str(card_number),
                    rarity=rarity,
                    image_url=image_url or cheapest.get("image_url", ""),
                    ebay_price_gbp=cheapest["price_gbp"],
                    ebay_shipping_gbp=cheapest["shipping_gbp"],
                    ebay_total_gbp=cheapest["total_gbp"],
                    ebay_url=cheapest["url"],
                    ebay_condition=cheapest.get("condition", ""),
                    cardmarket_price_eur=cardmarket_price,
                    cardmarket_url=f"https://www.cardmarket.com/en/Pokemon/Products/Search?searchString={card_name.replace(' ', '+')}",
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

        self.state.mark_set_analyzed(set_id, set_name, len(profitable))
        await self.telegram.send_set_summary(set_name, profitable)
        print(f"[✓] {set_name}: {len(profitable)} kârlı kart")
        return profitable

    async def quick_check(self, card_name: str) -> str:
        results = await self.pokewallet.search_card(card_name)
        if not results:
            return f"❌ '{card_name}' bulunamadı."

        card_data = results[0]
        name = card_data.get("name", card_name)
        number = card_data.get("number", "")
        set_info = card_data.get("set", {})
        set_name = set_info.get("name", "")
        images = card_data.get("images", {})
        image_url = card_data.get("image") or images.get("small") or images.get("large") or ""

        cardmarket_data = card_data.get("cardmarket", {})
        prices = cardmarket_data.get("prices", card_data.get("prices", {}))
        cardmarket_price = 0.0
        if isinstance(prices, dict):
            cm = prices.get("cardmarket", prices)
            cardmarket_price = float(
                cm.get("trendPrice") or cm.get("averageSellPrice") or
                cm.get("trend") or cm.get("price") or 0
            )

        if cardmarket_price < 0.5:
            return f"❌ {name}: Cardmarket fiyatı çok düşük (€{cardmarket_price:.2f})"

        search_query = f"Pokemon {name} {number}"
        ebay_listings = await self.ebay.search_sold_listings(search_query, max_results=5)

        if not ebay_listings:
            return f"❌ {name}: eBay UK sold listings'de bulunamadı."

        # Son 3 satışın ortalaması
        sorted_listings = sorted(ebay_listings, key=lambda x: x["total_gbp"])
        last_3 = sorted_listings[:3]
        avg_total_gbp = sum(l["total_gbp"] for l in last_3) / len(last_3)
        cheapest = last_3[0]
        calc = self.calculator.calculate(avg_total_gbp, cardmarket_price)

        status = "✅ KÂRLI" if calc["is_profitable"] else "❌ Kârsız"
        sold_info = " / ".join([f"£{l['total_gbp']:.2f}" for l in last_3])
        return (
            f"{status}\n\n"
            f"🃏 {name} #{number} ({set_name})\n"
            f"🇬🇧 eBay UK Son Satışlar: {sold_info}\n"
            f"🇬🇧 Ortalama: £{avg_total_gbp:.2f} (€{calc['total_cost_eur']:.2f})\n"
            f"🇪🇺 Cardmarket: €{cardmarket_price:.2f}\n"
            f"💵 Satış sonrası: €{calc['selling_price_after_fees_eur']:.2f}\n"
            f"💰 Kâr: €{calc['profit_eur']:.2f} ({calc['profit_percent']:.1f}%)\n"
            f"🔗 {cheapest['url']}"
        )


# --- Background Service ---

async def health_server():
    """Simple HTTP server for Render health checks."""
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
    """Listen for Telegram bot commands and inline button callbacks."""
    print("[Bot] Telegram komut dinleme başladı...")

    while True:
        try:
            updates = await engine.telegram.get_updates()
            for update in updates:
                # Handle callback queries (inline button presses)
                cb = update.get("callback_query")
                if cb:
                    cb_id = cb.get("id", "")
                    cb_data = cb.get("data", "")
                    cb_chat_id = str(cb.get("message", {}).get("chat", {}).get("id", ""))
                    print(f"[Bot] Processing callback: data='{cb_data}' chat={cb_chat_id}")

                    if cb_chat_id != TELEGRAM_CHAT_ID:
                        print(f"[Bot] Skipping callback: chat {cb_chat_id} != {TELEGRAM_CHAT_ID}")
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
                            set_id = cb_data[8:]
                            await engine.telegram.send_text(f"🔍 {set_id} analiz ediliyor... (bu biraz sürebilir)", cb_chat_id)
                            profitable = await engine.analyze_set(set_id, force=True)
                            if not profitable:
                                await engine.telegram.send_text(f"❌ {set_id}: Kârlı kart bulunamadı.", cb_chat_id)
                    except Exception as e:
                        print(f"[Bot] Callback error: {e}")
                        await engine.telegram.send_text(f"❌ Hata: {e}", cb_chat_id)

                    continue

                # Handle regular messages
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
                        await engine.telegram.send_text("Kullanım: /analyze <set_id>", chat_id)
                    else:
                        set_id = parts[1]
                        await engine.telegram.send_text(f"🔍 {set_id} analiz ediliyor...", chat_id)
                        profitable = await engine.analyze_set(set_id, force=True)
                        if not profitable:
                            await engine.telegram.send_text(f"❌ {set_id}: Kârlı kart bulunamadı.", chat_id)

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
                                f"   €{c['profit_eur']:.2f} kâr ({c['profit_percent']:.1f}%)"
                            )
                        await engine.telegram.send_text("\n".join(lines), chat_id)

                elif text.startswith("/help"):
                    await engine.telegram.send_text(
                        "🃏 Pokemon Arbitrage Bot\n\n"
                        "/series - Serileri butonlarla listele\n"
                        "/sets - Tüm setleri listele\n"
                        "/sets <seri adı> - Serinin setlerini butonlarla göster\n"
                        "/analyze <set_id> - Set analiz et\n"
                        "/check <kart adı> - Tek kart kontrol\n"
                        "/results - En kârlı kartlar\n"
                        "/status - Bot durumu\n"
                        "/help - Bu mesaj\n\n"
                        "Kullanım: /series tıkla → seri seç → set seç → otomatik analiz",
                        chat_id,
                    )

                elif text.startswith("/status"):
                    last_run = engine.state.state.get("last_run", "Hiç")
                    sets_done = len(engine.state.state.get("analyzed_sets", {}))
                    total_profitable = len(engine.state.results.get("profitable_cards", []))
                    await engine.telegram.send_text(
                        f"📊 Bot Durumu\n\n"
                        f"Son tarama: {last_run}\n"
                        f"Analiz edilen set: {sets_done}\n"
                        f"Bulunan kârlı kart: {total_profitable}\n"
                        f"Otomatik tarama: Her {AUTO_SCAN_INTERVAL_HOURS} saatte\n"
                        f"Watchlist: {', '.join(WATCHLIST_SETS) or 'Yok'}",
                        chat_id,
                    )

        except Exception as e:
            print(f"[Bot] Error: {e}")

        await asyncio.sleep(3)


async def auto_scan_loop(engine: ArbitrageEngine):
    """Periodically scan watchlist sets for arbitrage opportunities."""
    print(f"[AutoScan] Her {AUTO_SCAN_INTERVAL_HOURS} saatte otomatik tarama yapılacak")

    # Wait a bit on startup before first scan
    await asyncio.sleep(30)

    while True:
        try:
            sets_to_scan = WATCHLIST_SETS.copy()

            # If no watchlist, scan latest sets
            if not sets_to_scan:
                all_sets = await engine.pokewallet.get_sets()
                sorted_sets = sorted(all_sets, key=lambda s: _parse_date(s), reverse=True)
                sets_to_scan = [s.get("id", s.get("set_id", "")) for s in sorted_sets[:AUTO_SCAN_SET_COUNT]]

            print(f"[AutoScan] Taranacak setler: {sets_to_scan}")
            for set_id in sets_to_scan:
                if set_id:
                    await engine.analyze_set(set_id)
                    await asyncio.sleep(5)

        except Exception as e:
            print(f"[AutoScan] Error: {e}")

        await asyncio.sleep(AUTO_SCAN_INTERVAL_HOURS * 3600)


async def run_service():
    """Run as a background service (Render/Railway/Fly)."""
    engine = ArbitrageEngine()
    await engine.start()

    print("🃏 Pokemon Card Arbitrage Bot başlatıldı!")
    print(f"   Platform: Render/Railway/Fly")
    print(f"   Otomatik tarama: Her {AUTO_SCAN_INTERVAL_HOURS} saat")
    print(f"   Watchlist: {WATCHLIST_SETS or 'En son setler'}")
    print(f"   Min kâr: €{MIN_PROFIT_EUR} / %{MIN_PROFIT_PERCENT}")
    print()

    if TELEGRAM_TOKEN:
        await engine.telegram.send_text(
            "🚀 Pokemon Arbitrage Bot başladı!\n\n"
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
    """Run as CLI tool."""
    engine = ArbitrageEngine()
    await engine.start()

    try:
        command = sys.argv[1].lower()

        if command == "sets":
            print(await engine.list_sets())

        elif command == "analyze":
            if len(sys.argv) < 3:
                print("Kullanım: python arbitrage.py analyze <set_id> [--force]")
                return
            set_id = sys.argv[2]
            force = "--force" in sys.argv
            result = await engine.analyze_set(set_id, force=force)
            if not result:
                print(f"❌ '{set_id}': Kârlı kart bulunamadı (veya yakın zamanda taranmış).")

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
                sid = s.get("id", s.get("set_id", ""))
                if sid:
                    await engine.analyze_set(sid, force=True)

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
  python arbitrage.py serve          Arka plan servisi başlat (Render/Railway)
  python arbitrage.py sets           Setleri listele
  python arbitrage.py analyze <id>   Set analiz et
  python arbitrage.py check <ad>     Tek kart kontrol
  python arbitrage.py scan [N]       Son N seti tara
  python arbitrage.py results        Kârlı kartları göster

ÖRNEKLER:
  python arbitrage.py serve
  python arbitrage.py analyze sv6 --force
  python arbitrage.py check "Charizard ex"
  python arbitrage.py scan 3
""")


if __name__ == "__main__":
    if len(sys.argv) < 2 or sys.argv[1].lower() == "serve":
        asyncio.run(run_service())
    else:
        asyncio.run(run_cli())
