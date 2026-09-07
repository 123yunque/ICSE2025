"""Persist an evidence-based stop after a resumed service epoch."""
from datetime import datetime, timezone
from pathlib import Path
from collections import Counter
from lcb_experiment import load, save
from granularity3_local.decomposed_core import read_jsonl

run = Path('runs/fresh100_20260906')
out = Path('reports/fresh100_20260906')
counts = {}
for kind in ['control_flow', 'oracle_state', 'predicted_state', 'repeat_state']:
    requests = read_jsonl(run / 'prepared' / kind / 'requests.jsonl')
    responses = read_jsonl(run / kind / 'model_responses.jsonl')
    attempts = read_jsonl(run / kind / 'api_attempts.jsonl')
    counts[kind] = {'expected': len(requests), 'received': len(responses),
        'attempt_status': dict(Counter(r.get('status') for r in attempts)),
        'received_format': dict(Counter(r.get('validation') for r in attempts if r.get('status') == 'received'))}
record = {'stage': 'oracle_state', 'status': 'stopped',
    'reason': '恢复后服务再次返回 HTTP 401：该令牌额度已用尽。保护电路停止后续网络派发。',
    'updated_at': datetime.now(timezone.utc).isoformat(), 'response_counts': counts,
    'completed_conditions': ['control_flow'], 'incomplete_conditions': ['oracle_state', 'predicted_state', 'repeat_state'],
    'recovery_check': load(Path('runs/recovery_check_20260907/certificate.json')),
    'resume_rules': ['Retain all received first answers and frozen denominators.',
                     'After restoring external service quota, run a new independent recovery epoch.',
                     'Do not count circuit-blocked attempts as received model answers.',
                     'Continue only missing request IDs under the unchanged configuration.']}
save(run / 'progress.json', record)
save(out / 'STOP_20260907.json', record)
save(out / 'recovery_certificate_20260907.json', record['recovery_check'])
print(counts)
