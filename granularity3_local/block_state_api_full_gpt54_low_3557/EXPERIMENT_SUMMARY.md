# 粒度 3 Block/State 全量实验总结

## 结论

正式全量实验已完成，3557 个输入均收到模型响应，最终 gate 的五项标准全部通过。实验结果可以进入后续统计分析，不需要重新请求整个 task 集合。

## 正式配置

- API：`https://api.openlux.ai/v1`
- 模型：`gpt-5.4`
- 请求粒度：每个 task 的每个输入单独请求
- 输出格式：`flat_runs`
- `reasoning_effort`：`low`
- `verbosity`：`low`
- 并发数：3
- 普通/复杂输入超时：180/300 秒
- 最大 completion token：8192
- 自动重试次数：0；仅对传输失败使用断点续跑补齐
- 正式运行配置指纹：`2f5bc8281cfa248fa0e3d9e17b393bbd01d2e05ab31e3d869fb6151ecbf96e59`

全部 3558 条 API 尝试记录只有一种生成配置，没有混入 baseline、`reasoning_effort=none` 或 canary 配置。

## 完整性与重试

| 项目 | 结果 |
|---|---:|
| 计划输入 | 3557 |
| 唯一请求键 | 3557 |
| 收到响应 | 3557 |
| 唯一响应键 | 3557 |
| 缺失/多余响应 | 0/0 |
| API 尝试 | 3558 |
| 历史 API 错误 | 1 |
| 最终未解决 API 错误 | 0 |

`task_227/input_9` 第一次请求发生 `RemoteDisconnected`，随后以相同配置断点补跑成功。结构无效的模型响应未重抽样，避免改变“一输入对应一个模型结果”的实验口径。

## 最终 gate

| 指标 | 全量结果 | 门槛 | 状态 |
|---|---:|---:|---:|
| 格式有效率 | 3471/3557 = 97.58% | >= 95% | PASS |
| Expanded Block exact | 3357/3557 = 94.38% | >= 72.5% | PASS |
| 响应 P90 | 11.20 秒 | <= 60 秒 | PASS |
| 历史 API 错误率 | 1/3557 = 0.03% | <= 2% | PASS |
| 8192-token cap 命中率 | 1/3557 = 0.03% | <= 1% | PASS |

唯一按保守规则计为 token cap 命中的输入是 `task_770/input_7`：服务端报告 8579 completion tokens，但 `finish_reason=stop`，不是 `length` 截断。不存在大规模触发 token 上限的情况。

## 全量准确率

以下比例统一以全部 3557 个输入为分母，格式错误也计为失败：

| 指标 | 正确数 | 准确率 |
|---|---:|---:|
| Expanded Block exact | 3357 | 94.38% |
| Canonical Block exact | 3303 | 92.86% |
| Variable changes exact | 2912 | 81.87% |
| Expanded Block + changes joint exact | 2898 | 81.47% |
| Canonical Block + changes joint exact | 2883 | 81.05% |

## 无效响应

共有 86/3557（2.42%）条模型响应未进入评分：

| 类型 | 数量 |
|---|---:|
| 非法 JSON | 48 |
| 变量变化 step 超出 Block trace | 16 |
| schema/type 错误 | 15 |
| 同一步同一变量重复变化 | 7 |

这些记录保存在 `evaluation/response_errors.jsonl`，应作为格式失败参与最终统计，不建议再次请求后替换原始结果。

## Token 与时延

| 项目 | 结果 |
|---|---:|
| Prompt tokens | 2,574,626 |
| Completion tokens | 855,425 |
| 其中 reasoning tokens | 664,041 |
| Total tokens | 3,430,051 |
| 响应 P50 | 4.56 秒 |
| 响应 P90 | 11.20 秒 |
| 最大响应时间 | 178.16 秒 |

## 关键文件

- `selected_model_batches.jsonl`：3557 个实际请求
- `model_responses.jsonl`：3557 个原始模型响应
- `api_attempts.jsonl`：3558 次请求尝试、时延和 token 记录
- `evaluation/case_scores.jsonl`：逐输入 Oracle 对比结果
- `evaluation/response_errors.jsonl`：86 条无效响应及原因
- `evaluation/summary.json`：评估汇总
- `final_gate/gate.json`：机器可读 gate 结果
- `final_gate/FINAL_REPORT.md`：五项门槛报告
- `RUN_ARTIFACT_GUIDE.md`：代码文件、运行产物、文件含义和复现命令
- `CASE_STUDY.md`：正确案例与长循环格式错误案例的端到端分析
