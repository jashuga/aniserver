"""Tests for the safety net (~/.local/bin/htpc-watchdog) with a simulated Sonarr.
Run: python3 -m unittest discover -s ~/.local/opt/manga-request/tests -v"""
import importlib.machinery
import importlib.util
import tempfile
import time
import unittest
from pathlib import Path

from helpers import ROOT       # also keeps the tests away from the real services

loader = importlib.machinery.SourceFileLoader("watchdog", str(ROOT.parent / "bin/htpc-watchdog"))
spec = importlib.util.spec_from_loader("watchdog", loader)
wd = importlib.util.module_from_spec(spec)
loader.exec_module(wd)

NOW = time.time()
HOUR = 3600


def ep(i, season, number, aired_hours_ago, monitored=True, has_file=False):
    t = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(NOW - aired_hours_ago * HOUR))
    return {"id": i, "seasonNumber": season, "episodeNumber": number, "monitored": monitored, "hasFile": has_file, "airDateUtc": t}


class Fake:
    def __init__(self, series, episodes, queue=()):
        self.series, self.episodes, self.queue, self.commands, self.followed, self.pushes = series, episodes, list(queue), [], [], []

    def sonarr(self, method, path, body=None, timeout=60):
        if path.startswith("/queue"):
            return {"records": [{"episodeId": i} for i in self.queue]}
        if path == "/series":
            return self.series
        if path.startswith("/episode?seriesId="):
            return self.episodes[int(path.split("=")[1])]
        if method == "POST" and path == "/command":
            self.commands.append(body)
            return {}
        raise AssertionError(path)


class Watchdog(unittest.TestCase):
    def run_check(self, fake, state):
        saved = (wd.app.sonarr, wd.app.follow_new_episodes, wd.push, wd.DRY, wd.log)
        wd.app.sonarr, wd.DRY, wd.log = fake.sonarr, False, lambda msg: None
        wd.app.follow_new_episodes = lambda sid: fake.followed.append(sid)
        wd.push = lambda title, msg: fake.pushes.append(title)
        try:
            wd.check_shows(state, NOW)
        finally:
            wd.app.sonarr, wd.app.follow_new_episodes, wd.push, wd.DRY, wd.log = saved

    def test_show_left_with_nothing_to_download_is_switched_back_on_and_reported_once(self):
        # what the Steel Ball Run bug produced: JoJo in Sonarr, every episode unmonitored, no files
        fake = Fake([{"id": 7, "title": "JoJo"}], {7: [ep(1, 6, 1, 4000, monitored=False), ep(2, 6, 2, 200, monitored=False)]})
        state = {"searched": {}, "alerted": {}}
        self.run_check(fake, state)
        self.assertEqual(fake.followed, [7])
        self.assertEqual(len(fake.pushes), 1)
        self.run_check(fake, state)                # next run: fixed again if needed, but no second alert
        self.assertEqual(len(fake.pushes), 1)

    def test_aired_missing_episode_is_searched_again_then_reported_after_two_days(self):
        fake = Fake([{"id": 7, "title": "JoJo"}], {7: [ep(1, 6, 1, 4000, has_file=True), ep(2, 6, 2, 7), ep(3, 6, 3, 50)]})
        state = {"searched": {}, "alerted": {}}
        self.run_check(fake, state)
        self.assertEqual(fake.commands, [{"name": "EpisodeSearch", "episodeIds": [2, 3]}])
        self.assertEqual(fake.pushes, ["JoJo: no release found yet"])        # only S06E03 is 2+ days late
        self.run_check(fake, state)                # 3 hours later: not searched again yet, not reported again
        self.assertEqual(len(fake.commands), 1)
        self.assertEqual(len(fake.pushes), 1)

    def test_downloading_or_just_aired_episodes_are_left_alone(self):
        fake = Fake([{"id": 7, "title": "JoJo"}], {7: [ep(1, 6, 1, 30), ep(2, 6, 2, 2), ep(3, 6, 3, -100)]}, queue=[1])
        state = {"searched": {}, "alerted": {}}
        self.run_check(fake, state)
        self.assertEqual((fake.commands, fake.pushes, fake.followed), ([], [], []))


