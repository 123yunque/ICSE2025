# 粒度三实验复现说明

本目录发布第三粒度“Block 执行序列 + 稀疏变量变化”实验的实现、配置选择证据、正式 API 原始响应、本地 Oracle、逐输入评分和验收报告。正式实验为 `gpt-5.4`、`reasoning_effort=low`、每个输入一次独立请求，共 3557 个输入。

## 1. 发布内容

| 内容 | 目录/文件 | 是否需要外部服务 |
|---|---|---:|
| 核心实现 | `block_state_*.py`、`cfg.py`、`runtime.py` 等 | 否 |
| 新版与旧 baseline 测试 | `tests/`、`tests/legacy/` | 否 |
| 40-case 配置选择 | `block_state_api_canary_*_40*` | 否 |
| 300-case 预先选择计划 | `block_state_full_rollout_300_plan/` | 否 |
| 正式请求、响应、Oracle 与评分 | `block_state_api_full_gpt54_low_3557/` | 离线复评不需要 |
| 本地 Oracle 汇总与 API 批次 | `block_state_mbppplus_full/`、`block_state_model_batches/` | 重新生成时需要数据集 |

逐 case 的原始动态事件目录约 623 MB，包含数千个机器本地中间文件，未纳入 Git；其聚合记录、正式使用的 3557 条 Oracle 和全部正式评分均已发布。开发期 10-task 调试、probe 和自检目录也不参与正式结果。

## 2. 环境

主流程只依赖 Python 3.9 标准库。可使用 Conda 建立最小环境：

```powershell
conda env create -f granularity3_local\environment.yml
conda activate granularity3
```

所有命令均应在仓库根目录执行。

## 3. 克隆后先做完整性检查

```powershell
python -m granularity3_local.verify_release
```

该检查会逐行解析正式 JSONL，核对请求、Oracle 和响应的 `batch_id` 集合，确认 3471 条有效评分与 86 条格式失败恰好覆盖 3557 个输入，并验证 3558 次 API 尝试及正式配置指纹。

## 4. 不调用 API，重新计算全部指标

```powershell
python -m granularity3_local.block_state_evaluate `
  --model-batches granularity3_local\block_state_api_full_gpt54_low_3557\selected_model_batches.jsonl `
  --oracle-batches granularity3_local\block_state_api_full_gpt54_low_3557\selected_oracle_batches.jsonl `
  --responses granularity3_local\block_state_api_full_gpt54_low_3557\model_responses.jsonl `
  --output-dir granularity3_local\block_state_api_full_gpt54_low_3557\evaluation_reproduced `
  --allow-partial
```

复评结果应得到：

- 格式有效：3471/3557（97.58%）；
- Expanded Block exact：3357/3557（94.38%）；
- Variable changes exact：2912/3557（81.87%）；
- Expanded joint exact：2898/3557（81.47%）。

格式无效的 86 条响应按失败计入全输入分母，不重新采样。

## 5. 重新生成最终 Gate

```powershell
python -m granularity3_local.block_state_canary gate `
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

## 6. 从标准化 MBPP+ 重建 Oracle

源数据集不复制进本仓库。准备包含 `task_*/code.py` 和输入文件的标准化 MBPP+ 目录后运行：

```powershell
python -m granularity3_local.block_state_dataset `
  --dataset-root <standardized_mbppplus> `
  --output-root <new_oracle_output> `
  --inputs-per-task 10 `
  --workers 8 `
  --resume
```

随后用 `block_state_batch.py` 构造模型输入。正式实验的数据筛选统计保存在 `block_state_mbppplus_full/summary.json` 与 `block_state_model_batches/summary.json`。

## 7. 重新调用模型

在线复现实验需要兼容 OpenAI 接口的模型服务，并从环境变量读取密钥：

```powershell
$env:YUNWU_API_KEY = Read-Host 'OpenLux API key'
```

完整参数见正式目录的 `RUN_ARTIFACT_GUIDE.md`。新的模型调用必须写入新目录；不要覆盖已发布的正式响应。第三方服务的模型版本、路由和采样行为可能变化，因此在线重跑可以复现协议与计算过程，但不保证逐 token 或逐 case 与 2026 年 8 月的响应完全一致。

## 8. 可复现性边界

- **完全可复现**：JSONL 解析、模型结果与 Oracle 的比较、所有准确率和 Gate。
- **过程可复现**：本地 Oracle 与请求构造；前提是取得相同版本和相同标准化形式的 MBPP+ 输入。
- **不能保证逐响应一致**：重新请求托管大模型。已发布的原始响应用于固定论文结果并允许独立复评。

正式文件逐项含义、case 提取命令和案例图见 `block_state_api_full_gpt54_low_3557/RUN_ARTIFACT_GUIDE.md` 与 `CASE_STUDY.md`。
