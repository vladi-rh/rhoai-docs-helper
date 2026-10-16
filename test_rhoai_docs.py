#!/usr/bin/env python3
"""
Tests for rhoai_docs.py.  Run:  python3 -m unittest -v test_rhoai_docs

Nothing here touches the network: every test that would reach
docs.redhat.com replaces SESSION.get/head or get_html with a stub, so the
suite is deterministic and runs in under a second. Where a stub asserts
that a particular URL is *not* fetched, that is the point of the test —
discovery is supposed to avoid the site's slow landing page once it has a
cached version list.

Durations and sizes in the fixtures are made up; the shapes are real.
"""
import argparse
import io
import json
import os
import pathlib
import tempfile
import time
import unittest

import requests

import rhoai_docs as rd

VERSIONS = ["3.5", "3.4", "3.3", "3.2", "2.25", "2.16"]
RELEASES = {"3.5": "3.5.1", "3.4": "3.4.4", "3.3": "3.3.7",
            "2.25": "2.25.11", "2.16": "2.16.5"}
TIMING = {"3.5": 2.34, "3.4": 1.87, "3.3": 0.42,
          "3.2": 0.39, "2.25": 1.52, "2.16": 0.31}


def version_data(ok, total, dates, size=5_400_000):
    """One version's loaded state, as Catalog.data holds it."""
    statuses = {f"g{i}": [{"local_size": size if i < ok else 0, "doc_date": d}]
                for i, d in enumerate(dates or [None])}
    return {"chapters": [], "statuses": statuses,
            "counts": {"ok": ok, "total": total, "changed": 0, "corrupt": 0}}


DATA = {
    "3.5": version_data(39, 39, ["2026-08-19", "2026-10-15"]),
    "3.4": version_data(35, 35, ["2026-05-15", "2026-09-11"]),
    "3.3": version_data(0, 31, []),
    "3.2": version_data(0, 28, []),
    "2.25": version_data(28, 28, ["2025-10-28", "2026-09-11"]),
    "2.16": version_data(0, 22, []),
}
MANIFEST = {rd.meta_key(v): {"z_version": z} for v, z in RELEASES.items()}


def menu_html(versions):
    return ('<html><aside><select id="product_version">'
            + "".join(f'<option value="{v}">{v}</option>' for v in versions)
            + "</select></aside></html>")


class FakeCatalog:
    def __init__(self, states, timing=None, ages=None):
        self.state = dict(states)
        self.timing = dict(timing or {})
        self.ages = dict(ages or {})


def text_of(fragments):
    return "".join(text for _style, text in fragments)


def styles_of(fragments):
    return {text.strip(): style for style, text in fragments if text.strip()}


def at_width(columns):
    """Pin the terminal width, since the timing column adapts to it."""
    rd.shutil.get_terminal_size = lambda fallback=None: os.terminal_size(
        (columns, 40))


class RowLayout(unittest.TestCase):
    """The version menu is a hand-aligned table; columns must not drift."""

    def setUp(self):
        at_width(200)
        self.rows = rd.version_rows(VERSIONS, "3.5", DATA, MANIFEST)
        self.widths = rd.row_widths(self.rows)
        self.ready = FakeCatalog({v: "ready" for v in VERSIONS})

    def settled(self, row):
        """What tui_version_screen draws once the check is done."""
        return (f"  {row['name']:<{self.widths['name']}}   "
                + rd.row_body(row, self.widths) + row["extra"]
                + text_of(rd.row_tail(row, self.widths))).rstrip()

    def live(self, version):
        return text_of(rd.pending_row_fragments(
            version, self.ready, VERSIONS, "3.5", DATA, MANIFEST)).rstrip()

    def test_live_row_matches_the_settled_row(self):
        for row in self.rows:
            if row["count"]:
                self.assertEqual(self.live(row["v"]), self.settled(row),
                                 f"v{row['v']} moves when the check lands")

    def test_columns_align_across_rows(self):
        starts = {self.live(v).index("downloaded") for v in VERSIONS}
        self.assertEqual(len(starts), 1, f"ragged 'downloaded' column: {starts}")

    def test_date_span_is_not_an_arrow(self):
        # The arrow means "became" everywhere else in the tool
        span = next(r["date"] for r in self.rows if r["v"] == "3.5")
        self.assertEqual(span, "2026-08-19 … 2026-10-15")

    def test_version_with_nothing_downloaded_has_no_size_or_dates(self):
        row = next(r for r in self.rows if r["v"] == "3.3")
        self.assertEqual((row["size"], row["date"]), ("", ""))


