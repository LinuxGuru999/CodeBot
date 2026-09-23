# Load Testing Harness for CodeBot Core Services

**Ticket:** CB-6577062-D345  
**Purpose:** Provide stdlib-only load testing capabilities for core services without external dependencies like locust.

## Overview

This harness uses Python's built-in `concurrent.futures.ThreadPoolExecutor` and `urllib.request` to simulate concurrent users and measure:
- **Latency:** p50, p95, p99 percentiles in milliseconds
- **Throughput:** Requests per second (req/s)
- **Error Rate:** Percentage of failed requests

## Prerequisites

- Python 3.8+
- No external dependencies required (stdlib-only)
- For control_server tests: the `codebot.control_server` module must be importable

## Scripts

### 1. control_server_load.py

Load tests the HTTP endpoints of `control_server.py` (GET /health, /bots, /scheduler/status).

#### Modes

**Ephemeral localhost (default, safe for CI):**
```bash
python3 tests/load_tests/control_server_load.py --ephemeral-only --concurrency 10 --requests 100
```

> Note (CB-B4086): the legacy `CONTROL_ALLOW_UNAUTHENTICATED=1` prefix is
> removed — the harness boots with a throwaway `CONTROL_TOKEN` and matching
> `Authorization` header automatically.

**Remote target (requires explicit confirmation):**
```bash
python3 tests/load_tests/control_server_load.py --target https://your-server.fly.dev --token $CONTROL_TOKEN --concurrency 5 --requests 50
```

#### Options

| Option | Default | Description |
|--------|---------|-------------|
| `--concurrency` | 10 | Number of concurrent worker threads |
| `--requests` | 100 | Total number of requests to send |
| `--target` | None | Remote URL (omit for ephemeral localhost) |
| `--token` | None | Auth token (or set CONTROL_TOKEN env) |
| `--ephemeral-only` | False | Force ephemeral localhost mode |
| `--json` | False | Output results as JSON |

### 2. api_runner_load.py

Simulates concurrent `api_runner.py` execution since it has no HTTP endpoints. Uses stubbed LLM calls with configurable latency.

#### Usage

```bash
# Stubbed mode (default)
python3 tests/load_tests/api_runner_load.py --concurrency 10 --iterations 100 --stub

# With simulated failures
python3 tests/load_tests/api_runner_load.py --concurrency 5 --iterations 50 --failure-rate 0.1 --json
```

#### Options

| Option | Default | Description |
|--------|---------|-------------|
| `--concurrency` | 10 | Number of concurrent workers |
| `--iterations` | 100 | Total iterations to run |
| `--failure-rate` | 0.0 | Simulated failure rate (0.0-1.0) |
| `--stub` | True | Use stubbed mode |
| `--json` | False | Output results as JSON |

## Interpreting Results

### Latency Percentiles

- **p50 (median):** Typical response time for half your users
- **p95:** Response time for 95% of users (good SLA target)
- **p99:** Response time for worst 1% of requests (critical for tail latency)

Example output:
```
Latency:
  Mean:             45.23ms
  p50:              42.10ms
  p95:              78.50ms
  p99:              125.30ms
```

### Throughput

Measured in requests per second (req/s). Higher is better, but watch for:
- Diminishing returns as concurrency increases
- Throughput drops when error rate spikes (rate limiting, resource exhaustion)

### Error Rate

Percentage of requests that failed (status >= 400 or exception).

- **0%:** Ideal
- **<1%:** Acceptable for most scenarios
- **>5%:** Investigate rate limiting, server capacity, or network issues

**Common errors:**
- `401 Unauthorized:` Missing or invalid CONTROL_TOKEN
- `429 Too Many Requests:` Hit RateLimiter (5 attempts/60s window, 300s cooldown)
- `Connection refused:` Server not running or wrong port

## Rate Limiting Interaction

The `control_server.py` has a built-in `RateLimiter`:
- **5 attempts** per **60-second** window per IP
- **300-second cooldown** after exceeding limit

When load testing:
1. Use ephemeral localhost mode to avoid triggering production rate limits
2. If testing remote, space out test runs or increase `RATE_LIMIT_MAX_ATTEMPTS` env var
3. 429 errors are counted in error_rate but won't crash the harness

## Safety Warnings

⚠️ **NEVER run load tests against production without:**
1. Explicit `--target` flag (never defaults to production)
2. Understanding of rate limiting consequences
3. Approval from system owners

The harness defaults to ephemeral localhost servers to prevent accidental DoS.

## Example Commands

### Quick smoke test (ephemeral)
```bash
python3 tests/load_tests/control_server_load.py --ephemeral-only --concurrency 2 --requests 10
```

### Moderate load test
```bash
python3 tests/load_tests/control_server_load.py --ephemeral-only --concurrency 20 --requests 200 --json
```

### API runner concurrency test
```bash
python3 tests/load_tests/api_runner_load.py --concurrency 15 --iterations 150
```

## Sample Output

```
============================================================
Endpoint: /health
============================================================
Total Requests:     100
Successful:         98
Errors:             2
Wall Time:          2.341s

Latency:
  Mean:             23.45ms
  p50:              21.20ms
  p95:              45.80ms
  p99:              67.30ms

Throughput:         42.72 req/s
Error Rate:         2.00%
============================================================
```

JSON output (`--json`):
```json
{
  "endpoints": {
    "/health": {
      "total_requests": 100,
      "errors": 2,
      "wall_time": 2.341,
      "p50_ms": 21.2,
      "p95_ms": 45.8,
      "p99_ms": 67.3,
      "throughput": 42.72,
      "error_rate": 0.02
    }
  }
}
```

## Troubleshooting

**"control_server module not available"**
- Ensure you're in the project root: `cd /home/kozuka/Work/CodeBot`
- Check PYTHONPATH includes the project root

**"Connection refused"**
- Ephemeral server may have failed to start; check for port conflicts
- Remote target may be down or firewall-blocked

**High error rate (>50%)**
- Check if rate limiting is triggered (429 errors)
- Verify auth token is valid (401 errors)
- Reduce concurrency to avoid overwhelming the server

## Architecture Notes

- **No external deps:** Uses only stdlib (`concurrent.futures`, `urllib.request`, `statistics`)
- **Thread-based concurrency:** Simulates real user load better than async for CPU-bound servers
- **Bounded reads:** All HTTP responses capped at 1MB to prevent memory exhaustion
- **Ephemeral by default:** Never targets production without explicit flags

## Files

- `harness.py` - Core load testing engine (run_load_test, LoadResult, print_report)
- `control_server_load.py` - HTTP endpoint load tester for control_server.py
- `api_runner_load.py` - Concurrent execution simulator for api_runner.py
- `__init__.py` - Package marker
- `README.md` - This documentation
