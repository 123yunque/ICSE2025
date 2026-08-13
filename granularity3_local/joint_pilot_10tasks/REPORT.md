# 粒度三联合请求：10 Task 小规模实验

## 实验设置

- 模型：`gpt-5.4`
- 环境：`Npflower`，OpenAI-compatible Chat Completions
- 数据：MBPP+ 中 10 个结构多样的受支持 task，每个使用 `input_1`
- 调用：每个 task 一次联合请求
- 单次输出：完整执行行号序列、最多 8 个关键 probe 的 `next_block/state_delta`、最终返回值
- 本地标准答案：Python 真实执行生成的 `line_trace` 与 block/state oracle

选择的 task：`task_223`、`task_18`、`task_9`、`task_235`、`task_160`、`task_109`、`task_126`、`task_20`、`task_734`、`task_296`。

## 总体结果

| 指标 | 结果 |
|---|---:|
| API 成功 | 10/10 |
| 完整行号序列 Exact Match | 9/10（90.00%） |
| 最终返回值准确率 | 10/10（100%） |
| Next block 准确率 | 69/69（100%） |
| State delta 严格准确率 | 67/69（97.10%） |
| State delta 语义准确率 | 69/69（100%） |
| Probe 严格联合准确率 | 67/69（97.10%） |
| Case 四项严格联合正确 | 8/10（80.00%） |
| Case 四项语义联合正确 | 9/10（90.00%） |

严格 delta 要求输出 JSON 与 oracle 完全一致。语义 delta 会先删除 `before == after` 的冗余变量，再进行比较。

## Token 与耗时

| 项目 | 数值 |
|---|---:|
| Prompt tokens | 9,657 |
| Completion tokens | 2,183 |
| Total tokens | 11,840 |
| 平均每个 task | 1,184 tokens |
| API 响应总时长 | 37.64 秒 |
| 平均每个 task | 3.76 秒 |

## 主要误差

### task_235：格式冗余，不是状态值推理错误

模型在两个 probe 中额外输出：

```json
"n": {"before": 10, "after": 10}
```

`n` 没有变化，按协议不应出现在 `state_delta` 中。因此严格 delta 判错；删除无变化项后，语义 delta 与 oracle 完全一致。

### task_296：嵌套循环的完整路径错误

- Oracle 行号序列长度：38
- 模型行号序列长度：33
- 最长正确前缀：10
- 编辑距离：5

模型少预测了若干内层循环 header/condition 行。但该 case 的 8 个关键 probe 的 next 与 delta 全部正确，最终返回值也正确。这说明稀疏检查点正确不代表完整执行路径完全正确，保留 `line_trace` 任务是有价值的。

## 基础设施情况

首次调用时网关发生 TCP 超时；连接恢复后 9 个请求成功，`task_109` 遇到一次临时 HTTP 500，单独重试后成功。失败连接没有产生 token usage。正式实验需要有界重试和断点续跑。
