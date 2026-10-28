from copy import deepcopy
import base64
import json
from pathlib import Path

import pytest

from web_tools.xiaoruo_browser_adapter import NativeXiaoruoBrowserAdapter, NativeAdapterError


IDENTIFIER = 'native_12345678abcdef'
GENERATION = '12345:process-created:instance'
CAPS = {'tabs': True, 'navigation': True, 'readPage': True, 'click': True,
        'fill': True, 'press': True, 'selectOption': True, 'scroll': True,
        'evaluate': True,
        'screenshot': True, 'screenshotWindow': True,
        'cef': {'readPage': True, 'click': True, 'fill': True, 'press': True,
                'scroll': True, 'screenshot': 'rendered-page-png'},
        'lightpanda': {'readPage': True, 'click': True, 'fill': True, 'press': True,
                      'scroll': True, 'screenshot': 'native-reader-only'}}
PNG = b'\x89PNG\r\n\x1a\n' + b'bounded-test-png'


class Instance:
    instance_id = '12345678abcdef'
    pid = 12345
    permissions = ('browser.script',)

    def public_descriptor(self):
        return {'browser_id': IDENTIFIER, 'instance_id': self.instance_id,
                'connection_generation': GENERATION, 'process_id': self.pid}


class Discovery:
    def __init__(self):
        self.live = True
        self.instance = Instance()

    def discover(self):
        return [self.instance] if self.live else []


class Client:
    def __init__(self):
        self.calls = []
        self.cap_calls = 0
        self.caps = deepcopy(CAPS)
        self.items = [{'id': 7, 'url': 'https://example.test/old', 'pendingUrl': '',
                       'loading': False, 'documentReady': True, 'active': True,
                       'engine': 'CEF / Chromium'}]
        self.reply = None
        # These are native evaluate task results, not an implementation of the
        # JavaScript helpers. Tests can vary observed evidence independently of
        # task acceptance and can inspect the helper's JSON-only arguments.
        self.helper_calls = []
        self.helper_results = {
            'click': {'helper': 'xiaoruo1-cef-semantic-click/1',
                      'click_dispatched': True, 'verified': True,
                      'before': {'body_hash': 101}, 'after': {'body_hash': 202}},
            'observe_click': {'helper': 'xiaoruo1-cef-click-observation/1',
                              'verified': False, 'after': {'body_hash': 101}},
            'scroll': {'helper': 'xiaoruo1-cef-scroll/1', 'verified': True,
                       'requested': {'x': 5, 'y': 400},
                       'expected': {'x': 5, 'y': 400},
                       'maximum': {'x': 800, 'y': 1200},
                       'before': {'x': 0, 'y': 0}, 'after': {'x': 5, 'y': 400},
                       'scrolled': True, 'at_boundary': False},
        }

    async def capabilities(self, timeout=30):
        self.cap_calls += 1
        return deepcopy(self.caps)

    def helper_result(self, script):
        prefix = '(()=>{const args='
        assert script.startswith(prefix), 'Only a fixed native helper may run'
        arguments, end = json.JSONDecoder().raw_decode(script[len(prefix):])
        assert script[len(prefix) + end:].startswith(';\n')
        kinds = {'xiaoruo1-cef-semantic-click/1': 'click',
                 'xiaoruo1-cef-click-observation/1': 'observe_click',
                 'xiaoruo1-cef-scroll/1': 'scroll'}
        kind = next(kind for marker, kind in kinds.items() if marker in script)
        self.helper_calls.append((kind, arguments))
        return deepcopy(self.helper_results[kind])

    async def tabs(self, timeout=30):
        return {'tabs': deepcopy(self.items), 'activeTabId': 7}

    async def call_tool(self, name, arguments=None, timeout=30):
        self.calls.append((name, arguments))
        return {'structured': {'task': {'id': arguments['task_id'], 'state': 'running'}}}

    async def execute(self, action, args, timeout=30):
        self.calls.append((action, deepcopy(args)))
        if self.reply is not None:
            return deepcopy(self.reply)
        result = {'text': 'observed page'}
        if action == 'evaluate':
            result = self.helper_result(args['script'])
        if action == 'new_tab':
            self.items.append({'id': 9, 'url': args['url'], 'pendingUrl': '',
                               'loading': False, 'documentReady': True, 'active': False,
                               'engine': 'Lightpanda' if args.get('engine') == 'lightpanda' else 'CEF / Chromium'})
            result['tab_id'] = 9
        if action == 'close_tab':
            self.items = [item for item in self.items if item['id'] != args['tab_id']]
        if action == 'navigate':
            self.items[0].update(url=args['url'], pendingUrl='', loading=False, documentReady=True)
        if action == 'select_tab':
            self.items[0]['active'] = True
        task = {'id': 'xr-12345-1', 'state': 'succeeded', 'result': json.dumps(result)}
        images = [{'mimeType': 'image/png', 'data': base64.b64encode(PNG).decode()}] if action.startswith('screenshot') else []
        return {'ok': True, 'task_id': task['id'], 'native_task': task, 'images': images}


