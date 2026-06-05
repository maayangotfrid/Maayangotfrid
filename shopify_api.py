import os
import requests
from itertools import product as iterproduct
from pathlib import Path
from dotenv import load_dotenv

load_dotenv(Path(__file__).parent / ".env")

STORE_URL = os.getenv("SHOPIFY_STORE_URL", "").rstrip("/")
ACCESS_TOKEN = os.getenv("SHOPIFY_ACCESS_TOKEN", "")

_API_VERSION = "2024-04"


def _headers() -> dict:
    return {
        "X-Shopify-Access-Token": ACCESS_TOKEN,
        "Content-Type": "application/json",
    }


def is_configured() -> bool:
    return bool(STORE_URL and ACCESS_TOKEN)


def get_collections() -> list[dict]:
    url = f"{STORE_URL}/admin/api/{_API_VERSION}/custom_collections.json?limit=250"
    resp = requests.get(url, headers=_headers(), timeout=10)
    resp.raise_for_status()
    return resp.json().get("custom_collections", [])


def import_product(product: dict) -> dict:
    """
    Create a Shopify product from a product dict.

    Supported keys (all optional except title):
        title, description, price, images, variants, collection_id,
        vendor            – defaults to "AliExpress"
        product_type      – maps to Shopify product_type
        tags              – comma-separated string
        publish_status    – "draft" | "active"  (default "draft")
        publish_to_all_channels – bool → published_scope "global" or "web"
        seo_title         – metafield global.title_tag
        seo_description   – metafield global.description_tag
        metafields        – list of {key, value, namespace, type}
    """
    images = [{"src": img} for img in product.get("images", []) if img]

    # ------------------------------------------------------------------ #
    # Build variants + options (up to 3 option groups — Shopify maximum)  #
    # ------------------------------------------------------------------ #
    variants = []
    options = []

    raw_variants = product.get("variants", [])
    if raw_variants:
        groups = raw_variants[:3]
        options = [{"name": g["name"], "values": g["values"]} for g in groups]

        value_lists = [g["values"] for g in groups]
        for combo in iterproduct(*value_lists):
            variant = {
                "price": product.get("price", "0"),
                "inventory_management": "shopify",
                "inventory_quantity": 99,
            }
            for i, val in enumerate(combo):
                variant[f"option{i + 1}"] = val
            variants.append(variant)
    else:
        variants = [{
            "price": product.get("price", "0"),
            "inventory_management": "shopify",
            "inventory_quantity": 99,
        }]

    # ------------------------------------------------------------------ #
    # published_scope                                                      #
    # ------------------------------------------------------------------ #
    publish_to_all = product.get("publish_to_all_channels", True)
    published_scope = "global" if publish_to_all else "web"

    # ------------------------------------------------------------------ #
    # Metafields: SEO + any extra                                         #
    # ------------------------------------------------------------------ #
    metafields = []

    seo_title = (product.get("seo_title") or "").strip()
    seo_desc = (product.get("seo_description") or "").strip()

    if seo_title:
        metafields.append({
            "namespace": "global",
            "key": "title_tag",
            "value": seo_title,
            "type": "single_line_text_field",
        })
    if seo_desc:
        metafields.append({
            "namespace": "global",
            "key": "description_tag",
            "value": seo_desc,
            "type": "multi_line_text_field",
        })

    for mf in product.get("metafields", []):
        if mf.get("key") and mf.get("value"):
            metafields.append({
                "namespace": mf.get("namespace", "custom"),
                "key": mf["key"],
                "value": str(mf["value"]),
                "type": mf.get("type", "single_line_text_field"),
            })

    # ------------------------------------------------------------------ #
    # Assemble body                                                        #
    # ------------------------------------------------------------------ #
    body: dict = {
        "product": {
            "title": product.get("title", "Imported Product"),
            "body_html": product.get("description", ""),
            "vendor": product.get("vendor", "AliExpress"),
            "product_type": product.get("product_type", product.get("category", "")),
            "tags": product.get("tags", "aliexpress,imported"),
            "status": product.get("publish_status", "draft"),
            "published_scope": published_scope,
            "images": images,
            "variants": variants,
            "options": options if options else [{"name": "Title", "values": ["Default Title"]}],
        }
    }

    if metafields:
        body["product"]["metafields"] = metafields

    collection_id = product.get("collection_id")

    url = f"{STORE_URL}/admin/api/{_API_VERSION}/products.json"
    resp = requests.post(url, json=body, headers=_headers(), timeout=15)
    resp.raise_for_status()
    created = resp.json()["product"]

    if collection_id:
        _add_to_collection(created["id"], collection_id)

    return created


def _add_to_collection(product_id: int, collection_id: str) -> None:
    url = f"{STORE_URL}/admin/api/{_API_VERSION}/collects.json"
    requests.post(
        url,
        json={"collect": {"product_id": product_id, "collection_id": collection_id}},
        headers=_headers(),
        timeout=10,
    )
