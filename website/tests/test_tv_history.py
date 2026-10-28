"""The TV and the website keep separate watch histories: the TV resumes from its own spot (mpv's watch-later file),
and only Continue on TV (an explicit position) hands the website's spot over."""
import tempfile
import unittest
from pathlib import Path

from helpers import Patch, app


class TvHistory(unittest.TestCase):
    def play(self, pos, tv_spot=None):
        played = []
        with tempfile.TemporaryDirectory() as home:
            item = {"Path": "/media/Show/Show - 05.mkv", "Type": "Episode", "SeriesName": "Show", "ParentIndexNumber": 1, "IndexNumber": 5,
                    "UserData": {"PlaybackPositionTicks": 600 * 10**7}}            # the website is at 10:00
            with Patch(jf_item=lambda uid, iid: item, jf_uid=lambda user: "u1", media_path=lambda p: p,
                       kodi_play=lambda path: played.append(path)):
                old_home = app.Path.home
                app.Path.home = staticmethod(lambda: Path(home))
                try:
                    if tv_spot:
                        app.mpv_resume_at(item["Path"], tv_spot)
                    name, at = app.tv_play_item("alice", "ep5", pos)
                    after = app.tv_resume_point(item["Path"])
                finally:
                    app.Path.home = old_home
        return name, at, after, played

    def test_play_on_tv_resumes_where_the_tv_stopped_not_where_the_website_did(self):
        name, at, after, played = self.play(None, tv_spot=125)
        self.assertEqual((at, after), (125, 125))                                    # the website's 10:00 is ignored
        self.assertEqual(played, ["/media/Show/Show - 05.mkv"])

    def test_play_on_tv_starts_from_the_beginning_when_the_tv_never_played_it(self):
        self.assertEqual(self.play(None)[1:3], (0, 0))

    def test_continue_on_tv_hands_over_the_websites_spot(self):
        self.assertEqual(self.play(842.5, tv_spot=125)[1:3], (842.5, 842.5))


if __name__ == "__main__":
    unittest.main()
