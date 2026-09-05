"""Pinned release_v6 download and outcome-independent 100-question cohort."""
import argparse
import hashlib
import json
import random
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import requests

REVISION = "0fe84c3912ea0c4d4a78037083943e8f0c4dd505"
REPO = "livecodebench/code_generation_lite"
FILES = ["test.jsonl", "test2.jsonl", "test3.jsonl", "test4.jsonl", "test5.jsonl", "test6.jsonl"]


def dump(path, value):
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    Path(path).write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def download(root, name, endpoint):
    path = root / "source" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    url = f"{endpoint}/datasets/{REPO}/resolve/{REVISION}/{name}?download=true"
    info_path = path.with_suffix(".download.json")
    if path.exists() and info_path.exists():
        return json.loads(info_path.read_text(encoding="utf-8"))
    partial = path.with_suffix(".part")
    for attempt in range(5):
        try:
            offset = partial.stat().st_size if partial.exists() else 0
            headers = {"Range": f"bytes={offset}-"} if offset else {}
            with requests.get(url, stream=True, timeout=(30, 120), headers=headers) as response:
                response.raise_for_status()
                append = bool(offset and response.status_code == 206)
                mode = "ab" if append else "wb"
                size = offset if append else 0
                last_log = time.monotonic()
                with partial.open(mode) as handle:
                    for chunk in response.iter_content(1024 * 1024):
                        handle.write(chunk)
                        size += len(chunk)
                        if time.monotonic() - last_log >= 30:
                            print(f"download {name} {size // 1048576} MiB", flush=True)
                            last_log = time.monotonic()
            partial.replace(path)
            digest = hashlib.sha256()
            with path.open("rb") as handle:
                for chunk in iter(lambda: handle.read(1048576), b""):
                    digest.update(chunk)
            info = {"file": name, "bytes": path.stat().st_size, "sha256": digest.hexdigest(),
                    "revision": REVISION, "url": url}
            dump(info_path, info)
            print(f"downloaded {name} {info['bytes'] // 1048576} MiB", flush=True)
            return info
        except Exception as exc:
            print(f"download retry {name} {attempt + 1} {type(exc).__name__}", flush=True)
            if attempt == 4:
                raise
            time.sleep(3 * (attempt + 1))


def prepare(root, count=100, seed=20260905, endpoint="https://hf-mirror.com"):
    root = Path(root)
    with ThreadPoolExecutor(6) as pool:
        futures = [pool.submit(download, root, name, endpoint) for name in FILES]
        downloads = [future.result() for future in as_completed(futures)]
    wanted = set(random.Random(seed).sample(range(1055), count))
    selected, metadata, seen = [], [], set()
    index = 0
    for name in FILES:
        with (root / "source" / name).open(encoding="utf-8") as handle:
            for line_number, line in enumerate(handle, 1):
                row = json.loads(line)
                key = f"{row['platform']}/{row['question_id']}"
                if key in seen:
                    raise ValueError(f"duplicate problem {key}")
                seen.add(key)
                meta = {k: row.get(k) for k in ["platform", "question_id", "question_title", "difficulty", "contest_date", "contest_id"]}
                meta.update(problem_key=key, release_index=index, source_file=name, source_line=line_number)
                metadata.append(meta)
                if index in wanted:
                    row.update(meta)
                    row["task_id"] = f"task_lcb_{index:04d}"
                    selected.append(row)
                index += 1
    if index != 1055 or len(selected) != count:
        raise ValueError(f"release mismatch: {index} problems, {len(selected)} selected")
    with (root / "selected.jsonl").open("w", encoding="utf-8") as handle:
        for row in selected:
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")
    dump(root / "all_metadata.json", metadata)
    selection_bytes = (root / "selected.jsonl").read_bytes()
    dump(root / "manifest.json", {"dataset": REPO, "release": "release_v6", "revision": REVISION,
        "raw_problem_count": index, "selected_problem_count": len(selected), "seed": seed,
        "selection": "uniform sample without replacement from official release_v6 order before any generation",
        "selected_indices": sorted(wanted), "selected_sha256": hashlib.sha256(selection_bytes).hexdigest(),
        "platform_counts": dict(Counter(row["platform"] for row in selected)),
        "difficulty_counts": dict(Counter(row["difficulty"] for row in selected)),
        "selected": [{k: row[k] for k in ["task_id", "problem_key", "release_index", "source_file", "source_line"]} for row in selected],
        "downloads": sorted(downloads, key=lambda row: row["file"])})
    print(f"cohort frozen: {len(selected)} / {index}", flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", default="data")
    parser.add_argument("--count", type=int, default=100)
    parser.add_argument("--seed", type=int, default=20260905)
    parser.add_argument("--endpoint", default="https://hf-mirror.com")
    args = parser.parse_args()
    prepare(args.root, args.count, args.seed, args.endpoint)
