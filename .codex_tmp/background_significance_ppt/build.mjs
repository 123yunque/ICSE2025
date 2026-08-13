import fs from "node:fs/promises";
import { Presentation, PresentationFile } from "@oai/artifact-tool";

const OUT = "C:/Users/朱雀/.codex/visualizations/2026/08/10/019febab-8a1d-7301-ba2c-d8752613be12/background_significance_ppt/研究背景与意义.pptx";
const RENDER_DIR = "C:/Users/朱雀/.codex/visualizations/2026/08/10/019febab-8a1d-7301-ba2c-d8752613be12/background_significance_ppt/rendered";
const FONT = "Microsoft YaHei";
const BG = "#F8FAFC";
const INK = "#111827";
const MUTED = "#4B5563";
const BLUE = "#2563EB";
const PALE = "#E8F0FE";
const LINE = "#CBD5E1";

function addText(slide, text, x, y, w, h, size = 22, opts = {}) {
  const box = slide.shapes.add({
    geometry: "textbox",
    name: opts.name,
    position: { left: x, top: y, width: w, height: h },
    fill: "none",
    line: { style: "solid", fill: "none", width: 0 },
  });
  box.text = text;
  box.text.style = {
    fontSize: size,
    typeface: FONT,
    color: opts.color || INK,
    bold: Boolean(opts.bold),
    alignment: opts.align || "left",
    verticalAlignment: opts.valign || "top",
  };
  return box;
}

function addTitle(slide, title, page) {
  addText(slide, title, 56, 38, 1120, 70, 38, { bold: true, name: `slide-title-${page}` });
  addText(slide, String(page), 1180, 665, 40, 22, 14, { color: MUTED, align: "right" });
}

function addCard(slide, x, y, w, h, heading, lines, accent = BLUE) {
  slide.shapes.add({
    geometry: "roundRect",
    position: { left: x, top: y, width: w, height: h },
    fill: "#FFFFFF",
    line: { style: "solid", fill: LINE, width: 1 },
    borderRadius: "rounded-xl",
  });
  slide.shapes.add({
    geometry: "rect",
    position: { left: x, top: y, width: 8, height: h },
    fill: accent,
    line: { style: "solid", fill: accent, width: 0 },
  });
  addText(slide, heading, x + 28, y + 24, w - 52, 42, 25, { bold: true, color: accent });
  addText(slide, lines, x + 28, y + 78, w - 52, h - 96, 19, { color: MUTED });
}

function notes(slide, text) {
  slide.speakerNotes.textFrame.setText(`${text}\n\n[Sources]\n- 用户既定研究方案与本次对话内容（无外部素材）`);
}

async function writeBlob(path, blob) {
  await fs.writeFile(path, new Uint8Array(await blob.arrayBuffer()));
}

