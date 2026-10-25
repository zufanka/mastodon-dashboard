# mastodon-stats

Read-only dashboard for mastodon. One weekday-by-hour activity heatmap with a server selector (local time, normalized to each server's overall observed average on a fixed 0–2× color scale) and one merged trending-hashtag list. One Python script, one SQLite table, one generated HTML page. Python 3.10+; no third-party runtime dependencies, tokens, JavaScript packages, or CDN requests.

## Run

```sh
uv run python poll.py                                                # poll once, write out/index.html
uv run python -m http.server 8000 --bind 127.0.0.1 --directory out   # optional local server
```

Heatmap cells show posting rates per observed hour, not totals. Hatching means missing data; dotted borders mark tentative cells with less than three observed hours. Hover, tap, or focus a cell for rates and coverage; arrow keys navigate the heatmap.

`out/index.html` also works straight from disk, offline. Trends appear on the first run; activity needs repeated polls (28-day retention). Only publish `out/`. Options: `--database`, `--output`, `--contact` (contact URL for the User-Agent).