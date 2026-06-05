import re
import json
import time
import random
import requests
from bs4 import BeautifulSoup
import warnings
warnings.filterwarnings("ignore")

try:
    from seleniumbase import SB
    SELENIUM_AVAILABLE = True
except ImportError:
    SELENIUM_AVAILABLE = False


def _normalize_url(url: str) -> str:
    url = re.sub(r'https?://[a-z]{2}\.aliexpress\.com', 'https://www.aliexpress.com', url)
    match = re.search(r'/item/(\d+)', url)
    if match:
        return f"https://www.aliexpress.com/item/{match.group(1)}.html"
    return url


def _clean_img(url: str) -> str:
    if not url:
        return ""
    if not url.startswith("http"):
        url = "https:" + url
    url = re.sub(r'_\d+x\d+.*?\.(jpg|jpeg|png|webp)', r'.\1', url, flags=re.I)
    return url


def _fetch_description(desc_url: str) -> str:
    """Fetch description HTML from AliExpress description URL."""
    try:
        headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}
        r = requests.get(desc_url, headers=headers, timeout=10)
        if r.ok:
            return r.text
    except Exception:
        pass
    return ""


def scrape_product(url: str) -> dict:
    url = _normalize_url(url)
    if not SELENIUM_AVAILABLE:
        return {"status": "error", "message": "נדרש: pip install seleniumbase", "source_url": url}
    return _scrape_with_selenium(url)


def _scrape_with_selenium(url: str) -> dict:
    try:
        with SB(uc=True, headless=True, locale_code="en") as sb:
            sb.open(url)
            sb.sleep(random.uniform(2, 4))
            # Try to bypass any interstitial
            try:
                sb.click("button[data-role='close']", timeout=2)
            except Exception:
                pass
            html = sb.get_page_source()

        result = (
            _try_data_from_script(html, url)
            or _try_next_data(html, url)
            or _try_regex_extraction(html, url)
            or _try_html_fallback(html, url)
        )
        return result
    except Exception as e:
        return {"status": "error", "message": str(e), "source_url": url}


def _try_next_data(html: str, url: str) -> dict | None:
    """Extract from Next.js __NEXT_DATA__ (newer AliExpress pages)."""
    m = re.search(r'<script[^>]+id=["\']__NEXT_DATA__["\'][^>]*>(.+?)</script>', html, re.DOTALL)
    if not m:
        return None
    try:
        data = json.loads(m.group(1))
    except Exception:
        return None

    def find_deep(obj, *keys):
        for key in keys:
            if isinstance(obj, dict):
                obj = obj.get(key, {})
            else:
                return {}
        return obj or {}

    props = find_deep(data, "props", "pageProps")
    product = (
        find_deep(props, "initialData", "data", "productInfoComponent")
        or find_deep(props, "data", "productInfoComponent")
        or find_deep(props, "productInfo")
        or props
    )

    title = (product.get("subject") or product.get("title") or
             find_deep(product, "titleModule", "subject") or "")
    if not title or len(title) < 5:
        return None

    images = []
    img_list = (
        find_deep(product, "imageModule", "imagePathList")
        or find_deep(props, "initialData", "data", "imageModule", "imagePathList")
        or []
    )
    if isinstance(img_list, list):
        images = [_clean_img(i) for i in img_list if i]

    price_mod = find_deep(product, "priceModule")
    price_str = price_mod.get("formatedActivityPrice") or price_mod.get("formatedPrice", "0")
    price = re.sub(r"[^\d.]", "", price_str) or "0"

    variants = []
    sku_props = find_deep(product, "skuModule", "productSKUPropertyList")
    if isinstance(sku_props, list):
        for prop in sku_props:
            name = prop.get("skuPropertyName", "Option")
            values = [v.get("propertyValueDisplayName", "") for v in prop.get("skuPropertyValues", [])]
            if values:
                variants.append({"name": name, "values": values})

    desc_mod = find_deep(product, "descriptionModule")
    description = desc_mod.get("description", "")
    if not description:
        desc_url = desc_mod.get("descriptionUrl", "")
        if desc_url:
            description = _fetch_description(desc_url)

    return {
        "title": title, "description": description, "price": price,
        "images": [i for i in images if i][:20],
        "variants": variants, "source_url": url, "status": "ok",
    }


