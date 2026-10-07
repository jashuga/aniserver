"""Shared test setup: loads the site the way the server does, keeps every test away from the real Sonarr, Radarr,
Prowlarr, qBittorrent, Jellyfin, Kodi and AniList (they raise if a test forgets to swap in a fake), and swaps fakes in
everywhere a name is used (the code is split over the aniserver/ modules)."""
import importlib.util
import os
import sys
from pathlib import Path

os.environ["ANISERVER_NO_NETWORK"] = "1"
ROOT = Path(__file__).resolve().parent.parent          # website/
spec = importlib.util.spec_from_file_location("app", ROOT / "app.py")
app = importlib.util.module_from_spec(spec)
spec.loader.exec_module(app)


def modules():
    return [app] + [m for n, m in list(sys.modules.items()) if n.startswith("aniserver.")]


def everywhere(**values):
    """Set app.<name> and the same name in every aniserver module that has it; returns the value when there's one."""
    for k, v in values.items():
        for m in modules():
            if hasattr(m, k):
                setattr(m, k, v)
    return next(iter(values.values())) if len(values) == 1 else None


class Patch:
    """with Patch(sonarr=fake): every module that uses `sonarr` gets the fake until the block ends."""
    def __init__(self, **values):
        self.values = values

    def __enter__(self):
        self.saved = {k: getattr(app, k) for k in self.values}
        everywhere(**self.values)
        return self

    def __exit__(self, *exc):
        everywhere(**self.saved)
