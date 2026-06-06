import csv
import io
import json
import zipfile
from urllib.parse import urlparse

import requests as req_lib
from flask import Flask, render_template, request, jsonify, Response, stream_with_context

from scraper import scrape_product, search_products
import shopify_api

app = Flask(__name__)


# ---------------------------------------------------------------------------
# Pages
# ---------------------------------------------------------------------------

@app.route("/")
def index():
    return render_template("index.html")


# ---------------------------------------------------------------------------
# Status / config
# ---------------------------------------------------------------------------

@app.route("/api/status")
def status():
    configured = shopify_api.is_configured()
    collections = []
    if configured:
        try:
            collections = shopify_api.get_collections()
        except Exception:
            pass
    return jsonify({"configured": configured, "collections": collections})


# ---------------------------------------------------------------------------
# Scraping
# ---------------------------------------------------------------------------

@app.route("/api/scrape", methods=["POST"])
def scrape():
    data = request.json or {}
    url = data.get("url", "").strip()
    if not url:
        return jsonify({"error": "URL is required"}), 400
    if "aliexpress.com" not in url:
        return jsonify({"error": "Please provide an AliExpress product URL"}), 400
    try:
        product = scrape_product(url)
        return jsonify(product)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ---------------------------------------------------------------------------
# Search
# ---------------------------------------------------------------------------

@app.route("/api/search", methods=["POST"])
def search():
    data = request.json or {}
    keyword = data.get("keyword", "").strip()
    page = int(data.get("page", 1))
    if not keyword:
        return jsonify({"error": "Keyword is required"}), 400
    try:
        results = search_products(keyword, page)
        return jsonify({"results": results})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ---------------------------------------------------------------------------
# CSV bulk import
# ---------------------------------------------------------------------------

@app.route("/api/import-csv", methods=["POST"])
def import_csv():
    file = request.files.get("file")
    if not file:
        return jsonify({"error": "No file uploaded"}), 400

    content = file.read().decode("utf-8-sig")
    reader = csv.DictReader(io.StringIO(content))
    urls = [row.get("url") or row.get("URL") or row.get("link") or "" for row in reader]
    urls = [u.strip() for u in urls if u.strip() and "aliexpress.com" in u]

    if not urls:
        return jsonify({"error": "No valid AliExpress URLs found in CSV. Column must be named 'url' or 'URL'."}), 400

    results = []
    for url in urls[:50]:
        try:
            product = scrape_product(url)
            results.append(product)
        except Exception as e:
            results.append({"status": "error", "source_url": url, "message": str(e)})

    return jsonify({"products": results})


# ---------------------------------------------------------------------------
# Shopify import
# ---------------------------------------------------------------------------

@app.route("/api/import-to-shopify", methods=["POST"])
def import_to_shopify():
    if not shopify_api.is_configured():
        return jsonify({
            "error": "Shopify is not configured. Set SHOPIFY_STORE_URL and SHOPIFY_ACCESS_TOKEN in .env"
        }), 400

    data = request.json or {}
    products = data.get("products", [])
    if not products:
        return jsonify({"error": "No products provided"}), 400

    results = []
    for product in products:
        try:
            created = shopify_api.import_product(product)
            results.append({
                "status": "ok",
                "title": created["title"],
                "id": created["id"],
                "admin_url": f"{shopify_api.STORE_URL}/admin/products/{created['id']}",
            })
        except Exception as e:
            results.append({
                "status": "error",
                "title": product.get("title", "?"),
                "message": str(e),
            })

    return jsonify({"results": results})


# ---------------------------------------------------------------------------
# Image proxy  –  GET /api/proxy-image?url=<encoded>
# ---------------------------------------------------------------------------

@app.route("/api/proxy-image")
def proxy_image():
    """
    Proxy an AliExpress CDN image through Flask so the browser avoids CORS
    and hot-linking blocks.
    """
    image_url = request.args.get("url", "").strip()
    if not image_url:
        return jsonify({"error": "url parameter is required"}), 400

    # Allow-list: only proxy known alicdn / aliexpress domains
    allowed_hosts = (
        "alicdn.com",
        "aliexpress.com",
        "ae01.alicdn.com",
        "ae02.alicdn.com",
        "ae03.alicdn.com",
        "ae04.alicdn.com",
    )
    parsed = urlparse(image_url)
    host = parsed.netloc.lower()
    if not any(host.endswith(h) for h in allowed_hosts):
        return jsonify({"error": "Domain not allowed"}), 403

    try:
        headers = {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            ),
            "Referer": "https://www.aliexpress.com/",
            "Accept": "image/avif,image/webp,image/apng,image/*,*/*;q=0.8",
        }
        upstream = req_lib.get(image_url, headers=headers, stream=True, timeout=15)
        upstream.raise_for_status()

        content_type = upstream.headers.get("Content-Type", "image/jpeg")

        def generate():
            for chunk in upstream.iter_content(chunk_size=8192):
                if chunk:
                    yield chunk

        return Response(
            stream_with_context(generate()),
            content_type=content_type,
            headers={
                "Cache-Control": "public, max-age=86400",
                "Access-Control-Allow-Origin": "*",
            },
        )
    except req_lib.exceptions.RequestException as e:
        return jsonify({"error": str(e)}), 502


