#!/usr/bin/env python3
"""
RHOAI Documentation Downloader

Discovers all available versions and chapter guides from docs.redhat.com,
tracks downloads with SHA256 hashes, and provides an interactive TUI for
selecting what to download or update.

Usage:
  python rhoai_docs.py                     # TUI mode (default)
  python rhoai_docs.py --check             # check for updates, no downloads
  python rhoai_docs.py --version 3.5       # jump straight to that version in TUI
  python rhoai_docs.py --all               # download all missing/changed non-interactively
  python rhoai_docs.py --format pdf        # override format (pdf|html-single|both)
  python rhoai_docs.py --output ~/my-docs  # override output directory
  python rhoai_docs.py --verify            # re-hash local files against the manifest

Update detection compares two upstream signals against the manifest: the PDF's
own /CreationDate (day granularity) and its byte size. Size is what catches a
"shadow update" — the same version silently re-published the same day. A file
being replaced is never overwritten: it moves to <version>/archive/ first,
named with the publication date it carried.
"""

import argparse
import calendar
import concurrent.futures
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Optional

try:
    import requests
    import yaml
    from bs4 import BeautifulSoup
    import questionary
    from questionary import Style
    from questionary.prompts.common import InquirerControl
    from rich.console import Console
    from rich.progress import Progress, BarColumn, TextColumn, TimeElapsedColumn
    from rich.table import Table
    from rich import box
except ImportError:
    print("Missing dependencies. Install with:")
    print("  pip install requests pyyaml beautifulsoup4 questionary rich")
    sys.exit(1)

console = Console()

# Serialises manifest writes between the UI thread and the background
# version check
MANIFEST_LOCK = threading.Lock()

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

BASE_URL = "https://docs.redhat.com/en/documentation/red_hat_openshift_ai_self-managed"

# The landing page is the site's least reliable URL: 0.4 s on a good day,
# 27 s or a 503 on a bad one. Short limit once there is a cached list to
# carry on with, patient on a first run, which has no fallback.
DISCOVERY_TIMEOUT = 8
DISCOVERY_TIMEOUT_FIRST_RUN = 30
# resolve(), so the shipped yaml is still found when the script is reached
# through a symlink on PATH — otherwise every setting silently falls back to
# the DEFAULT_* constants below.
SCRIPT_DIR = Path(__file__).resolve().parent
BUNDLED_CONFIG = SCRIPT_DIR / "rhoai_docs.yaml"
MANIFEST_FILENAME = "manifest.json"

# Used only when rhoai_docs.yaml is missing or omits the key — the shipped
# yaml sets both, so changing these alone will not move an existing setup
DEFAULT_OUTPUT_DIR = "~/rhoai-docs"
DEFAULT_FORMAT = "pdf"
# Comparing one version with upstream costs one request per guide — around a
# hundred for a full library, and minutes when the site is slow. A version
# compared this recently is left alone by the menu's background pass; opening
# it still re-checks before anything is downloaded. 0 disables the skip.
DEFAULT_RECHECK_HOURS = 1

CATEGORY_ORDER = [
    "What's New", "Early Access", "Install", "Get started", "Plan",
    "Administer", "Develop", "Train", "Evaluate", "Maintain Safety",
    "Monitor", "Deploy",
]

TUI_STYLE = Style([
    ("qmark",        "fg:#00aaff bold"),
    ("question",     "bold"),
    ("answer",       "fg:#00ff88 bold"),
    ("pointer",      "fg:#00aaff bold"),
    ("highlighted",  "fg:#00aaff bold"),
    ("selected",     "fg:#00ff88"),
    ("separator",    "fg:#555555"),
    # questionary stamps "- " in front of every disabled row; left unstyled
    # it renders in full-brightness default and shouts over the busy row's
    # own animation, so it is toned down to match the separators
    ("disabled",     "fg:#555555"),
    # The key hints are the only discoverable documentation for Esc/←/→, so
    # they get colour rather than the usual muted grey
    ("instruction",  "fg:#ffcc00"),
])

STATUS_ICON = {
    "ok":      "[green]✓[/green]",
    "changed": "[yellow]↺[/yellow]",
    "corrupt": "[red]![/red]",
    "missing": "[dim]✗[/dim]",
}
STATUS_PLAIN = {"ok": "✓", "changed": "↺", "corrupt": "!", "missing": "✗"}

# Statuses that mean "this file needs (re)fetching"
NEEDS_DOWNLOAD = ("missing", "changed", "corrupt")

# Where superseded copies are kept instead of being overwritten
ARCHIVE_DIRNAME = "archive"

# Generated per-version files. Leading underscore so they sort above the PDFs
# instead of being buried among guides starting with the same letter.
INDEX_FILENAME = "_INDEX.md"
CHANGELOG_FILENAME = "_CHANGELOG.md"

# One line per run, in the output directory root rather than per version
RUNLOG_FILENAME = "runs.log"

# The guide whose text names the current z-stream release
RELEASE_NOTES_SLUG = "release_notes"

# Manifest slot for the cached list of published versions
VERSIONS_KEY = "_versions"

# Menu sentinels. These must never be None: questionary.Choice treats
# value=None as "use the title as the value", so a Choice(value=None) comes
# back as its own label text and an `if answer is None` guard never fires.
# (.ask() does return None on its own when the user aborts with Ctrl-C.)
LIBRARY_CHOICE = "__library__"
BACK_CHOICE = "__back__"
# Prefix for navigable-but-inert tree rows
LEAF_PREFIX = "__leaf__"
# Returned when the menu closes itself to pick up new results
REDRAW_CHOICE = "__redraw__"
QUIT_CHOICE = "__quit__"
# The animated "still working" row; never selectable, so never returned
BUSY_CHOICE = "__busy__"

# Fixed column widths keep the progress bar from shifting left and right as
# descriptions and byte counts change length between files.
DESC_W = 46
DETAIL_W = 11

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

def config_path() -> Optional[Path]:
    """
    Which settings file is in force, most specific first:

        $RHOAI_DOCS_CONFIG  →  ~/.config/rhoai-docs/config.yaml  →  the
        rhoai_docs.yaml shipped beside the script

    The middle one is what lets a cloned checkout stay clean: settings live
    outside it, so `git pull` never collides with local choices.
    """
    override = os.environ.get("RHOAI_DOCS_CONFIG")
    xdg = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")
    for candidate in (Path(override).expanduser() if override else None,
                      xdg / "rhoai-docs" / "config.yaml",
                      BUNDLED_CONFIG):
        if candidate and candidate.is_file():
            return candidate
    return None


def load_config() -> dict:
    """
    Settings resolve in this order, most specific first:
        CLI flag  →  the file config_path() picks  →  the DEFAULT_* constants.
    The constants only apply when no file supplies the key.
    """
    defaults = {"output_dir": DEFAULT_OUTPUT_DIR,
                "format": DEFAULT_FORMAT,
                "recheck_after_hours": DEFAULT_RECHECK_HOURS,
                "versions": []}
    path = config_path()
    if path:
        with open(path) as f:
            user = yaml.safe_load(f) or {}
        defaults.update({k: v for k, v in user.items() if v is not None})
    return defaults


# ---------------------------------------------------------------------------
# HTTP helpers
# ---------------------------------------------------------------------------

SESSION = requests.Session()
SESSION.headers.update({"Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"})


class SiteHealth:
    """
    Tracks server-side failures across a run.

    docs.redhat.com returns 503 in bursts lasting minutes. Without this the
    tool ploughs on through dozens of doomed requests and reports the outage as
    dozens of unrelated per-file errors; with it, we stop and say so once.
    """
    ABORT_AFTER = 3

    def __init__(self):
        self.server_errors = 0
        self.last_code: Optional[int] = None
        self._lock = threading.Lock()

    def record(self, code: Optional[int]) -> None:
        """
        Count 5xx and connection failures; anything else resets the streak.

        Locked because up to 20 probe threads report at once — safe under
        the GIL, not under a free-threaded build.
        """
        with self._lock:
            if code is None or code >= 500:
                self.server_errors += 1
                self.last_code = code
            else:
                self.server_errors = 0

    @property
    def degraded(self) -> bool:
        return self.server_errors >= self.ABORT_AFTER

    @property
    def suspected(self) -> bool:
        """
        At least one server-side failure, which may be all there is: a run
        can die on its first request, never reaching the three that mean
        degraded.
        """
        return self.server_errors > 0

    def describe(self) -> str:
        what = (f"returning HTTP {self.last_code}" if self.last_code
                else "not answering")
        often = " repeatedly" if self.degraded else ""
        return (f"docs.redhat.com is {what}{often}. This is a "
                f"site-side outage, not a problem with your request or your "
                f"local files — nothing has been corrupted. Wait a few minutes "
                f"and run again; anything already downloaded will be skipped.")

    @staticmethod
    def describe_error(e: Exception) -> str:
        """
        A short reason fit for a status line.

        requests' own message embeds the full URL, which for these guides runs
        past 200 characters and wraps over three terminal lines per failure.
        """
        code = getattr(getattr(e, "response", None), "status_code", None)
        if code:
            return f"HTTP {code}"
        if isinstance(e, requests.Timeout):
            return "timed out"
        if isinstance(e, requests.ConnectionError):
            return "connection failed"
        return type(e).__name__

    def warn(self) -> None:
        console.print(f"\n[yellow]⚠ Upstream unavailable.[/yellow] {self.describe()}")


HEALTH = SiteHealth()


class RunLog:
    """
    One line per run of the script, newest first, in <output_dir>/runs.log.

    Where _CHANGELOG.md details what moved in one version, this is the whole
    run on one line, so "when did I last pull 3.4, and did anything fail?"
    is answerable by eye:

        2026-10-16 12:06:02  menu   nothing changed
        2026-10-16 12:04:11  check  checked 3.5, 3.4, 3.3; upstream 503
        2026-10-16 11:58:03  menu   downloaded 2.25 (28); release 3.4.3→3.4.4

    Counts only, in local time — what exactly moved is in _CHANGELOG.md,
    how big it is in _INDEX.md. Facts are recorded where they happen, since
    afterwards nothing on disk says which run touched what, and the line is
    written from quit_now(): that leaves via os._exit(), so an atexit hook
    would never fire. A run that cannot be logged still succeeds.
    """

    def __init__(self):
        self.mode = "menu"
        self.path: Optional[Path] = None
        self.files: dict = {}          # version -> per-kind file counts
        self.releases: dict = {}       # version -> (previous z, detected z)
        self.snapshots: dict = {}      # version -> z-stream frozen this run
        self.regroups: dict = {}       # version -> number of layout changes
        self.seen: list[str] = []      # versions looked at, in the order seen
        self.notes: list[str] = []     # whole-run remarks, e.g. "interrupted"
        self.written = False

    # -- collection ---------------------------------------------------------

    def begin(self, output_dir: Path, mode: str) -> None:
        self.path = output_dir / RUNLOG_FILENAME
        self.mode = mode

    def saw(self, version: str) -> None:
        if version not in self.seen:
            self.seen.append(version)

    def _tally(self, version: str) -> dict:
        self.saw(version)
        return self.files.setdefault(
            version, {"new": 0, "updated": 0, "repaired": 0, "failed": 0})

    def saved(self, version: str, status: str) -> None:
        """One file written. `status` is the pre-download state of that file."""
        kind = {"changed": "updated", "corrupt": "repaired"}.get(status, "new")
        self._tally(version)[kind] += 1

    def failed(self, version: str) -> None:
        self._tally(version)["failed"] += 1

    def release(self, version: str, previous: Optional[str], now: str) -> None:
        self.releases[version] = (previous, now)
        self.saw(version)

    def snapshot(self, version: str, z_version: str) -> None:
        self.snapshots[version] = z_version
        self.saw(version)

    def regrouped(self, version: str, changes: int) -> None:
        self.regroups[version] = self.regroups.get(version, 0) + changes
        self.saw(version)

    def note(self, text: str) -> None:
        if text not in self.notes:
            self.notes.append(text)

    # -- rendering ----------------------------------------------------------

    def _releases(self) -> list[str]:
        # A z-stream seen for the first time is only news for a version whose
        # files are actually held — otherwise every first run would list a
        # release for all six versions, which says nothing about this run.
        return [f"{prev}→{now}" if prev else now
                for v, (prev, now) in self.releases.items()
                if prev or v in self.files]

    def changed(self) -> bool:
        """Did this run actually move anything, as opposed to just looking?"""
        return bool(self.files or self.snapshots or self.regroups
                    or self._releases())

    def segments(self) -> list[str]:
        out = []
        for kind, word in (("new", "downloaded"), ("updated", "updated"),
                           ("repaired", "repaired"), ("failed", "failed")):
            got = [(v, t[kind]) for v, t in self.files.items() if t[kind]]
            if got:
                out.append(word + " " + ", ".join(
                    f"{v} ({n})" for v, n in got))

        rel = self._releases()
        if rel:
            out.append("release " + ", ".join(rel))
        if self.snapshots:
            out.append("froze " + ", ".join(self.snapshots.values()))
        if self.regroups:
            out.append("regrouped " + ", ".join(
                f"{v} ({n})" for v, n in self.regroups.items()))

        quiet = [v for v in self.seen if v not in self.files
                 and v not in self.regroups and v not in self.releases]
        if quiet:
            out.append("checked " + ", ".join(quiet))

        if HEALTH.degraded:
            out.append(f"upstream {HEALTH.last_code or 'unreachable'}")
        out.extend(self.notes)
        return out

    def body(self) -> str:
        """Everything but the timestamp — what two runs are compared on."""
        return (f"{self.mode:<5}  "
                f"{'; '.join(self.segments()) or 'nothing changed'}")

    def line(self) -> str:
        return f"{time.strftime('%Y-%m-%d %H:%M:%S')}  {self.body()}"

    def write(self) -> None:
        # Nothing is created just to hold a log: no output directory means a
        # run that never got as far as writing anything, and the same rule
        # append_changelog follows for version directories applies here.
        if self.written or self.path is None or not self.path.parent.is_dir():
            return
        self.written = True
        try:
            # Newest first means a rewrite, not an append — via tmp +
            # os.replace, so an interrupted write cannot truncate history.
            kept = (self.path.read_text().splitlines()
                    if self.path.exists() else [])
            if not self.changed():
                # A run that moved nothing replaces its own previous line
                # instead of repeating it, so "when did I last look and find
                # nothing?" stays answerable without burying the runs that
                # did something. Real changes are never merged.
                kept = [k for k in kept if k.partition("  ")[2] != self.body()]
            text = "\n".join([self.line()] + kept)
            tmp = self.path.with_name(self.path.name + ".tmp")
            tmp.write_text(text + "\n")
            os.replace(tmp, self.path)
        except OSError:
            pass            # a log that cannot be written must not end a run


