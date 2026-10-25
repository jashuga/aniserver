"""The first-time setup wizard (python3 app.py setup), with fake programs and scripted answers."""
import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path

from helpers import Patch, app
from aniserver import wizard


class Wizard(unittest.TestCase):
    def test_fresh_install_writes_the_settings_and_creates_the_owners_login(self):
        answers = {"Your user name": "alice", "Your display name": "Alice", "Other people": "bob, carol",
                   "Public address of the website": "", "Password for alice": "a long enough password", "Repeat it": "a long enough password",
                   "6-digit code": "123456"}

        def ask(question, default="", secret=False):
            return next((v for k, v in answers.items() if question.startswith(k)), default)

        def http(url, data=None, headers=None, method=None, timeout=8):
            if url.endswith("/rootfolder"):
                return [{"path": "/media/anime"}] if ":8989" in url else [{"path": "/media/movies"}]
            if url.endswith("/api/v1/indexer"):
                return [{"id": 3, "name": "Nyaa.si"}, {"id": 5, "name": "Nyaa.si (Manga)"}, {"id": 4, "name": "Other"}]
            return "v5.0"
        with tempfile.TemporaryDirectory() as d:
            conf_file, users_file = Path(d) / "config.json", Path(d) / "remote-users.json"
            saved = (wizard.CONFIG_FILE, wizard.ask, wizard.yes, wizard.http, wizard.reachable, wizard.api_key_from_disk, wizard.jellyfin_key)
            wizard.CONFIG_FILE, wizard.ask, wizard.http = conf_file, ask, http
            wizard.yes = lambda question, default=True: False                       # no Kodi
            wizard.reachable = lambda url: not url.endswith("/jsonrpc")
            wizard.api_key_from_disk = lambda app_name: f"key-{app_name.lower()}"
            wizard.jellyfin_key = lambda url: "jf-key"
            try:
                with Patch(REMOTE_USERS_FILE=users_file, totp_ok=lambda secret, code, last=0: 42), contextlib.redirect_stdout(io.StringIO()):
                    self.assertEqual(wizard.main(), 0)
            finally:
                (wizard.CONFIG_FILE, wizard.ask, wizard.yes, wizard.http, wizard.reachable, wizard.api_key_from_disk, wizard.jellyfin_key) = saved
            conf = json.loads(conf_file.read_text())
            self.assertEqual(oct(conf_file.stat().st_mode & 0o777), "0o600")
            self.assertEqual((conf["sonarr_url"], conf["sonarr_key"], conf["radarr_key"], conf["jellyfin_key"]),
                             ("http://localhost:8989", "key-sonarr", "key-radarr", "jf-key"))
            self.assertEqual((conf["anime_folders"], conf["movie_folders"]), (["/media/anime"], ["/media/movies"]))
            self.assertEqual((conf["manga"], conf["indexer_id"], conf["kodi_url"]), (True, 5, ""))
            self.assertEqual(list(conf["profiles"]), ["alice", "bob", "carol"])
            self.assertEqual(conf["tv_profile"], "alice")
            self.assertEqual(len(conf["secret"]), 64)
            users = json.loads(users_file.read_text())
            self.assertEqual(list(users), ["alice"])
            self.assertTrue(app.check_password("a long enough password", users["alice"]["pw"]))


class OptionalParts(unittest.TestCase):
    def test_no_kodi_means_no_tv_buttons(self):
        self.assertIn("data-tv=", app.tv_button("x" * 32))
        with Patch(TV=False):
            self.assertEqual(app.tv_button("x" * 32), "")
            with Patch(qbit_live=lambda: [], _download_names=lambda: ({}, {}, {})):
                app._SWR.pop("dlnames", None)
                self.assertIs(app.live_downloads()["tv"], False)          # the Downloads page hides "Watch now on TV"
                app._SWR.pop("dlnames", None)


if __name__ == "__main__":
    unittest.main()