# ---------------------------------------------------------------------------
# Bulk image download  –  POST /api/download-images
# Body: { "images": [...], "title": "..." }
# Returns: ZIP file
# ---------------------------------------------------------------------------

@app.route("/api/download-images", methods=["POST"])
def download_images():
    """
    Download all provided image URLs and return them as a single ZIP archive.
    """
    data = request.json or {}
    images = data.get("images", [])
    title = data.get("title", "product").strip() or "product"

    if not images:
        return jsonify({"error": "No images provided"}), 400

    safe_title = "".join(
        c if c.isalnum() or c in " -_" else "_" for c in title
    )[:60].strip()

    zip_buffer = io.BytesIO()
    dl_headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/124.0.0.0 Safari/537.36"
        ),
        "Referer": "https://www.aliexpress.com/",
    }

    with zipfile.ZipFile(zip_buffer, "w", zipfile.ZIP_DEFLATED) as zf:
        for idx, img_url in enumerate(images[:30], start=1):
            try:
                r = req_lib.get(img_url, headers=dl_headers, timeout=15)
                r.raise_for_status()
                ct = r.headers.get("Content-Type", "image/jpeg")
                if "png" in ct:
                    ext = "png"
                elif "webp" in ct:
                    ext = "webp"
                else:
                    ext = "jpg"
                filename = f"{safe_title}_{idx:02d}.{ext}"
                zf.writestr(filename, r.content)
            except Exception:
                continue  # skip failed images silently

    zip_buffer.seek(0)
    zip_filename = f"{safe_title}_images.zip"
    return Response(
        zip_buffer.read(),
        mimetype="application/zip",
        headers={
            "Content-Disposition": f'attachment; filename="{zip_filename}"',
            "Access-Control-Allow-Origin": "*",
        },
    )


# ---------------------------------------------------------------------------
# Debug endpoint — returns raw scraped dict so you can see what was extracted
# ---------------------------------------------------------------------------

@app.route("/api/debug-scrape", methods=["POST"])
def debug_scrape():
    data = request.json or {}
    url = data.get("url", "").strip()
    if not url:
        return jsonify({"error": "URL required"}), 400
    try:
        result = scrape_product(url)
        return jsonify(result)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/debug-desc-url")
def debug_desc_url():
    """Render the product page and return any description-related URLs found in the HTML."""
    try:
        from seleniumbase import SB
        import re as _re
        url = request.args.get("url", "")
        m = _re.search(r'/item/(\d+)', url)
        item_id = m.group(1) if m else ""
        norm = f"https://www.aliexpress.com/item/{item_id}.html" if item_id else url
        with SB(uc=True, headless=True, locale_code="en") as sb:
            sb.open(norm)
            sb.sleep(4)
            sb.set_window_size(1920, 1080)
            sb.execute_script("window.scrollTo(0, document.body.scrollHeight)")
            sb.sleep(3)
            sb.execute_script("window.scrollTo(0, Math.floor(document.body.scrollHeight*0.85))")
            sb.sleep(3)
            html = sb.get_page_source()

            # Find the description container and its lazy-loaded images in the DOM
            dom_info = sb.execute_script(r"""
                (function() {
                    var out = {containers: [], descImages: []};
                    var sels = [
                        '#product-description', '[id*="product-description"]',
                        '[class*="detail-desc-decorate"]', '[class*="description--wrap"]',
                        '[class*="product-description"]', '[class*="detailmodule_html"]',
                        '[class*="extend--description"]', '[class*="pdp-comp-product-description"]',
                        '[class*="ProductDescription"]'
                    ];
                    var best = null, bestImgs = 0;
                    for (var i = 0; i < sels.length; i++) {
                        var els = document.querySelectorAll(sels[i]);
                        for (var j = 0; j < els.length; j++) {
                            var imgs = els[j].querySelectorAll('img');
                            out.containers.push(sels[i] + ' -> imgs:' + imgs.length + ' htmlLen:' + (els[j].innerHTML||'').length);
                            if (imgs.length > bestImgs) { best = els[j]; bestImgs = imgs.length; }
                        }
                    }
                    if (best) {
                        var imgs = best.querySelectorAll('img');
                        for (var k = 0; k < imgs.length; k++) {
                            var s = imgs[k].src || imgs[k].getAttribute('data-src') || imgs[k].getAttribute('src') || '';
                            if (s && s.length > 10 && s.indexOf('data:') !== 0) out.descImages.push(s);
                        }
                    }
                    return JSON.stringify(out);
                })();
            """)

        import json as _json
        try:
            dom = _json.loads(dom_info) if dom_info else {}
        except Exception:
            dom = {"raw": str(dom_info)[:500]}

        found = {}
        found["containers"] = dom.get("containers", [])
        found["desc_image_count"] = len(dom.get("descImages", []))
        found["desc_images_sample"] = dom.get("descImages", [])[:8]
        found["desc_htm"] = list(set(_re.findall(
            r'https?:[\\/]*[^\s"\'<>]*desc\.htm[^\s"\'<>]*', html)))[:5]
        found["html_len"] = len(html)
        found["item_id"] = item_id
        return jsonify(found)
    except Exception as e:
        import traceback
        return jsonify({"error": str(e), "traceback": traceback.format_exc()})