RUN = RunLog()


def get_html(url: str, timeout: int = 30) -> str:
    try:
        resp = SESSION.get(url, timeout=timeout, allow_redirects=True)
    except requests.RequestException:
        HEALTH.record(None)
        raise
    HEALTH.record(resp.status_code)
    resp.raise_for_status()
    return resp.text


def extract_pdf_date(header: str) -> Optional[str]:
    """Pull a '/CreationDate (D:YYYYMMDD…' out of a PDF header blob."""
    m = re.search(r"/CreationDate\s*\(D:([0-9]{8})", header)
    if not m:
        return None
    raw = m.group(1)
    return f"{raw[:4]}-{raw[4:6]}-{raw[6:8]}"


def probe_upstream(url: str, timeout: int = 20) -> dict:
    """
    One 8 KB ranged request that yields BOTH change signals at once.

    docs.redhat.com honours Range and answers 206 with
    'Content-Range: bytes 0-8191/882285' — so the full upstream size arrives in
    the same response whose body we parse for the PDF CreationDate. No separate
    HEAD needed.

    Returns {"doc_date": str|None, "size": int|None}; both None on failure.
    """
    blank = {"doc_date": None, "size": None}
    try:
        resp = SESSION.get(url, headers={"Range": "bytes=0-8191"},
                           timeout=timeout, allow_redirects=True)
        HEALTH.record(resp.status_code)
        if resp.status_code not in (200, 206):
            return blank

        size = None
        crange = resp.headers.get("Content-Range")   # 'bytes 0-8191/882285'
        if crange and "/" in crange:
            tail = crange.rsplit("/", 1)[1].strip()
            if tail.isdigit():
                size = int(tail)
        if size is None and resp.status_code == 200:
            # Server ignored Range: Content-Length is then the whole file
            declared = resp.headers.get("Content-Length")
            if declared and declared.isdigit():
                size = int(declared)

        body = resp.content.decode("latin-1", errors="replace")
        return {"doc_date": extract_pdf_date(body), "size": size}
    except requests.RequestException:
        HEALTH.record(None)
        return blank


def pdf_date_from_file(path: Path) -> Optional[str]:
    """Same date extraction as probe_upstream, but from a local file — no request."""
    try:
        with open(path, "rb") as f:
            return extract_pdf_date(f.read(8192).decode("latin-1", errors="replace"))
    except OSError:
        return None


def looks_like_pdf(path: Path) -> bool:
    """
    docs.redhat.com soft-404s: a bogus PDF URL returns 200 with a ~1.7 MB HTML
    page, which raise_for_status() happily accepts. Check the magic bytes so an
    error page never lands on disk named .pdf.
    """
    try:
        with open(path, "rb") as f:
            return f.read(5) == b"%PDF-"
    except OSError:
        return False


def clip(text: str, width: int) -> str:
    return text if len(text) <= width else text[:width - 1] + "…"


def pad_desc(text: str) -> str:
    """Clip or pad to DESC_W so every progress row starts its bar in the same column."""
    return text[:DESC_W - 1] + "…" if len(text) > DESC_W else text.ljust(DESC_W)


def fmt_secs(seconds: float) -> str:
    if seconds < 60:
        return f"{seconds:.1f}s"
    return f"{int(seconds) // 60}m{int(seconds) % 60:02d}s"


def fmt_age(seconds: float) -> str:
    """Rounded down, coarsest unit that fits: 45m, 3h, 2d."""
    if seconds < 3600:
        return f"{int(seconds // 60)}m"
    if seconds < 86400:
        return f"{int(seconds // 3600)}h"
    return f"{int(seconds // 86400)}d"


def seconds_since(stamp: Optional[str]) -> Optional[float]:
    """Age of one of the manifest's UTC timestamps, or None if unreadable."""
    if not stamp:
        return None
    try:
        return max(0.0, time.time() - calendar.timegm(
            time.strptime(stamp, "%Y-%m-%dT%H:%M:%SZ")))
    except (ValueError, TypeError):
        return None


def last_compared(manifest: dict, version: str,
                  chapters: list[dict]) -> Optional[float]:
    """
    Seconds since this version was last compared with upstream, measured
    from its *oldest* tracked file: one guide never checked makes the whole
    version due again.
    """
    # PDF entries only: enrich_with_upstream stamps last_check on those and
    # nothing else, so an html-single library simply never qualifies to skip.
    ages = [seconds_since((manifest.get(manifest_key(version, ch["slug"], "PDF"))
                           or {}).get("last_check"))
            for ch in chapters
            if manifest.get(manifest_key(version, ch["slug"], "PDF"))]
    if not ages or any(a is None for a in ages):
        return None
    return max(ages)


def fmt_bytes(n: Optional[int]) -> str:
    if n is None:
        return "?"
    size = float(n)
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} GB"


def download_binary(url: str, dest: Path, timeout: int = 120,
                    on_start=None, on_progress=None,
                    prev_meta: Optional[dict] = None,
                    archive: bool = True) -> dict:
    """
    Download url to dest.

    on_start(total_bytes_or_None) fires once the response headers are in;
    on_progress(bytes_so_far) fires after every chunk. Returns the metadata
    needed for the manifest — including the ETag from this very response, so
    no extra HEAD request is needed afterwards.

    Writes to a sibling '.part' file and only os.replace()s it into place once
    the download completes and validates, so an interrupted transfer can never
    truncate the copy already on disk, nor leave a half-file that later runs
    would mistake for a complete one. Any copy being superseded is moved to
    <version>/archive/ first (see archive_existing); prev_meta is that copy's
    current manifest record, used to name it.
    """
    resp = SESSION.get(url, stream=True, timeout=timeout)
    resp.raise_for_status()

    declared = resp.headers.get("Content-Length")
    total = int(declared) if declared and declared.isdigit() else None
    if on_start:
        on_start(total)

    hasher = hashlib.sha256()
    dest.parent.mkdir(parents=True, exist_ok=True)
    part = dest.with_name(dest.name + ".part")
    written = 0
    try:
        with open(part, "wb") as f:
            for chunk in resp.iter_content(chunk_size=65536):
                f.write(chunk)
                hasher.update(chunk)
                written += len(chunk)
                if on_progress:
                    on_progress(written)

        if total is not None and written != total:
            raise IOError(f"truncated: got {written} of {total} bytes")
        if dest.suffix == ".pdf" and not looks_like_pdf(part):
            raise IOError("server returned a non-PDF body (soft-404 error page?)")

        archived = archive_existing(dest, prev_meta) if archive else None
        os.replace(part, dest)
    except BaseException:
        # Includes KeyboardInterrupt — never leave a .part behind
        part.unlink(missing_ok=True)
        raise

    return {
        "sha256": hasher.hexdigest(),
        "size": written,
        "etag": resp.headers.get("ETag"),
        "archived": archived,
    }