@pytest.fixture
def adapter(tmp_path):
    discovery, client = Discovery(), Client()
    value = NativeXiaoruoBrowserAdapter(tmp_path, discovery, lambda instance: client)
    return value, discovery, client


async def call(adapter, method, payload=None):
    return await adapter.native_dispatch(IDENTIFIER, GENERATION, method, payload or {'tab_id': '7'}, 5)


async def test_discovery_is_native_and_caches_safe_capabilities(adapter):
    value, _, client = adapter
    first = await value.native_instances()
    second = await value.native_instances()
    entry = first['browsers'][0]
    assert entry['native_pid'] == 12345 and entry['adapter'] == 'native_ipc'
    assert entry['browser_id'] == IDENTIFIER and 'open_tab' in entry['supported_actions']
    assert client.cap_calls == 1 and first == second
    assert 'authToken' not in json.dumps(first) and 'pipeName' not in json.dumps(first)


async def test_generation_or_disappeared_process_never_falls_back(adapter):
    value, discovery, client = adapter
    with pytest.raises(NativeAdapterError):
        await value.native_dispatch(IDENTIFIER, 'stale-generation', 'read_page', {'tab_id': '7'}, 5)
    discovery.live = False
    with pytest.raises(NativeAdapterError):
        await call(value, 'read_page')
    assert client.calls == []


async def test_pending_url_is_preserved_with_exact_native_tab_id(adapter):
    value, _, client = adapter
    client.items[0].update(url='', pendingUrl='https://example.test/pending', loading=True, documentReady=False)
    result = await call(value, 'list_tabs', {})
    tab = result['tabs'][0]
    assert tab['tab_id'] == '7' and tab['url'] == 'https://example.test/pending'
    assert tab['committed_url'] == '' and tab['loading'] and not tab['document_ready']


async def test_new_tab_uses_native_runtime_enum_and_checks_same_id_readiness(adapter):
    value, _, client = adapter
    result = await call(value, 'open_tab', {'url': 'https://example.test/new', 'engine': 'chromium'})
    assert result['verified'] and result['tab_id'] == '9' and result['document_ready']
    assert client.calls == [('new_tab', {'url': 'https://example.test/new', 'engine': 'auto'})]
    assert client.items[0]['id'] == 7


@pytest.mark.parametrize('mutation', ['different_id', 'not_ready'])
async def test_new_tab_never_claims_an_unready_or_replacement_tab(adapter, mutation):
    value, _, client = adapter
    client.reply = {'ok': True, 'task_id': 'xr-12345-1',
                    'native_task': {'id': 'xr-12345-1', 'state': 'succeeded', 'result': {'tab_id': 9}}}
    client.items.append({'id': 10 if mutation == 'different_id' else 9,
                         'url': '', 'pendingUrl': 'https://example.test/new',
                         'loading': True, 'documentReady': False})
    result = await call(value, 'open_tab', {'url': 'https://example.test/new'})
    assert not result['verified'] and result['action_may_have_run']
    assert result['error']['code'] == 'native_tab_state_unverified'
    assert len(client.calls) == 1


