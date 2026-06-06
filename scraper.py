import re
import json
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


def _clean_img(url) -> str:
    if not url:
        return ""
    if isinstance(url, dict):
        url = url.get("imageUrl") or url.get("url") or url.get("src") or ""
    url = str(url).strip()
    if not url:
        return ""
    if url.startswith("//"):
        url = "https:" + url
    elif not url.startswith("http"):
        url = "https://" + url
    url = re.sub(r'_(\d+x\d+)[^/]*\.(jpe?g|png|webp)', r'.\2', url, flags=re.I)
    url = re.sub(r'_Q\d+\.(jpe?g|png|webp)', r'.\1', url, flags=re.I)
    return url


def _fetch_description(desc_url: str) -> str:
    """Fetch description HTML, fixing protocol-relative image URLs."""
    try:
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
            "Referer": "https://www.aliexpress.com/",
        }
        r = requests.get(desc_url, headers=headers, timeout=15)
        if not r.ok:
            return ""
        html = r.text
        # Reject 404/error pages
        if any(x in html for x in ['page-not-found', '404 page', "can't find that page",
                                     'Sorry, we can', 'error page']):
            return ""
        html = re.sub(r'src=["\']\/\/', 'src="https://', html)
        html = re.sub(r'<script[^>]*>.*?</script>', '', html, flags=re.DOTALL | re.I)
        return html
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
            sb.sleep(random.uniform(3, 5))
            try:
                sb.click("button[data-role='close']", timeout=2)
            except Exception:
                pass

            sb.execute_script("window.scrollTo(0, 800)")
            sb.sleep(1)
            sb.execute_script("window.scrollTo(0, document.body.scrollHeight)")
            sb.sleep(2)
            sb.execute_script("window.scrollTo(0, 0)")

            result = _try_js_extraction(sb, url)
            if result and result.get("title"):
                return result

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


