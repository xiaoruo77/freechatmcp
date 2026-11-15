from fastapi.testclient import TestClient
from web_mcp.server import create_app
from web_tools.browser_upload import allowed_url
import web_mcp.server as server_module


def test_fixed_fixture_never_serves_arbitrary_local_files(isolated_settings):
    with TestClient(create_app(isolated_settings)) as client:
        page = client.get('/upload-test/fixture.html')
        assert page.status_code == 200 and 'text/html' in page.headers['content-type']
        assert 'fixtureChanges' in page.text and 'shadow-host' in page.text
        assert client.get('/upload-test/web_agent.token').status_code == 404
        assert client.get('/upload-test/fixture.html?path=08_web_data/web_agent.token').text == page.text
        assert page.headers['cache-control'] == 'no-store'


def test_https_test_origin_is_exact_and_does_not_relax_media_destinations():
    assert allowed_url('http://127.0.0.1:48272/upload-test/fixture.html')
    assert not allowed_url('https://example.test/upload-test/fixture.html')
    for url in ['http://example.test/upload-test/fixture.html',
                'https://example.test/other',
                'https://example.test/upload-test/fixture.html?q=x',
                'https://example.test.evil.invalid/upload-test/fixture.html']:
        assert not allowed_url(url)


def test_download_fixture_is_static_and_never_reads_a_caller_path(isolated_settings):
    with TestClient(create_app(isolated_settings)) as client:
        first = client.get('/browser-test/download.txt')
        query = client.get('/browser-test/download.txt?path=C:/Users/private&url=https://outside.test')
        assert first.status_code == 200 and first.content == b'XIAORUO1_DISPOSABLE_BROWSER_DOWNLOAD_CHECK\n'
        assert query.content == first.content
        assert first.headers['content-disposition'] == 'attachment; filename="xiaoruo1-browser-validation.txt"'
        assert client.get('/browser-test/web_agent.token').status_code == 404


def test_browser_redirect_is_constant_and_cannot_forward_caller_urls(isolated_settings):
    with TestClient(create_app(isolated_settings)) as client:
        for query in ["", "?next=https%3A%2F%2Fevil.invalid%2Fprivate", "?url=%2Fapi%2Fconfig&path=web_agent.token"]:
            value = client.get('/browser-test/redirect' + query, follow_redirects=False)
            assert value.status_code == 302
            assert value.headers['location'] == '/upload-test/fixture.html?reliability=redirected'
            assert value.headers['cache-control'] == 'no-store'
            assert value.headers['x-content-type-options'] == 'nosniff'
        followed = client.get('/browser-test/redirect')
        assert followed.status_code == 200 and followed.url.path == '/upload-test/fixture.html'
        assert followed.url.query == b'reliability=redirected'
        assert 'id="flow-marker"' in followed.text


def test_slow_resource_waits_for_fixed_delay_and_returns_only_static_svg(isolated_settings, monkeypatch):
    original = server_module.asyncio
    delays = []

    class AsyncioProxy:
        def __getattr__(self, name):
            return getattr(original, name)

        async def sleep(self, seconds):
            delays.append(seconds)

    monkeypatch.setattr(server_module, 'asyncio', AsyncioProxy())
    with TestClient(create_app(isolated_settings)) as client:
        first = client.get('/browser-test/slow-image.svg')
        query = client.get('/browser-test/slow-image.svg?path=08_web_data/web_agent.token&url=https://evil.invalid')
        assert first.status_code == query.status_code == 200
        assert first.content == query.content
        assert first.headers['content-type'].startswith('image/svg+xml')
        assert first.headers['cache-control'] == 'no-store'
        assert first.headers['content-security-policy'] == "default-src 'none'; sandbox"
        assert '<svg ' in first.text and '<script' not in first.text and 'web_agent' not in first.text
        assert delays == [3.5, 3.5]


def test_browser_test_routes_never_expose_other_paths_or_fixture_query_data(isolated_settings):
    with TestClient(create_app(isolated_settings)) as client:
        for path in ['/browser-test/', '/browser-test/web_agent.token', '/browser-test/fixture.html',
                     '/browser-test/other.svg', '/browser-test/redirect/extra']:
            assert client.get(path).status_code == 404
        private_marker = 'CALLER_PRIVATE_TEXT_MUST_NOT_BE_REFLECTED'
        value = client.get('/upload-test/fixture.html?reliability=' + private_marker)
        assert value.status_code == 200 and private_marker not in value.text
        for selector in ['flow-same', 'flow-blank', 'flow-redirect', 'flow-cross-origin',
                         'flow-checkbox', 'flow-radio-a', 'flow-radio-b', 'flow-select',
                         'flow-editable', 'flow-apply', 'flow-control-state', 'flow-once',
                         'flow-once-count', 'flow-delayed', 'flow-delay-state']:
            assert f'id="{selector}"' in value.text
        assert 'href="https://example.com/" target="_blank" rel="noopener"' in value.text
        assert "new Set(['initial','same','blank','redirected','slow','cooperative'])" in value.text
        assert "image.src = '/browser-test/slow-image.svg'" in value.text
        assert 'form action=' not in value.text.lower()
