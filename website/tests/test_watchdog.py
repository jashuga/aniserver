"""Tests for the safety net (~/.local/bin/htpc-watchdog) with a simulated Sonarr.
Run: python3 -m unittest discover -s ~/.local/opt/manga-request/tests -v"""
import importlib.machinery
import importlib.util
import time
import unittest
from pathlib import Path

loader = importlib.machinery.SourceFileLoader("watchdog", str(Path.home() / ".local/bin/htpc-watchdog"))
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
    def run_check(self, sonarr_queue, radarr_queue, state, now):
        calls, pushes = [], []

        def api(queue):
            def call(method, path, body=None, timeout=60):
                if method == "DELETE":
                    calls.append((path.split("?")[0], body))
                    return None
                return {"records": queue}
            return call
        saved = (wd.app.sonarr, wd.app.radarr, wd.push, wd.DRY, wd.log)
        wd.app.sonarr, wd.app.radarr, wd.DRY, wd.log = api(sonarr_queue), api(radarr_queue), False, lambda msg, quiet=False: None
        wd.push = lambda title, msg: pushes.append(title)
        try:
            wd.check_stuck_imports(state, now)
        finally:
            wd.app.sonarr, wd.app.radarr, wd.push, wd.DRY, wd.log = saved
        return calls, pushes

    def test_extra_copy_is_removed_after_an_hour_and_a_real_problem_is_reported_once(self):
        blocked = {"trackedDownloadState": "importBlocked", "statusMessages": [{"messages": ["Episode 1x09 was unexpected"]}]}
        sonarr_queue = [  # an extra Blu-ray volume for episodes already on disk (the Re:Zero case) ...
            {**blocked, "id": 1, "downloadId": "BD", "series": {"title": "Re:Zero"}, "episode": {"hasFile": True}},
            {**blocked, "id": 2, "downloadId": "BD", "series": {"title": "Re:Zero"}, "episode": {"hasFile": True}},
            # ... and a download that was the only copy of an episode
            {**blocked, "id": 3, "downloadId": "ONLY", "series": {"title": "JoJo"}, "episode": {"hasFile": False}},
            {"trackedDownloadState": "downloading", "id": 4, "downloadId": "OK", "series": {"title": "JoJo"}, "episode": {"hasFile": False}}]
        radarr_queue = [{**blocked, "id": 9, "downloadId": "M", "movie": {"title": "Encanto", "hasFile": True}}]
        state = {"searched": {}, "alerted": {}}
        self.assertEqual(self.run_check(sonarr_queue, radarr_queue, state, NOW), ([], []))         # just noticed: wait
        calls, pushes = self.run_check(sonarr_queue, radarr_queue, state, NOW + 2 * HOUR)
        self.assertEqual(calls, [("/queue/bulk", {"ids": [1, 2]}), ("/queue/bulk", {"ids": [9]})])
        self.assertEqual(pushes, ["JoJo: a download finished but couldn't be added"])
        calls, pushes = self.run_check(sonarr_queue[2:], [], state, NOW + 3 * HOUR)               # still stuck later
        self.assertEqual((calls, pushes), ([], []))                                                  # never twice
        self.run_check([], [], state, NOW + 4 * HOUR)                                                # fixed by hand
        self.assertEqual(state["stuck"], {})


if __name__ == "__main__":
    unittest.main()
