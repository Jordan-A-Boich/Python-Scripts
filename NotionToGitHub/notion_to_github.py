#!/usr/bin/env python3
"""
Notion → GitHub one-time sync script.
Traverses a Notion parent page, finds all sub-pages, extracts
code blocks + markdown text, and pushes .md files to GitHub.
"""

import os
import re
import base64
import time
import requests

# ── CONFIG ────────────────────────────────────────────────────────────────────
NOTION_TOKEN      = os.environ["NOTION_TOKEN"]       # secret_xxxx
NOTION_PAGE_ID    = os.environ["NOTION_PAGE_ID"]     # ID of your "AlwaysOn" page
GITHUB_TOKEN      = os.environ["GITHUB_TOKEN"]       # ghp_xxxx
GITHUB_OWNER      = os.environ["GITHUB_OWNER"]       # your GitHub username or org
GITHUB_REPO       = os.environ["GITHUB_REPO"]        # repo name
GITHUB_BRANCH     = os.environ.get("GITHUB_BRANCH", "main")
GITHUB_FOLDER     = os.environ.get("GITHUB_FOLDER", "AlwaysOn")   # folder inside repo
FILE_PREFIX       = os.environ.get("FILE_PREFIX", "AAG")           # e.g. AAG-FileName.md
# ─────────────────────────────────────────────────────────────────────────────

NOTION_HEADERS = {
    "Authorization": f"Bearer {NOTION_TOKEN}",
    "Notion-Version": "2022-06-28",
    "Content-Type": "application/json",
}

GITHUB_HEADERS = {
    "Authorization": f"Bearer {GITHUB_TOKEN}",
    "Accept": "application/vnd.github+json",
}


# ── NOTION HELPERS ────────────────────────────────────────────────────────────

def get_blocks(block_id: str) -> list:
    """Fetch all children blocks for a given block/page ID (handles pagination)."""
    blocks = []
    url = f"https://api.notion.com/v1/blocks/{block_id}/children"
    params = {"page_size": 100}

    while True:
        resp = requests.get(url, headers=NOTION_HEADERS, params=params)
        resp.raise_for_status()
        data = resp.json()
        blocks.extend(data.get("results", []))
        if not data.get("has_more"):
            break
        params["start_cursor"] = data["next_cursor"]
        time.sleep(0.3)   # be kind to the API

    return blocks


def extract_plain_text(rich_text_list: list) -> str:
    """Pull plain text out of a Notion rich_text array."""
    return "".join(rt.get("plain_text", "") for rt in rich_text_list)


def blocks_to_markdown(blocks: list) -> str:
    """
    Convert a list of Notion blocks to a Markdown string.
    Handles: paragraph, headings, bulleted/numbered lists,
             code blocks, dividers, and nested children.
    """
    lines = []

    for block in blocks:
        btype = block.get("type")
        data  = block.get(btype, {})

        if btype == "paragraph":
            text = extract_plain_text(data.get("rich_text", []))
            lines.append(text if text else "")

        elif btype in ("heading_1", "heading_2", "heading_3"):
            level = {"heading_1": "#", "heading_2": "##", "heading_3": "###"}[btype]
            text = extract_plain_text(data.get("rich_text", []))
            lines.append(f"{level} {text}")

        elif btype == "bulleted_list_item":
            text = extract_plain_text(data.get("rich_text", []))
            lines.append(f"- {text}")

        elif btype == "numbered_list_item":
            text = extract_plain_text(data.get("rich_text", []))
            lines.append(f"1. {text}")

        elif btype == "code":
            language = data.get("language", "sql")
            code     = extract_plain_text(data.get("rich_text", []))
            caption  = extract_plain_text(data.get("caption", []))
            if caption:
                lines.append(f"**{caption}**")
            lines.append(f"```{language}\n{code}\n```")

        elif btype == "divider":
            lines.append("---")

        elif btype == "callout":
            text = extract_plain_text(data.get("rich_text", []))
            lines.append(f"> {text}")

        elif btype == "quote":
            text = extract_plain_text(data.get("rich_text", []))
            lines.append(f"> {text}")

        elif btype == "to_do":
            text    = extract_plain_text(data.get("rich_text", []))
            checked = "x" if data.get("checked") else " "
            lines.append(f"- [{checked}] {text}")

        # Recurse into nested children (e.g. toggles, synced blocks)
        if block.get("has_children"):
            child_blocks = get_blocks(block["id"])
            child_md     = blocks_to_markdown(child_blocks)
            if child_md:
                lines.append(child_md)

        lines.append("")   # blank line between blocks

    return "\n".join(lines).strip()


