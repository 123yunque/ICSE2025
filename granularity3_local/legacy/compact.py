import json
from collections import defaultdict
from pathlib import Path

from granularity3_local.oracle import write_json, write_jsonl


COMPACT_SCHEMA_VERSION = "g3-compact-batch-v1"
HIGH_VALUE_KINDS = {"if_predicate", "for_header", "while_header", "return"}


def read_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def read_jsonl(path):
    return [json.loads(line) for line in Path(path).read_text(encoding="utf-8").splitlines() if line.strip()]


def compact_value(value):
    if isinstance(value, dict):
        if value == {"$undefined": True}:
            return {"$u": 1}
        return {key: compact_value(item) for key, item in value.items()}
    if isinstance(value, list):
        return [compact_value(item) for item in value]
    return value


def expand_value(value):
    if isinstance(value, dict):
        if value == {"$u": 1}:
            return {"$undefined": True}
        return {key: expand_value(item) for key, item in value.items()}
    if isinstance(value, list):
        return [expand_value(item) for item in value]
    return value


def select_probe_indices(model_case, model_inputs, answers, max_occurrences=3):
    by_block = defaultdict(list)
    for index, (probe, answer) in enumerate(zip(model_inputs, answers)):
        block = probe["current_block"]
        kind = model_case["blocks"][block]["kind"]
        high_value = kind in HIGH_VALUE_KINDS or bool(answer.get("delta")) or "return" in answer
        if high_value:
            by_block[block].append(index)

    selected = set()
    for indices in by_block.values():
        transition_representatives = {}
        for index in indices:
            answer = answers[index]
            transition_representatives.setdefault((answer.get("next"), "return" in answer), index)
        selected.update(transition_representatives.values())
        if max_occurrences > 0 and len(indices) <= max_occurrences:
            selected.update(indices)
        elif max_occurrences == 1:
            selected.add(indices[0])
        elif max_occurrences == 2:
            selected.update((indices[0], indices[-1]))
        elif max_occurrences >= 3:
            selected.update((indices[0], indices[len(indices) // 2], indices[-1]))
    return sorted(selected)


def compact_case(model_case):
    blocks = [
        [block_id, data["kind"], data["source"]]
        for block_id, data in sorted(model_case["blocks"].items())
    ]
    edges = [
        [edge["from"], edge["to"], edge["edge_type"]]
        for edge in model_case["cfg_edges"]
    ]
    return {
        "fn": model_case["function"],
        "input": model_case["input"],
        "blocks": blocks,
        "edges": edges,
    }


def compact_probe(probe, short_id):
    return {
        "id": short_id,
        "block": probe["current_block"],
        "occ": probe["target_event"].rsplit("#", 1)[-1],
        "pre": compact_value(probe["state_before"]),
    }


def compact_answer(answer, short_id):
    result = {
        "id": short_id,
        "next": answer["next"],
        "delta": compact_value(answer["delta"]),
    }
    if "return" in answer:
        result["ret"] = compact_value(answer["return"])
    return result


def build_compact_batches(probe_dir, output_dir, batch_size=8, max_occurrences=3):
    probe_dir = Path(probe_dir)
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    model_case = read_json(probe_dir / "model_case.json")
    model_inputs = read_jsonl(probe_dir / "model_inputs.jsonl")
    answers = read_jsonl(probe_dir / "answers.jsonl")
    if len(model_inputs) != len(answers):
        raise ValueError("model_inputs and answers are not aligned")

    indices = select_probe_indices(model_case, model_inputs, answers, max_occurrences=max_occurrences)
    case_view = compact_case(model_case)
    batches = []
    answer_batches = []
    id_map = []
    for batch_index, start in enumerate(range(0, len(indices), batch_size), start=1):
        chosen = indices[start:start + batch_size]
        batch_id = f"{model_case['case_id']}/batch_{batch_index}"
        batch_probes = []
        batch_answers = []
        for offset, source_index in enumerate(chosen, start=1):
            short_id = f"p{offset}"
            probe = model_inputs[source_index]
            answer = answers[source_index]
            batch_probes.append(compact_probe(probe, short_id))
            batch_answers.append(compact_answer(answer, short_id))
            id_map.append({"batch_id": batch_id, "id": short_id, "probe_id": probe["probe_id"]})
        batches.append({
            "schema": COMPACT_SCHEMA_VERSION,
            "batch_id": batch_id,
            **case_view,
            "probes": batch_probes,
        })
        answer_batches.append({"batch_id": batch_id, "answers": batch_answers})

    write_jsonl(output_dir / "model_batches.jsonl", batches)
    write_jsonl(output_dir / "answer_batches.jsonl", answer_batches)
    write_jsonl(output_dir / "id_map.jsonl", id_map)
    manifest = {
        "schema_version": COMPACT_SCHEMA_VERSION,
        "case_id": model_case["case_id"],
        "original_probe_count": len(model_inputs),
        "selected_probe_count": len(indices),
        "batch_count": len(batches),
        "batch_size": batch_size,
        "max_occurrences_per_block": max_occurrences,
        "answer_isolation": True,
    }
    write_json(output_dir / "manifest.json", manifest)
    return {"batches": batches, "answers": answer_batches, "id_map": id_map, "manifest": manifest}


def build_compact_tree(case_root, output_root, batch_size=8, max_occurrences=3):
    case_root = Path(case_root)
    output_root = Path(output_root)
    manifests = []
    for input_path in sorted(case_root.rglob("model_inputs.jsonl")):
        probe_dir = input_path.parent
        relative = probe_dir.relative_to(case_root)
        result = build_compact_batches(
            probe_dir,
            output_root / relative,
            batch_size=batch_size,
            max_occurrences=max_occurrences,
        )
        manifests.append(result["manifest"])
    summary = {
        "schema_version": COMPACT_SCHEMA_VERSION,
        "case_count": len(manifests),
        "original_probe_count": sum(item["original_probe_count"] for item in manifests),
        "selected_probe_count": sum(item["selected_probe_count"] for item in manifests),
        "batch_count": sum(item["batch_count"] for item in manifests),
        "batch_size": batch_size,
        "max_occurrences_per_block": max_occurrences,
    }
    original = summary["original_probe_count"]
    summary["probe_reduction"] = 1 - summary["selected_probe_count"] / original if original else 0
    write_json(output_root / "summary.json", summary)
    return summary


def analyze_compact_tree(case_root, batch_size=8, max_occurrences=3):
    from granularity3_local.legacy.api_smoke import build_prompt
    from granularity3_local.legacy.compact_api import SYSTEM_PROMPT, build_compact_prompt

    case_root = Path(case_root)
    old_system = "Return one valid JSON object only. No explanation."
    stats = {
        "schema_version": COMPACT_SCHEMA_VERSION,
        "case_count": 0,
        "original_probe_count": 0,
        "selected_probe_count": 0,
        "batch_count": 0,
        "original_prompt_chars": 0,
        "compact_prompt_chars": 0,
        "batch_size": batch_size,
        "max_occurrences_per_block": max_occurrences,
    }
    for input_path in sorted(case_root.rglob("model_inputs.jsonl")):
        probe_dir = input_path.parent
        model_case = read_json(probe_dir / "model_case.json")
        model_inputs = read_jsonl(input_path)
        answers = read_jsonl(probe_dir / "answers.jsonl")
        indices = select_probe_indices(model_case, model_inputs, answers, max_occurrences=max_occurrences)
        stats["case_count"] += 1
        stats["original_probe_count"] += len(model_inputs)
        stats["selected_probe_count"] += len(indices)
        stats["original_prompt_chars"] += sum(
            len(old_system) + len(build_prompt(model_case, probe)) for probe in model_inputs
        )
        case_view = compact_case(model_case)
        for batch_index, start in enumerate(range(0, len(indices), batch_size), start=1):
            chosen = indices[start:start + batch_size]
            batch = {
                "schema": COMPACT_SCHEMA_VERSION,
                "batch_id": f"{model_case['case_id']}/batch_{batch_index}",
                **case_view,
                "probes": [compact_probe(model_inputs[index], f"p{offset}") for offset, index in enumerate(chosen, 1)],
            }
            stats["batch_count"] += 1
            stats["compact_prompt_chars"] += len(SYSTEM_PROMPT) + len(build_compact_prompt(batch))
    original_probes = stats["original_probe_count"]
    original_chars = stats["original_prompt_chars"]
    stats["probe_reduction"] = 1 - stats["selected_probe_count"] / original_probes if original_probes else 0
    stats["request_reduction"] = 1 - stats["batch_count"] / original_probes if original_probes else 0
    stats["prompt_char_reduction"] = 1 - stats["compact_prompt_chars"] / original_chars if original_chars else 0
    stats["rough_original_prompt_tokens"] = round(original_chars / 4)
    stats["rough_compact_prompt_tokens"] = round(stats["compact_prompt_chars"] / 4)
    return stats


def main():
    import argparse

    parser = argparse.ArgumentParser(description="Build token-efficient, batched model inputs from full probes.")
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--probe-dir")
    source.add_argument("--case-root")
    parser.add_argument("--output-dir", required=True)
    parser.add_argument("--batch-size", type=int, default=8)
    parser.add_argument("--max-occurrences", type=int, default=3)
    parser.add_argument("--analyze-only", action="store_true")
    args = parser.parse_args()
    if args.analyze_only:
        if not args.case_root:
            parser.error("--analyze-only requires --case-root")
        result = analyze_compact_tree(
            args.case_root,
            batch_size=args.batch_size,
            max_occurrences=args.max_occurrences,
        )
        Path(args.output_dir).mkdir(parents=True, exist_ok=True)
        write_json(Path(args.output_dir) / "compaction_analysis.json", result)
    elif args.case_root:
        result = build_compact_tree(
            args.case_root,
            args.output_dir,
            batch_size=args.batch_size,
            max_occurrences=args.max_occurrences,
        )
    else:
        result = build_compact_batches(
            args.probe_dir,
            args.output_dir,
            batch_size=args.batch_size,
            max_occurrences=args.max_occurrences,
        )["manifest"]
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
