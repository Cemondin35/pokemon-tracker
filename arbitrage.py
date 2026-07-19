#!/usr/bin/env python3
"""
Pokemon Card Arbitrage System
Buy from eBay UK → Sell on Cardmarket for profit.

Compares eBay UK sold/listed prices with Cardmarket prices (via PokéWallet API)
to find profitable arbitrage opportunities.
"""

import asyncio
import hashlib
import json
import os
import sys
import time
from pathlib import Path
from dataclasses import dataclass, field, asdict
from typing import Optional

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

# eBay UK shipping estimate (buyer pays or included)
EBAY_UK_SHIPPING_ESTIMATE_GBP = 1.50

# GBP to EUR conversion rate (update regularly)
GBP_TO_EUR = float(os.environ.get("GBP_TO_EUR", "1.17"))

# Minimum profit threshold in EUR to flag a card
MIN_PROFIT_EUR = float(os.environ.get("MIN_PROFIT_EUR", "2.0"))
MIN_PROFIT_PERCENT = float(os.environ.get("MIN_PROFIT_PERCENT", "15.0"))

# PokéWallet API key (free tier: 100 req/hour, 1000/day)
POKEWALLET_API_KEY = os.environ.get("POKEWALLET_API_KEY", "")

# State file for tracking analyzed sets and profitable cards
STATE_FILE = "arbitrage_state.json"
RESULTS_FILE = "arbitrage_results.json"

# Rate limiting
REQUEST_DELAY = 2  # seconds between requests


@dataclass
class CardPrice:
    name: str
    set_name: str
    set_id: str
    card_number: str
    rarity: str = ""
    image_url: str = ""
    # eBay UK data
    ebay_price_gbp: float = 0.0
    ebay_shipping_gbp: float = 0.0
    ebay_total_gbp: float = 0.0
    ebay_url: str = ""
    ebay_condition: str = "Near Mint"
    # Cardmarket data
    cardmarket_price_eur: float = 0.0
    cardmarket_trend_eur: float = 0.0
    cardmarket_url: str = ""
    # Calculated
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
    release_date: str = ""
    total_cards: int = 0
    profitable_cards: list = field(default_factory=list)
    last_checked: str = ""


# --- PokéWallet API Client (Cardmarket Prices) ---

class PokeWalletClient:
    def __init__(self, client: httpx.AsyncClient):
        self.client = client
        self.base_url = POKEWALLET_API_BASE
        self.headers = {}
        if POKEWALLET_API_KEY:
            self.headers["Authorization"] = f"Bearer {POKEWALLET_API_KEY}"

    async def get_sets(self) -> list[dict]:
        """Get all available Pokemon TCG sets."""
        try:
            resp = await self.client.get(
                f"{self.base_url}/sets",
                headers=self.headers,
                timeout=30
            )
            resp.raise_for_status()
            data = resp.json()
            return data if isinstance(data, list) else data.get("data", data.get("sets", []))
        except Exception as e:
            print(f"[PokeWallet] Error fetching sets: {e}")
            return []

    async def get_set_cards(self, set_id: str) -> list[dict]:
        """Get all cards in a set with Cardmarket prices."""
        try:
            resp = await self.client.get(
                f"{self.base_url}/sets/{set_id}/cards",
                headers=self.headers,
                timeout=30
            )
            resp.raise_for_status()
            data = resp.json()
            return data if isinstance(data, list) else data.get("data", data.get("cards", []))
        except Exception as e:
            print(f"[PokeWallet] Error fetching cards for set {set_id}: {e}")
            return []

    async def get_card_price(self, card_id: str) -> dict:
        """Get detailed price data for a specific card."""
        try:
            resp = await self.client.get(
                f"{self.base_url}/cards/{card_id}/prices",
                headers=self.headers,
                timeout=30
            )
            resp.raise_for_status()
            return resp.json()
        except Exception as e:
            print(f"[PokeWallet] Error fetching price for card {card_id}: {e}")
            return {}

    async def search_card(self, name: str, set_name: str = "") -> list[dict]:
        """Search for a card by name."""
        params = {"q": name}
        if set_name:
            params["set"] = set_name
        try:
            resp = await self.client.get(
                f"{self.base_url}/cards",
                params=params,
                headers=self.headers,
                timeout=30
            )
            resp.raise_for_status()
            data = resp.json()
            return data if isinstance(data, list) else data.get("data", data.get("cards", []))
        except Exception as e:
            print(f"[PokeWallet] Error searching card '{name}': {e}")
            return []


