# 基于动态切片的模型代码理解能力全面评测框架调研报告

> 报告主题：代码生成、输入输出推理与中间执行状态推理的统一评测  
> 重点方向：中间执行状态、控制流、数据流、动态切片、基本块划分与长循环压缩  
> 核心参考论文：[Reasoning Runtime Behavior of a Program with LLM: How Far Are We?](../../课题/ICSE2025/ICSE2025_Reasoning_LLM.pdf)  
> 报告日期：2026-08-03

## 1. 摘要

本报告面向一个“模型代码理解能力全面评测 framework”的设计目标，拟从三个粒度评估模型：

1. **代码生成（Code Generation）**：模型能否根据需求生成语法正确、功能正确的代码；
2. **输入输出推理（Input/Output Reasoning）**：模型能否根据代码与输入预测输出，或根据目标输出构造合法输入；
3. **中间执行状态推理（Intermediate Execution-State Reasoning）**：模型能否准确跟踪控制流、变量状态、数据依赖以及中间执行过程。

本报告重点研究第三个粒度，同时兼顾第二个粒度。调研表明，单独使用“代码行”“基本块”或“动态切片”都不足以兼顾唯一定位、执行精度、可读性和长轨迹压缩。推荐采用三层表示：

```text
静态定位层：Source-level CFG + Basic Block / Region
动态事实层：带 occurrence 的状态转移事件
因果查询层：Dynamic Dependence Graph / Dynamic Slice
```

报告暂将该表示称为 **HDET（Hierarchical Dynamic Execution Trace，层次化动态执行轨迹）**。其中：基本块用于稳定描述控制流结构；动态事件用于记录某次具体执行中的状态变化；动态切片用于表达某个变量或输出的真实因果来源。长循环不应通过简单删除中间迭代处理，而应保留无损 oracle，再在模型可见视图中使用循环层次、路径签名、确定性游程编码、状态增量和可验证递推式进行压缩。

最终建议第一版以 Python、单线程、确定性执行和纯函数/可控副作用为范围，实现自动化 tracer、source-level CFG、动态 def-use、控制依赖、动态切片和循环压缩，不依赖 LLM 生成题目或标注答案，从根本上降低人工构造与 API 成本。

## 2. 研究背景与目标

### 2.1 为什么需要三个评测粒度

现有代码模型评测往往集中于代码生成的最终功能正确性，例如 HumanEval 的 pass@k。该类指标能够回答“模型是否生成了可通过测试的程序”，但不能判断模型是否真正理解程序的执行语义。

输入输出推理进一步要求模型模拟程序，但最终输出仍可能通过模式匹配、记忆、自然语言线索或偶然碰撞得到。模型即使给出正确输出，其中间状态也可能已经偏离真实执行。

因此，一个完整的代码理解评测体系应覆盖：

| 粒度 | 核心问题 | 典型证据 |
|---|---|---|
| 代码生成 | 能否构造正确程序 | 编译、测试、pass@k |
| 输入输出推理 | 能否理解整体函数映射 | 输出预测、反向输入验证 |
| 中间状态推理 | 能否真实模拟执行并理解因果关系 | 路径、状态、依赖、切片、首次分歧 |

### 2.2 本项目拟解决的主要问题

本项目主要关注以下研究问题：

- **RQ1：如何唯一、结构化地表示一次程序执行？**
- **RQ2：如何自动选择具有代表性的中间执行检查点，避免人工构题？**
- **RQ3：如何同时表达控制流、数据流和具体运行时状态？**
- **RQ4：循环、递归或长执行轨迹如何压缩，同时不破坏可判分性？**
- **RQ5：如何区分“正确输出”与“正确执行推理”？**
- **RQ6：如何降低闭源模型 API 调用成本，并保证评测公平性？**

## 3. 核心参考工作：REval

### 3.1 论文基本信息

- **题目**：Reasoning Runtime Behavior of a Program with LLM: How Far Are We?
- **作者**：Junkai Chen, Zhiyuan Pan, Xing Hu, Zhenhao Li, Ge Li, Xin Xia
- **会议**：ICSE 2025 Research Track
- **框架**：REval
- **研究类型**：代码推理评测框架、基准构建与大规模实证研究

