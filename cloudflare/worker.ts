/**
 * 币安 fapi 透明代理 (Cloudflare Worker)
 *
 * 用途：让 GitHub Actions runner 通过 CF 出口 IP 访问币安合约 API，
 *      绕过 fapi.binance.com 对部分云厂商 IP 段的 451 屏蔽。
 *
 * 端点：
 *   GET /                        健康检查（不需 secret）
 *   GET /fapi/v1/...             透传到 fapi.binance.com（需要 secret）
 *   GET /futures/data/...        同上
 *
 * 认证：调用方必须带 `X-Proxy-Secret: <env.PROXY_SECRET>`，否则 401。
 *      用 `npx wrangler secret put PROXY_SECRET` 配置。
 */

const UPSTREAM = "https://fapi.binance.com";

// 严格白名单，避免 worker 被滥用为通用代理
const ALLOWED_PREFIXES = ["/fapi/", "/futures/data/"] as const;

interface Env {
  PROXY_SECRET?: string;
}

export default {
  async fetch(req: Request, env: Env): Promise<Response> {
    const url = new URL(req.url);

    // 健康检查 / 根路径
    if (url.pathname === "/" || url.pathname === "/healthz") {
      return new Response("ok\n", {
        status: 200,
        headers: { "content-type": "text/plain" },
      });
    }

    // 路径白名单
    if (!ALLOWED_PREFIXES.some((p) => url.pathname.startsWith(p))) {
      return new Response("forbidden path\n", { status: 403 });
    }

    // Secret 认证（仅当 env 配置了 PROXY_SECRET 时校验）
    if (env.PROXY_SECRET) {
      const got = req.headers.get("x-proxy-secret");
      if (got !== env.PROXY_SECRET) {
        return new Response("unauthorized\n", { status: 401 });
      }
    }

    // 透传到 fapi
    const upstreamUrl = UPSTREAM + url.pathname + url.search;
    const init: RequestInit = {
      method: req.method,
      headers: {
        "user-agent":
          "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) " +
          "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
        accept: "application/json",
      },
    };
    if (req.method !== "GET" && req.method !== "HEAD") {
      init.body = await req.arrayBuffer();
    }

    try {
      const upstream = await fetch(upstreamUrl, init);
      // 透传 body + status，剥离敏感头
      const body = await upstream.arrayBuffer();
      const headers = new Headers();
      headers.set(
        "content-type",
        upstream.headers.get("content-type") ?? "application/json",
      );
      headers.set("x-upstream-status", String(upstream.status));
      return new Response(body, { status: upstream.status, headers });
    } catch (err) {
      return new Response(`upstream error: ${err}\n`, {
        status: 502,
        headers: { "content-type": "text/plain" },
      });
    }
  },
};