async def test_type_uses_native_value_and_observed_css(adapter):
    value, _, client = adapter
    result = await call(value, 'type_text', {'tab_id': '7', 'by': 'css', 'selector': '#owned', 'text': 'hello', 'clear': True})
    assert result['verified'] and result['native_terminal']
    assert client.calls == [('fill', {'tab_id': 7, 'selector': '#owned', 'value': 'hello'})]


@pytest.mark.parametrize('extra', [{'clear': False}, {'by': 'role'}, {'frame_id': 12}, {'shadow_path': ['#host']}])
async def test_extension_only_arguments_are_not_silently_ignored(adapter, extra):
    value, _, client = adapter
    result = await call(value, 'type_text', {'tab_id': '7', 'selector': '#owned', 'text': 'hello', **extra})
    assert not result['ok'] and result['error']['code'] == 'unsupported'
    assert not client.calls


@pytest.mark.parametrize('state,code,terminal', [('accepted', '', False), ('running', '', False),
    ('failed', 'unsupported', True), ('timed_out', 'timeout', True), ('cancelled', '', True)])
async def test_only_succeeded_task_can_verify_action(adapter, state, code, terminal):
    value, _, client = adapter
    client.reply = {'ok': False, 'task_id': 'xr-12345-2',
                    'native_task': {'id': 'xr-12345-2', 'state': state, 'errorCode': code}}
    result = await call(value, 'click', {'tab_id': '7', 'selector': '#owned'})
    assert not result['ok'] and result['native_terminal'] is terminal
    assert result['native_state'] == state and result['native_task_id'] == 'xr-12345-2'
    assert not result['retryable'] and len(client.calls) == 1
    assert client.calls[0][0] == 'evaluate'


async def test_transport_unknown_retains_task_id_without_retry(adapter):
    value, _, client = adapter
    client.reply = {'ok': False, 'status': 'unknown', 'task_id': 'xr-12345-3',
                    'error': {'code': 'native_ipc_timeout', 'message': 'Static timeout'}}
    result = await call(value, 'click', {'tab_id': '7', 'selector': '#owned'})
    assert result['native_task_id'] == 'xr-12345-3' and result['action_may_have_run']
    assert not result['native_terminal'] and len(client.calls) == 1
    assert client.calls[0][0] == 'evaluate'


async def test_task_status_is_process_owned_and_separates_query_from_task_success(adapter):
    value, _, client = adapter
    denied = await call(value, 'task_status', {'task_id': 'xr-999-1'})
    assert not denied['ok'] and not client.calls
    status = await call(value, 'task_status', {'task_id': 'xr-12345-1'})
    assert status['verified'] and status['native_state'] == 'running'
    assert not status['native_terminal'] and not status['native_task_succeeded']


async def test_native_page_png_is_saved_privately_not_returned_as_base64(adapter):
    value, _, client = adapter
    result = await call(value, 'screenshot', {'tab_id': '7', 'mode': 'page'})
    assert result['verified'] and Path(result['path']).read_bytes() == PNG
    assert Path(result['path']).parent == value.data_root / 'browser_screenshots'
    assert 'data' not in result and 'data_url' not in result


async def test_lightpanda_page_pixels_unsupported_but_explicit_active_window_is_distinct(adapter):
    value, _, client = adapter
    client.items[0]['engine'] = 'Lightpanda'
    page = await call(value, 'screenshot', {'tab_id': '7', 'mode': 'page'})
    assert page['error']['code'] == 'unsupported' and not client.calls
    window = await call(value, 'screenshot', {'tab_id': '7', 'mode': 'window'})
    assert window['pixel_scope'] == 'native_window' and window['verified']
    assert client.calls[0][0] == 'screenshot_window'


async def test_window_capture_never_silently_selects_an_inactive_tab(adapter):
    value, _, client = adapter
    client.items[0]['active'] = False
    result = await call(value, 'screenshot', {'tab_id': '7', 'mode': 'window'})
    assert result['error']['code'] == 'native_window_target_inactive' and not client.calls


