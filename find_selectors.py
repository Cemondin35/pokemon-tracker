"""
Selector Bulucu — Yeni bir site eklerken doğru CSS selector'ları bulmak için.
Kullanım: python find_selectors.py https://siteadi.com/collections/pokemon
"""

import sys
import httpx
from bs4 import BeautifulSoup

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    )
}

COMMON_PRODUCT_SELECTORS = [
    ".product-item", ".product-card", ".product-card-wrapper",
    ".grid-product", ".grid__item", ".product-thumbnail",
    ".collection-product-card", ".ProductItem", ".product",
    "[data-product-id]", ".boost-pfs-filter-product-item",
]

COMMON_NAME_SELECTORS = [
    ".product-item__title", ".card__heading", ".product-card__title",
    ".grid-product__title", ".ProductItem__Title", ".product-title",
    "h2", "h3", ".product__title",
]

COMMON_PRICE_SELECTORS = [
    ".price-item--regular", ".price__regular .price-item",
    ".product-price", ".price", ".ProductItem__Price",
    ".grid-product__price",
]


def try_selectors(soup, selectors: list, label: str):
    for sel in selectors:
        els = soup.select(sel)
        if els:
            sample = els[0].get_text(strip=True)[:80]
            print(f"  ✅ {label}: '{sel}'  →  örnek: '{sample}'  ({len(els)} adet)")
            return sel
    print(f"  ❌ {label}: hiçbiri çalışmadı")
    return None


def main():
    if len(sys.argv) < 2:
        print("Kullanım: python find_selectors.py <URL>")
        sys.exit(1)

    url = sys.argv[1]
    print(f"\n🔍 Taranan URL: {url}\n")

    try:
        r = httpx.get(url, headers=HEADERS, timeout=20, follow_redirects=True)
        r.raise_for_status()
    except Exception as e:
        print(f"❌ Bağlanamadı: {e}")
        sys.exit(1)

    soup = BeautifulSoup(r.text, "html.parser")

    print("── Ürün Kartı Selector ──────────────────────")
    product_sel = try_selectors(soup, COMMON_PRODUCT_SELECTORS, "product_selector")

    if product_sel:
        first_card = soup.select_one(product_sel)
        print("\n── İsim Selector (kart içinde) ─────────────")
        try_selectors(first_card, COMMON_NAME_SELECTORS, "name_selector")

        print("\n── Fiyat Selector (kart içinde) ────────────")
        try_selectors(first_card, COMMON_PRICE_SELECTORS, "price_selector")

        # Link
        link = first_card.select_one("a")
        if link:
            print(f"\n  ✅ link_selector: 'a'  →  href: {link.get('href', '')[:80]}")

        # Stok göstergesi
        card_text = first_card.get_text()
        for keyword in ["Sold out", "Out of Stock", "Unavailable", "Add to cart"]:
            if keyword.lower() in card_text.lower():
                print(f"\n  📦 Stok keyword tespiti: '{keyword}' — soldout_text olarak kullan")
                break

    print("\n─────────────────────────────────────────────")
    print("Bu bilgileri config.json'daki ilgili siteye ekle.\n")


if __name__ == "__main__":
    main()
