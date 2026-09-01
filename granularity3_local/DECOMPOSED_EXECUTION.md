# 粒度 3：控制流与变量状态解耦实验

## 1. 实验目标

旧版粒度 3 要求模型一次性返回：

```text
block_trace + changes
```

其中 `changes` 还要求模型处理运行段索引、变量名、前后值、排序，以及与压缩 Trace 的对齐。旧版因此同时测量程序执行推理和输出协议遵循能力。

解耦版把一个程序输入拆成：

```text
1 个 Control-Flow Task
+
k 个 Variable-State Tasks
```

主实验把输出任务解耦，并回答两个条件化问题：

1. 模型能否预测程序实际经过的 Block？
2. 在真实执行路径已经给定时，模型能否维护一个目标变量的状态？

旧版联合实验保持不变，作为历史 `Joint Baseline`。由于旧版把同一 run
内的多次赋值压成首尾值，它只能在旧 run-level Oracle 与新语句级 Oracle
完全一致的变量子集上进行公平状态比较；不能直接把旧 `changes_exact`
与新 `state_exact` 当作同一指标。

## 2. 三种实验条件

### 2.1 Control-Flow Task

模型输入：

```text
Function Signature + Arguments + Block Definitions + Static Context
```

模型只返回：

```json
{
  "trace": [
    ["B001", 1],
    ["B002>B003", 3],
    ["B002", 1],
    ["B004", 1]
  ]
}
```

评估同时报告：

- `canonical_trace_exact`：压缩表示完全相同；
- `expanded_trace_exact`：展开后的真实 Block 序列相同。

主控制流指标使用 `expanded_trace_exact`，避免不同但等价的压缩方式造成误判。

### 2.2 Oracle-CF State Task

模型输入：

```text
Function Signature
+ Arguments
+ Block Definitions
+ Oracle Execution Trace
+ Target Variable
```

每个变量单独构造请求。模型只返回：

```json
{
  "states": [
    {"$u": 1},
    1,
    2,
    3
  ]
}
```

`states` 的定义是：

```text
函数入口状态
+
变量每次真正变化后的新状态
```

只读取变量或赋相同值时不重复记录。模型不再返回 Block、step、变量名或 `before/after` 对。

旧版 `oracle/events.jsonl` 是 Basic Block 级事件；一个 Block 内可能包含多条赋值，因此仍可能压缩块内变化。解耦版会对原始函数额外执行一次语句级状态插桩，从函数入口开始，在每条语句后记录状态，并验证插桩执行结果与原始运行 Oracle 完全相同。状态答案不从旧版压缩 `changes` 反推，因此循环内部和同一 Block 内的每次真实语句级变化都会保留。

### 2.3 Predicted-CF State Task

该条件是可选端到端实验：

```text
Control-Flow 模型响应
        ↓
Predicted Trace
        ↓
同一 Variable-State Task
```

它与 Oracle-CF State 使用相同状态答案，唯一变化是输入 Trace 来自模型预测。
Trace 来源仅保存在模型不可见的请求元数据中；模型输入不包含
`trace_source=oracle/predicted`，避免条件标签成为混淆变量。

核心比较：

```text
State@OracleCF - State@PredictedCF
```

该差值报告控制流错误向状态推理传播的程度。

## 3. 代码结构

```text
decomposed_core.py       协议、提示词、状态投影、响应校验、单项评分
decomposed_statement.py  语句级状态插桩与语义保持验证
decomposed_prepare.py    生成 CF、Oracle-CF State、Predicted-CF State 数据
decomposed_evaluate.py   分项评估和三条件联合报告
decomposed_api.py        可恢复、可并发的 Chat Completions API runner
decomposed_plan.py       冻结全量 cohort、审计规模并选择分层 canary
decomposed_gate.py       canary 与全量阶段质量门禁
tests/test_decomposed.py 解耦协议和端到端本地测试
```

旧版 `block_state_*` 文件及正式结果不被修改。

## 4. 生成 5-task 本地 Pilot

以下命令从现有本地动态 Oracle 选取 5 个具有真实变量变化的 task，每个 task 选择一个输入：

```powershell
conda run -n Npflower python -m granularity3_local.decomposed_prepare prepare `
  --local-output-root granularity3_local\block_state_mbppplus_full `
  --output-dir granularity3_local\decomposed_pilot_5tasks `
  --task-limit 5 `
  --inputs-per-task 1 `
  --max-events 500 `
  --max-statement-events 2000 `
  --max-state-items 500 `
  --require-state-change
```

`--require-state-change` 仅用于保证小规模 pilot 能覆盖 State Task。正式全量准备时应去掉该参数。

输出：