REval 的核心观点是：代码推理评测不能只关注输入与输出，还需要观察程序执行过程中的运行时行为以及不同推理任务之间的一致性。

### 3.2 REval 的四类任务

| 任务 | 全称 | 预测目标 | 主要语义 |
|---|---|---|---|
| CCP | Code Coverage Prediction | 某条语句是否执行 | 控制流可达性 |
| PSP | Program State Prediction | 语句执行后变量的值和类型 | 数据流、类型变化 |
| EPP | Execution Path Prediction | 当前语句之后的下一条执行语句 | 路径与分支行为 |
| OP | Output Prediction | 给定输入下的最终输出 | 整体执行结果 |

论文基于 HumanEval 和 ClassEval 执行标准实现与测试用例，通过 tracer 提取语句序列和局部变量状态，最终构建 3152 个问题。实验中各模型四任务平均准确率约为 44.4%，EPP 平均准确率约为 19.4%，说明精确控制流模拟尤其困难。

### 3.3 Incremental Consistency

REval 假设四类任务大致构成递进关系：

```text
CCP -> PSP -> EPP -> OP
```

模型若在较简单的前置任务上失败，却在后续任务中得到正确答案，可能存在逻辑不一致或推理捷径。REval 使用四个任务的正确性序列计算 Incremental Consistency Score。

该设计的重要价值在于：它不再只看孤立答案，而是开始评估不同推理结果之间是否协调一致。

## 4. REval 的主要局限

### 4.1 关键筛选规则仍由人工设计

REval 的 ground truth 来自程序真实执行，并非逐题人工标注；但其语句与变量选择规则仍由研究者人工制定。例如：

- CCP/EPP 优先选择基本块的最后一条语句；
- PSP 优先选择赋值、返回和发生变化的变量；
- 跳过简单赋值、复杂对象和不可序列化状态。

论文在内部有效性威胁中也承认，这些规则未必能够完整代表程序运行状态。这会引入选点偏差，并限制框架在新语言、新程序结构和真实项目上的扩展性。

### 4.2 statement index 缺少动态 occurrence

程序中的同一条语句可能被执行多次。尤其在循环中，第 1 次、第 10 次和最后一次执行后的变量状态和后继语句可能不同。

只使用静态 statement index 无法唯一标识动态执行位置。REval 在 EPP 中允许多个可能答案，本质上弱化了 path-sensitive 评测。严格的动态执行标准至少应包含：

```text
(run_id, frame_id, statement_or_block_id, occurrence, phase)
```

动态切片研究同样指出，动态 slicing criterion 必须定位到执行历史中某条语句的精确 occurrence，而不能只使用源代码位置。

### 4.3 对长循环的处理偏向规避

REval 通过 CFG 划分基本块，并优先选取基本块末尾语句，以避免完整执行序列过长。但这并没有表示：

- 每轮迭代的路径差异；
- 循环变量和累加状态的变化；
- 分支变化发生在哪一轮；
- 哪些迭代真正影响最终输出；
- 如何压缩但仍可恢复指定中间状态。

因此，REval 主要是选择少量局部 probe，并没有解决完整长程执行推理问题。

### 4.4 程序状态覆盖不充分

PSP 通常只询问一个变量的值和类型，并会忽略复杂结构。以下语义没有得到充分覆盖：

- 对象别名与共享引用；
- 列表、字典、集合内部元素；
- 对象字段和嵌套结构；
- 递归栈与调用上下文；
- 异常、I/O 和外部副作用；
- 堆状态和内存位置版本；
- 并发和异步执行。

### 4.5 IC 的固定递进关系并非严格因果关系

CCP、PSP、EPP 和 OP 在很多程序中确实相关，但并不总是构成严格的知识包含关系。例如：

- 被抽查变量可能与最终输出无关；
- 不同错误可能在后续计算中抵消；
- 多条路径可能产生相同输出；
- 模型可能通过函数名称或常见模式猜出输出；
- 一个局部 EPP 问题错误，不一定意味着整个输出预测必然错误。

因此，后续框架应使用实例级的动态依赖和因果关系，而不是对所有程序统一假设固定任务链。

### 4.6 API 成本与重复实验不足