_JS_EXTRACTION = """
(function() {
    try {
        var result = {title:'',price:'0',images:[],variants:[],descUrl:'',desc:''};

        // ── 0. Check _dida_config_ for description URL ────────────────────
        try {
            var dc = window._dida_config_ || {};
            var dcDesc = (dc.descUrl || dc.descriptionUrl || '');
            if (!dcDesc && dc.pageConfig) dcDesc = dc.pageConfig.descUrl || dc.pageConfig.descriptionUrl || '';
            if (dcDesc) result.descUrl = dcDesc.replace(/\\\//g, '/');
        } catch(e0) {}

        // ── 1. window.runParams (classic AliExpress) ──────────────────────
        var rp = window.runParams || {};
        var rpd = rp.data || {};
        var c = rpd.pageComponent || rpd.productComponent || rpd.itemInfoComponent
             || rp.pageComponent || rp.productComponent || {};

        result.title = (c.titleModule||{}).subject || (c.titleComponent||{}).subject
                    || rpd.subject || c.subject || '';

        var pm = c.priceModule || c.priceComponent || rpd.priceModule || {};
        result.price = pm.formatedActivityPrice || pm.formatedPrice
                    || pm.minActivityAmount || pm.minAmount || '0';

        var im = c.imageModule || c.imageComponent || rpd.imageModule || {};
        result.images = (im.imagePathList || []).slice(0, 20);

        var sm = c.skuModule || c.skuComponent || rpd.skuModule || rpd.skuComponent || {};
        var skuList = sm.productSKUPropertyList || sm.skuPropertyList || sm.properties || [];
        if (!skuList.length) {
            var found = null;
            function dig(obj, depth) {
                if (!obj || depth > 4 || typeof obj !== 'object') return;
                if (Array.isArray(obj.productSKUPropertyList) && obj.productSKUPropertyList.length) {
                    found = obj.productSKUPropertyList; return;
                }
                for (var k in obj) { if (!found) dig(obj[k], depth + 1); }
            }
            dig(rp, 0);
            if (found) skuList = found;
        }
        result.variants = skuList.map(function(p) {
            var vals = p.skuPropertyValues || p.values || p.propertyValues || [];
            return {
                n: p.skuPropertyName || p.name || p.propertyName || 'Option',
                v: vals.map(function(v) {
                    return v.propertyValueDisplayName || v.displayName || v.propertyValueName || v.name || '';
                }).filter(Boolean)
            };
        }).filter(function(x) { return x.v.length > 0; });

        var dm = c.descriptionModule || c.descriptionComponent || rpd.descriptionModule || {};
        result.descUrl = dm.descriptionUrl || '';
        result.desc = dm.description || '';

        if (result.title) return JSON.stringify(result);

        // ── 2. __NEXT_DATA__ ──────────────────────────────────────────────
        var nd = document.getElementById('__NEXT_DATA__');
        if (nd) return JSON.stringify({_nd: nd.textContent.substring(0, 100000)});

        // ── 3. DOM-based extraction (new AliExpress React pages) ──────────
        var h1el = document.querySelector('h1');
        result.title = h1el ? h1el.textContent.trim() : '';

        // Price + images from inline script tags
        var scripts = document.querySelectorAll('script');
        for (var si = 0; si < scripts.length; si++) {
            var st = scripts[si].textContent || '';
            if (!result.images.length && st.indexOf('imagePathList') > -1) {
                var im2 = st.match(/"imagePathList"\\s*:\\s*(\\[[^\\]]+\\])/);
                if (im2) { try { result.images = JSON.parse(im2[1]).slice(0, 20); } catch(e2) {} }
            }
            if (result.price === '0') {
                var pm2 = st.match(/"price"\\s*:\\s*"([\\d.]+)"/)
                       || st.match(/"salePrice"\\s*:\\s*"([\\d.]+)"/);
                if (pm2) result.price = pm2[1];
            }
            // descriptionUrl: use indexOf to avoid regex escaping issues
            if (!result.descUrl && st.indexOf('descriptionUrl') > -1) {
                var duIdx = st.indexOf('"descriptionUrl"');
                if (duIdx > -1) {
                    var duAfter = st.substring(duIdx + 17, duIdx + 317);
                    var duQ1 = duAfter.indexOf('"');
                    if (duQ1 > -1) {
                        var duRaw = duAfter.substring(duQ1 + 1, duAfter.indexOf('"', duQ1 + 1));
                        result.descUrl = duRaw.replace(/\\\//g, '/');
                    }
                }
            }
            // aeproductsourcesite URL (new AliExpress description endpoint)
            if (!result.descUrl && st.indexOf('aeproductsourcesite') > -1) {
                var aeIdx = st.indexOf('aeproductsourcesite');
                // Find the full URL around it
                var aeStart = aeIdx;
                while (aeStart > 0 && st[aeStart] !== '"' && st[aeStart] !== "'") aeStart--;
                var aeEnd = aeIdx;
                while (aeEnd < st.length && st[aeEnd] !== '"' && st[aeEnd] !== "'" && st[aeEnd] !== ' ') aeEnd++;
                var aeUrl = st.substring(aeStart + 1, aeEnd).replace(/\\\//g, '/');
                if (aeUrl.indexOf('http') === 0 || aeUrl.indexOf('//') === 0) {
                    result.descUrl = aeUrl.startsWith('//') ? 'https:' + aeUrl : aeUrl;
                }
            }
            if (result.images.length && result.price !== '0' && result.descUrl) break;
        }

        // Description: try lazy iframes
        if (!result.descUrl) {
            var frames = document.querySelectorAll('iframe');
            for (var fi = 0; fi < frames.length; fi++) {
                var fsrc = frames[fi].src || frames[fi].getAttribute('data-src') || '';
                if (fsrc && fsrc.length > 20
                    && (fsrc.indexOf('desc') > -1 || fsrc.indexOf('alicdn') > -1
                        || fsrc.indexOf('ae01') > -1 || fsrc.indexOf('ae02') > -1)) {
                    result.descUrl = fsrc; break;
                }
            }
        }
        // Description: look for iframe in description-related containers
        if (!result.descUrl) {
            var descDivs = document.querySelectorAll(
                '[class*="description"] iframe, [class*="detail-desc"] iframe, [id*="desc"] iframe'
            );
            if (descDivs.length) {
                var ds = descDivs[0].src || descDivs[0].getAttribute('data-src') || '';
                if (ds) result.descUrl = ds;
            }
        }
        // Description: try inline DOM content (new AliExpress renders desc inline)
        if (!result.desc) {
            var descContainers = document.querySelectorAll(
                '[class*="desc-content"], [class*="description-content"], ' +
                '[class*="product-description"], [class*="detail-desc-content"], ' +
                '[class*="pdp-comp-product-description"], [id*="product-description"]'
            );
            for (var dci = 0; dci < descContainers.length; dci++) {
                var dcHtml = descContainers[dci].innerHTML || '';
                if (dcHtml.length > 200) { result.desc = dcHtml.substring(0, 60000); break; }
            }
        }

        // Variants: parse span texts using "NAME: VALUE" label pattern
        // This is the most reliable approach for new AliExpress React pages
        if (!result.variants.length) {
            var allSkuSpans = document.querySelectorAll('[class*="sku"] span');
            var curGrpName = null, curGrpVals = [];
            for (var osi = 0; osi < allSkuSpans.length; osi++) {
                var otxt = allSkuSpans[osi].textContent.trim();
                if (!otxt || otxt.length > 80) continue;
                // Stop at combined selection display (e.g. "Color: Black, Size: 36")
                if (otxt.indexOf(',') > -1 && otxt.indexOf(':') > -1) break;
                // Detect label: "NAME: currentValue" — colon followed by non-breaking space
                var colonIdx = otxt.indexOf(':');
                var nbspIdx = otxt.indexOf(' ');
                if (colonIdx > 0 && nbspIdx === colonIdx + 1) {
                    // Save previous group
                    if (curGrpName && curGrpVals.length > 0) {
                        result.variants.push({n: curGrpName, v: curGrpVals});
                    }
                    curGrpName = otxt.substring(0, colonIdx).trim();
                    curGrpVals = [];
                } else if (curGrpName && colonIdx === -1 && otxt.length <= 30) {
                    // Plain text with no colon = an option value
                    if (curGrpVals.indexOf(otxt) === -1) curGrpVals.push(otxt);
                }
            }
            if (curGrpName && curGrpVals.length > 0) {
                result.variants.push({n: curGrpName, v: curGrpVals});
            }
        }

        if (result.title) return JSON.stringify(result);
        return null;
    } catch(e) { return null; }
})();
"""


