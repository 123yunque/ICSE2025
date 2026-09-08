"""Compare the completed LiveCodeBench run with MBPP+ under one scoring policy.

The MBPP+ responses predate the LiveCodeBench adapter audit.  This script removes
helper-only wrapper tasks and opaque ``$o`` state requests from MBPP+ before it
rebuilds fixed-denominator and task-clustered statistics.  It does not hide the
remaining design differences; they are recorded in the generated report.
"""
from __future__ import annotations

import ast
import csv
import hashlib
import json
import random
from collections import defaultdict
from pathlib import Path

from granularity3_local.decomposed_evaluate import build_combined_report
from lcb_analyze import cluster_metrics


ROOT = Path(__file__).resolve().parent
RUN_NAME = "fresh100_20260906"
LCB_REPORT = ROOT / "reports" / RUN_NAME
MBPP_ROOT = ROOT.parent / "granularity3_local" / "decomposed_results_full_v2"
MBPP_LOCAL = ROOT.parent / "granularity3_local" / "block_state_mbppplus_full"
OUT = LCB_REPORT / "mbppplus_comparison"
BOOTSTRAP_ITERATIONS = 2000
BOOTSTRAP_SEED = 20260905


def load(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


def read_jsonl(path: Path):
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


def read_csv(path: Path):
    rows = list(csv.DictReader(path.open(encoding="utf-8-sig", newline="")))
    for row in rows:
        for key, value in list(row.items()):
            if value == "True":
                row[key] = True
            elif value == "False":
                row[key] = False
            elif value and key in {
                "trace_length", "distinct_blocks", "compressed_runs", "state_length",
                "expected_variable_count", "predicted_state_attempted_variable_count",
            }:
                row[key] = int(value)
    return rows


def contains_opaque(value):
    if isinstance(value, dict):
        return "$o" in value or any(contains_opaque(item) for item in value.values())
    if isinstance(value, list):
        return any(contains_opaque(item) for item in value)
    return False


def helper_only_tasks(dataset_root: Path):
    """Apply the frozen LCB helper-only rule to MBPP+ source programs."""
    preflight = load(MBPP_LOCAL / "preflight.json")
    candidates = [
        row for row in preflight
        if row.get("supported")
        and "helper_functions_are_treated_as_atomic_calls" in row.get("warnings", [])
    ]
    control_nodes = (ast.If, ast.For, ast.AsyncFor, ast.While, ast.Try, ast.With, ast.AsyncWith)
    if hasattr(ast, "Match"):
        control_nodes += (ast.Match,)
    excluded = []
    for row in candidates:
        source = dataset_root / row["task_id"] / "code.py"
        tree = ast.parse(source.read_text(encoding="utf-8"))
        functions = {node.name: node for node in tree.body if isinstance(node, ast.FunctionDef)}
        target = functions[row["function"]]
        helper_names = set(functions) - {target.name}
        called_helpers = {
            node.func.id for node in ast.walk(target)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
            and node.func.id in helper_names
        }
        target_control = any(isinstance(node, control_nodes) for node in ast.walk(target))
        if called_helpers and not target_control:
            excluded.append(row["task_id"])
    return sorted(excluded)


def trace_context(trace):
    expanded = []
    for path, repeats in trace:
        expanded.extend(path.split(">") * int(repeats))
    length = len(expanded)
    if length <= 1:
        trace_bin = "1"
    elif length <= 2:
        trace_bin = "2"
    elif length <= 5:
        trace_bin = "3-5"
    elif length <= 10:
        trace_bin = "6-10"
    elif length <= 25:
        trace_bin = "11-25"
    elif length <= 100:
        trace_bin = "26-100"
    else:
        trace_bin = ">100"
    return {
        "trace_length": length,
        "trace_bin": trace_bin,
        "long_trace": length >= 10,
        "distinct_blocks": len(set(expanded)),
        "compressed_runs": len(trace),
        "repeated_block": len(set(expanded)) < length,
    }


def state_bin(length):
    if length <= 2:
        return "2"
    if length <= 5:
        return "3-5"
    if length <= 10:
        return "6-10"
    if length <= 25:
        return "11-25"
    if length <= 100:
        return "26-100"
    return ">100"


def build_mbpp():
    dataset_root = Path(load(MBPP_LOCAL / "summary.json")["config"]["dataset_root"])
    helper_tasks = set(helper_only_tasks(dataset_root))

    control_requests = read_jsonl(MBPP_ROOT / "control" / "requests.jsonl")
    control_scores = read_jsonl(MBPP_ROOT / "control" / "scores.jsonl")
    control_oracles = read_jsonl(MBPP_ROOT / "control" / "oracles.jsonl")
    state_requests = read_jsonl(MBPP_ROOT / "oracle_state" / "requests.jsonl")
    state_scores = read_jsonl(MBPP_ROOT / "oracle_state" / "scores.jsonl")
    state_oracles = read_jsonl(MBPP_ROOT / "oracle_state" / "oracles.jsonl")
    predicted_requests = read_jsonl(MBPP_ROOT / "predicted_state" / "requests.jsonl")
    predicted_scores = read_jsonl(MBPP_ROOT / "predicted_state" / "scores.jsonl")

    state_oracle_by_id = {row["request_id"]: row for row in state_oracles}
    opaque_ids = {
        request_id for request_id, row in state_oracle_by_id.items()
        if contains_opaque(row.get("answer", {}).get("states", []))
    }
    control_requests = [row for row in control_requests if row["task_id"] not in helper_tasks]
    state_requests = [
        row for row in state_requests
        if row["task_id"] not in helper_tasks and row["request_id"] not in opaque_ids
    ]
    retained_state_ids = {row["request_id"] for row in state_requests}
    predicted_requests = [
        row for row in predicted_requests
        if row.get("parent_state_request_id") in retained_state_ids
    ]
    combined = build_combined_report(
        control_requests,
        control_scores,
        state_requests,
        state_scores,
        predicted_requests,
        predicted_scores,
    )

    control_score_by_case = {row["case_key"]: row for row in control_scores}
    control_oracle_by_case = {row["case_key"]: row for row in control_oracles}
    control_rows = []
    for request in control_requests:
        score = control_score_by_case.get(request["case_key"], {})
        trace = control_oracle_by_case[request["case_key"]]["answer"]["trace"]
        control_rows.append({
            "task_id": request["task_id"],
            "case_key": request["case_key"],
            "format_valid": request["case_key"] in control_score_by_case,
            "expanded_exact": bool(score.get("expanded_trace_exact")),
            "canonical_exact": bool(score.get("canonical_trace_exact")),
            **trace_context(trace),
        })

    state_oracle_by_id = {row["request_id"]: row for row in state_oracles}
    retained_predicted_parents = {
        row["parent_state_request_id"] for row in predicted_requests
    }
    variable_rows = []
    for row in combined["scores"]:
        request_id = next(
            request["request_id"] for request in state_requests
            if request["case_key"] == row["case_key"]
            and request["target_variable"] == row["target_variable"]
        )
        cf_group = (
            "cf_correct" if row["control_flow_expanded_exact"]
            else "cf_wrong_constructable" if request_id in retained_predicted_parents
            else "cf_unconstructable"
        )
        length = len(state_oracle_by_id[request_id]["answer"]["states"])
        variable_rows.append({
            **row,
            "request_id": request_id,
            "cf_group": cf_group,
            "net_drop": int(row["oracle_cf_state_exact"]) - int(row["predicted_cf_state_exact"]),
            "state_length": length,
            "state_bin": state_bin(length),
        })

    return {
        "control": control_rows,
        "variable": variable_rows,
        "case": combined["case_scores"],
        "combined": combined["summary"],
        "policy": {
            "helper_only_tasks": sorted(helper_tasks),
            "helper_only_task_count": len(helper_tasks),
            "opaque_state_request_count": len(opaque_ids),
        },
        "original": load(MBPP_ROOT / "combined" / "summary.json"),
    }


def build_lcb():
    control = read_csv(LCB_REPORT / "control_cases.csv")
    valid_control_ids = {
        row["case_key"] for row in read_jsonl(
            ROOT / "runs" / RUN_NAME / "control_flow" / "evaluation" / "scores.jsonl"
        )
    }
    for row in control:
        row["format_valid"] = row["case_key"] in valid_control_ids
    return {
        "control": control,
        "variable": read_csv(LCB_REPORT / "paired_variables.csv"),
        "case": read_csv(LCB_REPORT / "state_cases.csv"),
        "summary": load(LCB_REPORT / "summary.json"),
    }


def method_audit():
    pairs = {
        "control_flow": (MBPP_ROOT / "control" / "run_config.json",
                         ROOT / "runs" / RUN_NAME / "control_flow" / "run_config.json"),
        "oracle_state": (MBPP_ROOT / "oracle_state" / "run_config.json",
                         ROOT / "runs" / RUN_NAME / "oracle_state" / "run_config.json"),
        "predicted_state": (MBPP_ROOT / "predicted_state" / "run_config.json",
                            ROOT / "runs" / RUN_NAME / "predicted_state" / "run_config.json"),
    }
    request_config = {}
    for kind, (mbpp_path, lcb_path) in pairs.items():
        mbpp, lcb = load(mbpp_path), load(lcb_path)
        request_config[kind] = {
            "equal": mbpp["generation"] == lcb["generation"] and mbpp["execution"] == lcb["execution"],
            "generation": lcb["generation"],
            "execution": lcb["execution"],
        }

    protocol_files = [
        "decomposed_core.py", "decomposed_prepare.py", "decomposed_evaluate.py",
        "decomposed_gate.py", "decomposed_plan.py", "decomposed_statement.py",
    ]
    file_audit = {}
    for name in protocol_files:
        mbpp_text = (ROOT.parent / "granularity3_local" / name).read_text(encoding="utf-8").replace("\r\n", "\n")
        lcb_text = (ROOT / "granularity3_local" / name).read_text(encoding="utf-8").replace("\r\n", "\n")
        mbpp_hash = hashlib.sha256(mbpp_text.encode()).hexdigest()
        lcb_hash = hashlib.sha256(lcb_text.encode()).hexdigest()
        file_audit[name] = {"equal": mbpp_hash == lcb_hash, "sha256": lcb_hash}
    return {
        "request_configs": request_config,
        "all_request_configs_equal": all(row["equal"] for row in request_config.values()),
        "protocol_files": file_audit,
        "all_protocol_files_equal_after_line_ending_normalization": all(
            row["equal"] for row in file_audit.values()
        ),
        "api_runner_difference": "LCB changes resume-count bookkeeping only; recorded request configurations are equal.",
    }


def cluster_difference(left, right, metric, iterations=BOOTSTRAP_ITERATIONS):
    """Independent, task-clustered bootstrap for LCB minus MBPP+."""
    def group(rows):
        grouped = defaultdict(list)
        for row in rows:
            grouped[row["task_id"]].append(row)
        return [
            (len(values), sum(float(row[metric]) for row in values))
            for values in grouped.values()
        ]

    left_groups, right_groups = group(left), group(right)
    rng = random.Random(BOOTSTRAP_SEED)
    samples = []
    for _ in range(iterations):
        lsample = [left_groups[rng.randrange(len(left_groups))] for _ in left_groups]
        rsample = [right_groups[rng.randrange(len(right_groups))] for _ in right_groups]
        lrate = sum(x[1] for x in lsample) / sum(x[0] for x in lsample)
        rrate = sum(x[1] for x in rsample) / sum(x[0] for x in rsample)
        samples.append(lrate - rrate)
    samples.sort()
    return [samples[int(0.025 * iterations)], samples[int(0.975 * iterations)]]


def metric_result(lcb_rows, mbpp_rows, metric):
    lcb = cluster_metrics(lcb_rows, [metric], BOOTSTRAP_ITERATIONS)[metric]
    mbpp = cluster_metrics(mbpp_rows, [metric], BOOTSTRAP_ITERATIONS)[metric]
    return {
        "livecodebench": lcb,
        "mbppplus": mbpp,
        "delta_livecodebench_minus_mbppplus": lcb["micro"] - mbpp["micro"],
        "delta_cluster_ci95": cluster_difference(lcb_rows, mbpp_rows, metric),
        "livecodebench_numerator": sum(bool(row[metric]) for row in lcb_rows),
        "mbppplus_numerator": sum(bool(row[metric]) for row in mbpp_rows),
    }


def paired(rows):
    output = {}
    for group in ("all", "cf_correct", "cf_wrong_constructable", "cf_unconstructable"):
        selected = rows if group == "all" else [row for row in rows if row["cf_group"] == group]
        if not selected:
            continue
        oracle = sum(bool(row["oracle_cf_state_exact"]) for row in selected) / len(selected)
        predicted = sum(bool(row["predicted_cf_state_exact"]) for row in selected) / len(selected)
        output[group] = {
            "count": len(selected),
            "oracle": oracle,
            "predicted": predicted,
            "oracle_minus_predicted": oracle - predicted,
        }
    return output


def complexity(rows):
    subsets = {
        "all": rows,
        "trace_length_ge_10": [row for row in rows if row["trace_length"] >= 10],
        "trace_length_gt_100": [row for row in rows if row["trace_length"] > 100],
    }
    output = {}
    for name, selected in subsets.items():
        valid = [row for row in selected if row["format_valid"]]
        output[name] = (
            cluster_metrics(selected, ["expanded_exact", "canonical_exact"], BOOTSTRAP_ITERATIONS)
            | {
                "count": len(selected),
                "problem_count": len({row["task_id"] for row in selected}),
                "format_valid_count": len(valid),
                "format_valid_rate": len(valid) / len(selected) if selected else None,
                "expanded_exact_given_valid": (
                    sum(bool(row["expanded_exact"]) for row in valid) / len(valid) if valid else None
                ),
            }
        )
    return output


def state_lengths(rows):
    labels = ("2", "3-5", "6-10", "11-25", "26-100", ">100")
    result = {}
    for label in labels:
        selected = [row for row in rows if row["state_bin"] == label]
        if selected:
            result[label] = {
                "count": len(selected),
                "oracle": sum(bool(row["oracle_cf_state_exact"]) for row in selected) / len(selected),
                "predicted": sum(bool(row["predicted_cf_state_exact"]) for row in selected) / len(selected),
            }
    return result


def pct(value):
    return "—" if value is None else f"{100 * value:.2f}%"


def estimate_cell(result, dataset, numerator_key):
    row = result[dataset]
    numerator = result[numerator_key]
    lo, hi = row["cluster_ci95"]
    return f"{pct(row['micro'])} [{pct(lo)}, {pct(hi)}] ({numerator}/{row['denominator']})"


def render(comparison):
    metric_labels = {
        "control_expanded_exact": "控制流 expanded exact",
        "control_canonical_exact": "控制流 canonical exact",
        "oracle_state_exact": "Oracle-CF 变量状态 exact",
        "predicted_state_exact": "Predicted-CF 变量状态 exact（固定 K）",
        "end_to_end_variable_exact": "端到端变量联合 exact（固定 K）",
        "oracle_case_exact": "Oracle-CF 案例全部变量 exact",
        "predicted_case_exact": "Predicted-CF 案例全部变量 exact（固定 M）",
        "end_to_end_case_exact": "端到端案例联合 exact（固定 M）",
    }
    lines = [
        "# LiveCodeBench 与 MBPP+ 解耦执行实验对照",
        "",
        "结论：两次运行的执行推理核心协议和 API 参数一致，但完整实验尚不满足“除数据集外其他因素全部一致”。因此下表是描述性跨数据集比较，不能把差值全部归因于数据集。",
        "",
        "MBPP+ 的旧结果在 LiveCodeBench 审计规则出台前完成。本报告先按同一规则重算 MBPP+：排除 helper-only 包装任务和含 `$o` 的不透明对象状态请求；其余首答、请求和固定分母不变。",
        "",
        "旧 MBPP+ 文档中的 73.90% / 70.86% 与下表不同，原因是旧口径包含 112 个带进程内存地址的 `$o` 请求；这些请求在两种状态条件下全部判错。统一过滤后，MBPP+ 的 K 从 2,241 变为 2,129。另排除 6 个 helper-only 任务及其 60 个平凡控制流案例，N 从 3,591 变为 3,531。",
        "",
        "| MBPP+ 指标 | 旧口径 | 统一过滤后 |",
        "| --- | ---: | ---: |",
        f"| 控制流 expanded exact | {pct(comparison['original_mbppplus_combined']['control_flow_expanded_exact_rate'])} | {pct(comparison['harmonized_mbppplus_combined']['control_flow_expanded_exact_rate'])} |",
        f"| Oracle-CF 状态 exact | {pct(comparison['original_mbppplus_combined']['oracle_cf_state_exact_rate'])} | {pct(comparison['harmonized_mbppplus_combined']['oracle_cf_state_exact_rate'])} |",
        f"| Predicted-CF 状态 exact（固定 K） | {pct(comparison['original_mbppplus_combined']['predicted_cf_state_exact_rate'])} | {pct(comparison['harmonized_mbppplus_combined']['predicted_cf_state_exact_rate'])} |",
        f"| 端到端变量联合 exact | {pct(comparison['original_mbppplus_combined']['end_to_end_joint_exact_rate'])} | {pct(comparison['harmonized_mbppplus_combined']['end_to_end_joint_exact_rate'])} |",
        f"| 端到端案例联合 exact | {pct(comparison['original_mbppplus_combined']['end_to_end_case_joint_exact_rate'])} | {pct(comparison['harmonized_mbppplus_combined']['end_to_end_case_joint_exact_rate'])} |",
        "",
        "## 同口径主结果",
        "",
        "所有区间均为按原始题目聚类的 2,000 次 bootstrap 95% 区间；差值为 LiveCodeBench − MBPP+。无效或不可构造回答按冻结分母计失败。",
        "",
        "| 指标 | MBPP+ | LiveCodeBench | 差值及 95% 区间 |",
        "| --- | ---: | ---: | ---: |",
    ]
    for name, label in metric_labels.items():
        result = comparison["metrics"][name]
        lo, hi = result["delta_cluster_ci95"]
        lines.append(
            f"| {label} | {estimate_cell(result, 'mbppplus', 'mbppplus_numerator')} | "
            f"{estimate_cell(result, 'livecodebench', 'livecodebench_numerator')} | "
            f"{100 * result['delta_livecodebench_minus_mbppplus']:+.2f} pp "
            f"[{100 * lo:+.2f}, {100 * hi:+.2f}] |"
        )

    mbpp_all = comparison["complexity"]["mbppplus"]["all"]
    lcb_all = comparison["complexity"]["livecodebench"]["all"]
    lines += [
        "",
        f"控制流固定分母差值受到格式有效率直接影响：MBPP+ 有效 {mbpp_all['format_valid_count']:,}/{mbpp_all['count']:,}（{pct(mbpp_all['format_valid_rate'])}），"
        f"LiveCodeBench 有效 {lcb_all['format_valid_count']:,}/{lcb_all['count']:,}（{pct(lcb_all['format_valid_rate'])}）。"
        f"只用于诊断的“格式有效回答内 expanded exact”为 {pct(mbpp_all['expanded_exact_given_valid'])} 与 {pct(lcb_all['expanded_exact_given_valid'])}，两者接近；"
        "它不能替代固定分母主指标。LiveCodeBench 的 31 个格式无效首答全部来自服务恢复前的 40 请求试跑。",
    ]

    lines += [
        "",
        "## 控制流复杂度",
        "",
        "| 范围 | MBPP+ 固定分母 | LiveCodeBench 固定分母 | MBPP+ 有效回答内 | LiveCodeBench 有效回答内 | n（MBPP+ / LCB） |",
        "| --- | ---: | ---: | ---: | ---: | ---: |",
    ]
    subset_labels = {"all": "全量", "trace_length_ge_10": "展开长度 ≥10", "trace_length_gt_100": "展开长度 >100"}
    for key, label in subset_labels.items():
        mbpp = comparison["complexity"]["mbppplus"][key]
        lcb = comparison["complexity"]["livecodebench"][key]
        lines.append(
            f"| {label} | {pct(mbpp['expanded_exact']['micro'])} | "
            f"{pct(lcb['expanded_exact']['micro'])} | {pct(mbpp['expanded_exact_given_valid'])} | "
            f"{pct(lcb['expanded_exact_given_valid'])} | {mbpp['count']} / {lcb['count']} |"
        )

    lines += [
        "",
        "LiveCodeBench 中展开长度 ≥10 的案例占 59.26%（208/351），MBPP+ 仅占 10.90%（385/3,531）。两者既有轨迹构成差异，也有服务格式失败差异；因此不能把总体控制流差值解释成数据集难度。",
        "",
        "## 状态序列长度",
        "",
        "| Oracle 状态长度 | MBPP+ Oracle | LiveCodeBench Oracle | MBPP+ Predicted | LiveCodeBench Predicted |",
        "| --- | ---: | ---: | ---: | ---: |",
    ]
    for label in ("2", "3-5", "6-10", "11-25", "26-100", ">100"):
        mbpp = comparison["state_length_bins"]["mbppplus"].get(label)
        lcb = comparison["state_length_bins"]["livecodebench"].get(label)
        if mbpp and lcb:
            lines.append(
                f"| {label} | {pct(mbpp['oracle'])} (n={mbpp['count']}) | "
                f"{pct(lcb['oracle'])} (n={lcb['count']}) | {pct(mbpp['predicted'])} | {pct(lcb['predicted'])} |"
            )

    lines += [
        "",
        "## 控制流错误与状态退化",
        "",
        "| 数据集 | 配对范围 | 变量数 | Oracle-CF | Predicted-CF | 净下降 |",
        "| --- | --- | ---: | ---: | ---: | ---: |",
    ]
    group_labels = {
        "all": "全部固定 K",
        "cf_correct": "控制流正确",
        "cf_wrong_constructable": "控制流错误、状态可构造",
        "cf_unconstructable": "控制流错误、状态不可构造",
    }
    for dataset, dataset_label in (("mbppplus", "MBPP+"), ("livecodebench", "LiveCodeBench")):
        for key, label in group_labels.items():
            row = comparison["paired"][dataset].get(key)
            if row:
                lines.append(
                    f"| {dataset_label} | {label} | {row['count']} | {pct(row['oracle'])} | "
                    f"{pct(row['predicted'])} | {100 * row['oracle_minus_predicted']:.2f} pp |"
                )

    lines += [
        "",
        "## 方法一致性审计",
        "",
        "| 因素 | 审计结论 |",
        "| --- | --- |",
        "| 执行推理协议 | 一致：`g3-decomposed-v2`，相同的 Control → Oracle-State → Predicted-State 定义。 |",
        "| 模型与请求参数 | 一致：`gpt-5.4`、reasoning `low`、verbosity `low`、JSON object、max completion 16,384、temperature 未设置。 |",
        "| 提示词 | 一致：Control system prompt SHA-256 为 `0418…b6e`，State 为 `af47…b17`。 |",
        "| 执行参数 | 一致：并发 3、超时 180 秒、retries 0、invalid 不重试。 |",
        "| 输入数量与准备上限 | 一致：每题最多 10 个输入；block events 500、statement events 2,000、state items 500、state answer 16,000 字符。 |",
        "| 评分与统计 | 本报告已统一：expanded 为控制流主指标；固定 N/K/M；无效和不可构造计失败；按 task 聚类 bootstrap。 |",
        "| 适配器过滤 | 原始运行不一致；本报告已对 MBPP+ 后验应用同一 helper-only 与 `$o` 排除规则。独立请求互不共享上下文，因此保留请求的首答不受该后验过滤影响。 |",
        "| 程序来源 | 不一致：MBPP+ 使用 benchmark 参考实现；LiveCodeBench 对 100 题重新生成候选代码，并只让验证通过且执行器支持的程序进入推理。 |",
        "| 抽样与入选 | 不一致：MBPP+ 接近全量 376 题；LiveCodeBench 从 1,055 题抽取 100 题，59 题生成验证通过，最终 49 题进入控制流。 |",
        "| 调用时间 | 不一致：两批请求在不同服务阶段运行；LiveCodeBench 还保留了恢复前 40 个控制流试跑首答。 |",
        "| Repeat-State 对照 | 不一致：LiveCodeBench 额外重复调用 40 个 Oracle-State 请求；MBPP+ 旧运行没有该对照。它不进入上面的主准确率。 |",
        "| 程序接口 | 数据集固有差异：MBPP+ 为函数调用；LiveCodeBench 同时含 Call-Based 与 StdIn。 |",
        "",
        "## 可支持的结论",
        "",
        "1. LiveCodeBench 的固定分母控制流 exact 低 7.81 个百分点，但格式有效回答内两边几乎相同（95.63% 对 95.99%）。固定分母差值主要伴随恢复前的 31 个格式失败；同时 LiveCodeBench 的长轨迹占比是 59.26%，远高于 MBPP+ 的 10.90%。不能据总体值断言模型在 LiveCodeBench 的执行推理更差。",
        "2. LiveCodeBench 的状态 exact 更高，而且在多数状态长度桶内仍更高。这说明差异不只来自序列长度，但还可能来自变量类型、程序来源和生成成功筛选；当前数据不足以把优势归因于数据集名称。",
        "3. 在控制流错误但状态请求仍可构造的同类请求中，两边的 Oracle→Predicted 净下降接近：MBPP+ 为 10.84 pp，LiveCodeBench 为 9.40 pp。LiveCodeBench 的全 K 净下降更大，主要伴随不可构造比例更高（9.26% 对 0.99%），其中包含恢复前的格式失败首答。",
        "4. 当前差值同时受到程序来源、生成成功筛选、样本规模和服务阶段影响，不能表述为“仅由数据集导致”。",
        "",
        "若要严格满足“仅数据集不同”，需要在同一时间窗交错调用两套数据：各自冻结 100 题，均用同一生成提示和候选次数生成代码，统一验证、过滤、10 输入规则和 Repeat-State 对照，再用本报告的固定分母与聚类统计。",
    ]
    return "\n".join(lines) + "\n"


def main():
    mbpp = build_mbpp()
    lcb = build_lcb()
    metrics = {
        "control_expanded_exact": metric_result(lcb["control"], mbpp["control"], "expanded_exact"),
        "control_canonical_exact": metric_result(lcb["control"], mbpp["control"], "canonical_exact"),
        "oracle_state_exact": metric_result(lcb["variable"], mbpp["variable"], "oracle_cf_state_exact"),
        "predicted_state_exact": metric_result(lcb["variable"], mbpp["variable"], "predicted_cf_state_exact"),
        "end_to_end_variable_exact": metric_result(lcb["variable"], mbpp["variable"], "end_to_end_joint_exact"),
        "oracle_case_exact": metric_result(lcb["case"], mbpp["case"], "oracle_cf_all_variables_exact"),
        "predicted_case_exact": metric_result(lcb["case"], mbpp["case"], "predicted_cf_all_variables_exact"),
        "end_to_end_case_exact": metric_result(lcb["case"], mbpp["case"], "end_to_end_case_joint_exact"),
    }
    comparison = {
        "schema_version": "lcb-mbppplus-comparison-v1",
        "bootstrap": {
            "unit": "original task; all inputs and variables sampled together",
            "iterations": BOOTSTRAP_ITERATIONS,
            "seed": BOOTSTRAP_SEED,
        },
        "verdict": "inference_core_aligned_full_experiment_not_dataset_only",
        "mbppplus_policy_harmonization": mbpp["policy"],
        "denominators": {
            "mbppplus": {"N": len(mbpp["control"]), "K": len(mbpp["variable"]), "M": len(mbpp["case"]),
                         "tasks_N": len({row['task_id'] for row in mbpp['control']}),
                         "tasks_K": len({row['task_id'] for row in mbpp['variable']})},
            "livecodebench": {"N": len(lcb["control"]), "K": len(lcb["variable"]), "M": len(lcb["case"]),
                              "tasks_N": len({row['task_id'] for row in lcb['control']}),
                              "tasks_K": len({row['task_id'] for row in lcb['variable']})},
        },
        "metrics": metrics,
        "complexity": {
            "mbppplus": complexity(mbpp["control"]),
            "livecodebench": complexity(lcb["control"]),
        },
        "paired": {
            "mbppplus": paired(mbpp["variable"]),
            "livecodebench": paired(lcb["variable"]),
        },
        "state_length_bins": {
            "mbppplus": state_lengths(mbpp["variable"]),
            "livecodebench": state_lengths(lcb["variable"]),
        },
        "original_mbppplus_combined": mbpp["original"],
        "harmonized_mbppplus_combined": mbpp["combined"],
        "method_audit": method_audit(),
    }
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "comparison.json").write_text(
        json.dumps(comparison, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    (OUT / "REPORT.md").write_text(render(comparison), encoding="utf-8")

    # Guard against accidental denominator or CSV parsing drift.
    assert len(lcb["control"]) == lcb["summary"]["N_control_cases"]
    assert len(lcb["variable"]) == lcb["summary"]["K_state_variables"]
    assert len(lcb["case"]) == lcb["summary"]["M_state_cases"]
    assert abs(metrics["control_expanded_exact"]["livecodebench"]["micro"]
               - lcb["summary"]["combined"]["control_flow_expanded_exact_rate"]) < 1e-12
    assert comparison["method_audit"]["all_request_configs_equal"]
    assert comparison["method_audit"]["all_protocol_files_equal_after_line_ending_normalization"]
    print(json.dumps({
        "output": str(OUT),
        "verdict": comparison["verdict"],
        "denominators": comparison["denominators"],
        "mbppplus_policy_harmonization": comparison["mbppplus_policy_harmonization"],
    }, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
