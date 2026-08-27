# 第三粒度正式实验：代码与产物指南

本文档说明本次 `gpt-5.4 + reasoning_effort=low + flat_runs` 全量实验涉及的代码、前置数据、正式运行产物、评分文件及复现方法。

## 1. 实验数据流

```text
标准化 MBPP+ task
    ↓
block_state_dataset.py：本地真实执行并生成 Oracle
    ↓
block_state_batch.py：生成模型请求与隔离的本地答案
    ↓
block_state_canary.py：40-case 配置选择、300-case 中期检查
    ↓
block_state_api.py：每个输入单独调用 API
    ↓
block_state_evaluate.py：模型结果与 Oracle 比较
    ↓
case_scores.jsonl / summary.json / final_gate
```

正式实验共选择 3557 个输入，得到 3557 条最终模型响应和 3558 条 API 尝试记录。多出的 1 条尝试是 `task_227/input_9` 首次连接中断后的同配置补跑。

## 2. 主流程代码

| 文件 | 作用 | 什么时候使用 |
|---|---|---|
| `block_state_dataset.py` | 遍历数据集，生成全量本地执行 Oracle | 从标准化数据集重新生成 Oracle |
| `block_state_local.py` | 将单个 case 的原始执行事件投影为 Block 轨迹和稀疏变量变化，并生成 `flat_runs` | 单 case 调试、Oracle 格式转换、长循环压缩 |
| `block_state_batch.py` | 提取函数、Block/CFG和输入，生成模型请求及隔离的 Oracle 文件 | 重新构造 API 输入文件 |
| `block_state_api.py` | 拆成每输入一个请求，调用 OpenLux，记录尝试、断点恢复、锁定配置并自动评分 | 运行或恢复在线实验 |
| `block_state_evaluate.py` | 校验响应格式，比较 Block/changes，生成逐输入和汇总指标 | 离线重新评分，不需要调用 API |
| `block_state_canary.py` | Canary 抽样、配置比较、300-case 和全量 Gate | 配置选择与实验验收 |

### 2.1 基础执行模块

| 文件 | 作用 |
|---|---|
| `preflight.py` | 函数识别和不支持结构预检，如递归和跳转 |
| `cfg.py` | 基于 AST 构造基本块及静态控制流关系 |
| `runtime.py` | 运行时 Block 和局部变量状态记录 |
| `state.py` | Python 值确定性编码、状态快照和状态差分 |
| `executor.py` | 执行目标程序并收集运行结果 |
| `isolated.py` | 使用隔离子进程执行，实施超时和资源限制 |
| `oracle.py` | 组织完整本地动态执行 Oracle 和审计文件 |

### 2.2 测试代码

`tests/test_block_state_*.py` 分别覆盖本地执行、数据集生成、请求构造、API 恢复、评分和 Gate。当前新版测试共 67 个。

运行测试：

```powershell
conda run -n Npflower python -m unittest discover `
  -s granularity3_local\tests `
  -p "test*.py"
```

## 3. 前置 Oracle 目录

目录：`granularity3_local/block_state_mbppplus_full/`

| 文件/目录 | 含义 | 用法 |
|---|---|---|
| `cases/` | 每个 `task/input` 的原始执行事件、CFG、局部答案和审计信息 | 调查 Oracle 是否正确；不发送给模型 |
| `case_records.jsonl` | 每个 case 的执行状态、大小、路径和错误摘要 | 数据集级审计与筛选 |
| `preflight.json` | task 预检结果 | 查看不支持的递归或跳转任务 |
| `summary.json` | 376个task、3641个计划输入、本地成功/失败等汇总 | 报告本地数据构建过程 |

该目录是本地真实执行结果，不是模型响应目录。

## 4. 请求准备目录

目录：`granularity3_local/block_state_model_batches/`

| 文件 | 含义 | 用法 |
|---|---|---|
| `model_batches.jsonl` | 初始模型请求；同一 task 可在一行包含多个输入 | `block_state_api.py` 的输入；正式运行时由 `--flat-runs` 拆成单输入请求 |
| `local_answer_batches.jsonl` | 与初始请求对应的本地 Oracle | API 程序拆分并生成正式 `selected_oracle_batches.jsonl` |
| `system_prompt.txt` | 模型输出格式和语义要求 | 审计提示词；其 SHA-256 写入正式配置 |
| `batch_manifest.jsonl` | 每个初始批次的 case 数、大小和拆批信息 | 检查批次构造过程 |
| `excluded_cases.jsonl` | 本地失败、事件过多、轨迹过大或答案过长的输入 | 说明为何最终为3557个输入 |
| `summary.json` | 366个可用task、3557个输入等批次汇总 | 报告请求构造统计 |

## 5. 300-case 正式阶段计划

