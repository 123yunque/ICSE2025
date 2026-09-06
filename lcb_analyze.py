"""Fixed-denominator, problem-clustered analysis of the fresh 100-question run."""
import argparse
import csv
import json
import random
from collections import Counter, defaultdict
from pathlib import Path

from lcb_experiment import load, save, sha
from granularity3_local.decomposed_core import read_jsonl, build_messages
from granularity3_local.decomposed_evaluate import build_combined_report


def ratio(a, b):
    return a / b if b else None


def bins(length, state=False):
    thresholds = [(2, '2'), (5, '3-5'), (10, '6-10'), (25, '11-25'), (100, '26-100')]
    if not state:
        thresholds = [(1, '1'), (2, '2')] + thresholds[1:]
    return next((label for limit, label in thresholds if length <= limit), '>100')


def cluster_metrics(rows, metrics, iterations=2000):
    """All inputs/variables for one original problem are sampled together."""
    grouped = defaultdict(list)
    for row in rows:
        grouped[row['task_id']].append(row)
    groups = list(grouped.values())
    output = {}
    if not groups:
        return {key: {'micro': None, 'macro': None, 'cluster_ci95': None} for key in metrics}
    rng = random.Random(20260905)
    totals = [[len(group)] + [sum(float(r[key]) for r in group) for key in metrics] for group in groups]
    samples = [[] for _ in metrics]
    for _ in range(iterations):
        sampled = [totals[rng.randrange(len(groups))] for _ in groups]
        denominator = sum(t[0] for t in sampled)
        for i in range(len(metrics)):
            samples[i].append(sum(t[i + 1] for t in sampled) / denominator)
    for i, key in enumerate(metrics):
        estimates = sorted(samples[i])
        output[key] = {'micro': sum(float(r[key]) for r in rows) / len(rows),
                       'macro': sum(t[i + 1] / t[0] for t in totals) / len(totals),
                       'cluster_ci95': [estimates[int(0.025 * iterations)], estimates[int(0.975 * iterations)]],
                       'problem_count': len(groups), 'denominator': len(rows)}
    return output


def four_cells(rows, left='oracle_cf_state_exact', right='predicted_cf_state_exact'):
    counts = Counter(('correct' if r[left] else 'wrong', 'correct' if r[right] else 'wrong') for r in rows)
    return {a + '_to_' + b: counts[(a, b)] for a in ('correct', 'wrong') for b in ('correct', 'wrong')}


def strata(rows, fields, metrics):
    result = []
    for field in fields:
        values = sorted({str(row.get(field, 'unknown')) for row in rows})
        for value in values:
            group = [r for r in rows if str(r.get(field, 'unknown')) == value]
            result.append({'field': field, 'value': value, 'count': len(group),
                           'problem_count': len({r['task_id'] for r in group}),
                           **{m: sum(float(r[m]) for r in group) / len(group) for m in metrics}})
    return result


def csv_rows(path, rows):
    if not rows:
        return
    fields = list(dict.fromkeys(k for row in rows for k in row))
    with Path(path).open('w', encoding='utf-8-sig', newline='') as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)


