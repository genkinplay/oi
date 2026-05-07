# OI Monitor on AWS Lambda

完整的 OI 异动监控部署到 AWS Lambda（Python 3.12），新加坡 region 实测对币安/coinank/DeepSeek 全通。

## 资源清单（SAM 自动创建）

| 资源 | 名称 | 说明 |
|---|---|---|
| Lambda | `oi-monitor` | 主函数，每 2 分钟跑一次 |
| DynamoDB | `oi-monitor-dedup` | dedup 状态，TTL 24h 自动清理 |
| EventBridge | `oi-monitor-schedule` | rate(2 minutes) 触发器 |
| CloudWatch Logs | `/aws/lambda/oi-monitor` | 日志保留 14 天 |
| IAM Role | `OIMonitorFunctionRole` | Lambda → DynamoDB 读写权限 |

## 一次性准备

```bash
# 装 SAM CLI（如果还没装）
brew install aws-sam-cli

# 确认 region 与之前测试 fapi-probe 时用的一致（新加坡）
aws configure get region    # 应返回 ap-southeast-1
# 不一致就改：
aws configure set region ap-southeast-1
```

## 部署

在 oi-monitor 仓库根目录：

```bash
# 第一次部署：交互式向导，会问你 secret 值并写入 samconfig.toml
make aws-deploy-guided

# 之后增量更新：
make redeploy
```

`--guided` 会逐项问参数。**所有 [...]内是默认值的题，直接 Enter 接受即可**（不要输入 y/n）：

```
Stack Name [oi-monitor]:                       <Enter>
AWS Region [ap-southeast-1]:                   <Enter>
Parameter ScheduleExpression [rate(2 minutes)]: <Enter>
Parameter ThresholdPct [10]:                   <Enter>
Parameter ThresholdPct15m [15]:                <Enter>
Parameter TopN [50]:                           <Enter>
Parameter DedupWindowSec [1800]:               <Enter>
Parameter DeepSeekModel [deepseek-v4-pro]:     <Enter>
Parameter DelistingListUrl [https://...]:      <Enter>
Parameter DeepSeekApiKey []:                   sk-xxxxx        ← 输入会回显
Parameter FeishuWebhookUrls []:                <留空 Enter 或 飞书 URL>
Parameter DingtalkWebhookUrls []:              https://oapi.dingtalk.com/robot/send?access_token=cb87...
Parameter WebhookUrl []:                       <留空 Enter 或 旧版通用 URL>
Confirm changes before deploy [y/N]:           N    ← 这种 [Y/N] 才输 y/n
Allow SAM CLI IAM role creation [Y/n]:         Y    ← 让 SAM 建 IAM role
Disable rollback [y/N]:                        N
Save arguments to configuration file [Y/n]:    Y
SAM configuration file [samconfig.toml]:       <Enter>   ← 别输 y！直接 Enter
SAM configuration environment [default]:       <Enter>
```

**注意**：`[Y/n]`、`[y/N]` 这种带斜杠的是 yes/no 问题，输 y 或 n；
其它所有 `[默认值]` 直接 Enter 表示接受。

第一次部署完，参数会保存到 `samconfig.toml`，之后 `sam deploy` 直接复用，不再问。

> 关于 secret 输入：SAM template 里没设 `NoEcho`，所以 DeepSeek key、webhook URL 等 **输入时会回显字符**（方便检查粘贴是否正确）。secret 仍然加密保存在 Lambda 环境变量里，CloudFormation Console 也能看到——这是个人项目可接受的取舍。

## 验证 / 监控 / 调试

```bash
# 立即手动触发一次（不等 cron）
make aws-invoke

# 实时尾随日志
make aws-logs

# 列出 stack 的所有资源
make aws-status

# 删除整个 stack（连同 Lambda、DynamoDB、IAM 全部清理）
make aws-destroy
```

## 改参数 / 改 secret

```bash
# 重新部署，向导问到时改值
make aws-deploy-guided
```

或者直接改 `samconfig.toml` 的 `parameter_overrides` 然后 `make redeploy`。

## 成本估算（免费层内）

- **Lambda**：1M 请求/月 + 400,000 GB-秒 永久免费
  - 每 2 分钟 = 21,600 次/月 × 256MB × 30s ≈ 165,000 GB-秒，远低于 400k
- **DynamoDB**：25GB 存储 + 200 万 WCU + 100 万 RCU 永久免费
  - 每次 ~3 写 + 5 读，21,600 次/月 = 65k 写 + 108k 读，远低于额度
- **EventBridge**：rule 触发 100% 免费
- **CloudWatch Logs**：5GB 摄入 + 5GB 存储免费
  - 我们日志小（每次 ~2KB），完全够用

**预期月费 = $0**

## 跟其他部署方式的关系

| 方式 | 状态 |
|---|---|
| **AWS Lambda（本目录）** | **新加坡 region 实测全通，推荐主用** |
| GitHub Actions（oi-monitor 仓库） | runner IP 被币安 451，需要 CF 代理 |
| Cloudflare Workers（cloudflare-oi/） | CF 出站对币安部分屏蔽，AI 不稳定 |
| 自托管 Go assistant | 需要服务器 |

## 代码组织

```
aws-lambda/
├── template.yaml         # SAM 模板
├── samconfig.toml        # 部署参数
├── README.md
└── app/
    ├── lambda_function.py    # Lambda 入口
    ├── oi_monitor.py         # 主流程（dedup 用 DynamoDB）
    ├── coinank.py            # 双 sortBy + 字段合并
    ├── binance_market.py     # 币安 fapi 接口（embed 兜底）
    ├── binance_symbols.json
    ├── ai_analyzer.py        # DeepSeek 调用
    ├── notifier.py           # 飞书/钉钉/通用 webhook
    ├── indicators.py         # SMA/EMA/MACD/ATR
    ├── delisting.py          # 远端下架清单
    ├── dedup.py              # DynamoDB dedup
    └── requirements.txt      # 仅 requests（boto3 Lambda 自带）
```