```text
decomposed_pilot_5tasks/
├── control_flow/
│   ├── requests.jsonl
│   ├── oracles.jsonl
│   └── oracle_responses.jsonl
├── oracle_state/
│   ├── requests.jsonl
│   ├── oracles.jsonl
│   └── oracle_responses.jsonl
├── case_manifest.jsonl
├── excluded.jsonl
└── summary.json
```

## 5. 本地确定性自检

Oracle 响应只用于验证数据、schema 和 evaluator，不代表模型结果。

### 5.1 Control Flow

```powershell
python -m granularity3_local.decomposed_evaluate evaluate `
  --requests granularity3_local\decomposed_pilot_5tasks\control_flow\requests.jsonl `
  --oracles granularity3_local\decomposed_pilot_5tasks\control_flow\oracles.jsonl `
  --responses granularity3_local\decomposed_pilot_5tasks\control_flow\oracle_responses.jsonl `
  --output-dir granularity3_local\decomposed_pilot_5tasks\control_flow\evaluation_local
```

### 5.2 Oracle-CF State

```powershell
python -m granularity3_local.decomposed_evaluate evaluate `
  --requests granularity3_local\decomposed_pilot_5tasks\oracle_state\requests.jsonl `
  --oracles granularity3_local\decomposed_pilot_5tasks\oracle_state\oracles.jsonl `
  --responses granularity3_local\decomposed_pilot_5tasks\oracle_state\oracle_responses.jsonl `
  --output-dir granularity3_local\decomposed_pilot_5tasks\oracle_state\evaluation_local
```

两项自检都应得到 `fully_valid=true` 和全请求口径准确率 `1.0`。

## 6. 真实 API 调用

环境变量沿用旧版 API runner：

```text
YUNWU_API_KEY
YUNWU_MODEL（也可以通过 --model 指定）
YUNWU_API_BASE_URL（未设置时使用现有默认地址）
```

### 6.1 5 个 Control-Flow 请求

```powershell
conda run -n Npflower python -m granularity3_local.decomposed_api `
  --kind control_flow `
  --requests granularity3_local\decomposed_pilot_5tasks\control_flow\requests.jsonl `
  --oracles granularity3_local\decomposed_pilot_5tasks\control_flow\oracles.jsonl `
  --output-dir granularity3_local\decomposed_api_control_5tasks `
  --task-limit 5 `
  --cases-per-task 1 `
  --model gpt-5.4 `
  --reasoning-effort low `
  --verbosity low `
  --max-completion-tokens 8192 `
  --retries 0 `
  --concurrency 3
```

### 6.2 Oracle-CF State 请求

```powershell
conda run -n Npflower python -m granularity3_local.decomposed_api `
  --kind oracle_state `
  --requests granularity3_local\decomposed_pilot_5tasks\oracle_state\requests.jsonl `
  --oracles granularity3_local\decomposed_pilot_5tasks\oracle_state\oracles.jsonl `
  --output-dir granularity3_local\decomposed_api_oracle_state_5tasks `
  --task-limit 5 `
  --cases-per-task 1 `
  --model gpt-5.4 `
  --reasoning-effort low `
  --verbosity low `
  --max-completion-tokens 8192 `
  --retries 0 `
  --concurrency 3
```

## 7. 构造并运行 Predicted-CF State

先把控制流 API 响应转换为 Predicted-CF State 请求：

```powershell
python -m granularity3_local.decomposed_prepare predicted-state `
  --control-requests granularity3_local\decomposed_pilot_5tasks\control_flow\requests.jsonl `
  --control-responses granularity3_local\decomposed_api_control_5tasks\model_responses.jsonl `
  --state-requests granularity3_local\decomposed_pilot_5tasks\oracle_state\requests.jsonl `
  --state-oracles granularity3_local\decomposed_pilot_5tasks\oracle_state\oracles.jsonl `
  --output-dir granularity3_local\decomposed_pilot_5tasks\predicted_state
```

然后运行：

```powershell
conda run -n Npflower python -m granularity3_local.decomposed_api `
  --kind predicted_state `
  --requests granularity3_local\decomposed_pilot_5tasks\predicted_state\requests.jsonl `
  --oracles granularity3_local\decomposed_pilot_5tasks\predicted_state\oracles.jsonl `
  --output-dir granularity3_local\decomposed_api_predicted_state_5tasks `
  --model gpt-5.4 `
  --reasoning-effort low `
  --verbosity low `
  --max-completion-tokens 8192 `
  --retries 0 `
  --concurrency 3
