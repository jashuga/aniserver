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
    def test_fresh_install_connects_the_programs_writes_the_settings_and_creates_the_login(self):
        answers = {"Your user name": "alice", "Your display name": "Alice", "Other people": "bob, carol",
                   "Public address of the website": "", "Password for alice": "a long enough password",
                   "Repeat it": "a long enough password", "6-digit code": "123456"}
        calls = []

        def ask(question, default="", secret=False):
            return next((v for k, v in answers.items() if question.startswith(k)), default)
        fakes = dict(
            ask=ask, yes=lambda question, default=True: False,                                      # no Kodi
            reachable=lambda url: not url.endswith("/jsonrpc"), arr_settings=lambda app_name: (None, f"key-{app_name.lower()}"),
            http=lambda url, data=None, headers=None, method=None, timeout=20: "v5.0",            # qBittorrent answers
            ensure_root_folder=lambda url, key, ver, folder: [str(folder)],
            ensure_download_client=lambda *a: calls.append(("download client", a[4])) or True,
            ensure_prowlarr=lambda url, key, apps: calls.append(("prowlarr", [a[0] for a in apps])) or ["Sonarr", "Radarr", "Nyaa.si"],
            nyaa_indexer=lambda url, key: 5,
            jellyfin_first_run=lambda url, user, pw: calls.append(("jellyfin admin", user, pw)) or True,
            jellyfin_token=lambda url, user, pw: "token", jellyfin_api_key=lambda url, token: "jf-key",
            ensure_libraries=lambda url, token, anime, movies: calls.append(("libraries", anime, movies)) or ["Anime", "Movies"])
        with tempfile.TemporaryDirectory() as d:
            conf_file, users_file = Path(d) / "config.json", Path(d) / "remote-users.json"
            saved = {k: getattr(wizard, k) for k in [*fakes, "CONFIG_FILE", "MEDIA"]}
            for k, v in {**fakes, "CONFIG_FILE": conf_file, "MEDIA": Path("/media")}.items():
                setattr(wizard, k, v)
            try:
                with Patch(REMOTE_USERS_FILE=users_file, totp_ok=lambda secret, code, last=0: 42), contextlib.redirect_stdout(io.StringIO()):
                    self.assertEqual(wizard.main(), 0)
            finally:
                for k, v in saved.items():
                    setattr(wizard, k, v)
            self.assertIn(("download client", "sonarr"), calls)
            self.assertIn(("prowlarr", ["Sonarr", "Radarr"]), calls)
            self.assertIn(("jellyfin admin", "alice", "a long enough password"), calls)      # one password for both
            self.assertIn(("libraries", ["/media/Anime"], ["/media/Movies"]), calls)
            conf = json.loads(conf_file.read_text())
            self.assertEqual(oct(conf_file.stat().st_mode & 0o777), "0o600")
            self.assertEqual((conf["sonarr_url"], conf["sonarr_key"], conf["radarr_key"], conf["jellyfin_key"]),
                             ("http://localhost:8989", "key-sonarr", "key-radarr", "jf-key"))
            self.assertEqual((conf["anime_folders"], conf["movie_folders"]), (["/media/Anime"], ["/media/Movies"]))
            self.assertEqual((conf["manga"], conf["indexer_id"], conf["kodi_url"]), (True, 5, ""))
            self.assertEqual(list(conf["profiles"]), ["alice", "bob", "carol"])
            self.assertEqual(len(conf["secret"]), 64)
            users = json.loads(users_file.read_text())
            self.assertEqual(list(users), ["alice"])
            self.assertTrue(app.check_password("a long enough password", users["alice"]["pw"]))

    def test_items_are_built_from_the_programs_own_schema(self):
        schema = [{"implementation": "Transmission", "fields": []},
                  {"implementation": "QBittorrent", "fields": [{"name": "host", "value": "localhost"}, {"name": "port", "value": 8080},
                                                              {"name": "tvCategory", "value": "tv-sonarr"}, {"name": "useSsl", "value": False}]}]
        item = wizard.from_schema(schema, "QBittorrent", {"port": 8090, "tvCategory": "sonarr", "movieCategory": "radarr"}, name="qBittorrent")
        self.assertEqual({f["name"]: f["value"] for f in item["fields"]}, {"host": "localhost", "port": 8090, "tvCategory": "sonarr", "useSsl": False})
        self.assertEqual(item["name"], "qBittorrent")


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
