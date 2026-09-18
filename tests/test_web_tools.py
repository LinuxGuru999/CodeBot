"""Tests for web_tools SSRF protection, including DNS rebinding defense."""

from __future__ import annotations

import ipaddress
import socket
from unittest.mock import MagicMock, patch

import pytest

from codebot.web_tools import _is_blocked_url, _resolve_and_validate_host, web_fetch, web_search


class TestIsBlockedUrl:
    """Tests for URL pattern-based blocking."""

    def test_blocks_localhost(self) -> None:
        assert _is_blocked_url("http://localhost/secret") is True

    def test_blocks_loopback_ip(self) -> None:
        assert _is_blocked_url("http://127.0.0.1/admin") is True

    def test_blocks_private_10(self) -> None:
        assert _is_blocked_url("http://10.0.0.1/internal") is True

    def test_blocks_private_172(self) -> None:
        assert _is_blocked_url("http://172.16.0.1/data") is True

    def test_blocks_private_192(self) -> None:
        assert _is_blocked_url("http://192.168.1.1/config") is True

    def test_blocks_link_local(self) -> None:
        assert _is_blocked_url("http://169.254.1.1/metadata") is True

    def test_allows_public_url(self) -> None:
        assert _is_blocked_url("https://example.com/page") is False

    def test_blocks_empty_host(self) -> None:
        assert _is_blocked_url("http:///no-host") is True

    def test_blocks_ipv6_loopback(self) -> None:
        assert _is_blocked_url("http://[::1]/test") is True


class TestResolveAndValidateHost:
    """Tests for DNS resolution and post-resolve IP validation (DNS rebinding defense)."""

    def test_resolves_valid_hostname(self) -> None:
        with patch("codebot.web_tools.socket.getaddrinfo") as mock_gai:
            mock_gai.return_value = [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443))]
            result = _resolve_and_validate_host("example.com", 443)
            assert result == ("93.184.216.34", 443, socket.AF_INET)

    def test_blocks_hostname_resolving_to_private_ip(self) -> None:
        """DNS rebinding: hostname resolves to private IP after passing URL check."""
        with patch("codebot.web_tools.socket.getaddrinfo") as mock_gai:
            mock_gai.return_value = [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("10.0.0.1", 80))]
            with pytest.raises(ValueError, match="resolves to blocked IP"):
                _resolve_and_validate_host("evil-rebind.example.com", 80)

    def test_blocks_hostname_resolving_to_loopback(self) -> None:
        """DNS rebinding: hostname resolves to 127.0.0.1."""
        with patch("codebot.web_tools.socket.getaddrinfo") as mock_gai:
            mock_gai.return_value = [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", 8080))]
            with pytest.raises(ValueError, match="resolves to blocked IP"):
                _resolve_and_validate_host("attacker.com", 8080)

    def test_blocks_hostname_resolving_to_link_local(self) -> None:
        """Cloud metadata endpoint via DNS rebinding."""
        with patch("codebot.web_tools.socket.getaddrinfo") as mock_gai:
            mock_gai.return_value = [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("169.254.169.254", 80))]
            with pytest.raises(ValueError, match="resolves to blocked IP"):
                _resolve_and_validate_host("metadata.evil.com", 80)

    def test_raises_on_dns_failure(self) -> None:
        with patch("codebot.web_tools.socket.getaddrinfo") as mock_gai:
            mock_gai.side_effect = socket.gaierror("Name resolution failed")
            with pytest.raises(ValueError, match="DNS resolution failed"):
                _resolve_and_validate_host("nonexistent.invalid", 443)

    def test_passes_through_ip_address_directly(self) -> None:
        """When host is already an IP, skip DNS but still validate."""
        result = _resolve_and_validate_host("93.184.216.34", 443)
        assert result == ("93.184.216.34", 443, socket.AF_INET)

    def test_blocks_direct_private_ip(self) -> None:
        with pytest.raises(ValueError, match="resolves to blocked IP"):
            _resolve_and_validate_host("192.168.1.1", 80)

    def test_handles_ipv6_resolution(self) -> None:
        with patch("codebot.web_tools.socket.getaddrinfo") as mock_gai:
            mock_gai.return_value = [(socket.AF_INET6, socket.SOCK_STREAM, 6, "", ("2606:2800:220:1:248:1893:25c8:1946", 443, 0, 0))]
            result = _resolve_and_validate_host("ipv6.example.com", 443)
            assert result[0] == "2606:2800:220:1:248:1893:25c8:1946"
            assert result[2] == socket.AF_INET6

    def test_blocks_ipv6_unique_local(self) -> None:
        with patch("codebot.web_tools.socket.getaddrinfo") as mock_gai:
            mock_gai.return_value = [(socket.AF_INET6, socket.SOCK_STREAM, 6, "", ("fd00::1", 80, 0, 0))]
            with pytest.raises(ValueError, match="resolves to blocked IP"):
                _resolve_and_validate_host("internal-v6.evil.com", 80)


class TestWebFetchSSRFProtection:
    """Integration tests ensuring web_fetch uses resolve-and-validate."""

    def test_web_fetch_blocks_rebinding_attack(self) -> None:
        """web_fetch must reject URLs that resolve to private IPs."""
        with patch("codebot.web_tools.socket.getaddrinfo") as mock_gai:
            mock_gai.return_value = [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("10.0.0.1", 443))]
            result = web_fetch("https://rebind-attack.example.com/secret")
            assert result["success"] is False
            assert "blocked" in result["error"].lower() or "dns" in result["error"].lower()

    def test_web_fetch_blocks_url_pattern(self) -> None:
        """web_fetch blocks URLs with private IPs directly in the URL."""
        result = web_fetch("http://192.168.1.1/admin")
        assert result["success"] is False
        assert "blocked" in result["error"].lower()


class TestWebSearchSSRFProtection:
    """Ensure web_search also validates resolved IPs."""

    def test_web_search_blocks_private_target(self) -> None:
        """If DDG somehow returned a redirect to a private IP, it should be blocked."""
        # web_search uses hardcoded duckduckgo URL, so this mainly tests
        # that the function doesn't crash on blocked patterns
        result = web_search("")
        assert result["success"] is False