class ServiceHealth(unittest.TestCase):
    def run_services(self, ok, state, now):
        restarts, pushes = [], []
        services = [("Jellyfin", (lambda: None) if ok else (lambda: 1 / 0), "jellyfin", "systemctl --user restart jellyfin"),
                    ("Sonarr", lambda: None, None, "sudo systemctl restart sonarr")]
        saved = (wd.SERVICES, wd.subprocess.run, wd.push, wd.DRY, wd.log)
        wd.SERVICES, wd.DRY, wd.log = services, False, lambda msg, quiet=False: None
        wd.subprocess.run = lambda cmd, timeout=None: restarts.append(cmd[-1])
        wd.push = lambda title, msg: pushes.append(title)
        try:
            wd.check_services(state, now)
        finally:
            wd.SERVICES, wd.subprocess.run, wd.push, wd.DRY, wd.log = saved
        return restarts, pushes

    def test_down_service_is_restarted_then_reported_once_then_cleared(self):
        state = {"searched": {}, "alerted": {}}
        restarts, pushes = self.run_services(False, state, NOW)
        self.assertEqual((restarts, pushes), (["jellyfin"], []))                     # restarted right away, no alert yet
        restarts, pushes = self.run_services(False, state, NOW + 15 * 60)
        self.assertEqual((restarts, pushes), ([], []))                               # not restarted again within 30 min
        restarts, pushes = self.run_services(False, state, NOW + 30 * 60)
        self.assertEqual(pushes, ["Jellyfin isn't responding"])                     # still down after 20+ min: one alert
        self.assertEqual(self.run_services(False, state, NOW + 45 * 60)[1], [])      # never twice
        self.run_services(True, state, NOW + 60 * 60)                                # it came back
        self.assertNotIn("Jellyfin", state["down"])
        self.assertNotIn("svc:Jellyfin", state["alerted"])


class StuckImports(unittest.TestCase):
    MISREAD = "Episode 1x02 was not found in the grabbed release: [Erai-raws] Show S01 - 01 ~ 03 [BATCH]"

    def run_check(self, sonarr_queue, radarr_queue, state, now, history=(), found=()):
        calls, pushes = [], []

        def api(queue):
            def call(method, path, body=None, timeout=60):
                if method != "GET":
                    calls.append((method, path, body))
                    return {}
                if path.startswith("/history"):
                    return {"records": [{"eventType": e} for e in history]}
                if path.startswith("/manualimport"):
                    return list(found)
                return {"records": queue}
            return call
        saved = (wd.app.sonarr, wd.app.radarr, wd.app.qbit, wd.push, wd.DRY, wd.log)
        wd.app.sonarr, wd.app.radarr, wd.DRY, wd.log = api(sonarr_queue), api(radarr_queue), False, lambda msg, quiet=False: None
        wd.app.qbit = lambda path, data=None, timeout=30: [{"hash": "batch", "content_path": "/dl/Show batch"}]
        wd.push = lambda title, msg: pushes.append(title)
        try:
            wd.check_stuck_imports(state, now)
        finally:
            wd.app.sonarr, wd.app.radarr, wd.app.qbit, wd.push, wd.DRY, wd.log = saved
        return calls, pushes

    @staticmethod
    def queue(dl, episodes, have, message):
        blocked = {"trackedDownloadState": "importBlocked", "statusMessages": [{"messages": [message]}]}
        return [{**blocked, "id": i, "downloadId": dl, "seriesId": 7, "episodeId": ep, "series": {"title": "Show"},
                 "episode": {"hasFile": have}} for i, ep in enumerate(episodes, 1)]

    @staticmethod
    def file(n, season=1, episode_id=None, reason=MISREAD):
        return {"path": f"/dl/Show batch/Show - {n:02d}.mkv", "series": {"id": 7}, "quality": {"quality": {"name": "WEBDL-1080p"}},
                "episodes": [{"id": episode_id or 100 + n, "seasonNumber": season}], "rejections": [{"reason": reason}] if reason else []}

    def test_unused_extra_copy_is_deleted_and_blocklisted_after_the_grace_period(self):
        # Re:Zero: an extra Blu-ray volume for episodes already on disk
        q = self.queue("BD", [101, 102], True, "Episode 1x09 was unexpected considering the S4 folder name")
        movie = [{"trackedDownloadState": "importBlocked", "id": 9, "downloadId": "M", "movie": {"title": "Encanto", "hasFile": True}}]
        state = {"searched": {}, "alerted": {}}
        self.assertEqual(self.run_check(q, movie, state, NOW), ([], []))                      # just noticed: wait
        calls, pushes = self.run_check(q, movie, state, NOW + HOUR)
        removed = [(path.split("?")[1].split("&")[:2], body) for method, path, body in calls]
        self.assertEqual(removed, [(["removeFromClient=true", "blocklist=true"], {"ids": [1, 2]}),
                                   (["removeFromClient=true", "blocklist=true"], {"ids": [9]})])
        self.assertEqual(pushes, [])
        self.assertEqual(state["stuck"], {})

    def test_download_whose_files_were_imported_is_only_untracked(self):
        q = self.queue("USED", [101, 102], True, self.MISREAD)
        calls, pushes = self.run_check(q, [], {"searched": {}, "alerted": {}, "stuck": {"Sonarr:USED": NOW - HOUR}}, NOW,
                                       history=["grabbed", "downloadFolderImported"])
        self.assertEqual([path.split("?")[1].split("&")[:2] for method, path, body in calls], [["removeFromClient=false", "blocklist=false"]])

    def test_misread_batch_is_imported_when_every_file_reads_cleanly(self):
        # Sonarr took "S01 - 01 ~ 03" for episode 1: it imported 01, and refuses 02 and 03
        q = self.queue("batch", [101, 102, 103], False, self.MISREAD)
        found = [self.file(1, reason="Episode file already imported at 10/6/2026"), self.file(2), self.file(3)]
        calls, pushes = self.run_check(q, [], {"searched": {}, "alerted": {}, "stuck": {"Sonarr:batch": NOW - HOUR}}, NOW, found=found)
        self.assertEqual(len(calls), 1)
        method, path, body = calls[0]
        self.assertEqual((method, path, body["name"], body["importMode"]), ("POST", "/command", "ManualImport", "copy"))
        self.assertEqual([(f["path"].rsplit("/", 1)[1], f["episodeIds"]) for f in body["files"]], [("Show - 02.mkv", [102]), ("Show - 03.mkv", [103])])
        self.assertEqual(pushes, [])

    def test_batch_that_sonarr_maps_oddly_is_left_alone_and_reported_once(self):
        # Brotherhood: TVDB's absolute numbers count specials, so file 21 reads as a special and 22 as episode 21
        q = self.queue("fmab", [101, 102, 103, 900], False, self.MISREAD)
        found = [self.file(1, reason="Episode file already imported at 10/6/2026"), self.file(2), self.file(3, season=0, episode_id=900)]
        state = {"searched": {}, "alerted": {}, "stuck": {"Sonarr:fmab": NOW - HOUR}}
        calls, pushes = self.run_check(q, [], state, NOW, found=found)
        self.assertEqual(calls, [])                                                          # nothing imported
        self.assertEqual(pushes, ["Show: a download finished but couldn't be added"])
        self.assertEqual(self.run_check(q, [], state, NOW + HOUR, found=found), ([], []))   # never twice
        self.run_check([], [], state, NOW + 2 * HOUR)                                        # fixed by hand
        self.assertEqual(state["stuck"], {})


