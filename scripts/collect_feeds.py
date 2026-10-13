import argparse
import concurrent.futures
import csv
import datetime
import email.utils
import hashlib
import html
import io
import json
import re
import subprocess
import tempfile
import threading
import time
import urllib.parse
import uuid
import xml.etree.ElementTree as ET
from collections import Counter, defaultdict
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
MAX_BYTES = 12 * 1024 * 1024
NAMESPACE = uuid.UUID("ef4073c5-598c-4fc0-b380-9c1e3ad71e21")
CATALOG_ID = str(uuid.uuid5(NAMESPACE, "devpulse-feed-discovery"))
HOST_LOCKS = defaultdict(lambda: threading.Semaphore(2))
HOST_LOCK_GUARD = threading.Lock()
ATOM = "http://www.w3.org/2005/Atom"
RDF = "http://www.w3.org/1999/02/22-rdf-syntax-ns#"
RSS1 = "http://purl.org/rss/1.0/"


def now():
    return datetime.datetime.now(datetime.timezone.utc).isoformat().replace("+00:00", "Z")


def read_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def write_json(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    temporary.replace(path)


def normalize_url(value):
    parsed = urllib.parse.urlsplit(value.strip())
    if parsed.scheme not in ("http", "https") or not parsed.hostname or parsed.username or parsed.password:
        raise ValueError("Expected a public HTTP or HTTPS URL")
    host = parsed.hostname.lower().encode("idna").decode("ascii")
    if host in ("localhost", "localhost.localdomain") or host.endswith(".local"):
        raise ValueError("Local hosts are not feed sources")
    port = parsed.port
    netloc = host + ((":" + str(port)) if port and port not in (80, 443) else "")
    return urllib.parse.urlunsplit((parsed.scheme.lower(), netloc, parsed.path or "/", parsed.query, ""))


def candidate_key(value):
    parsed = urllib.parse.urlsplit(normalize_url(value))
    return urllib.parse.urlunsplit(("https", parsed.netloc, parsed.path, parsed.query, ""))


def fetch(url, timeout=25):
    url = normalize_url(url)
    with tempfile.TemporaryDirectory(prefix="devpulse-rss-") as folder:
        body_path = Path(folder) / "body"
        header_path = Path(folder) / "headers"
        command = [
            "curl", "--silent", "--show-error", "--location", "--max-redirs", "5",
            "--proto", "=http,https", "--proto-redir", "=http,https",
            "--connect-timeout", "8", "--max-time", str(timeout), "--compressed",
            "--max-filesize", str(MAX_BYTES), "--user-agent", "DevPulseFeedDiscovery/1.0",
            "--header", "Accept: application/atom+xml,application/rss+xml,application/xml,text/xml;q=0.9,*/*;q=0.5",
            "--dump-header", str(header_path), "--output", str(body_path),
            "--write-out", "%{http_code}\n%{url_effective}\n%{content_type}\n", url,
        ]
        process = subprocess.run(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=timeout + 10)
        output = process.stdout.decode("utf-8", errors="replace").splitlines()
        status = int(output[0]) if output and output[0].isdigit() else 0
        final_url = output[1] if len(output) > 1 else url
        content_type = output[2] if len(output) > 2 else None
        headers = {}
        if header_path.exists():
            for line in header_path.read_text(encoding="utf-8", errors="replace").splitlines():
                if line.startswith("HTTP/"):
                    headers = {}
                elif ":" in line:
                    key, value = line.split(":", 1)
                    headers[key.lower()] = value.strip()
        if process.returncode:
            error = process.stderr.decode("utf-8", errors="replace").strip()[-500:]
            return {"status": status, "url": final_url, "content_type": content_type, "headers": headers, "error": error}, b""
        if not body_path.exists() or body_path.stat().st_size > MAX_BYTES:
            return {"status": status, "url": final_url, "error": "Response exceeds size limit", "headers": headers}, b""
        return {"status": status, "url": normalize_url(final_url), "content_type": content_type, "headers": headers}, body_path.read_bytes()


def parse_xml(body):
    if re.search(br"<!ENTITY\s", body, flags=re.IGNORECASE):
        raise ValueError("XML entity declarations are not accepted")
    return ET.fromstring(body)


def plain(value, limit=500):
    if not value:
        return None
    value = html.unescape(re.sub(r"<[^>]+>", " ", value))
    return re.sub(r"\s+", " ", value).strip()[:limit] or None


def local_name(element):
    return element.tag.rsplit("}", 1)[-1]


def child_text(element, name):
    for child in element:
        if local_name(child) == name:
            return "".join(child.itertext()).strip()
    return None


def public_link(value, base):
    if not value:
        return None
    try:
        return normalize_url(urllib.parse.urljoin(base, value))
    except (ValueError, UnicodeError):
        return None


def entry_link(entry, base, atom):
    if atom:
        for link in entry.findall("{" + ATOM + "}link"):
            if link.get("rel", "alternate") == "alternate":
                result = public_link(link.get("href"), base)
                if result:
                    return result
        return public_link(child_text(entry, "id"), base) if (child_text(entry, "id") or "").startswith(("http://", "https://")) else None
    link = public_link(child_text(entry, "link"), base)
    if link:
        return link
    guid = child_text(entry, "guid")
    return public_link(guid, base) if (guid or "").startswith(("http://", "https://")) else None


def parse_date(value):
    if not value:
        return None
    try:
        parsed = datetime.datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        try:
            parsed = email.utils.parsedate_to_datetime(value)
        except (ValueError, TypeError, IndexError):
            return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=datetime.timezone.utc)
    return parsed.astimezone(datetime.timezone.utc)


