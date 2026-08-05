# 面向代码大模型全面评测的中间执行状态表示调研

## 1. 调研目标与结论

本调研面向一个包含三种能力粒度的代码理解评测框架：代码生成、输入/输出推理和中间执行状态推理。重点研究第三种能力，回答四个问题：

1. 中间执行状态应以代码行、基本块、执行轨迹还是动态切片表示？
2. block 应如何确定性划分？
3. 循环和长执行轨迹如何压缩而不破坏正确性？
4. 如何避免 REval 中人工规则和模型 API 成本过高的问题？

核心结论是：**不应在 code、block 和动态切片之间三选一，而应采用层次化混合表示**：

```text
静态结构层：AST / CFG / Basic Block / SESE Region
动态事实层：带 occurrence 的 block/operation 执行事件与状态增量
动态因果层：Dynamic Dependence Graph 与 criterion-specific dynamic slice
模型视图层：由无损事实层确定性生成的压缩表示
```

其中，基本块负责稳定描述控制流，动态事件负责区分循环中同一语句的不同执行实例，动态依赖图负责表达控制流与数据流的因果关系。完整原始轨迹作为无损 oracle 保存；循环压缩只影响模型可见视图，不能替代 oracle。

## 2. 代表性顶会与重要工作

本调研优先选择 PLDI、ICSE、FSE、ICML 等程序语言、软件工程和机器学习顶会工作，同时补充与当前代码 LLM 评测直接相关的最新研究。

| 工作 |  venue | 核心表示 | 对本项目的启示 |
|---|---|---|---|
| The Program Structure Tree | PLDI 1994 | CFG 上 canonical SESE region 的层次树 | block 不应只停留在平坦 basic block，应进一步组织为唯一的层次区域 |
| Whole Program Paths | PLDI 1999 | basic-block/path ID 序列经 SEQUITUR 压缩为 grammar DAG | 完整控制流，包括循环和跨过程路径，可以无损压缩 |
| Precise Dynamic Slicing Algorithms | ICSE 2003，Distinguished Paper | 每个动态语句实例及其动态依赖构成 DDG | 动态状态必须带 occurrence；切片标准必须指向具体执行实例 |
| Cost Effective Dynamic Program Slicing | PLDI 2004 | 可快速遍历的 compact DDG | 不需要显式保存所有重复依赖，可以在保持精确切片的前提下压缩依赖表示 |
| Using Compressed Bytecode Traces for Slicing Java Programs | ICSE 2004 | 控制流与内存访问轨迹的 SEQUITUR 压缩 | 压缩轨迹仍可直接做反向数据/控制依赖分析，无须先完全解压 |
| Dynamic Slicing Long Running Programs through Execution Fast Forwarding | FSE 2006 | checkpoint、event log、meta-slice、deterministic replay | 对极长运行应采用切片驱动重放，而不是一次性记录所有细节 |
| TRACED | ICSE 2024 | 源代码、输入、覆盖标签和程序状态联合编码 | 证明控制流/状态监督能改善代码语义表示，但其标签粒度不适合作为完整 oracle |
| NExT | ICML 2024 | 将变化变量作为源码行后的 inline trace comments | 对 LLM 友好且节省 token，但省略循环中间轮次会丢失严格可判分性 |
| REval | ICSE 2025 | CCP、PSP、EPP、OP 四类 statement-level probe | 建立了中间行为评测入口，但 statement occurrence、复杂状态、自动选点和循环处理不足 |
| CES | ICSE 2026 | loop/branch/return checkpoints、coherence、divergence point | 支持在基本块边界做执行模拟，并用首次分歧替代单一最终准确率 |

### 2.1 PLDI 1994：Program Structure Tree 解决 block 的层次划分

Johnson、Pearson 和 Pingali 提出的 Program Structure Tree（PST）以 CFG 中的 single-entry single-exit（SESE）区域为基础。canonical SESE regions 之间只会嵌套、顺序连接或互不相交，因此可组织成唯一的层次树，并可在线性时间内构造。

该工作说明“block”至少有两个层次：

- basic block：内部没有控制转移，单入口、单出口；
- SESE region：由若干 basic blocks 构成，可对应分支、循环或其他结构化区域。

对评测框架而言，只使用平坦 basic blocks 会丢失“这是某个循环体/分支体”的结构信息；只使用 AST block 又无法准确描述短路、异常和实际跳转。因此应以 CFG basic block 为底层单位，以 PST/SESE region 构建上层结构。

