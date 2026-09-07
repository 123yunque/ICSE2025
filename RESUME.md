# 本批停点与恢复

2026-09-07 续跑入口：先运行 `recovery_check.py` 完成独立合成程序格式验证；
仅在保存通过的 certificate 后运行 `resume_experiment.py`。该入口核验原执行代码哈希，
保留全部已有主实验首答，追加调用有独立阶段标识，并在额度/认证拒绝时停止派发网络请求。
下文保留 2026-09-06 的历史停点，不能据此认定当前额度仍然不足。

2026-09-07 本次恢复后，CF 已完整收到 351/351；Oracle-State 收到 119/1,976 后，
服务再次返回 401“该令牌额度已用尽”，保护电路阻止了其余请求。补充额度后应新建恢复检查目录：

```powershell
conda run -n Npflower --no-capture-output python recovery_check.py --run runs/recovery_check_<新名称>
conda run -n Npflower --no-capture-output python resume_experiment.py --recovery runs/recovery_check_<新名称>
```

不要复用旧 certificate 来证明下一次服务恢复。主实验运行器仍仅补齐缺失 ID；已有 351 个 CF 与
119 个 Oracle-State 首答不重发。电路拦截生成的 `api_error` 是派发审计记录，不是模型回答。

本次 100 题构建和 Oracle 已完成；完整三条件推理未完成。

1. 59 题的本次新解答通过全部官方测试；40 题没有合格解；1 题确认官方测试违反约束。
2. 过滤后保留 49 题、351 个 CF 案例、1,976 个状态变量请求、318 个状态案例。
3. CF 试跑收到 40 个首答，9 个格式有效，未达 95% 门槛。多数无效首答输出代码或函数答案。
4. 随后的四个独立合成诊断请求均返回 HTTP 401“该令牌额度已用尽”，尚不能判断角色消息/JSON 模式问题的根因。

当前服务是配置文件中的 OpenAI 兼容服务，API 额度与 Codex/ChatGPT 任务额度不同。
需要在该服务补充当前令牌额度或在本机安全更新授权凭据；不要把密钥写入报告或聊天记录。

额度恢复后先执行：

```powershell
conda run -n Npflower --no-capture-output python diagnose_transport.py --retry-errors
```

该命令只重试未收到回答的合成诊断，旧错误记录归档。它不会重写主实验首答。
不要直接跳过工程门槛调用全部推理请求。先核对消息传递与协议遵循，再决定是否需要新的协议/运行版本。
如果修改了模型可见消息或生成配置，必须另建 run 并记录版本，不能覆盖当前首答后宣称同一次实验。
原 40 个 CF 首答和所有生成候选继续保留为工程试跑证据。

运行源文件哈希见 `runs/fresh100_20260906/execution_source.json`，
数据和候选生成指纹见 `config.json`、`cohort.json` 及每题 `candidate_*/request.json`。
当前状态与报告见 `reports/fresh100_20260906/STOP.json` 和 `REPORT.md`。
