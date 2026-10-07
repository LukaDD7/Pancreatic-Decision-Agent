#!/usr/bin/env python3
"""Call the user-specified MaaS endpoint with one fixed, nonclinical message."""

import argparse
import getpass
import json
import os
from pathlib import Path
import sys
import time
from datetime import datetime, timezone
from uuid import uuid4

import openai
from openai import OpenAI


BASE_URL = "https://apicz.boyuerichdata.com/v1/"
PROJECT_ROOT = Path(__file__).resolve().parents[1]
MESSAGES = [{"role": "user", "content": "你是谁"}]


def load_key():
    """Use only this provider's dedicated key, never ambient OPENAI_API_KEY."""
    key = os.environ.get("BOYU_API_KEY", "").strip()
    if key:
        return key
    path = PROJECT_ROOT / ".env.local"
    if path.is_file():
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            name, value = line.split("=", 1)
            if name.strip() == "BOYU_API_KEY":
                value = value.strip()
                if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
                    value = value[1:-1]
                if value:
                    return value
    if not sys.stdin.isatty():
        raise ValueError("请在本机 .env.local 设置 BOYU_API_KEY，或在终端交互运行此脚本。")
    key = getpass.getpass("输入该 MaaS 平台 API key（不回显、不保存）：").strip()
    if not key:
        raise ValueError("API key 为空；未发送请求。")
    return key


def save_result(record):
    output = PROJECT_ROOT / "outputs" / "connection_tests"
    output.mkdir(parents=True, exist_ok=True)
    path = output / f"{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}_{uuid4().hex[:8]}.json"
    path.write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"本地记录：{path}")


def run(args, client):
    record = {
        "purpose": "nonclinical_connectivity_check",
        "base_url": BASE_URL,
        "requested_model": args.model,
        "messages": MESSAGES,
        "max_tokens": args.max_tokens,
        "temperature": args.temperature,
        "sdk_version": openai.__version__,
        "started_at_utc": datetime.now(timezone.utc).isoformat(),
        "attempts": [],
    }
    for attempt in range(1, args.retries + 2):
        started = time.monotonic()
        try:
            response = client.chat.completions.create(
                model=args.model,
                messages=MESSAGES,
                max_tokens=args.max_tokens,
                temperature=args.temperature,
            )
        except (openai.APIStatusError, openai.APIConnectionError) as exc:
            status = getattr(exc, "status_code", None)
            transient = status in {408, 409, 429} or (status is not None and status >= 500) or isinstance(exc, openai.APIConnectionError)
            record["attempts"].append({
                "attempt": attempt, "error_type": type(exc).__name__,
                "http_status": status, "elapsed_seconds": round(time.monotonic() - started, 3),
            })
            # Do not echo provider exception bodies, headers, credentials or request data.
            print(f"请求失败：{type(exc).__name__}，HTTP={status}")
            if not transient or attempt > args.retries:
                record["status"] = "failed"
                save_result(record)
                return 1
            print(f"{args.retry_delay:g} 秒后重试（{attempt}/{args.retries}）。")
            time.sleep(args.retry_delay)
            continue
        record["attempts"].append({"attempt": attempt, "elapsed_seconds": round(time.monotonic() - started, 3)})
        record["response"] = response.model_dump(mode="json")
        choice = response.choices[0] if response.choices else None
        content = choice.message.content if choice else None
        record["status"] = "succeeded" if content else "response_received_without_answer_text"
        print(f"请求已返回；响应模型字段：{response.model}")
        if content:
            print(content)
        else:
            print("响应没有最终回答文本；接口已返回，但尚未验证完整生成。")
        if choice and choice.finish_reason == "length":
            print("输出达到 token 上限；可增加 --max-tokens 后再测。")
        save_result(record)
        return 0 if content else 2
    return 1


def main():
    os.umask(0o077)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model", default="kimi-k3")
    parser.add_argument("--max-tokens", type=int, default=200)
    parser.add_argument("--temperature", type=float, default=1.0)
    parser.add_argument("--timeout", type=float, default=60.0)
    parser.add_argument("--retries", type=int, default=5)
    parser.add_argument("--retry-delay", type=float, default=30.0)
    args = parser.parse_args()
    if args.max_tokens <= 0 or args.timeout <= 0 or args.retries < 0 or args.retry_delay < 0:
        parser.error("token/timeout 必须为正，重试次数/间隔不能为负。")
    try:
        key = load_key()
    except (ValueError, EOFError) as exc:
        print(str(exc), file=sys.stderr)
        return 1
    with OpenAI(api_key=key, base_url=BASE_URL, timeout=args.timeout, max_retries=0) as client:
        return run(args, client)


if __name__ == "__main__":
    raise SystemExit(main())