目录：`granularity3_local/block_state_full_rollout_300_plan/`

| 文件 | 含义 | 用法 |
|---|---|---|
| `stage_case_keys.txt` | 正式实验首先运行的300个输入键 | 传给 API 的 `--stage-case-keys-file` 和中期 Gate |
| `selection.json` | 抽样规则、复杂度分层、输入清单和选择哈希 | 证明300条不是事后挑选 |

该目录只定义正式实验的第一阶段，不包含模型响应。

## 6. 正式运行根目录

目录：`granularity3_local/block_state_api_full_gpt54_low_3557/`

### 6.1 正式输入和配置

| 文件 | 含义 | 是否必须保留 |
|---|---|---:|
| `run_config.json` | 模型、API地址、提示词哈希、token、reasoning、verbosity、超时、并发、输入顺序和运行配置指纹 | 必须 |
| `selected_model_batches.jsonl` | 真正参与正式实验的3557个单输入请求 | 必须 |
| `selected_oracle_batches.jsonl` | 与3557个正式请求一一对应、已转换为 `flat_runs` 的本地标准答案 | 必须 |

`selected_model_batches.jsonl` 与上游 `model_batches.jsonl` 不重复承担同一职责：前者是正式 API 实际使用的一输入一请求清单，后者是拆分前的初始 task 批次。

### 6.2 API 原始证据

| 文件 | 含义 | 用法 | 是否必须保留 |
|---|---|---|---:|
| `model_responses.jsonl` | 每个 `batch_id` 最终保留的一条原始模型响应，共3557条 | 离线评分的模型答案输入 | 必须 |
| `api_attempts.jsonl` | 每一次 API 尝试的状态、时延、token、配置、原始响应或错误，共3558条 | 计算时延/token/API错误；审计重试 | 必须 |
| `summary.json` | 当前 API 运行、恢复情况、token、时延和嵌套评估摘要 | 快速查看整次运行状态 | 建议保留 |

`model_responses.jsonl` 是每个输入的最终答案集合；`api_attempts.jsonl` 是完整请求历史。因此重试发生时，后者行数可以多于前者。

### 6.3 评分目录 `evaluation/`

| 文件 | 含义 | 用法 | 是否可重新生成 |
|---|---|---|---:|
| `model_predictions.jsonl` | 解析且通过结构验证的模型答案，附加可信的 `batch_id/id` | 调查模型预测内容 | 可以 |
| `case_scores.jsonl` | 每个有效输入的 Block、changes、joint 正确性及首个差异 | 主要逐输入分析文件 | 可以 |
| `batch_scores.jsonl` | 每个请求的汇总评分；当前一请求一输入，因此与 case 粒度接近 | 兼容批次级统计 | 可以 |
| `response_errors.jsonl` | 86条无法安全评分的模型响应及原因 | 格式错误分类；按失败计入全输入指标 | 可以 |
| `summary.json` | 正确数、有效响应分母和全输入分母下的准确率 | 论文能力指标来源 | 可以 |

根目录 `summary.json` 偏向 API 运行状态；`evaluation/summary.json` 偏向模型执行推理能力指标，不应混淆。

### 6.4 Gate 与说明文件

| 文件 | 含义 | 用法 |
|---|---|---|
| `midterm_gate_300/gate.json` | 前300输入的机器可读验收结果 | 证明中期门槛通过后才扩展全量 |
| `midterm_gate_300/MIDTERM_REPORT.md` | 前300输入的人类可读报告 | 中期实验记录 |
| `final_gate/gate.json` | 3557输入的机器可读最终Gate | 自动检查五项门槛 |
| `final_gate/FINAL_REPORT.md` | 最终Gate简报 | 快速查看PASS/FAIL |
| `EXPERIMENT_SUMMARY.md` | 正式配置、完整性、准确率、无效响应、token和时延总结 | 论文数据整理和人工阅读 |
| `RUN_ARTIFACT_GUIDE.md` | 本文档 | 理解代码、目录和复现方法 |
| `CASE_STUDY.md` | 两个真实输入的请求、推理、Oracle和比较流程 | 论文案例分析与方法展示 |
| `CASE_STUDY.png` | `task_770/input_6` 从请求、模型推理、本地执行到逐输入评分的静态流程图 | 论文、汇报和答辩插图 |

Gate 是运行质量门槛，不替代准确率分析。最终能力指标应读取 `evaluation/summary.json`。

## 7. 三个核心正式文件如何关联

```text
selected_model_batches.jsonl
    batch_id + 函数 + Block/CFG + 一个具体输入
                     ↓ API
model_responses.jsonl
    batch_id + 模型预测的 block_trace/changes
                     ↘
                      block_state_evaluate.py → case_scores.jsonl
                     ↗
selected_oracle_batches.jsonl
    batch_id + 本地真实执行的 block_trace/changes
```