```

格式无效或缺失的 Control-Flow 响应不会伪造 Trace；对应状态任务会写入 `excluded.jsonl`，最终联合报告按失败处理。

## 8. 指标

Control Flow：

```text
Canonical Trace Exact
Expanded Trace Exact
Canonical Format Valid Rate
```

Variable State：

```text
State Sequence Exact
State Position Accuracy
Correct Prefix Length
```

联合分析：

```text
Control-Flow Expanded Exact Rate
Oracle-CF State Exact Rate
Predicted-CF State Exact Rate
State Error Propagation Gap
End-to-End Joint Exact Rate
State Accuracy | CF Correct
State Accuracy | CF Wrong
```

论文主结果应优先使用 `*_all_requests` 或联合报告中的全任务口径，把缺失和格式无效响应计为失败。

## 9. 方法边界

Oracle-CF State Task 测量的是：

> 在真实执行路径已经固定时，模型维护目标变量动态状态的能力。

它不是完整端到端执行能力；完整能力由 Predicted-CF State 和联合指标补充。该设计的目的正是避免把控制流失败错误归因于数据流推理。

## 10. 正式全量 v2 协议

正式运行使用 `g3-decomposed-v2`。准备命令固定以下边界：基本块事件最多
500、语句事件最多 2000、单变量状态项最多 500、单变量 Oracle JSON 最多
16000 字符。最后一项防止超大容器值使模型输出超过 completion 上限。

```powershell
conda run -n Npflower python -m granularity3_local.decomposed_prepare prepare `
  --local-output-root granularity3_local\block_state_mbppplus_full `
  --output-dir granularity3_local\decomposed_full_v2_16k_prepared `
  --max-events 500 `
  --max-statement-events 2000 `
  --max-state-items 500 `
  --max-state-answer-chars 16000
```

准备目录中的 `cohort.json` 保存完整 case/request ID 和 SHA-256。随后冻结
旧基线共同集并选择跨状态长度和值类型的 40-case canary：

```powershell
conda run -n Npflower python -m granularity3_local.decomposed_plan `
  --prepared-dir granularity3_local\decomposed_full_v2_16k_prepared `
  --output-dir granularity3_local\decomposed_full_v2_16k_plan `
  --legacy-selected-requests granularity3_local\block_state_api_full_gpt54_low_3557\selected_model_batches.jsonl `
  --canary-case-count 40
```

API canary 和正式运行都使用 ID 文件精确选择，避免 `task-limit` 造成只取
前部简单案例。能力统计使用首个收到的响应，不对格式无效回答进行模型重试；
网络错误可通过相同配置 `--resume` 恢复，已收到但格式无效的回答使用
`--resume-received` 固定为失败。

正式 API 配置为 `gpt-5.4`、`reasoning_effort=low`、`verbosity=low`、
`max_completion_tokens=16384`、`retries=0`、API JSON mode。JSON mode 是
输出通道约束，不能替代本地 schema 校验；它防止兼容服务把 `<think>` 标签混入
JSON 正文。Control-Flow 与 Oracle-CF
State 可以并行；Predicted-CF State 必须在 Control-Flow 完成后构造。
API runner 逐条追加 attempts 与 responses，进程中断时仍可恢复；正常结束后再按
冻结 ID 顺序重写为规范 JSONL。`--progress-every 50` 只降低全量日志频率。

Control-Flow 全量命令：

```powershell
conda run -n Npflower python -m granularity3_local.decomposed_api `
  --kind control_flow `
  --requests granularity3_local\decomposed_full_v2_16k_prepared\control_flow\requests.jsonl `
  --oracles granularity3_local\decomposed_full_v2_16k_prepared\control_flow\oracles.jsonl `
  --output-dir granularity3_local\decomposed_full_v2_api_control_3591 `
  --request-ids-file granularity3_local\decomposed_full_v2_16k_plan\full_control_request_ids.txt `
  --model gpt-5.4 `
  --reasoning-effort low `
  --verbosity low `
  --max-completion-tokens 16384 `
  --json-mode `
  --retries 0 `
  --concurrency 3 `
  --progress-every 50
```

Oracle-CF State 全量命令：

```powershell
conda run -n Npflower python -m granularity3_local.decomposed_api `
  --kind oracle_state `
  --requests granularity3_local\decomposed_full_v2_16k_prepared\oracle_state\requests.jsonl `
  --oracles granularity3_local\decomposed_full_v2_16k_prepared\oracle_state\oracles.jsonl `
  --output-dir granularity3_local\decomposed_full_v2_api_oracle_state_2241 `
  --request-ids-file granularity3_local\decomposed_full_v2_16k_plan\full_oracle_state_request_ids.txt `
  --model gpt-5.4 `
  --reasoning-effort low `
  --verbosity low `
  --max-completion-tokens 16384 `
  --json-mode `
  --retries 0 `
  --concurrency 3 `
  --progress-every 50
```

正式报告同时给出：

- control case micro 与 task macro；
- state variable micro、case all-variable exact 与 task macro；
- 状态长度分桶；
- 1117/3591 的状态案例覆盖率；
- Oracle-CF、Predicted-CF 和端到端联合指标；
- 旧联合基线仅在 run-level 与 statement-level Oracle 兼容子集上的结果；
- 所有排除原因及完整 frozen cohort 哈希。