def archive_existing(dest: Path, prev_meta: Optional[dict],
                     tag: Optional[str] = None) -> Optional[dict]:
    """
    Move the current copy of dest into <version>/archive/ before it gets
    replaced, named with the publication date it carried, e.g.
      archive/release_notes_2026-09-18.pdf

    Falls back to the recorded download date, then the file's mtime, when no
    doc_date was ever captured. If that name is taken — a same-day shadow
    update, exactly the case upstream-size detection catches — a _N counter is
    appended before the extension, starting at 1:
      archive/release_notes_2026-09-18_1.pdf
      archive/release_notes_2026-09-18_2.pdf

    Returns the manifest record for the archived copy, or None if there was
    nothing to archive.
    """
    if not dest.exists():
        return None

    meta = prev_meta or {}
    stamp = meta.get("doc_date") or (meta.get("downloaded_at") or "")[:10]
    if not stamp:
        stamp = time.strftime("%Y-%m-%d", time.gmtime(dest.stat().st_mtime))

    archive_dir = dest.parent / ARCHIVE_DIRNAME
    archive_dir.mkdir(parents=True, exist_ok=True)
    # tag carries the z-stream for an archived index (_INDEX_3.5.1_2026-09-18.md)
    base = f"{dest.stem}_{tag}_{stamp}" if tag else f"{dest.stem}_{stamp}"
    target = archive_dir / f"{base}{dest.suffix}"
    n = 1
    while target.exists():
        # Count up rather than silently clobbering an already-archived copy
        target = archive_dir / f"{base}_{n}{dest.suffix}"
        n += 1

    os.replace(dest, target)
    return {
        "file": f"{ARCHIVE_DIRNAME}/{target.name}",
        "doc_date": meta.get("doc_date"),
        "sha256": meta.get("sha256"),
        "upstream_size": meta.get("upstream_size"),
        "downloaded_at": meta.get("downloaded_at"),
        "archived_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }


# ---------------------------------------------------------------------------
# Discovery
# ---------------------------------------------------------------------------

def discover_from_landing(timeout: int = DISCOVERY_TIMEOUT) -> tuple[str, list[str]]:
    """
    The current release and the full version list, from one page read: the
    landing page redirects to whatever Red Hat currently ships (the only
    authority on "latest") and the page it lands on carries the version menu.

    Only used when starting from nothing or under --rediscover — it is the
    site's least reliable URL, so a run that can avoid it does.
    """
    try:
        resp = SESSION.get(BASE_URL + "/", timeout=timeout, allow_redirects=True)
    except requests.RequestException:
        HEALTH.record(None)
        raise
    HEALTH.record(resp.status_code)
    resp.raise_for_status()
    m = re.search(r"/red_hat_openshift_ai_self-managed/([^/]+)", resp.url)
    if not m:
        raise RuntimeError(f"Could not extract version from redirect URL: {resp.url}")
    PAGE_CACHE[resp.url] = resp.text
    return m.group(1), picker_versions(resp.text)


def probe_version(ver: str) -> tuple[bool, Optional[int]]:
    """
    One HEAD request: does this version have a docs page of its own?
    Returns (exists, status_code) so callers can tell "no such version" apart
    from "the site is down" — a 503 is not a missing version.
    """
    try:
        resp = SESSION.head(f"{BASE_URL}/{ver}", timeout=10, allow_redirects=True)
        # 5xx only: these are speculative URLs for versions that mostly
        # do not exist, and the site tarpits a burst of misses rather than
        # 404ing them, so a hung probe is routine. Counting those as
        # outages marked a healthy site degraded and aborted the run.
        if resp.status_code >= 500:
            HEALTH.record(resp.status_code)
        # Reject redirects that landed on a different version or the base page
        return (resp.status_code == 200 and f"/{ver}" in resp.url), resp.status_code
    except requests.RequestException:
        return False, None


def version_probe_blocked(code: Optional[int]) -> bool:
    """A 5xx or a dead connection says nothing about whether the version exists."""
    return code is None or code >= 500


def version_exists(ver: str) -> bool:
    return probe_version(ver)[0]


# How far past the current release to look. After 3.5 the realistic next
# publications are 3.6 or 4.0 — 5.0 is not a thing that happens next, and the
# minors allow for a skipped release or two.
MINORS_AHEAD = 3
MAJORS_AHEAD = 1
# The 2.x docs that exist. 2.x is closed: 3.x has shipped, so the only future
# movement on this track is z-stream updates to 2.25 — which never get a URL of
# their own, they overwrite /2.25 in place and are caught by the normal
# size/date check. No new 2.x minor will ever appear, and an older one cannot
# come back once a newer is out, so this is a fixed list rather than a range.
# --rediscover re-sweeps the full historical range anyway, if that is ever wrong.
LEGACY_VERSIONS = ["2.16", "2.25"]
LEGACY_2X_MAX = 30     # only used by the exhaustive --rediscover sweep


def split_version(v: str) -> tuple[int, int]:
    major, minor = (int(x) for x in v.split(".")[:2])
    return major, minor


def ahead_candidates(latest: str) -> set[str]:
    """
    The not-yet-published versions worth checking for: the next few minors
    and the next major's first release.

    Deliberately short. Every miss is the slow path (0.2–1.5 s, since
    docs.redhat.com throttles bursts of 404s), and this only has to catch a
    version that is published but is not yet listed in the page's own
    version menu — which is where the list normally comes from, so this
    runs only when that menu cannot be read.
    """
    try:
        major, minor = split_version(latest)
    except (ValueError, IndexError):
        return set()
    ahead = {f"{major}.{minor + n}" for n in range(1, MINORS_AHEAD + 1)}
    ahead |= {f"{major + n}.0" for n in range(1, MAJORS_AHEAD + 1)}
    return ahead


def version_candidates(latest: str, exhaustive: bool = False) -> list[str]:
    """
    Cold-start probe list, derived from whatever upstream calls latest rather
    than a hardcoded ceiling (the original '3.0 … 3.10' would have silently
    missed 3.11 and every 4.x).

    Probing is needed at all because the docs page's own version switcher
    omits versions — it currently lists 3.2, 3.4 and 3.5 but not 3.3, 2.25 or
    2.16.

    Closed-out majors contribute their known published versions only, not a
    whole range of guesses. exhaustive=True (--rediscover) widens that back to
    every plausible minor, for the case where a new one appears against
    expectation.
    """
    try:
        major, minor = split_version(latest)
    except (ValueError, IndexError):
        major, minor = 3, 5          # a sane floor if the redirect surprises us

    if exhaustive:
        candidates = {f"2.{n}" for n in range(1, LEGACY_2X_MAX + 1)}
        for m in range(3, major):
            candidates |= {f"{m}.{n}" for n in range(0, 21)}
    else:
        candidates = set(LEGACY_VERSIONS)

    candidates |= {f"{major}.{n}" for n in range(0, minor + 1)}   # up to latest
    candidates |= ahead_candidates(latest)
    return sorted(candidates, key=split_version)


def frontier_candidates(latest: str, known: list[str]) -> list[str]:
    """
    The few versions worth re-probing when we already know what exists:
    a little past the newest minor, and the start of the next major.

    Everything already in `known` is skipped — a published docs version does
    not disappear, and if one ever did, loading its chapter list fails loudly.
    """
    return sorted(ahead_candidates(latest) - set(known), key=split_version)


def sort_versions(versions) -> list[str]:
    return sorted(set(versions), key=split_version, reverse=True)


def probe_versions(candidates: list[str], workers: int = 20) -> set[str]:
    found = set()
    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as ex:
        for ver, ok in zip(candidates, ex.map(version_exists, candidates)):
            if ok:
                found.add(ver)
    return found


# A version page is 365 KB and two things want it in the same run: the
# version menu, and that version's chapter list. Handed over once, then
# dropped, so nothing here goes stale across a long session.
PAGE_CACHE: dict[str, str] = {}


def picker_versions(html: str) -> list[str]:
    """The <select id="product_version"> options, newest first, or []."""
    picker = BeautifulSoup(html, "html.parser").select_one("#product_version")
    if not picker:
        return []
    return sort_versions(
        v for v in (o.get("value", "").strip() for o in picker.find_all("option"))
        if re.fullmatch(r"\d+(\.\d+)+", v))


def versions_from_picker(seed: str) -> list[str]:
    """
    Every published version, from the page's own <select id="product_version">
    — measured on 3.5, exactly what a full candidate sweep finds, in one
    0.3 s read instead of ~10 s of probing. Older pages were once said to
    list only a subset, hence the caller's union with what it already knew.

    Returns [] rather than raising, so callers can fall back to probing.
    """
    url = f"{BASE_URL}/{seed}"
    html = get_html(url, timeout=DISCOVERY_TIMEOUT)
    PAGE_CACHE[url] = html
    return picker_versions(html)


def discover_all_versions(latest: str, exhaustive: bool = False) -> list[str]:
    """
    Probe all plausible version URLs in parallel.

    The fallback for when the page's own version menu cannot be read. This
    is the slow path — docs.redhat.com throttles a burst of misses (a 404
    costs 0.2–1.5 s versus 0.05 s for a hit, some hang until they time out,
    and raising the worker count barely helps), so a full sweep runs well
    over ten seconds. See resolve_versions().
    """
    found = probe_versions(version_candidates(latest, exhaustive))
    found.add(latest)
    return sort_versions(found)


def resolve_versions(manifest: dict, output_dir: Path, latest: str,
                     force: bool = False,
                     picked: Optional[list[str]] = None) -> list[str]:
    """
    Return every known docs version, and record it in the manifest.

    Normally the caller has already read the list from the page's own
    version menu and passes it in. Probing candidate URLs is the fallback
    for when that menu cannot be read, and --rediscover forces it — the
    escape hatch if the menu is ever wrong or incomplete.
    """
    cache = manifest.get(VERSIONS_KEY, {})
    known = cache.get("found", [])

    if force:
        picked = []
    elif picked is None:
        try:
            picked = versions_from_picker(latest)
        except requests.RequestException:
            picked = []                 # fall through to probing

    swept = False
    if picked:
        # Unioned with what was already known: an older page's menu may
        # list less than the newest one does, and a version tracked before
        # must not silently vanish because a menu stopped mentioning it.
        versions = sort_versions(set(picked) | set(known) | {latest})
        new = set(picked) - set(known) if known else set()
    elif force or not known:
        versions = discover_all_versions(latest, exhaustive=force)
        swept, new = True, set()
    else:
        new = probe_versions(frontier_candidates(latest, known))
        versions = sort_versions(set(known) | new | {latest})
    if new:
        console.print(f"  [green]New version(s) published:[/green] "
                      f"[bold]{', '.join(sort_versions(new))}[/bold]")

    now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    manifest[VERSIONS_KEY] = {
        "found": versions,
        "latest": latest,
        "checked_at": now,
        "full_sweep_at": now if swept else cache.get("full_sweep_at"),
    }
    save_manifest(output_dir, manifest)
    return versions


def discover_chapters(version: str) -> list[dict]:
    """Return chapters as [{"slug", "title", "category"}, ...]."""
    url = f"{BASE_URL}/{version}"
    html = PAGE_CACHE.pop(url, None) or get_html(url)
    soup = BeautifulSoup(html, "html.parser")

    link_pattern = re.compile(
        rf"/documentation/red_hat_openshift_ai_self-managed/{re.escape(version)}/html/([^/\"' ]+)"
    )

    category_map: dict[str, str] = {}
    for section in soup.find_all(["section", "div", "article"]):
        header = section.find(["h2", "h3", "h4"])
        if not header:
            continue
        category = header.get_text(strip=True)
        if category not in CATEGORY_ORDER:
            continue
        for a in section.find_all("a", href=True):
            m = link_pattern.search(a["href"])
            if m:
                category_map[m.group(1)] = category

    chapters = []
    seen: set[str] = set()
    for a in soup.find_all("a", href=True):
        m = link_pattern.search(a["href"])
        if not m:
            continue
        slug = m.group(1)
        if slug in seen:
            continue
        seen.add(slug)
        title = a.get_text(strip=True) or slug.replace("_", " ").title()
        chapters.append({"slug": slug, "title": title,
                         "category": category_map.get(slug, "Other")})
    return chapters


def build_urls(version: str, slug: str, fmt: str) -> list[dict]:
    entries = []
    base = f"{BASE_URL}/{version}"
    if fmt in ("pdf", "both"):
        entries.append({"label": "PDF",
                        "url": f"{base}/pdf/{slug}/{slug}.pdf",
                        "filename": f"{slug}.pdf"})
    if fmt in ("html-single", "both"):
        entries.append({"label": "HTML",
                        "url": f"{base}/html-single/{slug}/index",
                        "filename": f"{slug}.html"})
    return entries


# ---------------------------------------------------------------------------
# Manifest
# ---------------------------------------------------------------------------

def load_manifest(output_dir: Path) -> dict:
    path = output_dir / MANIFEST_FILENAME
    if path.exists():
        with open(path) as f:
            return json.load(f)
    return {}


def save_manifest(output_dir: Path, manifest: dict) -> None:
    """
    Write-and-replace, under a lock.

    Atomic because this is the file everything else is reconciled against: a
    crash or Ctrl-C part-way through a plain overwrite would leave truncated
    JSON, and every downloaded file would then read as unrecorded. The lock
    keeps the background version check from writing underneath the UI thread.
    """
    output_dir.mkdir(parents=True, exist_ok=True)
    path = output_dir / MANIFEST_FILENAME
    tmp = path.with_name(path.name + ".tmp")
    with MANIFEST_LOCK:
        with open(tmp, "w") as f:
            json.dump(manifest, f, indent=2)
            f.write("\n")
        os.replace(tmp, path)


def manifest_key(version: str, slug: str, label: str) -> str:
    return f"{version}/{slug}/{label}"


# ---------------------------------------------------------------------------
# Status checking
# ---------------------------------------------------------------------------

def compute_sha256(path: Path) -> str:
    hasher = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            hasher.update(chunk)
    return hasher.hexdigest()


def check_chapter_status(output_dir: Path, manifest: dict, version: str,
                         slug: str, fmt: str, verify: bool = False) -> list[dict]:
    """
    Return per-file status entries for a chapter.

    Purely local: file existence, byte size against the manifest, and — only
    with verify=True — a full SHA256 re-hash. No network calls; upstream
    comparison happens later in enrich_with_upstream().

    A file present on disk with no manifest entry counts as 'missing', not
    'ok': that is what a download interrupted before its manifest write looks
    like, and treating it as complete is how a truncated PDF used to become
    permanent.
    """
    results = []
    for entry in build_urls(version, slug, fmt):
        key = manifest_key(version, slug, entry["label"])
        dest = output_dir / version / entry["filename"]
        recorded = manifest.get(key, {})
        local_size = None

        if not dest.exists():
            status = "missing"
        elif not recorded:
            status = "missing"          # unrecorded leftover — re-fetch it
        else:
            local_size = dest.stat().st_size
            if local_size != recorded["upstream_size"]:
                status = "corrupt"      # truncated or damaged on disk
            elif verify and compute_sha256(dest) != recorded["sha256"]:
                status = "corrupt"
            else:
                status = "ok"

        # Three distinct sizes travel together from here on:
        #   recorded_size — what the manifest says upstream served us
        #   local_size    — what is actually on disk right now
        #   upstream_size — what upstream serves today (set by enrich_with_upstream)
        results.append({**entry, "dest": dest, "status": status,
                        "doc_date": recorded.get("doc_date"),
                        "recorded_size": recorded.get("upstream_size"),
                        "local_size": local_size,
                        "last_check": recorded.get("last_check")})
    return results


def enrich_with_upstream(chapters: list[dict], statuses: dict,
                         manifest: dict, output_dir: Path,
                         version: str) -> bool:
    """
    Probe every chapter in parallel (one 8 KB ranged request each) and compare
    upstream against the manifest on two signals:

      * publication date — the PDF's own /CreationDate, day granularity
      * byte size        — from the response's Content-Range header

    Size is what catches a 'shadow update': the same version re-published with
    different content on the same day, which the date alone cannot see. Either
    signal differing marks the file 'changed'.

    Also stamps last_check on every probed manifest entry and persists it, so
    the manifest records when each file was last compared with upstream.
    Returns True if any chapter is now marked changed.
    """
    pdf_chapters = [ch for ch in chapters
                    if any(s["label"] == "PDF" for s in statuses.get(ch["slug"], []))]

    def probe_one(ch: dict) -> tuple[str, dict]:
        slug = ch["slug"]
        url = f"{BASE_URL}/{version}/pdf/{slug}/{slug}.pdf"
        return slug, probe_upstream(url)

    now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    any_changed = False
    touched = False

    with concurrent.futures.ThreadPoolExecutor(max_workers=10) as ex:
        for slug, up in ex.map(probe_one, pdf_chapters):
            key = manifest_key(version, slug, "PDF")
            recorded = manifest.get(key, {})
            for s in statuses.get(slug, []):
                if s["label"] != "PDF":
                    continue
                s["doc_date"] = up["doc_date"] or s.get("doc_date")
                s["upstream_size"] = up["size"]

                # 'ok' implies a manifest entry exists, so read it directly
                if s["status"] == "ok":
                    date_moved = (up["doc_date"] is not None
                                  and recorded["doc_date"] is not None
                                  and up["doc_date"] != recorded["doc_date"])
                    size_moved = (up["size"] is not None
                                  and up["size"] != recorded["upstream_size"])
                    if date_moved or size_moved:
                        s["status"] = "changed"
                        s["change_reason"] = ("size" if size_moved and not date_moved
                                              else "date")
                        any_changed = True

            if recorded and (up["doc_date"] or up["size"]):
                recorded["last_check"] = now
                touched = True

    if touched:
        save_manifest(output_dir, manifest)
    return any_changed


def worst_status(ch_statuses: list[dict]) -> str:
    """Aggregate a chapter's per-format statuses into the most urgent one."""
    for candidate in ("missing", "corrupt", "changed"):
        if any(s["status"] == candidate for s in ch_statuses):
            return candidate
    return "ok"


def version_aggregate(chapters: list[dict], statuses: dict) -> dict:
    """Return {total, ok, changed, corrupt, missing} counts for a version."""
    counts = {"total": len(chapters), "ok": 0, "changed": 0,
              "corrupt": 0, "missing": 0}
    for ch in chapters:
        for s in statuses.get(ch["slug"], []):
            counts[s["status"]] = counts.get(s["status"], 0) + 1
    return counts


# ---------------------------------------------------------------------------
# Page structure: grouping of guides into categories
# ---------------------------------------------------------------------------

def meta_key(version: str) -> str:
    """Manifest slot for version-level data, alongside the per-file entries."""
    return f"{version}/_meta"


def detect_zstream(version: str) -> Optional[str]:
    """
    Read the actual z-stream release (e.g. '3.5.1') out of the release notes.

    docs.redhat.com publishes no z-stream URLs at all — /3.5.1 is a 404, and a
    z-stream release silently overwrites the /3.5 docs in place. The only place
    the real version is stated is inside the release notes text.

    Uses the per-chapter HTML (~384 KB) rather than html-single (~1.1 MB); both
    carry it, and Range is ignored on these HTML endpoints so a partial fetch
    is not possible. Callers should only invoke this when the release notes
    actually changed — the z-stream cannot move without them moving.
    """
    url = f"{BASE_URL}/{version}/html/{RELEASE_NOTES_SLUG}/index"
    try:
        html = get_html(url, timeout=60)
    except requests.RequestException:
        return None
    found = re.findall(rf"{re.escape(version)}\.(\d+)", html)
    if not found:
        return None
    return f"{version}.{max(int(z) for z in found)}"


def snapshot_structure(chapters: list[dict]) -> dict:
    return {ch["slug"]: {"title": ch["title"], "category": ch["category"]}
            for ch in chapters}


def diff_structure(before: dict, after: dict) -> dict:
    """
    Compare two snapshots of how the docs page groups its guides.

    Red Hat reshuffles this within a single version — a guide can move to
    another category, be retitled, appear or disappear — with nothing in any
    PDF changing, so none of the per-file signals would notice.
    """
    added = sorted(s for s in after if s not in before)
    removed = sorted(s for s in before if s not in after)
    moved, retitled = [], []
    for slug in sorted(set(before) & set(after)):
        if before[slug]["category"] != after[slug]["category"]:
            moved.append((slug, before[slug]["category"], after[slug]["category"]))
        if before[slug]["title"] != after[slug]["title"]:
            retitled.append((slug, before[slug]["title"], after[slug]["title"]))
    return {"added": added, "removed": removed,
            "moved": moved, "retitled": retitled}


def has_changes(diff: dict) -> bool:
    return any(diff.values())


def refresh_zstream(output_dir: Path, version: str, manifest: dict,
                    events: list, release_notes_changed: bool) -> Optional[str]:
    """
    Keep the recorded z-stream release current, and log when it advances.

    Only hits the network when the release notes actually changed, or when no
    z-stream has ever been recorded — otherwise this would cost a 384 KB fetch
    on every single run to learn nothing.
    """
    meta = manifest.setdefault(meta_key(version), {})
    if not release_notes_changed and meta.get("z_version"):
        return meta["z_version"]

    detected = detect_zstream(version)
    if not detected:
        return meta.get("z_version")

    previous = meta.get("z_version")
    if previous != detected:
        RUN.release(version, previous, detected)
        now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        if previous:
            console.print(f"  [yellow]z-stream release: {previous} → "
                          f"[bold]{detected}[/bold][/yellow]")
            events.append(f"**z-stream release: {previous} → {detected}**")
        else:
            console.print(f"  [dim]z-stream release: {detected}[/dim]")
            events.append(f"z-stream release identified as {detected}")
        meta["z_history"] = list(meta.get("z_history", [])) + \
            [{"z_version": detected, "first_seen": now}]
    meta["z_version"] = detected
    meta["z_checked_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    save_manifest(output_dir, manifest)
    return detected


def snapshot_zstream(output_dir: Path, version: str, z_version: str,
                     events: list) -> Optional[Path]:
    """
    Freeze the current contents of <version>/ as <z_version>/ before a newer
    z-stream overwrites them, e.g. 3.5/ → 3.5.1/.

    Uses hardlinks, so an unchanged guide costs one directory entry and zero
    bytes; only genuinely new files consume space. This is safe precisely
    because downloads land via os.replace() — that swaps the directory entry
    and leaves the old inode (and therefore the snapshot) untouched. It would
    silently corrupt every snapshot if files were ever written in place.

    Note `du` will report each snapshot at full size; `du -shc --count-links=no`
    gives the real figure.
    """
    source = output_dir / version
    target = output_dir / z_version
    if target.exists() or not source.is_dir():
        return None

    files = sorted(p for p in source.iterdir() if p.is_file())
    if not files:
        return None

    target.mkdir(parents=True)
    linked = copied = 0
    for src in files:
        dst = target / src.name
        # _INDEX.md is rewritten and _CHANGELOG.md is appended to on later
        # runs. A hardlink would make those writes reach through and mutate
        # the frozen copy, so the generated files get real copies.
        if src.name.startswith("_"):
            shutil.copy2(src, dst)
            copied += 1
            continue
        try:
            os.link(src, dst)
            linked += 1
        except OSError:
            # Different filesystem, or one that cannot hardlink
            shutil.copy2(src, dst)
            copied += 1

    note = f"{linked} hardlinked" + (f", {copied} copied" if copied else "")
    console.print(f"  [green]✓[/green] froze previous release as "
                  f"[bold]{z_version}/[/bold] [dim]({note})[/dim]")
    events.append(f"snapshot: previous release frozen as `{z_version}/` ({note})")
    RUN.snapshot(version, z_version)
    return target


def append_changelog(output_dir: Path, version: str, events: list) -> None:
    """
    Append this run's changes to <version>/_CHANGELOG.md.

    Append-only and never rewritten, unlike _INDEX.md which always describes
    only the current state — this is the one place the whole history lands.
    """
    version_dir = output_dir / version
    # Never bring a version directory into existence just to log something.
    # refresh_zstream resolves the z-stream for versions you do not hold (so
    # the picker can show it), and that alone must not litter the output
    # directory with empty folders — same rule refresh_structure follows.
    if not events or not version_dir.is_dir():
        return
    path = version_dir / CHANGELOG_FILENAME

    if not path.exists():
        path.write_text(
            f"# Red Hat OpenShift AI {version} — change log\n\n"
            f"Appended by `rhoai_docs.py` whenever something moves upstream: a "
            f"z-stream release, an updated guide, or a change to how the docs "
            f"page groups its guides. Newest entries at the bottom.\n")

    stamp = time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime())
    with open(path, "a") as f:
        f.write(f"\n## {stamp}\n\n")
        for line in events:
            f.write(f"- {line}\n")


def ordered_categories(chapters: list[dict]) -> list[str]:
    present = {ch["category"] for ch in chapters}
    cats = [c for c in CATEGORY_ORDER if c in present]
    return cats + sorted(c for c in present if c not in CATEGORY_ORDER)


def guide_tree_lines(chapters: list[dict], statuses: dict) -> list[str]:
    """The category → guide tree as plain lines, for _INDEX.md."""
    return [line for line, _ in guide_tree_entries(chapters, statuses)]


def guide_tree_entries(chapters: list[dict],
                       statuses: dict) -> list[tuple[str, Optional[str]]]:
    """
    The category → guide tree, shared by _INDEX.md and the TUI's library view
    so the two can never drift apart. Each row is paired with the file it
    describes, or None for a category heading, so the menu can open it.
    """
    cats = ordered_categories(chapters)
    by_cat: dict[str, list[dict]] = {}
    for ch in chapters:
        by_cat.setdefault(ch["category"], []).append(ch)

    lines: list[tuple[str, Optional[str]]] = []
    for ci, cat in enumerate(cats):
        last_cat = ci == len(cats) - 1
        lines.append((f"{'└──' if last_cat else '├──'} {cat}", None))
        guides = by_cat[cat]
        for gi, ch in enumerate(guides):
            stem = "    " if last_cat else "│   "
            branch = "└──" if gi == len(guides) - 1 else "├──"
            st = statuses.get(ch["slug"], [])
            pdf = next((s for s in st if s["label"] == "PDF"), None)
            icon = STATUS_PLAIN[worst_status(st)] if st else " "
            name = (pdf or st[0])["filename"] if st else ch["slug"]
            date = (pdf or {}).get("doc_date") or ""
            size = fmt_bytes((pdf or {}).get("recorded_size"))
            # Filename last and never truncated: two guides in the same
            # category can share a long prefix and only differ at the end.
            lines.append((f"{stem}{branch} {icon} {clip(ch['title'], 52):<52}  "
                          f"{date:>10} {size:>10}  {name}",
                          name if st else None))
    return lines


def dated_span(chapters: list[dict], statuses: dict) -> str:
    """
    One line naming the oldest and newest guide behind the date range, so the
    span is attributable rather than a bare pair of dates. Worth spelling out:
    the newest is often the next version's Early Access notes, which ship
    inside the current version's docs.
    """
    dated = []
    for ch in chapters:
        pdf = next((s for s in statuses.get(ch["slug"], [])
                    if s["label"] == "PDF" and s.get("doc_date")), None)
        if pdf:
            dated.append((pdf["doc_date"], ch["title"]))
    if not dated:
        return ""
    dated.sort()
    (old_date, old_title), (new_date, new_title) = dated[0], dated[-1]
    if old_date == new_date:
        return f"All guides published {old_date}.\n"
    return (f"Guides published **{old_date} → {new_date}** — oldest: "
            f"{old_title} · newest: {new_title}.\n")


def write_index(output_dir: Path, version: str, chapters: list[dict],
                statuses: dict, history: Optional[list[dict]] = None,
                z_version: Optional[str] = None) -> Path:
    """
    Write <version>/INDEX.md: the docs page's own category grouping as a tree,
    since the PDFs themselves land in one flat directory with no hint of it.
    """
    version_dir = output_dir / version
    now = time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime())
    counts = version_aggregate(chapters, statuses)

    lines = [
        f"# Red Hat OpenShift AI {version} — documentation index",
        "",
        f"Generated {now} by `rhoai_docs.py` — do not edit, it is overwritten "
        f"on every run.",
        "",
        f"Source: <{BASE_URL}/{version}>",
        "",
        (f"Release: **{z_version}** — Red Hat publishes no per-z-stream docs "
         f"URL, so this directory always holds the newest z-stream of "
         f"{version}.\n" if z_version else ""),
        dated_span(chapters, statuses),
        f"{len(chapters)} guides in {len(ordered_categories(chapters))} categories "
        f"· {counts['ok']} current"
        + (f" · {counts['changed']} updated upstream" if counts["changed"] else "")
        + (f" · {counts['corrupt']} damaged" if counts["corrupt"] else "")
        + (f" · {counts['missing']} not downloaded" if counts["missing"] else ""),
        "",
        "```",
        f"{version}/",
    ]

    lines += guide_tree_lines(chapters, statuses)
    lines += ["```", "",
              "Legend: `✓` current · `↺` updated upstream · `!` local file "
              "damaged · `✗` not downloaded", ""]

    if history:
        titles = {ch["slug"]: ch["title"] for ch in chapters}
        lines += ["## Page structure history", "",
                  "Every time Red Hat reshuffles this version's page — a guide "
                  "moving category, being retitled, appearing or disappearing — "
                  "it is recorded here, newest first. The index as it stood "
                  "before each change is kept in `archive/`.", ""]
        for event in reversed(history):
            lines.append(f"### {event['at']}")
            if event.get("previous_index"):
                lines.append(f"Previous index: `{event['previous_index']}`")
            lines.append("")
            for slug in event.get("added", []):
                lines.append(f"- **added** — {titles.get(slug, slug)} (`{slug}`)")
            for slug in event.get("removed", []):
                lines.append(f"- **removed** — `{slug}`")
            for slug, old, new in event.get("moved", []):
                lines.append(f"- **moved** — {titles.get(slug, slug)}: {old} → {new}")
            for slug, old, new in event.get("retitled", []):
                lines.append(f"- **retitled** — `{slug}`: \"{old}\" → \"{new}\"")
            lines.append("")

    version_dir.mkdir(parents=True, exist_ok=True)
    path = version_dir / INDEX_FILENAME
    # Write-and-replace rather than truncate-in-place, so a hardlinked
    # snapshot of this file keeps its own content.
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text("\n".join(lines))
    os.replace(tmp, path)
    return path


