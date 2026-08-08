#!/usr/bin/env python3
"""Run a redacted, dependency-free official-vs-TRBotApi benchmark.

The script intentionally records bot ids and measurements only.  It never
serializes tokens, API credentials or the chat id into the history file.
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import shutil
import statistics
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path


DEFAULT_TEST_DC = "149.154.167.40:80"
DEFAULT_SAMPLES = 5
HTTP_TIMEOUT = 12.0
STARTUP_TIMEOUT = 120.0


def percentile(values: list[float], fraction: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    index = min(len(ordered) - 1, int((len(ordered) - 1) * fraction))
    return round(ordered[index], 3)


def summarize(samples: list[dict]) -> dict:
    latencies = [float(item["latency_ms"]) for item in samples if item.get("latency_ms") is not None]
    successes = sum(1 for item in samples if item.get("ok") is True)
    return {
        "samples": len(samples),
        "successes": successes,
        "success_rate": round(successes / len(samples), 4) if samples else 0.0,
        "mean_ms": round(statistics.fmean(latencies), 3) if latencies else None,
        "p50_ms": percentile(latencies, 0.50),
        "p95_ms": percentile(latencies, 0.95),
        "min_ms": round(min(latencies), 3) if latencies else None,
        "max_ms": round(max(latencies), 3) if latencies else None,
        "last_error_code": next(
            (item.get("error_code") for item in reversed(samples) if item.get("error_code") is not None),
            None,
        ),
    }


def bot_id(token: str) -> str:
    value = token.split(":", 1)[0].strip()
    if not value.isdigit():
        raise ValueError("a bot token must start with a numeric bot id")
    return value


def request_json(url: str, payload: dict | None, timeout: float = HTTP_TIMEOUT) -> dict:
    start = time.perf_counter()
    body = None if payload is None else json.dumps(payload, separators=(",", ":")).encode()
    request = urllib.request.Request(
        url,
        data=body,
        method="POST",
        headers={"content-type": "application/json"},
    )
    result: dict = {"ok": False, "error_code": None, "description": "request failed"}
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read(1_048_576)
            result = json.loads(raw.decode("utf-8"))
    except urllib.error.HTTPError as error:
        try:
            result = json.loads(error.read(1_048_576).decode("utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            result = {"ok": False, "error_code": error.code, "description": "HTTP error"}
    except (OSError, TimeoutError, urllib.error.URLError, json.JSONDecodeError) as error:
        result = {"ok": False, "error_code": None, "description": type(error).__name__}
    result["latency_ms"] = round((time.perf_counter() - start) * 1000.0, 3)
    result["ok"] = bool(result.get("ok"))
    result["error_code"] = result.get("error_code")
    # Keep the dashboard useful without retaining arbitrary server text.
    if not result["ok"]:
        result["description"] = str(result.get("description", "error"))[:120]
    else:
        result.pop("description", None)
    return result


def run_series(url: str, payload: dict | None, samples: int) -> dict:
    return summarize([request_json(url, payload) for _ in range(samples)])


def proc_field(pid: int, name: str) -> int | None:
    try:
        text = Path(f"/proc/{pid}/status").read_text(encoding="utf-8")
    except OSError:
        return None
    for line in text.splitlines():
        if line.startswith(name + ":"):
            fields = line.split()
            if len(fields) >= 2:
                try:
                    return int(fields[1])
                except ValueError:
                    return None
    return None


def smaps_field(pid: int, name: str) -> int | None:
    try:
        text = Path(f"/proc/{pid}/smaps_rollup").read_text(encoding="utf-8")
    except OSError:
        return None
    for line in text.splitlines():
        if line.startswith(name + ":"):
            fields = line.split()
            if len(fields) >= 2:
                try:
                    return int(fields[1])
                except ValueError:
                    return None
    return None


def memory_snapshot(pid: int) -> dict:
    return {
        "rss_kib": proc_field(pid, "VmRSS"),
        "hwm_kib": proc_field(pid, "VmHWM"),
        "vmsize_kib": proc_field(pid, "VmSize"),
        "threads": proc_field(pid, "Threads"),
        "pss_kib": smaps_field(pid, "Pss"),
    }


def server_url(port: int, token: str, method: str, test_path: bool = False) -> str:
    suffix = "/test" if test_path else ""
    return f"http://127.0.0.1:{port}/bot{token}{suffix}/{method}"


def start_server(
    trbotapi_dir: Path,
    token: str,
    api_id: str,
    api_hash: str,
    test_dc: str,
    port: int,
) -> tuple[subprocess.Popen, tempfile.NamedTemporaryFile]:
    binary = trbotapi_dir / "target" / "release" / "trbotapi-server"
    if not binary.is_file():
        raise FileNotFoundError(f"missing release binary: {binary}")
    log = tempfile.NamedTemporaryFile(prefix="trbotapi-benchmark-", suffix=".log", mode="w+b")
    environment = os.environ.copy()
    environment.update(
        {
            "TRBOTAPI_BIND": f"127.0.0.1:{port}",
            "TRBOTAPI_WORKERS": "1",
            "TRBOTAPI_BOT_ID": bot_id(token),
            "TRBOTAPI_BOT_TOKEN": token,
            "TRBOTAPI_API_ID": api_id,
            "TRBOTAPI_API_HASH": api_hash,
            "TRBOTAPI_TEST_DC": test_dc,
            "TRBOTAPI_CONNECT_TEST_DC": "1",
        }
    )
    process = subprocess.Popen(
        [str(binary)],
        cwd=trbotapi_dir,
        env=environment,
        stdin=subprocess.DEVNULL,
        stdout=log,
        stderr=log,
        close_fds=True,
    )
    return process, log


def start_official_server(
    official_dir: Path,
    api_id: str,
    api_hash: str,
    port: int,
) -> tuple[subprocess.Popen, tempfile.NamedTemporaryFile, Path]:
    binary = official_dir / "build" / "telegram-bot-api"
    if not binary.is_file():
        raise FileNotFoundError(f"missing official release binary: {binary}")
    work_dir = Path(tempfile.mkdtemp(prefix="official-bot-api-"))
    log = tempfile.NamedTemporaryFile(prefix="official-benchmark-", suffix=".log", mode="w+b")
    environment = os.environ.copy()
    # Keep credentials out of argv and therefore out of `ps` output.  The
    # official server documents these two environment variables as equivalents
    # of --api-id/--api-hash.
    environment.update({"TELEGRAM_API_ID": api_id, "TELEGRAM_API_HASH": api_hash})
    process = subprocess.Popen(
        [
            str(binary),
            "--local",
            "--http-ip-address",
            "127.0.0.1",
            "--http-port",
            str(port),
            "--dir",
            str(work_dir),
        ],
        cwd=official_dir,
        env=environment,
        stdin=subprocess.DEVNULL,
        stdout=log,
        stderr=log,
        close_fds=True,
    )
    return process, log, work_dir


def wait_for_server(process: subprocess.Popen, token: str, port: int, test_path: bool = False) -> dict:
    deadline = time.monotonic() + STARTUP_TIMEOUT
    last = None
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise RuntimeError("TRBotApi process exited before its HTTP listener became ready")
        last = request_json(server_url(port, token, "getMe", test_path), {})
        if last.get("ok"):
            return last
        time.sleep(0.5)
    raise TimeoutError(f"HTTP listener did not become ready: {last or 'no response'}")


def stop_server(process: subprocess.Popen, log: tempfile.NamedTemporaryFile) -> None:
    try:
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=8)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=4)
    finally:
        log.close()
        try:
            os.unlink(log.name)
        except OSError:
            pass


def stop_official_server(
    process: subprocess.Popen,
    log: tempfile.NamedTemporaryFile,
    work_dir: Path,
) -> None:
    stop_server(process, log)
    shutil.rmtree(work_dir, ignore_errors=True)


def benchmark_bot(
    token: str,
    official_port: int,
    official_process: subprocess.Popen,
    trbotapi_port: int,
    trbotapi_process: subprocess.Popen,
    samples: int,
    chat_id: str,
) -> dict:
    identifier = bot_id(token)
    chat_payload = {"chat_id": int(chat_id), "action": "typing"}
    official_get_me = run_series(server_url(official_port, token, "getMe", True), {}, samples)
    trbotapi_get_me = run_series(server_url(trbotapi_port, token, "getMe"), {}, samples)
    official_chat_action = run_series(
        server_url(official_port, token, "sendChatAction", True), chat_payload, 1
    )
    trbotapi_chat_action = run_series(
        server_url(trbotapi_port, token, "sendChatAction"), chat_payload, 1
    )
    return {
        "id": identifier,
        "official_get_me": official_get_me,
        "trbotapi_get_me": trbotapi_get_me,
        "official_chat_action": official_chat_action,
        "trbotapi_chat_action": trbotapi_chat_action,
        "official_memory": memory_snapshot(official_process.pid),
        "trbotapi_memory": memory_snapshot(trbotapi_process.pid),
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--trbotapi-dir", type=Path, required=True)
    parser.add_argument("--official-dir", type=Path, required=True)
    parser.add_argument("--history", type=Path, required=True)
    parser.add_argument("--samples", type=int, default=int(os.environ.get("BENCHMARK_SAMPLES", DEFAULT_SAMPLES)))
    parser.add_argument("--test-dc", default=os.environ.get("TRBOTAPI_TEST_DC", DEFAULT_TEST_DC))
    return parser.parse_args()


def git_revision(directory: Path) -> str:
    try:
        return subprocess.check_output(
            ["git", "-C", str(directory), "rev-parse", "HEAD"],
            text=True,
            stderr=subprocess.DEVNULL,
        ).strip()
    except (OSError, subprocess.CalledProcessError):
        return "unknown"


def memory_record(start: dict, after: dict) -> dict:
    return {
        "rss_start_kib": start.get("rss_kib"),
        "rss_after_kib": after.get("rss_kib"),
        "hwm_kib": after.get("hwm_kib"),
        "pss_kib": after.get("pss_kib"),
        "vmsize_kib": after.get("vmsize_kib"),
        "threads": after.get("threads"),
    }


def main() -> int:
    args = parse_args()
    if args.samples < 1 or args.samples > 100:
        raise ValueError("--samples must be between 1 and 100")
    tokens = [os.environ.get("TRBOTAPI_BOT_TOKEN_A", ""), os.environ.get("TRBOTAPI_BOT_TOKEN_B", "")]
    api_id = os.environ.get("TRBOTAPI_API_ID", "")
    api_hash = os.environ.get("TRBOTAPI_API_HASH", "")
    chat_id = os.environ.get("TRBOTAPI_CHAT_ID", "")
    if not all(tokens) or not api_id or not api_hash or not chat_id:
        raise ValueError("TRBOTAPI_BOT_TOKEN_A/B, TRBOTAPI_API_ID, TRBOTAPI_API_HASH and TRBOTAPI_CHAT_ID are required")
    for token in tokens:
        bot_id(token)
    int(chat_id)

    official_processes: list[tuple[subprocess.Popen, tempfile.NamedTemporaryFile, Path, str, int]] = []
    trbotapi_processes: list[tuple[subprocess.Popen, tempfile.NamedTemporaryFile, str, int]] = []
    started_at = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    try:
        for index, token in enumerate(tokens):
            official_process, official_log, work_dir = start_official_server(
                args.official_dir, api_id, api_hash, 19081 + index
            )
            official_processes.append((official_process, official_log, work_dir, token, 19081 + index))
            trbotapi_process, trbotapi_log = start_server(
                args.trbotapi_dir, token, api_id, api_hash, args.test_dc, 18081 + index
            )
            trbotapi_processes.append((trbotapi_process, trbotapi_log, token, 18081 + index))

        official_startup_memory = {}
        for process, _log, _work_dir, token, port in official_processes:
            wait_for_server(process, token, port, test_path=True)
            official_startup_memory[bot_id(token)] = memory_snapshot(process.pid)
        trbotapi_startup_memory = {}
        for process, _log, token, port in trbotapi_processes:
            wait_for_server(process, token, port)
            trbotapi_startup_memory[bot_id(token)] = memory_snapshot(process.pid)

        bots = {}
        for index, token in enumerate(tokens):
            official_process, _official_log, _work_dir, _official_token, official_port = official_processes[index]
            trbotapi_process, _trbotapi_log, _trbotapi_token, trbotapi_port = trbotapi_processes[index]
            measurement = benchmark_bot(
                token,
                official_port,
                official_process,
                trbotapi_port,
                trbotapi_process,
                args.samples,
                chat_id,
            )
            measurement["official_memory"] = memory_record(
                official_startup_memory[measurement["id"]], measurement.pop("official_memory")
            )
            measurement["trbotapi_memory"] = memory_record(
                trbotapi_startup_memory[measurement["id"]], measurement.pop("trbotapi_memory")
            )
            bots[measurement["id"]] = measurement

        trbotapi_binary = args.trbotapi_dir / "target" / "release" / "trbotapi-server"
        official_binary = args.official_dir / "build" / "telegram-bot-api"
        result = {
            "schema": 1,
            "status": "ok",
            "generated_at": started_at,
            "repositories": {
                "trbotapi": {"name": "tdd761914-arch/TRBotApi", "sha": git_revision(args.trbotapi_dir)},
                "official": {"name": "tdlib/telegram-bot-api", "sha": git_revision(args.official_dir)},
            },
            "runner": {"os": platform.platform(), "python": platform.python_version()},
            "samples_per_method": args.samples,
            "test_dc": args.test_dc,
            "artifact": {
                "trbotapi_release_binary_bytes": trbotapi_binary.stat().st_size,
                "official_release_binary_bytes": official_binary.stat().st_size,
            },
            "bots": bots,
        }
    finally:
        for process, log, _work_dir, _token, _port in official_processes:
            stop_official_server(process, log, _work_dir)
        for process, log, _token, _port in trbotapi_processes:
            stop_server(process, log)

    args.history.parent.mkdir(parents=True, exist_ok=True)
    try:
        history = json.loads(args.history.read_text(encoding="utf-8"))
        if not isinstance(history, list):
            history = []
    except (FileNotFoundError, json.JSONDecodeError):
        history = []
    history.append(result)
    args.history.write_text(json.dumps(history[-720:], indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"status": result["status"], "generated_at": result["generated_at"], "bots": list(result["bots"]), "history_entries": len(history[-720:])}, indent=2))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as error:  # do not print process environments or tokens
        print(f"benchmark failed: {type(error).__name__}: {error}", file=sys.stderr)
        raise