REval 对开源模型重复多次实验，但闭源模型因预算限制没有重复。若每个 `(程序, 输入, 检查点, 任务)` 都单独调用一次 API，成本会随检查点数量快速增长，也不利于测量跨任务一致性。

## 5. 后续相关研究及启示

### 5.1 NExT：内联增量轨迹

NExT 将执行状态以源代码内联注释表示，并主要记录发生变化的变量。相比每一步重复完整状态，该方法明显减少了 token 数量。对于循环，NExT 省略中间迭代，只展示部分首尾状态。

启示：

- 状态增量比完整快照更适合模型；
- 代码与执行状态对齐有利于可读性；
- 简单使用 `...` 会丢失中间信息，适合解释，不适合严格 oracle。

### 5.2 Execution Tuning：Dynamic Scratchpad

Execution Tuning 比较了三种执行轨迹策略：

1. 每一步完整状态；
2. 只记录发生变化的变量；
3. Dynamic Scratchpad：反复更新一个自包含的当前状态，而不是累计全部历史。

Dynamic Scratchpad 在长达约 14k steps 的执行上表现出优势，并可通过多步预测减少实际推理步数。

启示：Dynamic Scratchpad 适合模型侧的长程状态维护，但由于它不保留完整历史，不能替代 benchmark 的无损 oracle 和因果切片。

### 5.3 CES：执行连贯性和首次分歧

CES 不只判断中间值是否正确，还检查模型产生的执行模拟是否符合控制流和状态传播规则，并定位 simulation divergence point。它表明模型可能在中间模拟错误的情况下仍给出正确输出。

启示：本项目应报告首次分歧位置、正确前缀长度和因果一致性，而不应只报告最终准确率。

### 5.4 CoRE：实现不变性与过程透明性

CoRE 同时测试：

- 模型对多个功能等价实现是否保持稳定；
- 模型能否回答复杂循环和条件中的中间状态 probe。

研究发现模型可能表现出 superficial execution，即最终输出正确但中间状态错误。不过，CoRE 的 probe 仍依赖 LLM 生成和专家验证，数据构建成本较高。

启示：应保留实现不变性维度，但优先使用现有多实现数据、程序变换和执行器自动生成 probe。

### 5.5 StepCodeReasoner：结构化执行锚点

StepCodeReasoner 通过 teacher LLM 插入 `print` 作为执行锚点，将中间状态变为可验证训练目标。但其规则明确禁止在循环内部插入 print，并过滤输出轨迹超过 10 行的数据。

启示：结构化锚点是可行的监督方式，但“禁止循环内 probe”正好暴露了当前方法对长循环和递归处理不足，本项目可以在这一点上形成差异化贡献。

### 5.6 统一 trace 表示研究

EMNLP 2025 的研究统一比较了 Scratchpad、NExT、SemCoder、CodeExecutor 和 Concise 等语义表示，结果显示：在 prompt 中加入现有 execution trace 并不必然稳定提升模型效果。

启示：表示方案本身必须作为实验变量进行消融，不能预设“轨迹越多、效果越好”。需要同时比较信息量、token 成本、模型准确率和可回放性。

## 6. 总体方案：HDET 三层表示

### 6.1 设计结论

不建议在 code、block 和动态切片之间三选一。三者承担不同职责：

| 层次 | 主要结构 | 作用 |
|---|---|---|
| 静态定位层 | Source CFG、Region、Basic Block、Semantic Operation | 唯一定位代码结构与控制流 |
| 动态事实层 | Event、Occurrence、State Delta、Taken Edge | 描述某次具体执行 |
| 因果查询层 | Dynamic Dependence Graph、Dynamic Slice | 表达对指定结果真正产生影响的历史 |

整体关系如下：

```text
Source Code
    |
    v
AST / Semantic IR
    |
    v
CFG + Region Tree + Basic Blocks
    |
    v
Concrete Execution Events + State Deltas
    |
    v
Dynamic Data/Control Dependence Graph
    |
    v
Criterion-specific Dynamic Slice
    |
    v
Lossless Oracle / Compressed Model View / Evaluation Tasks
```

### 6.2 为什么使用层次化表示

