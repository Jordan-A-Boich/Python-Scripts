#!/usr/bin/env python3
"""SQL Server Patch Monitor.

Automated monitoring and downloading of the latest SQL Server Cumulative
Updates (CU) and Security Updates (GDR) to a staging directory, used to keep a
patch directory current for new server builds.

Supports SQL Server 2019, 2022 and 2025. The SQL Server version is a required
parameter. The output directory and an N-1 patching mode are optional.

    python sql_patch_monitor.py --sql-version 2022
    python sql_patch_monitor.py --sql-version 2019 --output-dir D:\\Patches
    python sql_patch_monitor.py --sql-version 2025 --n-1
    python sql_patch_monitor.py --sql-version 2022 --dry-run

Dependencies:
    pip install requests beautifulsoup4 lxml

Python 3.10+ required (uses X | Y union type hints).
"""

from __future__ import annotations

import argparse
import importlib.util
import logging
import os
import re
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime
from logging.handlers import RotatingFileHandler
from pathlib import Path
from urllib.parse import urljoin


def _ensure_dependencies() -> None:
    """Install any missing third-party dependencies, then proceed.

    Idempotent: packages already importable are left untouched; only the
    missing ones are pip-installed. Maps import name -> pip package name.
    """
    required = {
        "requests": "requests",
        "bs4": "beautifulsoup4",
        "lxml": "lxml",
    }
    missing = [
        pip_name
        for import_name, pip_name in required.items()
        if importlib.util.find_spec(import_name) is None
    ]
    if not missing:
        return
    print(f"Installing missing dependencies: {', '.join(missing)}", flush=True)
    try:
        subprocess.check_call(
            [sys.executable, "-m", "pip", "install", "--disable-pip-version-check", *missing]
        )
    except subprocess.CalledProcessError as exc:
        sys.exit(
            f"Failed to install dependencies ({', '.join(missing)}): {exc}\n"
            f"Install them manually with: pip install {' '.join(missing)}"
        )
    importlib.invalidate_caches()


_ensure_dependencies()

import requests  # noqa: E402  (imported after dependency bootstrap)
from bs4 import BeautifulSoup  # noqa: E402

# --------------------------------------------------------------------------- #
# Configuration
# --------------------------------------------------------------------------- #

# Source page for build info (do not change).
BLOGSPOT_URL = "https://sqlserverbuilds.blogspot.com/"

# Microsoft public endpoints used during download URL resolution.
CATALOG_SEARCH_URL = "https://www.catalog.update.microsoft.com/Search.aspx?q={kb}"
CATALOG_DOWNLOAD_URL = "https://www.catalog.update.microsoft.com/DownloadDialog.aspx"
DOWNLOAD_DETAILS_URL = "https://www.microsoft.com/en-us/download/details.aspx?id={id}"
SUPPORT_ARTICLE_URL = "https://support.microsoft.com/help/{kb}"

# Log rotation settings.
LOG_MAX_BYTES = 5 * 1024 * 1024  # 5 MB
LOG_BACKUP_COUNT = 3

# HTTP behaviour.
REQUEST_TIMEOUT = 45  # seconds, non-download requests
DOWNLOAD_TIMEOUT = 300  # seconds, downloads (5 minutes)
RETRY_ATTEMPTS = 3
RETRY_BACKOFF = 5  # initial backoff seconds, doubles each retry: 5 -> 10 -> 20

# Downloaded files smaller than this are treated as corrupt.
MIN_VALID_SIZE_MB = 50

# Per-supported-version metadata. Build prefix is used to validate that we are
# reading the correct table and that a resolved file matches the version.
VERSION_INFO: dict[str, dict[str, str]] = {
    "2019": {"label": "SQL Server 2019", "build_prefix": "15.0"},
    "2022": {"label": "SQL Server 2022", "build_prefix": "16.0"},
    "2025": {"label": "SQL Server 2025", "build_prefix": "17.0"},
}

INSTALL_ORDER_FILENAME = "INSTALL_ORDER.txt"

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)

log = logging.getLogger("sql_patch_monitor")


# --------------------------------------------------------------------------- #
# Data model
# --------------------------------------------------------------------------- #


@dataclass
class PatchRow:
    """A single parsed row from the build reference table."""

    cu_number: int | None
    kb: str | None  # e.g. "KB5081477"
    build: str | None  # e.g. "16.0.4255.1"
    release_date: str  # e.g. "2026-05-20"
    description: str
    is_security: bool  # security update / GDR hint from text
    withdrawn: bool
    direct_link: str | None
    raw_text: str = ""

    @property
    def kb_number(self) -> int | None:
        if not self.kb:
            return None
        digits = re.sub(r"\D", "", self.kb)
        return int(digits) if digits else None


