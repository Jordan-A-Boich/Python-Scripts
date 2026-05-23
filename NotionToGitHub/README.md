# Notion → GitHub SQL Script Sync

A Python script that exports SQL scripts from Notion pages into `.md` files and pushes them directly to a GitHub repository — no copy-pasting required.

---

## What It Does

- Reads a top-level Notion page (e.g. `AlwaysOn`, `Replication`, `Performance`)
- Finds all sub-pages beneath it (e.g. `001-AG Seeding Status`, `002-AG Health Check`)
- Extracts all content — SQL code blocks, markdown text, headings, and lists
- Creates a `.md` file per sub-page using a clean naming convention (e.g. `AAG-AG-Seeding-Status.md`)
- Pushes all files into a specified folder in your GitHub repo
- Safely re-runnable — updates existing files rather than erroring on duplicates

---

## Prerequisites

- Python 3.8+
- `requests` library (`pip install requests`)
- A Notion integration token
- A GitHub personal access token with `repo` scope

---

## Setup

### 1. Create a Notion Integration

1. Go to [notion.so/my-integrations](https://www.notion.so/my-integrations)
2. Click **+ New integration**, give it a name, and click Submit
3. Copy the **Internal Integration Token** (starts with `ntn_`)
4. Open your Notion page → click `...` (top right) → **Connections** → select your integration

> ⚠️ You must connect the integration to **each top-level page** you want to sync. If you get a 404 error, this is the most likely cause.

### 2. Get Your Notion Page ID

1. Open the page in a browser
2. Copy the ID from the URL — it's the 32-character string at the end, e.g.:  
   `https://notion.so/AlwaysOn-abc123def456...` → Page ID is `abc123def456...`

### 3. Create a GitHub Personal Access Token

1. Go to GitHub → **Settings** → **Developer settings** → **Personal access tokens** → **Tokens (classic)**
2. Click **Generate new token (classic)**
3. Check the **`repo`** scope
4. Copy the token (starts with `ghp_`)

---

## Usage

### Set Environment Variables (PowerShell)

```powershell
$env:NOTION_TOKEN="ntn_your_token_here"
$env:NOTION_PAGE_ID="your_32_char_page_id"
$env:GITHUB_TOKEN="ghp_your_token_here"
$env:GITHUB_OWNER="your-github-username"
$env:GITHUB_REPO="your-repo-name"
$env:GITHUB_BRANCH="main"
$env:GITHUB_FOLDER="AlwaysOn"
$env:FILE_PREFIX="AAG"
```

### Run the Script

```powershell
python notion_to_github.py
```

### Expected Output

```
📄 Fetching sub-pages from Notion page: abc123...
✅ Found 8 sub-page(s)

  📝 Processing: '001-AG Seeding Status'  →  AlwaysOn/AAG-AG-Seeding-Status.md
     ✅ Pushed to GitHub: AlwaysOn/AAG-AG-Seeding-Status.md
  📝 Processing: '002 - AG Health Check'  →  AlwaysOn/AAG-AG-Health-Check.md
     ✅ Pushed to GitHub: AlwaysOn/AAG-AG-Health-Check.md
...
🎉 Sync complete!
```

---

## Configuration Reference

| Variable | Required | Default | Description |
|---|---|---|---|
| `NOTION_TOKEN` | ✅ | — | Notion integration token (`ntn_...`) |
| `NOTION_PAGE_ID` | ✅ | — | ID of the top-level Notion page to sync |
| `GITHUB_TOKEN` | ✅ | — | GitHub personal access token (`ghp_...`) |
| `GITHUB_OWNER` | ✅ | — | GitHub username or organization name |
| `GITHUB_REPO` | ✅ | — | Target repository name |
| `GITHUB_BRANCH` | ❌ | `main` | Branch to push files to |
| `GITHUB_FOLDER` | ❌ | `AlwaysOn` | Folder path inside the repo |
| `FILE_PREFIX` | ❌ | `AAG` | Prefix added to every filename |

---

## File Naming Convention

Notion sub-page titles are automatically cleaned and formatted:

| Notion Page Title | GitHub Filename |
|---|---|
| `001-AG Seeding Status` | `AAG-AG-Seeding-Status.md` |
| `002 - AG Health Check` | `AAG-AG-Health-Check.md` |
| `003 Replication Latency` | `AAG-Replication-Latency.md` |

The script strips leading numeric prefixes (`001-`, `002 - `, etc.) and replaces spaces and special characters with hyphens.

---

## Supported Notion Block Types

| Block Type | Markdown Output |
|---|---|
| Paragraph | Plain text |
| Heading 1 / 2 / 3 | `#` / `##` / `###` |
| Bulleted list | `- item` |
| Numbered list | `1. item` |
| Code block | Fenced code block with language tag |
| Divider | `---` |
| Callout / Quote | `> blockquote` |
| To-do | `- [ ] item` / `- [x] item` |
| Nested children | Recursively included |

---

## Troubleshooting

| Error | Cause | Fix |
|---|---|---|
| `404 Not Found` (Notion) | Integration not connected to the page | Open the Notion page → `...` → Connections → add your integration |
| `401 Unauthorized` (Notion) | Invalid or expired token | Regenerate token at [notion.so/my-integrations](https://www.notion.so/my-integrations) |
| `401 Unauthorized` (GitHub) | Token missing `repo` scope or expired | Regenerate at GitHub → Settings → Developer settings |
| `No sub-pages found` | Wrong Page ID | Re-copy the ID from the browser URL |
| `export` not recognized | Running bash syntax in PowerShell | Use `$env:VARIABLE="value"` syntax instead |

---

## Running for Multiple Notion Pages

Each top-level Notion page (e.g. AlwaysOn, Replication, Performance) is a separate run. Update the relevant env variables and re-run the script each time:

```powershell
# Sync AlwaysOn
$env:NOTION_PAGE_ID="page_id_for_alwayson"
$env:GITHUB_FOLDER="AlwaysOn"
$env:FILE_PREFIX="AAG"
python notion_to_github.py

# Sync Replication
$env:NOTION_PAGE_ID="page_id_for_replication"
$env:GITHUB_FOLDER="Replication"
$env:FILE_PREFIX="REP"
python notion_to_github.py
```