def feed_metadata(body, url):
    root = parse_xml(body)
    if root.tag == "{" + ATOM + "}feed":
        feed = root
        entries = root.findall("{" + ATOM + "}entry")
        feed_format = "atom"
    elif local_name(root) == "rss":
        feed = next((child for child in root if local_name(child) == "channel"), None)
        if feed is None:
            raise ValueError("RSS channel is missing")
        entries = [child for child in feed if local_name(child) == "item"]
        feed_format = "rss"
    elif root.tag == "{" + RDF + "}RDF":
        feed = root.find("{" + RSS1 + "}channel")
        if feed is None:
            raise ValueError("RSS 1.0 channel is missing")
        entries = root.findall("{" + RSS1 + "}item")
        feed_format = "rss1"
    else:
        raise ValueError("Response is not an RSS or Atom feed")
    usable = [entry for entry in entries if plain(child_text(entry, "title"), 200) and entry_link(entry, url, feed_format == "atom")]
    if not usable:
        raise ValueError("Feed has no entries with a title and usable HTTP link")
    title = plain(child_text(feed, "title"), 200)
    if not title:
        raise ValueError("Feed title is missing")
    website = None
    if feed_format == "atom":
        for link in feed.findall("{" + ATOM + "}link"):
            if link.get("rel", "alternate") == "alternate":
                website = public_link(link.get("href"), url)
                if website:
                    break
        description = plain(child_text(feed, "subtitle"))
    else:
        website = public_link(child_text(feed, "link"), url)
        description = plain(child_text(feed, "description"))
    dates = []
    for entry in usable:
        for child in entry:
            if local_name(child) in ("pubDate", "published", "updated", "date"):
                date = parse_date(child.text)
                if date:
                    dates.append(date)
    latest = max(dates) if dates else None
    current = datetime.datetime.now(datetime.timezone.utc)
    freshness = "unknown"
    if latest:
        age = (current - latest).days
        freshness = "future-dated" if age < -2 else "recent" if age <= 365 else "older"
    language = child_text(feed, "language") or feed.get("{http://www.w3.org/XML/1998/namespace}lang")
    icon = public_link(child_text(feed, "icon") or child_text(feed, "logo"), url) if feed_format == "atom" else None
    if feed_format != "atom":
        for child in feed:
            if local_name(child) == "image":
                icon = public_link(child_text(child, "url") or child.get("href"), url)
                if icon:
                    break
    return {
        "title": title, "description": description, "website_url": website,
        "icon_url": icon, "language_tag": plain(language, 50), "feed_format": feed_format,
        "entry_count": len(entries), "usable_entry_count": len(usable),
        "latest_entry_at": latest.isoformat().replace("+00:00", "Z") if latest else None,
        "freshness": freshness,
    }


