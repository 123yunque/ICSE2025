# PPT Outline: REval 文献汇报

## Slide 1: 封面
- **Title**: Reasoning Runtime Behavior of a Program with LLM: How Far Are We?
- **Subtitle**: 代码大语言模型是否真的理解程序运行过程？
- **Key points**:
  - ICSE 2025 论文文献汇报
  - 主题：代码推理、运行时行为、增量一致性
  - 核心框架：REval
- **Visual idea**: 左侧为论文题目和关键词，右侧为“代码 -> 执行轨迹 -> 模型推理”的抽象流程图。
- **Layout role**: Cover
- **Required source images**: None

## Slide 2: 研究背景：从“会写代码”到“理解执行”
- **Key points**:
  - 代码 LLM 已广泛用于代码生成、调试和程序理解。
  - 现有评估常关注最终输出或功能正确性，如 HumanEval、ClassEval、CRUXEval。
  - 真实程序理解还需要掌握执行路径、变量状态和语句覆盖等运行时行为。
  - 论文关注的问题：模型是否只是猜对输出，还是能稳定模拟执行过程？
- **Visual idea**: 对比图：传统评估只看 input/output，REval 观察 execution path、program state、coverage 和 output。
- **Layout role**: Context / problem
- **Required source images**: None

## Slide 3: 科学问题与研究目标
- **Key points**:
  - RQ1：LLM 在 Runtime Behavior Reasoning 上表现如何？
  - RQ2：LLM 在 Incremental Consistency Evaluation 上表现如何？
  - 更深层问题：代码生成能力强是否等于代码推理能力强？
  - 目标：构建一个能评估“过程推理”和“逻辑一致性”的代码 LLM 基准框架。
- **Visual idea**: 三个问题卡片，分别对应“运行时行为”“增量一致性”“生成与推理关系”。
- **Layout role**: Problem framing
- **Required source images**: None

## Slide 4: REval 框架总览
- **Key points**:
  - 输入：可执行程序、测试用例和上下文。
  - 执行标准答案并通过 tracer 提取真实运行轨迹。
  - 构造四类运行时行为推理任务。
  - 同时评估单任务准确率和跨任务增量一致性。
- **Visual idea**: 中心流程图：Base benchmark -> Execution tracer -> Runtime behavior tasks -> Accuracy / IC Score。
- **Layout role**: Framework overview
- **Required source images**: None

## Slide 5: 四类运行时行为任务
- **Key points**:
  - CCP：判断某条语句是否会被执行。
  - PSP：预测语句执行后变量的值和类型。
  - EPP：预测当前语句后的下一条执行语句。
  - OP：预测给定输入下的程序输出。
  - 四类任务分别覆盖控制流、数据流、类型变化和最终行为。
- **Visual idea**: 2x2 任务矩阵，每格包含任务缩写、问题形式和评估重点。
- **Layout role**: Concept explanation
- **Required source images**: None

## Slide 6: 增量一致性：不只看答对，还看逻辑链
- **Key points**:
  - 论文假设四类任务存在递进依赖：CCP -> PSP -> EPP -> OP。
  - 如果前置任务错误，后续复杂任务却正确，可能说明模型答案逻辑不一致。
  - IC Score 奖励连续一致的正确推理序列。
  - 该指标用于识别“猜对答案但过程不可靠”的模型行为。
- **Visual idea**: 一条递进箭头链，展示 `{1,1,0,0}` 为一致、`{0,1,1,0}` 为不一致。
- **Layout role**: Metric explanation
- **Required source images**: None

## Slide 7: 实验设计
- **Key points**:
  - 基础基准：HumanEval 与 ClassEval。
  - 构造得到 3152 个问题实例，程序平均 408.3 tokens。
  - 模型覆盖 CodeLlama、Magicoder、StarCoder2、Gemma、Mistral、GPT-3.5、GPT-4。
  - 主要使用 few-shot prompting，额外测试 CodeLlama-7B-Instruct 的 CoT。
  - 开源模型重复 5 次；闭源模型因预算未重复。
- **Visual idea**: 实验设置仪表盘：数据集、模型族、prompt、硬件与参数。
- **Layout role**: Experimental design
- **Required source images**: None

## Slide 8: 关键结果 RQ1：运行时行为推理仍然困难
- **Key points**:
  - 四任务平均准确率仅 44.4%。
  - GPT-4-Turbo 最强，平均准确率 75.0%；GPT-3.5 为 55.7%。
  - 最强开源模型 CodeLlama-34B-Instruct 平均准确率 51.0%。
  - EPP 最难，平均准确率只有 19.4%；OP 相对最容易，平均 61.8%。
- **Visual idea**: 横向柱状图突出 GPT-4、GPT-3.5、CodeLlama-34B 和平均值；旁边放任务难度条。
- **Layout role**: Data evidence
- **Required source images**: None

## Slide 9: 关键结果 RQ2：模型普遍缺乏增量一致性
- **Key points**:
  - 平均 IC Score 只有 10.3。
  - GPT-4-Turbo 最高，为 42.5；GPT-3.5 为 20.6。
  - 多数模型 IC Score 低于 20，说明跨任务推理链不稳定。
  - RBR 与 IC 相关系数为 0.940，但代码生成能力与推理能力不完全等价。
  - HumanEval 与 RBR / IC 的相关系数分别为 0.772 和 0.724。
- **Visual idea**: 左侧 IC Score 排名条，右侧相关性三角矩阵。
- **Layout role**: Data evidence / interpretation
- **Required source images**: None

## Slide 10: 结论、创新点与不足
- **Key points**:
  - 结论：当前 LLM 仍难以可靠推理程序运行过程，尤其是执行路径和一致性。
  - 创新：从最终输出扩展到运行时行为；提出 REval 和 Incremental Consistency；构建新基准并做大规模实证。
  - 不足：任务筛选依赖规则；动态行为覆盖有限；主要基于 Python；IC 递进假设并非所有场景严格成立。
  - 启示：未来应引入执行轨迹训练、程序状态监督和面向执行推理的 prompt 策略。
- **Visual idea**: 三栏总结：Takeaway / Contributions / Limitations。
- **Layout role**: Summary / critique
- **Required source images**: None

