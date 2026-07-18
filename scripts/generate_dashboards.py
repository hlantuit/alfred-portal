"""
generate_dashboards.py — updates every community's Notion dashboard.

For each community in communities/:
  1. Reads config.json to get notion_page_id and blocks list
  2. Clears the existing page content
  3. Renders each block in order and appends to the page

Blocks are Python modules in blocks/. Each must expose:
  render(community_config) -> list[dict]   (Notion block objects)

Usage:
  python scripts/generate_dashboards.py

Requires:
  NOTION_TOKEN env var (set as GitHub secret, entered once)
"""

import json
import os
import sys
import importlib.util
from datetime import datetime, timezone

try:
    import requests
except ImportError:
    print("ERROR: requests not installed — run: pip install requests")
    sys.exit(1)

NOTION_VERSION = "2022-06-28"
REPO_ROOT      = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
COMMUNITIES_DIR = os.path.join(REPO_ROOT, "communities")
BLOCKS_DIR      = os.path.join(REPO_ROOT, "blocks")


def notion_headers(token):
    return {
        "Authorization": f"Bearer {token}",
        "Notion-Version": NOTION_VERSION,
        "Content-Type": "application/json",
    }


def load_block_module(block_name):
    path = os.path.join(BLOCKS_DIR, f"{block_name}.py")
    if not os.path.isfile(path):
        raise FileNotFoundError(f"Block not found: {path}")
    spec = importlib.util.spec_from_file_location(block_name, path)
    mod  = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def get_existing_block_ids(page_id, headers):
    """Return IDs of all top-level blocks on the page."""
    ids = []
    cursor = None
    while True:
        params = {"page_size": 100}
        if cursor:
            params["start_cursor"] = cursor
        r = requests.get(
            f"https://api.notion.com/v1/blocks/{page_id}/children",
            headers=headers, params=params, timeout=15
        )
        r.raise_for_status()
        data = r.json()
        ids.extend(b["id"] for b in data.get("results", []))
        if not data.get("has_more"):
            break
        cursor = data["next_cursor"]
    return ids


def delete_block(block_id, headers):
    requests.delete(
        f"https://api.notion.com/v1/blocks/{block_id}",
        headers=headers, timeout=15
    ).raise_for_status()


def append_blocks(page_id, blocks, headers):
    # Notion allows max 100 blocks per request
    for i in range(0, len(blocks), 100):
        chunk = blocks[i:i+100]
        r = requests.patch(
            f"https://api.notion.com/v1/blocks/{page_id}/children",
            headers=headers,
            json={"children": chunk},
            timeout=30
        )
        r.raise_for_status()


def update_community(community, token):
    sid     = community["id"]
    page_id = community["notion_page_id"]
    blocks  = community.get("blocks", [])
    headers = notion_headers(token)

    print(f"\n[{sid}] Updating Notion page {page_id}")
    print(f"[{sid}] Blocks: {blocks}")

    # 1. Delete existing content
    existing = get_existing_block_ids(page_id, headers)
    for bid in existing:
        try:
            delete_block(bid, headers)
        except Exception as e:
            print(f"[{sid}] WARNING: could not delete block {bid}: {e}")

    # 2. Build new content from block modules
    all_notion_blocks = []
    for block_name in blocks:
        try:
            mod = load_block_module(block_name)
            rendered = mod.render(community)
            all_notion_blocks.extend(rendered)
            print(f"[{sid}] Block '{block_name}': {len(rendered)} Notion blocks")
        except Exception as e:
            print(f"[{sid}] ERROR in block '{block_name}': {e}")

    # 3. Append timestamp footer
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    all_notion_blocks.append({
        "object": "block", "type": "paragraph",
        "paragraph": {"rich_text": [{
            "type": "text",
            "text": {"content": f"Last updated: {ts}"},
            "annotations": {"color": "gray", "italic": True}
        }]}
    })

    # 4. Push to Notion
    if all_notion_blocks:
        append_blocks(page_id, all_notion_blocks, headers)
        print(f"[{sid}] Done — {len(all_notion_blocks)} blocks written")
    else:
        print(f"[{sid}] No blocks to write")


def load_communities():
    communities = []
    for name in sorted(os.listdir(COMMUNITIES_DIR)):
        cfg_path = os.path.join(COMMUNITIES_DIR, name, "config.json")
        if os.path.isfile(cfg_path):
            with open(cfg_path) as f:
                communities.append(json.load(f))
    return communities


def main():
    token = os.environ.get("NOTION_TOKEN")
    if not token:
        print("ERROR: NOTION_TOKEN env var not set")
        sys.exit(1)

    communities = load_communities()
    print(f"Found {len(communities)} communities")

    for community in communities:
        try:
            update_community(community, token)
        except Exception as e:
            print(f"ERROR [{community['id']}]: {e}")

    print("\nAll communities updated.")


if __name__ == "__main__":
    main()