class RowState(unittest.TestCase):
    """While the check runs, each row says what is happening to it."""

    def setUp(self):
        at_width(200)
        self.catalog = FakeCatalog(
            {"3.5": "comparing", "3.4": "ready", "3.3": "loading",
             "3.2": "queued", "2.25": "failed", "2.16": "ready"})

    def frags(self, version, data=DATA):
        return rd.pending_row_fragments(version, self.catalog, VERSIONS,
                                        "3.5", data, MANIFEST)

    def test_queued_row_waits(self):
        self.assertIn("waiting…", text_of(self.frags("3.2", data={})))

    def test_loading_row_names_the_work(self):
        self.assertIn("reading guide list…", text_of(self.frags("3.3", data={})))

    def test_failed_row_says_so(self):
        self.assertIn("could not load", text_of(self.frags("2.25")))

    def test_busy_row_carries_the_spinner_in_its_indent(self):
        self.assertIn(self.frags("3.5")[0][1][0], rd.SPINNER)
        self.assertEqual(self.frags("3.4")[0][1][:2], "  ")

    def test_colour_arrives_per_row_not_all_at_once(self):
        done = [s for s, t in self.frags("3.4") if "35/35" in t][0]
        busy = [s for s, t in self.frags("3.5") if "39/39" in t][0]
        self.assertEqual(done, "fg:#00ff88")
        self.assertEqual(busy, "", "coloured before its own check finished")


class TimingColumn(unittest.TestCase):
    """'took 2.3s', dimmer than the dates, and never wide enough to wrap."""

    def rows_at(self, columns):
        at_width(columns)
        rows = rd.version_rows(VERSIONS, "3.5", DATA, MANIFEST, TIMING)
        widths = rd.row_widths(rows)
        widest = max(
            len(("   ○ " + f"  {r['name']:<{widths['name']}}   "
                 + rd.row_body(r, widths) + r["extra"]
                 + text_of(rd.row_tail(r, widths))).rstrip())
            for r in rows if r["count"])
        return rows, widths, widest

    def test_colours(self):
        rows, widths, _ = self.rows_at(200)
        styles = styles_of(rd.row_tail(rows[0], widths))
        self.assertEqual(styles["release 3.5.1"], "fg:#00ff88")
        self.assertEqual(styles["2026-08-19 … 2026-10-15"], "fg:#888888")
        self.assertEqual(styles["took 2.3s"], "fg:#444444")

    def test_wide_window_spells_out_the_label(self):
        _, widths, widest = self.rows_at(120)
        self.assertTrue(widths["wordy_secs"])
        self.assertLessEqual(widest, 120)

    def test_narrow_window_drops_to_a_bare_duration(self):
        _, widths, widest = self.rows_at(104)
        self.assertFalse(widths["wordy_secs"])
        self.assertTrue(widths["secs"])
        self.assertLessEqual(widest, 104)

    def test_very_narrow_drops_the_column_rather_than_wrapping(self):
        _, widths, _ = self.rows_at(88)
        self.assertEqual(widths["secs"], 0)

    def test_no_timing_recorded_means_no_column(self):
        at_width(200)
        rows = rd.version_rows(VERSIONS, "3.5", DATA, MANIFEST, None)
        self.assertEqual(rd.row_widths(rows)["secs"], 0)

    def test_nothing_shown_while_a_version_is_still_being_checked(self):
        at_width(200)
        catalog = FakeCatalog({"3.5": "comparing"}, TIMING)
        line = text_of(rd.pending_row_fragments(
            "3.5", catalog, VERSIONS, "3.5", DATA, MANIFEST))
        self.assertNotIn("took", line)


