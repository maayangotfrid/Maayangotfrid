import os
import requests
from itertools import product as iterproduct
from pathlib import Path
from dotenv import load_dotenv

load_dotenv(Path(__file__).parent / ".env")

STORE_URL = os.getenv("SHOPIFY_STORE_URL", "").rstrip("/")
ACCESS_TOKEN = os.getenv("SHOPIFY_ACCESS_TOKEN", "")


def _headers():
    return {
        "X-Shopify-Access-Token": ACCESS_TOKEN,
        "Content-Type": "application/json",
    }


def is_configured() -> bool:
    return bool(STORE_URL and ACCESS_TOKEN)


def get_collections() -> list[dict]:
    url = f"{STORE_URL}/admin/api/2024-04/custom_collections.json?limit=250"
    resp = requests.get(url, headers=_headers(), timeout=10)
    resp.raise_for_status()
    return resp.json().get("custom_collections", [])


def import_product(product: dict) -> dict:
    """Create a product in Shopify from the given data dict."""
    images = [{"src": img} for img in product.get("images", []) if img]

    variants = []
    options = []

    raw_variants = product.get("variants", [])
    if raw_variants:
        # Support up to 3 option groups (Shopify maximum)
        groups = raw_variants[:3]
        options = [{"name": g["name"], "values": g["values"]} for g in groups]

        # Create all combinations of option values
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

    body = {
        "product": {
            "title": product.get("title", "Imported Product"),
            "body_html": product.get("description", ""),
            "vendor": "AliExpress",
            "product_type": product.get("category", ""),
            "tags": "aliexpress,imported",
            "status": product.get("publish_status", "draft"),
            "images": images,
            "variants": variants,
            "options": options if options else [{"name": "Title", "values": ["Default Title"]}],
        }
    }

    collection_id = product.get("collection_id")

    url = f"{STORE_URL}/admin/api/2024-04/products.json"
    resp = requests.post(url, json=body, headers=_headers(), timeout=15)
    resp.raise_for_status()
    created = resp.json()["product"]

    if collection_id:
        _add_to_collection(created["id"], collection_id)

    return created


def _add_to_collection(product_id: int, collection_id: str):
    url = f"{STORE_URL}/admin/api/2024-04/collects.json"
    requests.post(url, json={"collect": {"product_id": product_id, "collection_id": collection_id}},
                  headers=_headers(), timeout=10)