三者通过 `batch_id` 对应，不应仅依赖文件行号。

## 8. 常用操作

### 8.1 查看一个输入的请求、模型响应和 Oracle

```powershell
$caseKey = 'task_770/input_7'
$runDir = 'granularity3_local\block_state_api_full_gpt54_low_3557'

Select-String -LiteralPath "$runDir\selected_model_batches.jsonl" -SimpleMatch "`"batch_id`": `"$caseKey`""
Select-String -LiteralPath "$runDir\model_responses.jsonl" -SimpleMatch "`"batch_id`": `"$caseKey`""
Select-String -LiteralPath "$runDir\selected_oracle_batches.jsonl" -SimpleMatch "`"batch_id`": `"$caseKey`""
Select-String -LiteralPath "$runDir\evaluation\case_scores.jsonl" -SimpleMatch "`"case_key`": `"$caseKey`""
Select-String -LiteralPath "$runDir\evaluation\response_errors.jsonl" -SimpleMatch "`"batch_id`": `"$caseKey`""
```

### 8.2 不调用 API，离线重新评分

使用新输出目录，避免覆盖正式评分：

```powershell
conda run -n Npflower python -m granularity3_local.block_state_evaluate `
  --model-batches granularity3_local\block_state_api_full_gpt54_low_3557\selected_model_batches.jsonl `
  --oracle-batches granularity3_local\block_state_api_full_gpt54_low_3557\selected_oracle_batches.jsonl `
  --responses granularity3_local\block_state_api_full_gpt54_low_3557\model_responses.jsonl `
  --output-dir granularity3_local\block_state_api_full_gpt54_low_3557\evaluation_reproduced `
  --allow-partial
```

`--allow-partial` 用于允许86条格式无效响应作为失败写入汇总；3557个输入本身没有缺响应。

### 8.3 恢复缺失的 API 响应

只有出现传输失败且正式响应数少于3557时才运行。必须保持所有参数和输出目录不变：

```powershell
conda run -n Npflower python -u -m granularity3_local.block_state_api `
  --model-batches granularity3_local\block_state_model_batches\model_batches.jsonl `
  --oracle-batches granularity3_local\block_state_model_batches\local_answer_batches.jsonl `
  --output-dir granularity3_local\block_state_api_full_gpt54_low_3557 `
  --all-tasks `
  --model gpt-5.4 `
  --base-url https://api.openlux.ai/v1 `
  --transport http `
  --timeout 180 `
  --complex-timeout 300 `
  --complex-loop-threshold 2 `
  --max-completion-tokens 8192 `
  --retries 0 `
  --reasoning-effort low `
  --verbosity low `
  --concurrency 3 `
  --flat-runs `
  --resume `
  --resume-received
```

`--resume-received` 会保留已经收到但格式无效的真实首轮模型结果，只补完全没有响应的输入，避免重采样偏差。

### 8.4 重新生成最终 Gate

```powershell
conda run -n Npflower python -m granularity3_local.block_state_canary gate `
  --run-dir granularity3_local\block_state_api_full_gpt54_low_3557 `
  --model-batches granularity3_local\block_state_api_full_gpt54_low_3557\selected_model_batches.jsonl `
  --output-dir granularity3_local\block_state_api_full_gpt54_low_3557\final_gate_reproduced `
  --report-filename FINAL_REPORT.md `
  --report-title "Full rollout final gate" `
  --min-format-valid-rate 0.95 `
  --min-expanded-block-rate 0.725 `
  --max-p90-seconds 60 `
  --max-api-error-rate 0.02 `
  --max-cap-hit-rate 0.01
```

## 9. 文件保留与清理原则

### 必须归档的原始证据

- `run_config.json`
- `selected_model_batches.jsonl`
- `selected_oracle_batches.jsonl`
- `model_responses.jsonl`
- `api_attempts.jsonl`
- 上游 `system_prompt.txt`
- 当前实验使用的代码版本和环境版本

### 建议归档的报告

- `evaluation/summary.json`
- `evaluation/case_scores.jsonl`
- `evaluation/response_errors.jsonl`
- `final_gate/gate.json`
- `EXPERIMENT_SUMMARY.md`

### 可以重新生成的派生文件

- `evaluation/model_predictions.jsonl`
- `evaluation/batch_scores.jsonl`
- `evaluation/case_scores.jsonl`
- `evaluation/response_errors.jsonl`
- `evaluation/summary.json`
- Gate目录

在完成代码、环境和原始文件归档前，不建议删除任何正式运行文件。尤其不能删除 `selected_*` 文件后仅依靠上游初始批次，因为它们记录了正式运行实际拆分和选择后的输入集合。