class SiteHealthTest(unittest.TestCase):
    def test_one_failure_is_suspected_three_are_degraded(self):
        health = rd.SiteHealth()
        health.record(503)
        self.assertTrue(health.suspected)
        self.assertFalse(health.degraded)
        self.assertNotIn("repeatedly", health.describe())
        health.record(503)
        health.record(503)
        self.assertTrue(health.degraded)
        self.assertIn("repeatedly", health.describe())

    def test_a_good_response_clears_the_streak(self):
        health = rd.SiteHealth()
        health.record(503)
        health.record(200)
        self.assertFalse(health.suspected)

    def test_a_dead_connection_is_described_without_a_code(self):
        health = rd.SiteHealth()
        health.record(None)
        self.assertIn("not answering", health.describe())


class ProbeHealthHygiene(unittest.TestCase):
    """
    Regression: probing versions that do not exist must not look like an
    outage. docs.redhat.com tarpits a burst of misses instead of 404ing,
    and counting those hangs marked a healthy site degraded, which aborts
    --all and the chapter screen.
    """

    def setUp(self):
        self.head = rd.SESSION.head
        rd.HEALTH.server_errors, rd.HEALTH.last_code = 0, None

    def tearDown(self):
        rd.SESSION.head = self.head
        rd.HEALTH.server_errors, rd.HEALTH.last_code = 0, None

    def test_hanging_probes_do_not_fake_an_outage(self):
        def hang(url, **kw):
            raise requests.ReadTimeout("tarpit")
        rd.SESSION.head = hang
        for version in ("9.1", "9.2", "9.3", "9.4", "9.5"):
            rd.probe_version(version)
        self.assertFalse(rd.HEALTH.degraded)

    def test_real_5xx_still_registers(self):
        rd.SESSION.head = lambda url, **kw: FakeResponse(503, url)
        for version in ("3.5", "3.4", "3.3"):
            rd.probe_version(version)
        self.assertTrue(rd.HEALTH.degraded)


class FakeResponse:
    def __init__(self, status, url, text=""):
        self.status_code, self.url, self.text = status, url, text

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(f"{self.status_code}", response=self)


class Discovery(unittest.TestCase):
    """
    Where the version list comes from, and what happens when each source
    is unavailable. The landing page is the site's least reliable URL, so
    a run with a cached list must not touch it at all.
    """

    def setUp(self):
        self.saved = (rd.get_html, rd.SESSION.get, rd.SESSION.head)
        self.tmp = tempfile.TemporaryDirectory()
        self.lib = pathlib.Path(self.tmp.name)

    def tearDown(self):
        rd.get_html, rd.SESSION.get, rd.SESSION.head = self.saved
        self.tmp.cleanup()

    def seed_manifest(self, versions):
        (self.lib / "manifest.json").write_text(json.dumps({"_versions": {
            "found": versions, "latest": versions[0],
            "checked_at": "2026-10-15T00:00:00Z"}}))

    def find(self, cached=None, get_html=None, get=None, head=None, force=False):
        def refuse(kind):
            def boom(*a, **kw):
                raise AssertionError(f"{kind} should not have been fetched")
            return boom
        if cached:
            self.seed_manifest(cached)
        rd.get_html = get_html or (lambda url, **kw: menu_html(VERSIONS))
        rd.SESSION.get = get or refuse("the landing page")
        rd.SESSION.head = head or refuse("a version probe")
        catalog = rd.Catalog(rd.load_manifest(self.lib), self.lib,
                             {"versions": []},
                             argparse.Namespace(rediscover=force, verify=False),
                             "pdf")
        return catalog, catalog._find_versions()

    def test_menu_parsing(self):
        self.assertEqual(rd.picker_versions(menu_html(["3.4", "2.16", "3.5"])),
                         ["3.5", "3.4", "2.16"])
        self.assertEqual(rd.picker_versions("<html>no menu</html>"), [])
        self.assertEqual(rd.picker_versions(menu_html(["3.5", "latest", ""])),
                         ["3.5"])

    def test_steady_state_never_touches_the_landing_page(self):
        catalog, found = self.find(cached=VERSIONS)
        self.assertEqual(found, VERSIONS)
        self.assertEqual(catalog.latest, "3.5")
        self.assertEqual(catalog.warnings, [])

    def test_first_run_reads_latest_and_the_list_in_one_request(self):
        landing = FakeResponse(200, rd.BASE_URL + "/3.5", menu_html(VERSIONS))
        catalog, found = self.find(
            get=lambda url, **kw: landing,
            get_html=lambda url, **kw: self.fail("a second page was fetched"))
        self.assertEqual((catalog.latest, found), ("3.5", VERSIONS))

    def test_falls_back_to_the_landing_page_if_a_version_page_fails(self):
        def dead(url, **kw):
            raise requests.ReadTimeout("x")
        landing = FakeResponse(200, rd.BASE_URL + "/3.5", menu_html(VERSIONS))
        _, found = self.find(cached=VERSIONS, get_html=dead,
                             get=lambda url, **kw: landing)
        self.assertEqual(found, VERSIONS)

    def test_a_shorter_menu_never_loses_a_known_version(self):
        _, found = self.find(cached=["3.5", "3.4"],
                             get_html=lambda url, **kw: menu_html(["3.5"]))
        self.assertEqual(set(found), {"3.5", "3.4"})

    def test_everything_down_falls_back_to_the_cached_list(self):
        def dead(*a, **kw):
            raise requests.ConnectionError("x")
        catalog, found = self.find(cached=VERSIONS, get_html=dead, get=dead,
                                   head=dead)
        self.assertEqual(found, VERSIONS)
        self.assertIn("showing the version list from", catalog.warnings[0])

    def test_first_run_with_everything_down_fails_loudly(self):
        def dead(*a, **kw):
            raise requests.ConnectionError("x")
        with self.assertRaises(requests.RequestException):
            self.find(get_html=dead, get=dead)


