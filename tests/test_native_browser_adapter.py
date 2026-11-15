from copy import deepcopy
import base64
import json
from pathlib import Path
import shutil
import subprocess

import pytest

from web_tools.xiaoruo_browser_adapter import NativeXiaoruoBrowserAdapter, NativeAdapterError, _cef_helper_script


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
            'read_dom': {'helper': 'xiaoruo1-cef-dom-observation/1', 'url': 'https://example.test/old',
                         'text': 'observed page', 'file_inputs': [], 'elements': [], 'images': [],
                         'observation_complete': True},
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
                 'xiaoruo1-cef-scroll/1': 'scroll',
                 'xiaoruo1-cef-dom-observation/1': 'read_dom'}
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


@pytest.mark.parametrize('action,mode', [('screenshot', 'page'), ('screenshot_window', 'window')])
async def test_status_recovers_original_capture_without_another_dispatch(adapter, action, mode):
    value, _, client = adapter

    async def status(name, arguments, timeout):
        client.calls.append((name, arguments))
        return {'structured': {'task': {'id': 'xr-12345-4', 'action': action, 'state': 'succeeded'}},
                'images': [{'mimeType': 'image/png', 'data': base64.b64encode(PNG).decode()}]}

    client.call_tool = status
    result = await call(value, 'task_status', {'task_id': 'xr-12345-4'})
    assert result['verified'] and result['native_task_succeeded']
    assert result['recovered_from_task_status'] and result['screenshot_mode'] == mode
    assert Path(result['path']).read_bytes() == PNG
    assert client.calls == [('xiaoruo_browser_task_status', {'task_id': 'xr-12345-4'})]
    assert 'data' not in result and 'images' not in result


@pytest.mark.parametrize('changes,is_error', [
    ({'id': 'xr-12345-other'}, False), ({'taskId': 'xr-12345-other'}, False),
    ({'state': 'unexpected'}, False), ({'state': []}, False), ({}, True),
])
async def test_status_rejects_mismatched_or_invalid_receipts_before_saving_pixels(adapter, changes, is_error):
    value, _, client = adapter

    async def status(name, arguments, timeout):
        return {'structured': {'task': {'id': 'xr-12345-4', 'action': 'screenshot', 'state': 'succeeded', **changes}},
                'is_error': is_error,
                'images': [{'mimeType': 'image/png', 'data': base64.b64encode(PNG).decode()}]}

    client.call_tool = status
    result = await call(value, 'task_status', {'task_id': 'xr-12345-4'})
    assert not result['verified'] and result['error']['code'] == 'native_task_status_invalid'
    assert result['action_may_have_run'] and not (value.data_root / 'browser_screenshots').exists()


@pytest.mark.parametrize('task', [
    {'state': 'running', 'action': 'screenshot'},
    {'state': 'timed_out', 'action': 'screenshot'},
    {'state': 'succeeded', 'action': 'screenshot', 'errorCode': 'timeout'},
    {'state': 'succeeded', 'action': 'read_page'},
    {'state': 'succeeded'},
])
async def test_status_never_invents_pixels_or_capture_success(adapter, task):
    value, _, client = adapter

    async def status(name, arguments, timeout):
        return {'structured': {'task': {'id': 'xr-12345-4', **task}},
                'images': [{'mimeType': 'image/png', 'data': base64.b64encode(PNG).decode()}]}

    client.call_tool = status
    result = await call(value, 'task_status', {'task_id': 'xr-12345-4'})
    assert result['verified'] and 'path' not in result
    assert result['native_task_succeeded'] == (task['state'] == 'succeeded' and not task.get('errorCode'))
    assert not (value.data_root / 'browser_screenshots').exists()


async def test_consumed_capture_keeps_valid_task_receipt_without_inventing_pixels(adapter):
    value, _, client = adapter

    async def status(name, arguments, timeout):
        return {'structured': {'task': {'id': 'xr-12345-4', 'action': 'screenshot', 'state': 'succeeded'}}, 'images': []}

    client.call_tool = status
    result = await call(value, 'task_status', {'task_id': 'xr-12345-4'})
    assert result['verified'] and result['native_task_succeeded'] and result['native_terminal']
    assert result['capture_pixels_available'] is False and 'recovered_from_task_status' not in result
    assert 'path' not in result


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