来源：Richard Johnson, David Pearson, Keshav Pingali, *The Program Structure Tree: Computing Control Regions in Linear Time*, PLDI 1994，DOI: 10.1145/178243.178258。

### 2.2 PLDI 1999：Whole Program Paths 解决控制流长轨迹压缩

Larus 将程序动态控制流表示为连续执行的 acyclic path/basic-block path ID 序列，再使用在线 SEQUITUR 算法把重复序列压缩为上下文无关文法，最终以 grammar DAG 表示完整 Whole Program Path。

其优势是：

- 控制流轨迹是无损的；
- 能跨越循环和函数边界；
- 重复循环自然形成递归/重复 grammar rules；
- 可以直接在压缩 DAG 上统计 hot subpaths 和动态上下文。

局限是它主要保存控制流，不保存变量值、内存位置和动态数据依赖。因此适合作为本项目的 control trace compression 层，而不是完整的执行状态表示。

来源：James R. Larus, *Whole Program Paths*, PLDI 1999。

### 2.3 ICSE 2003 与 PLDI 2004：动态依赖图解决唯一因果表示

精确动态切片的关键不是“某一源码行是否出现过”，而是“该源码行的第几次动态执行影响了当前值”。因此动态节点应表示为：

```text
dynamic-node = (run_id, frame_id, static_node_id, occurrence_id)
```

切片标准也必须精确到：

```text
criterion = (dynamic_node, memory_location, before_or_after)
```

ICSE 2003 的 Precise Dynamic Slicing Algorithms 比较了 full preprocessing、no preprocessing 和 limited preprocessing 三类精确算法，表明精确切片虽然需要动态依赖信息，但可以按查询需求构造和遍历。PLDI 2004 的 Cost Effective Dynamic Program Slicing 进一步利用重复动态依赖之间的冗余，构造紧凑且可快速遍历的 DDG；论文报告其 compact graph 为 20–210 MB，而传统完整表示达到 0.84–1.95 GB。

这条路线对本项目最重要，因为 DDG 同时提供：

- 实际执行的控制依赖；
- last-definition 到 use 的动态数据依赖；
- 循环中不同 occurrence 的精确区别；
- 可自动生成的因果型评测标签。

### 2.4 ICSE 2004：直接在压缩轨迹上做切片

Wang 和 Roychoudhury 将 Java bytecode trace、分支目标和内存引用信息在线压缩，并在压缩轨迹上反向遍历、即时恢复数据/控制依赖，从而计算动态切片，不要求先恢复完整原始轨迹。

这给出一个重要工程原则：

> 压缩格式不能只是展示文本，而应支持索引、局部展开、反向遍历和依赖查询。

本项目的循环压缩结构也应支持：

- 定位第 k 次迭代；
- 查询某个变量版本的 last writer；
- 展开某个 loop episode；
- 在不展开全部迭代的情况下计算 criterion-specific slice。

来源：Tao Wang, Abhik Roychoudhury, *Using Compressed Bytecode Traces for Slicing Java Programs*, ICSE 2004，DOI: 10.1109/ICSE.2004.1317473。

### 2.5 FSE 2006：长执行不应全部重放

Execution Fast Forwarding 将长运行划分为 checkpoint intervals，利用 event dependence 和 meta-slicing 从事件日志中删除与目标执行区域无关的事件，再确定性重放目标区域。论文报告空间需求可降低 72 到 44,490 倍。

其思想可转化为 benchmark 数据生成策略：

1. 首次运行只保存低成本 event log、checkpoint 和必要摘要；
2. 确定 probe/criterion 后计算 event-level relevance；
3. 只重放相关区间并采集细粒度 DDG；
4. 生成该 criterion 的中间状态和动态切片标签。

这样可以避免对每个输入都永久保存极细粒度全轨迹。

来源：Xiangyu Zhang, Sriraman Tallam, Rajiv Gupta, *Dynamic Slicing Long Running Programs through Execution Fast Forwarding*, FSE 2006，DOI: 10.1145/1181775.1181786。

### 2.6 ICML 2024 NExT：LLM 友好表示的优点和局限

NExT 将执行结果放在对应源码行的 inline comments 中，只记录该行执行后发生变化的变量，并按动态顺序给状态编号。它对循环仅保留部分迭代，中间轮次用省略号表示。论文报告，在 2K context 下，约 95% 的样本可容纳 inline representation，而传统 scratchpad 只能容纳约 60%。