class NewlyAddedOldShow(unittest.TestCase):
    def test_old_episodes_count_as_missing_from_when_the_show_was_added(self):
        added = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(NOW - HOUR))      # Brotherhood: added an hour ago, aired 2010
        fake = Fake([{"id": 18, "title": "Brotherhood", "added": added}], {18: [ep(1, 1, 64, 140000)]})
        state = {"searched": {}, "alerted": {}}
        Watchdog.run_check(self, fake, state)
        self.assertEqual((fake.commands, fake.pushes), ([], []))                     # its download gets a chance first
        saved = wd.app.sonarr, wd.app.follow_new_episodes, wd.push, wd.DRY, wd.log
        wd.app.sonarr, wd.DRY, wd.log = fake.sonarr, False, lambda msg: None
        wd.push = lambda title, msg: fake.pushes.append((title, msg))
        try:
            wd.check_shows(state, NOW + 7 * HOUR)
            self.assertEqual(len(fake.commands), 1)                                   # 6 hours after adding: search again
            wd.check_shows(state, NOW + 50 * HOUR)
        finally:
            wd.app.sonarr, wd.app.follow_new_episodes, wd.push, wd.DRY, wd.log = saved
        self.assertEqual(len(fake.pushes), 1)
        self.assertIn("2+ days after the show was added", fake.pushes[0][1])


