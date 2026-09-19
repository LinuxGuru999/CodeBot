#!/usr/bin/env python3
"""Tests for web_tools.py SSRF protection and redirect handling."""

import unittest
from unittest.mock import patch, MagicMock
import urllib.request
import urllib.parse
import socket

from codebot.web_tools import (
    _SSRFRedirectHandler,
    _resolve_and_validate_host,
    _is_blocked_ip,
    _is_blocked_url,
)


class TestIsBlockedIp(unittest.TestCase):
    """Tests for _is_blocked_ip helper."""

    def test_blocks_private_ipv4(self):
        self.assertTrue(_is_blocked_ip("192.168.1.1"))
        self.assertTrue(_is_blocked_ip("10.0.0.1"))
        self.assertTrue(_is_blocked_ip("172.16.0.1"))

    def test_blocks_loopback(self):
        self.assertTrue(_is_blocked_ip("127.0.0.1"))
        self.assertTrue(_is_blocked_ip("::1"))

    def test_blocks_link_local(self):
        self.assertTrue(_is_blocked_ip("169.254.169.254"))

    def test_allows_public_ipv4(self):
        self.assertFalse(_is_blocked_ip("8.8.8.8"))
        self.assertFalse(_is_blocked_ip("1.1.1.1"))
        self.assertFalse(_is_blocked_ip("93.184.216.34"))  # example.com

    def test_blocks_invalid_ip(self):
        self.assertTrue(_is_blocked_ip("not-an-ip"))


class TestResolveAndValidateHost(unittest.TestCase):
    """Tests for _resolve_and_validate_host."""

    def test_validates_ip_directly(self):
        # If host is an IP, it should be validated directly
        ip, port, family = _resolve_and_validate_host("8.8.8.8", 80)
        self.assertEqual(ip, "8.8.8.8")
        self.assertEqual(port, 80)

    def test_blocks_private_ip_directly(self):
        with self.assertRaises(ValueError) as context:
            _resolve_and_validate_host("192.168.1.1", 80)
        self.assertIn("blocked IP", str(context.exception))

    @patch("socket.getaddrinfo")
    def test_resolves_hostname_to_public_ip(self, mock_getaddrinfo):
        # Mock DNS resolution to return a public IP
        mock_getaddrinfo.return_value = [
            (socket.AF_INET, socket.SOCK_STREAM, 6, '', ('8.8.8.8', 80))
        ]
        ip, port, family = _resolve_and_validate_host("example.com", 80)
        self.assertEqual(ip, "8.8.8.8")
        mock_getaddrinfo.assert_called_once_with("example.com", 80, socket.AF_UNSPEC, socket.SOCK_STREAM)

    @patch("socket.getaddrinfo")
    def test_blocks_hostname_resolving_to_private_ip(self, mock_getaddrinfo):
        # Mock DNS resolution to return a private IP
        mock_getaddrinfo.return_value = [
            (socket.AF_INET, socket.SOCK_STREAM, 6, '', ('192.168.1.1', 80))
        ]
        with self.assertRaises(ValueError) as context:
            _resolve_and_validate_host("malicious.com", 80)
        self.assertIn("blocked IP", str(context.exception))

    @patch("socket.getaddrinfo")
    def test_blocks_hostname_resolving_to_localhost(self, mock_getaddrinfo):
        mock_getaddrinfo.return_value = [
            (socket.AF_INET, socket.SOCK_STREAM, 6, '', ('127.0.0.1', 80))
        ]
        with self.assertRaises(ValueError) as context:
            _resolve_and_validate_host("localhost", 80)
        self.assertIn("blocked IP", str(context.exception))

    @patch("socket.getaddrinfo")
    def test_dns_resolution_failure(self, mock_getaddrinfo):
        mock_getaddrinfo.side_effect = socket.gaierror("Name resolution failed")
        with self.assertRaises(ValueError) as context:
            _resolve_and_validate_host("nonexistent.domain", 80)
        self.assertIn("DNS resolution failed", str(context.exception))


