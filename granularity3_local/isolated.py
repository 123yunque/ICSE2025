import json
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path


PACKAGE_ROOT = Path(__file__).resolve().parent.parent


def run_isolated_case(
    code_path,
    function_name,
    input_text,
    task_id,
    variant,
    input_id,
    output_dir,
    timeout_seconds=5.0,
    max_events=10000,
    max_output_bytes=20_000_000,
    max_trace_bytes=None,
):
    output_dir = Path(output_dir)
    manifest_path = output_dir / "probes" / "manifest.json"
    if manifest_path.exists():
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        case = json.loads((output_dir / "oracle" / "case.json").read_text(encoding="utf-8"))
        return {
            "status": "cached",
            "event_count": case["event_count"],
            "probe_count": manifest["probe_count"],
            "result": case["result"],
            "output_dir": str(output_dir),
        }
    if output_dir.exists():
        return {"status": "incomplete_output_exists", "output_dir": str(output_dir)}

    output_dir.parent.mkdir(parents=True, exist_ok=True)
    start = time.perf_counter()
    with tempfile.TemporaryDirectory(prefix="g3-worker-", dir=str(output_dir.parent)) as temp_name:
        temp_dir = Path(temp_name)
        request_path = temp_dir / "request.json"
        response_path = temp_dir / "response.json"
        temp_case = temp_dir / "case"
        request = {
            "code_path": str(Path(code_path).resolve()),
            "function_name": function_name,
            "input_text": input_text,
            "task_id": task_id,
            "variant": variant,
            "input_id": input_id,
            "case_dir": str(temp_case),
            "max_events": max_events,
            "max_output_bytes": max_output_bytes,
            "max_trace_bytes": max_trace_bytes if max_trace_bytes is not None else max_output_bytes // 4,
        }
        request_path.write_text(json.dumps(request, ensure_ascii=False), encoding="utf-8")
        command = [
            sys.executable,
            "-m",
            "granularity3_local.legacy.worker",
            "--request",
            str(request_path),
            "--response",
            str(response_path),
        ]
        try:
            completed = subprocess.run(
                command,
                cwd=str(PACKAGE_ROOT),
                capture_output=True,
                text=True,
                timeout=timeout_seconds,
                check=False,
            )
        except subprocess.TimeoutExpired:
            return {
                "status": "timeout",
                "timeout_seconds": timeout_seconds,
                "elapsed_seconds": time.perf_counter() - start,
            }
        if not response_path.exists():
            return {
                "status": "worker_crash",
                "returncode": completed.returncode,
                "stdout": completed.stdout[-1000:],
                "stderr": completed.stderr[-1000:],
                "elapsed_seconds": time.perf_counter() - start,
            }
        response = json.loads(response_path.read_text(encoding="utf-8"))
        response["elapsed_seconds"] = time.perf_counter() - start
        if response["status"] == "success":
            shutil.move(str(temp_case), str(output_dir))
            response["output_dir"] = str(output_dir)
        return response
