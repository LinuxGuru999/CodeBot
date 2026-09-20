"""Core load testing harness — stdlib-only concurrent execution and metrics.

Ticket: CB-6577062-D345

Provides run_load_test() which fires concurrent requests via
concurrent.futures.ThreadPoolExecutor, captures per-request latency with
time.perf_counter, and aggregates p50/p95/p99 latency, throughput (req/s),
and error rate.  No external dependencies.
"""
from __future__ import annotations

import statistics
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from typing import Any, Callable


@dataclass
class LoadResult:
    """Aggregated load test metrics."""

    total_requests: int = 0
    successful_requests: int = 0
    failed_requests: int = 0
    error_rate: float = 0.0
    wall_time: float = 0.0
    throughput: float = 0.0  # requests per second
    latencies: list[float] = field(default_factory=list)
    p50: float = 0.0
    p95: float = 0.0
    p99: float = 0.0
    min_latency: float = 0.0
    max_latency: float = 0.0
    mean_latency: float = 0.0
    errors: list[dict[str, Any]] = field(default_factory=list)

    def compute_percentiles(self) -> None:
        """Compute latency percentiles from collected latencies."""
        if not self.latencies:
            return
        sorted_lat = sorted(self.latencies)
        self.p50 = _percentile(sorted_lat, 0.50)
        self.p95 = _percentile(sorted_lat, 0.95)
        self.p99 = _percentile(sorted_lat, 0.99)
        self.min_latency = sorted_lat[0]
        self.max_latency = sorted_lat[-1]
        self.mean_latency = statistics.mean(sorted_lat)

    def compute_derived(self) -> None:
        """Compute throughput and error rate from raw counts and wall time."""
        self.total_requests = self.successful_requests + self.failed_requests
        self.error_rate = (
            self.failed_requests / self.total_requests if self.total_requests > 0 else 0.0
        )
        self.throughput = (
            self.total_requests / self.wall_time if self.wall_time > 0 else 0.0
        )

    def to_dict(self) -> dict[str, Any]:
        """Serialize result to a JSON-friendly dict."""
        return {
            "total_requests": self.total_requests,
            "successful_requests": self.successful_requests,
            "failed_requests": self.failed_requests,
            "error_rate": round(self.error_rate, 4),
            "wall_time": round(self.wall_time, 4),
            "throughput": round(self.throughput, 2),
            "p50_ms": round(self.p50 * 1000, 2),
            "p95_ms": round(self.p95 * 1000, 2),
            "p99_ms": round(self.p99 * 1000, 2),
            "min_latency_ms": round(self.min_latency * 1000, 2),
            "max_latency_ms": round(self.max_latency * 1000, 2),
            "mean_latency_ms": round(self.mean_latency * 1000, 2),
            "errors": self.errors[:20],  # cap error list for display
        }


def _percentile(sorted_data: list[float], pct: float) -> float:
    """Compute percentile from pre-sorted data using linear interpolation."""
    if not sorted_data:
        return 0.0
    k = (len(sorted_data) - 1) * pct
    f = int(k)
    c = f + 1
    if c >= len(sorted_data):
        return sorted_data[-1]
    d = k - f
    return sorted_data[f] + d * (sorted_data[c] - sorted_data[f])


def run_load_test(
    target_fn: Callable[[], Any],
    concurrency: int = 10,
    requests_per_worker: int = 10,
    timeout: float = 60.0,
    label: str = "",
) -> LoadResult:
    """Execute a load test by calling target_fn concurrently.

    Args:
        target_fn: A zero-argument callable that performs one request/action.
            Should raise an exception on failure.
        concurrency: Number of concurrent worker threads.
        requests_per_worker: Each worker calls target_fn this many times.
        timeout: Max seconds to wait for all futures to complete.
        label: Optional label for reporting (e.g., endpoint name).

    Returns:
        LoadResult with aggregated metrics.
    """
    if concurrency <= 0:
        raise ValueError("concurrency must be >= 1")
    if requests_per_worker <= 0:
        raise ValueError("requests_per_worker must be >= 1")

    result = LoadResult()
    total_tasks = concurrency * requests_per_worker

    wall_start = time.perf_counter()
    with ThreadPoolExecutor(max_workers=concurrency) as executor:
        futures = {}
        for worker_id in range(concurrency):
            for _ in range(requests_per_worker):
                fut = executor.submit(target_fn)
                futures[fut] = worker_id

        for fut in as_completed(futures, timeout=timeout):
            try:
                value = fut.result()
                # If the call returned an error indicator, count it as failure
                if isinstance(value, dict) and value.get("_error"):
                    result.failed_requests += 1
                    result.errors.append({"type": "returned_error", "detail": str(value["_error"])})
                else:
                    result.successful_requests += 1
            except Exception as exc:
                result.failed_requests += 1
                result.errors.append({
                    "type": type(exc).__name__,
                    "detail": str(exc)[:200],
                })

    wall_end = time.perf_counter()
    result.wall_time = wall_end - wall_start
    result.compute_derived()
    result.compute_percentiles()
    return result


def print_report(result: LoadResult, label: str = "", file=None) -> None:
    """Print a human-readable load test report to stdout (or file).

    Args:
        result: LoadResult to display.
        label: Optional label for the report header.
        file: Output file object (defaults to sys.stdout).
    """
    out = file or sys.stdout
    header = f"{'=' * 60}\nLoad Test Report"
    if label:
        header += f" — {label}"
    header += f"\n{'=' * 60}"
    print(header, file=out)
    print(f"  Total requests:    {result.total_requests}", file=out)
    print(f"  Successful:        {result.successful_requests}", file=out)
    print(f"  Failed:            {result.failed_requests}", file=out)
    print(f"  Error rate:        {result.error_rate:.2%}", file=out)
    print(f"  Wall time:         {result.wall_time:.3f}s", file=out)
    print(f"  Throughput:        {result.throughput:.1f} req/s", file=out)
    if result.latencies:
        print(f"  Latency (ms):", file=out)
        print(f"    p50:             {result.p50 * 1000:.1f}", file=out)
        print(f"    p95:             {result.p95 * 1000:.1f}", file=out)
        print(f"    p99:             {result.p99 * 1000:.1f}", file=out)
        print(f"    min:             {result.min_latency * 1000:.1f}", file=out)
        print(f"    max:             {result.max_latency * 1000:.1f}", file=out)
        print(f"    mean:            {result.mean_latency * 1000:.1f}", file=out)
    else:
        print("  Latency:           N/A (no successful requests)", file=out)
    if result.errors:
        print(f"  Errors (first {min(len(result.errors), 5)}):", file=out)
        for err in result.errors[:5]:
            print(f"    - {err['type']}: {err['detail'][:100]}", file=out)
    print(f"{'=' * 60}", file=out)


def latency_instrumented(fn: Callable) -> Callable:
    """Decorator that measures and returns (latency_seconds, result) from fn.

    Use this to wrap individual request functions so run_load_test collects
    latency automatically.

    Usage::

        @latency_instrumented
        def my_request():
            ...
            return response_data

        result = run_load_test(my_request, concurrency=10, requests_per_worker=100)
    """
    def wrapper(*args, **kwargs):
        start = time.perf_counter()
        try:
            value = fn(*args, **kwargs)
            elapsed = time.perf_counter() - start
            return {"_latency": elapsed, "_result": value}
        except Exception:
            elapsed = time.perf_counter() - start
            raise  # let run_load_test's except handler count it
    wrapper.__name__ = getattr(fn, "__name__", "unknown")
    wrapper.__doc__ = getattr(fn, "__doc__", "")
    return wrapper