- **代码行过粗**：一行可能包含多个调用、短路表达式和副作用；
- **字节码过细**：精确但难以跨 Python 版本，也不利于人和 LLM 阅读；
- **基本块适合控制流**：块内部没有分支，出口由 terminator 决定；
- **semantic operation 适合状态更新**：可以定位块内关键赋值、调用和副作用；
- **动态 occurrence 解决循环歧义**；
- **动态切片提供实例级因果关系**；
- **region tree 提供循环和分支的自然压缩边界**。

## 7. 基本块与观察点划分

### 7.1 基本块定义

采用 source-level CFG 的单入口、单出口基本块。一个块包含连续 semantic operations，最后以 branch、jump、return、raise 等 terminator 结束。

### 7.2 确定性划分规则

以下位置作为 leader：

1. 函数或方法入口；
2. 条件分支目标；
3. 循环 header、body、latch 和 exit；
4. `return`、`raise`、`break`、`continue` 后继；
5. 异常处理入口；
6. 短路表达式和条件表达式的控制决策点；
7. 必要时显式表示调用和返回边界。

对于 Python，应将以下结构降低为显式 semantic operations：

- `and` / `or` 短路判断；
- chained comparison；
- list/dict/set comprehension；
- 条件表达式；
- 属性、下标和容器更新；
- 可能抛异常的调用；
- 隐式 iterator 更新。

### 7.3 Block ID

每份程序先计算规范化代码哈希，然后在函数内部按 reverse postorder 编号，同序节点按 source span 排序：

```text
<code_hash>/<qualified_function>/B007
```

动态实例增加 frame、occurrence 和 phase：

```text
run03/frame01/B007#12/exit
```

唯一性限定为：

```text
(code_hash, language_version, runtime_version, instrumentation_version)
```

功能等价但结构不同的实现不会共享 block ID。跨实现一致性应通过功能、抽象状态角色或输出关系比较，而不是强行对齐静态 ID。

### 7.4 Observation Point

基本块划分和状态观察点应分离。建议自动生成以下观察点：

- block entry / exit；
- branch predicate 求值后；
- 影响 branch/output 的 definition 后；
- function call 前后；
- return / exception；
- loop header、latch 和 exit；
- 动态切片中的关键定义点。

这样无需为了观测某个变量而任意切碎基本块。

## 8. 动态执行事件与规范状态表示

### 8.1 事件格式示例

```json
{
  "event_id": "run03/frame01/B007#12/exit",
  "static_block": "B007",
  "occurrence": 12,
  "from": "B007",
  "to": "B003",
  "edge": "backedge",
  "delta": [
    {"loc": "i@12", "type": "int", "before": "11", "after": "12"},
    {"loc": "sum@12", "type": "int", "before": "55", "after": "66"}
  ],
  "data_dep": ["run03/frame01/B005#12/exit"],
  "control_dep": ["run03/frame01/B003#12/header"]
}
```

### 8.2 状态使用 delta，而不是重复完整快照

原始执行器可以定期保存完整 checkpoint，但事件默认记录：

- 新增 location；
- 被修改 location 的 before/after；
- 被删除 location；
- 对象别名关系变化；
- return、exception 和外部 effect。

该设计可以大幅减少重复 token，同时保留状态重建能力。

### 8.3 Dynamic SSA 与内存位置

变量和内存位置需要版本化：

```text
x@1, x@2
arr[3]@4
obj.field@2
dict[key]@5
```

对于存在别名的对象，执行器按首次可达遍历顺序分配 object ID：

```text
obj#17
```

不同变量若引用同一对象，应指向同一个 object ID，避免把共享引用错误序列化为两个独立值。

### 8.4 Canonical Serialization

为保证唯一判分，建议规定：

- 整数以十进制字符串存储；
- 浮点数存储 IEEE bits 或 `float.hex()`；
- NaN、Inf 使用固定标签；
- 字典按 key 的规范序列排序；
- 集合按元素规范序列排序；
- tuple/list 保留顺序和类型；
- 对象字段按字段名排序；
- 循环引用使用 `$ref`；
- 不可完整展示的大对象在模型视图中可使用 shape、摘要和 hash，但 oracle 必须保留无损值。

必须严格区分：

