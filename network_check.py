"""Check required public endpoints without displaying credentials."""
import json
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import urlsplit

import requests


def check(url):
    try:
        response = requests.get(url, timeout=30)
        result = {"url": url, "status": response.status_code, "bytes": len(response.content)}
        if response.status_code == 200 and "/api/datasets/" in url:
            result["revision"] = response.json().get("sha")
        return result
    except Exception as exc:
        return {"url": url, "error": type(exc).__name__}


if __name__ == "__main__":
    print(json.dumps({"proxies": {key: urlsplit(value if "://" in value else "http://" + value).hostname
                                  for key, value in urllib.request.getproxies().items()}}), flush=True)
    endpoints = [
        "https://hf-mirror.com/api/datasets/livecodebench/code_generation_lite",
        "https://huggingface.co/api/datasets/livecodebench/code_generation_lite",
        "https://raw.githubusercontent.com/LiveCodeBench/LiveCodeBench/main/lcb_runner/evaluation/testing_util.py",
    ]
    with ThreadPoolExecutor(3) as pool:
        for result in pool.map(check, endpoints):
            print(json.dumps(result), flush=True)