def _run_dom_observation(selector, nodes):
    """Execute the shipped helper against local DOM objects, without a browser."""
    node = shutil.which('node')
    if not node:
        pytest.skip('existing node runtime unavailable')
    harness = r'''
const {script,nodes}=JSON.parse(require('fs').readFileSync(0,'utf8'));
class FixtureElement {
 constructor(spec){
  this.localName=spec.tag||'span';this.nodeType=1;this.id=spec.id||'';this.type=spec.type||'';
  this.innerText=spec.text||'';this.disabled=false;this.labels=[];this.parentElement=null;
  this.previousElementSibling=null;this.attributes={...spec.attributes};this.spec=spec;
  if(spec.shadow)this.shadowRoot=new FixtureRoot(spec.shadow);
  if(spec.frame)this.contentDocument=new FixtureRoot(spec.frame);
 }
 getAttribute(name){return this.attributes[name]??null;}
 hasAttribute(name){return Object.hasOwn(this.attributes,name);}
 getBoundingClientRect(){return {width:this.spec.hidden?0:100,height:this.spec.hidden?0:20};}
 getRootNode(){return this.root;}
 get value(){if(this.type==='password'||this.type==='hidden')throw new Error('sensitive input value must not be read');return this.spec.value;}
 setAttribute(){throw new Error('DOM mutation forbidden');}
 removeAttribute(){throw new Error('DOM mutation forbidden');}
 dispatchEvent(){throw new Error('DOM events forbidden');}
 focus(){throw new Error('focus forbidden');}
}
function matches(el,part){
 if(part==='*')return true;
 if(/^#[\w-]+$/.test(part))return el.id===part.slice(1);
 if(/^\.[\w-]+$/.test(part))return String(el.getAttribute('class')||'').split(/\s+/).includes(part.slice(1));
 if(part==='input:not([type="hidden"])')return el.localName==='input'&&el.type!=='hidden';
 const parsed=/^([\w-]+)?(?:\[([\w-]+)(?:="([^"]*)")?\])?$/.exec(part);
 if(!parsed||(!parsed[1]&&!parsed[2]))throw new SyntaxError('unsupported fixture selector');
 if(parsed[1]&&el.localName!==parsed[1])return false;
 if(!parsed[2])return true;
 const value=parsed[2]==='type'?el.type:el.getAttribute(parsed[2]);
 return parsed[3]===undefined?value!==null:value===parsed[3];
}
class FixtureRoot {
 constructor(specs){
  this.nodes=specs.map(spec=>new FixtureElement(spec));
  for(const el of this.nodes){el.root=this;Object.freeze(el.attributes);Object.freeze(el);}
  Object.freeze(this.nodes);
 }
 querySelectorAll(selector){const parts=selector.split(',').map(part=>part.trim());return this.nodes.filter(el=>parts.some(part=>matches(el,part)));}
}
const document=new FixtureRoot(nodes);document.title='Local observation fixture';document.body={innerText:'Fixture'};
Object.defineProperty(document,'cookie',{get(){throw new Error('cookie access forbidden');}});
const context={document,location:{href:'https://example.test/manager'},CSS:{escape:value=>value},
 getComputedStyle:el=>({display:el.spec.hidden?'none':'block',visibility:'visible',cursor:'auto',backgroundImage:'none'})};
const observed=require('vm').runInNewContext(script,context,{timeout:1000});
process.stdout.write(observed);
'''
    generated = _cef_helper_script('read_dom', {'selector': selector, 'max_chars': 1000})
    result = subprocess.run([node, '-e', harness],
                            input=json.dumps({'script': generated, 'nodes': nodes}, ensure_ascii=True),
                            encoding='utf-8', capture_output=True, timeout=10)
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def test_actual_dom_observation_describes_explicit_plain_text_match_without_mutation():
    observed = _run_dom_observation('#draft-count', [{'id': 'draft-count', 'text': '草稿1'}])
    assert observed['elements'] == []
    assert observed['selector_match_count'] == 1 and observed['selector_is_file'] is False
    assert observed['selected_elements'][0]['selector'] == '#draft-count'
    assert observed['selected_elements'][0]['name'] == '草稿1'
    assert observed['selected_elements'][0]['scope'] == 'main'
    assert observed['selected_elements'][0]['visible'] is True
    assert observed['selected_elements_truncated'] is False and observed['observation_complete'] is True


