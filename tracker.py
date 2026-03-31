"""
Pokemon Stock Tracker v2
- Shopify mağazalar için JSON API (hızlı & güvenilir)
- WooCommerce / PrestaShop / BigCommerce için HTML scraping
- Telegram bildirimleri
"""

import os
import json
import logging
import hashlib
import asyncio
import threading
from http.server import HTTPServer, BaseHTTPRequestHandler
from pathlib import Path
from datetime import datetime

import httpx
from bs4 import BeautifulSoup

# ── Logging ───────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler("tracker.log", encoding="utf-8"),
        logging.StreamHandler(),
    ],
)
log = logging.getLogger(__name__)

CONFIG_FILE = Path("config.json")
STATE_FILE  = Path("state.json")

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en-IE,en;q=0.9",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}


def load_json(path: Path, default):
    if path.exists():
        return json.loads(path.read_text(encoding="utf-8"))
    return default


def save_json(path: Path, data):
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")


def make_id(site_name: str, product_id) -> str:
    return hashlib.md5(f"{site_name}:{product_id}".encode()).hexdigest()[:12]


# ── Telegram ──────────────────────────────────────────────────────────────────
async def send_telegram(token: str, chat_id: str, text: str):
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = {"chat_id": chat_id, "text": text, "parse_mode": "HTML", "disable_web_page_preview": False}
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            r = await client.post(url, json=payload)
            if r.status_code != 200:
                log.error(f"Telegram hatası: {r.text}")
            else:
                log.info("✅ Telegram bildirimi gönderildi")
    except Exception as e:
        log.error(f"Telegram bağlantı hatası: {e}")


# ── Shopify JSON API ──────────────────────────────────────────────────────────
def scrape_shopify(site: dict) -> list[dict]:
    base_url   = site["base_url"]
    collection = site.get("collection_handle", "pokemon")
    api_url    = f"{base_url}/collections/{collection}/products.json?limit=250"

    try:
        r = httpx.get(api_url, headers=HEADERS, timeout=20, follow_redirects=True)
        r.raise_for_status()
        data = r.json()
    except Exception as e:
        log.warning(f"[{site['name']}] Shopify API hatası: {e}")
        return []

    products = []
    for p in data.get("products", []):
        variants = p.get("variants", [])
        in_stock = any(v.get("available", False) for v in variants)
        price = ""
        if variants:
            raw = variants[0].get("price", "")
            try:
                price = f"€{float(raw):.2f}" if raw else ""
            except Exception:
                price = str(raw)

        handle = p.get("handle", "")
        url    = f"{base_url}/products/{handle}" if handle else ""

        products.append({
            "id":       make_id(site["name"], p["id"]),
            "name":     p.get("title", "?"),
            "url":      url,
            "price":    price,
            "in_stock": in_stock,
        })

    log.info(f"[{site['name']}] Shopify API → {len(products)} ürün")
    return products


# ── WooCommerce HTML ──────────────────────────────────────────────────────────
def scrape_woocommerce(site: dict) -> list[dict]:
    try:
        r = httpx.get(site["url"], headers=HEADERS, timeout=20, follow_redirects=True)
        r.raise_for_status()
    except Exception as e:
        log.warning(f"[{site['name']}] WooCommerce bağlanamadı: {e}")
        return []

    soup  = BeautifulSoup(r.text, "html.parser")
    cards = soup.select("li.product, .product-type-simple, .type-product")

    if not cards:
        log.warning(f"[{site['name']}] WooCommerce ürün kartı bulunamadı")
        return []

    products = []
    for card in cards:
        name_el  = card.select_one(".woocommerce-loop-product__title, h2.product-name, .product_title")
        name     = name_el.get_text(strip=True) if name_el else "?"
        price_el = card.select_one(".price .woocommerce-Price-amount, .price bdi")
        price    = price_el.get_text(strip=True) if price_el else ""
        link_el  = card.select_one("a.woocommerce-LoopProduct-link, a")
        href     = link_el["href"] if link_el and link_el.get("href") else ""
        in_stock = "outofstock" not in card.get("class", [])
        products.append({"id": make_id(site["name"], name + href), "name": name, "url": href, "price": price, "in_stock": in_stock})

    log.info(f"[{site['name']}] WooCommerce → {len(products)} ürün")
    return products


