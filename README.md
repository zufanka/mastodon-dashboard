# mastodon-stats

Read-only dashboard for mastodon. One shared hourly activity chart (normalized to each server's observed average) and one merged trending-hashtag list. One Python script, one SQLite table, one generated HTML page. Python 3.10+; no third-party runtime dependencies, tokens, JavaScript packages, or CDN requests.

## Run

```sh
uv run python poll.py                                                # poll once, write out/index.html
uv run python -m http.server 8000 --bind 127.0.0.1 --directory out   # optional local server
```

`out/index.html` also works straight from disk, offline. Trends appear on the first run; activity needs repeated polls (28-day retention). Only publish `out/`. Options: `--database`, `--output`, `--contact` (contact URL for the User-Agent).