def _try_data_from_script(html: str, url: str) -> dict | None:
    patterns = [
        r'window\.runParams\s*=\s*(\{.+?\});\s*(?:window|var )',
        r'window\["runParams"\]\s*=\s*(\{.+?\})\s*;',
    ]
    for pat in patterns:
        m = re.search(pat, html, re.DOTALL)
        if not m:
            continue
        try:
            data = json.loads(m.group(1))
        except Exception:
            continue

        components = (
            data.get("data", {}).get("pageComponent")
            or data.get("pageComponent")
            or {}
        )

        title = components.get("titleModule", {}).get("subject", "")
        if not title:
            continue

        price_mod = components.get("priceModule", {})
        price_str = price_mod.get("formatedActivityPrice") or price_mod.get("formatedPrice", "0")
        price = re.sub(r"[^\d.]", "", price_str) or "0"

        images = [_clean_img(i) for i in components.get("imageModule", {}).get("imagePathList", [])]

        variants = []
        for prop in components.get("skuModule", {}).get("productSKUPropertyList", []):
            name = prop.get("skuPropertyName", "Option")
            values = [v.get("propertyValueDisplayName", "") for v in prop.get("skuPropertyValues", [])]
            if values:
                variants.append({"name": name, "values": values})

        # Try inline description first, then fetch from URL
        desc_mod = components.get("descriptionModule", {})
        description = desc_mod.get("description", "")
        if not description:
            desc_url = desc_mod.get("descriptionUrl", "")
            if desc_url:
                description = _fetch_description(desc_url)

        return {
            "title": title, "description": description, "price": price,
            "images": [i for i in images if i][:20],
            "variants": variants, "source_url": url, "status": "ok",
        }
    return None


def _try_regex_extraction(html: str, url: str) -> dict | None:
    title = ""
    for key in ["subject", "productName"]:
        m = re.search(rf'"{key}"\s*:\s*"([^"{{}}]{{10,}})"', html)
        if m:
            title = m.group(1)
            break

    if not title:
        return None

    price = "0"
    for pat in [r'"formatedActivityPrice"\s*:\s*"([^"]+)"', r'"formatedPrice"\s*:\s*"([^"]+)"']:
        p = re.search(pat, html)
        if p:
            price = re.sub(r"[^\d.]", "", p.group(1)) or "0"
            break

    images = []
    img_match = re.search(r'"imagePathList"\s*:\s*(\[[^\]]+\])', html)
    if img_match:
        try:
            images = [_clean_img(i) for i in json.loads(img_match.group(1)) if i][:20]
        except Exception:
            pass

    variants = []
    sku_match = re.search(r'"productSKUPropertyList"\s*:\s*(\[.+?\])\s*,\s*"', html, re.DOTALL)
    if sku_match:
        try:
            for prop in json.loads(sku_match.group(1)):
                name = prop.get("skuPropertyName", "Option")
                values = [v.get("propertyValueDisplayName", "") for v in prop.get("skuPropertyValues", [])]
                if values:
                    variants.append({"name": name, "values": values})
        except Exception:
            pass

    # Try to fetch description from descriptionUrl
    description = ""
    desc_url_match = re.search(r'"descriptionUrl"\s*:\s*"([^"]+)"', html)
    if desc_url_match:
        description = _fetch_description(desc_url_match.group(1))

    return {
        "title": title, "description": description, "price": price,
        "images": images, "variants": variants,
        "source_url": url, "status": "ok",
    }