class RunLogLine(unittest.TestCase):
    """One line per run; what it says and when it replaces itself."""

    def setUp(self):
        rd.HEALTH.server_errors, rd.HEALTH.last_code = 0, None

    def log(self, mode="menu", path=None):
        entry = rd.RunLog()
        entry.mode = mode
        if path:
            entry.begin(path, mode)
        return entry

    def test_quiet_run(self):
        self.assertTrue(self.log().body().endswith("nothing changed"))

    def test_downloads_are_counted(self):
        entry = self.log()
        for _ in range(28):
            entry.saved("2.25", "missing")
        self.assertIn("downloaded 2.25 (28)", entry.body())
        self.assertTrue(entry.changed())

    def test_looking_is_not_a_change(self):
        entry = self.log()
        entry.saw("3.5")
        entry.saw("3.4")
        self.assertFalse(entry.changed())
        self.assertIn("checked 3.5, 3.4", entry.body())

    def test_first_sight_of_a_release_on_an_unheld_version_is_not_a_change(self):
        entry = self.log()
        entry.release("3.5", None, "3.5.1")
        self.assertFalse(entry.changed())

    def test_a_zstream_advance_is_a_change(self):
        entry = self.log()
        entry.release("3.4", "3.4.3", "3.4.4")
        self.assertTrue(entry.changed())
        self.assertIn("release 3.4.3→3.4.4", entry.body())

    def test_an_outage_is_noted(self):
        rd.HEALTH.server_errors, rd.HEALTH.last_code = 3, 503
        try:
            self.assertIn("upstream 503", self.log().body())
        finally:
            rd.HEALTH.server_errors, rd.HEALTH.last_code = 0, None