def category_for(source, groups, title, url):
    mapping = source.get("group_categories", {})
    category = source.get("category", "developer-blogs")
    for group in groups:
        if group in mapping:
            category = mapping[group]
    value = (title + " " + url).lower()
    overrides = [
        ("android-development", r"android[- .]|androiddevelopers|droidcon|commonsware|androidweekly"),
        ("kotlin", r"kotlin|touchlab|kmp|cashapp.*multiplatform"),
        ("ios-swift", r"swiftbysundell|swiftwith|swiftlee|hackingwithswift|swift\.org|swiftjective|objc\.io|iosdevweekly|useyourloaf|donnywals|avanderlee|raywenderlich"),
        ("languages", r"rust-lang|go\.dev|golang|python\.org|realpython|ruby-lang|elixir-lang|php\.net|scala-lang|dotnet|\.net blog|java blog|inside\.java|clojure"),
        ("databases", r"postgres|mysql|mongodb|cockroach|sqlite|redis\.io|timescale|neo4j|clickhouse|planetscale"),
        ("cloud-devops", r"kubernetes|docker\.com|hashicorp|aws\.amazon|azure\.com|cloud\.google|cncf|prometheus|grafana|pulumi|terraform|ansible"),
        ("cybersecurity", r"security|krebson|schneier|portswigger|troyhunt|bleepingcomputer|thehackernews|darkreading"),
        ("ai-ml", r"machine learning|deep learning|huggingface|deepmind|openai\.com|anthropic|pytorch|tensorflow|bair\.berkeley|ai\.google"),
    ]
    if category in ("developer-blogs", "engineering", "frontend", "developer-tools", "technology", "programming"):
        for target, pattern in overrides:
            if re.search(pattern, value):
                return target
    return category


def extract_opml(body, source):
    try:
        root = parse_xml(body)
    except ET.ParseError:
        escaped = re.sub(br"&(?!#\d+;|#x[0-9a-fA-F]+;|amp;|lt;|gt;|quot;|apos;)", b"&amp;", body)
        root = parse_xml(escaped)
    candidates = []

    def walk(element, groups):
        for outline in element:
            if local_name(outline) != "outline":
                walk(outline, groups)
                continue
            title = outline.get("title") or outline.get("text") or ""
            url = outline.get("xmlUrl") or outline.get("xmlurl")
            if url:
                try:
                    url = normalize_url(url)
                except (ValueError, UnicodeError):
                    continue
                candidates.append({
                    "feed_url": url, "title": title, "website_url": outline.get("htmlUrl") or None,
                    "category": category_for(source, groups, title, url),
                    "sources": [{"url": source["url"], "groups": groups, "title": title}],
                })
            else:
                walk(outline, groups + [title])

    walk(root, [])
    return candidates


def merge_candidates(candidates):
    merged = {}
    for candidate in candidates:
        key = candidate_key(candidate["feed_url"])
        if key not in merged:
            merged[key] = dict(candidate, categories=[], sources=[])
        target = merged[key]
        for category in candidate.get("categories", [candidate.get("category", "developer-blogs")]):
            if category not in target["categories"]:
                target["categories"].append(category)
        for source in candidate.get("sources", [{"url": candidate.get("source_url", candidate["feed_url"]), "title": candidate["title"], "groups": []}]):
            if source not in target["sources"]:
                target["sources"].append(source)
        if candidate["feed_url"].startswith("https://"):
            target["feed_url"] = candidate["feed_url"]
    for value in merged.values():
        value.pop("category", None)
        value["categories"].sort()
        value["sources"].sort(key=lambda item: (item["url"], item.get("title", "")))
    return sorted(merged.values(), key=lambda item: candidate_key(item["feed_url"]))