def refresh_structure(output_dir: Path, version: str, chapters: list[dict],
                      statuses: dict, manifest: dict,
                      events: Optional[list] = None) -> None:
    """
    Diff the page's grouping against the last run, report it, persist the new
    snapshot and rewrite _INDEX.md. Skipped for versions with nothing on disk,
    so a broad --check does not litter empty directories.
    """
    if not (output_dir / version).is_dir():
        return

    events = events if events is not None else []
    meta = manifest.setdefault(meta_key(version), {})
    previous = meta.get("chapters", {})
    history = list(meta.get("changes", []))
    current = snapshot_structure(chapters)
    diff = diff_structure(previous, current) if previous else None
    now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    z_version = meta.get("z_version")

    if diff and has_changes(diff):
        console.print(f"  [yellow]Page structure changed for v{version}:[/yellow]")
        RUN.regrouped(version, sum(len(diff[k]) for k in
                                   ("added", "removed", "moved", "retitled")))
        titles = {ch["slug"]: ch["title"] for ch in chapters}
        for slug in diff["added"]:
            console.print(f"    [green]+[/green] new guide: {titles.get(slug, slug)}")
            events.append(f"structure: added — {titles.get(slug, slug)} (`{slug}`)")
        for slug in diff["removed"]:
            console.print(f"    [red]-[/red] removed: {slug}")
            events.append(f"structure: removed — `{slug}`")
        for slug, old, new in diff["moved"]:
            console.print(f"    [cyan]~[/cyan] moved: {titles.get(slug, slug)}: "
                          f"{old} → {new}")
            events.append(f"structure: moved — {titles.get(slug, slug)}: "
                          f"{old} → {new}")
        for slug, old, new in diff["retitled"]:
            console.print(f"    [cyan]~[/cyan] retitled: {old} → {new}")
            events.append(f"structure: retitled — \"{old}\" → \"{new}\"")

        # Keep the superseded index, exactly as PDFs are kept: the old file IS
        # the previous structure, in readable form. Tagged with the z-stream it
        # described, since that is what makes one index distinguishable.
        index_path = output_dir / version / INDEX_FILENAME
        archived = archive_existing(
            index_path, {"doc_date": (meta.get("captured_at") or now)[:10]},
            tag=z_version)
        if archived:
            console.print(f"    [dim]→ archived {Path(archived['file']).name}[/dim]")
            events.append(f"previous index kept as `{archived['file']}`")
        history.append({"at": now, **diff, "z_version": z_version,
                        "previous_index": archived["file"] if archived else None})

    meta.update({"captured_at": now, "chapters": current, "changes": history})
    save_manifest(output_dir, manifest)
    write_index(output_dir, version, chapters, statuses, history, z_version)


# ---------------------------------------------------------------------------
# Download (with rich progress bar)
# ---------------------------------------------------------------------------

def run_downloads(to_download: list[tuple[dict, list[dict]]],
                  version: str, manifest: dict, output_dir: Path,
                  events: Optional[list] = None) -> list[str]:
    """
    Download selected chapters, showing a rich progress bar.
    Returns the slugs successfully written, and appends one changelog line per
    file to `events`.
    """
    events = events if events is not None else []
    # Flatten to individual file entries
    entries = []
    for ch, ch_statuses in to_download:
        for s in ch_statuses:
            if s["status"] in NEEDS_DOWNLOAD:
                entries.append((ch, s))

    if not entries:
        console.print("[dim]Nothing to download.[/dim]")
        return []

    console.print()
    downloaded: list[str] = []
    done = 0
    failed = 0
    started = time.monotonic()
    with Progress(
        TextColumn("[bold blue]{task.description}"),
        # Flexible width: fills whatever the terminal allows, so nothing gets
        # clipped on a narrow window. Row-to-row alignment still holds because
        # every other column is fixed width.
        BarColumn(bar_width=None),
        TextColumn("[progress.percentage]{task.percentage:>3.0f}%"),
        TextColumn("[dim]{task.fields[detail]}"),
        TimeElapsedColumn(),
        console=console,
        transient=False,
        refresh_per_second=10,
    ) as progress:
        overall = progress.add_task(
            pad_desc(f"Downloading {len(entries)} file(s) for v{version}"),
            total=len(entries),
            detail=f"0/{len(entries)} files".ljust(DETAIL_W),
        )
        # ONE reusable row for the file in flight. Adding and removing a task
        # per file is what made the display jump: every add/remove reflowed
        # the whole live region.
        current = progress.add_task("", total=1, detail="".ljust(DETAIL_W))

        for ch, entry in entries:
            label = pad_desc(f"{entry['label']:4s}  {ch['title']}")
            progress.reset(current, total=1, completed=0,
                           description=label, detail="".ljust(DETAIL_W))
            file_started = time.monotonic()
            key = manifest_key(version, ch["slug"], entry["label"])
            prev_meta = manifest.get(key)
            try:
                result = download_binary(
                    entry["url"], entry["dest"],
                    on_start=lambda total: progress.update(current, total=total),
                    on_progress=lambda n: progress.update(
                        current, completed=n,
                        detail=fmt_bytes(n).rjust(DETAIL_W)),
                    prev_meta=prev_meta,
                    # Archive is for copies genuinely superseded upstream. A
                    # damaged local file is junk, not history — don't keep it.
                    archive=(entry["status"] == "changed"),
                )
                # Close the bar out at the real size, in case the server's
                # Content-Length was absent or disagreed.
                progress.update(current, total=result["size"],
                                completed=result["size"],
                                detail=fmt_bytes(result["size"]).rjust(DETAIL_W))
                elapsed = time.monotonic() - file_started

                stamp = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
                # Carry the archive history forward; the rest of the entry is
                # replaced wholesale so it always describes the live file.
                history = list((prev_meta or {}).get("archived", []))
                if result["archived"]:
                    history.append(result["archived"])
                manifest[key] = {
                    "sha256": result["sha256"],
                    "upstream_size": result["size"],
                    "doc_date": (pdf_date_from_file(entry["dest"])
                                 if entry["label"] == "PDF" else None),
                    "downloaded_at": stamp,
                    "last_check": stamp,
                    "upstream_etag": result["etag"],
                    "url": entry["url"],
                }
                if history:
                    manifest[key]["archived"] = history
                save_manifest(output_dir, manifest)
                done += 1
                downloaded.append(ch["slug"])

                RUN.saved(version, entry["status"])

                was = (prev_meta or {}).get("upstream_size")
                if entry["status"] == "changed":
                    events.append(f"updated: {ch['title']} "
                                  f"({fmt_bytes(was)} → {fmt_bytes(result['size'])})")
                elif entry["status"] == "corrupt":
                    events.append(f"repaired damaged local copy: {ch['title']}")
                else:
                    events.append(f"downloaded: {ch['title']} "
                                  f"({fmt_bytes(result['size'])})")

                # Just the fact, not the name — archived filenames run long
                # enough to wrap the line and break the column alignment.
                note = "  [dim]→ archived[/dim]" if result["archived"] else ""
                # Scrolls above the live region, so each finished file keeps a
                # permanent line showing its own size and download time.
                progress.console.print(
                    f"  [green]✓[/green] {label}"
                    f"[dim]{fmt_bytes(result['size']).rjust(DETAIL_W)}"
                    f"{fmt_secs(elapsed).rjust(8)}[/dim]{note}",
                    highlight=False)
            except (requests.RequestException, OSError) as e:
                failed += 1
                RUN.failed(version)
                code = getattr(getattr(e, "response", None), "status_code", None)
                if isinstance(e, requests.RequestException):
                    HEALTH.record(code)
                progress.console.print(
                    f"  [red]✗[/red] {label}"
                    f"[red]{HEALTH.describe_error(e):>{DETAIL_W}}[/red]",
                    highlight=False)
                if HEALTH.degraded:
                    progress.console.print(
                        f"  [yellow]Stopping after {HEALTH.server_errors} "
                        f"server errors — the remaining "
                        f"{len(entries) - done - failed} file(s) would almost "
                        f"certainly fail too.[/yellow]")
                    progress.advance(overall)
                    progress.update(
                        overall,
                        detail=f"{done + failed}/{len(entries)} files".ljust(DETAIL_W))
                    break
            progress.advance(overall)
            progress.update(overall,
                            detail=f"{done + failed}/{len(entries)} files".ljust(DETAIL_W))

        progress.remove_task(current)

    took = fmt_secs(time.monotonic() - started)
    if done:
        summary = (f"[green]✓[/green] Saved {done} file(s) in {took} to "
                   f"[bold]{output_dir / version}[/bold]")
        if failed:
            summary += f"  [red]({failed} failed)[/red]"
    else:
        # Don't put a green tick on a run where nothing was saved
        summary = (f"[red]✗[/red] Nothing downloaded — {failed} file(s) failed "
                   f"in {took}. Your existing files are untouched.")
    console.print(summary)
    if HEALTH.degraded:
        HEALTH.warn()
    return downloaded


