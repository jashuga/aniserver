"""Batch-first search (aniserver/search.py) against a simulated Sonarr and Prowlarr."""
import unittest
import urllib.parse

from helpers import Patch, app

SEASON1 = [{"id": n, "seasonNumber": 1, "episodeNumber": n} for n in range(1, 13)]
SEASON2 = [{"id": 100 + n, "seasonNumber": 2, "episodeNumber": n} for n in range(1, 13)]
SPECIAL = [{"id": 900, "seasonNumber": 0, "episodeNumber": 1}]
PROFILE = {"id": 7, "minFormatScore": 100, "items": [
    {"quality": {"id": 1, "name": "SDTV"}, "allowed": False},
    {"name": "WEB 1080p", "allowed": True, "items": [{"quality": {"id": 3}}, {"quality": {"id": 9}}]},
    {"name": "Bluray 1080p", "allowed": True, "items": [{"quality": {"id": 7}}]}]}


def release(title, seeders=20, indexer="Nyaa.si"):
    return {"guid": title, "title": title, "protocol": "torrent", "seeders": seeders, "indexer": indexer, "size": 9e9,
            "publishDate": "2026-10-01T00:00:00Z", "downloadUrl": f"http://prowlarr/dl/{len(title)}"}


class FakeSonarr:
    def __init__(self, parses, accept=lambda title: True):
        self.parses, self.accept, self.commands, self.pushed = parses, accept, [], []

    def __call__(self, method, path, body=None, timeout=60):
        if path == "/episode?seriesId=5":
            return SEASON1 + SEASON2 + SPECIAL
        if path == "/series/5":
            return {"id": 5, "title": "Orb", "qualityProfileId": 7, "alternateTitles": [{"title": "Chi. Chikyuu no Undou ni Tsuite"}]}
        if path == "/qualityprofile/7":
            return PROFILE
        if path == "/indexer":
            return [{"name": "Nyaa.si (Prowlarr)"}, {"name": "AnimeTosho.xyz (Prowlarr)"}]
        if path.startswith("/parse?title="):
            return self.parses.get(urllib.parse.unquote(path.split("=", 1)[1]))
        if method == "POST" and path == "/release/push":
            self.pushed.append((body["title"], body["indexer"]))
            ok = self.accept(body["title"])
            return [{"approved": ok, "rejections": [] if ok else ["Existing file meets cutoff"]}]
        if method == "POST" and path == "/command":
            self.commands.append(body)
            return {}
        raise AssertionError(f"unexpected Sonarr call {method} {path}")


def parsed(episodes, quality=9, score=500, series=5):
    return {"series": {"id": series}, "episodes": episodes, "customFormatScore": score,
            "parsedEpisodeInfo": {"quality": {"quality": {"id": quality}}}}