这种表示非常适合作为模型输入或训练 view，因为代码和状态位置对齐、冗余较少。但是它不适合作为唯一 oracle：

- 省略的循环轮次不可恢复；
- `repr` 不保证复杂对象的跨运行 canonical equality；
- 变量变化说明了结果，却没有显式说明 data/control cause；
- 同一源码行的多次执行虽有序号，但没有稳定 frame、block 和 memory-location identity。

因此本项目可以生成 NExT-like view，但底层必须保留更严格的结构化表示。

来源：Ansong Ni et al., *NExT: Teaching Large Language Models to Reason about Code Execution*, ICML 2024。

### 2.7 REval 与 CES：从单点 probe 转向完整、可检查的执行模拟

REval 用 CCP、PSP、EPP、OP 覆盖控制流、变量状态和输出，并在 CCP/EPP 构造时优先选择 basic block 末尾语句。但是其核心不足包括：

1. 语句以 source line/index 标识，没有把循环中的不同 occurrence 作为一等对象；
2. EPP 对同一语句可能接受多个 next statements，弱化了给定输入下本应唯一的路径；
3. PSP 通常只选择一个易序列化变量，复杂对象、别名、堆位置被过滤；
4. 语句和变量选择依赖人工启发式规则；
5. 长循环通过选 block 末尾而被回避，并未真正表示和压缩；
6. CCP→PSP→EPP→OP 的固定递进假设不总是对应实例级真实因果关系；
7. 闭源 API 因预算限制没有重复实验。

CES 将 probe 放到 loop、branch 和 return 等基本块边界，要求模型模拟一组运行属性，并检测 coherence violation 和 simulation divergence point。这比单独问一个随机变量更接近完整执行模拟，也说明“首次分歧位置”应成为本项目的核心指标。

## 3. 五类候选表示的比较

| 表示 | 唯一性/确定性 | 控制流 | 数据流 | 长循环 | LLM 友好性 | 适合角色 |
|---|---:|---:|---:|---:|---:|---|
| source line + full state | 低到中 | 弱 | 中 | 差 | 中 | 不推荐作为底层表示 |
| inline changed-variable trace | 中 | 中 | 隐式 | 依赖丢弃/省略 | 高 | 模型输入 view |
| basic-block path | 高 | 强 | 无 | 可用 grammar/RLE 压缩 | 高 | 控制流 oracle |
| operation-level event trace | 高 | 强 | 可记录 | 原始体积大 | 中 | 无损事实层 |
| DDG / dynamic slice | 高 | 强 | 强 | criterion-specific，通常更短 | 中 | 因果 oracle 和预测目标 |

这里的“唯一”应谨慎定义：它不是“语义等价程序具有同一表示”，而是固定以下条件后，序列化结果唯一：

```text
(source_hash, language_frontend_version, instrumentation_version,
 runtime_version, input_encoding, execution_seed)
```

跨等价实现的中间变量和 block 往往不存在一一映射，不能强行使用同一 block ID。等价实现一致性应通过 I/O、规范化语义角色或各实现自己的因果轨迹分别评价。

## 4. 推荐表示：Hierarchical Dynamic Dependence Trace

建议将底层表示暂命名为 HDDT（Hierarchical Dynamic Dependence Trace）。

### 4.1 静态结构层

```json
{
  "program_id": "sha256:...",
  "functions": [{
    "function_id": "F000",
    "regions": [
      {"region_id": "R003", "kind": "loop", "entry": "B004", "exit": "B009"}
    ],
    "blocks": [
      {
        "block_id": "B004",
        "operations": ["O011", "O012"],
        "successors": [
          {"to": "B005", "label": "true"},
          {"to": "B009", "label": "false"}
        ]
      }
    ]
  }]
}
```

ID 生成策略建议为：

- function 按 qualified name 和 source span 排序；
- block 按 CFG reverse postorder 编号，冲突时按 source span 和 operation index 排序；
- region 使用 canonical SESE/PST 顺序；
- source line 只作为展示元数据，不作为唯一 identity。

### 4.2 动态事件层

