"""Core load testing harness using stdlib concurrent.futures and urllib.

Provides run_load_test() for concurrent request execution with latency,
throughput, and error rate measurement. No external dependencies.

Ticket: CB-6577062-D345
"""
from __future__ import annotations

import statistics
import sys
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from typing import Any, Callable, Optional


@dataclass
class LoadResult:
    """Results from a load test run."""
    latencies: list[float] = field(default_factory=list)
    errors: int = 0
    total_requests: int = 0
    wall_time: float = 0.0
    
    @property
    def p50(self) -> float:
        """50th percentile latency in seconds."""
        if not self.latencies:
            return 0.0
        sorted_lat = sorted(self.latencies)
        idx = int(len(sorted_lat) * 0.50)
        return sorted_lat[min(idx, len(sorted_lat) - 1)]
    
    @property
    def p95(self) -> float:
        """95th percentile latency in seconds."""
        if not self.latencies:
            return 0.0
        sorted_lat = sorted(self.latencies)
        idx = int(len(sorted_lat) * 0.95)
        return sorted_lat[min(idx, len(sorted_lat) - 1)]
    
    @property
    def p99(self) -> float:
        """99th percentile latency in seconds."""
        if not self.latencies:
            return 0.0
        sorted_lat = sorted(self.latencies)
        idx = int(len(sorted_lat) * 0.99)
        return sorted_lat[min(idx, len(sorted_lat) - 1)]
    
    @property
    def throughput(self) -> float:
        """Requests per second."""
        if self.wall_time <= 0:
            return 0.0
        return self.total_requests / self.wall_time
    
    @property
    def error_rate(self) -> float:
        """Fraction of requests that failed."""
        if self.total_requests == 0:
            return 0.0
        return self.errors / self.total_requests
    
    @property
    def mean_latency(self) -> float:
        """Mean latency in seconds."""
        if not self.latencies:
            return 0.0
        return statistics.mean(self.latencies)


def run_load_test(
    target_fn: Callable[[], tuple[int, float]],
    concurrency: int = 10,
    requests_per_worker: int = 10,
    timeout: float = 30.0
) -> LoadResult:
    """Run a load test with concurrent workers.
    
    Args:
        target_fn: Function that returns (status_code, latency_seconds).
                   Should raise exception on failure.
        concurrency: Number of concurrent worker threads.
        requests_per_worker: Number of requests each worker makes.
        timeout: Timeout for the entire test run.
    
    Returns:
        LoadResult with latencies, errors, and computed metrics.
    """
    if concurrency <= 0 or requests_per_worker <= 0:
        raise ValueError("concurrency and requests_per_worker must be > 0")
    
    result = LoadResult()
    total_requests = concurrency * requests_per_worker
    result.total_requests = total_requests
    
    start_time = time.perf_counter()
    
    def worker(worker_id: int) -> None:
        for _ in range(requests_per_worker):
            try:
                status, latency = target_fn()
                result.latencies.append(latency)
                if status >= 400:
                    result.errors += 1
            except Exception:
                result.errors += 1
    
    with ThreadPoolExecutor(max_workers=concurrency) as executor:
        futures = [executor.submit(worker, i) for i in range(concurrency)]
        for future in as_completed(futures, timeout=timeout):
            try:
                future.result()
            except Exception:
                pass  # Errors already counted in worker
    
    end_time = time.perf_counter()
    result.wall_time = end_time - start_time
    
    return result


def run_http_load_test(
    url: str,
    method: str = "GET",
    headers: dict[str, str] | None = None,
    body: bytes | None = None,
    concurrency: int = 10,
    requests_per_worker: int = 10,
    timeout: float = 30.0,
    read_limit: int = 1024 * 1024
) -> LoadResult:
    """Run a load test against an HTTP endpoint.
    
    Args:
        url: Target URL.
        method: HTTP method (GET, POST, etc.).
        headers: Optional HTTP headers.
        body: Optional request body.
        concurrency: Number of concurrent worker threads.
        requests_per_worker: Number of requests each worker makes.
        timeout: Timeout per request.
        read_limit: Max bytes to read from response.
    
    Returns:
        LoadResult with latencies, errors, and computed metrics.
    """
    headers = headers or {}
    
    def make_request() -> tuple[int, float]:
        req = urllib.request.Request(url, data=body, headers=headers, method=method)
        start = time.perf_counter()
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                resp.read(read_limit)
                latency = time.perf_counter() - start
                return resp.status, latency
        except urllib.error.HTTPError as e:
            latency = time.perf_counter() - start
            e.read(read_limit)
            return e.code, latency
        except Exception:
            latency = time.perf_counter() - start
            raise
    
    return run_load_test(
        make_request,
        concurrency=concurrency,
        requests_per_worker=requests_per_worker,
        timeout=timeout * concurrency * requests_per_worker + 10
    )


def print_report(result: LoadResult, title: str = "Load Test Report") -> None:
    """Print a human-readable load test report."""
    print(f"\n{'='*60}")
    print(f"{title}")
    print(f"{'='*60}")
    print(f"Total Requests:     {result.total_requests}")
    print(f"Successful:         {result.total_requests - result.errors}")
    print(f"Errors:             {result.errors}")
    print(f"Wall Time:          {result.wall_time:.3f}s")
    print(f"\nLatency:")
    print(f"  Mean:             {result.mean_latency*1000:.2f}ms")
    print(f"  p50:              {result.p50*1000:.2f}ms")
    print(f"  p95:              {result.p95*1000:.2f}ms")
    print(f"  p99:              {result.p99*1000:.2f}ms")
    print(f"\nThroughput:         {result.throughput:.2f} req/s")
    print(f"Error Rate:         {result.error_rate*100:.2f}%")
    print(f"{'='*60}\n")


if __name__ == "__main__":
    # Simple smoke test
    def dummy_target() -> tuple[int, float]:
        time.sleep(0.01)
        return 200, 0.01
    
    result = run_load_test(dummy_target, concurrency=2, requests_per_worker=5)
    print_report(result, "Smoke Test")
    sys.exit(0 if result.errors == 0 else 1)
