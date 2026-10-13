---
name: update-feed-discovery
description: Update, regenerate, or add RSS and Atom feeds to the standalone DevPulse feed-discovery catalog, validate publisher availability, and prepare its static Pages bundle. Use for catalog maintenance, collection edits, or feed rediscovery.
---

# Update feed discovery

Use the current checkout when it contains `scripts/collect_feeds.py` and `master.json`. Otherwise use `/Users/shounakmulay/Documents/Work/feed-discovery`. Run the commands below from that repository root. Keep this work outside the DevPulse application repository.

An update request authorizes local catalog maintenance and verification. Deployment, remote pushes, and schema changes require their own authorization.

## Choose the update

Read the current scripts and input shapes before editing. Preserve unrelated local changes. Use one writer for the candidate files, validation results, identity registry, and publication files.

- For new publisher feeds, append records to `supplemental-sources.json`. Each record has `feed_url`, `title`, `category`, `website_url`, and `source_url`. Use advertised publisher feed URLs and retain discovery evidence.
- For a new directory, edit `sources.json`. OPML is the default input format. CSV sources require `format: csv` and the collector's existing semicolon-delimited field layout.
- For category changes, inspect `CATEGORIES` and `category_for()` in `scripts/collect_feeds.py`. Multiple candidate categories produce multiple collection memberships.
- For a fresh availability update, revalidate all candidates with `--refresh`.
- For an interrupted scan, run `validate` without `--refresh` to resume.

Do not hand-edit `master.json` or the immutable collection documents.

## Harvest and inspect source coverage

Before harvest, keep temporary copies of `candidates.json` and `source-report.json`. Record the current master counts and collection references for the later comparison.

```bash
python3 scripts/collect_feeds.py harvest
```

Inspect `source-report.json` before continuing. Harvest can record a failed source, overwrite `candidates.json`, and still exit successfully. Check every source for an `error`, an unexpected empty result, or a large count decrease.

If a source failed, restore the temporary candidate and source-report copies before retrying. Preserve source edits and supplemental additions. Do not publish removals caused by an incomplete harvest. If recovery requires further source research or the user requested removal of a source, resolve that explicitly within the requested scope.

## Validate and optionally rediscover

For a full update, run:

```bash
python3 scripts/collect_feeds.py validate --refresh
```

For additions or an interrupted scan, run:

```bash
python3 scripts/collect_feeds.py validate
```

Default validation reuses prior results. `--retry-failed` rechecks rejected candidates only. Neither command refreshes an accepted feed. Publication requires complete candidate coverage and checks no older than 24 hours. If prior checks are stale, use `--refresh`.

Inspect rejected candidates and compare proposed removals with the previous catalog. Broad timeouts, rate limits, or a publisher outage do not justify mass removals. Preserve the current published master while resolving those failures. Respect `Retry-After`. Limit routine retries to one additional pass, then report unresolved failures.

```bash
python3 scripts/collect_feeds.py validate --retry-failed
```

If old feed URLs may have moved, run publisher rediscovery after validation:

```bash
python3 scripts/rediscover_feeds.py
python3 scripts/collect_feeds.py validate
```

Rediscovery checks publisher pages for rejected candidates with HTTP 200, 404, or 410 responses. It updates both `supplemental-sources.json` and `candidates.json`. Inspect `rediscovery-report.json`. Newly discovered URLs still require feed validation.

## Preserve identities and publish locally

Keep `feed-identities.json`, `feed-aliases.json`, the collector's UUID namespace, and the catalog ID. Keep the current `master.json` so publication numbers continue from the existing catalog.

Confirm publisher aliases before adding them to `feed-aliases.json`. Publication checks that the current response hashes agree. If aliases disagree or established IDs conflict, investigate before publishing. Do not erase the registry or rewrite established IDs to bypass a conflict. Full-content and ordinary feed variants can remain separate.

```bash
python3 scripts/collect_feeds.py publish
python3 scripts/collect_feeds.py verify
```

Publication writes collection files under `collections/<slug>/<sha256>.json` before replacing the master. A shared feed has one stable ID and the same metadata across collections. Changed collection references increment the publication number. Unchanged content retains it.

Keep old immutable collection files. Clients can still hold older cached masters. Never modify a document at an existing hash path. This dataset embeds feed metadata directly in collection JSON. It does not generate OPML.

Review `verification-report.json` for counts, rejected feeds, aliases, and freshness flags. Availability is a point-in-time check. Older posts do not make a reachable feed invalid. Some accepted feeds can use HTTP.

## Prepare the Pages bundle

```bash
python3 scripts/prepare_pages.py
```

Inspect `dist/` and `feed-discovery-pages.zip`. The archive contains the upload folder's contents. Include `master.json`, its referenced immutable collections, retained older immutable collections, `_headers`, `_redirects`, and `404.html`. Exclude scripts, source inputs, audit logs, identity records, and validation reports from the public bundle. Packaging does not deploy it.

Append the maintenance outcome to `audit/decisions.tsv` with real evidence paths. Report added and removed feeds, the publication number, verification results, the bundle path, and unresolved source failures. State deployment and commit status separately when those actions were requested.