# ── PrestaShop HTML ───────────────────────────────────────────────────────────
def scrape_prestashop(site: dict) -> list[dict]:
    try:
        r = httpx.get(site["url"], headers=HEADERS, timeout=20, follow_redirects=True)
        r.raise_for_status()
    except Exception as e:
        log.warning(f"[{site['name']}] PrestaShop bağlanamadı: {e}")
        return []

    soup  = BeautifulSoup(r.text, "html.parser")
    cards = soup.select(".js-product-miniature, .product-miniature, .ajax_block_product")

    if not cards:
        log.warning(f"[{site['name']}] PrestaShop ürün kartı bulunamadı")
        return []

    products = []
    for card in cards:
        name_el  = card.select_one(".product-title a, h3.product-title, .product_name a")
        name     = name_el.get_text(strip=True) if name_el else "?"
        href     = name_el["href"] if name_el and name_el.get("href") else ""
        price_el = card.select_one(".price, .product-price-and-shipping .price")
        price    = price_el.get_text(strip=True) if price_el else ""
        out_el   = card.select_one(".product-unavailable, .out-of-stock, .unavailable")
        in_stock = out_el is None
        products.append({"id": make_id(site["name"], name + href), "name": name, "url": href, "price": price, "in_stock": in_stock})

    log.info(f"[{site['name']}] PrestaShop → {len(products)} ürün")
    return products


# ── BigCommerce HTML ──────────────────────────────────────────────────────────
def scrape_bigcommerce(site: dict) -> list[dict]:
    base = site.get("base_url", "")
    try:
        r = httpx.get(site["url"], headers=HEADERS, timeout=20, follow_redirects=True)
        r.raise_for_status()
    except Exception as e:
        log.warning(f"[{site['name']}] BigCommerce bağlanamadı: {e}")
        return []

    soup  = BeautifulSoup(r.text, "html.parser")
    cards = soup.select(".productGrid .product, [data-product-id], .listItem")

    if not cards:
        log.warning(f"[{site['name']}] BigCommerce ürün kartı bulunamadı")
        return []

    products = []
    for card in cards:
        name_el  = card.select_one(".card-title, .productCard-title, h4 a, h3 a")
        name     = name_el.get_text(strip=True) if name_el else "?"
        href     = name_el["href"] if name_el and name_el.get("href") else ""
        if href and not href.startswith("http"):
            href = base + href
        price_el = card.select_one(".price--withTax, .price--withoutTax, .productView-price")
        price    = price_el.get_text(strip=True) if price_el else ""
        sold_el  = card.select_one(".soldOut, .product-soldout")
        in_stock = sold_el is None
        products.append({"id": make_id(site["name"], name + href), "name": name, "url": href, "price": price, "in_stock": in_stock})

    log.info(f"[{site['name']}] BigCommerce → {len(products)} ürün")
    return products


# ── Generic HTML (fallback) ───────────────────────────────────────────────────
def scrape_generic(site: dict) -> list[dict]:
    base = site.get("base_url", "")
    try:
        r = httpx.get(site["url"], headers=HEADERS, timeout=20, follow_redirects=True)
        r.raise_for_status()
    except Exception as e:
        log.warning(f"[{site['name']}] Bağlanamadı: {e}")
        return []

    soup         = BeautifulSoup(r.text, "html.parser")
    product_sel  = site.get("product_selector", ".product-item")
    cards        = soup.select(product_sel)

    if not cards:
        log.warning(f"[{site['name']}] '{product_sel}' ile ürün bulunamadı")
        return []

    products     = []
    soldout_text = site.get("soldout_text", "")
    for card in cards:
        name_el  = card.select_one(site.get("name_selector",  "h2"))
        price_el = card.select_one(site.get("price_selector", ".price"))
        link_el  = card.select_one("a")
        name     = name_el.get_text(strip=True)  if name_el  else "?"
        price    = price_el.get_text(strip=True) if price_el else ""
        href     = link_el["href"] if link_el and link_el.get("href") else ""
        if href and not href.startswith("http"):
            href = base + href
        in_stock = (soldout_text not in card.get_text()) if soldout_text else True
        products.append({"id": make_id(site["name"], name + href), "name": name, "url": href, "price": price, "in_stock": in_stock})

    log.info(f"[{site['name']}] Generic → {len(products)} ürün")
    return products


SCRAPERS = {
    "shopify":     scrape_shopify,
    "woocommerce": scrape_woocommerce,
    "prestashop":  scrape_prestashop,
    "bigcommerce": scrape_bigcommerce,
    "generic":     scrape_generic,
}


def scrape_site(site: dict) -> list[dict]:
    return SCRAPERS.get(site.get("platform", "generic"), scrape_generic)(site)


