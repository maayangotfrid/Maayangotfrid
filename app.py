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
            sb.sleep(6)
            r = sb.execute_script("""
                try {
                    var rp=window.runParams||{},d=rp.data||{};
                    var c=d.pageComponent||d.productComponent||rp.pageComponent||{};
                    var sm=c.skuModule||d.skuModule||{};
                    var sl=sm.productSKUPropertyList||[];
                    return JSON.stringify({
                        rpKeys:Object.keys(rp),
                        dKeys:Object.keys(d).slice(0,25),
                        cKeys:Object.keys(c).slice(0,25),
                        smKeys:Object.keys(sm),
                        skuLen:sl.length,
                        firstSku:sl[0]?JSON.stringify(sl[0]).substring(0,300):'empty',
                        title:(c.titleModule||{}).subject||'',
                        price:(c.priceModule||{}).formatedActivityPrice||(c.priceModule||{}).formatedPrice||'',
                        imgLen:((c.imageModule||{}).imagePathList||[]).length,
                        descUrl:(c.descriptionModule||{}).descriptionUrl||''
                    });
                } catch(e){return JSON.stringify({jsError:e.message});}
            """)
        return jsonify(_j.loads(r) if r else {"result": "null from js"})
    except Exception as e:
        import traceback
        return jsonify({"error": str(e), "traceback": traceback.format_exc()})


# ---------------------------------------------------------------------------

if __name__ == "__main__":
    app.run(debug=True, port=5050)