async def test_cef_scroll_uses_fixed_helper_and_verifies_observed_absolute_coordinates(adapter):
    value, _, client = adapter
    result = await call(value, 'scroll', {'tab_id': '7', 'direction': 'down', 'pixels': 400, 'x': 5})
    assert result['verified'] and result['native_terminal']
    assert result['after'] == result['expected'] == {'x': 5, 'y': 400}
    assert result['before'] == {'x': 0, 'y': 0} and result['scrolled']
    assert client.helper_calls == [('scroll', {'x': 5, 'y': 400})]
    assert len(client.calls) == 1 and client.calls[0][0] == 'evaluate'
    assert client.calls[0][1]['tab_id'] == 7


async def test_lightpanda_scroll_still_uses_native_absolute_coordinates(adapter):
    value, _, client = adapter
    client.items[0]['engine'] = 'Lightpanda'
    result = await call(value, 'scroll', {'tab_id': '7', 'direction': 'down', 'pixels': 400, 'x': 5})
    assert result['verified'] and client.calls == [('scroll', {'tab_id': 7, 'x': 5, 'y': 400})]
    assert not client.helper_calls


async def test_cef_click_without_observed_effect_is_unverified_and_dispatched_once(adapter):
    value, _, client = adapter
    client.helper_results['click'].update(verified=False, after={'body_hash': 101})
    result = await call(value, 'click', {'tab_id': '7', 'selector': '#owned'})
    assert not result['verified'] and result['native_verification_required']
    assert result['native_terminal'] and result['native_state'] == 'succeeded'
    assert result['native_task_id'] == 'xr-12345-1' and result['action_may_have_run']
    assert result['before'] == result['after']
    assert [kind for kind, _ in client.helper_calls].count('click') == 1
    observations = [arguments for kind, arguments in client.helper_calls if kind == 'observe_click']
    assert observations and all(arguments == {'selector': '#owned', 'before': {'body_hash': 101}}
                                for arguments in observations)
    assert len(result['native_observation_task_ids']) == len(observations)
    assert all(action == 'evaluate' for action, _ in client.calls)


@pytest.mark.parametrize('selector', [
    'button[data-label="a\\\"b\'c"]',
    '#owned");globalThis.callerScriptRan=true;//',
])
async def test_cef_click_embeds_selector_only_as_escaped_json_and_ignores_caller_script(adapter, selector):
    value, _, client = adapter
    caller_script = 'globalThis.arbitraryCallerScriptRan = true'
    result = await call(value, 'click', {'tab_id': '7', 'selector': selector, 'script': caller_script})
    assert result['verified'] and client.helper_calls == [('click', {'selector': selector})]
    assert len(client.calls) == 1
    action, arguments = client.calls[0]
    assert action == 'evaluate' and set(arguments) == {'tab_id', 'script'}
    assert caller_script not in arguments['script']
    assert json.dumps({'selector': selector}, ensure_ascii=True) in arguments['script']


async def test_arbitrary_evaluate_method_is_not_exposed(adapter):
    value, _, client = adapter
    result = await call(value, 'evaluate', {'tab_id': '7', 'script': 'globalThis.callerScriptRan = true'})
    assert result['error']['code'] == 'unsupported' and not client.calls


async def test_cef_scroll_mismatch_does_not_verify_succeeded_task(adapter):
    value, _, client = adapter
    client.helper_results['scroll'].update(verified=False, after={'x': 0, 'y': 0}, scrolled=False)
    result = await call(value, 'scroll', {'tab_id': '7', 'x': 5, 'y': 400})
    assert not result['verified'] and result['native_verification_required']
    assert result['native_terminal'] and result['native_state'] == 'succeeded'
    assert result['action_may_have_run'] and result['after'] != result['expected']
    assert client.helper_calls == [('scroll', {'x': 5, 'y': 400})]
    assert len(client.calls) == 1 and client.calls[0][0] == 'evaluate'


