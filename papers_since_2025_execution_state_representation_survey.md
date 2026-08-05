# 2025 年以来 LLM 中间执行状态与动态轨迹表示调研

> 调研日期：2026-08-03  
> 研究目标：为“模型代码理解能力全面评测框架”的第 3 粒度——中间执行状态推理——设计自动、结构化、可唯一定位且低成本的表示；同时兼顾第 2 粒度输入—输出推理。  
> 论文范围：2025 年至今。重点分析 9 篇论文，其中 8 篇已进入 ICSE、ACL/EMNLP 正式会议或 Findings，1 篇为与长轨迹压缩高度相关的预印本。正式发表状态与预印本严格区分。

## 1. 结论先行

目前没有一篇论文同时解决“唯一定位、控制/数据流、自动构建、长循环压缩、低 API 成本”五个问题。最适合本项目的不是复制某一篇论文，而是组合四条技术路线：

1. 用 **ORCA/CES 的 CFG 与块级检查点**定义执行骨架；
2. 用 **动态切片**从待预测的变量或输出反向选择真正相关的执行实例；
3. 用 **Concise trace / dynamic scratchpad**只保存状态增量，并对重复循环进行分段、跳步和可验证摘要；
4. 用 **TAAF 的时间索引和查询相关子图**保存完整轨迹，但只向模型序列化与当前问题有关的最小子图。

建议最终表示为“**时间索引的动态程序依赖轨迹图**”（Time-indexed Dynamic Program Dependence Trace Graph，简称 T-DPDTG），而不是自然语言问题、纯行号序列或完整变量快照。

核心原则是：

- 原始轨迹在本地无损保存，供判分和按需展开；
- 提示词中的轨迹是查询相关的有损视图，但每一项都能映射回唯一的动态执行实例；
- 数据生成和 oracle 计算全部由解释器、CFG、动态依赖与序列化器完成；
- LLM 只作为被测对象，不参与真值生成。这样可以同时降低人工构造和教师模型 API 成本。

## 2. 论文选择与横向比较

