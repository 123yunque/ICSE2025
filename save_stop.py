"""Record the observed external service block and export a compact handoff."""
from datetime import datetime, timezone
from pathlib import Path
from lcb_experiment import load, save

run = Path('runs/fresh100_20260906')
old = load(run / 'progress.json')
save(run / 'progress_before_quota_diagnostic.json', old)
record = {'stage': 'transport_diagnostic', 'status': 'stopped',
    'reason': '服务返回 HTTP 401：该令牌额度已用尽。控制流试跑另有格式合规率不足问题，补充额度后须先完成独立诊断。',
    'updated_at': datetime.now(timezone.utc).isoformat(),
    'build_complete': True, 'full_inference_complete': False,
    'canary': {'received': 40, 'format_valid': 9, 'format_valid_rate': 0.225, 'required_rate': 0.95},
    'resume_rules': ['Do not repeat received main-run first answers.',
                     'Restore quota of the configured service outside this experiment.',
                     'Run independent synthetic transport diagnostics before bulk inference.',
                     'Version and disclose any necessary message/protocol change; do not silently overwrite this run.']}
save(run / 'progress.json', record)
out = Path('reports') / run.name
save(out / 'STOP.json', record)
save(out / 'control_flow_pilot_gate.json', load(run / 'pilot/control_flow_gate.json'))
save(out / 'transport_diagnostics.json', [load(p) for p in sorted(Path('runs/transport_diagnostic_20260906').glob('*.json'))])