# --- eBay UK Scraper ---

class EbayUKScraper:
    def __init__(self, client: httpx.AsyncClient):
        self.client = client
        self.base_url = EBAY_UK_BASE
        self.headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            "Accept-Language": "en-GB,en;q=0.9",
        }

    async def search_sold_listings(self, query: str, max_results: int = 10) -> list[dict]:
        """Search eBay UK sold/completed listings for a card."""
        params = {
            "_nkw": query,
            "_sacat": "183454",  # Pokemon TCG category
            "LH_Sold": "1",
            "LH_Complete": "1",
            "_sop": "13",  # Sort by price + shipping lowest first
            "LH_PrefLoc": "1",  # UK only
        }
        return await self._search(params, max_results)

    async def search_buy_it_now(self, query: str, max_results: int = 10) -> list[dict]:
        """Search eBay UK Buy It Now listings (current prices)."""
        params = {
            "_nkw": query,
            "_sacat": "183454",  # Pokemon TCG category
            "LH_BIN": "1",  # Buy It Now only
            "_sop": "15",  # Sort by price + shipping lowest first
            "LH_PrefLoc": "1",  # UK only
        }
        return await self._search(params, max_results)

    async def _search(self, params: dict, max_results: int) -> list[dict]:
        """Perform eBay search and parse results."""
        try:
            resp = await self.client.get(
                f"{self.base_url}/sch/i.html",
                params=params,
                headers=self.headers,
                timeout=30
            )
            resp.raise_for_status()
            return self._parse_listings(resp.text, max_results)
        except Exception as e:
            print(f"[eBay UK] Error searching: {e}")
            return []

    def _parse_listings(self, html: str, max_results: int) -> list[dict]:
        """Parse eBay search results HTML."""
        soup = BeautifulSoup(html, "lxml")
        listings = []

        items = soup.select(".s-item")
        for item in items[:max_results + 1]:
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
        """Extract numeric price from text like '£4.99' or '£2.50 postage'."""
        import re
        match = re.search(r"£([\d,]+\.?\d*)", text)
        if match:
            return float(match.group(1).replace(",", ""))
        return 0.0


# --- Arbitrage Calculator ---

class ArbitrageCalculator:
    @staticmethod
    def calculate(ebay_total_gbp: float, cardmarket_price_eur: float) -> dict:
        """Calculate profit from buying on eBay UK and selling on Cardmarket."""
        # Total cost in EUR (what you pay on eBay UK + shipping converted to EUR)
        total_cost_eur = ebay_total_gbp * GBP_TO_EUR

        # What you receive after Cardmarket takes their cut
        cardmarket_fee = cardmarket_price_eur * (CARDMARKET_COMMISSION_PERCENT / 100)
        selling_revenue_eur = cardmarket_price_eur - cardmarket_fee - CARDMARKET_SHIPPING_COST_EUR

        # Profit
        profit_eur = selling_revenue_eur - total_cost_eur
        profit_percent = (profit_eur / total_cost_eur * 100) if total_cost_eur > 0 else 0

        return {
            "total_cost_eur": round(total_cost_eur, 2),
            "selling_price_after_fees_eur": round(selling_revenue_eur, 2),
            "profit_eur": round(profit_eur, 2),
            "profit_percent": round(profit_percent, 1),
            "is_profitable": profit_eur >= MIN_PROFIT_EUR and profit_percent >= MIN_PROFIT_PERCENT,
        }


# --- Telegram Notifier ---