| # | 论文 | 发表状态 | 核心表示/方法 | 对 block 的处理 | 对长轨迹/循环的处理 | 自动化与成本 | 对本项目价值 |
|---|---|---|---|---|---|---|---|
| 1 | REval: *Reasoning Runtime Behavior of a Program with LLM: How Far Are We?* | ICSE 2025 Research Track | CCP、PSP、EPP、OP 四类运行行为问题；增量一致性 | 基于 CFG 将路径拆成 block，再挑语句提问 | 避免枚举长循环，偏向选择 block 最后语句 | tracer 自动取真值，但问题构造含人工规则；闭源 API 复现实验受预算限制 | 基线任务体系，但定位和构造方式需升级 |
| 2 | ORCA: *Planning a Large Language Model for Static Detection of Runtime Errors in Code Snippets* | ICSE 2025 Research Track | LLM 在 CFG 上按 Observation–Reasoning–Action 遍历，逐块维护符号表 | CFG basic block 是基本推理单元 | 未提出系统的循环摘要；逐块 API 交互仍随路径增长 | CFG 自动构建；论文 artifact 报告全实验约 4.50 美元，但仅支持单方法 CFG 映射 | block 划分和状态转移接口最直接的参考 |
| 3 | *What I Cannot Execute, I Do Not Understand* | 2025 arXiv 预印本 | full scratchpad、changed-variable compact scratchpad、单一当前状态 dynamic scratchpad | 行级或 Python bytecode 指令级 | dynamic scratchpad 可预测未来 N 步；显式保存 iterator 计数；长执行可跳步 | 执行轨迹自动采集并用于本地微调，避免逐样本闭源问答 | 长循环压缩最有针对性的工作，但尚未正式发表 |
| 4 | *Do Code Semantics Help?* | Findings of EMNLP 2025 | Trace Adapter 统一产生 Scratchpad、NExT、SemCoder、CodeExecutor、Concise 五种表示 | 主要是逐行表示，不建立唯一的块实例/依赖图 | Concise 只记录发生变化的变量，降低冗余 | 编译/执行和格式转换自动；SemCoder 自然语言版仍需要 LLM | 直接证据：简洁增量表示通常优于堆叠完整状态，但 trace 并非越多越好 |
| 5 | *Code Execution as Grounded Supervision for LLM Reasoning* | EMNLP 2025 Main | Snoop 调试器生成行序、调用/返回、局部变量更新，再由 LLM 翻译成自然语言 CoT | 行级执行步骤 | 通过翻译减少重复和 overthinking，但没有结构化循环摘要 | 轨迹自动，约 15K 样本；仍用 Qwen3-32B 翻译，且抽样用 o3 检查 | 说明“执行器真值”能替代人工 CoT；也暴露自然语言翻译的 API 与非唯一性问题 |
| 6 | CES: *Assessing Coherency and Consistency of Code Execution Reasoning by Large Language Models* | ICSE 2026 Research Track | 在 loop、branch、return 等位置预测程序属性；coherence rule、divergence point、跨测试一致性 | 控制结构边界天然形成检查点，可近似块入口/出口 | 对循环逐次保持 flow-sensitive 模拟；没有专门压缩表示 | 自动执行与规则判定；无需人工逐题判断模型过程是否自洽 | 适合定义块级一致性、首个偏离点和跨路径指标 |
| 7 | CoRE: *A Fine-Grained Code Reasoning Benchmark Beyond Output Prediction* | Findings of ACL 2026 | implementation invariance + process transparency；中间 probes 覆盖 Arithmetic/Logic/State/Boundary | probe 绑定到实现中的具体执行点，但仍是自然语言问答 | 没有循环压缩；probe 数量受人工/LLM生成成本制约 | 多个 LLM 生成等价实现和 probes，再执行和专家核验 | 必须加入“等价实现不变性”，防止只测模板记忆 |
| 8 | TAAF: *A Trace Abstraction and Analysis Framework Synergizing Knowledge Graphs and LLMs* | ICSE 2026 Research Track | 原始 trace → 时间索引 State System → 查询相关 KG → LLM 答案 | 系统事件而非源码 block，但状态转移建模可迁移 | 数百万事件先建立区间索引，再按时间窗口和查询抽取最小子图 | 索引与图构建确定性完成；只把小子图交给 LLM | 为“完整存储、按需压缩、唯一时间定位”提供最成熟架构 |
| 9 | CoReX: *Context-Aware Refinement-Based Slicing for Debugging Regression Failures* | ICSE 2026 Research Track | 上下文感知、refinement-based 的双版本切片 | 保留理解故障所需上下文，同时剔除不必要的长计算 | 明确针对 slice 中冗长计算与上下文缺失的矛盾 | 程序分析自动完成，不需要 LLM 生成 oracle | 直接指导动态切片视图不能“只追求最小”，要保留可理解上下文 |

说明：表中实际深入分析 9 篇，超过“至少 5 篇”的要求。第 3 篇单独标为预印本，其余均可从正式会议/ACL Anthology 页面核验录用状态。

## 3. 逐篇分析

### 3.1 REval（ICSE 2025）：中间状态评测的直接基线

