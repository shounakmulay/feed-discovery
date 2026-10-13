# DevPulse feed discovery

This repository contains DevPulse's static RSS and Atom feed catalog and the scripts that collect, validate, and publish it as JSON.

## Catalog format

[`master.json`](master.json) is the catalog entry point. It lists the current collections, their relative paths, SHA-256 hashes, and feed counts. It also records the schema version, catalog ID, publication number, unique feed count, and total feed memberships.

Each file under `collections/<slug>/<sha256>.json` contains a collection's metadata and feed definitions. Feed definitions include an ID, title, feed URL, format, and optional description, website URL, icon URL, and language tag. A feed can belong to more than one collection.

Collection filenames are the SHA-256 hash of their exact file bytes. Publication creates new files when collection content changes and retains older files. Clients can cache collection URLs and use `master.json` to discover the current version. The publication number increases when the collection references change.

## Verify the catalog locally

The scripts use Python 3 and its standard library. Feed collection and online validation also require `curl` on your `PATH`.

From the repository root, run the offline consistency check:

```sh
python3 scripts/collect_feeds.py verify
```

The check verifies collection hashes, IDs, feed counts, duplicate definitions, and the saved validation evidence. Success prints `VERIFIED` followed by catalog totals. This command does not fetch publishers or prove that feeds are still available today.

## Update the feed catalog

[`sources.json`](sources.json) lists upstream OPML and CSV directories. [`supplemental-sources.json`](supplemental-sources.json) contains individual feed candidates and their categories and source URLs.

After you edit the source lists, run these commands in order:

```sh
python3 scripts/collect_feeds.py harvest
python3 scripts/collect_feeds.py validate
python3 scripts/collect_feeds.py publish
python3 scripts/collect_feeds.py verify
```

`harvest` fetches the directories and merges the supplemental feeds into `candidates.json`. `validate` fetches candidates that have no saved result. Accepted feeds must return HTTP 200 and parse as RSS, RSS 1.0, or Atom with at least one usable entry. An entry needs a title and an HTTP or HTTPS link.

Use `validate --refresh` to check all candidates again. Use `validate --retry-failed` to retry rejected candidates. `--workers` sets validation concurrency and defaults to 24.

`publish` builds the collections and master file from saved validation results. It also updates `feed-identities.json` and `verification-report.json`. Review the generated changes and preserve older collection files so existing URLs remain valid.

For rejected candidates whose publisher pages may advertise another feed URL, run `python3 scripts/rediscover_feeds.py`. The script updates the supplemental sources, candidates, and rediscovery report. Run validation and publication again after rediscovery.

## Build the Cloudflare Pages bundle

Run the same build command used to prepare the static assets:

```sh
python3 scripts/prepare_pages.py
```

The script first verifies the catalog, then creates `dist/` and `feed-discovery-pages.zip`. Both outputs are ignored by Git. The bundle includes `master.json`, all collection versions, and Pages header, redirect, and 404 files. Source lists, validation reports, and this README stay outside the public bundle.

For a Cloudflare Pages project connected to this repository, use these build settings:

- Production branch: `main`.
- Build command: `python3 scripts/prepare_pages.py`.
- Build output directory: `dist`.

The bundle redirects `/` to `/master.json`. It permits cross-origin reads and exposes `ETag`. The master file requires cache revalidation, while collection files have a one-year immutable cache policy.

Submit changes through a pull request to `main`. A merge updates the production branch for Cloudflare's Git integration. The build script prepares local assets and does not deploy them.
