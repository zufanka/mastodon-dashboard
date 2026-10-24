# mastodon-stats

A small, read-only dashboard for **datasci.social**, **journa.host**, and **mastodon.social**.

- **Activity:** one shared hourly chart, normalized to each server's observed average, in your browser's timezone.
- **Hashtags:** one deduplicated list across the three servers, with badges and expandable seven-day usage details.

One Python script, one SQLite table, one generated HTML page. Python 3.10+ on Linux/macOS; no third-party runtime dependencies, tokens, JavaScript packages, or CDN requests.

## Run

From this directory:

```sh
uv run python poll.py
```

Open **`out/index.html`** in your browser. It works directly from disk, offline. Or serve it locally:

```sh
uv run python -m http.server 8000 --bind 127.0.0.1 --directory out
```

Then visit <http://127.0.0.1:8000>. Refresh after a new poll to see updated data. Only publish `out/`, not the project/data directory.

Trends work on the first run. Activity needs at least two observations; a day begins to fill the hourly chart, and small servers can need weeks of data. The script reads and retains the most recent **28 days** of samples.

## Deploy to Dokploy

**One Application, one named volume, one schedule. No separate database service.** The image contains Python and nginx; nginx stays running while Dokploy executes the collector periodically. No Compose, internal cron, uv, or Node is needed in the container.

