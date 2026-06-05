import csv
import io
import json
from flask import Flask, render_template, request, jsonify
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


if __name__ == "__main__":
    app.run(debug=True, port=5050)
