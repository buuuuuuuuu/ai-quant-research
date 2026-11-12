"""Exercise the shipped proxy profiles against a local HTTP upstream."""
import gzip
import shutil
import socket
import ssl
import subprocess
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

NGINX = shutil.which('nginx')
BODY = b'/* deterministic static content */' * 200


class Upstream(BaseHTTPRequestHandler):
    def do_GET(self):
        missing = self.path.startswith('/assets/missing')
        self.send_response(404 if missing else 200)
        self.send_header('Content-Type', 'application/javascript' if self.path.startswith('/assets/') else 'application/json')
        self.send_header('Content-Length', str(len(BODY)))
        self.end_headers()
        self.wfile.write(BODY)

    def log_message(self, *_):
        pass


def port():
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', 0))
        return sock.getsockname()[1]


@pytest.mark.skipif(not NGINX, reason='nginx unavailable; installed in CI')
@pytest.mark.parametrize('profile', ['http', 'https'])
def test_proxy_cache_and_gzip(tmp_path, profile):
    upstream = ThreadingHTTPServer(('127.0.0.1', 0), Upstream)
    thread = threading.Thread(target=upstream.serve_forever, daemon=True)
    thread.start()
    web_port, redirect_port = port(), port()
    text = (Path(__file__).resolve().parents[1] / f'deploy/nginx/ai-quant-{profile}.conf').read_text()
    text = text.replace('http://127.0.0.1:8080', f'http://127.0.0.1:{upstream.server_port}')
    text = text.replace('    listen [::]:80;\n', '').replace('    listen [::]:443 ssl http2;\n', '')
    text = text.replace('listen 80;', f'listen 127.0.0.1:{web_port if profile == "http" else redirect_port};')
    text = text.replace('listen 443 ssl http2;', f'listen 127.0.0.1:{web_port} ssl;')
    if profile == 'https':
        subprocess.run(['openssl', 'req', '-x509', '-newkey', 'rsa:2048', '-nodes', '-days', '1',
                        '-subj', '/CN=localhost', '-keyout', str(tmp_path / 'key.pem'),
                        '-out', str(tmp_path / 'cert.pem')], check=True, capture_output=True)
        text = text.replace('/etc/letsencrypt/live/research.example.com/fullchain.pem', str(tmp_path / 'cert.pem'))
        text = text.replace('/etc/letsencrypt/live/research.example.com/privkey.pem', str(tmp_path / 'key.pem'))
    config = tmp_path / 'nginx.conf'
    config.write_text(f'daemon off;\npid {tmp_path}/nginx.pid;\nerror_log stderr;\nevents {{}}\nhttp {{ access_log off;\n{text}\n}}')
    process = None
    try:
        subprocess.run([NGINX, '-t', '-p', str(tmp_path), '-c', str(config)], check=True, capture_output=True)
        process = subprocess.Popen([NGINX, '-p', str(tmp_path), '-c', str(config)], stderr=subprocess.PIPE)
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}),
            urllib.request.HTTPSHandler(context=ssl._create_unverified_context()))
        url = f'{profile}://127.0.0.1:{web_port}'
        for _ in range(100):
            try:
                with opener.open(url + '/health', timeout=1):
                    break
            except urllib.error.URLError:
                if process.poll() is not None:
                    pytest.fail(process.stderr.read().decode())
                time.sleep(.05)
        else:
            pytest.fail('nginx did not start')
        for query, expected in [('', 'no-cache'), ('?v=0.2.0', 'no-cache'),
                                ('?v=invalid', 'no-cache'), ('?v=abcdef0123456789', 'max-age=31536000')]:
            request = urllib.request.Request(url + '/assets/app.js' + query, headers={'Accept-Encoding': 'gzip'})
            with opener.open(request) as response:
                assert response.headers['Cache-Control'] == expected
                assert response.headers['Content-Encoding'] == 'gzip'
                assert 'Accept-Encoding' in response.headers['Vary']
                assert response.headers['X-Content-Type-Options'] == 'nosniff'
                assert gzip.decompress(response.read()) == BODY
        with opener.open(url + '/api/example') as response:
            assert 'max-age=31536000' not in response.headers.get('Cache-Control', '')
        with pytest.raises(urllib.error.HTTPError) as failed:
            opener.open(url + '/assets/missing?v=abcdef0123456789')
        assert failed.value.code == 404
        assert 'max-age=31536000' not in failed.value.headers.get('Cache-Control', '')
    finally:
        if process is not None:
            process.terminate()
            process.communicate(timeout=5)
        upstream.shutdown()
        upstream.server_close()
        thread.join(timeout=5)
