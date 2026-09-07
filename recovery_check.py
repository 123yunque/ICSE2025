"""Independent service recovery check; no replacement of dataset first answers."""
import os
import argparse
from pathlib import Path
from datetime import datetime, timezone
from concurrent.futures import ThreadPoolExecutor
from lcb_experiment import save, load, worker_process, sha, create_http_compatible_client
from granularity3_local.decomposed_core import read_jsonl, build_messages
from granularity3_local.decomposed_api import run_api_experiment
from granularity3_local.oracle import write_jsonl

parser = argparse.ArgumentParser()
parser.add_argument('--run', default='runs/recovery_check_20260907')
args = parser.parse_args()
ROOT = Path(args.run)
MAIN = Path('runs/fresh100_20260906')
PROGRAMS = [
    ('def solve(n):\n    total = 0\n    for i in range(n):\n        if i % 2:\n            total -= i\n        else:\n            total += i\n    return total\n', [(n,) for n in [0, 1, 5, 20, 60]]),
    ('def solve(n):\n    total = 0\n    for i in range(n):\n        for j in range(i):\n            total += i * j\n    return total\n', [(n,) for n in [0, 1, 3, 6, 9]]),
    ('def solve(a, b):\n    steps = 0\n    while b:\n        a, b = b, a % b\n        steps += 1\n    return a\n', [(1, 0), (8, 4), (21, 13), (100, 27), (233, 144)]),
    ('def solve(xs, target):\n    for i, value in enumerate(xs):\n        if value == target:\n            return i\n    return -1\n', [([], 1), ([1], 1), ([1, 2, 3], 2), ([2, 2, 2], 4), (list(range(35)), 34)]),
    ('def solve(n):\n    xs = []\n    for i in range(n):\n        xs.append(i)\n        if i % 2:\n            xs[-1] *= -1\n    return xs\n', [(n,) for n in [0, 1, 4, 12, 25]]),
    ('def solve(xs):\n    counts = {}\n    for x in xs:\n        counts[x] = counts.get(x, 0) + 1\n    return counts\n', [([],), ([1],), ([1, 2, 1],), ([3, 2, 3, 2, 1],), ([i % 4 for i in range(30)],)]),
    ('def solve(raw_input):\n    numbers = list(map(int, raw_input.split()))\n    total = 0\n    for number in numbers:\n        total += number\n    return str(total)\n', [(s,) for s in ['', '1\n', '1 2 -3\n', '4\n5\n6\n', ' '.join(str(i) for i in range(30))]]),
    ('def solve(n):\n    a, b = 0, 1\n    for i in range(n):\n        a, b = b, a + b\n    return a\n', [(n,) for n in [0, 1, 4, 12, 25]]),
]


def prepare_case(item):
    program, index, source, args = item
    task, inp = f'task_recovery_{program:03d}', f'input_{index:03d}'
    root = ROOT / 'cases' / task / inp
    result_path = root / 'result.json'
    if result_path.exists():
        return load(result_path)
    root.mkdir(parents=True, exist_ok=True)
    code_path = root / 'code.py'
    code_path.write_text(source, encoding='utf-8')
    result = worker_process({'mode': 'oracle', 'task_id': task, 'input_id': inp,
        'code_path': str(code_path.resolve()), 'input_text': repr(args),
        'local_root': str((root / 'local').resolve())}, root / 'worker', 90)
    if result.get('status') != 'success':
        raise RuntimeError(str(result))
    save(result_path, result)
    return result


def main():
    ROOT.mkdir(parents=True, exist_ok=True)
    save(ROOT / 'design.json', {'purpose': 'independent engineering recovery check',
         'programs': PROGRAMS, 'selection_before_calls': True, 'format_threshold': 0.95,
         'accuracy_used_as_gate': False, 'original_dataset_responses_unchanged': True})
    jobs = [(p, i, source, args) for p, (source, inputs) in enumerate(PROGRAMS) for i, args in enumerate(inputs)]
    with ThreadPoolExecutor(3) as pool:
        records = list(pool.map(prepare_case, jobs))
    controls, control_oracles, states, state_oracles = [], [], [], []
    for record in records:
        part = Path(record['prepared_dir'])
        controls += read_jsonl(part / 'control_flow/requests.jsonl')
        control_oracles += read_jsonl(part / 'control_flow/oracles.jsonl')
        sr = read_jsonl(part / 'oracle_state/requests.jsonl')
        so = {r['request_id']: r for r in read_jsonl(part / 'oracle_state/oracles.jsonl')}
        if not sr:
            # Zero-change calls remain CF-only, as in the main protocol.
            continue
        chosen = max(sr, key=lambda r: len(so[r['request_id']]['answer']['states']))
        states.append(chosen)
        state_oracles.append(so[chosen['request_id']])
    config = load(MAIN / 'config.json')
    client = create_http_compatible_client(os.environ['YUNWU_API_KEY'], config['api_base_url'])
    gates = {}
    for kind, requests, oracles in [('control_flow', controls, control_oracles), ('oracle_state', states, state_oracles)]:
        path = ROOT / kind
        summary = run_api_experiment(client, requests, oracles, path, config['model'], config['api_base_url'],
            timeout=180, max_completion_tokens=16384, retries=0, retry_invalid=False,
            reasoning_effort='low', verbosity='low', concurrency=3, json_mode=True,
            resume=(path / 'run_config.json').exists(), resume_received=True, progress_every=10)
        evaluation = summary['evaluation']
        truncations = sum(r.get('finish_reason') == 'length' for r in read_jsonl(path / 'api_attempts.jsonl'))
        gates[kind] = {'received': summary['response_count'], 'expected': len(requests),
            'format_valid_rate': evaluation['format_valid_rate'], 'truncations': truncations,
            'passed': evaluation['response_complete'] and evaluation['format_valid_rate'] >= 0.95 and not truncations}
        save(ROOT / 'gates.json', gates)
        print(kind + ' recovery gate: ' + str(gates[kind]), flush=True)
        if not gates[kind]['passed']:
            raise RuntimeError('Independent recovery format check failed; inspect diagnostics')
    save(ROOT / 'certificate.json', {'passed': True, 'gates': gates, 'model': config['model'],
         'api_base_url': config['api_base_url'], 'completed_at': datetime.now(timezone.utc).isoformat(),
         'control_messages_sha256': sha([build_messages(r) for r in controls]),
         'state_messages_sha256': sha([build_messages(r) for r in states]),
         'protocol_change': False, 'main_first_answers_replaced': False})


if __name__ == '__main__':
    main()