def extract_csv(body, source):
    rows = csv.DictReader(io.StringIO(body.decode("utf-8-sig")), delimiter=";")
    candidates = []
    for row in rows:
        if "rss" not in row.get("TYPE", "").lower() or row.get("SUBDOMAIN", "").lower() == "youtube":
            continue
        try:
            url = normalize_url(row["URL"])
        except (ValueError, UnicodeError):
            continue
        category = {"ISSE": "cybersecurity", "SRE": "cloud-devops", "SWE": "engineering"}.get(row.get("DOMAIN"), "engineering")
        if row.get("SUBDOMAIN") == "AI":
            category = "ai-ml"
        elif row.get("SUBDOMAIN") in ("Python", "Go", "Rust"):
            category = "languages"
        candidates.append({"feed_url": url, "title": row["NAME"], "website_url": None, "category": category, "sources": [{"url": source["url"], "groups": [row.get("DOMAIN", ""), row.get("SUBDOMAIN", "")], "title": row["NAME"]}]})
    return candidates


def harvest(extra=None):
    candidates = []
    source_results = []
    for source in read_json(ROOT / "sources.json"):
        response, body = fetch(source["url"], timeout=40)
        try:
            if response.get("error") or response["status"] != 200:
                raise ValueError(response.get("error") or "HTTP " + str(response["status"]))
            extracted = extract_csv(body, source) if source.get("format") == "csv" else extract_opml(body, source)
            candidates.extend(extracted)
            result = dict(source, candidate_count=len(extracted), document_sha256=hashlib.sha256(body).hexdigest(), retrieved_at=now())
            print("source", source["name"], len(extracted), flush=True)
        except (ValueError, ET.ParseError) as error:
            result = dict(source, error=str(error), retrieved_at=now())
            print("source failed", source["name"], str(error), flush=True)
        source_results.append(result)
    extra_path = Path(extra) if extra else ROOT / "supplemental-sources.json"
    if extra_path.exists():
        candidates.extend(read_json(extra_path))
    write_json(ROOT / "candidates.json", merge_candidates(candidates))
    write_json(ROOT / "source-report.json", source_results)
    print("unique candidates", len(read_json(ROOT / "candidates.json")), flush=True)


def validate_candidate(candidate):
    url = candidate["feed_url"]
    host = urllib.parse.urlsplit(url).hostname
    with HOST_LOCK_GUARD:
        lock = HOST_LOCKS[host]
    checked_at = now()
    with lock:
        response, body = fetch(candidate_key(url))
        if (response.get("error") or response["status"] != 200) and url.startswith("http://"):
            response, body = fetch(url)
    result = {"candidate": candidate, "checked_at": checked_at, "http_status": response["status"], "resolved_url": response["url"], "content_type": response.get("content_type"), "retry_after": response["headers"].get("retry-after")}
    if response.get("error") or response["status"] != 200:
        result["error"] = response.get("error") or "HTTP " + str(response["status"])
        result["accepted"] = False
        return result
    try:
        metadata = feed_metadata(body, response["url"])
        result.update(metadata)
        result.update({"accepted": True, "response_bytes": len(body), "response_sha256": hashlib.sha256(body).hexdigest(), "etag": response["headers"].get("etag"), "last_modified": response["headers"].get("last-modified")})
    except (ValueError, ET.ParseError, OverflowError) as error:
        result.update({"accepted": False, "error": str(error)})
    return result