```text
无损执行 oracle != 模型可见压缩视图
```

## 9. 动态依赖图与动态切片

### 9.1 切片标准

建议将 slicing criterion 定义为：

```text
Criterion = (
  run_id,
  event_id,
  memory_location,
  before_or_after
)
```

示例：

```text
(run03, B007#12/exit, sum@12, after)
```

### 9.2 动态依赖图

节点是动态事件 occurrence，主要边类型包括：

- dynamic def-use；
- actual control dependence；
- call / return dependence；
- parameter-in / parameter-out；
- heap location dependence；
- exception dependence；
- 可选的 external-effect dependence。

从 criterion 对应节点沿依赖边反向遍历，即可得到动态 backward slice。

### 9.3 动态切片在 benchmark 中的角色

动态切片可以有三种角色：

1. **Ground truth selection**：自动选择与输出或指定变量真正相关的事件；
2. **Prediction target**：要求模型预测相关 block/event 或依赖边；
3. **Trace-assisted input**：将切片作为额外上下文，测试模型利用执行信息的能力。

默认纯代码理解 track 中，动态切片不应作为输入。否则模型已经获得了“哪些语句相关”的因果提示，评测目标会从代码理解变成轨迹阅读。

## 10. 长循环与长执行轨迹压缩

### 10.1 核心原则

1. 原始 trace 和依赖图必须无损保存；
2. 压缩只作用于模型输入、可视化或训练视图；
3. 优先“先按 criterion 动态切片，再压缩”；
4. 压缩结果必须能够验证或回放；
5. 不能询问压缩视图中不可恢复的中间状态。

### 10.2 Loop Episode

每次动态进入自然循环时建立：

```text
LoopEpisode {
  loop_id,
  parent_episode,
  trip_count,
  initial_state,
  iteration_groups,
  exit_state
}
```

每轮迭代的 path signature 定义为：

```text
executed block sequence
+ taken-edge labels
+ slice-relevant write locations
```

### 10.3 确定性 RLE

对相邻且 signature 完全一致的迭代做 maximal run-length encoding：

```text
iterations 1..97:
  path = [B03, B04, B06, B08]
  repeat = 97
```

“最大连续段 + 固定 signature”能够保证压缩结果确定，不需要求解可能不唯一的最小 grammar。

### 10.4 状态压缩

#### 方法一：Slice-relevant Delta

只保留对当前 criterion 有依赖贡献的变量和内存位置变化。

#### 方法二：容器 Patch

```text
append(result, value)
set(arr, index, value)
delete(map, key)
```

避免每轮重复输出整个容器。

#### 方法三：可验证递推式

对于自动识别且经执行器验证的递推关系：

```text
i(k)   = k
sum(k) = k(k + 1) / 2
k in [1, 1000]
```

递推式必须通过抽样和精确回放验证，并记录算法版本。无法验证时退化为 delta/RLE，不应由 LLM 自由生成 oracle。

#### 方法四：异常迭代单独展开

下列迭代不能与普通迭代合并：

- branch signature 变化；
- break/continue/exception；
- relevant write-set 变化；
- 首次或最后一次定义 criterion；
- 嵌套循环 trip count 变化。

### 10.5 递归压缩

递归可使用 Call Episode Tree：

```text
CallEpisode(function, frame_id, depth, input_summary, return_summary)
```

对于结构相同的递归子调用，可按调用签名分组，但 frame occurrence 和实际依赖边仍必须保留在 oracle 中。

## 11. 中间状态推理任务设计

不建议仅复刻 CCP、PSP、EPP。推荐形成四类互补任务。

### 11.1 Local Transition Prediction

输入：代码、输入、当前 event/checkpoint。  
预测：下一 block、taken edge、state delta。

主要测试：局部控制流和单步状态更新。

### 11.2 Region / Loop Summary Prediction

输入：代码、输入、一个 branch/loop region。  
预测：trip count、path groups、exit state、关键递推关系。

主要测试：长程状态保持和循环理解。

### 11.3 Causal Dependency / Dynamic Slice Prediction

输入：代码、输入和 slicing criterion。  
预测：相关静态 block、动态 event occurrence 或依赖边。

主要测试：数据流、控制依赖和因果理解。

