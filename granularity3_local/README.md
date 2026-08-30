# 粒度三实验本地流水线

当前主线是“完整 Block 执行序列 + 稀疏变量变化”。模型只接收静态代码、Block/CFG 定义和函数输入；本地执行器生成 Oracle，最后由评分器比较模型结果与 Oracle。

正式 `gpt-5.4 + reasoning_effort=low` 全量实验已经完成：3557/3557 个输入收到响应，Expanded Block exact 为 94.38%，变量变化 exact 为 81.87%，联合 exact 为 81.47%。克隆仓库后可直接执行：

```powershell
conda env create -f granularity3_local\environment.yml
conda run -n granularity3 python -m granularity3_local.verify_release
```

完整离线复评流程见 [`REPRODUCIBILITY.md`](REPRODUCIBILITY.md)；正式运行产物逐项说明见 [`block_state_api_full_gpt54_low_3557/RUN_ARTIFACT_GUIDE.md`](block_state_api_full_gpt54_low_3557/RUN_ARTIFACT_GUIDE.md)，端到端案例见 [`CASE_STUDY.md`](block_state_api_full_gpt54_low_3557/CASE_STUDY.md) 和 [`CASE_STUDY.png`](block_state_api_full_gpt54_low_3557/CASE_STUDY.png)。

控制流与变量状态解耦的新实验见 [`DECOMPOSED_EXECUTION.md`](DECOMPOSED_EXECUTION.md)。该方案保留旧版联合实验作为基线，新增 Control-Flow、Oracle-CF State 和 Predicted-CF State 三种条件。

## 目录结构

```text
granularity3_local/
├── cfg.py                    # AST 基本块和静态 CFG
├── executor.py               # 运行时事件/行号记录
├── oracle.py                 # 本地执行 Oracle
├── preflight.py              # 任务预检和函数识别
├── state.py                  # 状态规范化
├── isolated.py               # 隔离执行
├── runtime.py                # 运行时状态记录
├── block_state_local.py      # 新版单 case 本地结果
├── block_state_dataset.py    # 新版数据集级 Oracle 生成
├── block_state_batch.py      # 模型请求准备：task 批处理或单 case
├── block_state_api.py        # 新版模型 API 调用
├── block_state_canary.py     # 分层抽样与新旧配置指标对比
├── block_state_evaluate.py   # 新版响应校验和评分
├── decomposed_core.py        # 控制流/逐变量状态解耦协议与核心逻辑
├── decomposed_statement.py   # 语句级变量状态插桩
├── decomposed_prepare.py     # 解耦数据与 Predicted-CF State 请求生成
├── decomposed_api.py         # 解耦任务 API 调用
├── decomposed_evaluate.py    # 解耦任务评分与联合分析
├── legacy/                   # 旧版 Probe/行号 baseline，不参与新版主流程
└── tests/                    # 新版测试；tests/legacy 保存旧版测试
```

旧版代码保留在 `legacy/`，用于复现历史 baseline；主目录不再混放旧入口。已删除未被主流程引用的 split/structured 实验分支。

## 当前结果目录

```text
block_state_mbppplus_full/                  本地全量 Oracle 聚合记录
block_state_model_batches/                  生成正式请求的初始批次与隔离 Oracle
block_state_api_canary_40/                   40-case 分层抽样清单
block_state_api_canary_gpt54_none_low_40/    被否决的 reasoning=none 配置证据
block_state_api_canary_gpt54_low_low_40_v2/  被采用的 reasoning=low 配置证据
block_state_full_rollout_300_plan/            300-case 中期检查预选计划
block_state_api_full_gpt54_low_3557/          3557-case 正式请求、响应、评分与报告
```

逐 case 原始动态事件、10-task 调试、probe 和自检目录可由代码重新生成，不纳入正式 Git 发布。正式目录已包含实际使用的全部 3557 条请求、Oracle、模型原始响应及逐输入评分。

## 运行新版本地 Oracle

单个 task：

```powershell
conda run -n Npflower python -m granularity3_local.block_state_local `
  --task-dir <task_dir> `
  --output-root granularity3_local\block_state_local_pilot `
  --limit 10
