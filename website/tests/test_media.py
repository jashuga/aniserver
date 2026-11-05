"""Regular movies and TV (aniserver/media.py): finding them, adding them with the regular profiles and folders, and
"Add & watch now" picking a release that can keep ahead of playback."""
import copy
import unittest

from helpers import Patch, app

import aniserver.media as media      # noqa: E402 (after helpers, which loads the site)


class FakeArr:
    """Just enough Sonarr/Radarr: lookups, profiles, the library, adds."""
    def __init__(self, lookup, library=(), profiles=("[Anime] Remux-1080p", "Movies 1080p", "TV 1080p")):
        self.lookup, self.library, self.added, self.commands, self.grabbed = lookup, list(library), [], [], []
        self.profiles = [{"id": n + 1, "name": p} for n, p in enumerate(profiles)]

    def __call__(self, method, path, body=None, timeout=60):
        if path in ("/movie", "/series") and method == "GET":
            return self.library
        if path == "/qualityprofile":
            return self.profiles
        if path.startswith(("/movie/lookup", "/series/lookup")):
            return copy.deepcopy(self.lookup)          # a fresh answer each time, like the real API
        if method == "POST" and path in ("/movie", "/series"):
            self.added.append(body)
            return dict(body, id=42)
        if method == "POST" and path == "/command":
            self.commands.append(body)
            return {}
        raise AssertionError(f"unexpected call {method} {path}")


SHOW = {"title": "Severance", "tvdbId": 371980, "year": 2022, "seriesType": "standard",
        "seasons": [{"seasonNumber": 0}, {"seasonNumber": 1}, {"seasonNumber": 2}],
        "originalLanguage": {"name": "English"}, "genres": ["Drama"], "ratings": {"votes": 900}}
MOVIE = {"title": "Dune", "tmdbId": 438631, "year": 2021, "digitalRelease": "2021-10-22T00:00:00Z",
         "originalLanguage": {"name": "English"}, "genres": ["Science Fiction"], "ratings": {"tmdb": {"value": 7.8, "votes": 12000}}}


class AddingTitles(unittest.TestCase):
    def setUp(self):
        self.searches, self.watching = [], []
        self.patch = Patch(drive_root=lambda folders, api: (folders[0], []), search_later=lambda sid, ids=None: self.searches.append(sid),
                           watch_while_downloading=lambda mid, user: self.watching.append((mid, user)))
        self.patch.__enter__()

    def tearDown(self):
        self.patch.__exit__()

    def test_a_show_is_added_as_a_regular_show_and_only_the_chosen_seasons_are_monitored(self):
        for mode, monitored in (("all", [False, True, True]), ("latest", [False, False, True]), ("future", [False, False, False])):
            sonarr = FakeArr([SHOW])
            with Patch(sonarr=sonarr):
                media.add_show(371980, mode)
            body = sonarr.added[0]
            self.assertEqual((body["seriesType"], body["qualityProfileId"]), ("standard", 3))      # TV 1080p
            self.assertTrue(body["rootFolderPath"].endswith("TV Shows"))
            self.assertEqual([x["monitored"] for x in body["seasons"]], monitored, mode)
            self.assertFalse(body["addOptions"]["searchForMissingEpisodes"])     # our season-first search does it
        self.assertEqual(self.searches, [42, 42])                                # not for "only new episodes"

    def test_a_movie_goes_to_the_movies_folder_and_add_and_watch_hands_it_to_the_tv(self):
        radarr = FakeArr(MOVIE)
        with Patch(radarr=radarr, TV=True):
            media.add_movie(438631, "alice")
            media.add_movie(438631, "alice", watch=True)
        plain, watch = radarr.added
        self.assertEqual(plain["qualityProfileId"], 2)                           # Movies 1080p
        self.assertTrue(plain["rootFolderPath"].endswith("/Movies"))
        self.assertTrue(plain["addOptions"]["searchForMovie"])
        self.assertFalse(watch["addOptions"]["searchForMovie"])                  # the watch-now thread picks the release
        self.assertEqual(self.watching, [(42, "alice")])

    def test_something_already_here_isnt_added_twice(self):
        radarr = FakeArr(MOVIE, library=[dict(MOVIE, id=7, hasFile=True)])
        with Patch(radarr=radarr):
            title, msg = media.add_movie(438631, "alice", watch=True)
        self.assertEqual((title, radarr.added, self.watching), ("Dune", [], []))
        self.assertIn("already", msg)

    def test_watching_while_downloading_prefers_the_best_release_with_enough_seeders(self):
        rel = lambda title, seeders, approved=True: {"title": title, "seeders": seeders, "approved": approved, "protocol": "torrent"}
        best_quality_few_seeds = rel("Dune.2021.1080p.BluRay.x264-GRP", 3)
        good = rel("Dune.2021.1080p.WEB-DL.DDP5.1-GRP", 80)
        rejected = rel("Dune.2021.CAM", 900, approved=False)
        self.assertEqual(media.pick_for_watching([rejected, best_quality_few_seeds, good])["title"], good["title"])
        self.assertEqual(media.pick_for_watching([best_quality_few_seeds, rel("x", 1)])["title"], best_quality_few_seeds["title"])
        self.assertIsNone(media.pick_for_watching([rejected]))