论文：[会议页](https://conf.researchr.org/details/icse-2025/icse-2025-research-track/32/Reasoning-Runtime-Behavior-of-a-Program-with-LLM-How-Far-Are-We-)；[PDF](https://ginolzh.github.io/papers/ICSE2025_Reasoning_LLM.pdf)

REval 将运行时推理拆成四类：代码覆盖预测（CCP）、程序状态预测（PSP）、执行路径预测（EPP）和输出预测（OP），并通过 Incremental Consistency 检查“前面过程预测错误但最终答案正确”的可疑情况。论文报告模型在 Runtime Behavior Reasoning 上平均准确率为 44.4%，平均 IC 分数只有 10.3，说明只看最终输出会明显高估代码理解。

与本项目最相关的是其自动 tracer 和 CFG-based block 处理。不过 REval 的 block 主要用于控制题量和选择提问位置，不是一个完整的动态块实例表示：

- 同一行在循环中多次出现时，行号不能唯一确定是哪一次执行；
- PSP 往往只问少量变量，无法完整刻画数据依赖；
- 长循环通过选择末端语句规避，而不是压缩并保留循环过程；
- 题目筛选、复杂对象过滤和检查点选择依赖人工启发式；
- 固定的 CCP→PSP→EPP→OP 链并不等于实例级因果链。

因此，REval 最适合作为“任务与指标基线”，不宜直接采用其问题格式作为统一中间表示。

### 3.2 ORCA（ICSE 2025）：CFG basic block + 符号表

论文：[会议页](https://conf.researchr.org/details/icse-2025/icse-2025-research-track/172/Planning-a-Large-Language-Model-for-Static-Detection-of-Runtime-Errors-in-Code-Snippe)；[官方 artifact/论文](https://github.com/smitpatel910/orca)

ORCA 把程序转换为 CFG，让 LLM 以 Observation–Reasoning–Action 的方式选择下一节点，并在每个块执行后维护 Symbol Table。相比逐行询问，它有三个优势：

1. block 边界由 CFG 构造器确定，较少依赖人工；
2. 控制流选择和数据状态更新在同一步中绑定；
3. runtime error 可以定位到首次错误 block，而不仅是最终输出。

但 ORCA 仍不适合直接当大规模评测数据生成器。其 artifact 明确说明 CFG 工具只能处理单方法，无法映射方法调用之间的 block connection；逐块调用 LLM 也会让成本随动态路径长度增长。其约 4.50 美元的复现实验成本只代表特定数据集和 GPT-3.5 设置，不意味着规模扩张后成本仍低。

可借鉴点：采用 CFG 的 maximal basic block 作为第一层结构；不可照搬点：不要让 LLM 决定真实路径和真实符号表，真值必须由执行器产生。

### 3.3 Execution Tuning（2025 预印本）：动态 scratchpad 与跳步

论文：[PDF](https://arxiv.org/pdf/2503.05703)

该工作比较三种轨迹表示：

- full scratchpad：每一步保存全部变量；
- compact scratchpad：只保存相对上一步发生变化的变量；
- dynamic scratchpad：始终维护一个自包含的“当前状态”，不累计全部历史。

dynamic scratchpad 还可要求模型直接预测 N 步后的状态，从而跳过低价值中间步骤。论文特别指出，丢掉历史后迭代器状态会变得歧义，例如循环值重复时仅看变量值无法判断当前是第几次迭代，因此显式记录 iterator count 和调用栈。实验中，Line-n 在较少步骤下仍可完成长执行，但随着跳步长度增加，变量值预测比控制流和迭代器预测下降更明显。

这给本项目两个直接启示：

- 循环压缩不能只保留变量值，必须保存 `(loop_id, iteration_id)`；
- “跳 N 步”不能由固定 N 决定，应由可验证摘要或查询相关性决定。

它的不足是表示仍主要是 Python `repr` 风格的平面状态，不是规范化对象图，也没有显式的数据依赖边；此外属于预印本，证据等级应低于正式会议论文。

### 3.4 Do Code Semantics Help?（Findings of EMNLP 2025）：五种 trace 表示的实证比较

论文：[ACL Anthology](https://aclanthology.org/2025.findings-emnlp.548/)

论文的 Trace Adapter 自动执行程序并把原始轨迹转为 Scratchpad、NExT、SemCoder、CodeExecutor 和 Concise 五种表示。其中：

- NExT 把状态以内联注释放回代码；
- SemCoder 用自然语言逐行解释；
- CodeExecutor 将逐行状态与代码分开展示；
- Concise 只记录该行发生变化的变量，忽略未变化项。

最重要的负面结论是：多数 trace 表示在 SFT 和 test-time scaling 中并不能稳定带来收益；在 56 个 test-time 对比中有 36 个加入语义信息不优于不加 trace。Concise 相对更稳定，在 14 个比较中的 11 个不差于无 trace 设置。

这否定了“给模型的轨迹越完整越好”。对本项目而言，正确策略是：完整状态只保留在后端 oracle；给模型的是状态增量、依赖切片和必要上下文。还应把 token 数量作为正式指标，避免用更长输入换取表面精度。

### 3.5 Code Execution as Grounded Supervision（EMNLP 2025 Main）：执行器生成可靠过程监督

论文：[ACL Anthology](https://aclanthology.org/2025.emnlp-main.1260/)

该工作用 Snoop 记录函数调用/返回、执行行和局部变量更新，然后让 Qwen3-32B 把原始 trace 翻译成人类风格的自然语言 CoT，构造约 15K 个训练样本。与教师模型直接生成 CoT 相比，执行轨迹为每一步提供可验证依据。论文报告其方法的最终输出正确率和中间步骤正确率分别为 98.3% 和 91.5%，并使推理输出 token 数下降。

它确实减少了人工标注，但没有完全解决成本：自然语言翻译仍需大模型，抽样质量检查还使用了另一个强模型；翻译后的 CoT 也不是唯一表示，同一执行可有多种正确措辞，给自动判分带来困难。

因此，本项目可采用其“执行器优先”原则，但不应把自然语言 CoT 作为 ground truth。自然语言只能是展示层；规范 JSON/图结构才是判分层。

### 3.6 CES（ICSE 2026）：coherence、首个偏离点与跨测试路径一致性

论文：[会议页](https://conf.researchr.org/details/icse-2026/icse-2026-research-track/230/Assessing-Coherency-and-Consistency-of-Code-Execution-Reasoning-by-Large-Language-Mod)；[PDF](https://arxiv.org/pdf/2510.15079)

CES 要求模型在循环、分支和返回等关键位置模拟程序属性，并使用 coherence rules 判断预测是否符合基本执行逻辑，即使具体值已经错误。它还定义 simulation divergence point，定位模型从哪一步开始偏离，并通过 prime path coverage 比较相同或不同测试输入下的强、弱、随机一致性。

相较 REval，CES 的重要进步是评测对象从孤立问答变成连续执行过程。关键控制结构天然对应 block 入口或出口，可定义：

- `next_block` 是否正确；
- `state_delta` 是否与该 block 的语义一致；
- 分支条件与所选 successor 是否一致；
- 错误是否在后续步骤传播；
- 不同输入覆盖同一 prime path 时，模型是否保持相似的正确性。

其局限是状态属性仍偏向预定义类型，没有形成通用的对象、堆和依赖图 schema；循环仍是逐步模拟，规模很大时输入/输出长度会迅速增长。

### 3.7 CoRE（Findings of ACL 2026）：等价实现不变性与过程透明度

论文：[PDF](https://aclanthology.org/2026.findings-acl.460.pdf)

CoRE 从两个轴扩展代码推理评测：

- implementation invariance：对功能等价但词法和结构不同的实现，模型是否保持正确；
- process transparency：通过 Arithmetic、Logic、State、Boundary 四类中间 probes 检查执行过程。

数据包含 60 个问题、255 个候选实现，平均每题约 4.3 个实现；中间 probes 涵盖状态、逻辑、边界和算术。论文进一步提出 Reasoning Consistency Score，将严格输出一致性与过程 fidelity 相乘，以惩罚“输出正确、过程错误”的 superficial execution。

对本项目而言，这意味着评测单元不能只是 `(code, input)`，而应是：

`(semantic_problem_id, implementation_id, input_id, dynamic_query)`。

但 CoRE 的 probe 仍以问题文本绑定到具体实现，生成阶段使用多个 LLM 并经过执行和专家核验，成本仍然不低。更合适的替代是从动态依赖图自动抽取 query，再通过模板序列化；同一个语义 query 可映射到不同实现中的不同动态节点。

### 3.8 TAAF（ICSE 2026）：时间索引、最小查询子图与分层存储

论文：[会议页](https://conf.researchr.org/details/icse-2026/icse-2026-research-track/304/TAAF-A-Trace-Abstraction-and-Analysis-Framework-Synergizing-Knowledge-Graphs-and-LLM)；[PDF](https://arxiv.org/pdf/2601.02632)

TAAF 面向 Linux kernel 等系统级巨型轨迹，提出三层变换：

`T(raw trace) → S(time-indexed State System) → Gq(query-specific KG) → A(answer)`。

State System 用稳定整数标识层次属性路径，并用 history tree 保存值在时间区间内的变化；接到查询后，只提取相关时间窗和实体关系，构造最小、带时间元数据的知识子图，再交给 LLM。论文报告相比原始或展平输入，图接地在部分设置中可提高最多约 31% 的答案准确率。

虽然 TAAF 的节点是 thread、CPU、锁、I/O，而不是源代码 block，但其架构非常适合迁移：

- `quark` 可换成稳定的 `block_id/variable_id/object_id`；
- history interval 可换成变量值或对象字段的有效区间；
- query-specific KG 可换成从动态切片得到的最小程序依赖子图；
- 原始轨迹永不丢失，序列化给模型的只是一个视图。

其局限是系统事件之间的关系较容易预定义，而 Python/Java 源码还涉及别名、堆对象、反射和库调用；迁移时必须补充语言级动态语义。

### 3.9 CoReX（ICSE 2026）：动态切片不是越小越好

论文：[会议页](https://conf.researchr.org/details/icse-2026/icse-2026-research-track/60/CoReX-Context-Aware-Refinement-Based-Slicing-for-Debugging-Regression-Failures)；[PDF](https://people.ece.ubc.ca/~mjulia/publications/2026_CoReX_ICSE.pdf)

CoReX 调研了 8 个国家的 50 多名从业者，指出双版本切片的两个相反问题：为了最小化语句数，切片可能删掉理解故障所需的上下文；为了保持信息传播，又可能保留过长而无助于理解的计算。其 context-aware refinement 思路同时优化缩减率和与开发者预期的一致性。

这对评测 trace 的启示是：纯 backward slice 可能只留下定义—使用链，却删掉分支条件、循环阶段和调用上下文，使模型看到一个难以理解的“骨架”；另一方面，完整传递链又可能包含数百次相同循环。因而需要两层视图：

- **causal core**：严格动态数据/控制依赖切片；
- **context envelope**：每个保留节点周围的 block header、调用点、分支条件、循环摘要和必要源代码窗口。

## 4. 主要方法归纳

### 4.1 表示层面的四类路线

1. **自然语言 probe/CoT**：REval、CoRE、Grounded Supervision。易于给通用 LLM 作答，但不唯一、难自动严格判分，通常还需要人工或教师 LLM。
2. **顺序状态轨迹**：Scratchpad、CodeExecutor、Concise、dynamic scratchpad。容易采集和序列化，但纯序列缺乏显式控制/数据因果关系。
3. **CFG/block 状态机**：ORCA、CES。结构清晰、适合定位执行偏差，但若只存符号表，无法表达跨块数据依赖、别名和堆变化。
4. **时间索引依赖图/切片**：TAAF、CoReX 所代表的图与切片路线。最适合唯一定位、按查询抽取和长轨迹压缩，但构建复杂度最高。

### 4.2 对 block 如何划分的结论

推荐“**CFG basic block + observable sub-block**”的两级划分：

第一层按标准 CFG maximal basic block：单入口、单出口，遇到 branch、jump、return、raise 等控制转移结束。

第二层为评测可观察性在块内增设边界：

- 函数调用前后；
- 可能抛异常的操作前后；
- 对外部可见的 heap/field/container 写；
- 短路表达式的每个决策点；
- 循环 header、body entry、latch、exit；
- 多变量同时更新或语义上关键的赋值之后。

这比“每行一个 block”更紧凑，也比纯 CFG block 更适合状态题。静态 block 标识为代码结构 ID，动态实例再增加 frame 和 visit 序号。

### 4.3 对循环过长如何压缩的结论

推荐四级策略，而不是单一截断：

1. **Delta encoding**：每次迭代只记录变化的变量/对象字段；
2. **Run-length / pattern grouping**：连续执行相同 block signature 的迭代合并为区间，例如 `visit=3..87`；
3. **Verified summary**：对可验证的递推关系生成摘要，如 `i: 3→87, step=1`、`sum += a[i]`，摘要必须由实际轨迹验证，而非 LLM 猜测；
4. **Query-aware expansion**：默认显示首轮、末轮、分支变化轮、首次异常轮、被动态切片命中的轮；其他区间按需展开。

必须保留 `loop_id + iteration_vector`，嵌套循环用向量如 `[outer=4, inner=17]`，否则相同行/相同变量值无法唯一定位。

## 5. 推荐的统一结构化表示

### 5.1 唯一动态节点 ID

每个执行节点定义为：

```text
ExecNodeID = (
  problem_id,
  implementation_id,
  test_id,
  process_id,
  thread_id,
  frame_id,
  static_block_id,
  block_visit_id,
  substep_id
)
```

在单线程算法题中可省略 process/thread，但 schema 中应保留字段。`static_block_id` 由规范化 AST/CFG 位置生成；`block_visit_id` 解决循环重复；`frame_id` 解决递归与多次调用。

### 5.2 节点内容

```json
{
  "id": "impl7/test2/frame3/B12/v9/s1",
  "source": {"file": "main.py", "span": [18, 4, 20, 17]},
  "control": {
    "predecessor": ".../B8/v9/s0",
    "edge": "branch_true",
    "condition_value": true,
    "loop_iteration": [9]
  },
  "state_delta": {
    "writes": {"local:sum": {"before": 31, "after": 37}},
    "heap_writes": []
  },
  "data_parents": [
    {"value": "local:sum", "defined_at": ".../B12/v8/s1"},
    {"value": "local:x", "defined_at": ".../B3/v1/s0"}
  ],
  "event": "normal"
}
```

边至少包括：`control-next`、`control-dependence`、`data-def-use`、`call`、`return`、`exception`、`heap-alias`。这样控制流和数据流不是两份松散文本，而是同一动态实例图中的不同边类型。

### 5.3 查询相关动态切片

对问题 `Q=(target_node, target_value/property)`：

1. 从目标变量在目标动态节点的定义开始反向遍历 `data-def-use`；
2. 加入决定这些节点是否执行的 `control-dependence`；
3. 跨过程沿 `call/return` 和实参—形参边传播；
4. 对别名写入沿 `heap-alias` 传播；
5. 添加 context envelope；
6. 对 slice 中的循环区间应用可验证压缩。

该 slice 是确定性 oracle，可用于自动生成状态值、下一块、依赖来源、路径条件和输出题，而无需 LLM 生成题目。

### 5.4 规范化与唯一序列化

为保证“结构化唯一表示”，需要规定：

- 字段固定顺序，JSON Canonicalization；
- 数字、NaN、无穷、负零采用显式类型标签；
- dict/map 按规范化 key 排序；
- set 排序后编码；
- 对象使用稳定 `object_id`，引用与内容分离；
- 大字符串/大容器保存长度、hash、首尾窗口，后端保留完整值；
- 每个压缩区间保存原始事件范围和内容 hash；
- canonical JSON 再计算 SHA-256，作为样本与答案版本标识。

## 6. 建议的评测任务

### 粒度 2：输入—输出推理

- 给 code + input 预测 output；
- 给 code + output 反推满足条件的 input；
- 对等价实现检查输出一致性；
- 对相同 prime path 的输入检查预测稳定性。

### 粒度 3：中间执行状态推理

建议至少包括六类自动题：

1. `NextBlock`：预测下一个动态 block 和边类型；
2. `StateDelta`：预测该 block 执行后的变量/堆增量；
3. `PathCondition`：预测分支条件和循环退出条件；
4. `DefUseSource`：判断某值来自哪个动态定义实例；
5. `SliceMembership`：判断哪些节点对目标值具有动态因果相关性；
6. `CheckpointState`：预测循环摘要边界或调用返回处的规范化状态。

每题同时记录 exact accuracy、字段级 F1、首个 divergence point、过程 coherence、跨实现一致性、跨测试路径一致性和 token/cost。

## 7. 推荐实施路线

### 第一阶段：Python 可执行原型

- AST + bytecode/coverage 构建 CFG；
- `sys.settrace`/调试器采集 line、call、return、exception；
- 对 locals、迭代器、调用栈和常见容器做规范化快照；
- 生成 basic block 动态实例、state delta 和 def-use 边；
- 先覆盖 HumanEval+/MBPP+ 中无 I/O、无并发的纯函数。

### 第二阶段：自动 query 与 loop compression

- 从 return、branch、loop exit、异常和随机 def-use criterion 自动采样；
- 实现 backward dynamic slice；
- 实现 delta、重复模式合并、首尾/变化点保留；
- 同时保存 raw、full graph、slice、compressed view 四层 artifact。

### 第三阶段：评测鲁棒性

- 为每个语义问题生成或收集 3–5 个等价实现；
- 用 execution equivalence 过滤；
- 比较同一语义 query 在不同实现中的过程正确率；
- 做 full trace / delta / slice / slice+context / compressed slice 的消融实验。

### 第四阶段：扩展语言与现实特性

- Java：JVMTI/JDI、字节码 CFG、对象图；
- 递归、异常、生成器、闭包；
- 最后再扩展反射、并发和外部库，因为这些会显著增加 oracle 不确定性。

## 8. 最终推荐

若目标是发表一套相对 REval 有明确增量的 framework，建议将贡献集中为三点：

1. **表示贡献**：提出带唯一动态实例 ID 的时间索引动态程序依赖轨迹图，将 block、状态增量、控制依赖和数据依赖统一表示；
2. **数据构建贡献**：从执行器与动态切片自动产生中间推理问题和 oracle，不用人工逐题构造，也不让教师 LLM生成真值；
3. **效率贡献**：提出可验证的循环区间摘要与 query-aware 展开，在保持目标动态切片语义的同时显著降低 token 和 API 成本。

最关键的消融实验应验证：

- basic block 是否优于逐行表示；
- state delta 是否优于 full snapshot；
- dynamic slice 是否优于完整 trace；
- slice + context 是否优于纯最小 slice；
- verified loop summary 是否在 token 大幅下降时保持状态推理准确率；
- 单实现高分是否能迁移到功能等价实现。

这套设计直接覆盖 REval 的人工启发式、动态位置不唯一、长循环回避和 API 成本问题，也吸收了 2025–2026 年代表性工作的正面结果与负面证据。

## 9. 参考文献（2025 年以来）

1. Chen et al. *Reasoning Runtime Behavior of a Program with LLM: How Far Are We?* ICSE 2025. [会议页](https://conf.researchr.org/details/icse-2025/icse-2025-research-track/32/Reasoning-Runtime-Behavior-of-a-Program-with-LLM-How-Far-Are-We-)
2. Patel et al. *Planning a Large Language Model for Static Detection of Runtime Errors in Code Snippets.* ICSE 2025. [会议页](https://conf.researchr.org/details/icse-2025/icse-2025-research-track/172/Planning-a-Large-Language-Model-for-Static-Detection-of-Runtime-Errors-in-Code-Snippe)
3. Armengol-Estapé et al. *What I Cannot Execute, I Do Not Understand: Training and Evaluating LLMs on Program Execution Traces.* arXiv:2503.05703, 2025. [PDF](https://arxiv.org/pdf/2503.05703)
4. Wang et al. *Do Code Semantics Help? A Comprehensive Study on Execution Trace-Based Information for Code Large Language Models.* Findings of EMNLP 2025. [ACL Anthology](https://aclanthology.org/2025.findings-emnlp.548/)
5. Jung et al. *Code Execution as Grounded Supervision for LLM Reasoning.* EMNLP 2025. [ACL Anthology](https://aclanthology.org/2025.emnlp-main.1260/)
6. Liu et al. *Assessing Coherency and Consistency of Code Execution Reasoning by Large Language Models.* ICSE 2026. [会议页](https://conf.researchr.org/details/icse-2026/icse-2026-research-track/230/Assessing-Coherency-and-Consistency-of-Code-Execution-Reasoning-by-Large-Language-Mod)
7. Gao et al. *CoRE: A Fine-Grained Code Reasoning Benchmark Beyond Output Prediction.* Findings of ACL 2026. [PDF](https://aclanthology.org/2026.findings-acl.460.pdf)
8. Ezaz et al. *TAAF: A Trace Abstraction and Analysis Framework Synergizing Knowledge Graphs and LLMs.* ICSE 2026. [会议页](https://conf.researchr.org/details/icse-2026/icse-2026-research-track/304/TAAF-A-Trace-Abstraction-and-Analysis-Framework-Synergizing-Knowledge-Graphs-and-LLM)
9. Badihi and Rubin. *CoReX: Context-Aware Refinement-Based Slicing for Debugging Regression Failures.* ICSE 2026. [会议页](https://conf.researchr.org/details/icse-2026/icse-2026-research-track/60/CoReX-Context-Aware-Refinement-Based-Slicing-for-Debugging-Regression-Failures)