1. Push this repository to GitHub. In Dokploy's **Git** settings, connect your GitHub account and authorize the Dokploy GitHub App for this repository. Create an **Application**, choose **GitHub** as the source, select the repository, and set branch **`main`**. See [Dokploy's GitHub setup](https://docs.dokploy.com/docs/core/github).
2. Select **Dockerfile** build: path `Dockerfile`, context `.`, build stage blank. Dokploy builds directly from the GitHub source—no ZIP, image registry, or GitHub Pages setup needed. `.gitignore` keeps local data and planning notes out of Git; `.dockerignore` limits the image build to the application files.
3. **Advanced → Mounts:** add a **Volume Mount**, name `mastodon-stats-data`, mount path `/data`. This holds SQLite, lock/cooldown state, and the public HTML. The database and table are created automatically on the first poll.
4. Keep **one replica on one node**. A local Docker volume does not follow the app to another server. Do not scale this SQLite deployment across nodes.
5. Add your domain: path `/`, **container port 80**, HTTPS enabled with Let's Encrypt. Point DNS at the server. Leave host port publishing empty—Dokploy's Traefik handles routing.
6. Deploy. A fresh volume returns **503 “Waiting for first collection”** until the first poll. Redeployments retain the previous page. Leave HTTP health checks on `/` disabled so the initial waiting state does not trigger restarts.
7. Add a schedule targeting this Application:

   | Setting | Value |
   |---|---|
   | Name | `poll-mastodon` |
   | Type | Application |
   | Cron | `*/15 * * * *` |
   | Timezone | `UTC` |
   | Shell | `sh` |
   | Enabled | Yes |

   Command (one line):

   ```sh
   /usr/local/bin/python /app/poll.py --database /data/stats.sqlite3 --output /data/public/index.html --contact https://datafrosch.fun
   ```

   Use your own contact URL for a copy of this app. The image already has Python and all required modules; the container command deliberately does not use uv.

8. Click the schedule's **Run** action once, check its logs, and open the domain. Verify the next scheduled run adds samples. Dokploy captures stdout/stderr; do not redirect job output into an unbounded log file.

nginx serves only `/` and `/index.html` from `/data/public`, not the database, lock, cooldown state, or application source. Generated HTML is readable by nginx; cooldown JSON remains private. The page is served with `Cache-Control: no-store` so a refresh sees the latest snapshot.

For backups, use **Dokploy Volume Backups** on the named volume, with the stop-container option for SQLite consistency. Back up before moving the app to another node. Never bake an existing database into the image.

### Try the container locally

```sh
docker build -t mastodon-stats .
docker run -d --name mastodon-stats -p 127.0.0.1:8080:80 \
  -v mastodon-stats-data:/data mastodon-stats
docker exec mastodon-stats /usr/local/bin/python /app/poll.py \
  --database /data/stats.sqlite3 --output /data/public/index.html
curl --fail http://127.0.0.1:8080/
```

Open <http://127.0.0.1:8080>. Remove the container with `docker rm -f mastodon-stats`; the named volume remains. Replace `docker` with `podman` if preferred.

Subsequent deployments use the same repository and persistent volume. Enable Dokploy auto-deploy for `main` if you want pushes to trigger a rebuild. The GitHub Actions workflow checks tests and the image build; it does not deploy, publish images, or need repository secrets.

Dokploy references: [GitHub source](https://docs.dokploy.com/docs/core/github), [scheduled jobs](https://docs.dokploy.com/docs/core/schedule-jobs), [mounts and replicas](https://docs.dokploy.com/docs/core/applications/advanced), [volume backups](https://docs.dokploy.com/docs/core/volume-backups).

## Local scheduling (alternative to Dokploy)

Scheduling is **not installed automatically**. Run the command once first (it creates `data/`), then add this with `crontab -e`:

```cron
*/15 * * * * cd /path/to/mastodon-stats && /absolute/path/to/uv run python poll.py >> data/poll.log 2>&1
```

Adjust both paths on another machine. Collection runs only while that machine is awake. The lock file prevents overlapping runs. Check `data/poll.log` occasionally; stop collection by removing the cron entry.

Options:

```sh
uv run python poll.py --database data/stats.sqlite3 --output out/index.html \
  --contact https://datafrosch.fun
```

Use your own contact URL when running a copy. Paths are relative to your working directory. The HTML template lives next to `poll.py`.

## What the numbers mean

- Activity estimates **local posting volume, including boosts**, not readers online, followers' activity, or likely engagement. There is no automatic “best time” prediction.
- Hourly rates are weighted by observed time. Counter decreases and polling gaps longer than 30 minutes are excluded; missing hours stay blank and lines never bridge them. A counter decrease resets the baseline for the next interval. Server-side caching and deletions make this an approximation.
- The shared activity axis is relative: **1× = that server's observed average**. Points show actual posts/hour and observed coverage on hover/focus; “Coverage & source details” shows each server's average and sample history. A server with no observed posts cannot be normalized, so it has no curve. The chart stays marked “Collecting data” until every server has at least 24 observed hours covering all local hours.
- Tags are merged case-insensitively, ranked by how many servers list them, then by average API position—not weekly volume. The first ten are shown initially; “Show all” reveals the rest. A missing badge only means the tag wasn't in that server's fetched top ten. Failed sources are excluded and identified explicitly.
- Trends reflect each server's view of local and federated activity. The API orders them by accelerating usage, **not** by weekly totals. Expand a tag for each server's seven-day sparkline and usage total. We never sum across servers: their federated views overlap. Daily buckets are UTC and today is incomplete; each sparkline has its own scale.
- This is **currently trending tags with a week of usage**, not an archive of all tags that trended last week. An empty list is valid.

## Requests and failures

Two public requests per server per run, sequential, with a contact User-Agent and 20-second timeouts. No posts or profiles are fetched or stored. Errors are printed to stderr and marked on the page; old activity observations remain visible, but failed trends are not shown as fresh data. A successful process exit means the page was generated, not that every upstream request succeeded; check source warnings in the page/job logs.

Rate-limit deadlines persist in a small `data/stats.sqlite3.cooldowns.json` file so later cron runs also respect them. No immediate retries or authentication workarounds. `/api/v1/instance` is deprecated; notices are logged, and a future removal would require changing the collector. Respect each server's terms and administrator requests.

## Tests

```sh
PYTHONDONTWRITEBYTECODE=1 uv run python -m unittest -v
node --test test_dashboard.js
```

Node is only needed for the frontend-calculation tests, not to run the app. Tests cover parsing, empty/error states, sample and hashtag deduplication, cooldown persistence, retention, safe HTML embedding, weighted rates, normalization, disconnected chart segments, merged ranking, timezone offsets, and DST.

GitHub Actions runs these tests and validates the Docker image on pushes and pull requests. Tests use mocked API responses and do not contact Mastodon.