def validate(workers, retry_failed=False, refresh=False):
    candidates = read_json(ROOT / "candidates.json")
    existing = read_json(ROOT / "validation-results.json") if (ROOT / "validation-results.json").exists() else []
    candidate_map = {candidate_key(item["feed_url"]): item for item in candidates}
    results = {candidate_key(item["candidate"]["feed_url"]): dict(item, candidate=candidate_map[candidate_key(item["candidate"]["feed_url"])]) for item in existing if candidate_key(item["candidate"]["feed_url"]) in candidate_map}
    pending = [candidate for candidate in candidates if refresh or candidate_key(candidate["feed_url"]) not in results or (retry_failed and not results[candidate_key(candidate["feed_url"])]["accepted"])]
    print("validation pending", len(pending), "previous", len(results), flush=True)
    start = time.monotonic()
    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as executor:
        futures = {executor.submit(validate_candidate, candidate): candidate for candidate in pending}
        for index, future in enumerate(concurrent.futures.as_completed(futures), 1):
            candidate = futures[future]
            try:
                result = future.result()
            except Exception as error:
                result = {"candidate": candidate, "checked_at": now(), "accepted": False, "error": str(error), "http_status": 0, "resolved_url": candidate["feed_url"]}
            results[candidate_key(candidate["feed_url"])] = result
            if index % 25 == 0 or index == len(pending):
                write_json(ROOT / "validation-results.json", sorted(results.values(), key=lambda item: candidate_key(item["candidate"]["feed_url"])))
                accepted = sum(item["accepted"] for item in results.values())
                print("validated", index, "/", len(pending), "accepted", accepted, "elapsed_seconds", round(time.monotonic() - start), flush=True)
    write_json(ROOT / "validation-results.json", sorted(results.values(), key=lambda item: candidate_key(item["candidate"]["feed_url"])))


CATEGORIES = {
    "developer-blogs": ("Developer blogs", "Personal writing about software development and the web."),
    "engineering": ("Engineering blogs", "Engineering teams, architecture, and software delivery."),
    "frontend": ("Frontend and web development", "HTML, CSS, JavaScript, frameworks, and web development."),
    "web-standards": ("Web standards and accessibility", "Web platform standards, accessibility, and web organizations."),
    "browsers": ("Browsers", "Browser engines, browser releases, and web platform implementation."),
    "developer-tools": ("Developer tools", "Editors, build tools, testing, and developer workflows."),
    "android-development": ("Android development", "Android apps, APIs, tooling, and development."),
    "kotlin": ("Kotlin and multiplatform", "Kotlin language and Kotlin Multiplatform development."),
    "ios-swift": ("iOS and Swift", "Apple platform development, Swift, and iOS."),
    "flutter": ("Flutter", "Flutter framework and cross-platform application development."),
    "backend": ("Backend and distributed systems", "Server development, APIs, architecture, and distributed systems."),
    "languages": ("Programming languages", "Language releases, runtimes, and programming techniques."),
    "programming": ("Programming", "General programming, software development, and developer communities."),
    "databases": ("Databases", "Database engines, data infrastructure, and storage."),
    "cloud-devops": ("Cloud, DevOps, and infrastructure", "Cloud platforms, containers, observability, and infrastructure."),
    "cybersecurity": ("Cybersecurity and privacy", "Security research, vulnerabilities, privacy, and defense."),
    "ai-ml": ("AI and machine learning", "AI research, machine learning tools, and applied AI."),
    "technology": ("Technology news", "Technology reporting, products, and the technology industry."),
    "android": ("Android news", "Android devices, apps, and ecosystem news."),
    "apple": ("Apple news", "Apple devices, software, and ecosystem news."),
    "science": ("Science", "Science research, discoveries, and scientific reporting."),
    "space": ("Space and astronomy", "Spaceflight, astronomy, and space science."),
    "environment": ("Climate and environment", "Climate, energy, conservation, and the environment."),
    "news-world": ("World news", "General news and international reporting."),
    "news-india": ("India news", "Indian news publishers and reporting."),
    "business-startups": ("Business and startups", "Business reporting, startups, entrepreneurship, and economics."),
    "design": ("Design and UX", "Product design, interface design, and user experience."),
    "gaming": ("Gaming", "Games, game development, and gaming industry reporting."),
    "hardware-makers": ("Hardware and makers", "Electronics, DIY projects, hardware, and making."),
    "learning": ("Learning and productivity", "Learning, education, and personal productivity."),
    "open-data": ("Open data and public technology", "Government technology, public services, and open data."),
}