class RunLogFile(unittest.TestCase):
    """
    Writing the file, with a fake clock: the dedupe key is the line minus
    its timestamp, so real runs need distinct seconds and sleeping for them
    would cost the suite ten seconds.
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = pathlib.Path(self.tmp.name)
        self.clock = 0
        self.strftime = rd.time.strftime
        rd.time.strftime = lambda fmt, *a: (
            f"2026-10-16 12:00:{self.clock:02d}" if "%S" in fmt and "T" not in fmt
            else self.strftime(fmt, *a))

    def tearDown(self):
        rd.time.strftime = self.strftime
        self.tmp.cleanup()

    def write(self, setup=lambda e: None):
        self.clock += 1
        entry = rd.RunLog()
        entry.begin(self.dir, "menu")
        setup(entry)
        entry.write()

    def lines(self):
        return (self.dir / rd.RUNLOG_FILENAME).read_text().splitlines()

    def test_quiet_runs_collapse_to_one_refreshed_line(self):
        for _ in range(3):
            self.write()
        self.assertEqual(len(self.lines()), 1)

    def test_identical_real_changes_are_both_kept(self):
        download = lambda e: e.saved("2.25", "missing")
        self.write(download)
        self.write(download)
        self.assertEqual(sum("downloaded" in l for l in self.lines()), 2)

    def test_newest_line_is_on_top(self):
        self.write(lambda e: e.saved("2.25", "missing"))
        self.write()
        self.assertTrue(self.lines()[0].endswith("nothing changed"))

    def test_no_temporary_file_is_left_behind(self):
        self.write()
        self.assertEqual(list(self.dir.glob("*.tmp")), [])

    def test_no_directory_is_created_just_to_log(self):
        missing = self.dir / "not-created"
        entry = rd.RunLog()
        entry.begin(missing, "menu")
        entry.write()
        self.assertFalse(missing.exists())


class BusyRow(unittest.TestCase):
    def test_spinner_is_green_and_names_the_stage(self):
        fragments = rd.busy_fragments("Loading guide lists…")
        self.assertTrue(any(s.startswith("fg:#00ff88") for s, _ in fragments))
        self.assertIn("Loading guide lists…", text_of(fragments))

    def test_spinner_animates(self):
        seen = set()
        for _ in range(40):
            seen.add(rd.spinner_frame())
            time.sleep(0.011)
        self.assertGreater(len(seen), 1)


class RecheckWindow(unittest.TestCase):
    """
    A version compared within the window is left alone by the background
    pass, and says so rather than implying it was just checked.
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.lib = pathlib.Path(self.tmp.name)
        self.saved = (rd.get_html, rd.SESSION.get, rd.enrich_with_upstream)
        self.console = rd.console
        rd.console = rd.Console(file=io.StringIO())   # keep the suite quiet
        self.compared = []
        rd.enrich_with_upstream = lambda ch, st, man, out, v: \
            self.compared.append(v)
        rd.get_html = lambda url, **kw: menu_html(VERSIONS)

    def tearDown(self):
        rd.get_html, rd.SESSION.get, rd.enrich_with_upstream = self.saved
        rd.console = self.console
        self.tmp.cleanup()

    def manifest_with(self, age_seconds):
        stamp = time.strftime("%Y-%m-%dT%H:%M:%SZ",
                              time.gmtime(time.time() - age_seconds))
        entries = {rd.manifest_key("3.5", f"g{i}", "PDF"):
                   {"sha256": "x", "upstream_size": 1, "last_check": stamp}
                   for i in range(2)}
        entries["_versions"] = {"found": ["3.5"], "latest": "3.5",
                                "checked_at": stamp}
        (self.lib / "manifest.json").write_text(json.dumps(entries))

    def run_catalog(self, hours):
        catalog = rd.Catalog(rd.load_manifest(self.lib), self.lib,
                             {"versions": ["3.5"], "recheck_after_hours": hours},
                             argparse.Namespace(rediscover=False, verify=False),
                             "pdf")
        chapters = [{"slug": "g0", "title": "A", "category": "C"},
                    {"slug": "g1", "title": "B", "category": "C"}]
        rd.discover_chapters = lambda v: chapters
        # both guides present on disk, so the version counts as held
        (self.lib / "3.5").mkdir(exist_ok=True)
        for i in range(2):
            (self.lib / "3.5" / f"g{i}.pdf").write_bytes(b"x")
        catalog.run_sync()
        return catalog

    def test_recent_comparison_is_skipped_and_dated(self):
        self.manifest_with(3 * 3600)          # compared three hours ago
        catalog = self.run_catalog(hours=6)
        self.assertEqual(self.compared, [], "re-compared inside the window")
        self.assertIn("3.5", catalog.ages)
        self.assertNotIn("3.5", catalog.checked,
                         "skipping must not mark it checked — opening it re-checks")

    def test_stale_comparison_is_redone(self):
        self.manifest_with(8 * 3600)
        self.run_catalog(hours=6)
        self.assertEqual(self.compared, ["3.5"])

    def test_zero_hours_disables_the_skip(self):
        self.manifest_with(60)
        self.run_catalog(hours=0)
        self.assertEqual(self.compared, ["3.5"])

    def test_row_says_how_old_the_answer_is(self):
        at_width(200)
        rows = rd.version_rows(["3.5"], "3.5", {"3.5": DATA["3.5"]}, MANIFEST,
                               timing={"3.5": 2.0}, ages={"3.5": 3 * 3600 + 60})
        widths = rd.row_widths(rows)
        text = text_of(rd.row_tail(rows[0], widths))
        self.assertIn("3h ago", text)
        self.assertNotIn("checked", text, "the heading says that already")
        self.assertNotIn("took", text, "an age and a duration are not both true")

    def test_age_wording(self):
        self.assertEqual(rd.fmt_age(45 * 60), "45m")
        self.assertEqual(rd.fmt_age(3 * 3600 + 59 * 60), "3h")
        self.assertEqual(rd.fmt_age(50 * 3600), "2d")

    def test_unreadable_timestamp_means_compare_again(self):
        self.assertIsNone(rd.seconds_since(None))
        self.assertIsNone(rd.seconds_since("not a date"))
        self.assertIsNone(rd.last_compared({}, "3.5", [{"slug": "g0"}]))


