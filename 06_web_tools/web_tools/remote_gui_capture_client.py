"""Loopback-only controller for one-use remote screenshot ingress tickets."""
from __future__ import annotations

import json
from pathlib import Path
import re
import time
from urllib.parse import urlsplit
from urllib.request import Request, ProxyHandler, build_opener


class RemoteCaptureClient:
    def __init__(self, data_root: Path, *, opener=None, timeout=20):
        self.data_root = Path(data_root).resolve()
        config = json.loads((self.data_root.parent / '07_web_config' / 'background_desktop.json').read_text(encoding='utf-8-sig'))
        self.public_origin = config.get('capture_public_origin', '')
        parsed = urlsplit(self.public_origin)
        if (parsed.scheme != 'https' or not parsed.hostname or parsed.username or parsed.password
                or parsed.path or parsed.query or parsed.fragment):
            raise ValueError('A trusted HTTPS screenshot origin must be configured')
        self._token = Path(config['token_file']).read_text(encoding='utf8').strip()
        if len(self._token) < 32:
            raise ValueError('The local screenshot bridge token is unavailable')
        self._opener = opener or build_opener(ProxyHandler({}))
        self._timeout = max(1, min(25, timeout))
        self._base = 'http://127.0.0.1:48271/internal/remote-gui/captures'

    @staticmethod
    def _id(capture_id):
        if not isinstance(capture_id, str) or not re.fullmatch(r'[a-f0-9]{32}', capture_id):
            raise ValueError('Invalid screenshot ticket identity')
        return capture_id

    def _request(self, method, path=''):
        request = Request(self._base + path, method=method,
            headers={'Authorization': 'Bearer ' + self._token},
            data=b'' if method == 'POST' else None)
        with self._opener.open(request, timeout=5) as response:
            raw = response.read(65537)
        if len(raw) > 65536:
            raise ValueError('Screenshot ticket response exceeded its bound')
        return json.loads(raw) if raw else {}

    def issue(self):
        ticket = self._request('POST')
        capture_id = self._id(ticket.get('capture_id'))
        if (ticket.get('upload_url') != self.public_origin + '/remote-gui/captures/' + capture_id
                or not isinstance(ticket.get('token'), str) or not 32 <= len(ticket['token']) <= 256):
            raise ValueError('Screenshot ticket did not match the trusted origin')
        return ticket

    def wait(self, capture_id):
        capture_id = self._id(capture_id)
        deadline = time.monotonic() + self._timeout
        while True:
            result = self._request('GET', '/' + capture_id)
            if result.get('status') == 'completed':
                capture = result.get('capture')
                mime_type = capture.get('mime_type') if isinstance(capture, dict) else None
                extension = {'image/png': '.png', 'image/jpeg': '.jpg'}.get(mime_type) if isinstance(mime_type, str) else None
                if (extension is None or not isinstance(capture.get('path'), str)
                        or not capture['path']):
                    raise ValueError('Screenshot was not saved in its exact capture path')
                path = Path(capture['path']).resolve()
                root = (self.data_root / 'background_desktop' / 'captures').resolve()
                if (path.parent != root or path.name != 'gui-' + capture_id + extension
                        or not path.is_file()):
                    raise ValueError('Screenshot was not saved in its exact capture path')
                return capture
            if result.get('status') == 'failed':
                raise RuntimeError('Remote screenshot upload failed')
            if result.get('status') not in {'pending', 'uploading'}:
                raise RuntimeError('Remote screenshot ticket is unavailable')
            if time.monotonic() >= deadline:
                raise TimeoutError('Remote screenshot upload acknowledgement was not received')
            time.sleep(.1)

    def cancel(self, capture_id):
        return self._request('DELETE', '/' + self._id(capture_id))
