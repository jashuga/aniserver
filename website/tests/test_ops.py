"""The owner's Server page (aniserver/ops.py): only the owner gets it, stuck downloads are found and replaced, the
Jellyfin password rescue checks what it's given, and the watchdog's log is read back."""
import os
import shutil
import tempfile
import time
import unittest
from pathlib import Path

from helpers import Patch, app

import aniserver.ops as ops        # noqa: E402 (after helpers, which loads the site)

NOW = 2_000_000_000


class FakeRequest:
    """Drives Handler.do_GET/do_POST without a socket; `user` is who is signed in."""
    def __init__(self, path, user, form=None):
        h = app.Handler.__new__(app.Handler)
        h.path, h.headers, h.client_address = path, {}, ("10.0.0.5", 1)
        h.user, h.remote_user = user, user
        h.authed = lambda: True
        h.form = lambda: form or {}
        self.sent, self.redirected = None, None
        h.send = lambda body, code=200, headers=None: setattr(self, "sent", (code, body))
        h.redirect = lambda to, cookie=None: setattr(self, "redirected", to)
        self.h = h


class OwnerOnly(unittest.TestCase):
    def test_only_the_owner_sees_the_server_page_and_its_buttons(self):
        other = next(u for u in app.PROFILES if u != app.OWNER)
        r = FakeRequest("/server", other)
        r.h.do_GET()
        self.assertEqual(r.sent[0], 404)
        restarted = []
        with Patch(restart_service=restarted.append):
            for path in ("/server/restart", "/account/jellyfin-reset", "/account/quickconnect"):
                r = FakeRequest(path, other, {"name": "Sonarr", "new": "x" * 12, "repeat": "x" * 12, "code": "ABC123"})
                r.h.do_POST()
                self.assertEqual(r.redirected, "/")
            self.assertEqual(restarted, [])
            r = FakeRequest("/server/restart", app.OWNER, {"name": "Sonarr"})
            r.h.do_POST()
            self.assertEqual(restarted, ["Sonarr"])
            self.assertIn("ok=Sonarr+restarted", r.redirected)
        self.assertIn('href="/server"', app.page("", "home", user=app.OWNER))
        self.assertNotIn('href="/server"', app.page("", "home", user=other))


class StuckDownloads(unittest.TestCase):
    def torrent(self, h, state, progress=0.0, speed=0, hours=0):
        return {"hash": h, "state": state, "progress": progress, "dlspeed": speed, "added_on": NOW - hours * 3600,
                "last_activity": NOW - hours * 3600}

    def test_dead_torrents_are_found_and_moving_or_new_ones_left_alone(self):
        torrents = [self.torrent("aa", "metaDL", hours=300), self.torrent("bb", "stalledDL", 0.4, hours=10),
                    self.torrent("cc", "downloading", 0.2, speed=500_000, hours=50), self.torrent("dd", "metaDL", hours=1)]
        queue = {"records": [{"id": n, "downloadId": h.upper(), "title": f"Show.S01E0{n}",
                              "series": {"title": "Show"}, "episode": {"seasonNumber": 1, "episodeNumber": n}}
                             for n, h in enumerate(("aa", "bb", "cc", "dd"), 1)]}
        with Patch(qbit=lambda path, *a, **k: torrents if "filter=downloading" in path else [],
                   sonarr=lambda m, p, *a, **k: queue, radarr=lambda m, p, *a, **k: {"records": []}):
            found = ops.stuck_downloads(NOW)
        self.assertEqual([(x["id"], x["what"], x["hours"]) for x in found], [(1, "Show S01E01", 300), (2, "Show S01E02", 10)])
        self.assertIn("never started", found[0]["why"])
        self.assertIn("40%", found[1]["why"])

    def test_a_finished_download_that_cant_be_filed_is_listed_for_removal(self):
        queue = {"records": [{"id": 5, "downloadId": "EE", "title": "Show.S01E05.REPACK", "trackedDownloadState": "importPending",
                              "trackedDownloadStatus": "warning", "series": {"title": "Show"},
                              "episode": {"seasonNumber": 1, "episodeNumber": 5, "hasFile": True},
                              "statusMessages": [{"messages": ["Not a Custom Format upgrade for existing episode file(s)."]}]},
                             {"id": 6, "downloadId": "FF", "title": "Show.S01E06", "trackedDownloadState": "importPending",
                              "trackedDownloadStatus": "ok", "series": {"title": "Show"}, "episode": {"seasonNumber": 1, "episodeNumber": 6}}]}
        with Patch(qbit=lambda path, *a, **k: [], sonarr=lambda m, p, *a, **k: queue, radarr=lambda m, p, *a, **k: {"records": []}):
            found = ops.stuck_downloads(NOW)
        self.assertEqual([(x["id"], x["have"]) for x in found], [(5, True)])          # S01E06 is just importing
        self.assertIn("isn't better", found[0]["why"])
        calls = []
        with Patch(sonarr=lambda m, p, *a, **k: calls.append(p)):
            ops.replace_download("sonarr", "5", search=False)
        self.assertTrue(calls[0].endswith("skipRedownload=true"))

    def test_replacing_blocklists_and_searches_again(self):
        calls = []
        with Patch(radarr=lambda m, p, *a, **k: calls.append((m, p))):
            ops.replace_download("radarr", "77")
        self.assertEqual(calls, [("DELETE", "/queue/77?removeFromClient=true&blocklist=true&skipRedownload=false")])
        with self.assertRaises(KeyError):
            ops.replace_download("prowlarr", "1")


