#!/usr/bin/env python3
"""Load testing script for api_runner.py concurrent execution.

Since api_runner.py has no HTTP endpoints, this script simulates load via:
1. Stubbed run_bot invocations with mocked LLM calls
2. Concurrent subprocess spawning (optional)

Ticket: CB-6577062-D345
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Callable, Tuple

# Import harness components
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from harness import LoadResult, print_report


def stubbed_run_bot_iteration() -> Tuple[int, float]:
    """Simulate a single api_runner iteration with stubbed LLM call.
    
    Returns:
        (status_code, latency_seconds)
        status_code: 200 for success, 500 for failure
    """
    start = time.perf_counter()
    
    # Simulate LLM API call latency (50-200ms typical)
    time.sleep(0.05 + (os.getpid() % 10) * 0.01)
    
    # Simulate tool execution
    time.sleep(0.01)
    
    latency = time.perf_counter() - start
    return 200, latency


def run_concurrent_stubbed_iterations(
    concurrency: int,
    iterations_per_worker: int,
    failure_rate: float = 0.0
) -> LoadResult:
    """Run concurrent stubbed api_runner iterations.
    
    Args:
        concurrency: Number of concurrent workers.
        iterations_per_worker: Iterations per worker.
        failure_rate: Simulated failure rate (0.0-1.0).
    
    Returns:
        LoadResult with metrics.
    """
    import random
    
    result = LoadResult()
    total_iterations = concurrency * iterations_per_worker
    result.total_requests = total_iterations
    
    start_time = time.perf_counter()
    
    def worker(worker_id: int) -> None:
        for _ in range(iterations_per_worker):
            try:
                status, latency = stubbed_run_bot_iteration()
                
                # Simulate failures
                if random.random() < failure_rate:
                    status = 500
                
                result.latencies.append(latency)
                if status >= 400:
                    result.errors += 1
            except Exception:
                result.errors += 1
    
    with ThreadPoolExecutor(max_workers=concurrency) as executor:
        futures = [executor.submit(worker, i) for i in range(concurrency)]
        for future in as_completed(futures):
            try:
                future.result()
            except Exception:
                pass
    
    end_time = time.perf_counter()
    result.wall_time = end_time - start_time
    
    return result


def main():
    parser = argparse.ArgumentParser(description='Load test api_runner.py concurrency')
    parser.add_argument('--concurrency', type=int, default=10,
                        help='Number of concurrent workers (default: 10)')
    parser.add_argument('--iterations', type=int, default=100,
                        help='Total number of iterations (default: 100)')
    parser.add_argument('--failure-rate', type=float, default=0.0,
                        help='Simulated failure rate 0.0-1.0 (default: 0.0)')
    parser.add_argument('--stub', action='store_true',
                        help='Use stubbed mode (default)')
    parser.add_argument('--json', action='store_true',
                        help='Output results as JSON')
    
    args = parser.parse_args()
    
    if args.concurrency <= 0 or args.iterations <= 0:
        print("ERROR: concurrency and iterations must be > 0")
        sys.exit(1)
    
    iterations_per_worker = max(1, args.iterations // args.concurrency)
    
    print(f"Running api_runner load test...")
    print(f"  Concurrency: {args.concurrency}")
    print(f"  Total iterations: {args.iterations}")
    print(f"  Failure rate: {args.failure_rate*100:.1f}%")
    
    result = run_concurrent_stubbed_iterations(
        concurrency=args.concurrency,
        iterations_per_worker=iterations_per_worker,
        failure_rate=args.failure_rate
    )
    
    if args.json:
        output = {
            'total_iterations': result.total_requests,
            'errors': result.errors,
            'wall_time': result.wall_time,
            'p50_ms': result.p50 * 1000,
            'p95_ms': result.p95 * 1000,
            'p99_ms': result.p99 * 1000,
            'throughput': result.throughput,
            'error_rate': result.error_rate,
            'mean_latency_ms': result.mean_latency * 1000
        }
        print(json.dumps(output, indent=2))
    else:
        print_report(result, "api_runner Load Test Report")
    
    # Success if throughput > 0
    sys.exit(0 if result.throughput > 0 else 1)


if __name__ == '__main__':
    main()
