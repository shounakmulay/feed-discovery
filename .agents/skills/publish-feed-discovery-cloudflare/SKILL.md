---
name: publish-feed-discovery-cloudflare
description: Publish the standalone DevPulse feed-discovery catalog as public static JSON to an existing Cloudflare Pages project and verify live content, caching, and ETags. Use when explicitly asked to deploy or publish the catalog, not for local feed updates alone.
---

# Publish feed discovery to Cloudflare

Use the checkout containing `scripts/prepare_pages.py` and `master.json`. Otherwise use `/Users/shounakmulay/Documents/Work/feed-discovery`. The private source repository is `shounakmulay/feed-discovery`. The deployed catalog is public.

Publish the current catalog. Refresh publisher feeds only when the user also requests a catalog update. Keep deployment credentials out of source files, Git, logs, and the public bundle.

## Verify the deployment target

Inspect the selected Cloudflare account, existing Pages projects, production branch, and domains. Use the dashboard or these read-only commands:

```bash
npx wrangler@4 whoami
npx wrangler@4 pages project list
npx wrangler@4 pages deployment list --project-name <verified-project-name>
```

The previous project name was `devpulse-feed-discovery`. Treat it as a lookup hint. Match the user's actual uploaded project and URL. Stop if the target remains ambiguous. Do not create a replacement project or change production branch settings without authorization.

## Build the static bundle

```bash
python3 scripts/prepare_pages.py
```

The packager verifies the catalog before writing `dist/` and `feed-discovery-pages.zip`. Inspect both outputs. Include the master, current and retained older immutable collections, `_headers`, `_redirects`, and `404.html`. Exclude `_worker.js`, `_routes.json`, any `functions` directory, scripts, source inputs, identity records, audit logs, and validation reports.

Keep the deployment static so requests do not invoke Pages Functions. No client API key or secret binding is required. Retain old immutable collections for clients holding cached masters. Do not edit hash-addressed documents or rewrite established feed identities.

## Deploy the verified folder

After verifying the target and local bundle, run the authorized production deployment. Use the project's actual production branch if it differs from `main`.

```bash
npx wrangler@4 pages deploy dist --project-name <verified-project-name> --branch main
```

Run from the repository root and confirm it contains no `functions` directory that Wrangler could upload. For a Direct Upload project, dashboard upload of the generated folder or ZIP is also supported. Wrangler uploads the folder, not the ZIP.

## Verify the live deployment

Check both the returned deployment URL and the intended production alias or custom domain without authentication headers:

- `/master.json` returns 200 and matches the local master bytes and publication number.
- `/` redirects to `/master.json`.
- A referenced collection returns 200 and its SHA-256 matches the master's reference. Retained old collection paths remain accessible.
- Master caching uses `public, max-age=0, must-revalidate`. Collections use `public, max-age=31536000, immutable`.
- The returned master ETag with `If-None-Match` returns 304 and no body. HEAD returns the same ETag with no body.
- An unknown path returns 404 rather than catalog JSON.
- Cloudflare deployment metadata shows no Functions or Worker bundle.

Do not declare publication verified from a successful upload alone. Report a failed live check with the deployment URL. Do not delete earlier deployments unless requested.

Record the deployment ID, verified URLs, publication number, bundle hash, and test outcomes in `audit/decisions.tsv`. Report deployment and Git commit status separately.

## Official references

- [Pages Direct Upload](https://developers.cloudflare.com/pages/get-started/direct-upload/).
- [Pages static request pricing](https://developers.cloudflare.com/pages/functions/pricing/).
- [Wrangler Pages commands](https://developers.cloudflare.com/workers/wrangler/commands/pages/).
