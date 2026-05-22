SQL Server Patch Monitor
> ## ⚠️ Disclaimer
>
> This script is provided **"as is", without warranty of any kind**, express or
> implied. It downloads files from third‑party and Microsoft websites and writes
> to the local filesystem. **You use it entirely at your own risk.** The author
> and contributors accept **no responsibility or liability** for any damage,
> data loss, downtime, broken environments, incorrect patches, or any other
> adverse outcome resulting from its use. You are solely responsible for testing
> it in a non‑production environment, validating every patch it downloads, and
> confirming it is appropriate for your systems before relying on it. By running
> this script you accept full responsibility for the results.
---
What it does
Automates the daily chore of keeping a SQL Server patch staging folder current.
On each run it:
Prepares the output directory (creates it if missing).
Scans the folder for patches already downloaded.
Scrapes the community build reference page (`sqlserverbuilds.blogspot.com`)
for the latest Cumulative Update (CU) and Security Update (GDR).
Compares what's available against what's already present.
Downloads anything missing (files are validated before use).
Cleans up old patch folders so only the current set remains.
Writes a human‑readable `INSTALL_ORDER.txt` telling whoever builds a
server exactly which file to install first and second.
It handles "version drift" (when Microsoft's download page and the build
reference page disagree about the latest CU) and is safe to run repeatedly as
nothing is re‑downloaded if it's already present and valid.
---
Prerequisites
Python 3.10 or newer.
Windows: `winget install Python.Python.3.12` or the installer from
python.org — tick "Add python.exe to
PATH" during install.
Verify in a new terminal: `python --version`
Internet access from the machine to:
PyPI (one‑time, to auto‑install the Python libraries below)
`sqlserverbuilds.blogspot.com`, `support.microsoft.com`,
`catalog.update.microsoft.com`, and `microsoft.com/download`
Write access to the output folder you point it at.
You do not need to install any Python libraries by hand. On first run the
script automatically installs what it needs (`requests`, `beautifulsoup4`,
`lxml`). If that auto‑install is blocked (e.g. system‑wide Python without admin
rights), install them manually once:
```
python -m pip install requests beautifulsoup4 lxml
```
---
How to run
```
python sql_patch_monitor.py --sql-version <2019|2022|2025> [options]
```
Parameters
Parameter	Required	Description
`--sql-version`	Yes	Which SQL Server version to monitor. One of `2019`, `2022`, `2025`.
`--output-dir`	No	Folder where patches and `INSTALL_ORDER.txt` are written. Defaults to the script's folder.
`--n-1`	No	N‑1 mode: grab the previous CU (plus its GDR if one exists) instead of the latest.
`--dry-run`	No	Report what it would download without downloading anything.
Examples
```bat
:: Latest SQL Server 2022 CU + GDR, downloaded next to the script
python sql_patch_monitor.py --sql-version 2022

:: Latest SQL 2019 patches into a specific folder
python sql_patch_monitor.py --sql-version 2019 --output-dir C:\Admin\Scripts

:: SQL 2025, one CU behind the latest (N-1 patching policy)
python sql_patch_monitor.py --sql-version 2025 --n-1

:: See what would happen for 2022 without downloading anything
python sql_patch_monitor.py --sql-version 2022 --dry-run
```
---
Output
After a run, the output folder contains:
```
<output-dir>\
    INSTALL_ORDER.txt                                  <- read this first
    SQL2022-CU25-KB5079000-16.0.4250.1-2026-04-10\
        SQLServer2022-KB5079000-x64.exe
    SQL2022-CU25-GDR-KB5081477-16.0.4255.1-2026-05-20\ <- if a GDR exists
        SQLServer2022-KB5081477-x64.exe
```
`INSTALL_ORDER.txt` lists the CU and GDR with their KB numbers, build numbers,
release dates, and exact file paths, plus a prominent warning if version drift
was detected.
A rolling log (`sql<version>_patch_monitor.log`) and a `.lock` file are also
written to the output folder.
---
Scheduling (Windows Task Scheduler)
Field	Value
Program/script	`python`
Add arguments	`C:\Admin\Scripts\sql_patch_monitor.py --sql-version 2022`
Start in	`C:\Admin\Scripts`
Trigger	Daily (e.g. 6:00 AM)
Run as	An account with write access to the output folder
If already running	Do not start a new instance
The script writes a `.lock` file so a long download is never stacked on top of
by the next scheduled run.
---
Notes & limitations
The build reference page is community‑maintained and can lag 1–3 days behind a
new CU. Microsoft's download page can also lag 24–48 hours. The script detects
both situations and logs clear guidance ("re‑run in 24–48 hours").
File integrity is checked by download size only — no SHA hash verification.
Always validate downloaded patches before deploying them to production.