class JellyfinRescue(unittest.TestCase):
    def setUp(self):
        self.calls = []

        def jf(path, method="GET", body=None):
            self.calls.append((method, path, body))
            return [{"Name": "Alice", "Id": "u1"}] if path == "/Users" else True
        self.patch = Patch(jellyfin=jf)
        self.patch.__enter__()

    def tearDown(self):
        self.patch.__exit__()

    def test_a_new_password_is_set_without_the_old_one_after_checking_it(self):
        self.assertIn("12 characters", ops.reset_jellyfin_password("alice", "short", "short"))
        self.assertIn("don't match", ops.reset_jellyfin_password("alice", "a" * 12, "b" * 12))
        self.assertEqual(self.calls, [])
        self.assertIsNone(ops.reset_jellyfin_password("alice", "a" * 12, "a" * 12))
        self.assertEqual(self.calls[-1], ("POST", "/Users/u1/Password", {"CurrentPw": "", "NewPw": "a" * 12}))

    def test_quick_connect_codes_are_approved_for_the_signed_in_user(self):
        self.assertIsNotNone(ops.approve_quick_connect("alice", "12; DROP"))
        self.assertIsNone(ops.approve_quick_connect("alice", " 760 276 "))
        self.assertEqual(self.calls[-1][:2], ("POST", "/QuickConnect/Authorize?code=760276&userId=u1"))


class RecycleBin(unittest.TestCase):
    def test_emptying_frees_the_bin_and_the_torrents_that_only_seed_it(self):
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            bin_, dl, lib = d / "Media/.recycle", d / "downloads", d / "Anime"
            for f in (bin_ / "Old Show", dl / "old", dl / "kept", lib):
                f.mkdir(parents=True)
            big = b"x" * 60_000_000
            (dl / "old/ep1.mkv").write_bytes(big)                          # replaced copy: torrent + recycle bin
            os.link(dl / "old/ep1.mkv", bin_ / "Old Show/ep1.mkv")
            (dl / "kept/ep2.mkv").write_bytes(big)                         # torrent + recycle bin + still in the library
            os.link(dl / "kept/ep2.mkv", bin_ / "Old Show/ep2.mkv")
            os.link(dl / "kept/ep2.mkv", lib / "ep2.mkv")
            (bin_ / "deleted.mkv").write_bytes(b"y" * 1000)                # only in the bin
            torrents = [{"hash": "old", "progress": 1, "category": "sonarr", "content_path": str(dl / "old")},
                        {"hash": "kept", "progress": 1, "category": "sonarr", "content_path": str(dl / "kept")},
                        {"hash": "manga", "progress": 1, "category": "manga", "content_path": str(dl / "old")}]
            deleted = []

            def qb(path, data=None, timeout=30):
                if path == "/torrents/delete":
                    deleted.append(data)
                    for h in data["hashes"].split("|"):
                        shutil.rmtree(dl / h)
                    return None
                return torrents
            with Patch(qbit=qb, RECYCLE=bin_, HOME=d, sleep=lambda s: None):
                total, freed = ops.recycle_size()
                self.assertEqual((total, freed), (120_001_000, 60_001_000))
                ops.empty_recycle_bin()
            self.assertEqual(deleted, [{"hashes": "old", "deleteFiles": "true"}])
            self.assertEqual(list(bin_.iterdir()), [])
            self.assertTrue((lib / "ep2.mkv").exists() and (dl / "kept/ep2.mkv").exists())

    def test_it_refuses_a_bin_that_is_home_or_root(self):
        with Patch(RECYCLE=Path("/")):
            with self.assertRaises(RuntimeError):
                ops.empty_recycle_bin()


class WatchdogLog(unittest.TestCase):
    def test_newest_first_with_alerts_marked_and_dry_runs_left_out(self):
        with tempfile.TemporaryDirectory() as d:
            log, state = Path(d, "watchdog.log"), Path(d, "watchdog.json")
            log.write_text("2026-11-20 19:59:37 FMAB S01E01: its old file is gone\n"
                           "2026-11-20 20:00:00 [dry-run] would do something\n"
                           "2026-11-21 08:15:02 alert: Show: no release found yet | S01E02 aired 3 days ago.\n")
            state.write_text("{}")
            with Patch(WATCHDOG_LOG=log, WATCHDOG_STATE=state):
                acts, last = ops.watchdog_activity()
        self.assertEqual(acts, [("2026-11-21 08:15", True, "Show: no release found yet: S01E02 aired 3 days ago."),
                                ("2026-11-20 19:59", False, "FMAB S01E01: its old file is gone")])
        self.assertAlmostEqual(last, time.time(), delta=60)


if __name__ == "__main__":
    unittest.main()
