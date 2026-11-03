import datetime as dt
import sys
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import ranking

START = dt.datetime(2026, 1, 1, tzinfo=dt.timezone.utc)


def article(i, quality=90):
    return {"key": str(i), "content": quality, "metadata": quality,
            "cleanliness": quality, "fingerprint": str(i),
            "published_at": (START - dt.timedelta(days=i * 30)).isoformat()}


def result(url, date, quality=90, count=20, source="https://a.example/list", accepted=True):
    return {"accepted": accepted, "checked_at": date.isoformat(), "http_status": 200 if accepted else 503,
            "resolved_url": url, "candidate": {
                "feed_url": url, "categories": ["development"],
                "sources": [{"url": source}]},
            "title": "Example", "website_url": url, "description": "Great RSS", "language_tag": "en",
            "entry_evidence": [article(i, quality) for i in range(count)] if accepted else []}


def rank(results, state=None, at=START, positions=None):
    sources = [{"url": "https://a.example/list", "directory_url": "https://github.com/org-a/source"},
               {"url": "https://b.example/list", "directory_url": "https://github.com/org-a/source"},
               {"url": "https://c.example/list", "directory_url": "https://github.com/org-c/other"}]
    return ranking.score_catalog(results, sources, {}, state, positions, at=at)


class EvidenceTests(unittest.TestCase):
    def test_rss_full_article_beats_headline_only(self):
        rich = ET.fromstring('<item xmlns:content="http://purl.org/rss/1.0/modules/content/">'
                           '<title>Rich</title><link>https://example.com/a</link><guid>abc</guid>'
                           '<content:encoded><![CDATA[<p>' + 'Substantial content. ' * 80 + '</p>]]></content:encoded></item>')
        thin = ET.fromstring('<item><title>Thin</title><link>https://example.com/b</link>'
                           '<description>Read more</description></item>')
        samples = ranking.entry_evidence([rich, thin], "https://example.com/rss")
        self.assertGreater(samples[0]["content"], 90)
        self.assertLess(samples[1]["content"], 40)

    def test_atom_xhtml_content_and_date(self):
        atom = ET.fromstring('''<entry xmlns="http://www.w3.org/2005/Atom">
          <title>Hello</title><id>tag:x</id><link href="https://example.com/a"/>
          <updated>2026-01-01T00:00:00Z</updated>
          <content type="xhtml"><div xmlns="http://www.w3.org/1999/xhtml"><p>Visible body</p></div></content>
        </entry>''')
        sample = ranking.entry_evidence([atom], "https://example.com/atom")[0]
        self.assertIsNotNone(sample["published_at"])
        self.assertGreater(sample["content"], 10)

    def test_popularity_collapses_same_project(self):
        sources = {"https://a.example/list": "github:org-a/source", "https://b.example/list": "github:org-a/source"}
        candidate = {"sources": [{"url": "https://a.example/list"}, {"url": "https://b.example/list"}]}
        self.assertEqual(len(ranking.sources_for(candidate, sources)), 1)
        self.assertLess(ranking.popularity({"x"}, False), ranking.popularity({"x", "y"}, False))


class HistoricalRankingTests(unittest.TestCase):
    def test_repeated_scan_does_not_add_evidence(self):
        first, state = rank([result("https://example.com/rss", START)], at=START)
        again, state = rank([result("https://example.com/rss", START)], state, at=START)
        self.assertEqual(first, again)
        self.assertEqual(len(state["feeds"]["https://example.com/rss"]["checks"]), 1)
        self.assertEqual(len(state["feeds"]["https://example.com/rss"]["articles"]), 20)

    def test_bad_fortnight_does_not_erase_long_term_quality(self):
        url = "https://example.com/rss"
        first, state = rank([result(url, START, quality=95)], at=START)
        date = START + dt.timedelta(days=14)
        poor = result(url, date, quality=10)
        poor["entry_evidence"] = [article(100 + i, 10) for i in range(20)]
        next_report, state = rank([poor], state, at=date)
        self.assertLess(first["feeds"][url]["quality"] - next_report["feeds"][url]["quality"], 4)
        self.assertEqual(first["feeds"][url]["score"], next_report["feeds"][url]["score"])

    def test_unobserved_days_are_not_failed_checks(self):
        url = "https://example.com/rss"
        _, state = rank([result(url, START)], at=START)
        later = START + dt.timedelta(days=75)
        _, state = rank([result(url, START)], state, at=later)
        self.assertEqual(len(state["feeds"][url]["checks"]), 1)
        self.assertEqual(ranking.reliability(state["feeds"][url]["checks"], later), 100 * 10 / 11)

    def test_single_transient_failure_is_downweighted(self):
        url = "https://example.com/rss"
        _, state = rank([result(url, START)], at=START)
        fail = result(url, START + dt.timedelta(days=14), accepted=False)
        fail["error"] = "connection timed out"
        _, state = rank([fail], state, at=START + dt.timedelta(days=14))
        self.assertEqual(state["feeds"][url]["checks"][-1]["weight"], 0.25)
        self.assertEqual(len(state["feeds"][url]["articles"]), 20)

    def test_inactive_monthly_feed_not_penalized(self):
        entries = {str(i): article(i) for i in range(5)}
        score = ranking.activity(entries, START + dt.timedelta(days=45))
        self.assertEqual(score, 90)

    def test_one_feed_does_not_dominate_category(self):
        strong = [result("https://example.com/great", START, quality=98)]
        broad = [result(f"https://host{i}.example/rss", START, quality=90) for i in range(20)]
        for x in broad:
            x["candidate"]["categories"] = ["broad"]
        one, _ = rank(strong + broad)
        self.assertGreater(one["categories"]["broad"]["score"], one["categories"]["development"]["score"])

    def test_stable_score_requires_two_distinct_scans_for_minor_change(self):
        record = {}
        self.assertEqual(ranking.stable_published_score(70, record, "one"), 70)
        self.assertEqual(ranking.stable_published_score(72, record, "two"), 70)
        self.assertEqual(ranking.stable_published_score(72, record, "two"), 70)
        self.assertEqual(ranking.stable_published_score(72, record, "three"), 72)


if __name__ == "__main__":
    unittest.main()
