export default {
  async fetch(request, env) {
    const suppliedKey = request.headers.get("X-API-Key");
    if (!env.FEED_DISCOVERY_API_KEY || !suppliedKey || suppliedKey !== env.FEED_DISCOVERY_API_KEY) {
      return Response.json({ error: "Unauthorized" }, {
        status: 401,
        headers: { "Cache-Control": "no-store" },
      });
    }

    const url = new URL(request.url);
    if (url.pathname === "/") {
      url.pathname = "/master.json";
    }
    const asset = await env.ASSETS.fetch(new Request(url, request));
    const response = new Response(asset.body, asset);
    const collection = /^\/collections\/[^/]+\/[0-9a-f]{64}\.json$/.test(url.pathname);
    const cacheControl = url.pathname === "/master.json"
      ? "private, max-age=0, must-revalidate"
      : collection && (asset.status === 200 || asset.status === 304)
        ? "private, max-age=31536000, immutable"
        : "private, no-store";
    response.headers.set("Cache-Control", cacheControl);
    response.headers.set("CDN-Cache-Control", "no-store");
    response.headers.set("Cloudflare-CDN-Cache-Control", "no-store");
    response.headers.append("Vary", "X-API-Key");
    response.headers.set("Access-Control-Allow-Origin", "*");
    response.headers.set("Access-Control-Expose-Headers", "ETag");
    return response;
  },
};