```json
{
  "event_id": "run01/frame02/B004#17/exit",
  "static_block": "B004",
  "occurrence": 17,
  "taken_edge": "B004->B005:true",
  "delta": [
    {"loc": "local:i", "version": 18, "type": "int", "before": "17", "after": "18"},
    {"loc": "local:sum", "version": 18, "type": "int", "before": "153", "after": "171"}
  ],
  "data_deps": ["run01/frame02/B006#16/exit"],
  "control_deps": ["run01/frame02/B004#17/entry"]
}
```

状态应使用 canonical serializer：

- primitive 带显式类型；
- float 使用 IEEE bits 或 hex representation；
- dict/set 按 canonical key bytes 排序；
- list/tuple 保留顺序并区分类型；
- heap object 按首次可达的确定性遍历分配 object ID；
- alias 使用同一 object ID；
- cycle 使用 `$ref`；
- 外部资源、文件和网络状态作为 typed side-effect event，而不是直接调用 `repr`。

### 4.3 动态依赖层

动态数据依赖记录实际 last-definition → use；动态控制依赖记录本次执行中使节点被执行的 predicate occurrence。对于 heap/container，需要把 location 细化为：

```text
root variable / object-id / field | index | key
```

切片查询形式：

```json
{
  "run": "run01",
  "event": "run01/frame02/B004#17/exit",
  "location": "local:sum@18",
  "phase": "after"
}
```

在动态 data/control dependence edges 上反向可达即可生成 node slice 和 edge slice。

## 5. block 的推荐划分算法

### 5.1 第一层：semantic operations

首先把语言语法降低为可执行语义操作。Python 中不能直接认为“一行代码就是一个操作”，因为以下结构可能在一行内产生多个控制决策或副作用：

- `a and b`、`a or b`；
- conditional expression；
- chained comparison；
- comprehension/generator；
- function call 与异常边；
- 多目标赋值、属性或下标写入。

### 5.2 第二层：CFG basic blocks

使用标准 leader 规则：

1. 函数入口是 leader；
2. 任意 branch/jump/exception edge 的目标是 leader；
3. terminator 后的下一操作是 leader；
4. loop header、latch、exit、handler entry、call-return continuation 显式成为边界；
5. block 内保持单入口，只有末尾操作改变控制流。

不要为了“语义看起来重要”而人工切 block。状态 probe 可以放在 block 内 operation 后，但不能反过来改变 block 定义。

### 5.3 第三层：SESE regions / PST

在 basic-block CFG 上计算 canonical SESE regions，构成 region tree：

```text
Function
  Sequence Region
    Branch Region
    Loop Region
      Nested Branch Region
```

LLM prompt 可主要显示 region/block 层，严格判分仍落到 operation/event 层。这兼顾可读性、唯一划分和精度。

## 6. 循环和长执行轨迹的压缩设计

### 6.1 首要原则：oracle 无损，view 可压缩

必须区分：

- **Oracle representation**：足以回放、定位第 k 次迭代和重新计算任意 slice；
- **Model view**：在固定 token budget 下生成的压缩文本或 JSON；
- **Target representation**：模型实际需要预测的字段。

不能把 `first two + ... + last two` 当作 oracle，否则无法判断模型对省略迭代的预测是否正确。

### 6.2 控制流压缩

每次迭代建立 path signature：

```text
signature = ordered block IDs + taken edge labels + call/return shape
```

建议分两级：

1. MVP 使用自然循环边界上的 maximal run-length encoding；
2. 长期使用 WPP/SEQUITUR，将 path token stream 压缩为 grammar DAG。

RLE 算法和 tie-breaking 完全确定，适合追求唯一表示；SEQUITUR 压缩率更高，但必须固定算法版本和 rule-numbering 规范。

### 6.3 状态压缩

按以下优先级：

1. 只保存 changed locations；
2. 容器使用 patch，如 `append`、`set(index)`、`delete(key)`；
3. 只对 slice-relevant locations 生成模型视图；
4. 自动检测 induction/reduction recurrence；
5. recurrence 必须通过 concrete replay 验证后才能替换 delta stream。

示例：

```json
{
  "loop": "R003",
  "trip_count": 1000,
  "path_groups": [
    {"iterations": [1, 1000], "signature": "P001", "repeat": 1000}
  ],
  "summaries": [
    {"loc": "i", "formula": "i(k)=k", "domain": "1<=k<=1000", "verified": true},
    {"loc": "sum", "formula": "sum(k)=k*(k+1)/2", "domain": "1<=k<=1000", "verified": true}
  ]
}
```

