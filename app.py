import csv
import io
import json
import zipfile
import requests as req_lib
from flask import Flask, render_template, request, jsonify, Response, stream_with_context
from scraper import scrape_product, search_products
import shopify_api

app = Flask(__name__)


@app.route("/")
def index():
    return render_template("index.html")


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
    for url in urls[:50]:  # cap at 50 to avoid abuse
        try:
            product = scrape_product(url)
            results.append(product)
        except Exception as e:
            results.append({"status": "error", "source_url": url, "message": str(e)})

    return jsonify({"products": results})


@app.route("/api/import-to-shopify", methods=["POST"])
def import_to_shopify():
    if not shopify_api.is_configured():
        return jsonify({"error": "Shopify is not configured. Set SHOPIFY_STORE_URL and SHOPIFY_ACCESS_TOKEN in .env"}), 400

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
            results.append({"status": "error", "title": product.get("title", "?"), "message": str(e)})

    return jsonify({"results": results})


@app.route("/api/proxy-image")
def proxy_image():
    """
    Proxy an image from AliExpress CDN so the browser never hits CORS issues.
    Usage: /api/proxy-image?url=https%3A%2F%2Fae01.alicdn.com%2F...
    """
    image_url = request.args.get("url", "").strip()
    if not image_url:
        return jsonify({"error": "url parameter is required"}), 400

    # Basic allow-list: only proxy alicdn / aliexpress domains
    allowed_hosts = ("alicdn.com", "aliexpress.com", "ae01.alicdn.com",
                     "ae02.alicdn.com", "ae03.alicdn.com", "ae04.alicdn.com")
    from urllib.parse import urlparse
    parsed = urlparse(image_url)
    host = parsed.netloc.lower()
    if not any(host.endswith(h) for h in allowed_hosts):
        return jsonify({"error": "Domain not allowed"}), 403

    try:
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
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


@app.route("/api/download-images", methods=["POST"])
def download_images():
    """
    Accept a list of image URLs and a product title.
    Download all images and return them as a ZIP file.
    Body: { "images": ["https://..."], "title": "Product Name" }
    """
    data = request.json or {}
    images = data.get("images", [])
    title = data.get("title", "product").strip() or "product"

    if not images:
        return jsonify({"error": "No images provided"}), 400

    # Sanitise filename
    safe_title = "".join(c if c.isalnum() or c in " -_" else "_" for c in title)[:60].strip()

    zip_buffer = io.BytesIO()
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        "Referer": "https://www.aliexpress.com/",
    }

    with zipfile.ZipFile(zip_buffer, "w", zipfile.ZIP_DEFLATED) as zf:
        for idx, img_url in enumerate(images[:30], start=1):
            try:
                r = req_lib.get(img_url, headers=headers, timeout=15)
                r.raise_for_status()
                # Determine extension from Content-Type or URL
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
                # Skip failed images silently
                continue

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


if __name__ == "__main__":
    app.run(debug=True, port=5050)