class PageReuse(unittest.TestCase):
    """The version menu and the chapter list want the same 365 KB page."""

    def setUp(self):
        rd.PAGE_CACHE.clear()
        self.get_html = rd.get_html
        self.fetched = []
        rd.get_html = lambda url, **kw: (self.fetched.append(url),
                                         menu_html(VERSIONS))[1]

    def tearDown(self):
        rd.get_html = self.get_html
        rd.PAGE_CACHE.clear()

    def test_chapter_list_reuses_the_discovery_page(self):
        rd.versions_from_picker("3.5")
        rd.discover_chapters("3.5")
        self.assertEqual(len(self.fetched), 1, f"fetched twice: {self.fetched}")

    def test_the_page_is_handed_over_only_once(self):
        rd.versions_from_picker("3.5")
        rd.discover_chapters("3.5")
        rd.discover_chapters("3.5")
        self.assertEqual(len(self.fetched), 2)

    def test_another_version_is_not_served_from_the_cache(self):
        rd.versions_from_picker("3.5")
        rd.discover_chapters("3.4")
        self.assertEqual(len(self.fetched), 2)


class SkippedHeading(unittest.TestCase):
    """The heading carries the one number the rows cannot: when next due."""

    class Cat:
        def __init__(self, ages, hours=1):
            self.ages = ages
            self.config = {"recheck_after_hours": hours}
            self.versions = ["3.5", "3.4", "3.3"]
            self.state = {v: "ready" for v in self.versions}

    def test_counts_the_skipped_and_dates_the_soonest(self):
        label = rd.versions_label(
            self.Cat({"3.5": 6 * 60, "3.4": 17 * 60, "2.25": 5 * 60}), 6)
        self.assertIn("3 checked within 1h", label)
        self.assertIn("next due in 55m", label)   # from the youngest, 5m old

    def test_window_is_described_not_assumed(self):
        label = rd.versions_label(self.Cat({"3.5": 2 * 3600}, hours=6), 6)
        self.assertIn("within 6h", label)
        self.assertIn("next due in 4h", label)

    def test_about_to_expire_says_due_now(self):
        self.assertIn("due now", rd.versions_label(self.Cat({"3.5": 3590}), 6))

    def test_nothing_skipped_leaves_the_heading_alone(self):
        self.assertEqual(rd.versions_label(self.Cat({}), 6),
                         "  ─── Versions (6) ───")

    def test_no_catalog_is_safe(self):
        self.assertEqual(rd.versions_label(None, 6), "  ─── Versions (6) ───")


