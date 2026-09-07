# 本批停点与恢复

截至 2026-09-07，本次 100 题实验因 OpenAI 兼容 API 额度不足停在 `predicted_state`：

- Control-Flow：351/351 已收到；320 个格式有效，31 个格式无效首答按固定分母计失败。
- Oracle-State：1,976/1,976 已收到；1,974 个格式有效，状态 exact 为 1,856/1,976（93.93%）。
- Predicted-State：可构造 1,793 个请求，已收到 356 个；355 个格式有效，1 个格式无效。
- Repeat-State：0/40。

最后一个真实服务错误为 HTTP 403 `local:pre_consume_token_quota_failed`。当时服务报告余额
约 US$0.009354，而下一次请求预扣需要约 US$0.009626。保护电路随后阻止 1,435 个排队请求
实际派发；这些 `not_dispatched_circuit_open` 记录不是模型调用，也没有回答。

补充额度后必须新建独立恢复检查目录，再接续主实验：

```powershell
conda run -n Npflower --no-capture-output python recovery_check.py --run runs/recovery_check_<新名称>
conda run -n Npflower --no-capture-output python resume_experiment.py --recovery runs/recovery_check_<新名称>
```

不要复用旧 certificate 来证明下一次服务恢复。恢复入口核验冻结执行代码哈希，只补齐缺失
request ID；已有 351 个 Control-Flow、1,976 个 Oracle-State 和 356 个 Predicted-State 首答不重发。
不要同时启动两个同一阶段的运行器。

当前报告见 `reports/fresh100_20260906/REPORT.md`；停止审计见 `STOP_20260907.json`；两次恢复
证书与各条件响应哈希见 `service_epochs.json`。原始请求、首答、请求指纹和 usage 记录保存在
`runs/fresh100_20260906/`。

服务 API 额度与 Codex/ChatGPT 任务额度相互独立。凭据仅从 `YUNWU_API_KEY` 环境变量读取，
不要写入报告或聊天记录。若模型可见消息或生成配置发生变化，必须另建 run，不能覆盖当前首答。
