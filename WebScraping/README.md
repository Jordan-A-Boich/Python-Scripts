# scrape_ms_learn.py

A command-line tool that searches Microsoft Learn documentation and returns matching page titles and URLs, one per line, ready to copy or save to a file.

## Requirements

pip install requests

## Usage

python scrape_ms_learn.py <term> [options]

### Arguments

| Argument       | Description |
|----------------|-------------|
| `term`         | Keyword or phrase to search for. Multi-word terms do not need quotes. |
| `--docs`       | Restrict results to a specific doc section (e.g. `sql`, `azure`, `dotnet`). Omit to search all of Microsoft Learn. |
| `--view`       | Version moniker to append to every link (e.g. `sql-server-ver17`). |
| `--max`        | Cap the number of results returned. Default: fetch everything. |
| `--output, -o` | Save output to a file instead of printing to the terminal. |
| `--locale`     | Locale to use. Default: `en-us`. |

## Examples

# Search all of Microsoft Learn
python scrape_ms_learn.py tempdb

# Multi-word search — no quotes needed
python scrape_ms_learn.py availability group

# Restrict to SQL Server docs
python scrape_ms_learn.py replication --docs sql

# Pin links to a specific SQL Server version
python scrape_ms_learn.py replication --docs sql --view sql-server-ver17

# Save output to a file
python scrape_ms_learn.py tempdb --output tempdb_links.txt

# Quick preview — cap at 50 results
python scrape_ms_learn.py backup --max 50

# Search Azure docs
python scrape_ms_learn.py sql database --docs azure

## Output Format

Each result is printed as the page title followed by its URL, with a blank line between entries:

tempdb Database
https://learn.microsoft.com/en-us/sql/relational-databases/databases/tempdb-database

Optimizing tempdb Performance
https://learn.microsoft.com/en-us/sql/relational-databases/databases/tempdb-database#optimizing-tempdb-performance-in-sql-server

## How It Works

The script calls the Microsoft Learn search API (learn.microsoft.com/api/search), which indexes actual page content — not just titles and URLs. This means a search for tempdb will surface pages that discuss tempdb heavily even if the word never appears in their title (e.g. wait stats guides, memory pressure articles, contention troubleshooting).

Results are paginated automatically in batches of 30 (the API maximum) until all results are retrieved or the --max limit is reached. Duplicate URLs are filtered out across pages.