def get_page_title(page: dict) -> str:
    """Extract the title from a Notion page object."""
    props = page.get("properties", {})
    # Title property can be named anything; find the one with type "title"
    for prop in props.values():
        if prop.get("type") == "title":
            return extract_plain_text(prop.get("title", []))
    return "Untitled"


# ── FILENAME HELPERS ──────────────────────────────────────────────────────────

def title_to_filename(title: str, prefix: str) -> str:
    """
    Convert a Notion page title like '001-AG Seeding Status'
    to a GitHub filename like 'AAG-AG-Seeding-Status.md'.

    Steps:
    1. Strip leading numeric prefix (e.g. '001-', '002 - ')
    2. Title-case remaining words
    3. Replace spaces/special chars with hyphens
    4. Prepend the configured prefix
    """
    # Remove leading number + separator (e.g. "001-", "002 - ", "01 ")
    cleaned = re.sub(r"^\d+\s*[-–—]?\s*", "", title).strip()

    # Replace any non-alphanumeric (except hyphens) with hyphens
    slug = re.sub(r"[^a-zA-Z0-9]+", "-", cleaned).strip("-")

    return f"{prefix}-{slug}.md"


# ── GITHUB HELPERS ────────────────────────────────────────────────────────────

def get_existing_sha(path: str) -> str | None:
    """Return the blob SHA if a file already exists in the repo (needed for updates)."""
    url = f"https://api.github.com/repos/{GITHUB_OWNER}/{GITHUB_REPO}/contents/{path}"
    resp = requests.get(url, headers=GITHUB_HEADERS, params={"ref": GITHUB_BRANCH})
    if resp.status_code == 200:
        return resp.json().get("sha")
    return None


def push_file(path: str, content: str, commit_message: str) -> None:
    """Create or update a file in the GitHub repo."""
    url     = f"https://api.github.com/repos/{GITHUB_OWNER}/{GITHUB_REPO}/contents/{path}"
    encoded = base64.b64encode(content.encode("utf-8")).decode("utf-8")

    payload: dict = {
        "message": commit_message,
        "content": encoded,
        "branch":  GITHUB_BRANCH,
    }

    existing_sha = get_existing_sha(path)
    if existing_sha:
        payload["sha"] = existing_sha   # required for updates

    resp = requests.put(url, headers=GITHUB_HEADERS, json=payload)
    resp.raise_for_status()


# ── MAIN ──────────────────────────────────────────────────────────────────────

def main():
    print(f"📄 Fetching sub-pages from Notion page: {NOTION_PAGE_ID}")
    top_blocks = get_blocks(NOTION_PAGE_ID)

    # Collect child_page blocks (sub-pages)
    sub_pages = [b for b in top_blocks if b.get("type") == "child_page"]

    if not sub_pages:
        print("⚠️  No sub-pages found. Check your NOTION_PAGE_ID.")
        return

    print(f"✅ Found {len(sub_pages)} sub-page(s)\n")

    for page_block in sub_pages:
        page_id    = page_block["id"]
        page_title = page_block.get("child_page", {}).get("title", "Untitled")
        filename   = title_to_filename(page_title, FILE_PREFIX)
        gh_path    = f"{GITHUB_FOLDER}/{filename}"

        print(f"  📝 Processing: '{page_title}'  →  {gh_path}")

        # Fetch all blocks inside this sub-page
        blocks = get_blocks(page_id)

        if not blocks:
            print(f"     ⚠️  No content found, skipping.")
            continue

        # Build markdown content
        md_content = f"# {page_title}\n\n" + blocks_to_markdown(blocks)

        # Push to GitHub
        try:
            push_file(
                path=gh_path,
                content=md_content,
                commit_message=f"sync: add {filename} from Notion",
            )
            print(f"     ✅ Pushed to GitHub: {gh_path}")
        except requests.HTTPError as e:
            print(f"     ❌ GitHub error for {gh_path}: {e.response.text}")

        time.sleep(0.5)   # avoid GitHub secondary rate limits

    print("\n🎉 Sync complete!")


if __name__ == "__main__":
    main()
