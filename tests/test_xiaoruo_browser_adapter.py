import pytest
from web_tools.xiaoruo_browser_adapter import XiaoruoBrowserAdapter


class ExistingClient:
    def __init__(self):
        self.calls = []

    async def browser_instances(self):
        return {'ok': True, 'browsers': [
            {'browser_id': 'edge-1', 'browser_kind': 'edge'},
            {'browser_id': 'manual-cef-1', 'browser_kind': 'xiaoruo_browser', 'connection_generation': 'generation1'}]}

    async def browser_control(self, **kwargs):
        self.calls.append(kwargs)
        return {'ok': True, **kwargs}


async def test_existing_manual_cef_is_used_without_launching_or_copying_profile():
    client = ExistingClient()
    adapter = XiaoruoBrowserAdapter(client)
    inventory = await adapter.discover()
    assert [b['browser_id'] for b in inventory['browsers']] == ['manual-cef-1']
    assert not inventory['starts_new_browser'] and not inventory['exports_profile_data']
    result = await adapter.call('manual-cef-1', 'read', tab_id='tab-7')
    assert result['ok'] and client.calls[0]['tab_id'] == 'tab-7'


@pytest.mark.parametrize('target', ['edge-1', 'stale-browser', 'lightpanda', 'stdio-launcher'])
async def test_no_silent_fallback_to_other_browser_engine_or_new_session(target):
    client = ExistingClient()
    result = await XiaoruoBrowserAdapter(client).call(target, 'navigate', tab_id='tab-7', confirm=True)
    assert not result['ok'] and not client.calls


async def test_confirmation_generation_and_operation_are_preserved():
    client = ExistingClient()
    result = await XiaoruoBrowserAdapter(client).call('manual-cef-1', 'type', tab_id='tab-7',
        arguments={'text': 'sample', 'expected_generation': 'generation1'},
        operation_id='operation-1', confirm=True, timeout=2000)
    assert result['confirm'] is True
    assert result['arguments']['expected_generation'] == 'generation1'
    assert result['operation_id'] == 'operation-1' and result['timeout'] == 2000