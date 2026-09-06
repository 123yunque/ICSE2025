"""Export fixed real-case material for human semantic review, before inference."""
import json
from pathlib import Path
from lcb_experiment import load, save
from granularity3_local.decomposed_core import read_jsonl

run = Path('runs/fresh100_20260906')
keys = [('0012', 1), ('0033', 0), ('0042', 0), ('0055', 0), ('0063', 0),
        ('0074', 0), ('0077', 0), ('0079', 0), ('0080', 0), ('0260', 0)]
rows = []
for task_num, index in keys:
    task, inp = 'task_lcb_' + task_num, 'input_' + str(index).zfill(3)
    part = run / 'case_work' / task / inp / 'prepared'
    requests = read_jsonl(part / 'control_flow/requests.jsonl')
    if not requests:
        continue
    states = read_jsonl(part / 'oracle_state/oracles.jsonl')
    compact_states = [r for r in states if len(json.dumps(r['answer'])) <= 1800]
    compact_states.sort(key=lambda r: -len(r['answer']['states']))
    item = {'case_key': task + '/' + inp, 'code': (run / 'solutions' / task / 'code.py').read_text(),
            'args': requests[0]['request']['args'],
            'trace': read_jsonl(part / 'control_flow/oracles.jsonl')[0]['answer']['trace'],
            'states': {r['target_variable']: r['answer']['states'] for r in compact_states[:2]}}
    rows.append(item)
save(run / 'audit/review_material.json', rows)
for item in rows:
    print(json.dumps(item, ensure_ascii=False), flush=True)