def _try_js_extraction(sb, url: str) -> dict | None:
    try:
        raw = sb.execute_script(_JS_EXTRACTION)

        if not raw:
            return None

        data = json.loads(raw)

        if "_nd" in data:
            return _parse_next_raw(data["_nd"], url)

        title = data.get("title", "")
        if not title:
            return None

        price_str = data.get("price", "0")
        price = re.sub(r"[^\d.]", "", price_str) or "0"

        images = [_clean_img(i) for i in data.get("images", []) if i]

        variants = []
        for item in data.get("variants", []):
            name = item.get("n", "Option")
            values = [v for v in item.get("v", []) if v]
            if values:
                variants.append({"name": name, "values": values})

        description = data.get("desc", "")
        desc_url = data.get("descUrl", "")
        if not description and desc_url:
            description = _fetch_description(desc_url)

        return {
            "title": title,
            "description": description,
            "price": price,
            "_desc_url": desc_url,  # debug
            "images": [i for i in images if i][:20],
            "variants": variants,
            "source_url": url,
            "status": "ok",
        }

    except Exception:
        pass
    return None


def _parse_runparams_comp(comp: dict, url: str) -> dict | None:
    title = comp.get("titleModule", {}).get("subject", "")
    if not title:
        return None

    price_mod = comp.get("priceModule", {})
    price_str = (price_mod.get("formatedActivityPrice")
                 or price_mod.get("formatedPrice", "0"))
    price = re.sub(r"[^\d.]", "", price_str) or "0"

    images = [_clean_img(i) for i in
              comp.get("imageModule", {}).get("imagePathList", [])]

    for prop in comp.get("skuModule", {}).get("productSKUPropertyList", []):
        for val in prop.get("skuPropertyValues", []):
            img = val.get("skuPropertyImagePath", "")
            if img:
                c = _clean_img(img)
                if c and c not in images:
                    images.append(c)

    variants = []
    for prop in comp.get("skuModule", {}).get("productSKUPropertyList", []):
        name = prop.get("skuPropertyName", "Option")
        values = [v.get("propertyValueDisplayName", "")
                  for v in prop.get("skuPropertyValues", [])]
        if values:
            variants.append({"name": name, "values": values})

    desc_mod = comp.get("descriptionModule", {})
    description = desc_mod.get("description", "")
    if not description:
        desc_url = desc_mod.get("descriptionUrl", "")
        if desc_url:
            description = _fetch_description(desc_url)

    return {
        "title": title,
        "description": description,
        "price": price,
        "images": [i for i in images if i][:20],
        "variants": variants,
        "source_url": url,
        "status": "ok",
    }