def download_with_snapshot(to_download: list[tuple[dict, list[dict]]],
                           version: str, manifest: dict, output_dir: Path,
                           events: list) -> list[str]:
    """
    Order of operations matters here:

      1. See whether the release notes are among the files about to change.
      2. If so, ask upstream what z-stream it is serving now.
      3. If that is newer than what we hold, freeze the CURRENT directory as
         <old z-stream>/ — this has to happen before anything is overwritten.
      4. Only then download.
    """
    release_notes_changing = any(
        ch["slug"] == RELEASE_NOTES_SLUG
        and any(s["status"] in NEEDS_DOWNLOAD for s in st)
        for ch, st in to_download)

    meta = manifest.setdefault(meta_key(version), {})
    held = meta.get("z_version")
    if release_notes_changing:
        with console.status(f"Identifying the z-stream release of v{version}…"):
            detected = detect_zstream(version)
        if detected and held and detected != held:
            snapshot_zstream(output_dir, version, held, events)

    downloaded = run_downloads(to_download, version, manifest, output_dir, events)
    # Re-read after the download: the release notes on disk are now the new
    # ones, so this records the z-stream the directory actually holds.
    refresh_zstream(output_dir, version, manifest, events,
                    release_notes_changed=RELEASE_NOTES_SLUG in downloaded)
    return downloaded


# ---------------------------------------------------------------------------
# Local library view
# ---------------------------------------------------------------------------

def print_farewell(output_dir: Path, manifest: dict) -> None:
    """
    Say where the files are on the way out — the output directory is
    configurable and easy to forget, and it is the one thing worth knowing
    after the TUI closes.
    """
    entries = scan_library(output_dir, manifest)
    if not entries:
        console.print(f"\n[dim]Nothing downloaded yet. Files will go to "
                      f"[/dim][bold]{output_dir}[/bold]")
        return
    live = [e for e in entries if not e["snapshot"]]
    total = sum(e["bytes"] + e["archived_bytes"] for e in entries)
    versions = ", ".join(e["name"] for e in live)
    console.print(f"\nYou will find the downloaded docs at "
                  f"[bold]{output_dir}[/bold]")
    console.print(f"[dim]{versions}  ·  {fmt_bytes(total)}  ·  "
                  f"{INDEX_FILENAME} in each version directory lists its "
                  f"guides[/dim]")


class LiveText:
    """
    A string that re-reads itself every time it is rendered.

    questionary builds its choice list once, so a plain status string freezes
    at whatever it said when the menu was drawn. It formats titles with
    "{}".format(...) on every frame, so an object with __format__ gives a
    genuinely live line — paired with refresh_interval below, the progress
    actually ticks instead of needing a keypress to update.
    """

    def __init__(self, render):
        self.render = render

    def __format__(self, spec):
        return format(self.render(), spec)

    def __str__(self):
        return str(self.render())


class LiveChoice(questionary.Choice):
    """
    A choice whose *styled* fragments are rebuilt on every frame.

    LiveText above can only produce plain text, since questionary renders a
    scalar title under one style class. A list title passes through to
    prompt_toolkit untouched and is rebuilt on every repaint, so reading it
    from a property makes the row live *and* individually coloured.

    disabled=True marks a status row; questionary skips those when moving.
    """

    def __init__(self, render, value: str, disabled=None):
        self.render = render
        super().__init__(title=[("", "")], value=value, disabled=disabled)

    @property
    def title(self):
        return self.render()

    @title.setter
    def title(self, _):
        pass        # the render callback is the only source of truth


def busy_fragments(stage: str) -> list:
    """
    The status row while the version list is being found: the usual spinner,
    green because nothing else is on screen yet to show the program is
    alive. Once the rows appear they animate themselves.
    """
    return [("fg:#00ff88 bold", spinner_frame()),
            ("fg:#888888", f" {stage}")]


def exit_when_set(app, event: threading.Event, result) -> None:
    """
    Close a running prompt from another thread once `event` fires, so the
    menu redraws itself the moment the background check finishes instead of
    waiting for the user to press something.

    prompt_toolkit is not thread-safe, hence call_soon_threadsafe. Failures
    are swallowed deliberately: the worst case is the old behaviour, where
    the new rows appear on the next redraw.
    """
    def wait():
        if not event.wait(timeout=600):
            return
        try:
            loop = getattr(app, "loop", None)
            if loop and app.is_running:
                loop.call_soon_threadsafe(app.exit, result)
        except Exception:
            pass

    threading.Thread(target=wait, daemon=True).start()


def bind_back(app, result) -> None:
    """
    Esc and ← leave the current screen, on every prompt type.

    Esc is bound non-eagerly on purpose: arrow keys arrive as escape
    sequences, so an eager binding would swallow them. prompt_toolkit waits
    briefly to disambiguate, which costs a short delay on Esc alone and keeps
    the arrows working.
    """
    def leave(event):
        event.app.exit(result=result)

    app.key_bindings.add("escape")(leave)
    app.key_bindings.add("left")(leave)


SPINNER = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"


def spinner_frame() -> str:
    return SPINNER[int(time.monotonic() * 10) % len(SPINNER)]


def skipped_note(catalog) -> str:
    """
    How many versions were left alone as recently compared, and when the
    first of them is due again. Lives in the heading rather than per row:
    the rows are already at the edge of a normal window, and the soonest
    expiry is one number for the whole list.
    """
    ages = getattr(catalog, "ages", None)
    if not ages:
        return ""
    window = 3600 * float((catalog.config or {}).get("recheck_after_hours") or 0)
    due = window - min(ages.values())
    return (f" · {len(ages)} checked within {fmt_age(window)}"
            + (f", next due in {fmt_age(due)}" if due >= 60 else ", due now"))


def versions_label(catalog, settled_count: Optional[int] = None) -> str:
    """Heading for the versions block, counting what is known right now."""
    if settled_count is not None:
        return (f"  ─── Versions ({settled_count}){skipped_note(catalog)} ───"
                if settled_count else "  ─── Versions (none found) ───")
    if catalog is None or not catalog.versions:
        return "  ─── Versions (searching…) ───"
    found = len(catalog.versions)
    ready = sum(1 for v in catalog.versions
                if catalog.state.get(v) == "ready")
    if ready == found:
        return f"  ─── Versions ({found}){skipped_note(catalog)} ───"
    return f"  ─── Versions ({found} found · {ready} ready) ───"


def version_rows(all_versions: list[str], latest: Optional[str],
                 version_data: dict, manifest: Optional[dict],
                 timing: Optional[dict] = None,
                 ages: Optional[dict] = None) -> list[dict]:
    """
    The column values behind every version row.

    Shared by the live rows drawn while the background check runs and the
    coloured rows drawn once it settles, so a row keeps its columns in the
    same places from the first frame to the last.
    """
    rows = []
    for v in all_versions:
        data = version_data.get(v)
        name = f"v{v}" + (" (latest)" if v == latest else "")
        z = (manifest or {}).get(meta_key(v), {}).get("z_version")
        if not data:
            # Discovered but its guide list has not arrived yet (or failed).
            # Every key the renderer reads must still be present. The release
            # is kept even though this row prints none: it comes from the
            # manifest, so counting it now sizes the release column once
            # instead of widening it as each version lands.
            rows.append({"v": v, "name": name, "count": "", "extra": "",
                         "z": z, "colour": "", "date": "", "size": "",
                         "secs": "", "secs_label": ""})
            continue

        c = data["counts"]
        ok_n, total = c["ok"], c["total"]
        extra = ""
        if c["changed"]:
            extra += f"  ·  {c['changed']} updated"
        if c["corrupt"]:
            extra += f"  ·  {c['corrupt']} damaged"

        # Complete → green, partial → yellow, nothing held → default.
        # questionary renders no Rich markup; it takes prompt_toolkit
        # (style, text) pairs, which is why this is not f-string colour.
        if ok_n == 0:
            colour = ""
        elif ok_n == total and not c["changed"] and not c["corrupt"]:
            colour = "fg:#00ff88"
        else:
            colour = "fg:#ffcc00"

        # Bytes actually on disk for this version's tracked guides. Archived
        # and snapshot copies are deliberately excluded — this answers "how
        # big is this doc set", not "how much disk does the tree use".
        on_disk = sum(s["local_size"] or 0
                      for st in data["statuses"].values() for s in st)

        # Publication dates span months within one version — 3.5 runs from
        # August to October — so a single date would misrepresent the set.
        # Worse, the newest can belong to a guide about a *different* version
        # (3.5 ships "3.6 Early Access release notes"), so showing only the
        # maximum implies the whole set is fresher than it is. Per-guide dates
        # are in _INDEX.md and the --check table.
        dates = [s["doc_date"] for st in data["statuses"].values() for s in st
                 if s.get("doc_date")]
        if not dates:
            span = ""
        elif min(dates) == max(dates):
            span = min(dates)
        else:
            span = f"{min(dates)} … {max(dates)}"

        # Either how long this run's check took, or — when it was skipped
        # as recent — how old the answer on screen actually is. Saying
        # nothing would imply freshness the row does not have.
        took, age = (timing or {}).get(v), (ages or {}).get(v)
        if age is not None:
            # No "checked" prefix: the heading above the list already says
            # what the ages are, and the rows are tight enough as it is.
            secs, secs_label = f"{fmt_age(age)} ago", ""
        elif took:
            secs, secs_label = fmt_secs(took), "took "
        else:
            secs, secs_label = "", ""
        rows.append({"v": v, "name": name, "count": f"{ok_n}/{total}",
                     "extra": extra, "z": z, "colour": colour,
                     "date": span,
                     "size": fmt_bytes(on_disk) if on_disk else "",
                     "secs": secs, "secs_label": secs_label})
    return rows


def row_widths(rows: list[dict]) -> dict:
    """
    Column widths measured from the real strings: version names ("v3.5
    (latest)" vs "v2.25") and counts ("0/31" vs "39/39") differ in width, so
    fixed padding leaves the columns ragged.
    """
    w = {
        "name": max((len(r["name"]) for r in rows), default=0),
        "count": max((len(r["count"]) for r in rows), default=0),
        "body": max((len(r["count"]) + len(" downloaded") + len(r["extra"])
                     for r in rows if r["count"]), default=0),
        "rel": max((len(f"release {r['z']}") for r in rows
                    if r["z"] and r["z"] != r["v"]), default=0),
        "date": max((len(r["date"]) for r in rows if r.get("date")), default=0),
        "size": max((len(r["size"]) for r in rows), default=0),
        "secs": max((len(r["secs"]) for r in rows), default=0),
        "wordy": max((len(r["secs_label"]) + len(r["secs"])
                      for r in rows if r["secs"]), default=0),
    }
    # How the timing column is spelled depends on what the window can take.
    # A wrapped row splits in two and pushes the rest of the list down, so
    # the words go first, then the column itself — the row proper always
    # wins the space.
    base = (POINTER_COLUMNS + 2 + w["name"] + 3 + w["body"]
            + sum(3 + w[c] for c in ("rel", "date", "size") if w[c]))
    room = shutil.get_terminal_size().columns - base
    w["wordy_secs"] = bool(w["secs"]) and room >= 3 + w["wordy"]
    if w["secs"] and room < 3 + w["secs"]:
        w["secs"] = 0
    return w


def row_body(r: dict, w: dict) -> str:
    """The "39/39 downloaded" cell, right-aligned on the widest count."""
    return f"{r['count']:>{w['count']}} downloaded"


# questionary's own left gutter: three columns for the pointer, two for the
# ○/● indicator. Not ours to set, but it eats into the row's width budget.
POINTER_COLUMNS = 5