# ── Bildirim mesajı ───────────────────────────────────────────────────────────
def build_message(site_name: str, new_products: list, restocked: list) -> str:
    lines = ["🎮 <b>Pokemon Stok Bildirimi</b>",
             f"🏪 <b>{site_name}</b>",
             f"🕐 {datetime.now().strftime('%d.%m.%Y %H:%M')}\n"]

    if new_products:
        lines.append("✨ <b>YENİ ÜRÜNLER:</b>")
        for p in new_products[:15]:
            extra = f" — {p['price']}" if p["price"] else ""
            link  = f'\n   🔗 <a href="{p["url"]}">Satın al</a>' if p["url"] else ""
            lines.append(f"  • {p['name']}{extra}{link}")
        if len(new_products) > 15:
            lines.append(f"  ... ve {len(new_products) - 15} ürün daha")

    if restocked:
        lines.append("\n📦 <b>YENİDEN STOKTA:</b>")
        for p in restocked[:15]:
            extra = f" — {p['price']}" if p["price"] else ""
            link  = f'\n   🔗 <a href="{p["url"]}">Satın al</a>' if p["url"] else ""
            lines.append(f"  • {p['name']}{extra}{link}")
        if len(restocked) > 15:
            lines.append(f"  ... ve {len(restocked) - 15} ürün daha")

    return "\n".join(lines)


# ── Site kontrolü ─────────────────────────────────────────────────────────────
async def check_site(site: dict, state: dict, config: dict):
    site_key = site["name"]
    known    = state.get(site_key, {})
    products = scrape_site(site)
    if not products:
        return

    new_products, restocked = [], []
    for p in products:
        if p["id"] not in known:
            if p["in_stock"]:
                new_products.append(p)
        elif not known[p["id"]].get("in_stock", True) and p["in_stock"]:
            restocked.append(p)

    state[site_key] = {p["id"]: p for p in products}

    if new_products or restocked:
        msg = build_message(site_key, new_products, restocked)
        await send_telegram(config["_token"], config["_chat_id"], msg)
    else:
        log.info(f"[{site_key}] Değişiklik yok ({len(products)} ürün)")


# ── Ana döngü ─────────────────────────────────────────────────────────────────
async def main():
    config = load_json(CONFIG_FILE, None)
    if not config:
        log.error("❌ config.json bulunamadı!"); return

    # Fly.io secrets > config.json
    token    = os.getenv("TELEGRAM_TOKEN")   or config.get("telegram_token", "")
    chat_id  = os.getenv("TELEGRAM_CHAT_ID") or config.get("telegram_chat_id", "")
    sites    = config.get("sites", [])
    interval = config.get("check_interval_minutes", 15)

    if not token or token == "BURAYA_YENİ_TOKEN_YAZ":
        log.error("❌ Telegram token eksik! config.json'a yaz veya fly secrets set ile ekle."); return
    if not chat_id or chat_id == "BURAYA_CHAT_ID_YAZ":
        log.error("❌ Telegram chat_id eksik! config.json'a yaz veya fly secrets set ile ekle."); return
    if not sites:
        log.error("❌ config.json'da site bulunamadı!"); return

    # Çözülmüş değerleri config'e göm (check_site'a geçmek için)
    config["_token"]   = token
    config["_chat_id"] = chat_id

    log.info(f"🚀 Tracker başladı — {len(sites)} site, her {interval} dk")
    await send_telegram(token, chat_id,
        f"🚀 <b>Pokemon Tracker Başladı!</b>\n"
        f"📡 {len(sites)} site takip ediliyor\n"
        f"⏱ Her {interval} dakikada kontrol\n"
        f"🕐 {datetime.now().strftime('%d.%m.%Y %H:%M')}"
    )

    while True:
        state = load_json(STATE_FILE, {})
        log.info(f"── Döngü {datetime.now().strftime('%H:%M')} ──")
        for site in sites:
            try:
                await check_site(site, state, config)
                await asyncio.sleep(2)
            except Exception as e:
                log.error(f"[{site['name']}] Hata: {e}")
        save_json(STATE_FILE, state)
        log.info(f"── {interval} dk bekleniyor ──\n")
        await asyncio.sleep(interval * 60)


# ── Render için basit web sunucusu (uyumasın diye) ────────────────────────────
class HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"Pokemon Tracker is running!")
    def log_message(self, format, *args):
        pass  # HTTP loglarını sustur

def start_web_server():
    port = int(os.getenv("PORT", 8080))
    server = HTTPServer(("0.0.0.0", port), HealthHandler)
    log.info(f"Web sunucusu port {port} üzerinde başladı")
    server.serve_forever()


if __name__ == "__main__":
    # Web sunucusunu arka planda başlat (Render için)
    t = threading.Thread(target=start_web_server, daemon=True)
    t.start()
    asyncio.run(main())