class OpenInDefaultApp(unittest.TestCase):
    """Enter on a leaf hands the file to the desktop's own viewer."""

    def setUp(self):
        self.popen = rd.subprocess.Popen
        self.platform = rd.sys.platform
        self.launched = []
        rd.subprocess.Popen = lambda cmd, **kw: self.launched.append((cmd, kw))

    def tearDown(self):
        rd.subprocess.Popen = self.popen
        rd.sys.platform = self.platform

    def test_linux_uses_xdg_open(self):
        rd.sys.platform = "linux"
        self.assertIsNone(rd.open_in_default_app(pathlib.Path("/tmp/a.pdf")))
        cmd, kw = self.launched[0]
        self.assertEqual(cmd, ["xdg-open", "/tmp/a.pdf"])

    def test_macos_uses_open(self):
        rd.sys.platform = "darwin"
        rd.open_in_default_app(pathlib.Path("/tmp/a.pdf"))
        self.assertEqual(self.launched[0][0], ["open", "/tmp/a.pdf"])

    def test_viewer_is_detached_and_silenced(self):
        rd.sys.platform = "linux"
        rd.open_in_default_app(pathlib.Path("/tmp/a.pdf"))
        _cmd, kw = self.launched[0]
        self.assertTrue(kw["start_new_session"], "viewer would die with us")
        self.assertEqual(kw["stdout"], rd.subprocess.DEVNULL)
        self.assertEqual(kw["stderr"], rd.subprocess.DEVNULL,
                         "stray output would corrupt the menu frame")

    def test_missing_launcher_is_reported_not_raised(self):
        rd.sys.platform = "linux"
        def boom(*a, **kw):
            raise FileNotFoundError(2, "No such file or directory", "xdg-open")
        rd.subprocess.Popen = boom
        problem = rd.open_in_default_app(pathlib.Path("/tmp/a.pdf"))
        self.assertIn("FileNotFoundError", problem)


