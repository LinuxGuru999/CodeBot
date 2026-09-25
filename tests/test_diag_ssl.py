import socket, ssl, threading, tempfile, os
import http.server

def test_diag_ssl_webfetch_first():
    from cryptography import x509
    from cryptography.x509.oid import NameOID
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    import datetime, ipaddress
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    subject = issuer = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "localhost")])
    cert = (x509.CertificateBuilder().subject_name(subject).issuer_name(issuer).public_key(key.public_key()).serial_number(x509.random_serial_number()).not_valid_before(datetime.datetime.now(datetime.timezone.utc)).not_valid_after(datetime.datetime.now(datetime.timezone.utc)+datetime.timedelta(days=1)).add_extension(x509.SubjectAlternativeName([x509.DNSName("localhost"), x509.IPAddress(ipaddress.IPv4Address("127.0.0.1"))]), critical=False).sign(key, hashes.SHA256()))
    cert_pem = cert.public_bytes(serialization.Encoding.PEM)
    key_pem = key.private_bytes(encoding=serialization.Encoding.PEM, format=serialization.PrivateFormat.TraditionalOpenSSL, encryption_algorithm=serialization.NoEncryption())
    cf, cp = tempfile.mkstemp(suffix=".pem")
    kf, kp = tempfile.mkstemp(suffix=".pem")
    try:
        os.write(cf, cert_pem); os.close(cf)
        os.write(kf, key_pem); os.close(kf)
        server_ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        server_ctx.load_cert_chain(cp, kp)
        body = b"Valid cert response content"
        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                print(f"HANDLER got request path={self.path}")
                self.send_response(200)
                self.send_header("Content-Type", "text/plain")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            def log_message(self, *a, **k):
                pass
        srv = http.server.HTTPServer(("127.0.0.1", 0), Handler)
        srv.socket = server_ctx.wrap_socket(srv.socket, server_side=True)
        port = srv.server_address[1]
        print(f"PORT={port} sock={srv.socket} fileno={srv.socket.fileno()}")
        def serve():
            for i in range(5):
                print(f"SERVER waiting handle_request #{i}")
                try:
                    srv.handle_request()
                    print(f"SERVER handle_request #{i} returned")
                except Exception as e:
                    print(f"SERVER handle_request #{i} exception: {e!r}")
                    break
            print("SERVER thread exiting")
        t = threading.Thread(target=serve, daemon=True)
        t.start()
        import time; time.sleep(0.3)
        print(f"THREAD alive={t.is_alive()} listener fileno={srv.socket.fileno()}")
        # web_fetch FIRST, before any other connection
        from unittest.mock import patch
        from codebot.web_tools import web_fetch
        url = f"https://127.0.0.1:{port}/"
        print(f"FETCH url={url}")
        trusted = ssl.create_default_context()
        trusted.check_hostname = False
        trusted.verify_mode = ssl.CERT_REQUIRED
        trusted.load_verify_locations(cp)
        with patch("codebot.web_tools._SECURE_SSL_CONTEXT", trusted):
            with patch("codebot.web_tools.is_blocked_url", return_value=False):
                with patch("codebot.web_tools._resolve_and_validate_host", return_value=("127.0.0.1", port, socket.AF_INET)):
                    result = web_fetch(url, max_bytes=4096)
        print(f"RESULT={result}")
        # also check what _PinnedHTTPSConnection sees
        import urllib.request
        from codebot import web_tools
        print(f"patched global is trusted: {web_tools._SECURE_SSL_CONTEXT is trusted}")
        try:
            srv.server_close()
        except Exception:
            pass
        t.join(timeout=3)
        print("DONE")
        assert True
    finally:
        for p in (cp, kp):
            try: os.unlink(p)
            except OSError: pass