无法证明或验证 summary 时必须回退到 exact delta/RLE，不应让 LLM 生成摘要作为 ground truth。

### 6.4 slice-first, compress-second

对某个 output/state criterion，先计算动态切片，再压缩切片内的 loop episodes。大量与目标无关的迭代、变量和调用会自然消失；对 reduction 等确实依赖全部迭代的情况，再使用 recurrence 或 grammar compression。

这是比统一截断前 N 个事件更可靠的策略，因为保留的是因果相关性，而不是时间邻近性。

### 6.5 极长执行：checkpoint + selective replay

当完整细粒度 trace 仍不可承受时：

```text
coarse event log + checkpoints
    -> choose criterion
    -> meta-slice relevant events/intervals
    -> deterministic selective replay
    -> collect fine-grained HDDT/DDG
```

该方案将 FSE 2006 的 execution fast forwarding 转化为 benchmark oracle 构建机制。

## 7. 自动构建与 API 成本控制

### 7.1 ground truth 构建不使用 LLM

以下步骤均应由解析器、执行器和模板完成：

```text
correct program + tests
  -> semantic lowering / CFG / PST
  -> instrumented sandbox execution
  -> raw events and canonical values
  -> dynamic data/control dependencies
  -> automatic criteria enumeration
  -> dynamic slices and loop compression
  -> schema validation and replay validation
  -> deterministic question rendering
```

criterion 可以自动从以下位置采样：

- branch/loop predicate；
- block exit 的 live-out variables；
- return/exception values；
- 发生非平凡更新的 heap/container location；
- output backward slice 上的定义；
- 首次/末次迭代、path signature 变化迭代、边界迭代。

这样消除 REval 的逐类人工选点规则，也不需要 teacher LLM 插入 probe。

### 7.2 输入生成

优先组合：

- benchmark 原始 tests；
- property-based generation；
- coverage-guided fuzzing；
- concolic/symbolic execution；
- branch-distance 和边界值生成。

LLM 生成输入只能作为补充，且必须经过执行、覆盖和去重过滤。

### 7.3 模型评测 API 降本

- 同一 `(program, input)` 的多个 probe 可放在一次结构化响应中；
- 主实验 temperature 设为确定性/最低随机性配置；
- 只在分层抽样子集上进行多次采样和独立 probe 测试；
- 同时报告 accuracy、input tokens、output tokens、calls 和 wall time；
- 闭源模型做预算分层，全部模型跑 core set，代表模型跑 extended set；
- 解析失败与语义错误分开统计，避免为修复格式反复调用 API。

## 8. 推荐评测任务与指标

### 8.1 中间执行状态任务

| 任务 | 目标 | 核心能力 |
|---|---|---|
| Next Transition | next block、taken edge、state delta | 局部控制流和状态更新 |
| k-Step State | k 个 event/iteration 后的状态 | 长程状态跟踪 |
| Region Exit State | branch/loop/call region 的 exit state | 层次化程序理解 |
| Loop Summary | trip count、path groups、verified recurrence | 循环抽象能力 |
| Dynamic Dependency | 预测 last-writer/control-parent edges | 控制流与数据流因果理解 |
| Dynamic Slice | 预测 criterion 的 node/edge slice | 全局因果理解 |
| Full Compressed Trace | 预测 HDDT 的模型视图 | 完整执行模拟 |

### 8.2 指标

建议至少报告：

- next-block / edge accuracy；
- type、location、value accuracy；
- state-delta precision、recall、F1；
- control-path edit distance；
- dynamic-dependence edge F1；
- dynamic-slice node F1 与 edge F1；
- first divergence event；
- longest correct prefix；
- loop trip-count 和 region-exit-state accuracy；
- compressed trace replay validity；
- `P(output correct | trace correct)` 与 suspiciously-correct-output rate；
- accuracy per API call / per 1K tokens。

最终输出正确但中间轨迹错误，应单独归类为 superficial/suspicious success，而不是只计一次正确。

## 9. 建议的第一版实现范围

第一版建议限制为 Python、单线程、单进程、可控外部副作用：

1. AST/bytecode semantic operations；
2. source-level CFG basic blocks；
3. natural loop + canonical SESE/PST regions；
4. 带 frame 和 occurrence 的 block-entry/block-exit events；
5. locals、arguments、return、exception 和常见容器 patch；
6. dynamic last-definition/use 与 actual control dependence；
7. basic RLE loop compression；
8. criterion-specific backward dynamic slicing；
9. Next Transition、Region Exit State、Dynamic Slice 三个核心任务；
10. exact replay、first divergence 和 slice edge F1 三类核心指标。

