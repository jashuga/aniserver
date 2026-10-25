# Changelog

## v1.0.0 — 2026-10-25

First public release.

- **Website**: Netflix-style home, show pages, My Shows, live Downloads, AniList browse/add, manga search and follows,
  per-person accounts (password + TOTP once per browser) with their own watch history, password changes, deleting titles.
- **Player**: direct play / remux / adaptive GPU ladder / burned-in fallback, chosen per connection and remembered per
  network; JASSUB subtitles with the release's fonts; skip opening, next episode, resume, Continue on TV.
- **Adding anime**: AniList → Fribb → TVDB, seasons matched by air date (split cours, continuing seasons, Netflix-first
  dates, OVAs), never leaves a show with nothing monitored.
- **TV**: Kodi + mpv; watch while downloading; Kodi's library kept in step automatically.
- **Anime artwork** in Japanese or English (posters and title logos), set on the Account page.
- **Safety net** (`htpc-watchdog`): service restarts, re-searches, stuck/misread imports, Kodi sync, phone alerts.
- 52 tests that run without any of the real services.
