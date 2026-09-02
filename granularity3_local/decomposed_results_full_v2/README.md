# 粒度 3 解耦全量实验结果

本目录是 `codex/g3-decomposed-execution` 分支上 `g3-decomposed-v2` 协议的正式全量结果包。
实验于 2026-09-02 使用 `gpt-5.4`、`reasoning_effort=low`、`verbosity=low`、
JSON 模式和 16,384 completion-token 上限运行。

协议按 `1 + k` 拆分：每个具体输入先执行 1 个控制流问题，再对该输入中每个被跟踪变量执行
1 个独立状态问题。Oracle-CF 和 Predicted-CF 使用相同状态问题与同一运行时 Oracle，唯一差别是
`execution_trace` 分别来自本地标准轨迹和模型控制流预测。因此两路状态结果可直接测量控制流错误的
传播损失。

## 样本口径

| 项目 | 数量 |
| --- | ---: |
| 控制流任务 / 具体输入案例 | 3591 |
| 涉及的 MBPP+ task | 366 |
| 存在被跟踪变量变化的案例 | 1117 |
| 不存在被跟踪变量变化的案例 | 2473 |
| Oracle-CF 状态请求 / 被跟踪变量 | 2241 |
| 涉及状态请求的 task | 126 |
| 可构造 Predicted-CF 状态请求 | 2218 |
| 因控制流回答格式无效而无法构造 | 23 |

状态案例覆盖率为 `1117 / 3591 = 31.1055%`。其余 2473 个案例没有任何被跟踪变量发生实际
值变化，按协议只评估控制流，不制造无信息的状态问题。

正式 cohort 哈希：

- 案例 ID：`480c08c60044205e56c19335eb1074b8640af361b9013562fa4881a0d48a4ba6`
- 状态请求 ID：`8288c37b2c247a0d679a3c591756619642b2e016ee9185b6b1a3d4d097135b12`

完整分布与排除原因位于 `cohort/plan_summary.json` 和 `cohort/prepare_summary.json`。

## 主要结果

| 指标 | 结果 |
| --- | ---: |
| Control 格式有效率 | 98.9696% |
| Control canonical 轨迹精确率（全请求） | 91.6736% |
| Control 展开轨迹精确率（全请求） | 95.0710% |
| Oracle-CF 状态变量精确率（2241 个请求） | 73.8956% |
| Oracle-CF 案例全部变量精确率（1117 个案例） | 59.6240% |
| Predicted-CF 状态变量精确率（按全部 2241 个状态请求计） | 70.8612% |
| Predicted-CF 案例全部变量精确率（按全部 1117 个案例计） | 57.6544% |
| 端到端变量联合精确率 | 59.5716% |
| 端到端案例全部变量联合精确率 | 54.7896% |
| Oracle-CF 到 Predicted-CF 的状态传播损失 | 3.0344 个百分点 |

Predicted-State API 实际执行的 2218 个请求中，状态精确率为 71.5960%；联合报告把因无效控制流
而未能构造的 23 个状态请求按失败计入，因此全口径结果为 70.8612%。模型控制流正确时，
Predicted-CF 状态精确率为 75.4237%；控制流错误时为 53.7155%。这说明控制流错误会显著降低
后续状态推理，但状态模型有时仍能在错误轨迹条件下得到正确状态序列。

质量门禁已通过：Control、Oracle-State、Predicted-State 的最终响应完整率均为 100%，格式有效率
均高于 95%，所有收到的 API 回答 `finish_reason` 都是 `stop`。详见
`combined/quality_gate.json`。

## 文件说明

`control/`、`oracle_state/` 和 `predicted_state/` 各自保留：

- `run_config.json`：模型、API、JSON 模式、token 上限、并发和选择配置；
- `requests.jsonl`：实际发送给模型的请求；
- `oracles.jsonl`：本地执行器产生的标准执行结果；
- `received_attempts.jsonl`：每个请求最终收到的原始模型回答、校验状态、耗时和 token 用量；
- `predictions.jsonl`：从有效回答解析出的模型预测；
- `scores.jsonl`：逐请求比较结果；
- `case_scores.jsonl`、`task_scores.jsonl`、`length_bin_scores.jsonl`：相应聚合明细；
- `response_errors.jsonl`：格式无效回答及原因；
- `summary.json`：该阶段最终汇总。

`combined/` 保存三路联合评分、逐案例/逐 task 聚合、最终汇总和质量门禁。
`legacy_compatibility/` 保存与旧联合实验可兼容子集的比较。旧结果只有
`1232 / 2241 = 54.9755%` 的状态变量与新语句级协议兼容，因此不能把旧子集准确率直接当作
新全量协议的基线。

API 不提供模型隐藏思维链，因此这里的“推理结果”是模型最终回答；
`received_attempts.jsonl` 另有每条请求的 `reasoning_tokens` 计数。历史额度、连接和超时错误没有
放入正式结果包，只保留最终收到的回答。运行时采用“首个收到的回答即计分”：格式无效的首答不会
重试选优，API 未收到响应的请求才会断点补跑。

## 比较逻辑与复现入口

控制流和状态的严格比较逻辑在 `../decomposed_core.py` 的 `score_control_flow()`、
`score_state()`；批量评估和三路联合报告在 `../decomposed_evaluate.py` 的
`evaluate_response_records()`、`build_combined_report()`；门禁在 `../decomposed_gate.py`。

生成联合报告：

```powershell
python -m granularity3_local.decomposed_evaluate report `
  --control-requests granularity3_local/decomposed_results_full_v2/control/requests.jsonl `
  --control-scores granularity3_local/decomposed_results_full_v2/control/scores.jsonl `
  --oracle-state-requests granularity3_local/decomposed_results_full_v2/oracle_state/requests.jsonl `
  --oracle-state-scores granularity3_local/decomposed_results_full_v2/oracle_state/scores.jsonl `
  --predicted-state-requests granularity3_local/decomposed_results_full_v2/predicted_state/requests.jsonl `
  --predicted-state-scores granularity3_local/decomposed_results_full_v2/predicted_state/scores.jsonl `
  --output-dir granularity3_local/decomposed_results_full_v2/combined_reproduced
```

更完整的准备、API 执行、断点恢复与双控制流实验命令见 `../DECOMPOSED_EXECUTION.md`。

## 运行事件说明

Control 与 Oracle-State 首次并行运行时曾遇到额度耗尽，历史 `summary.json` 中的
`api_error_count` 因此分别累计为 2535 和 1705；Predicted-State 历史 API 错误为 19。
这些数字是所有失败尝试的历史计数，不是最终缺失数。充值后按固定请求 ID 断点恢复，三路最终均
达到响应完整率 100%。正式归档只保留每个请求最终收到的一条回答，避免把临时额度错误和重复日志
混入实验数据。
