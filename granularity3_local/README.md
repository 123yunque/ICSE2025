# 粒度三：大模型之前的本地流水线

当前本地流水线实现：

```text
源代码与输入
  → 确定性状态编码
  → AST基本块划分与静态CFG
  → AST运行时插桩
  → 原程序/插桩程序语义一致性检查
  → 动态block occurrence与状态增量
  → 本地完整Oracle
  → 模型输入和本地答案隔离导出
```

当前支持顺序语句、`if`、普通 `for`、普通 `while` 和 `return`。为避免静默产生错误
oracle，遇到 `break/continue`、循环 `else`、异常结构和 `with` 时会明确拒绝。

## 构建本地Oracle

```powershell
python -m granularity3_local.oracle `
  --code <code.py> `
  --function <function_name> `
  --input "(<arguments>)" `
  --task-id <task_id> `
  --variant original `
  --input-id input_1 `
  --output-dir <oracle_dir>
```

## 一条命令运行全部本地阶段

对于现有标准任务目录（包含 `code.py` 和 `code_inputs.txt`）：

```powershell
python -m granularity3_local.pipeline `
  --task-dir <task_dir> `
  --function <function_name> `
  --variant original `
  --limit 3 `
  --output-root <output_root>
```

该命令对每个输入依次完成语义保持检查、CFG、动态Oracle和Probe/答案隔离，并生成任务汇总。

## 导出大模型之前的数据

```powershell
python -m granularity3_local.probes `
  --oracle-dir <oracle_dir> `
  --output-dir <probe_dir>
```

输出中：

- `model_case.json`：提供给模型一次的代码块定义、CFG、输入和任务定义；
- `model_inputs.jsonl`：逐事件提供给模型的目标事件和执行前状态；
- `answers.jsonl`：不提供给模型的本地 `next/delta/return` 标准答案；
- `manifest.json`：样本数和文件关系。

## 测试

```powershell
python -m unittest discover -s granularity3_local/tests -v
```

## 数据集级生成

```powershell
python -m granularity3_local.dataset_pipeline `
  --dataset-root <standardized_dataset_root> `
  --output-root <output_root> `
  --inputs-per-task 3 `
  --workers 8 `
  --timeout-seconds 5 `
  --max-events 10000 `
  --max-output-bytes 20000000
```

批量入口会先运行静态预检，仅对受支持目标函数启动独立子进程。重复运行时会复用完整缓存，
并重试尚未成功的样本。

生成完成后执行完整性校验：

```powershell
python -m granularity3_local.validate_dataset --output-root <output_root>
```

## Token 优化的紧凑 Probe

完整 oracle 和原始 probe 保持不变。模型调用前可生成紧凑视图：过滤低信息事件，限制同一
block 的重复 occurrence，删除冗余字段，并把同一 case 的最多 8 个 probe 合并为一个请求。

```powershell
python -m granularity3_local.compact `
  --case-root granularity3_local\mbppplus_3inputs_audit\cases `
  --output-dir granularity3_local\compact_full `
  --batch-size 8 `
  --max-occurrences 3
```

只估算压缩效果而不生成批次：

```powershell
python -m granularity3_local.compact `
  --case-root granularity3_local\mbppplus_3inputs_audit\cases `
  --output-dir granularity3_local\compact_analysis `
  --batch-size 8 `
  --max-occurrences 3 `
  --analyze-only
```

对一个已经生成的紧凑批次调用模型：

```powershell
python -m granularity3_local.compact_api `
  --batch-dir <compact_case_probe_dir> `
  --batch-index 1 `
  --output <result.json> `
  --base-url https://yunwu.ai/v1 `
  --model gpt-5.4
```

API 密钥只从 `YUNWU_API_KEY` 环境变量读取。紧凑格式用 `{"$u":1}` 唯一表示未定义值，
本地完整 oracle 仍使用 `{"$undefined":true}`，二者通过确定性转换对应。