class TelegramNotifier:
    def __init__(self, client: httpx.AsyncClient):
        self.client = client
        self.token = TELEGRAM_TOKEN
        self.chat_id = TELEGRAM_CHAT_ID

    async def send_profitable_card(self, card: CardPrice):
        """Send a profitable card alert with image to Telegram."""
        if not self.token or not self.chat_id:
            print("[Telegram] Token or chat_id not configured, skipping notification")
            return

        message = (
            f"💰 *KÂR FIRSATI!*\n\n"
            f"🃏 *{self._escape_md(card.name)}*\n"
            f"📦 Set: {self._escape_md(card.set_name)}\n"
            f"🔢 #{card.card_number}\n"
            f"⭐ {self._escape_md(card.rarity)}\n\n"
            f"🇬🇧 *eBay UK:* £{card.ebay_total_gbp:.2f} (€{card.total_cost_eur:.2f})\n"
            f"🇪🇺 *Cardmarket:* €{card.cardmarket_price_eur:.2f}\n"
            f"💵 *Satış sonrası:* €{card.selling_price_after_fees_eur:.2f}\n\n"
            f"✅ *KÂR: €{card.profit_eur:.2f} ({card.profit_percent:.1f}%)*\n\n"
            f"🔗 [eBay'den Al]({card.ebay_url})\n"
            f"🔗 [Cardmarket]({card.cardmarket_url})"
        )

        if card.image_url:
            await self._send_photo(card.image_url, message)
        else:
            await self._send_message(message)

    async def send_set_summary(self, set_info: SetInfo, profitable_cards: list[CardPrice]):
        """Send a summary of profitable cards in a set."""
        if not self.token or not self.chat_id:
            return

        if not profitable_cards:
            return

        message = (
            f"📊 *SET ANALİZİ TAMAMLANDI*\n\n"
            f"📦 *{self._escape_md(set_info.name)}*\n"
            f"💰 Kârlı kart sayısı: *{len(profitable_cards)}*\n\n"
        )

        for i, card in enumerate(profitable_cards[:10], 1):
            message += (
                f"{i}. {self._escape_md(card.name)} #{card.card_number}\n"
                f"   💵 Kâr: €{card.profit_eur:.2f} ({card.profit_percent:.1f}%)\n"
            )

        if len(profitable_cards) > 10:
            message += f"\n...ve {len(profitable_cards) - 10} kart daha!"

        await self._send_message(message)

    async def _send_photo(self, photo_url: str, caption: str):
        """Send photo with caption."""
        try:
            url = f"https://api.telegram.org/bot{self.token}/sendPhoto"
            data = {
                "chat_id": self.chat_id,
                "photo": photo_url,
                "caption": caption,
                "parse_mode": "Markdown",
            }
            await self.client.post(url, json=data, timeout=30)
        except Exception as e:
            print(f"[Telegram] Error sending photo: {e}")
            await self._send_message(caption)

    async def _send_message(self, text: str):
        """Send text message."""
        try:
            url = f"https://api.telegram.org/bot{self.token}/sendMessage"
            data = {
                "chat_id": self.chat_id,
                "text": text,
                "parse_mode": "Markdown",
                "disable_web_page_preview": False,
            }
            await self.client.post(url, json=data, timeout=30)
        except Exception as e:
            print(f"[Telegram] Error sending message: {e}")

    @staticmethod
    def _escape_md(text: str) -> str:
        """Escape special Markdown characters."""
        for char in ["_", "*", "[", "]", "(", ")", "~", "`", ">", "#", "+", "-", "=", "|", "{", "}", ".", "!"]:
            text = text.replace(char, f"\\{char}")
        return text


# --- State Manager ---

class StateManager:
    def __init__(self):
        self.state = self._load_state()
        self.results = self._load_results()

    def _load_state(self) -> dict:
        try:
            with open(STATE_FILE, "r") as f:
                return json.load(f)
        except (FileNotFoundError, json.JSONDecodeError):
            return {"analyzed_sets": {}, "last_run": ""}

    def _load_results(self) -> dict:
        try:
            with open(RESULTS_FILE, "r") as f:
                return json.load(f)
        except (FileNotFoundError, json.JSONDecodeError):
            return {"profitable_cards": [], "sets_analyzed": 0}

    def save_state(self):
        with open(STATE_FILE, "w") as f:
            json.dump(self.state, f, indent=2)

    def save_results(self):
        with open(RESULTS_FILE, "w") as f:
            json.dump(self.results, f, indent=2, default=str)

    def mark_set_analyzed(self, set_id: str, set_name: str, profitable_count: int):
        self.state["analyzed_sets"][set_id] = {
            "name": set_name,
            "profitable_count": profitable_count,
            "last_checked": time.strftime("%Y-%m-%d %H:%M"),
        }
        self.state["last_run"] = time.strftime("%Y-%m-%d %H:%M")
        self.save_state()

    def add_profitable_card(self, card: CardPrice):
        card_data = {
            "name": card.name,
            "set_name": card.set_name,
            "card_number": card.card_number,
            "rarity": card.rarity,
            "ebay_price_gbp": card.ebay_total_gbp,
            "cardmarket_price_eur": card.cardmarket_price_eur,
            "profit_eur": card.profit_eur,
            "profit_percent": card.profit_percent,
            "ebay_url": card.ebay_url,
            "cardmarket_url": card.cardmarket_url,
            "image_url": card.image_url,
            "found_at": time.strftime("%Y-%m-%d %H:%M"),
        }
        self.results["profitable_cards"].append(card_data)
        self.save_results()

    def is_set_analyzed_recently(self, set_id: str, hours: int = 24) -> bool:
        """Check if set was analyzed within the last N hours."""
        set_data = self.state.get("analyzed_sets", {}).get(set_id)
        if not set_data:
            return False
        last_checked = set_data.get("last_checked", "")
        if not last_checked:
            return False
        try:
            from datetime import datetime
            checked_time = datetime.strptime(last_checked, "%Y-%m-%d %H:%M")
            diff = datetime.now() - checked_time
            return diff.total_seconds() < hours * 3600
        except Exception:
            return False


