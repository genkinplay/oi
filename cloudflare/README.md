# Binance Proxy on Cloudflare Worker

把币安 `fapi.binance.com` 透传一层 CF Worker，让 GitHub Actions runner 通过 CF 出口 IP 访问，绕开 451 地域屏蔽。

## 部署

需要 Cloudflare 账号 + 安装过 Node.js。

```bash
cd cloudflare

# 一次性登录 Cloudflare
npx wrangler login

# 设置共享密钥（防止 Worker 被随便代理）
npx wrangler secret put PROXY_SECRET
# 提示输入时，给一个长随机字符串，例如：openssl rand -hex 32

# 部署
npx wrangler deploy
```

部署成功后会拿到形如：
```
https://oi-binance-proxy.<your-subdomain>.workers.dev
```

测试健康检查：
```bash
curl https://oi-binance-proxy.<your-subdomain>.workers.dev/
# → ok
```

测试代理（用对应 PROXY_SECRET）：
```bash
curl -H "X-Proxy-Secret: <你的密钥>" \
  "https://oi-binance-proxy.<your-subdomain>.workers.dev/fapi/v1/exchangeInfo" \
  | jq '.symbols | length'
# → 600+
```

## 在 GitHub Actions 接入

仓库 `Settings` → 配置以下两项：

**Variables** (公开，不敏感)：
| Name | Value |
|---|---|
| `BINANCE_FAPI_BASE` | `https://oi-binance-proxy.<your-subdomain>.workers.dev` |

**Secrets** (敏感)：
| Name | Value |
|---|---|
| `BINANCE_PROXY_SECRET` | 部署时设的 PROXY_SECRET |

下次跑 OI Monitor 时，`scripts/binance_market.py` 会自动通过 worker 转发，AI 分析模块也会恢复使用。

## 路径白名单

Worker 仅放行：
- `/fapi/*`
- `/futures/data/*`

其它路径直接 403，避免被滥用为通用代理。

## 配额

- 免费 plan：100k requests/天，CPU 时间 10ms/请求
- 当前流量预估：每次 OI Monitor 跑 ~15 fetches，假设 1 分钟一次 = 21,600/天，**远低于免费额度**
- 如果要把整个 OI Monitor 也搬到 Worker，再考虑升级