def _parse_next_raw(raw_text: str, url: str) -> dict | None:
    try:
        data = json.loads(raw_text)
    except Exception:
        return None

    def dig(obj, *keys):
        for k in keys:
            if not isinstance(obj, dict):
                return {}
            obj = obj.get(k, {})
        return obj or {}

    props = dig(data, "props", "pageProps")
    comp = (
        dig(props, "initialData", "data", "productInfoComponent")
        or dig(props, "data", "productInfoComponent")
        or {}
    )

    title = (comp.get("subject") or comp.get("title")
             or dig(comp, "titleModule", "subject") or "")
    if not title or len(title) < 5:
        return None

    price_mod = dig(comp, "priceModule")
    price_str = (price_mod.get("formatedActivityPrice")
                 or price_mod.get("formatedPrice", "0"))
    price = re.sub(r"[^\d.]", "", price_str) or "0"

    img_list = (
        dig(comp, "imageModule", "imagePathList")
        or dig(props, "initialData", "data", "imageModule", "imagePathList")
        or []
    )
    images = [_clean_img(i) for i in img_list if i]

    variants = []
    sku_props = dig(comp, "skuModule", "productSKUPropertyList")
    if isinstance(sku_props, list):
        for prop in sku_props:
            name = prop.get("skuPropertyName", "Option")
            values = [v.get("propertyValueDisplayName", "")
                      for v in prop.get("skuPropertyValues", [])]
            if values:
                variants.append({"name": name, "values": values})

    desc_mod = dig(comp, "descriptionModule")
    description = desc_mod.get("description", "")
    if not description:
        desc_url = desc_mod.get("descriptionUrl", "")
        if desc_url:
            description = _fetch_description(desc_url)

    return {
        "title": title,
        "description": description,
        "price": price,
        "images": [i for i in images if i][:20],
        "variants": variants,
        "source_url": url,
        "status": "ok",
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

        comp = (
            data.get("data", {}).get("pageComponent")
            or data.get("pageComponent")
            or {}
        )
        if comp.get("titleModule", {}).get("subject"):
            return _parse_runparams_comp(comp, url)
    return None


def _try_next_data(html: str, url: str) -> dict | None:
    m = re.search(r'<script[^>]+id=["\']__NEXT_DATA__["\'][^>]*>(.+?)</script>',
                  html, re.DOTALL)
    if not m:
        return None
    return _parse_next_raw(m.group(1), url)


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
    for pat in [r'"formatedActivityPrice"\s*:\s*"([^"]+)"',
                r'"formatedPrice"\s*:\s*"([^"]+)"',
                r'"salePrice"\s*:\s*"([\d.]+)"',
                r'"price"\s*:\s*"([\d.]+)"']:
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
    sku_match = re.search(r'"productSKUPropertyList"\s*:\s*(\[.+?\])\s*,\s*"',
                          html, re.DOTALL)
    if sku_match:
        try:
            for prop in json.loads(sku_match.group(1)):
                name = prop.get("skuPropertyName", "Option")
                values = [v.get("propertyValueDisplayName", "")
                          for v in prop.get("skuPropertyValues", [])]
                if values:
                    variants.append({"name": name, "values": values})
        except Exception:
            pass

    description = ""
    desc_m = re.search(r'"descriptionUrl"\s*:\s*"([^"]+)"', html)
    if desc_m:
        description = _fetch_description(desc_m.group(1).replace('\/', '/'))

    return {
        "title": title,
        "description": description,
        "price": price,
        "images": images,
        "variants": variants,
        "source_url": url,
        "status": "ok",
    }


def _try_html_fallback(html: str, url: str) -> dict:
    soup = BeautifulSoup(html, "html5lib")
    title_tag = soup.find("h1")
    title = title_tag.get_text(strip=True) if title_tag else "Unknown Product"

    price = "0"
    for pat in [r'"price"\s*:\s*"([\d.]+)"', r'"salePrice"\s*:\s*"([\d.]+)"',
                r'"formatedPrice"\s*:\s*"([^"]+)"']:
        pm = re.search(pat, html)
        if pm:
            price = re.sub(r"[^\d.]", "", pm.group(1)) or "0"
            break

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

    if not images:
        seen = set()
        for img in soup.find_all("img"):
            src = img.get("src") or img.get("data-src") or ""
            if not src or "alicdn" not in src:
                continue
            size_m = re.search(r'_(\d+)x(\d+)', src)
            if size_m and (int(size_m.group(1)) < 300 or int(size_m.group(2)) < 300):
                continue
            clean = _clean_img(src)
            if clean and clean not in seen:
                seen.add(clean)
                images.append(clean)
        images = images[:20]

    return {
        "title": title,
        "description": "",
        "price": price,
        "images": images,
        "variants": [],
        "source_url": url,
        "status": "ok",
    }


def search_products(keyword: str, page: int = 1) -> list[dict]:
    if not SELENIUM_AVAILABLE:
        return []

    url = (f"https://www.aliexpress.com/wholesale"
           f"?SearchText={requests.utils.quote(keyword)}&page={page}")
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
    m = re.search(r'"itemList"\s*:\s*\{"content"\s*:\s*(\[.+?\])\s*[,}]',
                  html, re.DOTALL)
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