# --- Main Arbitrage Engine ---

class ArbitrageEngine:
    def __init__(self):
        self.client: Optional[httpx.AsyncClient] = None
        self.pokewallet: Optional[PokeWalletClient] = None
        self.ebay: Optional[EbayUKScraper] = None
        self.telegram: Optional[TelegramNotifier] = None
        self.state: Optional[StateManager] = None
        self.calculator = ArbitrageCalculator()

    async def initialize(self):
        """Initialize HTTP client and all components."""
        self.client = httpx.AsyncClient(
            follow_redirects=True,
            timeout=30,
        )
        self.pokewallet = PokeWalletClient(self.client)
        self.ebay = EbayUKScraper(self.client)
        self.telegram = TelegramNotifier(self.client)
        self.state = StateManager()

    async def close(self):
        if self.client:
            await self.client.aclose()

    async def list_sets(self) -> list[dict]:
        """List all available sets."""
        sets = await self.pokewallet.get_sets()
        print(f"\n📦 {len(sets)} set bulundu.\n")
        for i, s in enumerate(sets[:30], 1):
            name = s.get("name", "Unknown")
            sid = s.get("id", s.get("set_id", ""))
            total = s.get("total", s.get("totalCards", "?"))
            print(f"  {i:2d}. [{sid}] {name} ({total} kart)")
        if len(sets) > 30:
            print(f"  ... ve {len(sets) - 30} set daha")
        return sets

    async def analyze_set(self, set_id: str, force: bool = False) -> list[CardPrice]:
        """Analyze a complete set for arbitrage opportunities."""
        if not force and self.state.is_set_analyzed_recently(set_id):
            print(f"[!] Set '{set_id}' son 24 saatte zaten analiz edilmiş. --force ile tekrar çalıştır.")
            return []

        print(f"\n🔍 Set analiz ediliyor: {set_id}")
        print("=" * 50)

        # Get all cards in the set from PokéWallet (includes Cardmarket prices)
        cards = await self.pokewallet.get_set_cards(set_id)
        if not cards:
            print(f"[!] Set '{set_id}' için kart bulunamadı.")
            return []

        set_name = cards[0].get("set", {}).get("name", set_id) if cards else set_id
        print(f"📦 {set_name}: {len(cards)} kart bulundu")
        print(f"🔄 eBay UK fiyatları kontrol ediliyor...\n")

        profitable_cards = []

        for i, card_data in enumerate(cards, 1):
            card_name = card_data.get("name", "Unknown")
            card_number = card_data.get("number", card_data.get("card_number", ""))
            rarity = card_data.get("rarity", "")
            image_url = card_data.get("image", card_data.get("images", {}).get("small", ""))

            # Get Cardmarket price
            prices = card_data.get("prices", card_data.get("cardmarket", {}))
            cardmarket_price = 0.0
            cardmarket_trend = 0.0

            if isinstance(prices, dict):
                cm = prices.get("cardmarket", prices)
                cardmarket_price = float(cm.get("trendPrice", cm.get("trend", cm.get("price", 0))) or 0)
                cardmarket_trend = float(cm.get("avg30", cm.get("averageSellPrice", 0)) or 0)

            # Skip cards with no/very low Cardmarket price
            if cardmarket_price < 1.0:
                continue

            # Search eBay UK for this card
            search_query = f"Pokemon {card_name} {card_number} {set_name}"
            ebay_listings = await self.ebay.search_buy_it_now(search_query, max_results=5)
            await asyncio.sleep(REQUEST_DELAY)

            if not ebay_listings:
                # Try sold listings if no BIN available
                ebay_listings = await self.ebay.search_sold_listings(search_query, max_results=5)
                await asyncio.sleep(REQUEST_DELAY)

            if not ebay_listings:
                continue

            # Use the cheapest listing
            cheapest = min(ebay_listings, key=lambda x: x["total_gbp"])

            # Calculate arbitrage
            calc = self.calculator.calculate(cheapest["total_gbp"], cardmarket_price)

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
                cardmarket_trend_eur=cardmarket_trend,
                cardmarket_url=f"https://www.cardmarket.com/en/Pokemon/Products/Search?searchString={card_name.replace(' ', '+')}",
                total_cost_eur=calc["total_cost_eur"],
                selling_price_after_fees_eur=calc["selling_price_after_fees_eur"],
                profit_eur=calc["profit_eur"],
                profit_percent=calc["profit_percent"],
                is_profitable=calc["is_profitable"],
            )

            status = "✅" if card_price.is_profitable else "❌"
            print(
                f"  [{i}/{len(cards)}] {status} {card_name} #{card_number} | "
                f"eBay: £{cheapest['total_gbp']:.2f} → CM: €{cardmarket_price:.2f} | "
                f"Kâr: €{calc['profit_eur']:.2f} ({calc['profit_percent']:.1f}%)"
            )

            if card_price.is_profitable:
                profitable_cards.append(card_price)
                self.state.add_profitable_card(card_price)
                await self.telegram.send_profitable_card(card_price)

        # Save state
        self.state.mark_set_analyzed(set_id, set_name, len(profitable_cards))

        # Send summary
        set_info = SetInfo(id=set_id, name=set_name)
        await self.telegram.send_set_summary(set_info, profitable_cards)

        print(f"\n{'=' * 50}")
        print(f"📊 SONUÇ: {set_name}")
        print(f"   Toplam kart: {len(cards)}")
        print(f"   Kârlı kart: {len(profitable_cards)}")
        if profitable_cards:
            total_profit = sum(c.profit_eur for c in profitable_cards)
            print(f"   Toplam potansiyel kâr: €{total_profit:.2f}")
        print()

        return profitable_cards

    async def quick_check(self, card_name: str, set_name: str = "") -> Optional[CardPrice]:
        """Quick check a single card for arbitrage opportunity."""
        print(f"\n🔍 Hızlı kontrol: {card_name}")

        # Search PokéWallet for Cardmarket price
        results = await self.pokewallet.search_card(card_name, set_name)
        if not results:
            print(f"[!] '{card_name}' PokéWallet'ta bulunamadı.")
            return None

        card_data = results[0]
        card_number = card_data.get("number", "")
        set_info = card_data.get("set", {})
        actual_set_name = set_info.get("name", set_name)
        image_url = card_data.get("image", card_data.get("images", {}).get("small", ""))

        prices = card_data.get("prices", card_data.get("cardmarket", {}))
        cardmarket_price = 0.0
        if isinstance(prices, dict):
            cm = prices.get("cardmarket", prices)
            cardmarket_price = float(cm.get("trendPrice", cm.get("trend", cm.get("price", 0))) or 0)

        if cardmarket_price < 0.5:
            print(f"[!] Cardmarket fiyatı çok düşük: €{cardmarket_price:.2f}")
            return None

        # Search eBay UK
        search_query = f"Pokemon {card_name} {card_number}"
        ebay_listings = await self.ebay.search_buy_it_now(search_query, max_results=5)
        if not ebay_listings:
            ebay_listings = await self.ebay.search_sold_listings(search_query, max_results=5)

        if not ebay_listings:
            print(f"[!] '{card_name}' eBay UK'de bulunamadı.")
            return None

        cheapest = min(ebay_listings, key=lambda x: x["total_gbp"])
        calc = self.calculator.calculate(cheapest["total_gbp"], cardmarket_price)

        status = "✅ KÂRLI!" if calc["is_profitable"] else "❌ Kârsız"
        print(f"\n  📊 {status}")
        print(f"  🇬🇧 eBay UK: £{cheapest['total_gbp']:.2f} (€{calc['total_cost_eur']:.2f})")
        print(f"  🇪🇺 Cardmarket: €{cardmarket_price:.2f}")
        print(f"  💰 Kâr: €{calc['profit_eur']:.2f} ({calc['profit_percent']:.1f}%)")

        return None

    async def scan_trending_sets(self, limit: int = 5):
        """Scan trending/popular sets for arbitrage opportunities."""
        print("\n🔥 Trending setler taranıyor...")
        sets = await self.pokewallet.get_sets()

        # Sort by release date (newest first) and take top N
        sets_sorted = sorted(sets, key=lambda s: s.get("releaseDate", ""), reverse=True)

        all_profitable = []
        for s in sets_sorted[:limit]:
            set_id = s.get("id", s.get("set_id", ""))
            if set_id:
                profitable = await self.analyze_set(set_id)
                all_profitable.extend(profitable)

        print(f"\n🎉 TOPLAM: {len(all_profitable)} kârlı kart bulundu!")
        return all_profitable