@app.route("/api/debug-keys")
def debug_keys():
    try:
        from seleniumbase import SB
        import re, json as _j
        url = request.args.get("url", "")
        m = re.search(r'/item/(\d+)', url)
        url = f"https://www.aliexpress.com/item/{m.group(1)}.html" if m else url
        with SB(uc=True, headless=True, locale_code="en") as sb:
            sb.open(url)
            sb.sleep(3)
            sb.execute_script("window.scrollTo(0, 600)")
            sb.sleep(4)
            sb.execute_script("window.scrollTo(0, 1200)")
            sb.sleep(3)
            r = sb.execute_script("""
                (function() {
                    try {
                        var out = {};

                        // 1. window.runParams
                        var rp = window.runParams || {};
                        out.rpKeys = Object.keys(rp).slice(0,20);
                        if (rp.data) out.rpDataKeys = Object.keys(rp.data).slice(0,20);

                        // 2. Check window.AES (AliExpress data store)
                        out.foundGlobals = [];
                        if (window.AES) {
                            out.foundGlobals.push('AES');
                            try {
                                out.aesKeys = Object.keys(window.AES).slice(0,20);
                                // AES often has modules
                                if (window.AES.data) out.aesDataKeys = Object.keys(window.AES.data).slice(0,20);
                            } catch(e3){}
                        }
                        if (window.Hawe) {
                            out.foundGlobals.push('Hawe');
                            try { out.haweKeys = Object.keys(window.Hawe).slice(0,20); } catch(e4){}
                        }
                        // Check other known globals
                        var knownGlobals2 = ['__g_AEM_data','__INITIAL_DATA__','PAGE_INIT_DATA',
                            'GEP_CONFIG','AES_CONFIG','productData','itemData'];
                        for (var gi=0; gi<knownGlobals2.length; gi++) {
                            if (window[knownGlobals2[gi]]) out.foundGlobals.push(knownGlobals2[gi]);
                        }
                        // GEP_CONFIG might have product info
                        if (window.GEP_CONFIG) {
                            try { out.gepKeys = Object.keys(window.GEP_CONFIG).slice(0,20); } catch(e5){}
                        }

                        // 3. All window keys that look like data (objects, not functions/DOM)
                        out.windowDataKeys = Object.keys(window).filter(function(k){
                            try {
                                var v=window[k];
                                return v && typeof v==='object' && !Array.isArray(v)
                                    && !(v instanceof Element) && !(v instanceof Window)
                                    && Object.keys(v).length > 2 && k[0] !== '_' && k !== 'document';
                            } catch(e){return false;}
                        }).slice(0,30);

                        // 4. __NEXT_DATA__
                        var nd = document.getElementById('__NEXT_DATA__');
                        out.hasNextData = !!nd;
                        if (nd) {
                            try {
                                var ndP = JSON.parse(nd.textContent);
                                out.ndKeys = Object.keys(ndP).slice(0,10);
                                var pp = (ndP.props||{}).pageProps||{};
                                out.ppKeys = Object.keys(pp).slice(0,20);
                            } catch(e2){ out.ndErr = e2.message; }
                        }

                        // 5. Script tag scan
                        var scripts = document.querySelectorAll('script');
                        var fTitle='', fPrice='', fImgLen=0, fSkuLen=0, fDescUrl='';
                        for (var i=0; i<scripts.length; i++) {
                            var t = scripts[i].textContent||'';
                            if (!fTitle && t.indexOf('subject') > -1) {
                                var tm = t.match(/"subject"\s*:\s*"([^"]{5,200})"/);
                                if (tm) fTitle = tm[1];
                            }
                            if (!fPrice) {
                                var pm2 = t.match(/"formatedPrice"\s*:\s*"([^"]+)"/)||t.match(/"salePrice"\s*:\s*"([^"]+)"/)||t.match(/"price"\s*:\s*"([^"]+)"/);
                                if (pm2) fPrice = pm2[1];
                            }
                            if (!fImgLen && t.indexOf('imagePathList') > -1) {
                                var im2 = t.match(/"imagePathList"\s*:\s*\[([^\]]+)\]/);
                                if (im2) fImgLen = (im2[1].match(/https/g)||[]).length;
                            }
                            if (!fSkuLen && t.indexOf('SKU') > -1) {
                                fSkuLen = (t.match(/"skuPropertyName"/g)||[]).length;
                            }
                            if (!fDescUrl && t.indexOf('descriptionUrl') > -1) {
                                var du = t.match(/"descriptionUrl"\s*:\s*"([^"]+)"/);
                                if (du) fDescUrl = du[1];
                            }
                        }
                        out.scriptTitle = fTitle;
                        out.scriptPrice = fPrice;
                        out.scriptImgLen = fImgLen;
                        out.scriptSkuLen = fSkuLen;
                        out.scriptDescUrl = fDescUrl;

                        // 6. DOM extraction
                        out.pageTitle = document.title;
                        var h1 = document.querySelector('h1');
                        out.h1 = h1 ? h1.textContent.trim().substring(0,100) : '';
                        var mDesc = document.querySelector('meta[name="description"]');
                        out.metaDesc = mDesc ? mDesc.content.substring(0,150) : '';
                        // Price in DOM
                        var priceEl = document.querySelector('[class*="price"],[itemprop="price"]');
                        out.domPrice = priceEl ? priceEl.textContent.trim().substring(0,30) : '';

                        // 7. Variant elements in DOM (color/size buttons)
                        var skuGroups = [];
                        // Try various selectors for SKU/variant sections
                        var selectors = [
                            '[class*="sku-item"]', '[class*="skuItem"]',
                            '[class*="sku-list"]', '[class*="skuList"]',
                            '[class*="sku-property"]', '[class*="skuProperty"]',
                            '[class*="product-sku"]', '[class*="variants"]',
                            '[data-sku-col]', '[class*="sku"]'
                        ];
                        var foundSkuEls = [];
                        for (var si=0; si<selectors.length; si++) {
                            var els = document.querySelectorAll(selectors[si]);
                            if (els.length > 0) {
                                foundSkuEls.push(selectors[si] + ':' + els.length);
                            }
                        }
                        out.skuDomSelectors = foundSkuEls;

                        // Try to get variant option texts
                        var optionTexts = [];
                        var optEls = document.querySelectorAll('[class*="sku"] span, [class*="variant"] span, [class*="option"] span');
                        for (var oi=0; oi<Math.min(optEls.length,30); oi++) {
                            var txt = optEls[oi].textContent.trim();
                            if (txt && txt.length < 50 && txt.length > 0) optionTexts.push(txt);
                        }
                        out.optionTexts = optionTexts.slice(0,20);

                        // 8. Script scan for new AE format variants
                        var scripts2 = document.querySelectorAll('script');
                        var skuRaw = '';
                        for (var si2=0; si2<scripts2.length; si2++) {
                            var st = scripts2[si2].textContent||'';
                            if (st.indexOf('"color"') > -1 || st.indexOf('"size"') > -1 || st.indexOf('"Color"') > -1 || st.indexOf('"Size"') > -1) {
                                // show first 500 chars of this script
                                skuRaw = st.substring(0, 500);
                                break;
                            }
                        }
                        out.colorSizeScriptSample = skuRaw.substring(0,300);

                        return JSON.stringify(out);
                    } catch(e){return JSON.stringify({jsError:e.message});}
                })()
            """)
        return jsonify(_j.loads(r) if r else {"result": "null from js"})
    except Exception as e:
        import traceback
        return jsonify({"error": str(e), "traceback": traceback.format_exc()})


# ---------------------------------------------------------------------------

if __name__ == "__main__":
    app.run(debug=True, port=5050)