def row_tail(r: dict, w: dict, lit: bool = True) -> list:
    """
    Release / dates / size / how long the check took, padded to fixed widths
    so the columns line up even when a row has no release or no files.

    Fragments, not one string: the release is a fact about the version like
    the count is, so it shares that green, while the timing sits a shade
    below the dates. `lit` is off while the row's own check is still running.
    """
    rel = f"release {r['z']}" if r["z"] and r["z"] != r["v"] else ""
    pad = w["body"] - len(row_body(r, w)) - len(r["extra"]) + 3
    out = [("", " " * pad),
           ("fg:#00ff88" if lit and rel else "", f"{rel:<{w['rel']}}")]
    if w["date"]:
        out.append(("fg:#888888", f"   {r['date']:>{w['date']}}"))
    if w["size"]:
        out.append(("fg:#888888", f"   {r['size']:>{w['size']}}"))
    if w["secs"]:
        # Blank, not absent, until this row's own check is in: the time so
        # far would still grow, and the column has to hold its place.
        wordy = w.get("wordy_secs")
        took = (f"{r['secs_label'] if wordy else ''}{r['secs']}") if lit else ""
        out.append(("fg:#444444",
                    f"   {took:>{w['wordy'] if wordy else w['secs']}}"))
    return out


def pending_row_fragments(version: str, catalog, all_versions: list[str],
                          latest: Optional[str], version_data: dict,
                          manifest: Optional[dict]) -> list:
    """
    One version's row while the background check runs.

    Says what is happening to *this* version rather than showing a static
    placeholder, so a row waiting its turn is distinguishable from one being
    fetched now. Widths are re-measured every frame because rows arrive one
    at a time: a width frozen before the first result leaves them ragged.
    """
    rows = version_rows(all_versions, latest, version_data, manifest,
                        catalog.timing, catalog.ages)
    w = row_widths(rows)
    r = next((row for row in rows if row["v"] == version), None)
    state = catalog.state.get(version, "queued")

    # The indent every row already carries doubles as the "busy" slot, so
    # the spinner costs no width; the phase itself is named on the status
    # line above the list.
    mark = f"{spinner_frame()} " if state in ("loading", "comparing") else "  "
    head = [("", f"{mark}{r['name'] if r else version:<{w['name']}}   ")]

    if state == "failed":
        return head + [("fg:#888888", "could not load")]
    if r is None or not r["count"]:
        return head + [("fg:#888888", "reading guide list…" if state == "loading"
                        else "waiting…")]

    # Colour lands per row, as soon as that version's own comparison is in —
    # it does not wait for the others. Before that the counts are local-only
    # and a complete-looking set can still turn out to have updates upstream,
    # so an early green would have to be taken back.
    done = state == "ready"
    out = head + [(r["colour"] if done else "", row_body(r, w))]
    if r["extra"]:
        out.append(("fg:#ffcc00" if done else "", r["extra"]))
    return out + row_tail(r, w, lit=done)


def pointed_control(app):
    """questionary keeps the list control private; find it in the layout."""
    for container in app.layout.walk():
        content = getattr(container, "content", None)
        if isinstance(content, InquirerControl):
            return content
    return None


def ask_menu(message: str, choices: list, default=None,
             instruction: str = "", back_value=None,
             redraw_on: Optional[threading.Event] = None,
             redraw_value=None, refresh_interval: Optional[float] = None,
             erase_when_done: bool = False):
    """
    questionary.select with tree-style keys on top of the defaults:

      → and Enter   select / expand the row under the cursor
      ← and Esc     leave this screen, same as its own Back/Quit row

    so getting out does not mean scrolling to the bottom of a 50-row tree to
    press Enter on "Back".

    Esc is bound non-eagerly on purpose: arrow keys arrive as escape
    sequences, so an eager binding would swallow them. prompt_toolkit waits
    briefly to disambiguate, which costs a short delay on Esc alone and keeps
    the arrows working.
    """
    extra = {}
    if refresh_interval is not None:
        extra["refresh_interval"] = refresh_interval
    if erase_when_done:
        extra["erase_when_done"] = True
    question = questionary.select(
        message, choices=choices, default=default, style=TUI_STYLE,
        use_indicator=True, instruction=instruction, **extra,
    )
    if redraw_on is not None and not redraw_on.is_set():
        exit_when_set(question.application, redraw_on, redraw_value)
    bindings = question.application.key_bindings
    bind_back(question.application, back_value)

    def choose(event):
        # Mirrors questionary's own Enter handler
        control = pointed_control(event.app)
        if control is None:
            return
        control.is_answered = True
        event.app.exit(result=control.get_pointed_at().value)

    bindings.add("right")(choose)
    return question.ask()


def is_zstream_dir(name: str) -> bool:
    """3.5.1 is a frozen snapshot; 3.5 is a live version directory."""
    return bool(re.fullmatch(r"\d+\.\d+\.\d+", name))


def scan_library(output_dir: Path, manifest: dict) -> list[dict]:
    """
    Everything on disk, read without touching the network: live version
    directories, their archives, and frozen z-stream snapshots.
    """
    if not output_dir.is_dir():
        return []

    entries = []
    for d in sorted(output_dir.iterdir()):
        if not d.is_dir() or not re.fullmatch(r"\d+(\.\d+)+", d.name):
            continue
        pdfs = sorted(d.glob("*.pdf"))
        archive = d / ARCHIVE_DIRNAME
        archived = sorted(archive.glob("*")) if archive.is_dir() else []
        meta = manifest.get(meta_key(d.name), {})
        entries.append({
            "name": d.name,
            "path": d,
            "snapshot": is_zstream_dir(d.name),
            "files": len(pdfs),
            "bytes": sum(p.stat().st_size for p in pdfs),
            "archived": len(archived),
            "archived_bytes": sum(p.stat().st_size for p in archived),
            "z_version": meta.get("z_version"),
            "has_index": (d / INDEX_FILENAME).exists(),
            "has_changelog": (d / CHANGELOG_FILENAME).exists(),
        })

    # Snapshots sort directly under the version they came from
    def key(e):
        parts = [int(x) for x in e["name"].split(".")]
        return (-parts[0], -parts[1], parts[2] if len(parts) > 2 else -1)

    return sorted(entries, key=key)


def open_in_default_app(path: Path) -> Optional[str]:
    """
    Hand a file to whatever the desktop opens it with. Returns an error to
    show the user, or None when the viewer was launched.

    Detached and silenced on purpose: the viewer must outlive this process
    rather than die with it, and a stray line from xdg-open on stdout would
    land in the middle of the menu and corrupt the frame.
    """
    try:
        if sys.platform == "darwin":
            launcher = ["open", str(path)]
        elif os.name == "nt":
            os.startfile(path)                       # type: ignore[attr-defined]
            return None
        else:
            launcher = ["xdg-open", str(path)]
        subprocess.Popen(launcher, stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL, start_new_session=True)
    except OSError as e:
        return f"{type(e).__name__}: {e}"
    return None


def library_choices(entries: list[dict], expanded: set,
                    version_data: dict) -> tuple[list, dict]:
    """
    The tree IS the menu. Version rows expand and collapse on Enter; a leaf
    row opens the file it names, when it names one.

    Returns the choices and a map from leaf value to the path it stands for
    — a category heading or a guide that was never downloaded maps to
    nothing, and pressing Enter on it does nothing, as before.

    Leaves are real choices rather than Separators on purpose. Separators are
    rendered in questionary's disabled style (uniformly grey, which makes a
    40-line tree unreadable) and are skipped when navigating, so the viewport
    jumps from one version straight to the next and most of an expanded tree
    cannot be reached at all. Real choices keep normal colours and let the
    cursor walk the tree; Enter on one is simply a no-op that redraws with the
    cursor left where it was.
    """
    name_w = max(len(e["name"]) + 1 for e in entries)
    files_w = max(len(f"{e['files']} guides") for e in entries)
    size_w = max(len(fmt_bytes(e["bytes"])) for e in entries)

    choices: list = []
    paths: dict[str, Path] = {}
    for i, e in enumerate(entries):
        last = i == len(entries) - 1
        stem = "    " if last else "│   "
        open_ = e["name"] in expanded
        trailer = ("frozen snapshot" if e["snapshot"]
                   else f"release {e['z_version']}" if e["z_version"] else "")
        choices.append(questionary.Choice(title=[
            ("", f"{'└──' if last else '├──'} "),
            ("fg:#00aaff bold", "▾ " if open_ else "▸ "),
            ("bold", f"{e['name'] + '/':<{name_w}}"),
            ("", f"  {e['files']} guides".ljust(files_w + 2)),
            ("", f"  {fmt_bytes(e['bytes']):>{size_w}}"),
            ("fg:#888888", f"   {trailer}"),
        ], value=e["name"]))

        if not open_:
            continue

        leaves: list[tuple[str, Optional[Path]]] = []
        data = version_data.get(e["name"])
        if data and not e["snapshot"]:
            leaves += [(f"{stem}{line}",
                        e["path"] / name if name else None)
                       for line, name in guide_tree_entries(data["chapters"],
                                                            data["statuses"])]
        else:
            # A frozen snapshot carries no chapter metadata — list its files
            leaves += [(f"{stem}    {fmt_bytes(f.stat().st_size):>10}  {f.name}", f)
                       for f in sorted(x for x in e["path"].iterdir()
                                       if x.is_file())]
        if e["archived"]:
            leaves.append((f"{stem}├── archive/   {e['archived']} superseded  "
                           f"{fmt_bytes(e['archived_bytes'])}",
                           e["path"] / ARCHIVE_DIRNAME))
        if e["has_index"]:
            leaves.append((f"{stem}├── {INDEX_FILENAME}",
                           e["path"] / INDEX_FILENAME))
        if e["has_changelog"]:
            leaves.append((f"{stem}└── {CHANGELOG_FILENAME}",
                           e["path"] / CHANGELOG_FILENAME))

        # Values are positional and stable for a given expansion state, which
        # is what lets the cursor be restored after an Enter.
        for line, path in leaves:
            value = f"{LEAF_PREFIX}{len(choices)}"
            if path is not None and path.exists():
                paths[value] = path
            choices.append(questionary.Choice(title=line, value=value))
    return choices, paths


def tui_library_screen(output_dir: Path, manifest: dict,
                       version_data: dict) -> None:
    """
    Navigate what is on disk, expanding versions in place. Entirely local —
    makes no requests, so it works during an upstream outage.
    """
    expanded: set = set()
    cursor = None
    while True:
        entries = scan_library(output_dir, manifest)
        if not entries:
            console.print(f"\n[dim]Nothing downloaded yet under "
                          f"{output_dir}[/dim]")
            return

        live = sum(1 for e in entries if not e["snapshot"])
        snaps = len(entries) - live
        total = sum(e["bytes"] + e["archived_bytes"] for e in entries)
        header = (f"{output_dir}  —  {live} version(s)"
                  + (f", {snaps} snapshot(s)" if snaps else "")
                  + f", {fmt_bytes(total)}")
        if snaps:
            header += "  (snapshots are hardlinked; real usage is lower)"

        choices, leaf_paths = library_choices(entries, expanded, version_data)
        choices.append(questionary.Separator("  " + "─" * 20))
        choices.append(questionary.Choice(title="  ← Back", value=BACK_CHOICE))

        console.print()
        pick = ask_menu(
            header, choices, default=cursor,
            instruction="(↑↓ navigate, Enter/→ expands or opens, Esc/← back)",
            back_value=BACK_CHOICE)
        if pick in (None, BACK_CHOICE):
            return

        cursor = pick                       # keep the cursor where it was
        if not str(pick).startswith(LEAF_PREFIX):
            expanded.symmetric_difference_update({pick})
        elif pick in leaf_paths:
            target = leaf_paths[pick]
            problem = open_in_default_app(target)
            if problem:
                console.print(f"[red]Could not open[/red] {target.name} — "
                              f"{problem}")
            else:
                console.print(f"[dim]Opening[/dim] {target.name}[dim] in your "
                              f"default application…[/dim]")


# ---------------------------------------------------------------------------
# TUI screens
# ---------------------------------------------------------------------------

def tui_version_screen(all_versions: list[str], latest: Optional[str],
                       version_data: dict,   # version -> {chapters, statuses, counts}
                       manifest: Optional[dict] = None,
                       library: str = "",
                       catalog=None) -> Optional[str]:
    """
    Show version picker. Returns selected version string, or None if user quits.
    version_data may be partially populated (loaded lazily).
    """
    pending = catalog is not None and not catalog.done.is_set()

    # Build every row first, then size the columns from the real strings —
    # the same helpers the live rows use, so the table does not re-flow when
    # the background check finishes.
    rows = version_rows(all_versions, latest, version_data, manifest,
                        getattr(catalog, "timing", None),
                        getattr(catalog, "ages", None))
    w = row_widths(rows)

    choices = []
    for r in rows:
        if pending:
            # Rebuilt on every frame, so each row advances in place — no
            # prompt teardown, no flicker — and keeps its own colour the
            # moment its own check lands, rather than the whole table
            # turning green at the end.
            choices.append(LiveChoice(
                lambda v=r["v"]: pending_row_fragments(
                    v, catalog, all_versions, latest, version_data, manifest),
                value=r["v"]))
            continue

        parts = [("", f"  {r['name']:<{w['name']}}   ")]
        if not r["count"]:
            parts.append(("fg:#888888", "…"))
            choices.append(questionary.Choice(title=parts, value=r["v"]))
            continue

        parts.append((r["colour"], row_body(r, w)))
        if r["extra"]:
            parts.append(("fg:#ffcc00", r["extra"]))
        parts += row_tail(r, w)
        choices.append(questionary.Choice(title=parts, value=r["v"]))

    # Library entry on top: it answers "what do I already have?" without any
    # network at all, which the per-version rows below cannot.
    header = [questionary.Choice(
        title=[("", "  "), ("fg:#00aaff bold", "Local library"),
               ("fg:#888888", f"   {library}")],
        value=LIBRARY_CHOICE)]

    if pending and not rows:
        # Nothing is known yet, so this line is the only thing on screen that
        # can show the program is alive — hence green rather than the grey
        # spinner below. The menu is rebuilt the moment the version list
        # lands (see redraw_on), which is when the rows take over.
        header.append(LiveChoice(
            lambda: busy_fragments(catalog.stage or "Searching for versions…"),
            value=BUSY_CHOICE))
    elif pending:
        # Status only, deliberately not selectable — but live, so you can
        # watch it progress rather than wonder whether it is stuck. The rows
        # themselves animate from here on, so this stays understated.
        header.append(questionary.Separator(LiveText(
            lambda: f"  {spinner_frame()} "
                    f"{catalog.stage or 'Checking upstream…'}")))
    elif catalog is not None and catalog.error:
        header.append(questionary.Separator(
            "  ⚠ upstream unreachable — local library still works"))

    if pending:
        # Separators are formatted every frame too, so this count tracks
        # discovery as it happens instead of freezing at whatever was known
        # when the menu was drawn.
        label = LiveText(lambda: versions_label(catalog))
    else:
        label = versions_label(catalog, len(rows))
    choices = header + [questionary.Separator(label)] + choices

    choices.append(questionary.Separator("  " + "─" * 20))
    choices.append(questionary.Choice(title="  Quit", value=QUIT_CHOICE))

    console.print()
    return ask_menu(
        "Select a version to browse:", choices,
        instruction="(↑↓ navigate, Enter/→ select, Esc quits)",
        back_value=QUIT_CHOICE,
        # While the check runs, animate the status row and close the menu as
        # soon as it finishes so the version rows appear on their own.
        redraw_on=catalog.rows_changed if pending else None,
        redraw_value=REDRAW_CHOICE,
        refresh_interval=0.1 if pending else None,
        # Each redraw would otherwise leave its finished prompt in the
        # scrollback; with a dozen incremental updates that is a wall of
        # stale menus.
        erase_when_done=pending)