async function main() {
  const deck = Presentation.create({ slideSize: { width: 1280, height: 720 } });

  // 1 — section opener, based on Codex Grid slide 02.
  {
    const s = deck.slides.add();
    s.background.fill = BG;
    addText(s, "开题汇报", 56, 48, 300, 38, 23, { bold: true, color: BLUE });
    addText(s, "研究背景与意义", 885, 48, 330, 38, 23, { bold: true, align: "right" });
    addText(s, "为什么需要重新评估\n代码大模型的推理能力？", 56, 310, 1050, 245, 56, { bold: true, valign: "bottom" });
    slideAccent(s);
    notes(s, "本部分依次说明应用背景、现有评测的两类关键不足、研究必要性以及理论、方法和实践价值。");
  }

  // 2 — two-column layout, based on Codex Grid slide 04.
  {
    const s = deck.slides.add();
    s.background.fill = BG;
    addTitle(s, "代码大模型的应用正在从“生成代码”走向“理解与推理”", 2);
    addText(s, "应用范围持续扩展", 64, 168, 470, 46, 27, { bold: true, color: BLUE });
    addText(s, "代码生成、补全与重构\n\n缺陷修复与测试用例生成\n\n逐步参与复杂软件开发活动", 64, 238, 485, 290, 23, { color: MUTED });
    s.shapes.add({ geometry: "straightConnector1", position: { left: 620, top: 165, width: 0, height: 410 }, fill: "none", line: { style: "solid", fill: LINE, width: 2 } });
    addText(s, "能力要求随之升级", 680, 168, 470, 46, 27, { bold: true, color: BLUE });
    addText(s, "不仅要生成可运行的代码，\n还应理解程序语义与实际行为。\n\n能否正确推断控制流、状态变化和输出，\n成为代码大模型可靠应用的基础。", 680, 238, 500, 300, 23, { color: MUTED });
    notes(s, "代码大模型已经应用于生成、修复和测试等场景。应用越深入，对程序语义理解和执行推理能力的要求越高。评测目标因此需要由任务完成结果扩展到程序实际行为。 ");
  }

  // 3 — two comparison cards, based on Codex Grid slide 11.
  {
    const s = deck.slides.add();
    s.background.fill = BG;
    addTitle(s, "现有评测面临两项根本性挑战", 3);
    addText(s, "传统高分并不必然代表真实推理能力：评测既要控制数据可信性，也要深入程序执行过程。", 64, 125, 1120, 78, 24, { color: MUTED });
    addCard(s, 64, 255, 545, 330, "基准污染风险", "• 公开题目与参考实现可能进入训练语料\n\n• 原始基准高分可能混入记忆效应\n\n• 难以区分“模型见过”与“模型会做”");
    addCard(s, 671, 255, 545, 330, "结果级评测局限", "• Pass@k与输出准确率主要反映最终结果\n\n• 正确答案可能来自模式匹配或偶然一致\n\n• 无法定位首个控制流或状态推理错误", "#7C3AED");
    notes(s, "第一项挑战是评测数据是否可信。黑盒条件下不能直接证明污染，但可以诊断潜在记忆依赖风险。第二项挑战是评测粒度不足，最终结果无法说明控制流和状态推理是否正确。");
  }

  // 4 — three-stage timeline, based on Codex Grid slide 17.
  {
    const s = deck.slides.add();
    s.background.fill = BG;
    addTitle(s, "可信评估需要回答三个递进问题", 4);
    s.shapes.add({ geometry: "straightConnector1", position: { left: 120, top: 312, width: 1020, height: 0 }, fill: "none", line: { style: "solid", fill: LINE, width: 3 } });
    const xs = [150, 530, 910];
    const labels = ["数据层", "行为与过程层", "能力提升层"];
    const heads = ["评测结果是否可信？", "模型是否真正理解？", "如何提升真实推理？"];
    const desc = ["识别公开基准中的潜在污染与记忆依赖风险", "同时考察最终输出、控制流和中间执行状态", "利用可验证的执行差异形成过程监督信号"];
    for (let i = 0; i < 3; i++) {
      s.shapes.add({ geometry: "ellipse", position: { left: xs[i], top: 300, width: 24, height: 24 }, fill: BLUE, line: { style: "solid", fill: BLUE, width: 0 } });
      addText(s, labels[i], xs[i] - 8, 245, 200, 32, 18, { bold: true, color: BLUE });
      addText(s, heads[i], xs[i] - 8, 365, 320, 46, 25, { bold: true });
      addText(s, desc[i], xs[i] - 8, 430, 310, 105, 19, { color: MUTED });
    }
    notes(s, "研究必要性可概括为三个问题：先判断成绩是否受到记忆影响，再判断模型是否真正理解程序执行，最后探索如何利用可信执行信息促进能力提升。这里呈现问题逻辑，不展开技术路线。");
  }

  // 5 — three-callout significance slide, based on Codex Grid slide 09.
  {
    const s = deck.slides.add();
    s.background.fill = BG;
    addTitle(s, "研究价值体现在理论认识、评测方法与可靠应用三个层面", 5);
    addText(s, "建立从评测数据可信性、外部行为正确性到内部执行过程正确性的完整评价视角，并为模型增强提供依据。", 64, 125, 1120, 80, 23, { color: MUTED });
    addCard(s, 64, 260, 350, 300, "理论意义", "区分基准记忆、结果正确与过程正确，揭示代码大模型的真实推理能力边界");
    addCard(s, 465, 260, 350, 300, "方法意义", "推动代码评测由结果级评价走向可信、结构化和过程级评价", "#0F766E");
    addCard(s, 866, 260, 350, 300, "实践意义", "支撑模型选择、错误诊断、训练改进与软件工程场景中的可靠应用", "#B45309");
    notes(s, "理论上，本研究帮助理解模型的真实程序推理能力；方法上，形成更加可信和细粒度的评测视角；实践上，为模型选择、错误分析和能力增强提供依据。");
  }

  const pptx = await PresentationFile.exportPptx(deck);
  await pptx.save(OUT);
  await fs.mkdir(RENDER_DIR, { recursive: true });
  for (const [index, slide] of deck.slides.items.entries()) {
    await writeBlob(`${RENDER_DIR}/slide-${index + 1}.png`, await deck.export({ slide, format: "png", scale: 1 }));
    await fs.writeFile(`${RENDER_DIR}/slide-${index + 1}.layout.json`, await (await slide.export({ format: "layout" })).text());
  }
  await writeBlob(`${RENDER_DIR}/montage.webp`, await deck.export({ format: "webp", montage: true, scale: 1 }));
}

function slideAccent(slide) {
  slide.shapes.add({ geometry: "rect", position: { left: 56, top: 600, width: 180, height: 8 }, fill: BLUE, line: { style: "solid", fill: BLUE, width: 0 } });
}

main().catch((err) => {
  console.error(err);
  process.exitCode = 1;
});
