"""Tests for the account page (password changes), deleting a show/movie, and the live Downloads data.
Everything runs against temporary files and simulated Sonarr/Radarr/qBittorrent - nothing real is touched.
Run: python3 -m unittest discover -s ~/.local/opt/manga-request/tests -v"""
import importlib.util
import json
import tempfile
import time
import unittest
from pathlib import Path

from helpers import Patch, app


class WebsitePassword(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.users = Path(self.tmp.name) / "remote-users.json"
        self.users.write_text(json.dumps({"tester": {"pw": app.hash_password("old-password-123"), "totp": "JBSWY3DPEHPK3PXP", "last": 0}}))
        self.creds = Path(self.tmp.name) / "credentials.txt"
        self.creds.write_text("")
        self.patch = Patch(REMOTE_USERS_FILE=self.users, CREDENTIALS_FILE=self.creds)
        self.patch.__enter__()

    def tearDown(self):
        self.patch.__exit__()
        self.tmp.cleanup()

    def test_wrong_current_password_is_refused(self):
        self.assertEqual(app.change_website_password("tester", "nope", "new-password-456", "new-password-456"), "wrong-current")

    def test_new_password_rules(self):
        self.assertIn("12 characters", app.change_website_password("tester", "old-password-123", "short", "short"))
        self.assertIn("don't match", app.change_website_password("tester", "old-password-123", "new-password-456", "new-password-457"))
        self.assertIn("same as the current", app.change_website_password("tester", "old-password-123", "old-password-123", "old-password-123"))

    def test_changing_signs_out_every_other_device(self):
        exp = int(time.time()) + 3600
        session, trusted = app.remote_token("tester", exp), app.remote_token("tester", exp, "trusted")
        self.assertEqual(app.remote_token_user(session), "tester")
        self.assertIsNone(app.change_website_password("tester", "old-password-123", "new-password-456", "new-password-456"))
        self.assertIsNone(app.remote_token_user(session))                    # old sign-ins stop working...
        self.assertIsNone(app.remote_token_user(trusted, "trusted"))         # ...including remembered devices
        self.assertEqual(app.remote_token_user(app.remote_token("tester", exp)), "tester")    # new ones work
        rec = app.remote_users()["tester"]
        self.assertTrue(app.check_password("new-password-456", rec["pw"]))
        self.assertEqual(rec["totp"], "JBSWY3DPEHPK3PXP")                    # authenticator code unchanged

    def test_existing_sign_ins_survive_this_update(self):
        # users who never changed their password (no "gen") keep their current cookies after the upgrade
        import hashlib, hmac
        exp = int(time.time()) + 3600
        old_style = f"tester:{exp}:" + hmac.new(app.CONF["secret"].encode(), f"tester:{exp}:remote".encode(), hashlib.sha256).hexdigest()
        self.assertEqual(app.remote_token_user(old_style), "tester")


class CredentialsNote(unittest.TestCase):
    def test_only_that_persons_line_changes(self):
        with tempfile.TemporaryDirectory() as d:
            f = Path(d) / "c.txt"
            f.write_text("Jellyfin  x  user: alice    password: basil-birch\n"
                         "Jellyfin  (Bob)  user: bob    password: otter-lemon  (ask Bob to change it)\n"
                         "Komga  user: bob@home.lan    password: example-pw\n")
            with Patch(CREDENTIALS_FILE=f):
                app.note_password_changed("Jellyfin", "bob")
            lines = f.read_text().splitlines()
            self.assertIn("basil-birch", lines[0])
            self.assertNotIn("otter-lemon", lines[1])
            self.assertIn("changed on the website", lines[1])
            self.assertIn("example-pw", lines[2])

    def test_any_persons_change_it_note_goes_away_with_the_old_password(self):
        with tempfile.TemporaryDirectory() as d:
            f = Path(d) / "c.txt"
            f.write_text("Jellyfin  (Bob)  user: bob    password: otter-lemon  (ask Bob to change it)\n"
                         "Jellyfin  (Carol)   user: carol     password: maple-onyx  (ask Carol to change it)\n")
            with Patch(CREDENTIALS_FILE=f):
                app.note_password_changed("Jellyfin", "carol")
            bob, carol = f.read_text().splitlines()
            self.assertIn("otter-lemon", bob)
            self.assertNotIn("maple-onyx", carol)
            self.assertNotIn("ask Carol", carol)
            self.assertIn("changed on the website", carol)


class FakeArr:
    def __init__(self, responses):
        self.responses, self.calls = responses, []

    def __call__(self, method, path, body=None, timeout=60):
        self.calls.append((method, path))
        if method == "DELETE":
            return None
        return self.responses[path]


class DeleteTitle(unittest.TestCase):
    def run_delete(self, key, sonarr, radarr, torrents):
        qcalls, refreshed = [], []

        def qbit(path, data=None, timeout=30):
            if path == "/torrents/info":
                return [{"hash": h} for h in torrents]
            qcalls.append((path, data))
        with tempfile.TemporaryDirectory() as d, Patch(sonarr=sonarr, radarr=radarr, qbit=qbit, DELETE_LOG=Path(d) / "deleted.log",
                                                        jellyfin=lambda *a, **k: refreshed.append("jellyfin"),
                                                        kodi_rpc=lambda *a, **k: refreshed.append("kodi")):
            result = app.delete_title(key, "alice")
            log = (Path(d) / "deleted.log").read_text()
        return result, qcalls, refreshed, log

    def test_deleting_a_show(self):
        sonarr = FakeArr({"/series": [{"id": 5, "tvdbId": 111, "title": "Show A", "statistics": {"sizeOnDisk": 2e9}},
                                      {"id": 6, "tvdbId": 222, "title": "Show B", "statistics": {"sizeOnDisk": 1e9}}],
                          "/history/series?seriesId=5": [{"downloadId": "AAA"}, {"downloadId": "BBB"}, {}],
                          "/queue?pageSize=1000": {"records": [{"id": 9, "seriesId": 5, "downloadId": "CCC"},
                                                               {"id": 10, "seriesId": 6, "downloadId": "DDD"}]}})
        (title, size), qcalls, refreshed, log = self.run_delete("s111", sonarr, FakeArr({}), ["aaa", "ccc", "ddd"])
        self.assertEqual((title, size), ("Show A", 2e9))
        self.assertIn(("DELETE", "/queue/9?removeFromClient=true&blocklist=false"), sonarr.calls)
        self.assertNotIn(("DELETE", "/queue/10?removeFromClient=true&blocklist=false"), sonarr.calls)     # other show untouched
        self.assertIn(("DELETE", "/series/5?deleteFiles=true&addImportListExclusion=false"), sonarr.calls)
        self.assertEqual(len(qcalls), 1)
        self.assertEqual(sorted(qcalls[0][1]["hashes"].split("|")), ["aaa", "ccc"])       # seeding copies too (BBB is already gone)
        self.assertEqual(qcalls[0][1]["deleteFiles"], "true")
        self.assertEqual(refreshed, ["jellyfin", "kodi"])
        self.assertIn("alice deleted Show A", log)

    def test_deleting_a_movie(self):
        radarr = FakeArr({"/movie": [{"id": 3, "tmdbId": 568124, "title": "Encanto", "year": 2021, "sizeOnDisk": 5e9}],
                          "/history/movie?movieId=3": [{"downloadId": "EEE"}],
                          "/queue?pageSize=1000": {"records": []}})
        (title, size), qcalls, _, _ = self.run_delete("m568124", FakeArr({}), radarr, ["eee", "fff"])
        self.assertEqual((title, size), ("Encanto (2021)", 5e9))
        self.assertIn(("DELETE", "/movie/3?deleteFiles=true&addImportExclusion=false"), radarr.calls)
        self.assertEqual(qcalls[0][1]["hashes"], "eee")


class LiveDownloads(unittest.TestCase):
    def test_states_and_names(self):
        now = time.time()
        torrents = [
            {"hash": "a1", "name": "Show.S01E05.1080p", "category": "sonarr", "progress": 0.5, "state": "downloading", "dlspeed": 5e6,
             "eta": 120, "size": 1e9, "amount_left": 5e8, "num_seeds": 12, "num_complete": 300, "num_leechs": 4, "added_on": now - 60},
            {"hash": "a2", "name": "Show.S01E06.1080p", "category": "sonarr", "progress": 0.1, "state": "downloading", "dlspeed": 0,
             "eta": 8640000, "size": 1e9, "amount_left": 9e8, "added_on": now - 50},
            {"hash": "a3", "name": "Movie.2021.1080p", "category": "radarr", "progress": 1, "state": "stalledUP", "size": 5e9,
             "amount_left": 0, "added_on": now - 900, "completion_on": now - 30},
            {"hash": "a4", "name": "Show.S01E04.1080p", "category": "sonarr", "progress": 1, "state": "uploading", "size": 1e9,
             "amount_left": 0, "added_on": now - 9000, "completion_on": now - 8000},
            {"hash": "a5", "name": "Old.S01E01", "category": "sonarr", "progress": 1, "state": "stalledUP", "size": 1e9,
             "amount_left": 0, "added_on": now - 9 * 86400, "completion_on": now - 8 * 86400},
            {"hash": "a6", "name": "[1r0n] Some Manga v07 (Digital) (1r0n)", "category": "manga", "progress": 0, "state": "metaDL",
             "size": 0, "amount_left": 0, "added_on": now - 10},
            {"hash": "a7", "name": "Not.Ours", "category": "", "progress": 0.3, "state": "downloading", "size": 1, "amount_left": 1},
            {"hash": "a8", "name": "Show S4 - Vol.3 [BD]", "category": "sonarr", "progress": 1, "state": "queuedUP", "size": 7e9,
             "amount_left": 0, "added_on": now - 3000, "completion_on": now - 2000}]
        names = ({"A1": {"kind": "show", "title": "Show", "eps": {(1, 5): "Five"}, "poster": "p", "link": "abc"},
                  "A3": {"kind": "movie", "title": "Movie (2021)", "eps": {}, "poster": "", "link": None},
                  "A4": {"kind": "show", "title": "Show", "eps": {(1, 4): ""}, "poster": "", "link": "abc"}},
                 {"A3": "importing", "A8": "blocked"}, {"A4": "2026-10-03T10:00:00Z"})
        app._SWR.pop("dlnames", None)
        with Patch(qbit_live=lambda: torrents, _download_names=lambda: names):
            data = app.live_downloads()
        app._SWR.pop("dlnames", None)
        by = {i["id"]: i for i in data["items"]}
        self.assertEqual(set(by), {"A1", "A2", "A3", "A4", "A6", "A8"})    # 8-day-old finished one and other categories left out
        self.assertEqual((by["A1"]["state"], by["A1"]["title"], by["A1"]["sub"], by["A1"]["link"]), ("downloading", "Show", "S01E05 · Five", "/title/abc"))
        self.assertEqual(by["A1"]["eta"], 120)
        self.assertEqual(by["A2"]["state"], "stalled")                     # "downloading" at 0 B/s
        self.assertIsNone(by["A2"]["eta"])
        self.assertEqual(by["A3"]["state"], "importing")                   # finished, Radarr still moving it into the library
        self.assertEqual(by["A4"]["state"], "ready")
        self.assertEqual(by["A8"]["state"], "blocked")                     # finished, but Sonarr can't import it
        self.assertEqual((by["A6"]["state"], by["A6"]["kind"]), ("finding", "manga"))
        self.assertEqual([i["id"] for i in data["items"]][0], "A1")         # active downloads first
        self.assertEqual(data["speed"], 5e6)


class HubCards(unittest.TestCase):
    def test_a_download_sonarr_or_radarr_cant_import_is_not_shown_as_downloading(self):
        # what Re:Zero looked like: every episode on disk, plus an extra Blu-ray volume stuck as "importBlocked"
        stuck = {"trackedDownloadState": "importBlocked", "status": "completed", "size": 7e9, "sizeleft": 0}
        sonarr_data = {"/series": [{"id": 13, "tvdbId": 1, "title": "Show", "statistics": {"episodeCount": 2, "episodeFileCount": 2}}],
                       "/queue?pageSize=1000&includeEpisode=true": {"records": [
                           {**stuck, "seriesId": 13, "downloadId": "BD", "title": "Show S4 - Vol.3", "episodeId": 1},
                           {**stuck, "seriesId": 13, "downloadId": "BD", "title": "Show S4 - Vol.3", "episodeId": 2},
                           {"seriesId": 13, "downloadId": "NEW", "title": "Show S04E03", "trackedDownloadState": "downloading",
                            "status": "downloading", "size": 1e9, "sizeleft": 5e8, "episodeId": 3}]},
                       "/command": []}
        radarr_data = {"/movie": [{"id": 5, "tmdbId": 9, "title": "Movie", "hasFile": True, "movieFile": {"path": "/x.mkv"}}],
                       "/queue?pageSize=1000": {"records": [{**stuck, "movieId": 5, "downloadId": "M", "title": "Movie.BD"}]},
                       "/command": []}
        with Patch(sonarr=lambda method, path, body=None, timeout=60: sonarr_data[path],
                   radarr=lambda method, path, body=None, timeout=60: radarr_data[path], qbit_live=lambda: []):
            cards = {c["title"]: c for c in app.hub_cards(with_files=False)}
        self.assertEqual([d["state"] for d in cards["Show"]["downloads"]], ["downloading"])   # only the real download
        self.assertEqual(cards["Movie"]["downloads"], [])


if __name__ == "__main__":
    unittest.main()
