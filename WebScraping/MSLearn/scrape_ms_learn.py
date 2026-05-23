#!/usr/bin/env python3
"""
Microsoft Learn Link Finder

Searches across Microsoft Learn using its search API, which indexes actual
page content — not just titles — so you get comprehensive results.

Usage:
    python scrape_ms_learn.py <term> [options]

Examples:
    python scrape_ms_learn.py tempdb
    python scrape_ms_learn.py tempdb --docs sql
    python scrape_ms_learn.py "always on" --output alwayson.txt
    python scrape_ms_learn.py replication --docs sql --view sql-server-ver17

Options:
    --docs    Restrict results to a doc section (e.g. sql, azure, dotnet).
              Omit to search all of Microsoft Learn.
    --view    Version moniker to append to links (e.g. sql-server-ver17)
    --max     Max results to fetch total (default: 500)
    --output  Save to a file instead of printing to stdout
    --locale  Locale (default: en-us)

Requirements:
    pip install requests
"""

import argparse
import sys
from urllib.parse import quote as url_quote
import requests


BASE        = "https://learn.microsoft.com"
SEARCH_API  = f"{BASE}/api/search"
PAGE_SIZE   = 30
DEFAULT_MAX = None


def _build_url(term: str, locale: str, top: int, skip: int) -> str:
    """
    Build the search URL manually so that OData parameters ($top, $skip)
    are not percent-encoded by the requests library.  If we pass them via
    the `params` dict, requests turns '$top' into '%24top' which the API
    rejects with a 400.
    """
    safe_term = url_quote(term, safe="")
    return (
        f"{SEARCH_API}"
        f"?search={safe_term}"
        f"&locale={locale}"
        f"&$top={top}"
        f"&$skip={skip}"
    )


def search_ms_learn(
    term: str,
    locale: str,
    docs: str | None,
    max_results: int,
    view: str | None,
) -> list[tuple[str, str]]:
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/120.0.0.0 Safari/537.36"
        ),
        "Accept": "application/json, */*",
    }

    docs_prefix = f"{BASE}/{locale}/{docs}/".lower() if docs else None

    links: list[tuple[str, str]] = []
    seen:  set[str]              = set()
    skip  = 0

    while max_results is None or len(links) < max_results:
        batch = PAGE_SIZE if max_results is None else min(PAGE_SIZE, max_results - len(links))
        url   = _build_url(term, locale, batch, skip)

        print(f"  Fetching results {skip + 1}–{skip + batch}...", file=sys.stderr)

        try:
            r = requests.get(url, headers=headers, timeout=30)
            if not r.ok:
                print(f"  HTTP {r.status_code}: {r.reason}", file=sys.stderr)
                print(f"  Response body: {r.text[:400]}", file=sys.stderr)
                break
            data = r.json()
        except requests.RequestException as e:
            print(f"  Request error: {e}", file=sys.stderr)
            break
        except ValueError:
            print("  Error: Response was not valid JSON.", file=sys.stderr)
            break

        # MS Learn API may use 'results' or OData 'value'
        results = data.get("results", data.get("value", []))
        if not results:
            break

        for item in results:
            title = item.get("title", "").strip()
            url_  = item.get("url",   "").strip()

            if not title or not url_:
                continue

            if docs_prefix and not url_.lower().startswith(docs_prefix):
                continue

            if url_ in seen:
                continue
            seen.add(url_)

            if view and "view=" not in url_:
                sep = "&" if "?" in url_ else "?"
                url_ += f"{sep}view={view}"

            links.append((title, url_))

        skip += len(results)

        # '@odata.count' or 'count' tells us the total available
        total = data.get("@odata.count", data.get("count", 0))
        if total and skip >= total:
            print(f"  All {total} available results retrieved.", file=sys.stderr)
            break

    return links


def format_output(links: list[tuple[str, str]]) -> str:
    return "\n\n".join(f"{title}\n{url}" for title, url in links)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Search Microsoft Learn for documentation links matching a term.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("term", nargs="+", help="Keyword or phrase (e.g. tempdb, availability group).")
    parser.add_argument(
        "--docs", default=None,
        help="Restrict to a doc section, e.g. sql, azure, dotnet. Default: all sections.",
    )
    parser.add_argument(
        "--view", default=None,
        help="Version moniker to append to links (e.g. sql-server-ver17).",
    )
    parser.add_argument(
    	"--max", type=int, default=None,
    	help="Cap the number of results (default: fetch all).",
    )
    parser.add_argument(
        "--output", "-o", default=None,
        help="Write output to this file instead of stdout.",
    )
    parser.add_argument("--locale", default="en-us", help="Locale (default: en-us).")
    args = parser.parse_args()
    args.term = " ".join(args.term)	

    scope = f"'{args.docs}' docs" if args.docs else "all Microsoft Learn"
    print(f"\nSearching {scope} for: {args.term!r}", file=sys.stderr)
    print(f"  Search API : {SEARCH_API}\n", file=sys.stderr)

    links = search_ms_learn(
        term        = args.term,
        locale      = args.locale,
        docs        = args.docs,
        max_results = args.max,
        view        = args.view,
    )

    if not links:
        print(
            f"\n  No results found for '{args.term}'.\n"
            f"  Try removing --docs to search all sections, or broaden the term.",
            file=sys.stderr,
        )
        sys.exit(1)

    print(f"\n  Found {len(links)} links.\n", file=sys.stderr)

    output = format_output(links)

    if args.output:
        with open(args.output, "w", encoding="utf-8") as fh:
            fh.write(output + "\n")
        print(f"Saved to: {args.output}", file=sys.stderr)
    else:
        print(output)


if __name__ == "__main__":
    main()