# --- CLI Interface ---

async def main():
    engine = ArbitrageEngine()
    await engine.initialize()

    try:
        if len(sys.argv) < 2:
            print_help()
            return

        command = sys.argv[1].lower()

        if command == "sets":
            await engine.list_sets()

        elif command == "analyze":
            if len(sys.argv) < 3:
                print("Kullanım: python arbitrage.py analyze <set_id> [--force]")
                return
            set_id = sys.argv[2]
            force = "--force" in sys.argv
            await engine.analyze_set(set_id, force=force)

        elif command == "check":
            if len(sys.argv) < 3:
                print("Kullanım: python arbitrage.py check <kart_adı>")
                return
            card_name = " ".join(sys.argv[2:])
            await engine.quick_check(card_name)

        elif command == "scan":
            limit = int(sys.argv[2]) if len(sys.argv) > 2 else 5
            await engine.scan_trending_sets(limit=limit)

        elif command == "results":
            show_results()

        else:
            print_help()

    finally:
        await engine.close()


def print_help():
    print("""
╔══════════════════════════════════════════════════════════════╗
║          🃏 Pokemon Card Arbitrage System 🃏                 ║
║          eBay UK → Cardmarket Kâr Sistemi                   ║
╠══════════════════════════════════════════════════════════════╣
║                                                              ║
║  Komutlar:                                                   ║
║                                                              ║
║  sets              Tüm setleri listele                       ║
║  analyze <set_id>  Bir seti analiz et (kârlı kartları bul)   ║
║  check <kart_adı>  Tek bir kartı hızlı kontrol et           ║
║  scan [N]          Son N trending seti tara (varsayılan: 5)  ║
║  results           Bulunan kârlı kartları göster             ║
║                                                              ║
║  Örnekler:                                                   ║
║  python arbitrage.py sets                                    ║
║  python arbitrage.py analyze sv6                             ║
║  python arbitrage.py check "Charizard ex"                    ║
║  python arbitrage.py scan 3                                  ║
║                                                              ║
║  Ortam Değişkenleri:                                         ║
║  TELEGRAM_TOKEN     - Telegram bot token                     ║
║  TELEGRAM_CHAT_ID   - Telegram chat ID                       ║
║  POKEWALLET_API_KEY - PokéWallet API anahtarı               ║
║  GBP_TO_EUR         - GBP/EUR kuru (varsayılan: 1.17)        ║
║  MIN_PROFIT_EUR     - Min kâr eşiği € (varsayılan: 2.0)     ║
║  MIN_PROFIT_PERCENT - Min kâr yüzdesi (varsayılan: 15%)     ║
║                                                              ║
╚══════════════════════════════════════════════════════════════╝
""")


