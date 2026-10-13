---
name: publish-feed-discovery-cloudflare
description: Publish the standalone DevPulse feed-discovery catalog to an existing Cloudflare Pages project with X-API-Key authentication, encrypted secrets, and live HTTP verification. Use when explicitly asked to deploy or publish the catalog, not for local feed updates alone.
---

# Publish feed discovery to Cloudflare

Use the checkout containing `scripts/prepare_pages.py` and `cloudflare/_worker.js`. Otherwise use `/Users/shounakmulay/Documents/Work/feed-discovery`. The private source repository is `shounakmulay/feed-discovery`.

Publish the current catalog. Refresh publisher feeds only when the user also requests a catalog update. A deployment request does not authorize deleting historical deployments, creating a replacement project, or pushing Git changes.

## Verify the deployment target

Inspect the selected Cloudflare account, existing Pages projects, production branch, domains, and deployments. Use the dashboard or these read-only commands:

```bash
npx wrangler@4 whoami
npx wrangler@4 pages project list
npx wrangler@4 pages deployment list --project-name <verified-project-name>
```

The previous project name was `devpulse-feed-discovery`. Treat it as a lookup hint. Match the user's actual uploaded project and URL. Do not assume the private GitHub repository has automatic deployment configured. Stop if the target remains ambiguous.

Verify that the production branch is `main` before using the production command below. If it differs, use its actual branch. Do not change branch settings to make the example work.

## Configure encrypted secrets before deployment

`FEED_DISCOVERY_API_KEY` is the catalog's client credential. A Cloudflare API token or Wrangler login authorizes deployment. Keep those credentials separate.

Use the selected Pages project's **Settings > Variables and Secrets**. Set `FEED_DISCOVERY_API_KEY` and select **Encrypt**. Configure Production and Preview separately before deploying either environment. Check the secret name and environment without revealing its value. Prefer the dashboard when environment targeting is unclear.

If using a supported Wrangler secret command, have the user enter the value directly in its interactive prompt. Never place a secret in a command argument, source file, Git commit, log, chat message, or public bundle. Local `.dev.vars` files must be ignored by Git. Do not copy them into `dist/`.

If the secret is unavailable, complete local checks and report the missing setup. Do not deploy. An unset binding must reject every request.

For a Workers Free plan, set **Settings > Runtime > Fail open / closed** to **Fail closed**. Verify the setting before deployment. Fail open can serve static assets without the authentication Worker after the daily Function allowance is exhausted.

## Build and inspect the authenticated bundle

Run from the repository root:

```bash
node --test tests/api-key.test.js
python3 scripts/collect_feeds.py verify
python3 scripts/prepare_pages.py
```

Inspect `dist/` and `feed-discovery-pages.zip`. The bundle contains the master, current and retained older immutable collections, `_worker.js`, `_routes.json`, `_headers`, and `404.html`. It has no `_redirects` file. The Worker rewrites an authorized `/` request to `/master.json`.

Confirm that `_routes.json` has `version: 1`, `include: ["/*"]`, and `exclude: []`. Every path passes through the Worker, including old collection paths and missing assets. `_headers` alone does not provide authentication.

Confirm that `cloudflare/_worker.js` checks the exact `X-API-Key` value against `env.FEED_DISCOVERY_API_KEY` before calling `env.ASSETS.fetch`. Missing bindings, missing keys, and wrong keys return 401. Authorized responses preserve asset ETags, use private cache headers, vary by `X-API-Key`, and disable shared CDN caching.

This endpoint targets native clients. Unauthenticated OPTIONS requests return 401. Browser cross-origin preflight is not supported by this policy. Do not add an OPTIONS authentication bypass during publication.

Retain older immutable collections for clients with cached masters. Do not edit hash-addressed documents or replace stable feed identities while preparing a deployment.

## Deploy the verified folder

Once the target, secret, runtime setting, and local checks are verified, run the authorized production deployment:

```bash
npx wrangler@4 pages deploy dist --project-name <verified-project-name> --branch main
```

Use a separately authorized preview branch when publishing a preview. Configure that environment's secret first.

For a Direct Upload project, dashboard upload of the generated folder or ZIP is also supported. Keep `_worker.js` at the bundle root. Dashboard drag-and-drop does not compile a `functions` directory. Wrangler uploads the folder, not the ZIP.

## Verify the live deployment

Use the returned deployment URL and the production alias or custom domain. Supply the valid key through secure input and print only test outcomes. Avoid verbose request traces and captured request headers.

Check these behaviors:

- Missing and wrong keys return 401 for GET and HEAD on `/`, `/master.json`, a current collection, a retained old collection, and a missing path.
- Unauthenticated OPTIONS returns 401.
- A valid key returns master JSON with status 200 and the expected catalog ID and publication number.
- A valid key with the returned `If-None-Match` ETag returns 304. Missing or wrong keys with that same ETag still return 401.
- A valid key fetches a referenced collection. Its body hash matches the master's SHA-256. HEAD preserves the authenticated response metadata without a body.
- Authorized responses have private cache headers and `Vary: X-API-Key`. Unauthorized responses use `Cache-Control: no-store`.
- The deployment URL and the intended alias both enforce authentication.

Do not declare publication verified from a successful upload alone. If a live check fails, report the deployment and the failing check. Do not roll back to an older unauthenticated deployment.

List earlier deployment URLs and branch aliases. A new authenticated deployment does not retrofit authentication into older public deployments. Report any older URL that still serves the catalog publicly. Obtain explicit approval before deleting or otherwise disabling those deployments.

Record the deployment ID, verified URLs, publication number, bundle hash, and test outcomes in `audit/decisions.tsv`. Record secret presence only. Report historical public exposure separately from the new deployment's authentication.

## Official references

Recheck these when platform behavior or CLI options differ:

- [Pages Direct Upload](https://developers.cloudflare.com/pages/get-started/direct-upload/).
- [Pages advanced mode and asset forwarding](https://developers.cloudflare.com/pages/functions/advanced-mode/).
- [Pages encrypted secrets](https://developers.cloudflare.com/pages/functions/bindings/#secrets).
- [Pages fail-open and fail-closed settings](https://developers.cloudflare.com/pages/functions/routing/#fail-open--closed).
- [Wrangler Pages commands](https://developers.cloudflare.com/workers/wrangler/commands/pages/).