class KodiLibrary(unittest.TestCase):
    """The TV must list everything Sonarr/Radarr have (JoJo S06E04 was missing because Sonarr skipped Kodi's update while
    a video was open; Kodi had also matched The Apothecary Diaries to a different show by name)."""

    def run_check(self, kodi_shows, kodi_movies, series, movies, state, now, kodi_up=True):
        calls, pushes = [], []

        def kodi_rpc(method, params=None):
            if not kodi_up:
                raise OSError("connection refused")
            if method == "VideoLibrary.GetTVShows":
                return {"result": {"tvshows": kodi_shows}}
            if method == "VideoLibrary.GetMovies":
                return {"result": {"movies": kodi_movies}}
            calls.append((method, params))
            return {"result": "OK"}
        saved = (wd.app.kodi_rpc, wd.app.sonarr, wd.app.radarr, wd.push, wd.DRY, wd.log)
        wd.app.kodi_rpc, wd.DRY, wd.log = kodi_rpc, False, lambda msg, quiet=False: None
        wd.app.sonarr = lambda method, path, body=None, timeout=60: series
        wd.app.radarr = lambda method, path, body=None, timeout=60: movies
        wd.push = lambda title, msg: pushes.append(title)
        try:
            wd.check_kodi(state, now)
        finally:
            wd.app.kodi_rpc, wd.app.sonarr, wd.app.radarr, wd.push, wd.DRY, wd.log = saved
        return calls, pushes

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        for name in ("JoJo", "Brotherhood", "Apothecary"):
            (self.root / name).mkdir()
        self.state = {"searched": {}, "alerted": {}}

    def tearDown(self):
        self.tmp.cleanup()

    def show(self, sid, name, files, tvdb):
        return {"id": sid, "title": name, "path": str(self.root / name), "tvdbId": tvdb, "statistics": {"episodeFileCount": files}}

    def in_kodi(self, kid, name, episodes, tvdb):
        return {"tvshowid": kid, "label": name, "file": str(self.root / name) + "/", "episode": episodes, "uniqueid": {"tvdb": str(tvdb)}}

    def test_missing_episode_is_scanned_once_and_the_show_gets_a_tvshow_nfo(self):
        series = [self.show(1, "JoJo", 4, 79151)]
        calls, pushes = self.run_check([self.in_kodi(6, "JoJo", 3, 79151)], [], series, [], self.state, NOW)
        self.assertEqual(calls, [("VideoLibrary.Scan", {"directory": str(self.root / "JoJo") + "/", "showdialogs": False})])
        self.assertEqual((self.root / "JoJo/tvshow.nfo").read_text().strip(), "https://thetvdb.com/?tab=series&id=79151")
        self.assertEqual(self.run_check([self.in_kodi(6, "JoJo", 3, 79151)], [], series, [], self.state, NOW + HOUR), ([], []))
        self.run_check([self.in_kodi(6, "JoJo", 4, 79151)], [], series, [], self.state, NOW + 2 * HOUR)      # caught up
        self.assertEqual(self.state["kodi"], {})

    def test_brand_new_show_is_found_through_its_parent_folder(self):
        calls, _ = self.run_check([], [], [self.show(2, "Brotherhood", 64, 85249)], [], self.state, NOW)
        self.assertEqual(calls, [("VideoLibrary.Scan", {"directory": str(self.root) + "/", "showdialogs": False})])

    def test_show_matched_by_name_to_the_wrong_entry_is_reidentified(self):
        calls, _ = self.run_check([self.in_kodi(9, "Apothecary", 0, 482073)], [], [self.show(3, "Apothecary", 2, 431162)], [], self.state, NOW)
        self.assertEqual(calls, [("VideoLibrary.RefreshTVShow", {"tvshowid": 9, "ignorenfo": False, "refreshepisodes": True})])

    def test_movies_and_several_shows_behind_mean_one_full_scan(self):
        movie = {"id": 5, "title": "Encanto", "path": str(self.root / "Movies/Encanto (2021)"), "movieFile": {"path": str(self.root / "Movies/Encanto (2021)/e.mkv")}}
        calls, _ = self.run_check([], [], [self.show(1, "JoJo", 4, 79151)], [movie], self.state, NOW)
        self.assertEqual(calls, [("VideoLibrary.Scan", {"showdialogs": False})])

    def test_nothing_happens_while_kodi_is_closed(self):
        self.assertEqual(self.run_check([], [], [self.show(1, "JoJo", 4, 79151)], [], self.state, NOW, kodi_up=False), ([], []))
        self.assertFalse((self.root / "JoJo/tvshow.nfo").exists())

    def test_still_missing_after_three_scans_is_reported_once(self):
        series, kodi = [self.show(1, "JoJo", 4, 79151)], [self.in_kodi(6, "JoJo", 3, 79151)]
        pushes = [self.run_check(kodi, [], series, [], self.state, NOW + n * 7 * HOUR)[1] for n in range(5)]
        self.assertEqual(pushes, [[], [], ["JoJo isn't showing up on the TV"], [], []])


if __name__ == "__main__":
    unittest.main()