def tui_chapter_screen(version: str, chapters: list[dict],
                       statuses: dict) -> list[dict]:
    """
    Show chapter checkbox list. Returns list of selected chapter dicts.
    Chapters with status missing/changed are pre-checked.
    """
    by_category: dict[str, list[dict]] = {}
    for ch in chapters:
        by_category.setdefault(ch["category"], []).append(ch)

    ordered_cats = [c for c in CATEGORY_ORDER if c in by_category]
    ordered_cats += [c for c in by_category if c not in CATEGORY_ORDER]

    choices = []
    for cat in ordered_cats:
        choices.append(questionary.Separator(f"── {cat} ──"))
        for ch in by_category[cat]:
            ch_statuses = statuses.get(ch["slug"], [])
            worst = worst_status(ch_statuses)
            icon = STATUS_PLAIN[worst]
            pre_checked = worst in NEEDS_DOWNLOAD
            # Append doc date from PDF entry if available
            pdf_entry = next((s for s in ch_statuses if s["label"] == "PDF"), None)
            date_str = f"  [{pdf_entry['doc_date']}]" if pdf_entry and pdf_entry.get("doc_date") else ""
            reason = next((s.get("change_reason") for s in ch_statuses
                           if s.get("change_reason")), None)
            if worst == "changed" and reason:
                date_str += f"  ({reason} changed upstream)"
            elif worst == "corrupt":
                date_str += "  (local file damaged)"
            label = f"{icon}  {ch['title']}{date_str}"
            choices.append(questionary.Choice(
                title=label,
                value=ch["slug"],
                checked=pre_checked,
            ))

    console.print(f"\n  [bold]Version {version}[/bold]\n", highlight=False)

    question = questionary.checkbox(
        "Select chapters to download:",
        choices=choices,
        style=TUI_STYLE,
        instruction="(space toggle · a all · i invert · Enter confirm · "
                    "Esc/← back)",
        # Transient: these two prompts are navigation, not a record of
        # anything. Leaving their answers behind puts lines like
        # "done (27 selections)" above a menu where nothing was downloaded.
        erase_when_done=True,
    )
    # checkbox() builds its own prompt, so it never passed through ask_menu
    # and had no way back except Ctrl-C
    bind_back(question.application, None)
    selected_slugs = question.ask()

    # Ctrl-C, Esc or ← → treat as Back
    if selected_slugs is None:
        return "back", []

    selected = [ch for ch in chapters if ch["slug"] in set(selected_slugs)]

    n = len(selected)
    action_choices = [
        questionary.Choice(title=f"  Download {n} selected chapter{'s' if n != 1 else ''}",
                           value="download"),
        # No "←" in the label: the key hint already says Esc/←, and the glyph
        # reads oddly when questionary echoes the chosen row back
        questionary.Choice(title="  Back to version list", value="back"),
        questionary.Choice(title="  Quit",                 value="quit"),
    ]
    action = ask_menu("What do you want to do?", action_choices,
                      instruction="(Enter/→ select, Esc/← back)",
                      back_value="back", erase_when_done=True)

    if action is None:
        action = "back"  # Ctrl-C

    return action, selected


def print_version_table(version: str, chapters: list[dict],
                        statuses: dict, fmt: str) -> None:
    """Rich table showing chapter statuses for --check mode."""
    table = Table(
        title=f"Version {version}",
        box=box.SIMPLE_HEAD,
        show_header=True,
        header_style="bold cyan",
        expand=False,
    )
    table.add_column("", width=3, no_wrap=True)
    table.add_column("Category", style="dim", no_wrap=True, min_width=14)
    table.add_column("Guide", min_width=40)
    table.add_column("Published", width=12, style="dim", no_wrap=True)
    table.add_column("Note", width=26, style="dim", no_wrap=True)
    if fmt == "both":
        table.add_column("PDF", width=4, justify="center")
        table.add_column("HTML", width=5, justify="center")

    by_category: dict[str, list[dict]] = {}
    for ch in chapters:
        by_category.setdefault(ch["category"], []).append(ch)

    ordered_cats = [c for c in CATEGORY_ORDER if c in by_category]
    ordered_cats += [c for c in by_category if c not in CATEGORY_ORDER]

    for cat in ordered_cats:
        for ch in by_category[cat]:
            ch_statuses = statuses.get(ch["slug"], [])
            pdf_s = next((s for s in ch_statuses if s["label"] == "PDF"), None)
            doc_date = pdf_s.get("doc_date") or "" if pdf_s else ""
            worst = worst_status(ch_statuses)

            note = ""
            if worst == "changed":
                reason = next((s.get("change_reason") for s in ch_statuses
                               if s.get("change_reason")), "date")
                if reason == "size":
                    was = next((s.get("recorded_size") for s in ch_statuses
                                if s.get("recorded_size") is not None), None)
                    now_up = next((s.get("upstream_size") for s in ch_statuses
                                   if s.get("upstream_size") is not None), None)
                    note = f"size {fmt_bytes(was)} → {fmt_bytes(now_up)}"
                else:
                    note = "new publication"
            elif worst == "corrupt":
                local = next((s.get("local_size") for s in ch_statuses
                              if s.get("local_size") is not None), None)
                expect = next((s.get("recorded_size") for s in ch_statuses
                               if s.get("recorded_size") is not None), None)
                note = (f"damaged {fmt_bytes(local)}/{fmt_bytes(expect)}"
                        if local is not None else "damaged")

            if fmt == "both":
                html_s = next((s for s in ch_statuses if s["label"] == "HTML"), None)
                pdf_icon = STATUS_ICON.get(pdf_s["status"], "") if pdf_s else ""
                html_icon = STATUS_ICON.get(html_s["status"], "") if html_s else ""
                table.add_row(STATUS_ICON[worst], cat, ch["title"], doc_date, note,
                              pdf_icon, html_icon)
            else:
                table.add_row(STATUS_ICON[worst], cat, ch["title"], doc_date, note)

    console.print(table)


class Catalog:
    """
    Everything that needs the network, resolved off the UI thread.

    The local library is on disk and instant; discovery, chapter lists and the
    upstream comparison are neither. Running them in a worker lets the menu
    appear immediately and fill in when the answers arrive, instead of the
    user staring at a spinner before anything is usable — which matters most
    when docs.redhat.com is slow or down, exactly when you still want to read
    what you already downloaded.
    """

    def __init__(self, manifest, output_dir, config, args, fmt, only=None):
        self.manifest, self.output_dir = manifest, output_dir
        self.config, self.args, self.fmt = config, args, fmt
        # With --version there is exactly one version in play; discovery would
        # be pure waste, as would probing the frontier for new releases.
        self.only = only
        self.latest: Optional[str] = None
        self.versions: list[str] = []
        self.data: dict[str, dict] = {}
        self.checked: set = set()
        self.loaded = 0
        # Per-version progress, so each row can say what is happening to it
        # rather than the menu carrying one global status line:
        #   queued → loading → comparing → ready (or failed)
        self.state: dict[str, str] = {}
        # Set only when the set of rows changes, which is the only thing that
        # needs the prompt torn down and rebuilt. Row *contents* update in
        # place via LiveText, so ordinary progress causes no flicker at all.
        self.rows_changed = threading.Event()
        self.error: Optional[str] = None
        self.stage = "Checking for new versions…"
        self.done = threading.Event()
        # Set every time a single version becomes displayable, so the menu can
        # show each one as it lands instead of all of them at the very end
        self.updated = threading.Event()
        self.warnings: list[str] = []
        # Seconds of upstream work per version: reading its guide list, plus
        # comparing the files held against upstream. Wall clock, so the
        # parallel phase charges each version only its own request time.
        self.timing: dict[str, float] = {}
        # Versions whose comparison was skipped as recent: seconds since
        self.ages: dict[str, float] = {}

    def mark_updated(self) -> None:
        self.updated.set()

    def start(self) -> "Catalog":
        # Daemon: a half-finished version check must never hold up exit
        threading.Thread(target=self._run, daemon=True).start()
        return self

    def run_sync(self) -> "Catalog":
        self._run()
        return self

    def _find_versions(self) -> list[str]:
        """
        The published version list: from a page the site answers quickly
        for if it can, from the manifest if it cannot.

        Every version page carries the version menu and answers in 0.1–0.3 s.
        The landing page carries it too *and* names the current release, but
        is the site's least reliable URL — so it is read only when starting
        from nothing, where its redirect is the only way to learn "latest".
        """
        cache = self.manifest.get(VERSIONS_KEY) or {}
        cached = sort_versions(cache.get("found") or [])

        if cached and not self.args.rediscover:
            try:
                picked = versions_from_picker(cached[0])
            except requests.RequestException:
                picked = []
            if picked:
                # Highest published version, rather than whatever the
                # landing page redirects to. The two agree unless a version
                # is published before being promoted — a far smaller price
                # than that page's variance.
                found = resolve_versions(self.manifest, self.output_dir,
                                         sort_versions(set(picked) | set(cached))[0],
                                         picked=picked)
                self.latest = found[0]
                return found

        try:
            self.latest, picked = discover_from_landing(
                DISCOVERY_TIMEOUT if cached else DISCOVERY_TIMEOUT_FIRST_RUN)
            return resolve_versions(self.manifest, self.output_dir, self.latest,
                                    force=self.args.rediscover,
                                    picked=picked or None)
        except requests.RequestException:
            if not cached:
                raise           # nothing to fall back on: a genuine dead end

        newest = cached[0]
        self.latest = newest

        # One cheap page to tell the two cases apart, because probe failures
        # are swallowed as "no such version": if even a version we know
        # exists will not answer, the site is down and probing the frontier
        # would only say "nothing new" for the wrong reason.
        alive, code = probe_version(newest)
        if not alive and version_probe_blocked(code):
            self.warnings.append(
                f"Could not reach docs.redhat.com — showing the version list "
                f"from {(cache.get('checked_at') or 'the last run')[:10]}. "
                f"New releases will not appear until it answers again.")
            return cached

        found = resolve_versions(self.manifest, self.output_dir,
                                 self.latest, force=self.args.rediscover)
        self.latest = found[0]
        self.warnings.append(
            f"docs.redhat.com did not answer for the current-release page; "
            f"treating v{self.latest} as the newest, from the version pages "
            f"themselves.")
        return found

    def _run(self) -> None:
        try:
            if self.only:
                self.versions = [self.only]
            else:
                found = self._find_versions()
                wanted = [str(v) for v in (self.config.get("versions") or [])]
                self.versions = ([v for v in found if v in wanted] if wanted
                                 else found)
                # Without this a typo, or a version Red Hat has retired, just
                # silently shows nothing. Unquoted YAML numbers are the usual
                # cause: 3.10 parses as the float 3.1.
                unmatched = [v for v in wanted if v not in found]
                if unmatched:
                    self.warnings.append(
                        f"rhoai_docs.yaml asks for version(s) "
                        f"{', '.join(unmatched)}, which upstream does not "
                        f"publish — they will not appear. Quote values in the "
                        f"yaml (\"3.10\"), since unquoted 3.10 reads as 3.1.")
            self.state = {v: "queued" for v in self.versions}
            self.rows_changed.set()      # the row set is known now
            self.mark_updated()

            self.stage = "Loading guide lists…"
            with concurrent.futures.ThreadPoolExecutor(max_workers=4) as ex:
                futures = {ex.submit(self._load_version, v): v
                           for v in self.versions}
                # as_completed, not map: map yields in submission order, so a
                # slow first version would hold back every one behind it.
                for fut in concurrent.futures.as_completed(futures):
                    v, result = fut.result()
                    if result:
                        self.data[v] = result
                        self.state[v] = "loaded"
                    else:
                        self.state[v] = "failed"
                    self.loaded += 1
                    self.stage = (f"Loading guide lists "
                                  f"({self.loaded}/{len(self.versions)})…")
                    self.mark_updated()

            # Only versions already on disk are compared with upstream: for
            # the rest there is nothing to compare against, and probing them
            # would cost one request per guide to learn nothing.
            held = [v for v in self.versions
                    if self.data.get(v)
                    and self.data[v]["counts"]["ok"]
                    + self.data[v]["counts"]["corrupt"] > 0]
            # Comparing is ~100 requests and can run to minutes; one that
            # happened within the window is not worth repeating just to
            # draw the menu. Opening the version still re-checks, since
            # these are deliberately not added to self.checked.
            window = 3600 * float(self.config.get("recheck_after_hours")
                                  or 0)
            fresh = {}
            if window:
                for v in list(held):
                    age = last_compared(self.manifest, v,
                                        self.data[v]["chapters"])
                    if age is not None and age < window:
                        fresh[v] = age
                        self.state[v] = "ready"
                        held.remove(v)
                self.ages.update(fresh)
                self.mark_updated()
            for i, v in enumerate(held, 1):
                self.stage = f"Checking downloaded versions ({i}/{len(held)})…"
                self.state[v] = "comparing"
                self.mark_updated()
                d = self.data[v]
                started = time.monotonic()
                enrich_with_upstream(d["chapters"], d["statuses"], self.manifest,
                                     self.output_dir, v)
                d["counts"] = version_aggregate(d["chapters"], d["statuses"])
                self.timing[v] = self.timing.get(v, 0) + time.monotonic() - started
                self.checked.add(v)
                self.state[v] = "ready"
                self.mark_updated()      # this version's row is now accurate
            for v in self.versions:
                self.state.setdefault(v, "ready")
                if self.state[v] == "loaded":
                    self.state[v] = "ready"   # nothing held, nothing to compare
        except Exception as e:
            self.error = str(e)
        finally:
            self.stage = ""
            self.rows_changed.set()      # final repaint, in full colour
            self.done.set()

    def _load_version(self, v: str):
        self.state[v] = "loading"
        self.mark_updated()
        started = time.monotonic()
        try:
            chapters = discover_chapters(v)
            statuses = {ch["slug"]: check_chapter_status(
                self.output_dir, self.manifest, v, ch["slug"], self.fmt,
                verify=self.args.verify) for ch in chapters}
            self.timing[v] = self.timing.get(v, 0) + time.monotonic() - started
            return v, {"chapters": chapters, "statuses": statuses,
                       "counts": version_aggregate(chapters, statuses)}
        except Exception as e:
            # Collected rather than printed: the UI thread owns the terminal
            self.warnings.append(f"Could not load version {v}: {e}")
            return v, None

    def wait(self, message: str = "Checking upstream…") -> None:
        if self.done.is_set():
            return
        with console.status(message):
            while not self.done.wait(timeout=0.3):
                pass

    def flush_warnings(self) -> None:
        while self.warnings:
            console.print(f"[yellow]Warning:[/yellow] {self.warnings.pop(0)}")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def parse_args():
    p = argparse.ArgumentParser(
        description="Download RHOAI documentation with hash tracking.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    p.add_argument("--check", action="store_true",
                   help="Show status for all versions, do not download")
    p.add_argument("--all", action="store_true", dest="download_all",
                   help="Download all missing/changed chapters non-interactively")
    p.add_argument("--version", metavar="VER",
                   help="Jump to a specific version (skips version picker)")
    p.add_argument("--format", choices=["pdf", "html-single", "both"],
                   dest="fmt", help="Override format from config")
    p.add_argument("--output", metavar="DIR",
                   help="Override output directory from config")
    p.add_argument("--verify", action="store_true",
                   help="Re-hash local files against the manifest (slower; the "
                        "default check compares byte size only)")
    p.add_argument("--rediscover", action="store_true",
                   help="Re-probe every candidate version URL instead of "
                        "reusing the cached list (slow: docs.redhat.com "
                        "throttles bursts of 404s)")
    return p.parse_args()