def show_results():
    """Display saved profitable cards."""
    try:
        with open(RESULTS_FILE, "r") as f:
            data = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        print("[!] Henüz sonuç yok. Önce bir set analiz edin.")
        return

    cards = data.get("profitable_cards", [])
    if not cards:
        print("[!] Henüz kârlı kart bulunamadı.")
        return

    print(f"\n💰 KÂRLI KARTLAR ({len(cards)} adet)")
    print("=" * 70)

    # Sort by profit
    cards_sorted = sorted(cards, key=lambda c: c.get("profit_eur", 0), reverse=True)

    for i, card in enumerate(cards_sorted, 1):
        print(
            f"  {i:3d}. {card['name']} #{card.get('card_number', '?')} "
            f"({card['set_name']})\n"
            f"       eBay: £{card['ebay_price_gbp']:.2f} → CM: €{card['cardmarket_price_eur']:.2f} | "
            f"Kâr: €{card['profit_eur']:.2f} ({card['profit_percent']:.1f}%)\n"
            f"       Bulunma: {card.get('found_at', '?')}"
        )

    total_profit = sum(c.get("profit_eur", 0) for c in cards)
    print(f"\n  📊 Toplam potansiyel kâr: €{total_profit:.2f}")
    print()


if __name__ == "__main__":
    asyncio.run(main())