@dataclass
class TargetPatch:
    """A patch we intend to ensure is present on the share."""

    kind: str  # "CU" or "GDR"
    cu_number: int
    kb: str
    build: str | None
    release_date: str
    description: str
    direct_link: str | None


@dataclass
class ResolvedPatch:
    """A target patch plus the resolution outcome."""

    target: TargetPatch
    download_url: str | None = None
    resolved_kb: str | None = None
    folder_name: str | None = None
    file_path: Path | None = None
    drift: bool = False
    drift_note: str = ""
    skipped: bool = False
    skip_reason: str = ""


@dataclass
class RunContext:
    sql_version: str
    label: str
    build_prefix: str
    output_dir: Path
    n_minus_1: bool
    dry_run: bool
    session: requests.Session
    local_kbs: set[int] = field(default_factory=set)


# --------------------------------------------------------------------------- #
# Argument parsing
# --------------------------------------------------------------------------- #


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Monitor and download the latest SQL Server CU and GDR patches.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--sql-version",
        required=True,
        choices=sorted(VERSION_INFO.keys()),
        help="SQL Server major version to monitor (required). One of 2019, 2022, 2025.",
    )
    parser.add_argument(
        "--output-dir",
        "--dir",
        dest="output_dir",
        default=None,
        help=(
            "Directory where patches and INSTALL_ORDER.txt are written. "
            "Defaults to the directory containing this script."
        ),
    )
    parser.add_argument(
        "--n-1",
        "--n-minus-1",
        dest="n_minus_1",
        action="store_true",
        help=(
            "N-1 patching mode. Grab the CU prior to the most recent one "
            "(plus its GDR security release if available) instead of the latest CU."
        ),
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Check and report only. Resolve and report patches but download nothing.",
    )
    return parser.parse_args(argv)


# --------------------------------------------------------------------------- #
# Logging, lock file
# --------------------------------------------------------------------------- #


def setup_logging(output_dir: Path, sql_version: str) -> Path:
    log_file = output_dir / f"sql{sql_version}_patch_monitor.log"
    log_file.parent.mkdir(parents=True, exist_ok=True)

    log.setLevel(logging.INFO)
    log.handlers.clear()

    fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s")

    file_handler = RotatingFileHandler(
        log_file, maxBytes=LOG_MAX_BYTES, backupCount=LOG_BACKUP_COUNT, encoding="utf-8"
    )
    file_handler.setFormatter(fmt)
    log.addHandler(file_handler)

    console = logging.StreamHandler(sys.stdout)
    console.setFormatter(fmt)
    log.addHandler(console)

    return log_file