### 11.4 Full / Compressed Execution Simulation

输入：代码和输入。  
预测：满足 schema 的压缩 HDET，最后给出输出。

主要测试：完整执行模拟、过程正确性和最终结果一致性。

## 12. 输入输出推理任务设计

### 12.1 Input-to-Output

给定程序和具体输入，预测规范化输出。该任务通常答案唯一，可使用 canonical serialization 后 exact match。

### 12.2 Output-to-Input

给定程序和目标输出，预测一个满足条件的输入。由于反向问题经常多解，不应要求模型匹配唯一参考输入，而应执行验证：

```text
accept iff execute(program, predicted_input) == target_output
```

同时需要限定输入域、类型和资源边界，避免无限多解、平凡解或不可终止搜索。

### 12.3 可增加的稳健性维度

- 常规输入、边界输入、无效输入；
- 同一路径不同状态；
- 不同路径相同输出；
- 相同功能的等价实现；
- 语义保持变换前后；
- 输出碰撞与捷径检测。

## 13. 评测指标

### 13.1 控制流指标

- next-block accuracy；
- taken-edge accuracy；
- path edit distance；
- loop trip-count accuracy；
- branch sequence accuracy。

### 13.2 状态指标

- location identification precision/recall/F1；
- type accuracy；
- value accuracy；
- state-delta exact match；
- full-state reconstruction success；
- alias relation accuracy；
- container patch replay success。

### 13.3 因果指标

- dynamic slice node-F1；
- dynamic slice edge-F1；
- data/control dependence 分类准确率；
- last-writer accuracy；
- output-relevant definition recall。

### 13.4 过程指标

- first divergence position；
- longest correct prefix；
- correct-step ratio；
- compressed trace replay success；
- output accuracy conditioned on process correctness；
- suspiciously correct output rate。

### 13.5 效率指标

- accuracy per 1k input tokens；
- output tokens per correct execution；
- API calls per program/input；
- latency；
- monetary cost；
- trace compression ratio；
- oracle generation time和存储开销。

## 14. 自动化数据构建流水线

```text
Executable Program + Tests
        |
        v
Input Collection / Fuzzing / Boundary Generation
        |
        v
AST + Semantic IR + Source CFG
        |
        v
Sandbox Execution + Lossless Tracing
        |
        v
Dynamic Def-Use + Control Dependence
        |
        v
Criterion Enumeration + Dynamic Slicing
        |
        v
Loop/Call Episode Compression
        |
        v
Schema Validation + Replay Validation
        |
        v
Template-based Task Rendering
        |
        v
Model Evaluation + Structured Parsing + Metrics
```

### 14.1 不使用 LLM 的环节

原则上以下环节均可确定性完成：

- trace 生成；
- CFG 和动态依赖构建；
- checkpoint 枚举；
- ground truth 生成；
- 动态切片；
- 问题模板渲染；
- 回放验证；
- 结果判分。

### 14.2 输入来源

优先级建议：

1. 原 benchmark 测试用例；
2. property-based testing；
3. coverage-guided fuzzing；
4. 边界值模板；
5. concolic/symbolic execution；
6. 必要时才使用 LLM 生成输入，并必须经过执行验证和覆盖率筛选。

### 14.3 等价实现来源

为了评估 implementation invariance，可优先使用：

- CodeNet 等数据中的多个人类正确提交；
- 同一问题的不同参考实现；
- 语义保持程序变换；
- 编译器或 AST 级重写；
- 最后才使用 LLM 生成，并通过完整测试、差分测试和随机测试验证。

## 15. API 成本控制与实验协议

### 15.1 Joint Evaluation

一次请求中让模型返回同一 `(program, input)` 下的多个结构化 probe：

```json
{
  "path": [],
  "state_deltas": [],
  "slice": [],
  "output": null
}
```

优点：减少重复输入代码产生的 token 和调用次数，并可直接检查过程一致性。

### 15.2 Independent Evaluation

在较小代表性子集上，将各任务独立调用，用于测量：

- joint prompt 是否产生信息泄漏；
- 前一个答案是否提示后一个答案；
- 单项能力与联合模拟能力差异。

### 15.3 重复策略

