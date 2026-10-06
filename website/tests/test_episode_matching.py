"""Tests for adding anime seasons: AniList entries -> the right TVDB episodes in Sonarr, matched by air date.
Run: python3 -m unittest discover -s ~/.local/opt/manga-request/tests -v
(Regression: JoJo Steel Ball Run's "2nd & 3rd STAGE" is TVDB season 6 episodes 2-12; it was guessed as a
non-existent season 7 and the show was left with nothing monitored.)"""
import calendar
import importlib.util
import time
import unittest
from pathlib import Path

spec = importlib.util.spec_from_file_location("app", Path(__file__).resolve().parent.parent / "app.py")
app = importlib.util.module_from_spec(spec)
spec.loader.exec_module(app)

DAY = 86400


def utc(y, m, d, hour=20):
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(calendar.timegm((y, m, d, hour, 0, 0))))


def weekly(season, first_ep, count, y, m, d, hour=20, start_id=1):
    t0 = calendar.timegm((y, m, d, hour, 0, 0))
    return [{"id": start_id + k, "seasonNumber": season, "episodeNumber": first_ep + k, "monitored": False, "hasFile": False,
             "airDateUtc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(t0 + 7 * DAY * k))} for k in range(count)]


def entry(start, end=None, episodes=None, status="FINISHED", fmt="TV"):
    date = lambda t: {"year": t[0], "month": t[1], "day": t[2]} if t else {"year": None, "month": None, "day": None}
    return {"id": 1, "format": fmt, "status": status, "episodes": episodes, "startDate": date(start), "endDate": date(end)}


def codes(eps):
    return [(e["seasonNumber"], e["episodeNumber"]) for e in eps]


# JoJo (TVDB 262954) season 6 as TVDB lists it: the 1st stage special, then the weekly 2nd/3rd stage
JOJO_S6 = ([{"id": 100, "seasonNumber": 6, "episodeNumber": 1, "monitored": False, "hasFile": False, "airDateUtc": utc(2026, 3, 19)}]
           + weekly(6, 2, 11, 2026, 9, 25, start_id=101))
SBR_1ST = entry((2026, 3, 19), (2026, 3, 19), 1, "FINISHED", "ONA")
SBR_2ND = entry((2026, 9, 25), None, 11, "RELEASING", "ONA")


class EntryEpisodes(unittest.TestCase):
    def test_steel_ball_run_1st_stage_is_s6e1(self):
        self.assertEqual(codes(app.entry_episodes(JOJO_S6, SBR_1ST)), [(6, 1)])

    def test_steel_ball_run_2nd_stage_is_s6e2_to_e12(self):
        self.assertEqual(codes(app.entry_episodes(JOJO_S6, SBR_2ND)), [(6, n) for n in range(2, 13)])

    def test_airing_entry_stops_at_its_episode_count(self):
        eps = JOJO_S6 + weekly(7, 1, 6, 2026, 12, 11, start_id=200)   # TVDB already lists what comes next
        self.assertEqual(codes(app.entry_episodes(eps, SBR_2ND))[-1], (6, 12))

    def test_split_cour_part_2_in_the_same_tvdb_season(self):
        s3 = weekly(3, 1, 8, 2024, 10, 2) + weekly(3, 9, 8, 2025, 2, 5, start_id=50)
        part2 = entry((2025, 2, 5), (2025, 3, 26), 8)
        self.assertEqual(codes(app.entry_episodes(s3, part2)), [(3, n) for n in range(9, 17)])

    def test_japan_date_vs_utc_air_time(self):
        # broadcast at midnight in Japan = 15:00 UTC the previous day
        eps = weekly(2, 1, 12, 2025, 1, 4, hour=15)
        tv = entry((2025, 1, 5), (2025, 3, 23), 12)
        self.assertEqual(len(app.entry_episodes(eps, tv)), 12)

    def test_previous_cour_ending_a_week_earlier_is_not_included(self):
        eps = weekly(1, 1, 12, 2025, 1, 5) + weekly(1, 13, 12, 2025, 3, 30, start_id=40)
        second = entry((2025, 3, 30), (2025, 6, 15), 12)
        self.assertEqual(codes(app.entry_episodes(eps, second)), [(1, n) for n in range(13, 25)])

    def test_netflix_all_at_once(self):
        eps = [dict(e, airDateUtc=utc(2026, 1, 15)) for e in weekly(1, 1, 10, 2026, 1, 15)]
        self.assertEqual(len(app.entry_episodes(eps, entry((2026, 1, 15), (2026, 1, 15), 10, fmt="ONA"))), 10)

    def test_finished_entry_keeps_a_recap_tvdb_counts_as_an_episode(self):
        eps = weekly(1, 1, 13, 2025, 4, 6)
        self.assertEqual(len(app.entry_episodes(eps, entry((2025, 4, 6), (2025, 6, 29), 12))), 13)

    def test_unknown_start_day_falls_back(self):
        self.assertEqual(app.entry_episodes(JOJO_S6, entry(None, None, 12, "NOT_YET_RELEASED")), [])

    def test_ova_matches_specials(self):
        eps = weekly(1, 1, 12, 2024, 1, 7) + [{"id": 90, "seasonNumber": 0, "episodeNumber": 3, "monitored": False,
                                               "hasFile": False, "airDateUtc": utc(2024, 8, 21)}]
        self.assertEqual(codes(app.entry_episodes(eps, entry((2024, 8, 21), (2024, 8, 21), 1, fmt="OVA"))), [(0, 3)])

    def test_tv_entry_never_grabs_specials(self):
        eps = [{"id": 90, "seasonNumber": 0, "episodeNumber": 3, "monitored": False, "hasFile": False, "airDateUtc": utc(2024, 8, 21)}]
        self.assertEqual(app.entry_episodes(eps, entry((2024, 8, 21), (2024, 8, 21), 1)), [])

    def test_netflix_first_release_with_later_tv_dates_on_tvdb(self):
        # Stone Ocean: Netflix 2021-12-01 (12 eps), TVDB lists the TV broadcast from January
        s5 = weekly(5, 1, 38, 2022, 1, 8)
        part1 = entry((2021, 12, 1), (2021, 12, 1), 12, fmt="ONA")
        self.assertEqual(codes(app.entry_episodes(s5, part1)), [(5, n) for n in range(1, 13)])

    def test_finished_entry_never_gets_fewer_episodes_than_it_has(self):
        # Stone Ocean part 2: Netflix Sep-Dec 2022 (26 eps); on TVDB's TV dates only some fall inside that window
        s5 = weekly(5, 1, 12, 2022, 1, 8) + weekly(5, 13, 26, 2022, 10, 1, start_id=60)
        part2 = entry((2022, 9, 1), (2022, 12, 1), 26, fmt="ONA")
        self.assertEqual(codes(app.entry_episodes(s5, part2)), [(5, n) for n in range(13, 39)])

    def test_ova_released_during_a_tv_season_does_not_take_the_season(self):
        # Thus Spoke Rohan Kishibe (4 OVAs, 2017-2020) vs Golden Wind (39 eps, 2018-2019), no matching specials on TVDB
        eps = weekly(4, 1, 39, 2018, 10, 5)
        rohan = entry((2017, 7, 31), (2020, 12, 30), 4, fmt="OVA")
        self.assertEqual(app.entry_episodes(eps, rohan), [])

    def test_description(self):
        self.assertEqual(app.describe_episodes(app.entry_episodes(JOJO_S6, SBR_2ND)), "season 6, episodes 2-12")
        self.assertEqual(app.describe_episodes(app.entry_episodes(JOJO_S6, SBR_1ST)), "season 6, episode 1")


class FakeSonarr:
    """Just enough of Sonarr's API: series with seasons, episodes, monitoring (incl. season -> episode cascade), commands."""
    def __init__(self, episodes, seasons):
        self.eps = [dict(e) for e in episodes]
        self.series = {"id": 1, "monitored": False, "seasons": [{"seasonNumber": n, "monitored": False} for n in seasons]}
        self.commands = []

    def __call__(self, method, path, body=None, timeout=60):
        if method == "GET" and path.startswith("/episode?"):
            return [dict(e) for e in self.eps]
        if method == "GET" and path.startswith("/series/"):
            return {**self.series, "seasons": [dict(x) for x in self.series["seasons"]]}
        if method == "PUT" and path.startswith("/series/"):
            before = {x["seasonNumber"]: x["monitored"] for x in self.series["seasons"]}
            for x in body["seasons"]:
                if x["monitored"] != before.get(x["seasonNumber"]):     # Sonarr flips every episode of that season
                    for e in self.eps:
                        if e["seasonNumber"] == x["seasonNumber"]:
                            e["monitored"] = x["monitored"]
            self.series = body
            return body
        if method == "PUT" and path == "/episode/monitor":
            for e in self.eps:
                if e["id"] in body["episodeIds"]:
                    e["monitored"] = body["monitored"]
            return None
        if method == "POST" and path == "/command":
            self.commands.append(body)
            return {"id": len(self.commands), "status": "queued"}
        raise AssertionError(f"unexpected Sonarr call {method} {path}")

    def monitored(self):
        return sorted((e["seasonNumber"], e["episodeNumber"]) for e in self.eps if e["monitored"])


class MonitorEntry(unittest.TestCase):
    def setUp(self):
        self._sonarr, self._time = app.sonarr, app.time.time
        app.time.time = lambda: calendar.timegm((2026, 10, 3, 12, 0, 0))     # "today": S6E3 aired yesterday

    def tearDown(self):
        app.sonarr, app.time.time = self._sonarr, self._time

    def test_adding_the_2nd_stage_monitors_exactly_its_episodes_and_searches_the_aired_ones(self):
        app.sonarr = fake = FakeSonarr(JOJO_S6, [0, 1, 2, 3, 4, 5, 6])
        what, aired = app.monitor_entry(1, SBR_2ND, season_hint=7)
        self.assertEqual(fake.monitored(), [(6, n) for n in range(2, 13)])       # E1 (1st stage) left alone
        self.assertTrue(fake.series["monitored"])
        self.assertTrue(next(x for x in fake.series["seasons"] if x["seasonNumber"] == 6)["monitored"])   # later listings too
        self.assertEqual(fake.commands, [{"name": "EpisodeSearch", "episodeIds": [101, 102]}])
        self.assertEqual((what, aired), ("season 6, episodes 2-12", 2))

    def test_adding_the_1st_stage_monitors_only_s6e1(self):
        app.sonarr = fake = FakeSonarr(JOJO_S6, [0, 6])
        app.monitor_entry(1, SBR_1ST)
        self.assertEqual(fake.monitored(), [(6, 1)])
        self.assertFalse(next(x for x in fake.series["seasons"] if x["seasonNumber"] == 6)["monitored"])

    def test_episodes_monitored_before_stay_monitored(self):
        eps = [dict(e, monitored=True) if e["episodeNumber"] == 1 else e for e in JOJO_S6]
        app.sonarr = fake = FakeSonarr(eps, [6])
        app.monitor_entry(1, SBR_2ND)
        self.assertEqual(fake.monitored(), [(6, n) for n in range(1, 13)])

    def test_nothing_on_tvdb_yet_keeps_following_the_show(self):
        app.sonarr = FakeSonarr(JOJO_S6, [6])
        self.assertIsNone(app.monitor_entry(1, entry((2027, 4, 1), None, 12, "NOT_YET_RELEASED")))
        followed = []
        app.follow_new_episodes, original = (lambda sid: followed.append(sid)), app.follow_new_episodes
        try:
            msg = app.entry_result(1, entry((2027, 4, 1), None, 12, "NOT_YET_RELEASED"), None)
        finally:
            app.follow_new_episodes = original
        self.assertEqual(followed, [1])
        self.assertIn("as soon as they're listed", msg)


class UpcomingEntries(unittest.TestCase):
    def test_upcoming_entry_does_not_fall_back_to_the_old_season(self):
        old = [dict(e, hasFile=False) for e in weekly(3, 1, 12, 2025, 10, 3)]
        app.sonarr, saved = FakeSonarr(old, [3]), app.sonarr
        try:
            self.assertIsNone(app.monitor_entry(1, entry((2027, 1, 9), None, 12, "NOT_YET_RELEASED"), season_hint=3))
            self.assertEqual(app.sonarr.monitored(), [])
            self.assertEqual(app.sonarr.commands, [])
        finally:
            app.sonarr = saved


class SeasonFallback(unittest.TestCase):
    def test_guessed_season_that_tvdb_does_not_have(self):
        self._lookup, app.tvdb_seasons = app.tvdb_seasons, lambda tvdb, library=None: {0, 1, 2, 3, 4, 5, 6}
        try:
            self.assertEqual(app.settle_season(262954, 7), 6)
            self.assertEqual(app.settle_season(262954, 6), 6)
        finally:
            app.tvdb_seasons = self._lookup


if __name__ == "__main__":
    unittest.main()
