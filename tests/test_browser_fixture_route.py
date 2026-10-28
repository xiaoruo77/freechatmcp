from fastapi.testclient import TestClient
from web_mcp.server import create_app
from web_tools.browser_upload import allowed_url


def test_fixed_fixture_never_serves_arbitrary_local_files(isolated_settings):
    with TestClient(create_app(isolated_settings)) as client:
        page = client.get('/upload-test/fixture.html')
        assert page.status_code == 200 and 'text/html' in page.headers['content-type']
        assert 'fixtureChanges' in page.text and 'shadow-host' in page.text
        assert client.get('/upload-test/web_agent.token').status_code == 404
        assert client.get('/upload-test/fixture.html?path=08_web_data/web_agent.token').text == page.text
        assert page.headers['cache-control'] == 'no-store'


def test_https_test_origin_is_exact_and_does_not_relax_media_destinations():
    assert allowed_url('https://mcp.example.com/upload-test/fixture.html')
    for url in ['http://mcp.example.com/upload-test/fixture.html',
                'https://mcp.example.com/other',
                'https://mcp.example.com/upload-test/fixture.html?q=x',
                'https://mcp.example.com.evil.invalid/upload-test/fixture.html']:
        assert not allowed_url(url)