- 主结果可使用固定解码参数和一次确定性调用；
- 在分层抽样子集上重复多次，估计模型方差；
- 对开源模型做完整重复；
- 对闭源模型做预算受控重复；
- 同时报告准确率、方差和调用成本。

## 16. 推荐实验矩阵

### 16.1 表示消融

| 方案 | 表示 |
|---|---|
| A | 仅代码和输入 |
| B | 全量 line-level state snapshot |
| C | changed-variable delta |
| D | block-level path + delta |
| E | slice-first block/event trace |
| F | HDET + loop compression |
| G | dynamic scratchpad current state |

比较：准确率、首次分歧、token 数、压缩率和 API 成本。

### 16.2 粒度消融

- source line；
- semantic operation；
- basic block；
- region/loop；
- bytecode instruction。

### 16.3 长度分层

- 1–10 events；
- 11–50 events；
- 51–200 events；
- 201–1000 events；
- 1000+ events。

### 16.4 程序结构分层

- straight-line；
- single branch；
- nested branch；
- single loop；
- nested loop；
- recursion；
- exceptions；
- mutable containers；
- aliasing；
- library calls。

## 17. 第一版 MVP 实施建议

### 17.1 范围

- Python；
- 单线程；
- 确定性执行；
- 独立函数；
- 内置基础类型与常见容器；
- 暂不处理网络、文件系统和不可控第三方副作用。

### 17.2 必须实现的组件

1. AST/CFG 构建器；
2. source-level basic block 和 region tree；
3. 带 frame/occurrence 的 tracer；
4. canonical value serializer；
5. state delta 与动态 location version；
6. dynamic def-use 和 control dependence；
7. backward dynamic slicer；
8. loop episode + deterministic RLE；
9. JSON Schema validator；
10. trace replay validator；
11. benchmark renderer 和 evaluator。

### 17.3 首批任务

建议首先实现：

- Next Block + State Delta；
- Loop Exit State；
- Dynamic Slice Prediction；
- Input-to-Output；
- Output-to-Input Execution Validation。

### 17.4 建议数据集

第一阶段可从 HumanEval/MBPP 的可执行正确实现开始；第二阶段加入 HumanEval+、MBPP+、LiveCodeBench 和多实现数据；第三阶段再扩展 class-level、real-world project 和多语言场景。

## 18. 研究创新点预期

相对于 REval 和现有工作，本项目可能形成以下贡献：

1. **从单点 probe 扩展到可回放的层次化执行表示**；
2. **使用 occurrence 解决循环语句动态定位歧义**；
3. **统一控制流、状态变化和动态因果依赖**；
4. **提出 slice-first、replayable 的长循环压缩策略**；
5. **由确定性程序分析自动构题和标注，降低人工与 teacher-LLM 成本**；
6. **使用 first divergence、slice edge-F1 和 replay success 等过程指标**；
7. **联合评估代码生成、输入输出推理和中间执行状态推理**；
8. **通过等价实现和输出碰撞样本检测 superficial execution**。

## 19. 风险与待解决问题

### 19.1 Python 动态特性

反射、动态属性、monkey patch、生成器、闭包、decorator 和 C 扩展会增加准确 CFG 和状态跟踪难度。MVP 应明确支持边界，并记录 unsupported reason，而不是静默生成不完整轨迹。

### 19.2 插桩语义污染

AST 改写和 `print` 插桩可能改变行号、异常、性能或对象行为。优先使用不修改源程序语义的 tracing hook；如需改写，应做原程序与插桩程序的 differential execution validation。

### 19.3 状态序列化副作用

调用自定义 `repr`、属性 getter 或迭代器可能产生副作用。serializer 应避免执行用户自定义逻辑，并设置深度、大小和时间限制。

### 19.4 动态切片精度

只根据执行行号和变量名构建依赖可能不够精确，特别是容器和别名。应逐步引入 field/index-sensitive location，而不是把整个对象视为一个变量。

### 19.5 压缩任务混淆

如果压缩格式过于复杂，评测可能同时测量“理解压缩语法”和“理解程序”。需要提供格式示例、schema、解压器，并设置无压缩基线。

### 19.6 数据污染