def test_actual_dom_observation_selected_metadata_keeps_password_value_redacted():
    observed = _run_dom_observation('#password', [{'tag': 'input', 'id': 'password', 'type': 'password',
                                                  'text': 'password-must-not-leak', 'value': 'password-must-not-leak'}])
    assert observed['selector_match_count'] == 1
    selected = observed['selected_elements'][0]
    assert selected['selector'] == '#password' and selected['type'] == 'password'
    assert 'value' not in selected and selected['name'] == ''
    assert 'password-must-not-leak' not in json.dumps(observed)


@pytest.mark.parametrize('selector', ['#csrf', '*'])
def test_actual_dom_observation_selected_metadata_never_reads_hidden_token_value(selector):
    observed = _run_dom_observation(selector, [{'tag': 'input', 'id': 'csrf', 'type': 'hidden', 'hidden': True,
                                               'attributes': {'name': 'csrf_token'}, 'value': 'auth-token-must-not-leak'}])
    selected = observed['selected_elements'][0]
    assert selected['selector'] == '#csrf' and selected['type'] == 'hidden'
    assert selected['name'] == 'csrf_token' and selected['visible'] is False and 'value' not in selected
    assert 'auth-token-must-not-leak' not in json.dumps(observed)


def test_actual_dom_observation_selected_metadata_preserves_accessible_root_scope():
    observed = _run_dom_observation('.chosen', [
        {'id': 'main-count', 'text': 'Main draft', 'attributes': {'class': 'chosen'}},
        {'tag': 'div', 'id': 'shadow-host', 'shadow': [
            {'id': 'shadow-count', 'text': 'Shadow draft', 'attributes': {'class': 'chosen'}}]},
        {'tag': 'iframe', 'id': 'frame-host', 'frame': [
            {'id': 'frame-count', 'text': 'Frame draft', 'attributes': {'class': 'chosen'}}]},
    ])
    assert observed['selector_match_count'] == 3
    assert [(item['selector'], item['scope']) for item in observed['selected_elements']] == [
        ('#main-count', 'main'), ('#shadow-count', 'main / open-shadow'),
        ('#frame-count', 'main / same-origin-frame')]


@pytest.mark.parametrize('count,truncated', [(64, False), (65, True)])
def test_actual_dom_observation_bounds_selected_metadata_without_hiding_file_coverage(count, truncated):
    nodes = [{'id': 'draft-' + str(index), 'text': 'Draft', 'attributes': {'class': 'chosen'}} for index in range(count)]
    nodes.append({'tag': 'input', 'id': 'video', 'type': 'file'})
    observed = _run_dom_observation('.chosen', nodes)
    assert observed['selector_match_count'] == count and len(observed['selected_elements']) == 64
    assert observed['selected_elements_truncated'] is truncated and observed['truncated'] is truncated
    assert observed['observation_complete'] is True and observed['file_inputs'][0]['selector'] == '#video'


async def test_bounded_selector_read_exposes_selected_metadata_through_adapter(adapter):
    value, _, client = adapter
    selected = [{'selector': '#draft-count', 'tag': 'span', 'name': '草稿1', 'scope': 'main', 'visible': True}]
    client.helper_results['read_dom'].update(selected_elements=selected, selected_elements_truncated=False,
                                           selector_match_count=1, selector_is_file=False)
    result = await call(value, 'read_page', {'tab_id': '7', 'selector': '#draft-count', 'max_chars': 1000})
    assert result['selected_elements'] == selected and result['selected_elements_truncated'] is False
    assert client.helper_calls == [('read_dom', {'selector': '#draft-count', 'max_chars': 1000})]