后续再扩展 object alias、第三方库摘要、递归、generator、async 和多语言。这样可以先验证表示选择与评测有效性，而不会在第一版陷入完整 Python heap model 和跨语言 IR 的工程复杂度。

## 10. 可形成的研究问题

- **RQ1：表示粒度。** operation、basic block、SESE region 和 dynamic slice 哪种表示最能区分模型能力？
- **RQ2：压缩保真度。** RLE、grammar、delta、recurrence 和 slice-first compression 对 token、准确率和 first divergence 有何影响？
- **RQ3：过程与结果一致性。** 正确 output 是否由正确 control/data dependencies 支撑？
- **RQ4：实现不变性。** 功能等价但结构不同的实现上，模型的 I/O 与因果推理能力是否稳定？
- **RQ5：成本。** 自动构建和联合 probe 能否在保持区分度的同时显著降低人工与 API 成本？

## 11. 最终选择

综合顶会工作，推荐选择：

> **PST/SESE 层次化 basic blocks + occurrence-aware dynamic events + compact DDG/dynamic slice + lossless loop grammar/RLE + criterion-aware state deltas。**

其中：

- block 划分由 CFG 和 canonical SESE 算法决定，不由人工语义判断决定；
- 循环压缩优先使用确定性结构压缩和 verified summaries，不直接丢弃中间轮次；
- dynamic slice 是 ground truth/预测目标，不默认作为输入泄露给模型；
- LLM 友好的 inline trace 仅作为由底层 oracle 派生的实验 view；
- 所有标签由程序执行与依赖分析自动生成，LLM API 仅用于被测模型推理。

这一表示比 REval 的 statement-level probe 更精确，比完整 line-state scratchpad 更节省，比单独 basic-block path 包含更多数据因果信息，也比自然语言执行摘要更容易唯一序列化和自动判分。

## 参考文献与资料

1. Johnson, R., Pearson, D., Pingali, K. *The Program Structure Tree: Computing Control Regions in Linear Time*. PLDI 1994. https://iss.oden.utexas.edu/Publications/Papers/PLDI1994.pdf
2. Larus, J. R. *Whole Program Paths*. PLDI 1999. https://www.cs.princeton.edu/courses/archive/fall99/cs597d/restricted/papers/larus_pldi99.pdf
3. Zhang, X., Gupta, R., Zhang, Y. *Precise Dynamic Slicing Algorithms*. ICSE 2003. https://people.cs.pitt.edu/~zhangyt/research/icse03.pdf
4. Zhang, X., Gupta, R. *Cost Effective Dynamic Program Slicing*. PLDI 2004. https://www.cs.ucr.edu/~gupta/research/Publications/Comp/pldi04.pdf
5. Wang, T., Roychoudhury, A. *Using Compressed Bytecode Traces for Slicing Java Programs*. ICSE 2004. DOI: 10.1109/ICSE.2004.1317473
6. Zhang, X., Tallam, S., Gupta, R. *Dynamic Slicing Long Running Programs through Execution Fast Forwarding*. FSE 2006. https://www.cs.purdue.edu/homes/xyzhang/fall07/Papers/fse06.pdf
7. Ding, Y. et al. *TRACED: Execution-aware Pre-training for Source Code*. ICSE 2024. https://conf.researchr.org/details/icse-2024/icse-2024-research-track/31/TRACED-Execution-aware-Pre-training-for-Source-Code
8. Ni, A. et al. *NExT: Teaching Large Language Models to Reason about Code Execution*. ICML 2024. https://proceedings.mlr.press/v235/ni24a.html
9. Chen, J. et al. *Reasoning Runtime Behavior of a Program with LLM: How Far Are We?* ICSE 2025. DOI: 10.1109/ICSE55347.2025.00012
10. Liu, C., Chen, Y., Jabbarvand, R. *Assessing Coherency and Consistency of Code Execution Reasoning by Large Language Models*. ICSE 2026. https://arxiv.org/abs/2510.15079
11. Armengol-Estapé, J. et al. *What I Cannot Execute, I Do Not Understand: Training and Evaluating LLMs on Program Execution Traces*. 2025 preprint. https://arxiv.org/abs/2503.05703