HumanEval 等经典数据可能出现在模型训练语料中。应加入较新的题目、语义保持变换、多实现、隐藏输入和生成式结构样本，并单独报告受污染风险。

## 20. 最终结论

本项目的关键不应是再增加一种 line-level 中间变量问答，而应建立一个**结构化、唯一定位、可回放、可压缩、可因果判分**的执行表示。

最推荐的技术路线是：

```text
基本块定义控制流结构
+ 动态 occurrence 定义具体执行位置
+ state delta 定义状态变化
+ dynamic dependence 定义真实因果关系
+ region/loop episode 定义层次结构
+ slice-first compression 解决长轨迹
```

动态切片不是 code 或 block 的替代品，而是建立在它们之上的查询与因果层。原始执行轨迹必须作为无损 oracle 保存；模型可见表示则可以根据任务使用 block、delta、slice 和循环摘要进行压缩。

第一版应从 Python MVP 开始，将数据构造和答案生成完全交给确定性执行与程序分析工具，LLM 只作为被评测对象。这样既能降低 API 成本，也能避免 teacher LLM 带来的人工启发式和标注不稳定性，并为后续扩展到训练、代码修复和多语言评测提供统一基础。

## 参考文献

1. Chen, J., Pan, Z., Hu, X., Li, Z., Li, G., & Xia, X. *Reasoning Runtime Behavior of a Program with LLM: How Far Are We?* ICSE 2025. 本地文件：[ICSE2025_Reasoning_LLM.pdf](../../课题/ICSE2025/ICSE2025_Reasoning_LLM.pdf)
2. Agrawal, H., & Horgan, J. R. *Dynamic Program Slicing*. PLDI 1990. <https://www.cs.purdue.edu/homes/xyzhang/spring07/Papers/p246-agrawal.pdf>
3. Yadavally, A., Li, Y., & Nguyen, T. N. *Predictive Program Slicing via Execution Knowledge-Guided Dynamic Dependence Learning*. FSE 2024. <https://aashishyadavally.github.io/assets/pdf/pub-fse2024.pdf>
4. Ni, A., et al. *NExT: Teaching Large Language Models to Reason about Code Execution*. 2024. <https://arxiv.org/abs/2404.14662>
5. Liu, C., et al. *Code Execution with Pre-trained Language Models*. Findings of ACL 2023. <https://aclanthology.org/2023.findings-acl.308/>
6. Ding, Y., et al. *TRACED: Execution-aware Pre-training for Source Code*. ICSE 2024. <https://conf.researchr.org/details/icse-2024/icse-2024-research-track/31/TRACED-Execution-aware-Pre-training-for-Source-Code>
7. Armengol-Estapé, J., et al. *What I Cannot Execute, I Do Not Understand: Training and Evaluating LLMs on Program Execution Traces*. 2025. <https://arxiv.org/abs/2503.05703>
8. Wang, J., Xie, X., Hu, Q., Liu, S., & Li, Y. *Do Code Semantics Help? A Comprehensive Study on Execution Trace-Based Information for Code Large Language Models*. Findings of EMNLP 2025. <https://aclanthology.org/2025.findings-emnlp.548/>
9. Liu, C., Chen, Y., & Jabbarvand, R. *Assessing Coherency and Consistency of Code Execution Reasoning by Large Language Models*. ICSE 2026. <https://arxiv.org/abs/2510.15079>
10. Gao, J., et al. *CoRE: A Fine-Grained Code Reasoning Benchmark Beyond Output Prediction*. Findings of ACL 2026. <https://aclanthology.org/2026.findings-acl.460/>
11. Wang, H., Li, R., Sha, L., & Zhang, J. M. *StepCodeReasoner: Aligning Code Reasoning with Stepwise Execution Traces via Reinforcement Learning*. 2026. <https://arxiv.org/abs/2605.11922>
12. LLVM Project. *Programmer's Manual: BasicBlock*. <https://llvm.org/docs/ProgrammersManual.html>
13. LLVM Project. *Loop Terminology and Canonical Forms*. <https://llvm.org/docs/LoopTerminology.html>
14. Kini, D., et al. *Data Race Detection on Compressed Traces*. 2018. <https://arxiv.org/abs/1807.08427>
