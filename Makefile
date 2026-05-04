PY     = uv run python
PROBE  = scripts/probe.py
ZIP    = probe-lambda.zip
SAM_DIR = aws-lambda

.PHONY: probe probe-zip oi run-bian \
        aws-deploy aws-deploy-guided aws-invoke aws-invoke-summary aws-logs aws-status aws-destroy

# 本地跑探测脚本，看本机出站对项目所有外部地址的可达性
probe:
	$(PY) $(PROBE)

# 打包 probe.py 成 zip，可直接上传到 AWS Lambda（handler: probe.lambda_handler）
probe-zip:
	@rm -f $(ZIP)
	cd scripts && zip -j ../$(ZIP) probe.py >/dev/null
	@echo "✅ 已打包 $(ZIP)"
	@echo "   AWS Lambda handler 入口：probe.lambda_handler"
	@echo "   上传方式：aws lambda update-function-code --function-name fapi-probe --zip-file fileb://$(ZIP)"
	@echo "   或在 console 直接拖拽 $(ZIP) 到 Lambda function code 区域"

# 跑主流程（OI 监控本身）
oi:
	$(PY) scripts/oi_monitor.py

# 跑下架公告监控
run-bian:
	$(PY) scripts/bian.py

# --- AWS Lambda (SAM) ---

# 第一次部署：交互式向导，写入 samconfig.toml
aws-deploy-guided:
	cd $(SAM_DIR) && sam build && sam deploy --guided

# 增量部署
aws-deploy:
	cd $(SAM_DIR) && sam build && sam deploy

# 立即手动触发一次主流程（不等 cron）
aws-invoke:
	aws lambda invoke \
	  --function-name oi-monitor \
	  --payload '{}' \
	  --cli-binary-format raw-in-base64-out \
	  /tmp/oi-monitor-out.json && \
	  cat /tmp/oi-monitor-out.json && echo

# 立即手动触发一次 24h 复盘统计
aws-invoke-summary:
	aws lambda invoke \
	  --function-name oi-monitor \
	  --payload '{"action":"summary"}' \
	  --cli-binary-format raw-in-base64-out \
	  /tmp/oi-monitor-summary.json && \
	  cat /tmp/oi-monitor-summary.json && echo

# 实时尾随 CloudWatch Logs
aws-logs:
	cd $(SAM_DIR) && sam logs --stack-name oi-monitor --tail

# 查看 stack 的资源
aws-status:
	aws cloudformation describe-stack-resources --stack-name oi-monitor \
	  --query 'StackResources[].{Type:ResourceType,Name:LogicalResourceId,Status:ResourceStatus}' \
	  --output table

# 删除整个 stack
aws-destroy:
	cd $(SAM_DIR) && sam delete --stack-name oi-monitor