def _pid_running(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # exists but owned by another user
    except OSError:
        return False
    return True


class LockFile:
    """Simple PID lock file that takes over stale locks."""

    def __init__(self, path: Path):
        self.path = path
        self.acquired = False

    def acquire(self) -> bool:
        if self.path.exists():
            try:
                existing = int(self.path.read_text().strip())
            except (ValueError, OSError):
                existing = None
            if existing is not None and _pid_running(existing):
                log.error(
                    "Another instance is already running (PID %s). Lock: %s",
                    existing,
                    self.path,
                )
                return False
            log.warning("Stale lock found (PID %s). Taking over.", existing)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(str(os.getpid()))
        self.acquired = True
        return True

    def release(self) -> None:
        if self.acquired and self.path.exists():
            try:
                self.path.unlink()
            except OSError:
                pass
        self.acquired = False


# --------------------------------------------------------------------------- #
# HTTP helpers
# --------------------------------------------------------------------------- #


def build_session() -> requests.Session:
    session = requests.Session()
    session.headers.update({"User-Agent": USER_AGENT})
    return session


def http_get(
    session: requests.Session, url: str, *, timeout: int = REQUEST_TIMEOUT, **kwargs
) -> requests.Response | None:
    backoff = RETRY_BACKOFF
    for attempt in range(1, RETRY_ATTEMPTS + 1):
        try:
            resp = session.get(url, timeout=timeout, **kwargs)
            resp.raise_for_status()
            return resp
        except requests.RequestException as exc:
            log.warning("GET %s failed (attempt %s/%s): %s", url, attempt, RETRY_ATTEMPTS, exc)
            if attempt < RETRY_ATTEMPTS:
                time.sleep(backoff)
                backoff *= 2
    return None


def http_post(
    session: requests.Session, url: str, *, timeout: int = REQUEST_TIMEOUT, **kwargs
) -> requests.Response | None:
    backoff = RETRY_BACKOFF
    for attempt in range(1, RETRY_ATTEMPTS + 1):
        try:
            resp = session.post(url, timeout=timeout, **kwargs)
            resp.raise_for_status()
            return resp
        except requests.RequestException as exc:
            log.warning("POST %s failed (attempt %s/%s): %s", url, attempt, RETRY_ATTEMPTS, exc)
            if attempt < RETRY_ATTEMPTS:
                time.sleep(backoff)
                backoff *= 2
    return None


# --------------------------------------------------------------------------- #
# Build reference scraping & classification
# --------------------------------------------------------------------------- #

_BUILD_RE = re.compile(r"\b(\d{2}\.\d+\.\d+\.\d+)\b")
# The build reference lists KB numbers as bare digits (e.g. "5074819"); the
# "KB" prefix is optional. SQL Server KB numbers are 7 digits.
_KB_RE = re.compile(r"\b(?:KB\s*)?(\d{7})\b", re.IGNORECASE)
_CU_RE = re.compile(r"\bCU\s*?(\d+)\b", re.IGNORECASE)
_CU_LONG_RE = re.compile(r"cumulative\s+update\s+(\d+)", re.IGNORECASE)
_DATE_RE = re.compile(
    r"(\d{4}-\d{2}-\d{2})|"
    r"([A-Z][a-z]+\s+\d{1,2},\s+\d{4})|"
    r"(\d{1,2}/\d{1,2}/\d{4})"
)


def _find_version_table(soup: BeautifulSoup, label: str):
    """Locate the first table following a heading containing `label`."""
    target = label.lower()
    for heading in soup.find_all(re.compile(r"^h[1-6]$")):
        text = heading.get_text(" ", strip=True).lower()
        if target in text:
            table = heading.find_next("table")
            if table is not None:
                return table
    # Fallback: scan all tables, return the first whose text mentions the label.
    for table in soup.find_all("table"):
        if target in table.get_text(" ", strip=True).lower():
            return table
    return None


def _normalise_date(raw: str) -> str:
    raw = raw.strip()
    for fmt in ("%Y-%m-%d", "%B %d, %Y", "%b %d, %Y", "%m/%d/%Y"):
        try:
            return datetime.strptime(raw, fmt).strftime("%Y-%m-%d")
        except ValueError:
            continue
    return raw


def _parse_row(cells_text: str, row, build_prefix: str) -> PatchRow | None:
    lowered = cells_text.lower()

    withdrawn = "withdrawn" in lowered

    # A row may list several build representations (e.g. 16.0.x, 16.00.x,
    # 2022.160.x). Pick the one belonging to this SQL Server version.
    build = None
    for candidate in _BUILD_RE.findall(cells_text):
        if candidate.startswith(build_prefix + "."):
            build = candidate
            break

    kb_match = _KB_RE.search(cells_text)
    kb = f"KB{kb_match.group(1)}" if kb_match else None

    cu_match = _CU_RE.search(cells_text) or _CU_LONG_RE.search(cells_text)
    cu_number = int(cu_match.group(1)) if cu_match else None

    date_match = _DATE_RE.search(cells_text)
    release_date = _normalise_date(date_match.group(0)) if date_match else ""

    is_security = (
        "security update" in lowered
        or "security patch" in lowered
        or "gdr" in lowered
    )

    direct_link = None
    for a in row.find_all("a", href=True):
        href = a["href"]
        if href.lower().endswith(".exe"):
            direct_link = href
            break

    if cu_number is None and kb is None and build is None:
        return None

    return PatchRow(
        cu_number=cu_number,
        kb=kb,
        build=build,
        release_date=release_date,
        description=cells_text.strip(),
        is_security=is_security,
        withdrawn=withdrawn,
        direct_link=direct_link,
        raw_text=cells_text.strip(),
    )


def scrape_build_rows(ctx: RunContext) -> list[PatchRow]:
    log.info("Fetching build reference page: %s", BLOGSPOT_URL)
    resp = http_get(ctx.session, BLOGSPOT_URL)
    if resp is None:
        raise RuntimeError(f"Unable to fetch build reference page: {BLOGSPOT_URL}")

    soup = BeautifulSoup(resp.text, "lxml")
    table = _find_version_table(soup, ctx.label)
    if table is None:
        raise RuntimeError(
            f"Could not locate a build table for '{ctx.label}' on {BLOGSPOT_URL}"
        )

    rows: list[PatchRow] = []
    for tr in table.find_all("tr"):
        cells = tr.find_all(["td", "th"])
        if not cells:
            continue
        cells_text = " | ".join(c.get_text(" ", strip=True) for c in cells)
        parsed = _parse_row(cells_text, tr, ctx.build_prefix)
        if parsed is None:
            continue
        if parsed.cu_number is None:
            # Only interested in CU/GDR rows (those carry a CU number).
            continue
        if parsed.withdrawn:
            log.info("Skipping withdrawn row: %s", parsed.raw_text[:120])
            continue
        rows.append(parsed)

    log.info("Parsed %d CU/GDR rows for %s.", len(rows), ctx.label)
    return rows


def _build_sort_key(row: PatchRow) -> tuple:
    if row.build:
        parts = tuple(int(p) for p in row.build.split("."))
    else:
        parts = (0,)
    return (parts, row.kb_number or 0)


def classify_targets(rows: list[PatchRow], n_minus_1: bool) -> tuple[TargetPatch | None, TargetPatch | None, int | None]:
    """Group rows by CU number and select base CU + GDR for the target CU.

    Returns (cu_target, gdr_target, target_cu_number).

    Microsoft naming changed around CU5: after CU5 both the base CU and its
    security update may carry a "Security update" prefix. We therefore group by
    CU number and resolve roles by build/date position within each group — the
    lowest build for a CU number is the base CU; a later (higher) build for the
    same CU number is the security update (GDR).
    """
    groups: dict[int, list[PatchRow]] = {}
    for row in rows:
        if row.cu_number is None:
            continue
        groups.setdefault(row.cu_number, []).append(row)

    if not groups:
        return None, None, None

    distinct_cus = sorted(groups.keys(), reverse=True)
    if n_minus_1:
        if len(distinct_cus) < 2:
            log.warning(
                "N-1 requested but only one CU (CU%s) is available. Falling back to it.",
                distinct_cus[0],
            )
            target_cu = distinct_cus[0]
        else:
            target_cu = distinct_cus[1]
    else:
        target_cu = distinct_cus[0]

    group = sorted(groups[target_cu], key=_build_sort_key)

    # Base CU: prefer a non-security row; otherwise the lowest build in the group.
    base_candidates = [r for r in group if not r.is_security]
    base_row = base_candidates[0] if base_candidates else group[0]

    # GDR: the highest-build security row that is newer than the base CU.
    security_candidates = [
        r
        for r in group
        if r.is_security and _build_sort_key(r) > _build_sort_key(base_row)
    ]
    gdr_row = security_candidates[-1] if security_candidates else None

    cu_target = None
    if base_row.kb:
        cu_target = TargetPatch(
            kind="CU",
            cu_number=target_cu,
            kb=base_row.kb,
            build=base_row.build,
            release_date=base_row.release_date,
            description=base_row.description,
            direct_link=base_row.direct_link,
        )

    gdr_target = None
    if gdr_row and gdr_row.kb:
        gdr_target = TargetPatch(
            kind="GDR",
            cu_number=target_cu,
            kb=gdr_row.kb,
            build=gdr_row.build,
            release_date=gdr_row.release_date,
            description=gdr_row.description,
            direct_link=gdr_row.direct_link,
        )

    return cu_target, gdr_target, target_cu


# --------------------------------------------------------------------------- #
# Patch directory management
# --------------------------------------------------------------------------- #

_FOLDER_KB_RE = re.compile(r"KB(\d{6,7})", re.IGNORECASE)


def prepare_patch_directory(ctx: RunContext) -> None:
    ctx.output_dir.mkdir(parents=True, exist_ok=True)
    log.info("Patch directory ready: %s", ctx.output_dir)


def scan_existing_patches(ctx: RunContext) -> set[int]:
    """Scan for already-downloaded patches and register all KB numbers.

    A drift folder embeds two KB numbers in its name; both are registered so the
    patch is recognised on subsequent runs even when the build reference still
    asks for the older KB.
    """
    prefix = f"SQL{ctx.sql_version}-"
    found: set[int] = set()
    for entry in ctx.output_dir.iterdir():
        if not entry.is_dir() or not entry.name.startswith(prefix):
            continue
        for match in _FOLDER_KB_RE.finditer(entry.name):
            found.add(int(match.group(1)))
        # Clean up stale .tmp files from previously crashed downloads.
        for tmp in entry.glob("*.tmp"):
            log.info("Removing stale temp file: %s", tmp)
            try:
                tmp.unlink()
            except OSError as exc:
                log.warning("Could not remove %s: %s", tmp, exc)
    if found:
        log.info("Existing patch KBs on share: %s", ", ".join(f"KB{k}" for k in sorted(found)))
    else:
        log.info("No existing patches found on share.")
    ctx.local_kbs = found
    return found


def folder_name_for(
    ctx: RunContext, target: TargetPatch, resolved_kb: str | None, drift: bool
) -> str:
    """Build the named subfolder for a patch.

    Normal CU:   SQL2022-CU25-KB5081477-16.0.4255.1-2026-05-20
    Normal GDR:  SQL2022-CU25-GDR-KB5092000-16.0.4257.2-2026-07-08
    Drift:       SQL2022-CU25-KB5081477-KB5080999-2026-05-17  (both KBs, no build)
    """
    base = f"SQL{ctx.sql_version}-CU{target.cu_number}"
    date = target.release_date or datetime.now().strftime("%Y-%m-%d")
    if drift and resolved_kb and resolved_kb != target.kb:
        # Resolved (actual) KB first, then blogspot-expected KB. No build yet.
        return f"{base}-{resolved_kb}-{target.kb}-{date}"
    kb = resolved_kb or target.kb
    if target.kind == "GDR":
        return f"{base}-GDR-{kb}-{target.build or 'unknown'}-{date}"
    return f"{base}-{kb}-{target.build or 'unknown'}-{date}"


def cleanup_old_folders(ctx: RunContext, keep_folders: set[str]) -> None:
    """Remove SQL{version}-* folders that are not part of the current patch set."""
    prefix = f"SQL{ctx.sql_version}-"
    for entry in ctx.output_dir.iterdir():
        if not entry.is_dir() or not entry.name.startswith(prefix):
            continue
        if entry.name in keep_folders:
            continue
        log.info("Cleaning up outdated patch folder: %s", entry.name)
        try:
            shutil.rmtree(entry)
        except OSError as exc:
            log.warning("Could not remove %s: %s", entry, exc)


# --------------------------------------------------------------------------- #
# Download URL resolution (Strategies A, B, C1, C2, C3)
# --------------------------------------------------------------------------- #

_EXE_URL_RE = re.compile(r"https?://[^\s\"'<>]+?\.exe", re.IGNORECASE)
_GUID_RE = re.compile(
    r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"
)
_DC_ID_RE = re.compile(r"download(?:/details\.aspx\?id=|\.aspx\?id=|/)?(\d{4,7})", re.IGNORECASE)


def _kb_in_url(url: str, kb: str) -> bool:
    digits = re.sub(r"\D", "", kb)
    return digits in url


def _strategy_a(target: TargetPatch) -> str | None:
    if target.direct_link and target.direct_link.lower().endswith(".exe"):
        log.info("Strategy A: direct .exe link from build reference.")
        return target.direct_link
    return None


def _strategy_b_catalog(ctx: RunContext, target: TargetPatch) -> str | None:
    """Microsoft Update Catalog: GUID via input scan / raw HTML, then POST dialog."""
    search_url = CATALOG_SEARCH_URL.format(kb=re.sub(r"\D", "", target.kb))
    resp = http_get(ctx.session, search_url)
    if resp is None:
        return None
    soup = BeautifulSoup(resp.text, "lxml")

    guids: list[str] = []
    for inp in soup.find_all("input", attrs={"id": True}):
        candidate = inp["id"].split("_")[0]
        if _GUID_RE.fullmatch(candidate):
            guids.append(candidate)
    if not guids:
        guids = list(dict.fromkeys(_GUID_RE.findall(resp.text)))

    for guid in guids:
        url = _catalog_post(ctx, guid)
        if url:
            log.info("Strategy B: resolved via Update Catalog (GUID %s).", guid)
            return url
    return None


def _catalog_post(ctx: RunContext, guid: str) -> str | None:
    import json

    payload = {
        "updateIDs": json.dumps([{"size": 0, "languages": "", "uidInfo": guid, "updateID": guid}]),
        "updateIDsBlockedForImport": "",
        "wsusApiPresent": "",
        "contentImport": "",
        "sku": "",
        "serverName": "",
        "ssl": "",
        "portNumber": "",
        "version": "",
    }
    resp = http_post(ctx.session, CATALOG_DOWNLOAD_URL, data=payload)
    if resp is None:
        return None
    match = _EXE_URL_RE.search(resp.text)
    return match.group(0) if match else None


def _strategy_c_article(ctx: RunContext, target: TargetPatch) -> str | None:
    """Fetch the KB article and try C1 (catalog link), C2 (DC details), C3 (raw exe)."""
    article_url = SUPPORT_ARTICLE_URL.format(kb=re.sub(r"\D", "", target.kb))
    resp = http_get(ctx.session, article_url)
    if resp is None:
        return None
    html = resp.text
    soup = BeautifulSoup(html, "lxml")

    # C1: catalog link embedded in the article.
    for a in soup.find_all("a", href=True):
        href = a["href"]
        if "catalog.update.microsoft.com" in href.lower():
            guids = _GUID_RE.findall(href)
            for guid in guids:
                url = _catalog_post(ctx, guid)
                if url:
                    log.info("Strategy C1: catalog link from KB article.")
                    return url

    # C2: Download Center details.aspx pages — iterate up to 8 ids, validate KB.
    dc_ids = list(dict.fromkeys(_DC_ID_RE.findall(html)))[:8]
    for dc_id in dc_ids:
        details_url = DOWNLOAD_DETAILS_URL.format(id=dc_id)
        d_resp = http_get(ctx.session, details_url)
        if d_resp is None:
            continue
        for exe in _EXE_URL_RE.findall(d_resp.text):
            if _kb_in_url(exe, target.kb):
                log.info("Strategy C2: resolved via Download Center id %s.", dc_id)
                return exe

    # C3: raw .exe url containing the KB number anywhere in the article HTML.
    for exe in _EXE_URL_RE.findall(html):
        if _kb_in_url(exe, target.kb):
            log.info("Strategy C3: raw .exe URL containing the KB number.")
            return urljoin(article_url, exe)

    return None


def _kb_from_url(url: str) -> str | None:
    match = _KB_RE.search(url)
    if match:
        return f"KB{match.group(1)}"
    # exe filenames like SQLServer2022-KB5081477-x64.exe
    match = re.search(r"KB(\d{6,7})", url, re.IGNORECASE)
    return f"KB{match.group(1)}" if match else None


def resolve_download_url(ctx: RunContext, target: TargetPatch) -> tuple[str | None, str | None]:
    """Resolve a download URL, returning (url, resolved_kb)."""
    log.info("Resolving download for %s CU%s %s ...", target.kind, target.cu_number, target.kb)
    for strategy in (
        lambda: _strategy_a(target),
        lambda: _strategy_b_catalog(ctx, target),
        lambda: _strategy_c_article(ctx, target),
    ):
        url = strategy()
        if url:
            resolved_kb = _kb_from_url(url) or target.kb
            return url, resolved_kb
    return None, None


# --------------------------------------------------------------------------- #
# Download
# --------------------------------------------------------------------------- #


def download_file(ctx: RunContext, url: str, dest_dir: Path, file_name: str) -> Path | None:
    """Stream to a .tmp file, validate size, then rename to .exe atomically."""
    dest_dir.mkdir(parents=True, exist_ok=True)
    final_path = dest_dir / file_name
    tmp_path = dest_dir / (file_name + ".tmp")

    if final_path.exists():
        size_mb = final_path.stat().st_size / (1024 * 1024)
        if size_mb >= MIN_VALID_SIZE_MB:
            log.info("Already present and valid (%.1f MB): %s", size_mb, final_path.name)
            return final_path
        log.warning("Existing file too small (%.1f MB), re-downloading.", size_mb)
        final_path.unlink()

    backoff = RETRY_BACKOFF
    for attempt in range(1, RETRY_ATTEMPTS + 1):
        try:
            with ctx.session.get(url, stream=True, timeout=DOWNLOAD_TIMEOUT) as resp:
                resp.raise_for_status()
                expected = int(resp.headers.get("Content-Length", 0))
                written = 0
                with open(tmp_path, "wb") as fh:
                    for chunk in resp.iter_content(chunk_size=1024 * 256):
                        if chunk:
                            fh.write(chunk)
                            written += len(chunk)

            size_mb = written / (1024 * 1024)
            if expected and written < expected:
                raise IOError(
                    f"Incomplete download: {written} of {expected} bytes."
                )
            if size_mb < MIN_VALID_SIZE_MB:
                raise IOError(
                    f"Downloaded file too small ({size_mb:.1f} MB < {MIN_VALID_SIZE_MB} MB)."
                )

            tmp_path.replace(final_path)
            log.info("Downloaded %s (%.1f MB).", final_path.name, size_mb)
            return final_path
        except (requests.RequestException, IOError) as exc:
            log.warning("Download failed (attempt %s/%s): %s", attempt, RETRY_ATTEMPTS, exc)
            if tmp_path.exists():
                try:
                    tmp_path.unlink()
                except OSError:
                    pass
            if attempt < RETRY_ATTEMPTS:
                time.sleep(backoff)
                backoff *= 2

    log.error("Giving up on download: %s", url)
    return None


def exe_name_for(ctx: RunContext, resolved_kb: str) -> str:
    return f"SQLServer{ctx.sql_version}-{resolved_kb}-x64.exe"


# --------------------------------------------------------------------------- #
# Drift detection & per-patch processing
# --------------------------------------------------------------------------- #


def process_cu(ctx: RunContext, cu: TargetPatch) -> ResolvedPatch:
    result = ResolvedPatch(target=cu)
    url, resolved_kb = resolve_download_url(ctx, cu)
    if url is None:
        result.skipped = True
        result.skip_reason = (
            "Could not resolve a download URL through any strategy. "
            "The Microsoft Download Center may not have updated its page for this "
            "KB yet. Re-run the script in 24-48 hours."
        )
        log.error("CU%s: %s", cu.cu_number, result.skip_reason)
        return result

    result.download_url = url
    result.resolved_kb = resolved_kb

    expected_n = cu.kb and int(re.sub(r"\D", "", cu.kb))
    resolved_n = resolved_kb and int(re.sub(r"\D", "", resolved_kb))

    if expected_n and resolved_n:
        if resolved_n > expected_n:
            # Download Center ahead of blogspot. CUs are cumulative, accept it.
            result.drift = True
            result.drift_note = (
                f"Download Center served {resolved_kb} but the build reference "
                f"expected {cu.kb}. The newer CU is cumulative and was downloaded. "
                f"The GDR for {cu.kb} is suppressed until the build reference updates."
            )
            log.warning("CU%s drift (DC ahead): %s", cu.cu_number, result.drift_note)
        elif resolved_n < expected_n:
            # Blogspot ahead of Download Center. Reject the stale file.
            result.skipped = True
            result.skip_reason = (
                f"Download Center resolved {resolved_kb} but the build reference "
                f"expected {cu.kb}. The Microsoft Download Center may not have "
                f"updated its page for this KB yet. Re-run the script in 24-48 hours."
            )
            log.error("CU%s drift (build ref ahead): %s", cu.cu_number, result.skip_reason)
            return result

    result.folder_name = folder_name_for(ctx, cu, resolved_kb, result.drift)
    result.file_path = ctx.output_dir / result.folder_name / exe_name_for(ctx, resolved_kb)

    if ctx.dry_run:
        log.info(
            "[DRY RUN] Would download CU%s %s -> %s",
            cu.cu_number,
            resolved_kb,
            result.file_path,
        )
        return result

    downloaded = download_file(
        ctx, url, result.file_path.parent, result.file_path.name
    )
    if downloaded is None:
        result.skipped = True
        result.skip_reason = "Download failed after retries."
    return result


def process_gdr(ctx: RunContext, gdr: TargetPatch, cu_result: ResolvedPatch) -> ResolvedPatch:
    result = ResolvedPatch(target=gdr)

    if cu_result.drift:
        result.skipped = True
        result.skip_reason = (
            "Suppressed due to CU version drift — the GDR cannot be applied on top "
            "of the newer CU served by the Download Center."
        )
        log.warning("GDR CU%s: %s", gdr.cu_number, result.skip_reason)
        return result

    url, resolved_kb = resolve_download_url(ctx, gdr)
    if url is None:
        result.skipped = True
        result.skip_reason = (
            "Could not resolve a download URL for the security update."
        )
        log.error("GDR CU%s: %s", gdr.cu_number, result.skip_reason)
        return result

    result.download_url = url
    result.resolved_kb = resolved_kb
    result.folder_name = folder_name_for(ctx, gdr, resolved_kb, False)
    result.file_path = ctx.output_dir / result.folder_name / exe_name_for(ctx, resolved_kb)

    if ctx.dry_run:
        log.info(
            "[DRY RUN] Would download GDR CU%s %s -> %s",
            gdr.cu_number,
            resolved_kb,
            result.file_path,
        )
        return result

    downloaded = download_file(ctx, url, result.file_path.parent, result.file_path.name)
    if downloaded is None:
        result.skipped = True
        result.skip_reason = "Download failed after retries."
    return result


# --------------------------------------------------------------------------- #
# INSTALL_ORDER.txt
# --------------------------------------------------------------------------- #


def write_install_order(
    ctx: RunContext,
    cu_result: ResolvedPatch | None,
    gdr_result: ResolvedPatch | None,
) -> None:
    lines: list[str] = []
    lines.append("=" * 70)
    lines.append(f"{ctx.label} — PATCH INSTALL ORDER")
    lines.append(f"Generated: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    if ctx.n_minus_1:
        lines.append("Patching mode: N-1 (one CU behind the latest)")
    else:
        lines.append("Patching mode: latest CU + GDR")
    lines.append("=" * 70)
    lines.append("")

    drift = cu_result.drift if cu_result else False
    if drift:
        lines.append("!" * 70)
        lines.append("VERSION DRIFT WARNING")
        lines.append(cu_result.drift_note)
        lines.append("The security update (GDR) has been suppressed for this run.")
        lines.append("!" * 70)
        lines.append("")

    # Step 1: CU
    lines.append("STEP 1 — CUMULATIVE UPDATE (CU)")
    lines.append("-" * 70)
    if cu_result and not cu_result.skipped and cu_result.file_path:
        cu = cu_result.target
        lines.append(f"  CU number    : CU{cu.cu_number}")
        lines.append(f"  KB           : {cu_result.resolved_kb or cu.kb}")
        lines.append(f"  Build        : {cu.build or 'unknown'}")
        lines.append(f"  Release date : {cu.release_date or 'unknown'}")
        lines.append(f"  Description  : {cu.description}")
        lines.append(f"  File         : {cu_result.file_path}")
    elif cu_result and cu_result.skipped:
        lines.append(f"  NOT AVAILABLE: {cu_result.skip_reason}")
    else:
        lines.append("  No Cumulative Update available.")
    lines.append("")

    # Step 2: GDR
    lines.append("STEP 2 — SECURITY UPDATE (GDR)")
    lines.append("-" * 70)
    if gdr_result and not gdr_result.skipped and gdr_result.file_path:
        gdr = gdr_result.target
        lines.append(f"  CU number    : CU{gdr.cu_number}")
        lines.append(f"  KB           : {gdr_result.resolved_kb or gdr.kb}")
        lines.append(f"  Build        : {gdr.build or 'unknown'}")
        lines.append(f"  Release date : {gdr.release_date or 'unknown'}")
        lines.append(f"  Description  : {gdr.description}")
        lines.append(f"  File         : {gdr_result.file_path}")
    elif gdr_result and gdr_result.skipped:
        lines.append(f"  No Security Update applied: {gdr_result.skip_reason}")
    else:
        lines.append("  No Security Update available.")
    lines.append("")

    lines.append("-" * 70)
    lines.append(f"Share root: {ctx.output_dir}")
    lines.append("")

    out_path = ctx.output_dir / INSTALL_ORDER_FILENAME
    out_path.write_text("\n".join(lines), encoding="utf-8")
    log.info("Wrote install guide: %s", out_path)


# --------------------------------------------------------------------------- #
# Main
# --------------------------------------------------------------------------- #


def resolve_output_dir(arg: str | None) -> Path:
    if arg:
        return Path(arg).expanduser().resolve()
    return Path(__file__).resolve().parent


def run(ctx: RunContext) -> int:
    prepare_patch_directory(ctx)
    scan_existing_patches(ctx)

    rows = scrape_build_rows(ctx)
    cu_target, gdr_target, target_cu = classify_targets(rows, ctx.n_minus_1)

    if cu_target is None:
        log.error("No Cumulative Update could be identified for %s.", ctx.label)
        write_install_order(ctx, None, None)
        return 1

    mode = "N-1" if ctx.n_minus_1 else "latest"
    log.info("Target CU (%s mode): CU%s %s", mode, target_cu, cu_target.kb)
    if gdr_target:
        log.info("Target GDR: %s", gdr_target.kb)
    else:
        log.info("No GDR security update available for CU%s.", target_cu)

    cu_result = process_cu(ctx, cu_target)

    gdr_result = None
    if gdr_target:
        gdr_result = process_gdr(ctx, gdr_target, cu_result)

    # Cleanup runs after downloads so a failed run leaves old patches in place.
    if not ctx.dry_run:
        keep: set[str] = set()
        if cu_result.folder_name and not cu_result.skipped:
            keep.add(cu_result.folder_name)
        if gdr_result and gdr_result.folder_name and not gdr_result.skipped:
            keep.add(gdr_result.folder_name)
        if keep:
            cleanup_old_folders(ctx, keep)
        else:
            log.warning("No patches downloaded successfully; skipping cleanup.")

    write_install_order(ctx, cu_result, gdr_result)

    if cu_result.skipped:
        return 1
    return 0


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    output_dir = resolve_output_dir(args.output_dir)

    setup_logging(output_dir, args.sql_version)

    info = VERSION_INFO[args.sql_version]
    log.info("=" * 70)
    log.info("SQL Server Patch Monitor starting.")
    log.info("Version: %s | Output: %s | N-1: %s | Dry run: %s",
             info["label"], output_dir, args.n_minus_1, args.dry_run)
    log.info("=" * 70)

    lock = LockFile(output_dir / f"sql{args.sql_version}_patch_monitor.lock")
    if not lock.acquire():
        return 2

    ctx = RunContext(
        sql_version=args.sql_version,
        label=info["label"],
        build_prefix=info["build_prefix"],
        output_dir=output_dir,
        n_minus_1=args.n_minus_1,
        dry_run=args.dry_run,
        session=build_session(),
    )

    try:
        return run(ctx)
    except Exception as exc:  # noqa: BLE001 — top-level guard for scheduled runs
        log.exception("Run failed: %s", exc)
        return 1
    finally:
        lock.release()
        log.info("SQL Server Patch Monitor finished.")


if __name__ == "__main__":
    sys.exit(main())
