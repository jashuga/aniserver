"""Every JavaScript file the site sends must parse (a syntax error silently kills every button on the page), and pages
must load the CSS/JS from assets/ as long-cached files. Needs node; run: python3 -m unittest discover -s tests -v"""
import shutil
import subprocess
import unittest

from helpers import ROOT, app
ASSETS = sorted((ROOT / "assets").glob("*.*"))


@unittest.skipUnless(shutil.which("node"), "node is not installed")
class ScriptsParse(unittest.TestCase):
    def test_all_page_scripts(self):
        scripts = [f for f in ASSETS if f.suffix == ".js"]
        self.assertTrue(scripts)
        for f in scripts:
            with self.subTest(script=f.name):
                r = subprocess.run(["node", "--check", str(f)], capture_output=True, text=True)
                self.assertEqual(r.returncode, 0, f"{f.name} doesn't parse:\n{r.stderr}")


class Assets(unittest.TestCase):
    def test_pages_reference_cached_files_with_content_hashes(self):
        for f in ASSETS:
            url = app.asset(f.name)
            data, zipped, ctype = app.ASSET_FILES[url]
            self.assertEqual(data, f.read_bytes())
            self.assertRegex(url, r"^/assets/[a-z]+\.[0-9a-f]{12}\.(css|js)$")
        html = app.page("<p>hi</p>", "home", "localhost", "t")
        self.assertIn(app.asset("site.css"), html)
        self.assertIn(app.asset("site.js"), html)
        self.assertNotIn("<style>", html)


if __name__ == "__main__":
    unittest.main()
