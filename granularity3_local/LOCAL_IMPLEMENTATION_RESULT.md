# 粒度三：大模型之前的本地部分实现结果

## 已完成的数据流

```text
代码 + 测试输入
  → 状态规范化
  → AST基本块划分
  → 静态CFG
  → AST插入事件记录器
  → 原程序与插桩程序分别执行并校验返回值
  → block occurrence、执行前后状态及状态增量
  → 完整本地Oracle
  → 模型可见输入与本地答案隔离
```

## 分阶段测试

| 阶段 | 验证内容 | 结果 |
|---|---|---:|
| 状态表示 | 容器排序、特殊浮点、快照冻结、创建/修改/删除 | 4/4通过 |
| 静态CFG | if、for、while基本块及边 | 3/3通过 |
| 动态执行 | 分支路径、for occurrence、while occurrence和delta | 3/3通过 |
| Oracle | JSON/JSONL落盘、语义保持、重复执行哈希一致 | 1/1通过 |
| Probe隔离 | 输入答案分离、probe_id对齐、无答案字段泄露 | 1/1通过 |
| 端到端 | 两输入批量生成所有本地产物 | 1/1通过 |

合计13项测试全部通过。

## 真实MBPP+小规模结果

| 任务 | 结构 | 输入数 | 成功 | 失败 | 事件/Probe |
|---|---|---:|---:|---:|---:|
| task_109 | for循环+循环内分支 | 3 | 3 | 0 | 48 |
| task_223 | 条件分支+提前返回 | 4 | 4 | 0 | 11 |
| task_296 | 嵌套for循环+分支 | 1 | 1 | 0 | 38 |
| 合计 | — | 8 | 8 | 0 | 97 |

每个成功输入均满足：

- 插桩程序返回值与原程序完全一致；
- 每个动态事件都有唯一 `frame/block#occurrence`；
- 保存执行前状态、执行后状态、delta、真实next及return；
- 一个事件导出一个模型Probe和一个隔离的本地答案。

## 本地产物

每个输入的目录结构：

```text
cases/<task>/<variant>/<input>/
├── oracle/
│   ├── case.json
│   ├── cfg.json
│   ├── events.jsonl
│   └── hashes.json
└── probes/
    ├── model_case.json
    ├── model_inputs.jsonl
    ├── answers.jsonl
    └── manifest.json
```

其中只把 `model_case.json + model_inputs.jsonl` 交给大模型，`answers.jsonl` 留在本地。

## 当前明确边界

当前MVP支持顺序语句、if、普通for、普通while和return。遇到以下结构会明确记录失败，
不会生成可能不正确的oracle：

- break/continue；
- for-else/while-else；
- try/raise及异常控制流；
- with；
- 辅助函数和递归的多frame追踪。

这些属于下一轮本地能力扩展，不影响当前已支持结构的实验结果。