def main():
    args = parse_args()
    config = load_config()

    fmt = args.fmt or config["format"]
    output_dir = Path(os.path.expanduser(args.output or config["output_dir"]))
    version_filter = args.version

    RUN.begin(output_dir, "check" if args.check
              else "all" if args.download_all else "menu")

    console.rule("[bold]RHOAI Documentation Downloader[/bold]")

    # Loaded before discovery: it carries the cached version list
    manifest = load_manifest(output_dir)

    # --- Resolve which versions we're working with ---
    # With --version there is nothing to pick between, so skip discovery
    # entirely and just confirm the one version exists.
    if version_filter:
        with console.status(f"Checking version {version_filter}…"):
            try:
                exists, code = probe_version(version_filter)
            except Exception as e:
                console.print(f"[red]ERROR:[/red] Could not reach docs.redhat.com: {e}")
                sys.exit(1)
        if not exists:
            if version_probe_blocked(code):
                HEALTH.last_code = code
                HEALTH.server_errors = HEALTH.ABORT_AFTER
                HEALTH.warn()
            else:
                console.print(f"[red]ERROR:[/red] No docs found for version "
                              f"[bold]{version_filter}[/bold]. "
                              f"Run without --version to list what's available.")
            sys.exit(1)
        latest = None
        versions_to_show = [version_filter]
        console.print(f"  Version: [bold green]{version_filter}[/bold green]")
    interactive = not (args.check or args.download_all or version_filter)
    catalog = Catalog(manifest, output_dir, config, args, fmt, only=version_filter)

    if interactive:
        # Start the network work and get out of the way — the menu below is
        # drawn from local state and does not wait for any of it.
        catalog.start()
    else:
        cached = manifest.get(VERSIONS_KEY, {}).get("found")
        note = ("Discovering available versions (first run, this takes a while)…"
                if not cached and not args.rediscover
                else "Re-probing every candidate version…" if args.rediscover
                else "Checking upstream…")
        with console.status(note):
            catalog.run_sync()
        catalog.flush_warnings()
        if catalog.error:
            # A site-side failure is named as one; anything else keeps the
            # original text, since then the cause really might be local.
            if HEALTH.suspected:
                HEALTH.warn()
            else:
                console.print(f"[red]ERROR:[/red] Could not reach "
                              f"docs.redhat.com: {catalog.error}")
            RUN.note("upstream unreachable")
            sys.exit(1)

    if not version_filter and not interactive:
        latest = catalog.latest
        versions_to_show = catalog.versions
        console.print("  Latest: "
                      + (f"[bold green]{latest}[/bold green]" if latest
                         else "[dim]unknown (upstream unreachable)[/dim]")
                      + f"  │  Found: [bold]{', '.join(versions_to_show)}[/bold]")

    version_data = catalog.data

    # ==========================================================================
    # --check mode: show tables for all versions and exit
    # ==========================================================================
    if args.check:
        for v in versions_to_show:
            data = version_data.get(v)
            if not data:
                continue
            RUN.saw(v)
            if v not in catalog.checked:     # held versions already compared
                with console.status(f"Checking v{v} against upstream…"):
                    enrich_with_upstream(data["chapters"], data["statuses"],
                                         manifest, output_dir, v)
                    data["counts"] = version_aggregate(data["chapters"],
                                                       data["statuses"])
            c = data["counts"]
            console.print(f"\n[bold]v{v}[/bold]  "
                          f"[green]{c['ok']}/{c['total']} downloaded[/green]"
                          + (f"  [yellow]{c['changed']} updated[/yellow]" if c["changed"] else "")
                          + (f"  [red]{c['corrupt']} damaged[/red]" if c["corrupt"] else ""))
            print_version_table(v, data["chapters"], data["statuses"], fmt)
            events: list[str] = []
            refresh_zstream(output_dir, v, manifest, events,
                            release_notes_changed=False)
            refresh_structure(output_dir, v, data["chapters"], data["statuses"],
                              manifest, events)
            append_changelog(output_dir, v, events)
        if HEALTH.degraded:
            HEALTH.warn()
        console.print("\n[dim]Legend:  [green]✓[/green] downloaded  "
                      "[yellow]↺[/yellow] upstream changed  [red]![/red] local file "
                      "damaged  ✗ not downloaded[/dim]")
        return

    # ==========================================================================
    # --all mode: download everything without prompting
    # ==========================================================================
    if args.download_all:
        for v in versions_to_show:
            data = version_data.get(v)
            if not data:
                continue
            RUN.saw(v)
            chapters, statuses = data["chapters"], data["statuses"]
            # Without this probe --all only ever sees local state, so an
            # upstream change could never be picked up non-interactively.
            with console.status(f"Checking v{v} against upstream…"):
                enrich_with_upstream(chapters, statuses, manifest, output_dir, v)
            if HEALTH.degraded:
                HEALTH.warn()
                sys.exit(1)
            to_do = [(ch, statuses[ch["slug"]]) for ch in chapters
                     if any(s["status"] in NEEDS_DOWNLOAD
                            for s in statuses.get(ch["slug"], []))]
            events: list[str] = []
            if to_do:
                console.print(f"\n[bold]v{v}[/bold] — downloading {len(to_do)} chapters")
                download_with_snapshot(to_do, v, manifest, output_dir, events)
                statuses = {ch["slug"]: check_chapter_status(output_dir, manifest, v,
                                                             ch["slug"], fmt)
                            for ch in chapters}
            else:
                console.print(f"\n[bold]v{v}[/bold] — [green]all up to date[/green]")
                refresh_zstream(output_dir, v, manifest, events,
                                release_notes_changed=False)
            refresh_structure(output_dir, v, chapters, statuses, manifest, events)
            append_changelog(output_dir, v, events)
        return

    # ==========================================================================
    # TUI mode (default)
    # ==========================================================================
    checked = catalog.checked
    reported_error = False

    while True:
        # If --version was passed, skip the picker — there is nothing to pick
        if version_filter:
            if version_filter not in version_data:
                console.print(f"[red]ERROR:[/red] Could not load chapters for "
                              f"version [bold]{version_filter}[/bold].")
                sys.exit(1)
            chosen_version = version_filter
        else:
            catalog.flush_warnings()
            if catalog.error and not reported_error:
                reported_error = True
                RUN.note("upstream unreachable")
                if HEALTH.suspected:
                    HEALTH.warn()
                else:
                    console.print(f"\n[red]Could not reach docs.redhat.com:[/red] "
                                  f"{catalog.error}")
                console.print("[dim]Your downloaded files are still browsable "
                              "in the local library.[/dim]")
            lib = scan_library(output_dir, manifest)
            summary = "nothing downloaded yet"
            if lib:
                live = [e for e in lib if not e["snapshot"]]
                snaps = len(lib) - len(live)
                total = sum(e["bytes"] + e["archived_bytes"] for e in lib)
                summary = (f"{len(live)} version(s)"
                           + (f" · {snaps} snapshot(s)" if snaps else "")
                           + f" · {fmt_bytes(total)}")
            # Redrawn every pass: whatever the background check has finished
            # by now simply appears, without interrupting what you were doing.
            versions_to_show = catalog.versions
            chosen_version = tui_version_screen(
                versions_to_show, catalog.latest, version_data,
                manifest, summary, catalog
            )

        if chosen_version == REDRAW_CHOICE:
            # Another version became displayable; redraw to include it
            catalog.rows_changed.clear()
            continue

        if chosen_version == LIBRARY_CHOICE:
            tui_library_screen(output_dir, manifest, version_data)
            continue

        if chosen_version in (None, QUIT_CHOICE):
            print_farewell(output_dir, manifest)
            break

        data = version_data.get(chosen_version)
        if not data and not catalog.done.is_set():
            # Picked a version whose guide list is still in flight — this is
            # the one place it is worth blocking, since there is nothing to
            # show without it.
            catalog.wait(f"Loading v{chosen_version}…")
            catalog.flush_warnings()
            data = version_data.get(chosen_version)
        if not data:
            # One unreadable version must not end the session
            console.print(f"[red]Could not load version "
                          f"{chosen_version}.[/red]")
            continue

        RUN.saw(chosen_version)
        chapters, statuses = data["chapters"], data["statuses"]

        # Versions you already hold were checked before the picker; one you
        # have nothing of gets checked now, on first open.
        if chosen_version not in checked:
            with console.status(f"Checking v{chosen_version} against upstream…"):
                enrich_with_upstream(chapters, statuses, manifest, output_dir,
                                     chosen_version)
                data["counts"] = version_aggregate(chapters, statuses)
            checked.add(chosen_version)

        # Don't walk the user into a chapter picker whose downloads are all
        # going to fail — say so and stop while the outage lasts.
        if HEALTH.degraded:
            HEALTH.warn()
            console.print("[dim]Nothing was changed. Exiting.[/dim]")
            sys.exit(1)

        events: list[str] = []
        # Check the page grouping on every visit, not only after a download —
        # a reshuffle with no file change would otherwise go unnoticed.
        refresh_structure(output_dir, chosen_version, chapters, statuses,
                          manifest, events)

        c = data["counts"]
        z = manifest.get(meta_key(chosen_version), {}).get("z_version")
        console.print(f"\n  [bold]v{chosen_version}[/bold]"
                      + (f" [dim](release {z})[/dim]" if z else "")
                      + f"  {c['ok']}/{c['total']} downloaded"
                      + (f"  [yellow]{c['changed']} updated upstream[/yellow]"
                         if c["changed"] else "")
                      + (f"  [red]{c['corrupt']} damaged[/red]"
                         if c["corrupt"] else ""))

        action, selected_chapters = tui_chapter_screen(chosen_version, chapters, statuses)

        if action == "quit":
            print_farewell(output_dir, manifest)
            break

        if action == "back":
            # If --version was passed there's nowhere to go back to
            if version_filter:
                break
            continue  # loop back to version picker

        # action == "download"
        if not selected_chapters:
            console.print("[dim]Nothing selected.[/dim]")
        else:
            to_download = [(ch, statuses[ch["slug"]]) for ch in selected_chapters]
            download_with_snapshot(to_download, chosen_version, manifest,
                                   output_dir, events)

            # Refresh statuses; re-use already-fetched doc_dates from statuses
            new_statuses = {ch["slug"]: check_chapter_status(output_dir, manifest,
                                                              chosen_version, ch["slug"],
                                                              fmt, verify=args.verify)
                            for ch in chapters}
            for ch in chapters:
                slug = ch["slug"]
                for s in new_statuses.get(slug, []):
                    old = next((x for x in statuses.get(slug, [])
                                if x["label"] == s["label"]), None)
                    if old and old.get("doc_date"):
                        s["doc_date"] = old["doc_date"]
            version_data[chosen_version]["statuses"] = new_statuses
            version_data[chosen_version]["counts"] = version_aggregate(chapters, new_statuses)
            refresh_structure(output_dir, chosen_version, chapters,
                              new_statuses, manifest, events)

        append_changelog(output_dir, chosen_version, events)

        # After download, loop back to chapter screen for the same version
        # (unless --version was passed, in which case exit after one round)
        if version_filter:
            break


def quit_now(code: int = 0) -> None:
    """
    Exit without running interpreter shutdown.

    ThreadPoolExecutor registers an atexit hook that joins its workers, and
    those workers are not daemon threads — so quitting while the background
    version check is still running blocks for as long as its in-flight HTTP
    requests take, making Esc/← look broken when in fact the keypress landed
    and only the process exit stalled. Measured: 0.3 s to leave the menu,
    5 s to leave the process.

    Safe to skip: every file write in this program is already atomic
    (download via .part + os.replace, manifest and index via .tmp +
    os.replace), so there is nothing buffered that shutdown would flush.

    The run log is written here rather than from an atexit hook for exactly
    that reason: os._exit() never runs them, and this is the one funnel
    every exit path goes through.
    """
    RUN.write()
    sys.stdout.flush()
    sys.stderr.flush()
    os._exit(code)


if __name__ == "__main__":
    try:
        main()
        quit_now(0)
    except KeyboardInterrupt:
        # A stack trace through requests/urllib3/ssl tells the user nothing.
        # Partial downloads go to .part files that download_binary removes on
        # the way out, and the manifest is written atomically, so there is
        # never anything to repair here.
        RUN.note("interrupted")
        console.print("\n[dim]Interrupted — nothing left half-written. "
                      "Re-run to pick up where this stopped.[/dim]")
        quit_now(130)
    except SystemExit as e:
        # sys.exit() anywhere in main() would otherwise stall on the same
        # executor join as a normal return
        quit_now(e.code if isinstance(e.code, int) else 0)
    except Exception as e:
        # The traceback is still the useful output here, so it is re-raised
        # untouched — the log line just records that the run ended this way.
        RUN.note(f"crashed: {type(e).__name__}")
        RUN.write()
        raise
