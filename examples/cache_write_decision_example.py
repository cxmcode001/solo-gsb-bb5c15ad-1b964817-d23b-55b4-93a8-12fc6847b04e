#!/usr/bin/env python3
"""/cache-write-decision 调用示例（仅依赖 Python 3 标准库）。

脚本完整演示一次“回源验收 → 共享缓存写入决策”的典型流程：
  1. 调用 /plan 规划回源块；
  2. 模拟上游对每个 Range 请求返回 206 Partial Content（携带
     Content-Range、强 ETag 与响应体）；
  3. 把整批块提交 /verify-fetch-batch 验收，拿到确认的强 ETag；
  4. 对每个验收通过的块调用 /cache-write-decision：根据对象 URI、块区间、
     确认的 ETag、原请求头与上游 Cache-Control/Age/Vary 判定能否写入
     共享缓存，并取回稳定缓存键（同一输入多次调用得到同一个键）；
  5. 演示三种典型拒绝：no-store、带 Authorization 但未显式允许共享、
     Age 耗尽新鲜期（过期）。

用法：
    python examples/cache_write_decision_example.py

环境变量：
    RANGE_PLANNER_URL  服务地址（默认 http://127.0.0.1:8080）
"""

from __future__ import annotations

import base64
import json
import os
import sys
import urllib.error
import urllib.request

BASE_URL = os.environ.get("RANGE_PLANNER_URL", "http://127.0.0.1:8080")

OBJECT_LENGTH = 256
OBJECT_URI = "https://objects.example.com/bucket/blob-1"
# 模拟上游当前对象版本的强 ETag（弱标签 W/ 会被服务拒绝）。
ETAG = '"demo-v1"'
# 客户端原始请求头：代理回源时携带的协商头会进入 Vary 缓存键。
REQUEST_HEADERS = {"Accept-Encoding": "gzip", "User-Agent": "demo-agent/1.0"}


def post_json(path: str, payload: dict) -> dict:
    req = urllib.request.Request(
        f"{BASE_URL}{path}",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        print(f"POST {path} -> HTTP {exc.code}: {exc.read().decode()}")
        sys.exit(1)


def fake_upstream_fetch(obj: bytes, start: int, end: int) -> dict:
    """模拟上游对 Range: bytes=start-end 的 206 响应要素。"""
    return {
        "status_code": 206,
        "content_range": f"bytes {start}-{end}/{len(obj)}",
        "etag": ETAG,
        "body": obj[start : end + 1],
    }


def decide_write(
    block: dict, etag: str, cache_control: str, age: str, vary: str | None
) -> dict:
    """对单个已验收块调用 /cache-write-decision。"""
    body: dict = {
        "uri": OBJECT_URI,
        "start": block["start"],
        "end": block["end"],
        "etag": etag,
        "request_headers": REQUEST_HEADERS,
        "cache_control": cache_control,
        "age": age,
    }
    if vary is not None:
        body["vary"] = vary
    return post_json("/cache-write-decision", body)


def main() -> None:
    obj = bytes((i % 256 for i in range(OBJECT_LENGTH)))

    # 1. 规划：请求 [0,99] 与末尾 16 字节。
    plan = post_json(
        "/plan",
        {
            "object_length": OBJECT_LENGTH,
            "ranges": [
                {"type": "interval", "start": 0, "end": 99},
                {"type": "suffix", "last_n": 16},
            ],
            "max_gap": 8,
            "fetch_limit": 128,
        },
    )
    print("blocks:", [(b["index"], b["start"], b["end"]) for b in plan["blocks"]])

    # 2. 按 range_header 逐块“回源”（这里用本地对象模拟）。
    responses = []
    for block in plan["blocks"]:
        upstream = fake_upstream_fetch(obj, block["start"], block["end"])
        responses.append(
            {
                "block_index": block["index"],
                "status_code": upstream["status_code"],
                "content_range": upstream["content_range"],
                "etag": upstream["etag"],
                "body_b64": base64.b64encode(upstream["body"]).decode("ascii"),
            }
        )

    # 3. 整批验收：拿到确认的强 ETag，作为缓存键的版本组成部分。
    verified = post_json(
        "/verify-fetch-batch",
        {
            "object_length": OBJECT_LENGTH,
            "blocks": plan["blocks"],
            "responses": responses,
            "expected_etag": ETAG,
        },
    )
    confirmed_etag = verified["etag"]
    print("confirmed etag:", confirmed_etag)

    # 4. 逐块做共享缓存写入决策。上游响应头：Cache-Control 给 1 小时新鲜期
    #    （已缓存 60 秒），Vary 指定 Accept-Encoding 参与缓存键。
    cache_control = "max-age=3600"
    age = "60"
    vary = "Accept-Encoding"
    keys = []
    for block in plan["blocks"]:
        d = decide_write(block, confirmed_etag, cache_control, age, vary)
        assert d["cacheable"], d
        keys.append(d["cache_key"])
        print(
            f"块 [{block['start']},{block['end']}]: cacheable={d['cacheable']}, "
            f"remaining={d['remaining_seconds']}s, key={d['cache_key'][:16]}..."
        )
    # 缓存键稳定：同一输入再次调用得到完全相同的键。
    again = decide_write(plan["blocks"][0], confirmed_etag, cache_control, age, vary)
    assert again["cache_key"] == keys[0], "同一输入必须得到同一个缓存键"
    # 不同块区间 / 不同 Vary 取值都会得到不同的键。
    assert len(set(keys)) == len(keys)
    other = decide_write(plan["blocks"][0], confirmed_etag, cache_control, age, vary)
    assert other["cache_key"] == keys[0]
    print("OK: 缓存键对同一输入稳定，且随块区间区分")

    # 5. 典型拒绝场景（HTTP 200，cacheable=false，reason 标明原因）。
    d = decide_write(plan["blocks"][0], confirmed_etag, "no-store", age, vary)
    print(f"no-store        -> cacheable={d['cacheable']}, reason={d['reason']}")
    assert d["reason"] == "no_store"

    # 客户端请求带 Authorization，而响应未显式允许共享缓存。
    body = {
        "uri": OBJECT_URI,
        "start": 0,
        "end": 99,
        "etag": confirmed_etag,
        "request_headers": {**REQUEST_HEADERS, "Authorization": "Bearer t"},
        "cache_control": "max-age=3600",
    }
    d = post_json("/cache-write-decision", body)
    print(f"authorization   -> cacheable={d['cacheable']}, reason={d['reason']}")
    assert d["reason"] == "authorization_not_shared"

    # Age 耗尽新鲜期：剩余不足 1 秒即视为过期。
    d = decide_write(
        plan["blocks"][0], confirmed_etag, "max-age=60", "60", vary
    )
    print(f"expired         -> cacheable={d['cacheable']}, reason={d['reason']}")
    assert d["reason"] == "expired"

    print("OK: 写入决策链路演示完成（可缓存判定 + 三类拒绝均符合预期）")


if __name__ == "__main__":
    main()
