"""Compact status without displaying prompts, hidden tests, or credentials."""
import json
import time
from collections import Counter
from pathlib import Path
from granularity3_local.decomposed_core import read_jsonl

run = Path('runs/fresh100_20260906')
results = read_jsonl(run / 'build_results.jsonl')
pending = []
for task in sorted((run / 'solutions').glob('task_*')):
    if (task / 'result.json').exists():
        continue
    candidates = sorted(task.glob('candidate_*'))
    if candidates:
        candidate = candidates[-1]
        step = 'judging' if (candidate / 'received.json').exists() else 'generating'
        files = list(candidate.rglob('*.json'))
        pending.append({'task': task.name, 'candidate': candidate.name, 'step': step,
                        'seconds_since_update': round(time.time() - max(p.stat().st_mtime for p in files)) if files else None})
summary = {'build_completed': len(results), 'build_status': dict(Counter(r['status'] for r in results)),
           'supported_verified': sum(bool(r.get('preflight', {}).get('supported')) for r in results), 'pending': pending}
for name in ['control_flow', 'oracle_state', 'predicted_state', 'repeat_state']:
    summary[name] = {'received': len(read_jsonl(run / name / 'model_responses.jsonl')),
                     'expected': len(read_jsonl(run / 'prepared' / name / 'requests.jsonl'))}
for name in ['progress.json', 'prepared/summary.json']:
    if (run / name).exists():
        summary[name] = json.loads((run / name).read_text(encoding='utf-8'))
print(json.dumps(summary, ensure_ascii=False, indent=2))
