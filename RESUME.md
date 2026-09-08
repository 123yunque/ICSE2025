# 本批运行状态

截至 2026-09-08，`runs/fresh100_20260906` 已完整结束，流水线状态为 `complete=true`：

- Control-Flow：351/351；
- Oracle-State：1,976/1,976；
- Predicted-State：1,793/1,793；
- Repeat-State：40/40。

运行跨越三个通过格式门槛的独立服务恢复检查。恢复期间未更换模型、提示、冻结请求或评分规则；
恢复入口只补齐缺失 request ID，已有首答未重发。历史额度停止仍保存在
`reports/fresh100_20260906/STOP_20260907.json`，最终服务阶段证书和各条件响应哈希见
`reports/fresh100_20260906/service_epochs.json`。

最终结果见 `reports/fresh100_20260906/REPORT.md` 和 `summary.json`。原始请求、首答、请求指纹、
Oracle、官方测试判定及 usage 记录保存在 `runs/fresh100_20260906/`。API 凭据仅从
`YUNWU_API_KEY` 环境变量读取，没有写入产物。

重新生成离线报告不调用 API：

```powershell
conda run -n Npflower --no-capture-output python lcb_analyze.py --run runs/fresh100_20260906
```

新的独立复现实验必须使用新的 run 目录，不能覆盖本次首答。
