# LiveCodeBench release_v6：全新 100 题实验

分支 `codex/livecodebench-v6-100` 是同一仓库的独立 worktree。复用基础提交
`ea510130f7c09ab7c0e0d0d8a3398bf2c65d8ddd` 的 CFG、插桩、提示和评分代码；未复制旧实验结果。

固定数据为 `livecodebench/code_generation_lite`、revision
`0fe84c3912ea0c4d4a78037083943e8f0c4dd505`、累计 `release_v6`（六批文件，共 1,055 题）。
使用种子 `20260905` 从官方拼接顺序无放回均匀抽取 100 题，抽样在任何生成调用前完成。
本批恰好 LeetCode / AtCoder 各 50 题，easy / medium / hard 为 31 / 42 / 27。
没有抽中 Codeforces，不能将本批解释为按平台分层的代表性样本。

## 固定协议

- 代码生成：`gpt-5.4`、high、JSON mode、16,384 completion tokens；每题最多三个独立候选，选最早通过全部公开及私有测试的一份。隐藏测试不进入生成提示。
- 函数调用题统一为 `solve(*args)`，标准输入题为 `solve(raw_input: str) -> str`；使用薄包装送入官方判题器，跟踪相同算法核心。
- 每题预选最多 10 个原始官方输入：公开按顺序最多 3 个，私有按固定题目种子抽取并补足；不按执行预测成绩替换题目、解答或输入。
- 重复输入的输出按官方等价规则比较：函数题解析 JSON；STDIN 保留行数，逐行去首尾空白，允许相同 Decimal 数字序列。末尾换行差异不视作冲突。审计修复前的误排除记录保存在每题 `format_audit/`，仅这些未生成解答的题恢复构建。
- Oracle：Block 500 事件、语句 2,000 事件、变量状态 500 项、状态答案 16,000 字符、原始轨迹 4 MiB；超限排除而非截断。辅助函数按原子调用处理；不支持的语法如 break/continue、递归单独报告，不要求生成器回避。
- 判题：上游提交及本地文件哈希见 `vendor/SOURCE.json`。仅 Windows deadline 适配，比较规则不变；每测试 6 秒、候选进程 180 秒。Oracle 进程 90 秒。Python 3.9 / Conda `Npflower`。
- 执行预测：沿用 `g3-decomposed-v2`；Control-Flow → Oracle-CF State / Predicted-CF State，`gpt-5.4`、low、JSON mode、16,384 tokens、并发 3。
- 最多 40 个跨接口案例先检查首答完整、格式有效率 ≥95%、无 token 截断，门槛不包含预测准确率。首次收到的无效答案仍计分，不重试挑选答案。
- 单独固定最多 40 个 Oracle-State 请求重复调用，衡量独立调用波动；这些回答不替换主实验首答。
- 真实案例审计后、任何执行推理调用前固定适配器规则：排除包含 `$o`（函数/迭代器等不透明对象、进程内存地址）的变量状态；目标函数调用自身嵌套辅助函数且目标层无控制流的包装实现记为 `helper_only_target`。不更换其已选中代码。该规则单独记入 `preparation_policy.json`，跨数据集比较时须采用同样过滤。
- CF 分母 N、状态变量分母 K、状态案例分母 M 固定。无效 CF 造成不可构造的 Predicted-State 在 K/M 中计失败。
- 全部合格案例为主结果；长度分桶预先固定，L≥10 仅为长轨迹次要分析。配对四格、相同模型可见消息、宏/微平均与按原始题目 2,000 次 bootstrap 同时报告。

## 运行

API 凭据从环境变量 `YUNWU_API_KEY` 读取，不写入仓库。服务地址默认继承原代码，
可在建立新 run 前通过 `YUNWU_API_BASE_URL` 指定；同一 run 的配置冻结后不变。

```powershell
conda run -n Npflower --no-capture-output python lcb_data.py
conda run -n Npflower --no-capture-output python -m unittest test_lcb granularity3_local.tests.test_decomposed
conda run -n Npflower --no-capture-output python lcb_pipeline.py
```

已完成构建时可用 `lcb_pipeline.py --skip-build`。不要同时启动两个同一阶段的运行器。
恢复仅接续 `runs/fresh100_20260906` 本次运行，收到过的首答与请求指纹一致时保留，
没有收到的网络失败允许补齐。新的独立复现实验必须使用新的 `--run runs/<名称>`。

`lcb_experiment.py` 还提供 `build`、`prepare`、`control_flow`、`oracle_state`、
`predicted_state`、`repeat_state` 分阶段入口；`--pilot` 执行工程检查。
`lcb_analyze.py` 从落盘首答重新计算报告。

## 产物

原始数据和生成结果因体积较大不纳入 Git；它们实际保留在本 worktree。

- `data/manifest.json`：六个源文件哈希、固定 100 题 ID 和抽样规则。
- `runs/fresh100_20260906/solutions/`：每题所有生成请求、首答、解答、官方测试结果及勘误审计。
- `runs/fresh100_20260906/prepared/`：本次新计算的 Oracle、冻结请求和排除清单。
- 同一 run 下三个实验条件和 `repeat_state/`：完整模型首答、API 记录和评分。
- `reports/fresh100_20260906/REPORT.md`：结果报告，明确区分进行中和完整结果。

这是从 LiveCodeBench 代码生成题派生的执行推理实验。构建通过率、执行器覆盖率及给定通过测试代码后的推理准确率分别报告，后者不是官方代码生成 pass@1。
