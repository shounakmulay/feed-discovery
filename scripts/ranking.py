"""Deterministic offline feed/category ranking.

Observations come from validation fetches. Local state is not published. Replaying
the same validation scan does not add observations or repeatedly reweight articles.
"""

import datetime as dt
import hashlib
import html
import math
import re
import statistics
import urllib.parse
from collections import defaultdict
from html.parser import HTMLParser

UTC = dt.timezone.utc
HALF_LIFE_DAYS = 90
MAX_SCAN_INFLUENCE = 0.20
HISTORY_DAYS = 180
MAX_ARTICLES = 100
TOP_FEEDS = 20
CHANGE_THRESHOLD = 3.0


def timestamp(value):
    if not value:
        return None
    try:
        parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
        return parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed.astimezone(UTC)
    except (ValueError, TypeError):
        return None


def clamp(number, low=0.0, high=100.0):
    return max(low, min(high, float(number)))


def local_name(element):
    return element.tag.rsplit("}", 1)[-1]


def field(element, names):
    for child in element:
        if local_name(child) in names:
            return child
    return None


def field_text(element, names):
    child = field(element, names)
    return "".join(child.itertext()).strip() if child is not None else ""


class TextExtractor(HTMLParser):
    """Extract readable text while detecting likely boilerplate/pollution."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.fragments = []
        self.skip = 0
        self.suspicious = 0
        self.links = 0

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag in ("script", "style", "nav", "footer", "form"):
            self.skip += 1
            self.suspicious += 1
        if tag == "a":
            self.links += 1
        if tag == "img" and (attrs.get("width") == "1" or attrs.get("height") == "1"):
            self.suspicious += 1
        if any(name.startswith("on") for name in attrs):
            self.suspicious += 1

    def handle_endtag(self, tag):
        if tag in ("script", "style", "nav", "footer", "form"):
            self.skip = max(0, self.skip - 1)

    def handle_data(self, value):
        if not self.skip:
            self.fragments.append(value)


def readable(markup):
    extractor = TextExtractor()
    try:
        extractor.feed(html.unescape(markup))
        extractor.close()
        text = " ".join(extractor.fragments)
    except (ValueError, AssertionError):
        text = re.sub(r"<[^>]+>", " ", html.unescape(markup))
    text = re.sub(r"\s+", " ", text).strip()
    words = len(text.split())
    link_penalty = 30 if extractor.links >= 8 and words < extractor.links * 12 else 0
    cleanliness = clamp(100 - 12 * extractor.suspicious - link_penalty)
    return text, cleanliness


def entry_evidence(entries, base_url):
    """Sample up to 20 articles, distinguishing full content and summaries.

    Content extraction is heuristic, not an HTML sanitizer. Stable entry keys
    let future scans avoid counting the same post several times.
    """
    result = []
    seen = set()
    for entry in entries[:20]:
        title = field_text(entry, {"title"})
        link = field_text(entry, {"link"})
        if not link:
            for child in entry:
                if local_name(child) == "link" and child.get("rel", "alternate") == "alternate":
                    link = child.get("href") or ""
                    break
        identifier = field_text(entry, {"guid", "id"}) or link or title
        key = hashlib.sha256(identifier.encode("utf-8")).hexdigest()[:24]
        if key in seen:
            continue
        seen.add(key)
        published = field_text(entry, {"pubDate", "published", "updated", "date"})
        published_at = timestamp(published)
        if published and published_at is None:
            try:
                import email.utils
                published_at = email.utils.parsedate_to_datetime(published)
                if published_at.tzinfo is None:
                    published_at = published_at.replace(tzinfo=UTC)
                published_at = published_at.astimezone(UTC)
            except (TypeError, ValueError, IndexError, OverflowError):
                published_at = None
        content_element = field(entry, {"encoded", "content"})
        summary_element = field(entry, {"summary", "description"})
        chosen = content_element if content_element is not None else summary_element
        if chosen is None:
            markup = ""
        elif list(chosen):
            markup = " ".join("".join(item.itertext()) for item in chosen)
        else:
            markup = chosen.text or ""
        plain, clean = readable(markup)
        length = len(plain)
        if length == 0:
            content = 10.0
        else:
            content = clamp(25 + 75 * min(1.0, length / 750))
            if content_element is None:
                content = min(content, 80.0)
        metadata = (
            (25 if title else 0) + (25 if link else 0)
            + (25 if published_at else 0)
            + (15 if field_text(entry, {"guid", "id"}) else 0)
            + (10 if field_text(entry, {"author", "creator"}) else 0)
        )
        result.append({
            "key": key, "content": round(content, 2),
            "metadata": metadata, "cleanliness": round(clean, 2),
            "published_at": published_at.isoformat().replace("+00:00", "Z") if published_at else None,
            "fingerprint": hashlib.sha256((title.casefold() + "\n" + plain[:2000]).encode("utf-8")).hexdigest()[:24],
        })
    return result


def sources_for(candidate, directory_map):
    groups = set()
    for source in candidate.get("sources", []):
        origin = directory_map.get(source.get("url", ""))
        if origin:
            groups.add(origin)
    return groups


def canonical_source(url):
    """Collapse several directory lists belonging to the same project."""
    parsed = urllib.parse.urlsplit(url)
    pieces = parsed.path.strip("/").split("/")
    if parsed.hostname in ("raw.githubusercontent.com", "github.com") and len(pieces) >= 2:
        return "github:" + pieces[0].lower() + "/" + pieces[1].lower()
    return (parsed.hostname or "").lower() + parsed.path.rstrip("/")


def publisher_domain(feed):
    host = (urllib.parse.urlsplit(feed.get("website_url") or feed.get("feed_url") or "").hostname or "").lower()
    return host[4:] if host.startswith("www.") else host


def featured(feed, category, featured_domains):
    host = publisher_domain(feed)
    return any(host == domain or host.endswith("." + domain)
               for domain in featured_domains.get(category, ()))


def popularity(independent_sources, is_featured):
    # Cold-start prior, never represented as actual subscriber popularity.
    directory = 1 - math.exp(-0.65 * len(independent_sources))
    return clamp(45 + 35 * directory + (10 if is_featured else 0))


def reliability(checks, at):
    cutoff = at - dt.timedelta(days=HISTORY_DAYS)
    observations = [c for c in checks if timestamp(c["at"]) and timestamp(c["at"]) >= cutoff]
    successes = sum(c["weight"] for c in observations if c["ok"])
    failures = sum(c["weight"] for c in observations if not c["ok"])
    return 100 * (9 + successes) / (10 + successes + failures)


def activity(entries, at):
    dates = sorted({timestamp(e.get("published_at")) for e in entries.values()
                    if timestamp(e.get("published_at")) and timestamp(e.get("published_at")) <= at})
    if len(dates) < 5:
        return 85.0
    gaps = [(b - a).total_seconds() / 86400 for a, b in zip(dates, dates[1:]) if b > a]
    if not gaps:
        return 85.0
    typical = max(1.0, statistics.median(gaps))
    overdue = (at - dates[-1]).total_seconds() / 86400
    grace = max(180.0, 4 * typical)
    if overdue <= grace:
        return 90.0
    return clamp(90 - 55 * min(1, (overdue - grace) / max(grace, 180)))


def article_quality(entries):
    if not entries:
        return 65.0, 0
    items = list(entries.values())
    fingerprints = [e.get("fingerprint") for e in items if e.get("fingerprint")]
    uniqueness = 100 * len(set(fingerprints)) / len(fingerprints) if fingerprints else 85
    content = statistics.mean(e["content"] for e in items)
    metadata = statistics.mean(e["metadata"] for e in items)
    clean = statistics.mean(e["cleanliness"] for e in items)
    sampled = 0.375 * content + 0.25 * metadata + 0.25 * clean + 0.125 * uniqueness
    count = len(items)
    return (sampled * count + 65 * 5) / (count + 5), count


def feed_metadata_quality(result):
    return (20 + (30 if result.get("description") else 0)
            + (30 if result.get("website_url") else 0)
            + (10 if result.get("language_tag") else 0)
            + (10 if result.get("icon_url") else 0))


def smooth(current, observed, last_seen, checked_at, new_articles):
    if current is None:
        return observed
    if not new_articles:
        return current
    elapsed = max(0.0, (checked_at - last_seen).total_seconds() / 86400) if last_seen else 0
    alpha = min(MAX_SCAN_INFLUENCE, 1 - 2 ** (-elapsed / HALF_LIFE_DAYS))
    return current * (1 - alpha) + observed * alpha


def stable_published_score(raw, record, scan_id):
    """Small changes must persist across two distinct scans."""
    old = record.get("published_score")
    if old is None:
        new = raw
    elif abs(raw - old) >= CHANGE_THRESHOLD:
        new = raw
    elif abs(raw - old) < 1.5:
        new = old
    elif scan_id != record.get("ranked_scan"):
        direction = 1 if raw > old else -1
        previous = record.get("pending_direction")
        pending = record.get("pending_count", 0) + 1 if previous == direction else 1
        record["pending_count"] = pending
        record["pending_direction"] = direction
        new = raw if pending >= 2 else old
    else:
        new = old
    if new != old:
        record["pending_count"] = 0
        record["pending_direction"] = 0
    record["published_score"] = round(new, 2)
    record["ranked_scan"] = scan_id
    return record["published_score"]


def score_catalog(results, sources, featured_domains, previous_state=None, previous_positions=None, at=None):
    """Return diagnostic ranking and local history state; no I/O or network."""
    at = at or dt.datetime.now(UTC)
    state = previous_state if previous_state is not None else {"version": 1, "feeds": {}, "categories": {}}
    state.setdefault("feeds", {})
    state.setdefault("categories", {})
    source_map = {item["url"]: canonical_source(item.get("directory_url") or item["url"])
                  for item in sources}
    grouped = {}
    for result in results:
        url = result.get("resolved_url") or result["candidate"]["feed_url"]
        record = grouped.setdefault(url, {"results": [], "sources": set(), "categories": set()})
        record["results"].append(result)
        record["sources"].update(sources_for(result["candidate"], source_map))
        record["categories"].update(result["candidate"].get("categories", []))
    feed_scores = {}
    categories = defaultdict(list)
    for url, item in sorted(grouped.items()):
        successful = [r for r in item["results"] if r.get("accepted")]
        latest = max(successful or item["results"], key=lambda r: r.get("checked_at", ""))
        history = state["feeds"].setdefault(url, {"articles": {}, "checks": []})
        articles = history.setdefault("articles", {})
        checks = history.setdefault("checks", [])
        checked_at = timestamp(latest.get("checked_at")) or at
        last_check = timestamp(history.get("last_checked_at"))
        is_new_check = last_check is None or checked_at > last_check
        new_articles = 0
        if is_new_check:
            failure = not latest.get("accepted")
            error = str(latest.get("error", "")).lower()
            transient = (
                any(word in error for word in ("timeout", "timed out", "rate limit", "429", "503", "502", "connection"))
                or latest.get("http_status") in (429, 500, 502, 503, 504)
            )
            checks.append({"at": checked_at.isoformat(), "ok": not failure,
                           "weight": 0.25 if failure and transient else 1.0})
            checks[:] = [c for c in checks if timestamp(c["at"]) and (at - timestamp(c["at"])).days <= HISTORY_DAYS]
            for article in latest.get("entry_evidence", []) if not failure else []:
                key = article["key"]
                if key not in articles or articles[key].get("fingerprint") != article.get("fingerprint"):
                    new_articles += 1
                articles[key] = article
            if len(articles) > MAX_ARTICLES:
                kept = sorted(articles.items(), key=lambda x: (x[1].get("published_at") or "", x[0]), reverse=True)[:MAX_ARTICLES]
                history["articles"] = articles = dict(kept)
            observed, count = article_quality(articles)
            prior_time = timestamp(history.get("last_quality_observed_at"))
            if new_articles or history.get("article_quality") is None:
                history["article_quality"] = round(smooth(history.get("article_quality"), observed, prior_time, checked_at, new_articles), 4)
                history["last_quality_observed_at"] = checked_at.isoformat()
            history["last_checked_at"] = checked_at.isoformat()
        if not successful:
            continue
        meta = feed_metadata_quality(latest)
        health = reliability(checks, at)
        ongoing = activity(articles, at)
        core = history.get("article_quality", 65)
        quality = clamp(0.85 * core + 0.10 * health + 0.05 * ongoing)
        quality = clamp(0.95 * quality + 0.05 * meta)
        hint = any(featured(latest, cat, featured_domains) for cat in item["categories"])
        pop = popularity(item["sources"], hint)
        raw = min(40.0, 0.55 * quality + 0.45 * pop) if quality < 40 else 0.55 * quality + 0.45 * pop
        scan_id = history.get("last_checked_at", "")
        stable = stable_published_score(raw, history, scan_id)
        details = {"quality": round(quality, 2), "popularity_prior": round(pop, 2),
                   "score": stable, "article_count": len(articles),
                   "distinct_directory_sources": len(item["sources"]),
                   "confidence": round(len(articles) / (len(articles) + 5), 3),
                   "publisher_domain": publisher_domain(latest),
                   "categories": sorted(item["categories"])}
        feed_scores[url] = details
        for category in item["categories"]:
            categories[category].append({"url": url, **details})
    ordered_categories = {}
    prior = previous_positions or {}
    for category, feeds in categories.items():
        ordered = sorted(feeds, key=lambda f: (-f["score"], f["url"]))
        top = ordered[:TOP_FEEDS]
        top_strength = (sum(f["score"] for f in top) + 5 * 60) / (len(top) + 5)
        good_domains = {f["publisher_domain"] for f in feeds if f["quality"] >= 70}
        coverage = 100 * min(1, math.log1p(len(good_domains)) / math.log1p(100))
        diversity = 100 * len({f["publisher_domain"] for f in top}) / len(top)
        raw = 0.70 * top_strength + 0.20 * coverage + 0.10 * diversity
        category_record = state["categories"].setdefault(category, {})
        scan_id = max((state["feeds"][f["url"]].get("last_checked_at", "") for f in feeds), default="")
        stable = stable_published_score(raw, category_record, scan_id)
        ordered_categories[category] = {"score": stable, "feed_count": len(feeds)}
    order = sorted(ordered_categories, key=lambda c: (-ordered_categories[c]["score"], prior.get(c, 10**9), c))
    return {"version": 1, "feeds": feed_scores, "categories": ordered_categories,
            "category_order": order}, state
