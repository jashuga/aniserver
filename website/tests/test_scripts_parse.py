"""Every JavaScript block the site sends must parse (a syntax error silently kills every button on the page).
Needs node; run: python3 -m unittest discover -s ~/.local/opt/manga-request/tests -v"""
import importlib.util
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

spec = importlib.util.spec_from_file_location("app", Path(__file__).resolve().parent.parent / "app.py")
app = importlib.util.module_from_spec(spec)
spec.loader.exec_module(app)


@unittest.skipUnless(shutil.which("node"), "node is not installed")
class ScriptsParse(unittest.TestCase):
    def test_all_page_scripts(self):
        for name in ("SCRIPT", "HOME_JS", "PLAYER_JS", "DOWNLOADS_JS"):
            with self.subTest(script=name), tempfile.NamedTemporaryFile("w", suffix=".js") as f:
                f.write(getattr(app, name))
                f.flush()
                r = subprocess.run(["node", "--check", f.name], capture_output=True, text=True)
                self.assertEqual(r.returncode, 0, f"{name} doesn't parse:\n{r.stderr}")


if __name__ == "__main__":
    unittest.main()


class Assets(unittest.TestCase):
    def test_pages_reference_cached_files_with_content_hashes(self):
        for name, text in (("site.css", app.STYLE + app.STREAM_STYLE), ("site.js", app.SCRIPT + ";\n" + app.HOME_JS),
                           ("player.css", app.PLAYER_CSS), ("player.js", app.PLAYER_JS)):
            url = app.asset(name)
            data, zipped, ctype = app.ASSET_FILES[url]
            self.assertEqual(data.decode(), text)
            self.assertRegex(url, r"^/assets/[a-z]+\.[0-9a-f]{12}\.(css|js)$")
        html = app.page("<p>hi</p>", "home", "localhost", "t")
        self.assertIn(app.asset("site.css"), html)
        self.assertIn(app.asset("site.js"), html)
        self.assertNotIn("<style>", html)
