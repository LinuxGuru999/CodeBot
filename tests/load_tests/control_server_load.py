#!/usr/bin/env python3
"""Load testing script for control_server.py HTTP endpoints.

Spins an ephemeral ThreadingHTTPServer when --ephemeral-only is set,
or targets a remote server via --target URL.

Ticket: CB-6577062-D345
"""
from __future__ import annotations

import argparse
import json
import os
import socket
import sys
import threading
import time
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from typing import Optional

# Import harness components
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from harness import LoadResult, run_http_load_test, print_report

# Try to import control server components for ephemeral mode
try:
    from codebot.control_server import ControlHandler, RATE_LIMIT_MAX_ATTEMPTS
    HAS_CONTROL_SERVER = True
except ImportError:
    HAS_CONTROL_SERVER = False


def find_free_port() -> int:
    """Find a free port on localhost."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(('127.0.0.1', 0))
        s.listen(1)
        port = s.getsockname()[1]
    return port


class EphemeralServer:
    """Context manager for ephemeral control server (CB-B4086: fail-closed).

    The CONTROL_ALLOW_UNAUTHENTICATED bypass was removed; this harness
    boots the server with a throwaway CONTROL_TOKEN and returns a matching
    Authorization header via ``auth_headers`` so load tests exercise
    authenticated endpoints.
    """

    def __init__(self, token: str = "load-test-token"):
        self.token = token
        self.port: Optional[int] = None
        self.server: Optional[ThreadingHTTPServer] = None
        self.thread: Optional[threading.Thread] = None

    @property
    def auth_headers(self) -> dict:
        return {"Authorization": f"Bearer {self.token}"}

    def __enter__(self):
        if not HAS_CONTROL_SERVER:
            raise RuntimeError("control_server module not available")

        self.port = find_free_port()

        # Set up environment for ephemeral server
        os.environ.pop("CONTROL_ALLOW_UNAUTHENTICATED", None)
        os.environ["CONTROL_TOKEN"] = self.token
        os.environ["PORT"] = str(self.port)
        
        self.server = ThreadingHTTPServer(('127.0.0.1', self.port), ControlHandler)
        self.thread = threading.Thread(target=self.server.serve_forever)
        self.thread.daemon = True
        self.thread.start()
        
        # Wait for server to be ready
        time.sleep(0.1)
        
        return self
    
    def __exit__(self, exc_type, exc_val, exc_tb):
        if self.server:
            self.server.shutdown()
            self.server = None
        if self.thread:
            self.thread.join(timeout=2)
            self.thread = None


def run_scenarios(
    base_url: str,
    headers: dict[str, str],
    concurrency: int,
    requests: int
) -> dict[str, LoadResult]:
    """Run load test scenarios against control server endpoints."""
    results = {}
    
    endpoints = [
        ('/health', 'GET', None),
        ('/bots', 'GET', None),
        ('/scheduler/status', 'GET', None),
    ]
    
    for endpoint, method, body in endpoints:
        url = f"{base_url}{endpoint}"
        print(f"Testing {endpoint}...")
        
        result = run_http_load_test(
            url=url,
            method=method,
            headers=headers,
            body=body,
            concurrency=concurrency,
            requests_per_worker=requests // len(endpoints),
            timeout=10.0
        )
        results[endpoint] = result
        print_report(result, f"Endpoint: {endpoint}")
    
    return results


def main():
    parser = argparse.ArgumentParser(description='Load test control_server.py')
    parser.add_argument('--concurrency', type=int, default=10,
                        help='Number of concurrent workers (default: 10)')
    parser.add_argument('--requests', type=int, default=100,
                        help='Total number of requests (default: 100)')
    parser.add_argument('--target', type=str, default=None,
                        help='Target URL (default: ephemeral localhost)')
    parser.add_argument('--token', type=str, default=None,
                        help='Auth token (default: from CONTROL_TOKEN env)')
    parser.add_argument('--ephemeral-only', action='store_true',
                        help='Force ephemeral localhost server mode')
    parser.add_argument('--json', action='store_true',
                        help='Output results as JSON')
    
    args = parser.parse_args()
    
    # Determine target
    if args.target and not args.ephemeral_only:
        base_url = args.target.rstrip('/')
        cleanup = None
    else:
        if not HAS_CONTROL_SERVER:
            print("ERROR: Cannot run ephemeral server - control_server module not available")
            sys.exit(1)
        
        try:
            with EphemeralServer() as server:
                base_url = f"http://127.0.0.1:{server.port}"
                print(f"Started ephemeral server on {base_url}")

                headers = dict(server.auth_headers)
                if args.token:
                    headers['Authorization'] = f'Bearer {args.token}'
                
                results = run_scenarios(base_url, headers, args.concurrency, args.requests)
                
                if args.json:
                    output = {
                        'endpoints': {
                            k: {
                                'total_requests': v.total_requests,
                                'errors': v.errors,
                                'wall_time': v.wall_time,
                                'p50_ms': v.p50 * 1000,
                                'p95_ms': v.p95 * 1000,
                                'p99_ms': v.p99 * 1000,
                                'throughput': v.throughput,
                                'error_rate': v.error_rate
                            }
                            for k, v in results.items()
                        }
                    }
                    print(json.dumps(output, indent=2))
                
                # Return success if any scenario had throughput > 0
                any_success = any(r.throughput > 0 for r in results.values())
                sys.exit(0 if any_success else 1)
                
        except Exception as e:
            print(f"ERROR: {e}")
            sys.exit(1)
        return
    
    # Remote target mode
    headers = {}
    if args.token:
        headers['Authorization'] = f'Bearer {args.token}'
    elif os.environ.get('CONTROL_TOKEN'):
        headers['Authorization'] = f'Bearer {os.environ["CONTROL_TOKEN"]}'
    
    print(f"Targeting remote server: {base_url}")
    results = run_scenarios(base_url, headers, args.concurrency, args.requests)
    
    if args.json:
        output = {
            'endpoints': {
                k: {
                    'total_requests': v.total_requests,
                    'errors': v.errors,
                    'wall_time': v.wall_time,
                    'p50_ms': v.p50 * 1000,
                    'p95_ms': v.p95 * 1000,
                    'p99_ms': v.p99 * 1000,
                    'throughput': v.throughput,
                    'error_rate': v.error_rate
                }
                for k, v in results.items()
            }
        }
        print(json.dumps(output, indent=2))
    
    sys.exit(0)


if __name__ == '__main__':
    main()
