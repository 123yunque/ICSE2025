# 粒度 3 解耦实验：5-task 保留结果

本目录只保留 5 个任务试运行中不可替代或对复现实验必要的产物。任务为
`task_3`、`task_4`、`task_9`、`task_11`、`task_12`，模型为 `gpt-5.4`，
`reasoning_effort=low`，`verbosity=low`。

## 最终结果

| 阶段 | 请求数 | 最终结果 |
| --- | ---: | ---: |
| 控制流 | 5 | 展开轨迹 5/5；canonical 轨迹 3/5 |
| Oracle-CF 状态 | 7 | 精确状态序列 7/7 |
| Predicted-CF 状态 | 7 | 精确状态序列 7/7 |
| 端到端联合 | 5 | 5/5 |

状态结果使用语句级 Oracle。它修正了旧基本块级 Oracle 对 `task_11/input_1`
中间状态 `helo` 的遗漏；旧的 6/7 评估不属于最终结果，未予保留。

## 文件说明

`control_flow/`、`oracle_state/` 和 `predicted_state/` 各自保留：

- `run_config.json`：模型、API、采样及任务选择配置；
- `requests.jsonl`：实际发送给模型的请求；
- `predictions.jsonl`：从原始响应解析、校验后的模型推理结果；
- `oracles.jsonl`：最终语句级 Oracle；
- `api_attempts.jsonl`：原始模型最终回答、校验状态及 reasoning/token 用量；
- `scores.jsonl`：逐请求最终评分；
- `summary.json`：该阶段最终汇总。

`combined/` 保留逐案例联合评分及最终联合汇总。

实际使用的比较逻辑位于 `decomposed_core.py` 的 `score_control_flow()`、
`score_state()`，以及 `decomposed_evaluate.py` 的
`evaluate_response_records()`、`build_combined_report()`。

API 不返回模型的隐藏思维链，因此“推理结果”指模型最终输出；
`api_attempts.jsonl` 中额外保留了每次调用的 `reasoning_tokens` 计数。

以下内容没有保留：可由准备脚本重新生成的 pilot 缓存、与
`api_attempts.jsonl` 重复的 `model_responses.jsonl`、空错误文件，以及修正
Oracle 之前的旧评分。虽然 `predictions.jsonl` 可由原始响应重新生成，但为便于
直接检查和比较，仍按用户要求保留。
