"""Inspect the recorded duplicate-input conflict without calling a model."""
import json
from pathlib import Path
from lcb_experiment import decode_tests, sha, save

with Path('data/selected.jsonl').open(encoding='utf-8') as handle:
    for line in handle:
        row = json.loads(line)
        if row['task_id'] not in {'task_lcb_0810', 'task_lcb_0813', 'task_lcb_0891', 'task_lcb_0974'}:
            continue
        matches = []
        seen = {}
        tests = decode_tests(row['public_test_cases']) + decode_tests(row['private_test_cases'])
        for i, test in enumerate(tests):
            key = (test['testtype'], test['input'])
            if key in seen and seen[key][1] != test['output']:
                before, after = seen[key][1], test['output']
                a = [s.strip() for s in before.strip().split('\n')]
                b = [s.strip() for s in after.strip().split('\n')]
                matches.append({'test_indices': [seen[key][0], i], 'input_sha256': sha(test['input']),
                                'kind': test['testtype'], 'output_a': before[:1000], 'output_b': after[:1000],
                                'line_stripped_equal': a == b})
            else:
                seen[key] = (i, test['output'])
        save(Path('runs/fresh100_20260906/solutions') / row['task_id'] / 'duplicate_audit.json', matches)
        print(json.dumps({'task': row['task_id'], 'question_title': row['question_title'], 'conflicts': matches}, ensure_ascii=False))