@pytest.mark.parametrize('native_result,verified', [
    ({'helper': 'xiaoruo1-cef-semantic-click/1', 'verified': True}, True),
    (json.dumps({'helper': 'xiaoruo1-cef-semantic-click/1', 'verified': True}), True),
    ('not-json', False),
    (json.dumps(['unexpected', 'array']), False),
    ({'helper': 'unrelated-script', 'verified': True}, False),
    ({'helper': 'xiaoruo1-cef-semantic-click/1', 'verified': 'true'}, False),
    ({'helper': 'xiaoruo1-cef-semantic-click/1'}, False),
])
async def test_cef_click_verification_requires_recognized_native_result(native_result, verified, adapter):
    value, _, client = adapter
    client.reply = {'ok': True, 'task_id': 'xr-12345-4',
                    'native_task': {'id': 'xr-12345-4', 'state': 'succeeded', 'result': native_result}}
    result = await call(value, 'click', {'tab_id': '7', 'selector': '#owned'})
    assert result['verified'] is verified
    assert result['native_verification_required'] is not verified
    assert result['native_task_id'] == 'xr-12345-4' and result['native_terminal']
    assert len(client.calls) == 1 and client.calls[0][0] == 'evaluate'


@pytest.mark.parametrize('coordinates', [{'x': -1}, {'y': 100001}, {'x': True}, {'y': 2.5}])
async def test_cef_scroll_rejects_unbounded_or_noninteger_coordinates_before_dispatch(adapter, coordinates):
    value, _, client = adapter
    result = await call(value, 'scroll', {'tab_id': '7', 'x': 0, 'y': 0, **coordinates})
    assert result['error']['code'] == 'invalid_argument' and not client.calls


@pytest.mark.parametrize('method,payload', [('click', {'selector': '#owned'}), ('scroll', {'y': 400})])
@pytest.mark.parametrize('missing', ['permission', 'evaluate'])
async def test_cef_semantic_operation_requires_script_permission_and_capability_without_fallback(adapter, method, payload, missing):
    value, discovery, client = adapter
    if missing == 'permission':
        discovery.instance.permissions = ()
    else:
        client.caps['evaluate'] = False
    result = await call(value, method, {'tab_id': '7', **payload})
    assert result['error']['code'] == 'unsupported' and not result['verified']
    assert not client.calls and not client.helper_calls


async def test_lightpanda_missing_operation_does_not_inherit_cef_only_global_flag(adapter):
    value, _, client = adapter
    client.items[0]['engine'] = 'Lightpanda'
    result = await call(value, 'select_option', {'tab_id': '7', 'selector': '#choice', 'option': 'one'})
    assert result['error']['code'] == 'unsupported' and not client.calls


async def test_wait_performs_native_state_read_instead_of_claiming_sleep_success(adapter):
    value, _, client = adapter
    result = await call(value, 'wait', {'tab_id': '7', 'milliseconds': 0})
    assert result['verified'] and client.calls[0][0] == 'read_page'


@pytest.mark.parametrize('engine', ['unknown-engine', 'miniblink', 'webview2'])
async def test_unknown_or_other_engine_is_not_silently_replaced(adapter, engine):
    value, _, client = adapter
    result = await call(value, 'open_tab', {'url': 'https://example.test/', 'engine': engine})
    assert result['error']['code'] == 'unsupported' and not client.calls


async def test_existing_tab_engine_never_changes_without_separate_authorization(adapter):
    value, _, client = adapter
    result = await call(value, 'navigate', {'tab_id': '7', 'url': 'https://example.test/', 'engine': 'lightpanda'})
    assert result['error']['code'] == 'native_engine_change_denied' and not client.calls


async def test_single_client_busy_still_discovers_instance_and_never_uses_extension(adapter):
    value, _, client = adapter

    class Busy(Exception):
        code = 'native_ipc_busy'

    async def busy(timeout=30):
        raise Busy('No sensitive details')

    client.capabilities = busy
    result = await value.native_instances()
    instance = result['browsers'][0]
    assert instance['busy'] is True and instance['native_pid'] == 12345
    assert instance['supported_actions'] == ['status', 'list_tabs', 'task_status']
    assert not client.calls