class LibraryLeafPaths(unittest.TestCase):
    """Which rows of the tree are openable, and what they point at."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.lib = pathlib.Path(self.tmp.name)
        self.version = self.lib / "3.5"
        (self.version / rd.ARCHIVE_DIRNAME).mkdir(parents=True)
        (self.version / "release_notes.pdf").write_bytes(b"%PDF-1.7")
        (self.version / rd.INDEX_FILENAME).write_text("# index")
        (self.version / rd.CHANGELOG_FILENAME).write_text("# log")
        (self.version / rd.ARCHIVE_DIRNAME / "old.pdf").write_bytes(b"%PDF-1.7")
        self.entries = [{
            "name": "3.5", "path": self.version, "files": 1, "bytes": 8,
            "archived": 1, "archived_bytes": 8, "snapshot": False,
            "z_version": "3.5.1", "has_index": True, "has_changelog": True,
        }]
        self.data = {"3.5": {
            "chapters": [{"slug": "release_notes", "title": "Release Notes",
                          "category": "About"},
                         {"slug": "never_fetched", "title": "Not Downloaded",
                          "category": "About"}],
            "statuses": {"release_notes": [
                {"label": "PDF", "filename": "release_notes.pdf",
                 "status": "ok", "doc_date": "2026-10-15", "recorded_size": 8}]},
        }}

    def tearDown(self):
        self.tmp.cleanup()

    def leaves(self):
        _choices, paths = rd.library_choices(self.entries, {"3.5"}, self.data)
        return {p.name for p in paths.values()}

    def test_downloaded_guide_is_openable(self):
        self.assertIn("release_notes.pdf", self.leaves())

    def test_generated_files_and_archive_are_openable(self):
        self.assertTrue({rd.INDEX_FILENAME, rd.CHANGELOG_FILENAME,
                         rd.ARCHIVE_DIRNAME} <= self.leaves())

    def test_a_guide_never_downloaded_opens_nothing(self):
        _choices, paths = rd.library_choices(self.entries, {"3.5"}, self.data)
        self.assertFalse(any("never_fetched" in str(p) for p in paths.values()))

    def test_category_headings_open_nothing(self):
        choices, paths = rd.library_choices(self.entries, {"3.5"}, self.data)
        headings = [c for c in choices
                    if isinstance(c.title, str) and c.title.endswith("About")]
        self.assertTrue(headings, "no category heading in the tree")
        self.assertFalse(any(h.value in paths for h in headings))

    def test_collapsed_version_exposes_no_leaves(self):
        _choices, paths = rd.library_choices(self.entries, set(), self.data)
        self.assertEqual(paths, {})

    def test_index_still_renders_the_same_tree(self):
        lines = rd.guide_tree_lines(self.data["3.5"]["chapters"],
                                    self.data["3.5"]["statuses"])
        self.assertTrue(all(isinstance(l, str) for l in lines))
        self.assertTrue(any("release_notes.pdf" in l for l in lines))


class ConfigResolution(unittest.TestCase):
    """
    Where settings come from when the script is installed on PATH. The
    symlink case matters most: it used to silently fall back to defaults.
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = pathlib.Path(self.tmp.name)
        self.env = {k: os.environ.get(k)
                    for k in ("RHOAI_DOCS_CONFIG", "XDG_CONFIG_HOME")}
        for k in self.env:
            os.environ.pop(k, None)
        self.bundled = rd.BUNDLED_CONFIG

    def tearDown(self):
        for k, v in self.env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        rd.BUNDLED_CONFIG = self.bundled
        self.tmp.cleanup()

    def test_script_dir_follows_a_symlink_to_the_real_file(self):
        link = self.dir / "rhoai-docs"
        link.symlink_to(pathlib.Path(rd.__file__).resolve())
        namespace = {"__file__": str(link), "__name__": "probe"}
        exec(compile(link.read_text(), str(link), "exec"), namespace)
        self.assertEqual(namespace["SCRIPT_DIR"],
                         pathlib.Path(rd.__file__).resolve().parent,
                         "a symlinked install would lose the bundled config")

    def test_bundled_config_is_the_fallback(self):
        self.assertEqual(rd.config_path(), rd.BUNDLED_CONFIG)

    def test_user_config_wins_over_the_bundled_one(self):
        (self.dir / "rhoai-docs").mkdir()
        user = self.dir / "rhoai-docs" / "config.yaml"
        user.write_text("output_dir: ~/elsewhere\n")
        os.environ["XDG_CONFIG_HOME"] = str(self.dir)
        self.assertEqual(rd.config_path(), user)
        self.assertEqual(rd.load_config()["output_dir"], "~/elsewhere")

    def test_env_var_wins_over_everything(self):
        one_off = self.dir / "one-off.yaml"
        one_off.write_text("output_dir: ~/from-env\n")
        (self.dir / "rhoai-docs").mkdir()
        (self.dir / "rhoai-docs" / "config.yaml").write_text("output_dir: ~/xdg\n")
        os.environ["XDG_CONFIG_HOME"] = str(self.dir)
        os.environ["RHOAI_DOCS_CONFIG"] = str(one_off)
        self.assertEqual(rd.load_config()["output_dir"], "~/from-env")

    def test_no_config_anywhere_falls_back_to_the_constants(self):
        rd.BUNDLED_CONFIG = self.dir / "absent.yaml"
        os.environ["XDG_CONFIG_HOME"] = str(self.dir / "empty")
        self.assertIsNone(rd.config_path())
        config = rd.load_config()
        self.assertEqual(config["output_dir"], rd.DEFAULT_OUTPUT_DIR)
        self.assertEqual(config["recheck_after_hours"], rd.DEFAULT_RECHECK_HOURS)

    def test_a_partial_user_config_keeps_the_other_defaults(self):
        (self.dir / "rhoai-docs").mkdir()
        (self.dir / "rhoai-docs" / "config.yaml").write_text("format: both\n")
        os.environ["XDG_CONFIG_HOME"] = str(self.dir)
        config = rd.load_config()
        self.assertEqual(config["format"], "both")
        self.assertEqual(config["output_dir"], rd.DEFAULT_OUTPUT_DIR)


if __name__ == "__main__":
    unittest.main(verbosity=2)