def _try_html_fallback(html: str, url: str) -> dict:
    soup = BeautifulSoup(html, "html5lib")
    title_tag = soup.find("h1")
    title = title_tag.get_text(strip=True) if title_tag else "Unknown Product"

    price = "0"
    for cls in [re.compile(r"product-price", re.I), re.compile(r"price", re.I)]:
        tag = soup.find(class_=cls)
        if tag:
            m = re.search(r"[\d.]+", tag.get_text())
            if m:
                price = m.group()
                break

    # First try: find imagePathList in any script tag (most reliable)
    images = []
    for script in soup.find_all("script"):
        text = script.string or ""
        img_m = re.search(r'"imagePathList"\s*:\s*(\[[^\]]{20,}\])', text)
        if img_m:
            try:
                imgs = json.loads(img_m.group(1))
                images = [_clean_img(i) for i in imgs if i][:20]
                if images:
                    break
            except Exception:
                pass

    # Second try: look for large alicdn images in HTML (skip small icons/swatches)
    if not images:
        seen = set()
        for img in soup.find_all("img"):
            src = img.get("src") or img.get("data-src") or ""
            if not src or ("alicdn" not in src and "ae0" not in src):
                continue
            size_m = re.search(r'_(\d+)x(\d+)', src)
            if size_m:
                w, h = int(size_m.group(1)), int(size_m.group(2))
                if w < 300 or h < 300:
                    continue
            clean = _clean_img(src)
            if clean and clean not in seen:
                seen.add(clean)
                images.append(clean)
        images = images[:20]

    return {
        "title": title, "description": "", "price": price,
        "images": images, "variants": [],
        "source_url": url, "status": "ok",
    }


def search_products(keyword: str, page: int = 1) -> list[dict]:
    if not SELENIUM_AVAILABLE:
        return []

    url = f"https://www.aliexpress.com/wholesale?SearchText={requests.utils.quote(keyword)}&page={page}"
    try:
        with SB(uc=True, headless=True, locale_code="en") as sb:
            sb.open(url)
            sb.sleep(random.uniform(3, 5))
            html = sb.get_page_source()

        results = _parse_search_json(html)
        if results:
            return results

        soup = BeautifulSoup(html, "html5lib")
        return _parse_search_html(soup)
    except Exception:
        return []


def _parse_search_json(html: str) -> list[dict]:
    m = re.search(r'"itemList"\s*:\s*\{"content"\s*:\s*(\[.+?\])\s*[,}]', html, re.DOTALL)
    if not m:
        return []
    try:
        items = json.loads(m.group(1))
        results = []
        for item in items:
            img = item.get("image", {}).get("imgUrl", "")
            title = item.get("title", {}).get("displayTitle", "")
            if not title:
                continue
            results.append({
                "title": title,
                "price": item.get("prices", {}).get("salePrice", {}).get("formattedPrice", ""),
                "image": _clean_img(img),
                "url": "https:" + item.get("productDetailUrl", ""),
            })
        return results
    except Exception:
        return []


def _parse_search_html(soup: BeautifulSoup) -> list[dict]:
    results = []
    seen = set()
    for card in soup.select("a[href*='/item/']"):
        href = card.get("href", "")
        if not href or href in seen:
            continue
        seen.add(href)
        title_el = card.find(["h3", "span"], class_=re.compile(r"title|name", re.I))
        img_el = card.find("img")
        price_el = card.find(class_=re.compile(r"price", re.I))
        title = title_el.get_text(strip=True) if title_el else ""
        if not title or len(title) < 5:
            continue
        if not href.startswith("http"):
            href = "https://www.aliexpress.com" + href
        results.append({
            "title": title,
            "price": price_el.get_text(strip=True) if price_el else "",
            "image": _clean_img(img_el.get("src") or "") if img_el else "",
            "url": href,
        })
        if len(results) >= 20:
            break
    return results