class BatchFirst(unittest.TestCase):
    def run_search(self, sonarr, releases, ids, prowlarr_up=True):
        queries = []

        def prowlarr(method, path, body=None, timeout=90):
            if not prowlarr_up:
                raise OSError("Prowlarr isn't answering")
            queries.append(urllib.parse.parse_qs(urllib.parse.urlsplit(path).query)["query"][0])
            return releases
        with Patch(sonarr=sonarr, prowlarr=prowlarr):
            done = app.search_episodes(5, ids)
        return done, queries

    def test_the_best_batch_is_pushed_and_nothing_else_searched(self):
        parses = {"[SubsPlease] Orb (01-12)": parsed(SEASON1, score=300),
                  "[Judas] Orb (Season 1) [Batch]": parsed(SEASON1, score=900),
                  "[LQ] Orb S01": parsed(SEASON1, score=-10000),                     # below the profile's minimum
                  "[SubsPlease] Orb - 05": parsed(SEASON1[4:5], score=900),          # one episode: not a batch
                  "[Judas] Other Show (Season 1)": parsed(SEASON1, score=900, series=6)}
        sonarr = FakeSonarr(parses)
        done, queries = self.run_search(sonarr, [release(t) for t in parses], [e["id"] for e in SEASON1])
        self.assertEqual(sonarr.pushed, [("[Judas] Orb (Season 1) [Batch]", "Nyaa.si (Prowlarr)")])
        self.assertEqual(done, {"batches": ["[Judas] Orb (Season 1) [Batch]"], "season_searches": [], "episode_search": []})
        self.assertEqual(sonarr.commands, [])
        self.assertEqual(queries, ["Orb", "Chi. Chikyuu no Undou ni Tsuite"])

    def test_a_turned_down_batch_is_followed_by_the_next_and_then_by_sonarrs_own_search(self):
        parses = {f"Orb S01 v{n}": parsed(SEASON1, score=900 - n) for n in range(6)}
        sonarr = FakeSonarr(parses, accept=lambda title: False)
        done, _ = self.run_search(sonarr, [release(t) for t in parses], [e["id"] for e in SEASON1])
        self.assertEqual([t for t, _ in sonarr.pushed], [f"Orb S01 v{n}" for n in range(4)])   # MAX_PUSHES, best first
        self.assertEqual(sonarr.commands, [{"name": "SeasonSearch", "seriesId": 5, "seasonNumber": 1}])
        self.assertEqual(done["season_searches"], [1])

    def test_better_quality_wins_over_format_score_like_in_sonarr_and_big_batches_can_be_left_out(self):
        parses = {"Orb S01 WEB": parsed(SEASON1, quality=9, score=900), "Orb S01 BD": parsed(SEASON1, quality=7, score=200)}
        releases = [release("Orb S01 WEB"), dict(release("Orb S01 BD"), size=60e9)]
        sonarr = FakeSonarr(parses)
        with Patch(sonarr=sonarr):
            ranked = app.batch_candidates(sonarr("GET", "/series/5"), {1: {e["id"] for e in SEASON1}}, releases)
            capped = app.batch_candidates(sonarr("GET", "/series/5"), {1: {e["id"] for e in SEASON1}}, releases, max_episode_gb=1.5)
        self.assertEqual([r["title"] for r, _ in ranked], ["Orb S01 BD", "Orb S01 WEB"])
        self.assertEqual([r["title"] for r, _ in capped], ["Orb S01 WEB"])            # 60 GB / 12 episodes is over 1.5

    def test_one_multi_season_batch_covers_both_seasons(self):
        parses = {"[Judas] Orb (Seasons 1-2) [Batch]": parsed(SEASON1 + SEASON2)}
        sonarr = FakeSonarr(parses)
        done, _ = self.run_search(sonarr, [release(t) for t in parses], [e["id"] for e in SEASON1 + SEASON2])
        self.assertEqual(len(sonarr.pushed), 1)
        self.assertEqual(sonarr.commands, [])

    def test_a_few_episodes_and_specials_skip_the_batch_search(self):
        sonarr = FakeSonarr({})
        done, queries = self.run_search(sonarr, [], [1, 2, 900])
        self.assertEqual(queries, [])
        self.assertEqual(sonarr.commands, [{"name": "EpisodeSearch", "episodeIds": [900, 1, 2]}])

    def test_without_prowlarr_it_falls_back_to_season_searches(self):
        sonarr = FakeSonarr({})
        done, _ = self.run_search(sonarr, [], [e["id"] for e in SEASON1 + SEASON2], prowlarr_up=False)
        self.assertEqual(sonarr.commands, [{"name": "SeasonSearch", "seriesId": 5, "seasonNumber": 1},
                                           {"name": "SeasonSearch", "seriesId": 5, "seasonNumber": 2}])

    def test_releases_from_indexers_sonarr_does_not_use_are_ignored(self):
        parses = {"[Judas] Orb (Season 1) [Batch]": parsed(SEASON1)}
        sonarr = FakeSonarr(parses)
        self.run_search(sonarr, [release("[Judas] Orb (Season 1) [Batch]", indexer="Nyaa.si (Manga)")], [e["id"] for e in SEASON1])
        self.assertEqual(sonarr.pushed, [])
        self.assertEqual(sonarr.commands, [{"name": "SeasonSearch", "seriesId": 5, "seasonNumber": 1}])


if __name__ == "__main__":
    unittest.main()