def analyze(run, data='data'):
    run = Path(run)
    out = Path('reports') / run.name
    out.mkdir(parents=True, exist_ok=True)
    build = read_jsonl(run / 'build_results.jsonl')
    metadata = {'task_lcb_' + str(r['release_index']).zfill(4): r for r in load(Path(data) / 'all_metadata.json')}
    builds = {r['task_id']: r for r in build}
    cases = {r['case_key']: r for r in read_jsonl(run / 'local/case_records.jsonl')}
    controls = read_jsonl(run / 'prepared/control_flow/requests.jsonl')
    states = read_jsonl(run / 'prepared/oracle_state/requests.jsonl')
    predicted = read_jsonl(run / 'prepared/predicted_state/requests.jsonl')
    cs = read_jsonl(run / 'control_flow/evaluation/scores.jsonl')
    oscores = read_jsonl(run / 'oracle_state/evaluation/scores.jsonl')
    ps = read_jsonl(run / 'predicted_state/evaluation/scores.jsonl')
    combined = build_combined_report(controls, cs, states, oscores, predicted, ps, out / 'combined')
    control_answers = {r['case_key']: r['answer']['trace'] for r in read_jsonl(run / 'prepared/control_flow/oracles.jsonl')}
    state_answers = {r['request_id']: r['answer']['states'] for r in read_jsonl(run / 'prepared/oracle_state/oracles.jsonl')}
    control_scores = {r['case_key']: r for r in cs}
    oracle_scores = {r['request_id']: r for r in oscores}
    predicted_scores = {r['request_id']: r for r in ps}
    predicted_by_parent = {r['parent_state_request_id']: r for r in predicted}
    state_by_key = {(r['case_key'], r['target_variable']): r for r in states}

    def context(request):
        task = request['task_id']
        trace = control_answers[request['case_key']]
        counts = Counter()
        for path, repeats in trace:
            for block in path.split('>'):
                counts[block] += repeats
        length = sum(counts.values())
        return {'problem_key': metadata[task]['problem_key'], 'platform': metadata[task]['platform'],
                'difficulty': metadata[task]['difficulty'], 'contest_date': metadata[task]['contest_date'],
                'interface': builds[task]['interface'], 'test_source': cases[request['case_key']]['test_source'],
                'trace_length': length, 'trace_bin': bins(length), 'long_trace': length >= 10,
                'distinct_blocks': len(counts), 'compressed_runs': len(trace),
                'repeated_block': any(n > 1 for n in counts.values()),
                'selected_candidate': builds[task].get('selected_candidate'),
                'helper_functions': builds[task].get('preflight', {}).get('function_count', 1) > 1}

    control_rows = []
    for request in controls:
        score = control_scores.get(request['case_key'], {})
        control_rows.append({'task_id': request['task_id'], 'case_key': request['case_key'], **context(request),
                             'expanded_exact': bool(score.get('expanded_trace_exact')),
                             'canonical_exact': bool(score.get('canonical_trace_exact'))})
    variable_rows = []
    for row in combined['scores']:
        request = state_by_key[row['case_key'], row['target_variable']]
        rid = request['request_id']
        pred = predicted_by_parent.get(rid)
        oracle_score = oracle_scores.get(rid, {})
        predicted_score = predicted_scores.get(pred['request_id'], {}) if pred else {}
        length = len(state_answers[rid])
        cf_group = ('cf_correct' if row['control_flow_expanded_exact'] else
                    'cf_wrong_constructable' if pred else 'cf_unconstructable')
        variable_rows.append({**row, 'request_id': rid, **context(request), 'cf_group': cf_group,
            'state_length': length, 'state_bin': bins(length, state=True),
            'identical_visible_messages': bool(pred and build_messages(request) == build_messages(pred)),
            'net_drop': int(row['oracle_cf_state_exact']) - int(row['predicted_cf_state_exact']),
            'oracle_position_accuracy': oracle_score.get('state_position_accuracy', 0.0),
            'predicted_position_accuracy': predicted_score.get('state_position_accuracy', 0.0),
            'oracle_prefix': oracle_score.get('correct_prefix_length', 0),
            'predicted_prefix': predicted_score.get('correct_prefix_length', 0),
            'oracle_normalized_prefix': oracle_score.get('correct_prefix_length', 0) / length,
            'predicted_normalized_prefix': predicted_score.get('correct_prefix_length', 0) / length})
    case_rows = combined['case_scores']
    for row in case_rows:
        row.update(context(row))
        row['failure_bucket'] = ('control_flow_failed' if not row['control_flow_expanded_exact'] else
                                'state_failed_given_correct_cf' if not row['predicted_cf_all_variables_exact'] else 'all_correct')
    variable_metrics = ['oracle_cf_state_exact', 'predicted_cf_state_exact', 'net_drop', 'end_to_end_joint_exact',
                        'oracle_position_accuracy', 'predicted_position_accuracy',
                        'oracle_prefix', 'predicted_prefix', 'oracle_normalized_prefix', 'predicted_normalized_prefix']
    case_metrics = ['oracle_cf_all_variables_exact', 'predicted_cf_all_variables_exact', 'end_to_end_case_joint_exact']
    fields = ['platform', 'difficulty', 'interface', 'test_source', 'trace_bin', 'long_trace', 'repeated_block',
              'distinct_blocks', 'compressed_runs', 'selected_candidate', 'helper_functions']
    var_strata = strata(variable_rows, fields + ['state_bin', 'cf_group', 'identical_visible_messages'], variable_metrics)
    cf_strata = strata(control_rows, fields, ['expanded_exact', 'canonical_exact'])
    repeat_requests = read_jsonl(run / 'prepared/repeat_state/requests.jsonl')
    repeat_scores = {r['request_id']: r for r in read_jsonl(run / 'repeat_state/evaluation/scores.jsonl')}
    repeat_rows = [{'task_id': r['task_id'], 'request_id': r['request_id'],
                    'first_exact': bool(oracle_scores.get(r['request_id'], {}).get('state_exact')),
                    'repeat_exact': bool(repeat_scores.get(r['request_id'], {}).get('state_exact'))} for r in repeat_requests]
    response_coverage = {}
    for kind, requests in [('control_flow', controls), ('oracle_state', states), ('predicted_state', predicted), ('repeat_state', repeat_requests)]:
        replies = read_jsonl(run / kind / 'model_responses.jsonl')
        ids = {r['request_id'] for r in requests}
        received = {r['request_id'] for r in replies} & ids
        attempts = read_jsonl(run / kind / 'api_attempts.jsonl')
        response_coverage[kind] = {'expected': len(requests), 'received': len(received),
            'missing': len(ids - received), 'truncated': sum(r.get('finish_reason') == 'length' for r in attempts)}
    exclusions = read_jsonl(run / 'prepared/excluded.jsonl')
    usage = Counter()
    for path in (run / 'solutions').glob('*/candidate_*/received.json'):
        reply = load(path)
        for name in ['prompt_tokens', 'completion_tokens']:
            usage['generation_' + name] += reply.get(name) or 0
    for kind in response_coverage:
        for attempt in read_jsonl(run / kind / 'api_attempts.jsonl'):
            if attempt.get('status') == 'received':
                for name in ['prompt_tokens', 'completion_tokens', 'reasoning_tokens']:
                    usage['inference_' + name] += attempt.get(name) or 0
    paired_groups = {group: four_cells([r for r in variable_rows if r['cf_group'] == group])
                     for group in ['cf_correct', 'cf_wrong_constructable', 'cf_unconstructable']}
    candidate_failures = Counter()
    for result in build:
        for attempt in result.get('attempts', []):
            if attempt.get('status') == 'invalid_candidate':
                candidate_failures['invalid_code_response_or_interface'] += 1
            else:
                verdict = attempt.get('verdict', {})
                if verdict.get('status') != 'passed':
                    label = verdict.get('metadata', {}).get('error_message', verdict.get('status', 'unknown'))
                    if str(label).startswith('Wrong answer'):
                        label = 'Wrong answer'
                    candidate_failures[str(label)] += 1
    summary = {'run_id': run.name, 'protocol': 'g3-decomposed-v2',
        'complete': len(build) == 100 and not any(r['status'] == 'api_incomplete' for r in build)
                    and (run / 'prepared/summary.json').exists()
                    and load(run / 'prepared/summary.json')['problem_count'] == 100
                    and (not states or (run / 'prepared/predicted_state/requests.jsonl').exists())
                    and all(r['missing'] == 0 for r in response_coverage.values())
                    and len(repeat_requests) == min(40, len(states)),
        'raw_pool': 1055, 'selected_problems': 100, 'build_status': dict(Counter(r['status'] for r in build)),
        'candidate_failure_counts': dict(candidate_failures),
        'verified_supported_problems': sum(bool(r.get('preflight', {}).get('supported')) for r in build),
        'eligible_problems': len({r['task_id'] for r in controls}),
        'N_control_cases': len(controls), 'K_state_variables': len(states), 'M_state_cases': len(case_rows),
        'responses': response_coverage, 'combined': combined['summary'],
        'control_metrics': cluster_metrics(control_rows, ['expanded_exact', 'canonical_exact']),
        'variable_metrics': cluster_metrics(variable_rows, variable_metrics),
        'case_metrics': cluster_metrics(case_rows, case_metrics),
        'paired_four_cells': four_cells(variable_rows), 'paired_by_cf': paired_groups,
        'identical_message_pairs': four_cells([r for r in variable_rows if r['identical_visible_messages']]),
        'repeat_control': {'count': len(repeat_rows), 'four_cells': four_cells(repeat_rows, 'first_exact', 'repeat_exact')},
        'case_failure_buckets': dict(Counter(r['failure_bucket'] for r in case_rows)),
        'exclusion_reasons': dict(Counter(str(r.get('error_type') or r.get('reason') or r.get('status')) for r in exclusions)),
        'token_usage': dict(usage), 'bootstrap': {'unit': 'original problem; all inputs and variables together', 'iterations': 2000, 'seed': 20260905}}
    save(out / 'summary.json', summary)
    save(out / 'cohort.json', load(run / 'cohort.json'))
    save(out / 'run_config.json', load(run / 'config.json'))
    for name in ['execution_source.json', 'preparation_policy.json']:
        if (run / name).exists():
            save(out / name, load(run / name))
    csv_rows(out / 'control_cases.csv', control_rows)
    csv_rows(out / 'paired_variables.csv', variable_rows)
    csv_rows(out / 'state_cases.csv', case_rows)
    csv_rows(out / 'control_strata.csv', cf_strata)
    csv_rows(out / 'state_strata.csv', var_strata)
    csv_rows(out / 'repeat_state.csv', repeat_rows)
    csv_rows(out / 'problem_funnel.csv', [{'task_id': r['task_id'], 'problem_key': r['problem_key'],
        'status': r['status'], 'support': r.get('preflight', {}).get('status'),
        'selected_candidate': r.get('selected_candidate'), 'total_tests': r.get('total_tests'),
        'interface': r.get('interface')} for r in build])
    def metric_line(label, result):
        if result['micro'] is None:
            return '| ' + label + ' | 无可评测请求 | — |'
        lo, hi = result['cluster_ci95']
        return f"| {label} | {result['micro']:.2%} | [{lo:.2%}, {hi:.2%}] |"
    text = [f'# LiveCodeBench 100 题实验：{run.name}', '',
        '状态：' + ('本批实验完成。' if summary['complete'] else '进行中；以下仅为当前已落盘记录，不是最终结果。'), '',
        f"固定从 release_v6 的 1,055 题均匀抽取 100 题，种子 20260905。所有解答和模型回答来自本次运行。",
        f"构建状态：`{json.dumps(summary['build_status'], ensure_ascii=False)}`。",
        f"进入控制流评测 {summary['eligible_problems']} 题，N={len(controls)} 个输入案例；状态分母 K={len(states)} 个变量请求，M={len(case_rows)} 个案例。", '',
        '| 指标 | 全请求准确率 | 按题目 bootstrap 95% 区间 |', '| --- | ---: | ---: |',
        metric_line('控制流展开 exact', summary['control_metrics']['expanded_exact']),
        metric_line('Oracle-CF 状态 exact', summary['variable_metrics']['oracle_cf_state_exact']),
        metric_line('Predicted-CF 状态 exact（同 K）', summary['variable_metrics']['predicted_cf_state_exact']),
        metric_line('端到端案例联合 exact（同 M）', summary['case_metrics']['end_to_end_case_joint_exact']), '',
        '状态配对四格：`' + json.dumps(summary['paired_four_cells']) + '`。',
        '重复调用对照：`' + json.dumps(summary['repeat_control']) + '`。', '',
        '无效及缺失回答按冻结分母计失败。代码构建失败、已知数据问题和执行器排除单独报告；这是派生执行推理实验，不能作为官方代码生成 pass@1。',
        '两次状态调用的差异包含模型调用波动与路径表示差异，不能全部归为控制流的因果影响。', '',
        '固定长轨迹次要分析使用 L≥10；分桶、宏平均、正确前缀、配对分组及覆盖明细见同目录 CSV 和 summary.json。',
        '原始候选、全部测试判定、Oracle、首答和请求指纹保存在 `runs/' + run.name + '/`。']
    (out / 'REPORT.md').write_text('\n'.join(text) + '\n', encoding='utf-8')
    print(json.dumps({k: summary[k] for k in ['complete', 'build_status', 'eligible_problems', 'N_control_cases', 'K_state_variables', 'M_state_cases', 'responses']}, ensure_ascii=False), flush=True)
    return summary


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--run', default='runs/fresh100_20260906')
    parser.add_argument('--data', default='data')
    args = parser.parse_args()
    analyze(args.run, args.data)