class WatchingWhileDownloading(unittest.TestCase):
    def test_the_picked_release_is_grabbed_and_its_download_handed_to_the_tv(self):
        calls, started = [], []

        def radarr(method, path, body=None, timeout=60):
            calls.append((method, path.split("?")[0]))
            if path.startswith("/release?"):
                return [{"guid": "g1", "indexerId": 3, "title": "Dune 1080p", "seeders": 50, "approved": True, "protocol": "torrent"}]
            if method == "POST" and path == "/release":
                self.assertEqual(body, {"guid": "g1", "indexerId": 3})
                return {}
            if path.startswith("/queue?"):
                return {"records": [{"movieId": 99, "downloadId": "OTHER"}, {"movieId": 42, "downloadId": "ABC123"}]}
            raise AssertionError(path)
        with Patch(radarr=radarr, watch_now=lambda h, user: started.append((h, user))):
            media.watch_while_downloading(42, "alice", log=lambda m: None)
        self.assertEqual(started, [("ABC123", "alice")])          # its own download, not another movie's

    def test_nothing_acceptable_leaves_it_to_radarrs_own_search(self):
        commands = []

        def radarr(method, path, body=None, timeout=60):
            if path.startswith("/release?"):
                return [{"title": "CAM", "approved": False, "seeders": 999, "protocol": "torrent"}]
            if path == "/command":
                commands.append(body)
                return {}
            raise AssertionError(path)
        with Patch(radarr=radarr, watch_now=lambda h, user: self.fail("nothing to watch")):
            media.watch_while_downloading(42, "alice", log=lambda m: None)
        self.assertEqual(commands, [{"name": "MoviesSearch", "movieIds": [42]}])


class FindingTitles(unittest.TestCase):
    def test_the_better_known_kind_comes_first_and_anime_is_sent_to_the_anime_page(self):
        films = [dict(MOVIE, title="Severance", tmdbId=1, ratings={"tmdb": {"votes": 40}})]
        shows = [SHOW, dict(SHOW, title="Severance: Anime", tvdbId=2, originalLanguage={"name": "Japanese"}, genres=["Animation"])]
        with Patch(radarr=FakeArr(films), sonarr=FakeArr(shows)):
            found = media.find_titles("severance")
        self.assertEqual([h for h, _ in found], ["TV shows", "Movies"])
        self.assertEqual([c["anime"] for c in found[0][1]], [False, True])
        html = app.media_details_html(found[0][1][1], 0, "/movies")
        self.assertIn("/anime?q=", html)
        self.assertNotIn("/media/addshow", html)


if __name__ == "__main__":
    unittest.main()