def publish():
    results = read_json(ROOT / "validation-results.json")
    candidates = read_json(ROOT / "candidates.json")
    expected = {candidate_key(item["feed_url"]) for item in candidates}
    actual = {candidate_key(item["candidate"]["feed_url"]) for item in results}
    if expected != actual or len(actual) != len(results):
        raise ValueError("Validation coverage is incomplete or has duplicate results")
    current = datetime.datetime.now(datetime.timezone.utc)
    if any((current - parse_date(item["checked_at"])).total_seconds() > 86400 for item in results):
        raise ValueError("Validation is older than 24 hours; run validate --refresh")
    accepted = {}
    for result in results:
        if not result["accepted"]:
            continue
        key = normalize_url(result["resolved_url"])
        if key not in accepted:
            accepted[key] = dict(result, categories=set(), candidates=[])
        accepted[key]["categories"].update(result["candidate"]["categories"])
        accepted[key]["candidates"].append(result["candidate"])
    aliases = read_json(ROOT / "feed-aliases.json") if (ROOT / "feed-aliases.json").exists() else []
    for alias in aliases:
        canonical = normalize_url(alias["canonical_url"])
        alternate = normalize_url(alias["alias_url"])
        if canonical not in accepted or alternate not in accepted:
            continue
        primary = accepted[canonical]
        duplicate = accepted[alternate]
        if primary["response_sha256"] != duplicate["response_sha256"]:
            raise ValueError("Previously confirmed feed aliases now differ; review " + alternate)
        primary["categories"].update(duplicate["categories"])
        primary["candidates"].extend(duplicate["candidates"])
        primary.setdefault("verified_aliases", []).append(alternate)
        del accepted[alternate]
    groups = defaultdict(list)
    identities = read_json(ROOT / "feed-identities.json") if (ROOT / "feed-identities.json").exists() else {}
    evidence = []
    for url, result in sorted(accepted.items()):
        aliases = [candidate_key(candidate["feed_url"]) for candidate in result["candidates"]] + [candidate_key(url)] + [candidate_key(value) for value in result.get("verified_aliases", [])]
        known = sorted(set(identities[alias] for alias in aliases if alias in identities))
        if len(known) > 1:
            raise ValueError("Conflicting established IDs for redirected feed " + url)
        feed_id = known[0] if known else str(uuid.uuid5(NAMESPACE, url))
        for alias in aliases:
            identities[alias] = feed_id
        feed = {"id": feed_id, "feed_url": url, "title": result["title"], "description": result["description"], "website_url": result["website_url"], "icon_url": result["icon_url"], "language_tag": result["language_tag"], "feed_format": result["feed_format"]}
        for category in sorted(result["categories"]):
            groups[category].append(feed)
        evidence.append({"id": feed_id, "feed_url": url, "verified_aliases": result.get("verified_aliases", []), "categories": sorted(result["categories"]), "checked_at": result["checked_at"], "http_status": result["http_status"], "content_type": result["content_type"], "response_bytes": result["response_bytes"], "response_sha256": result["response_sha256"], "entry_count": result["entry_count"], "usable_entry_count": result["usable_entry_count"], "latest_entry_at": result["latest_entry_at"], "freshness": result["freshness"], "etag": result["etag"], "last_modified": result["last_modified"], "sources": result["candidates"]})
    references = []
    for position, category in enumerate(CATEGORIES):
        feeds = groups.get(category)
        if not feeds:
            continue
        title, description = CATEGORIES[category]
        document = {"schema_version": 1, "id": str(uuid.uuid5(NAMESPACE, "collection:" + category)), "slug": category, "title": title, "description": description, "feeds": sorted(feeds, key=lambda item: (item["title"].casefold(), item["id"]))}
        encoded = (json.dumps(document, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8")
        digest = hashlib.sha256(encoded).hexdigest()
        relative = "collections/" + category + "/" + digest + ".json"
        path = ROOT / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists() and path.read_bytes() != encoded:
            raise ValueError("Immutable collection collision")
        path.write_bytes(encoded)
        references.append({"id": document["id"], "slug": category, "title": title, "path": relative, "sha256": digest, "position": position, "feed_count": len(feeds)})
    previous = read_json(ROOT / "master.json") if (ROOT / "master.json").exists() else None
    changed = not previous or previous["collections"] != references
    publication = (previous["publication"] + int(changed)) if previous else 1
    master = {"schema_version": 1, "catalog_id": CATALOG_ID, "publication": publication, "collections": references, "unique_feed_count": len(accepted), "feed_membership_count": sum(len(feeds) for feeds in groups.values())}
    write_json(ROOT / "feed-identities.json", identities)
    write_json(ROOT / "verification-report.json", {"generated_at": now(), "criteria": {"http_status": 200, "formats": ["rss", "rss1", "atom"], "minimum_usable_entries": 1, "requires_title_and_http_entry_link": True, "tls_verification": True, "maximum_response_bytes": MAX_BYTES}, "candidate_count": len(results), "unique_verified_feeds": len(accepted), "collections": len(references), "freshness_counts": dict(Counter(item["freshness"] for item in evidence)), "feeds": evidence, "rejected": [item for item in results if not item["accepted"]]})
    write_json(ROOT / "master.json", master)
    print("published", len(accepted), "unique feeds", len(references), "collections", master["feed_membership_count"], "memberships", flush=True)


def verify():
    master = read_json(ROOT / "master.json")
    report = read_json(ROOT / "verification-report.json")
    evidence = {item["id"]: item for item in report["feeds"]}
    results = read_json(ROOT / "validation-results.json")
    expected = {candidate_key(item["feed_url"]) for item in read_json(ROOT / "candidates.json")}
    assert expected == {candidate_key(item["candidate"]["feed_url"]) for item in results}
    assert report["candidate_count"] == len(expected) == len(results)
    definitions = {}
    memberships = 0
    for reference in master["collections"]:
        path = ROOT / reference["path"]
        assert path.resolve().is_relative_to(ROOT.resolve()) if hasattr(path.resolve(), "is_relative_to") else str(path.resolve()).startswith(str(ROOT.resolve()) + "/")
        body = path.read_bytes()
        assert hashlib.sha256(body).hexdigest() == reference["sha256"], path
        document = json.loads(body)
        assert document["id"] == reference["id"]
        assert document["schema_version"] == 1 and "opml_text" not in document
        assert len(document["feeds"]) == reference["feed_count"]
        ids = [feed["id"] for feed in document["feeds"]]
        assert len(ids) == len(set(ids))
        for feed in document["feeds"]:
            uuid.UUID(feed["id"])
            if feed["id"] in definitions:
                assert definitions[feed["id"]] == feed, feed["id"]
            definitions[feed["id"]] = feed
            proof = evidence[feed["id"]]
            assert proof["http_status"] == 200 and proof["usable_entry_count"] > 0
            assert proof["feed_url"] == feed["feed_url"]
            assert normalize_url(feed["feed_url"]) == feed["feed_url"]
            assert feed["title"] and feed["feed_format"] in ("rss", "rss1", "atom")
        memberships += len(ids)
    assert len(definitions) == master["unique_feed_count"] == len(evidence)
    assert len({feed["feed_url"] for feed in definitions.values()}) == len(definitions)
    assert memberships == master["feed_membership_count"]
    print("VERIFIED", len(definitions), "unique feeds", len(master["collections"]), "collections", memberships, "memberships")
    for reference in master["collections"]:
        print(reference["slug"], reference["feed_count"])


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=("harvest", "validate", "publish", "verify"))
    parser.add_argument("--extra")
    parser.add_argument("--workers", type=int, default=24)
    parser.add_argument("--retry-failed", action="store_true")
    parser.add_argument("--refresh", action="store_true")
    args = parser.parse_args()
    if args.command == "harvest":
        harvest(args.extra)
    elif args.command == "validate":
        validate(args.workers, args.retry_failed, args.refresh)
    elif args.command == "publish":
        publish()
    else:
        verify()


if __name__ == "__main__":
    main()