```

全数据集：

```powershell
conda run -n Npflower python -m granularity3_local.block_state_dataset `
  --dataset-root <standardized_mbppplus> `
  --output-root granularity3_local\block_state_mbppplus_full `
  --inputs-per-task 10 `
  --workers 8 `
  --resume
```

新版本地答案只保留两个模型比较目标：

```json
{
  "block_trace": ["B001", "B002"],
  "changes": [[0, "x", {"$u": 1}, 0]]
}
```

完整原始事件仍保存在 case 目录中，用于审计；不发送给模型。

## 构造批量模型请求

```powershell
conda run -n Npflower python -m granularity3_local.block_state_batch `
  --dataset-root <standardized_mbppplus> `
  --local-output-root granularity3_local\block_state_mbppplus_full `
  --output-dir granularity3_local\block_state_model_batches `
  --max-cases 10
```

同一 task 的代码和 Block 定义只发送一次，最多 10 个输入放入同一个 `cases` 数组。预计输出过长时会自动拆批。

如果希望在文件层面也生成“一条输入一条请求”，加上：

```powershell
conda run -n Npflower python -m granularity3_local.block_state_batch `
  --dataset-root <standardized_mbppplus> `
  --local-output-root granularity3_local\block_state_mbppplus_full `
  --output-dir granularity3_local\block_state_model_batches_per_case `
  --one-case-per-request
```

这会保留相同的请求/Oracle 数据契约，但每行只包含一个 `cases` 元素。

## 调用模型和评分

API 密钥只从 `YUNWU_API_KEY` 读取。默认 OpenAI 兼容入口为
`https://api.openlux.ai/v1`，也可以用 `--base-url` 或
`YUNWU_API_BASE_URL` 覆盖：

```powershell
conda run -n Npflower python -m granularity3_local.block_state_api `
  --model-batches granularity3_local\block_state_model_batches\model_batches.jsonl `
  --oracle-batches granularity3_local\block_state_model_batches\local_answer_batches.jsonl `
  --output-dir granularity3_local\block_state_api_per_case_10tasks `
  --tasks task_109,task_126 `
  --model gpt-5.4 `
  --retries 1 `
  --flat-runs `
  --resume
```

`--flat-runs` 会在 API 调用前将已有 task 批次拆成稳定的
`task_id/input_id` 请求；每次 API 请求只携带一个输入。`--resume` 只复用请求内容、模型、提示词、生成参数和返回格式指纹全部一致，且通过完整结构校验的响应，适合长时间运行或中断后继续。每个输出目录会固定保存 `run_config.json`；模型、API 地址、提示词、token 上限、reasoning、verbosity、temperature、超时、并发度或输入集合任一项变化，程序都会拒绝在原目录续跑。

需要保留首轮格式无效响应、只补齐因网络中断而完全没有响应的输入时，同时使用
`--resume --resume-received`。这能避免把真实的格式失败覆盖成第二次采样结果。

推荐的全量实验生成配置来自 40 输入 canary：

```powershell
conda run -n Npflower python -m granularity3_local.block_state_api `
  --model-batches granularity3_local\block_state_model_batches\model_batches.jsonl `
  --oracle-batches granularity3_local\block_state_model_batches\local_answer_batches.jsonl `
  --output-dir <new_output_dir> `
  --tasks <task_ids> `
  --model gpt-5.4 `
  --base-url https://api.openlux.ai/v1 `
  --transport http `
  --flat-runs `
  --reasoning-effort low `
  --verbosity low `
  --max-completion-tokens 8192 `
  --timeout 180 `
  --complex-timeout 300 `
  --complex-loop-threshold 2 `
  --concurrency 3 `
  --retries 0
```

请求同时受底层网络 timeout 和墙钟硬截止时间约束；即使网关持续发送不完整数据，单个请求到期后也会记录 `TimeoutError`，不会无限阻塞整个实验。静态代码含至少两个循环头时使用 `--complex-timeout`，但输出仍是压缩运行段，不会在本地展开超长循环。