class TestSSRFRedirectHandler(unittest.TestCase):
    """Tests for _SSRFRedirectHandler."""

    def setUp(self):
        self.handler = _SSRFRedirectHandler()
        # Create a dummy request for context
        self.dummy_req = urllib.request.Request("http://example.com/start")
        self.dummy_fp = None
        self.dummy_headers = {}

    def test_redirect_to_private_ip_blocked(self):
        """Redirect to a hostname resolving to a private IP should be blocked."""
        newurl = "http://internal.service/path"
        with patch("socket.getaddrinfo") as mock_getaddrinfo:
            mock_getaddrinfo.return_value = [
                (socket.AF_INET, socket.SOCK_STREAM, 6, '', ('192.168.1.1', 80))
            ]
            with self.assertRaises(ValueError) as context:
                self.handler.redirect_request(
                    self.dummy_req, self.dummy_fp, 302, "Found", self.dummy_headers, newurl
                )
            self.assertIn("blocked IP", str(context.exception))

    def test_redirect_to_localhost_blocked(self):
        """Redirect to localhost should be blocked."""
        newurl = "http://localhost/admin"
        with self.assertRaises(ValueError) as context:
            self.handler.redirect_request(
                self.dummy_req, self.dummy_fp, 302, "Found", self.dummy_headers, newurl
            )
        self.assertIn("blocked host", str(context.exception))

    def test_redirect_to_127_0_0_1_blocked(self):
        """Redirect to 127.0.0.1 should be blocked."""
        newurl = "http://127.0.0.1/admin"
        with self.assertRaises(ValueError) as context:
            self.handler.redirect_request(
                self.dummy_req, self.dummy_fp, 302, "Found", self.dummy_headers, newurl
            )
        self.assertIn("blocked host", str(context.exception))

    def test_redirect_to_link_local_blocked(self):
        """Redirect to link-local address should be blocked."""
        newurl = "http://169.254.169.254/latest/meta-data/"
        with self.assertRaises(ValueError) as context:
            self.handler.redirect_request(
                self.dummy_req, self.dummy_fp, 302, "Found", self.dummy_headers, newurl
            )
        self.assertIn("blocked host", str(context.exception))

    @patch("socket.getaddrinfo")
    def test_redirect_to_public_ip_allowed(self, mock_getaddrinfo):
        """Redirect to a hostname resolving to a public IP should be allowed."""
        mock_getaddrinfo.return_value = [
            (socket.AF_INET, socket.SOCK_STREAM, 6, '', ('93.184.216.34', 80))
        ]
        newurl = "http://example.com/redirected"
        
        # We need to mock super().redirect_request to avoid actual network calls
        # and to verify it was called
        with patch.object(urllib.request.HTTPRedirectHandler, 'redirect_request') as mock_super_redirect:
            mock_super_redirect.return_value = urllib.request.Request(newurl)
            
            result = self.handler.redirect_request(
                self.dummy_req, self.dummy_fp, 302, "Found", self.dummy_headers, newurl
            )
            
            mock_super_redirect.assert_called_once()
            self.assertIsNotNone(result)

    def test_redirect_to_empty_host_blocked(self):
        """Redirect with no host should be blocked."""
        newurl = "http:///path"
        result = self.handler.redirect_request(
            self.dummy_req, self.dummy_fp, 302, "Found", self.dummy_headers, newurl
        )
        self.assertIsNone(result)

    @patch("socket.getaddrinfo")
    def test_redirect_chain_dns_rebinding_blocked(self, mock_getaddrinfo):
        """Simulate DNS rebinding: first resolve public, then private.
        
        Note: The current implementation resolves DNS at the time of redirect.
        If an attacker controls DNS, they can change the IP between checks.
        However, our handler resolves and validates *before* following.
        This test verifies that if the DNS returns a private IP at redirect time,
        it is blocked.
        """
        # Attacker makes malicious.com resolve to private IP at redirect time
        mock_getaddrinfo.return_value = [
            (socket.AF_INET, socket.SOCK_STREAM, 6, '', ('10.0.0.1', 80))
        ]
        newurl = "http://malicious.com/steal"
        
        with self.assertRaises(ValueError) as context:
            self.handler.redirect_request(
                self.dummy_req, self.dummy_fp, 302, "Found", self.dummy_headers, newurl
            )
        self.assertIn("blocked IP", str(context.exception))


class TestIsBlockedUrl(unittest.TestCase):
    """Tests for _is_blocked_url fast-path check."""

    def test_blocks_localhost(self):
        self.assertTrue(_is_blocked_url("http://localhost/path"))

    def test_blocks_private_ip(self):
        self.assertTrue(_is_blocked_url("http://192.168.1.1/path"))
        self.assertTrue(_is_blocked_url("http://10.0.0.1/path"))

    def test_blocks_link_local(self):
        self.assertTrue(_is_blocked_url("http://169.254.169.254/path"))

    def test_allows_public_url(self):
        self.assertFalse(_is_blocked_url("http://example.com/path"))
        self.assertFalse(_is_blocked_url("https://google.com/search"))

    def test_blocks_empty_host(self):
        self.assertTrue(_is_blocked_url("http:///path"))


if __name__ == "__main__":
    unittest.main()
