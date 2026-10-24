import io
from pathlib import Path
import sqlite3
import stat
import tempfile
import unittest
from unittest.mock import patch
from urllib.error import HTTPError

import poll


NOW = 1792800000


def tag(name="DataScience"):
    return {"name": name, "history": [
        {"day": str(NOW-i*86400), "uses": str(7-i), "accounts": "2"}
        for i in range(7)
    ]}


def good_fetch(url, user_agent):
    if "/trends/" in url:
        return [tag()], 0
    return {"stats": {"status_count": 100}}, 0


class PollTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.database = Path(self.temp.name)/"data.sqlite3"
        self.output = Path(self.temp.name)/"index.html"

    def collect(self, fetch=good_fetch, now=NOW):
        return poll.collect(self.database, self.output, "test-agent", fetch, now)

    def count(self):
        with sqlite3.connect(self.database) as db:
            return db.execute("SELECT count(*) FROM samples").fetchone()[0]

    def test_two_runs_append_and_same_timestamp_deduplicates(self):
        self.collect()
        self.collect()
        self.assertEqual(self.count(), 3)
        result = self.collect(now=NOW+900)
        self.assertEqual(self.count(), 6)
        self.assertEqual(len(result["servers"][0]["samples"]), 2)
        self.assertNotIn("__DASHBOARD_DATA__", self.output.read_text())

    def test_public_html_is_readable_before_atomic_publish(self):
        replace = poll.os.replace
        published = []
        def checked_replace(source, destination):
            if Path(destination) == self.output:
                published.append(stat.S_IMODE(Path(source).stat().st_mode))
            return replace(source, destination)
        with patch("poll.os.replace", side_effect=checked_replace):
            self.collect()
        self.assertEqual(published, [0o644])
        self.assertEqual(stat.S_IMODE(self.output.stat().st_mode), 0o644)

    def test_default_atomic_writes_stay_private(self):
        private = Path(self.temp.name)/"private.json"
        poll.atomic_write(private, "{}")
        self.assertEqual(stat.S_IMODE(private.stat().st_mode), 0o600)
        private.chmod(0o644)
        poll.atomic_write(private, "{}")
        self.assertEqual(stat.S_IMODE(private.stat().st_mode), 0o600)

    def test_empty_trends_are_valid(self):
        def fetch(url, agent):
            return ([], 0) if "/trends/" in url else good_fetch(url, agent)
        result = self.collect(fetch)
        self.assertEqual(result["servers"][0]["tags"], [])
        self.assertIsNone(result["servers"][0]["trends_error"])

    def test_failed_server_does_not_hide_prior_samples_or_other_servers(self):
        self.collect()
        def fetch(url, agent):
            if "datasci.social" in url:
                raise poll.FetchError("HTTP 503")
            return good_fetch(url, agent)
        result = self.collect(fetch, NOW+900)
        first, second, _ = result["servers"]
        self.assertEqual(len(first["samples"]), 1)
        self.assertIn("503", first["activity_error"])
        self.assertEqual(len(second["samples"]), 2)
        self.assertTrue(self.output.exists())

    def test_rate_limit_persists_across_runs_and_skips_second_endpoint(self):
        calls = []
        def limited(url, agent):
            calls.append(url)
            if "datasci.social" in url:
                raise poll.FetchError("HTTP 429", NOW+3600)
            return good_fetch(url, agent)
        self.collect(limited)
        cooldown = self.database.with_name(self.database.name + ".cooldowns.json")
        self.assertEqual(stat.S_IMODE(cooldown.stat().st_mode), 0o600)
        self.assertEqual(sum("datasci.social" in url for url in calls), 1)
        calls.clear()
        self.collect(limited, NOW+900)
        self.assertFalse(any("datasci.social" in url for url in calls))
        calls.clear()
        self.collect(limited, NOW+3601)
        self.assertTrue(any("datasci.social" in url for url in calls))

    def test_exhausted_success_stores_sample_and_pauses_trends(self):
        def fetch(url, agent):
            payload, _ = good_fetch(url, agent)
            return payload, NOW+3600
        result = self.collect(fetch)
        self.assertEqual(self.count(), 3)
        self.assertIsNotNone(result["servers"][0]["trends_error"])

    def test_malformed_responses_are_errors_not_empty_trends(self):
        def fetch(url, agent):
            return {}, 0
        result = self.collect(fetch)
        self.assertEqual(self.count(), 0)
        for server in result["servers"]:
            self.assertIsNotNone(server["activity_error"])
            self.assertIsNotNone(server["trends_error"])
            self.assertIsNone(server["tags"])

    def test_counter_decrease_is_preserved_for_chart_to_exclude(self):
        self.collect()
        def fetch(url, agent):
            if "/trends/" in url:
                return [], 0
            return {"stats": {"status_count": 90}}, 0
        result = self.collect(fetch, NOW+900)
        self.assertEqual(result["servers"][0]["samples"], [(NOW,100), (NOW+900,90)])

    def test_old_samples_pruned(self):
        self.collect()
        result = self.collect(now=NOW+29*86400)
        self.assertEqual(self.count(), 3)
        self.assertEqual(len(result["servers"][0]["samples"]), 1)

    def test_script_injection_is_escaped(self):
        def fetch(url, agent):
            if "/trends/" in url:
                return [tag("</script><script>alert(1)</script>")], 0
            return good_fetch(url, agent)
        self.collect(fetch)
        html = self.output.read_text()
        self.assertNotIn("<script>alert(1)</script>", html)
        self.assertIn("\\u003c/script>", html)

    def test_trend_counts_parsed_and_sorted(self):
        parsed = poll.parse_trends([tag()])[0]
        self.assertEqual(len(parsed["history"]), 7)
        self.assertEqual(parsed["history"][-1]["uses"], 7)
        self.assertLess(parsed["history"][0]["day"], parsed["history"][-1]["day"])
        with self.assertRaises(ValueError):
            poll.parse_trends([{"name": "x", "history": []}])
        for bad in (True, -1, 1.1, None):
            with self.assertRaises((ValueError, TypeError)):
                poll.nonnegative_int(bad)

    def test_lock_prevents_overlapping_runs(self):
        with poll.exclusive_run(self.database):
            with self.assertRaises(RuntimeError):
                with poll.exclusive_run(self.database):
                    self.fail("Acquired an already-held lock")

    def test_retry_after_and_reset_use_later_deadline(self):
        headers = {"Retry-After": "3600", "X-RateLimit-Reset": "2026-10-24T00:05:00Z"}
        self.assertEqual(poll.retry_deadline(headers, NOW), NOW+3600)
        self.assertEqual(poll.retry_deadline({"Retry-After": "Sat, 24 Oct 2026 01:00:00 GMT"}, NOW), NOW+3600)
        self.assertEqual(poll.retry_deadline({"Retry-After": "invalid"}, NOW, NOW+900), NOW+900)

    def test_normal_rate_limit_headers_do_not_trigger_cooldown(self):
        class Response(io.BytesIO):
            headers = {"X-RateLimit-Remaining": "299", "X-RateLimit-Reset": "2026-10-24T00:05:00Z"}
        with patch("poll.urlopen", return_value=Response(b'{"ok": true}')):
            payload, deadline = poll.fetch_json("https://example.test/api", "test-agent")
        self.assertEqual(payload, {"ok": True})
        self.assertEqual(deadline, 0)

    def test_html_http_error_does_not_get_parsed_as_json(self):
        error = HTTPError("https://example.test", 422, "Unprocessable", {}, io.BytesIO(b"<html>no</html>"))
        with patch("poll.urlopen", side_effect=error):
            with self.assertRaisesRegex(poll.FetchError, "HTTP 422"):
                poll.fetch_json("https://example.test", "test-agent")


if __name__ == "__main__":
    unittest.main()