扁平运行段模式下，模型只返回两个顶层字段，不包含 `results`、`id` 或元数据包装：

```json
{
  "block_trace": [
    ["B001", 1],
    ["B002>B003", 1000],
    ["B002", 1],
    ["B004", 1]
  ],
  "changes": [
    [0, "i", {"$u": 1}, 0],
    [1, "i", 0, 1000]
  ]
}
```

其中 `block_trace` 每行是 `[路径, 连续重复次数]`，路径中的 Block 用 `>` 连接；`changes` 每行是 `[运行段索引, 变量, 段前值, 段后值]`。本地 Oracle 会先转换成相同格式，API 返回可以直接比较。旧的展开序列和 task 批量模式仍保留兼容。

Block 评分有两个口径：

- `canonical_block_exact`：压缩行、路径和重复次数逐项相同，用于检查模型是否遵守规范分段。
- `expanded_block_exact`：两份压缩结果代表的完整 Block 序列相同，用于报告完整 Block 序列准确率。实现只遍历压缩运行段，并用周期串比较处理不同分段；即使重复次数极大，也不会创建展开序列。

相邻相同路径未合并仍可无损解释，因此会参与评分，并通过 `canonical_format_valid=false` 单独标记；未知 Block、非法重复次数或无法解析的 JSON 仍会拒绝。兼容字段 `block_exact` 仍等于 `canonical_block_exact`。汇总中的普通 `*_rate` 以可安全评分的响应为分母，`*_rate_all_cases` 以全部预期输入为分母，无法评分的响应按错误计入。

API 运行过程会自动评分。若要单独重新评分，必须使用 API 输出目录中已经拆成单 case 的清单：

```powershell
conda run -n Npflower python -m granularity3_local.block_state_evaluate `
  --model-batches granularity3_local\block_state_api_per_case_10tasks\selected_model_batches.jsonl `
  --oracle-batches granularity3_local\block_state_api_per_case_10tasks\selected_oracle_batches.jsonl `
  --responses granularity3_local\block_state_api_per_case_10tasks\model_responses.jsonl `
  --output-dir granularity3_local\block_state_api_per_case_10tasks\evaluation
```

## Canary 结果

分层抽取 10 个 task、每个 task 4 个输入，共 40 个输入。相对旧默认配置，推荐配置 `reasoning_effort=low`、`verbosity=low`、`max_completion_tokens=8192` 的结果为：

- Expanded Block：72.5% → 77.5%；
- completion token：134,108 → 35,363，减少 73.63%；
- 响应时间 P90：101.84 秒 → 32.82 秒，下降 67.77%；
- OpenLux API 错误：0；40 条均报告 reasoning 明细，共 29,879 reasoning token；
- 同配置恢复跳过 40/40、没有新调用；把 token 上限改为 4096 后在请求前拒绝混用目录。

`reasoning_effort=none` 虽更快，但 Expanded Block 只有 22.5%，不能用于全量实验。完整机器可读对比保存在 `block_state_api_canary_gpt54_low_low_40_v2/comparison/comparison.json`。

## 测试

```powershell
conda run -n Npflower python -m unittest discover `
  -s granularity3_local\tests `
  -p "test*.py"

conda run -n Npflower python -m unittest discover `
  -s granularity3_local\tests\legacy `
  -p "test*.py"
```

旧实验输出、演示目录和 task296 多轮试验产物已清理；它们不参与当前新版流程。

## 正式结果快速入口

- `block_state_api_full_gpt54_low_3557/EXPERIMENT_SUMMARY.md`：配置、完整性、准确率、Token 与时延总结；
- `block_state_api_full_gpt54_low_3557/evaluation/summary.json`：论文能力指标的机器可读来源；
- `block_state_api_full_gpt54_low_3557/evaluation/case_scores.jsonl`：逐输入比较结果；
- `block_state_api_full_gpt54_low_3557/evaluation/response_errors.jsonl`：86 条格式无效响应；
- `block_state_api_full_gpt54_low_3557/final_gate/gate.json`：正式运行质量门槛；
- `REPRODUCIBILITY.md`：从环境建立、离线复评到在线重跑的完整说明。
