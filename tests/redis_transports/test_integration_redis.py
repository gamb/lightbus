import asyncio
import logging

import jsonschema
import pytest
import lightbus
from lightbus import BusNode
from lightbus.api import registry
from lightbus.config import Config
from lightbus.exceptions import LightbusTimeout, LightbusServerError
from lightbus.plugins import manually_set_plugins
from lightbus.transports.redis import StreamUse
from lightbus.utilities.async import cancel

pytestmark = pytest.mark.integration

stream_use_test_data = [StreamUse.PER_EVENT, StreamUse.PER_API]


@pytest.mark.run_loop
async def test_bus_fixture(bus: lightbus.BusNode):
    """Just sanity check the fixture"""
    rpc_transport = bus.bus_client.transport_registry.get_rpc_transport('default')
    result_transport = bus.bus_client.transport_registry.get_result_transport('default')
    event_transport = bus.bus_client.transport_registry.get_event_transport('default')

    connection_manager_rpc = await rpc_transport.connection_manager()
    connection_manager_result = await result_transport.connection_manager()
    connection_manager_event = await event_transport.connection_manager()

    with await connection_manager_rpc as redis_rpc, \
            await connection_manager_result as redis_result, \
            await connection_manager_event as redis_event:

        # Each transport should have its own connection
        assert redis_rpc is not redis_result
        assert redis_result is not redis_event
        assert redis_rpc is not redis_event

        # Check they are all looking at the same redis instance
        await redis_rpc.set('X', 1)
        assert await redis_result.get('X')
        assert await redis_rpc.get('X')

        info = await redis_rpc.info()
        # transports: rpc, result, event, schema
        assert int(info['clients']['connected_clients']) == 4


@pytest.mark.run_loop
async def test_rpc(bus: lightbus.BusNode, dummy_api):
    """Full rpc call integration test"""

    async def co_call_rpc():
        await asyncio.sleep(0.1)
        return await bus.my.dummy.my_proc.call_async(field='Hello! 😎')

    async def co_consume_rpcs():
        return await bus.bus_client.consume_rpcs(apis=[dummy_api])

    (call_task, ), (consume_task, ) = await asyncio.wait([co_call_rpc(), co_consume_rpcs()], return_when=asyncio.FIRST_COMPLETED)
    consume_task.cancel()
    assert call_task.result() == 'value: Hello! 😎'


@pytest.mark.run_loop
async def test_rpc_timeout(bus: lightbus.BusNode, dummy_api):
    """Full rpc call integration test"""

    async def co_call_rpc():
        await asyncio.sleep(0.1)
        return await bus.my.dummy.sudden_death.call_async(n=0)

    async def co_consume_rpcs():
        return await bus.bus_client.consume_rpcs(apis=[dummy_api])

    (call_task, ), (consume_task, ) = await asyncio.wait([co_call_rpc(), co_consume_rpcs()], return_when=asyncio.FIRST_COMPLETED)
    consume_task.cancel()

    with pytest.raises(LightbusTimeout):
        call_task.result()


@pytest.mark.run_loop
async def test_rpc_error(bus: lightbus.BusNode, dummy_api):
    """Test what happens when the remote procedure throws an error"""

    async def co_call_rpc():
        await asyncio.sleep(0.1)
        return await bus.my.dummy.general_error.call_async()

    async def co_consume_rpcs():
        return await bus.bus_client.consume_rpcs(apis=[dummy_api])

    (call_task, ), (consume_task, ) = await asyncio.wait([co_call_rpc(), co_consume_rpcs()], return_when=asyncio.FIRST_COMPLETED)

    consume_task.cancel()
    call_task.cancel()

    with pytest.raises(LightbusServerError):
        await call_task.result()


@pytest.mark.run_loop
@pytest.mark.parametrize("stream_use", stream_use_test_data, ids=['stream_per_event', 'stream_per_api'])
async def test_event(bus: lightbus.BusNode, dummy_api, stream_use):
    """Full event integration test"""
    bus.bus_client.transport_registry.get_event_transport('default').stream_use = stream_use
    manually_set_plugins({})
    received_kwargs = []
    received_api_name = None
    received_event_name = None

    async def listener(api_name, event_name, **kwargs):
        nonlocal received_kwargs, received_api_name, received_event_name
        received_kwargs.append(kwargs)
        received_api_name = api_name
        received_event_name = event_name

    await bus.my.dummy.my_event.listen_async(listener)
    await asyncio.sleep(0.01)
    await bus.my.dummy.my_event.fire_async(field='Hello! 😎')
    await asyncio.sleep(0.01)

    # await asyncio.gather(co_fire_event(), co_listen_for_events())
    assert received_kwargs == [{'field': 'Hello! 😎'}]
    assert received_api_name == 'my.dummy'
    assert received_event_name == 'my_event'


@pytest.mark.run_loop
async def test_rpc_ids(bus: lightbus.BusNode, dummy_api, mocker):
    """Ensure the rpc_id comes back correctly"""

    async def co_call_rpc():
        await asyncio.sleep(0.1)
        return await bus.my.dummy.my_proc.call_async(field='foo')

    async def co_consume_rpcs():
        return await bus.bus_client.consume_rpcs(apis=[dummy_api])

    mocker.spy(bus.bus_client, 'send_result')

    (call_task, ), (consume_task, ) = await asyncio.wait([co_call_rpc(), co_consume_rpcs()], return_when=asyncio.FIRST_COMPLETED)
    _, kw = bus.bus_client.send_result.call_args
    rpc_message = kw['rpc_message']
    result_message = kw['result_message']
    consume_task.cancel()

    assert rpc_message.rpc_id
    assert result_message.rpc_id
    assert rpc_message.rpc_id == result_message.rpc_id


class ApiA(lightbus.Api):
    event_a = lightbus.Event()

    class Meta:
        name = 'api_a'

    def rpc_a(self): return 'A'


class ApiB(lightbus.Api):
    event_b = lightbus.Event()

    class Meta:
        name = 'api_b'

    def rpc_b(self): return 'b'


@pytest.mark.run_loop
async def test_multiple_rpc_transports(loop, server, redis_server_b, consume_rpcs):
    """Configure a bus with two redis transports and ensure they write to the correct redis servers"""
    registry.add(ApiA())
    registry.add(ApiB())

    manually_set_plugins(plugins={})

    redis_server_a = server

    port_a = redis_server_a.tcp_address.port
    port_b = redis_server_b.tcp_address.port

    logging.warning(f'Server A port: {port_a}')
    logging.warning(f'Server B port: {port_b}')

    config = Config.load_dict({
        'bus': {
            'schema': {
                'transport': {'redis': {'url': f'redis://localhost:{port_a}'}},
            }
        },
        'apis': {
            'default': {
                'rpc_transport': {'redis': {'url': f'redis://localhost:{port_a}'}},
                'result_transport': {'redis': {'url': f'redis://localhost:{port_a}'}},
            },
            'api_b': {
                'rpc_transport': {'redis': {'url': f'redis://localhost:{port_b}'}},
                'result_transport': {'redis': {'url': f'redis://localhost:{port_b}'}},
            },
        }
    })

    bus = BusNode(name='', parent=None, bus_client=lightbus.BusClient(config=config, loop=loop))
    asyncio.ensure_future(consume_rpcs(bus))
    await asyncio.sleep(0.1)

    await bus.api_a.rpc_a.call_async()
    await bus.api_b.rpc_b.call_async()


@pytest.mark.run_loop
async def test_multiple_event_transports(loop, server, redis_server_b):
    """Configure a bus with two redis transports and ensure they write to the correct redis servers"""
    registry.add(ApiA())
    registry.add(ApiB())

    manually_set_plugins(plugins={})

    redis_server_a = server

    port_a = redis_server_a.tcp_address.port
    port_b = redis_server_b.tcp_address.port

    logging.warning(f'Server A port: {port_a}')
    logging.warning(f'Server B port: {port_b}')

    config = Config.load_dict({
        'bus': {
            'schema': {
                'transport': {'redis': {'url': f'redis://localhost:{port_a}'}},
            }
        },
        'apis': {
            'default': {
                'event_transport': {'redis': {'url': f'redis://localhost:{port_a}'}},
            },
            'api_b': {
                'event_transport': {'redis': {'url': f'redis://localhost:{port_b}'}},
            },
        }
    })

    bus = BusNode(name='', parent=None, bus_client=lightbus.BusClient(config=config, loop=loop))
    await asyncio.sleep(0.1)

    await bus.api_a.event_a.fire_async()
    await bus.api_b.event_b.fire_async()

    connection_manager_a = bus.bus_client.transport_registry.get_event_transport('api_a').connection_manager
    connection_manager_b = bus.bus_client.transport_registry.get_event_transport('api_b').connection_manager

    with await connection_manager_a() as redis:
        assert await redis.xrange('api_a.event_a:stream')
        assert await redis.xrange('api_b.event_b:stream') == []

    with await connection_manager_b() as redis:
        assert await redis.xrange('api_a.event_a:stream') == []
        assert await redis.xrange('api_b.event_b:stream')


@pytest.mark.run_loop
async def test_validation_rpc(loop, bus: lightbus.BusNode, dummy_api, mocker):
    """Check validation happens when performing an RPC"""
    config = Config.load_dict({
        'apis': {
            'default': {'validate': True, 'strict_validation': True}
        }
    })
    bus.bus_client.config = config
    mocker.patch('jsonschema.validate', autospec=True)

    async def co_consume_rpcs():
        return await bus.bus_client.consume_rpcs(apis=[dummy_api])

    await bus.bus_client.schema.add_api(dummy_api)
    await bus.bus_client.schema.save_to_bus()
    await bus.bus_client.schema.load_from_bus()

    consume_task = asyncio.ensure_future(co_consume_rpcs(), loop=loop)

    await asyncio.sleep(0.1)
    result = await bus.my.dummy.my_proc.call_async(field='Hello')

    await cancel(consume_task)

    assert result == 'value: Hello'

    # Validate gets called
    jsonschema.validate.assert_called_with(
        'value: Hello',
        {
            '$schema': 'http://json-schema.org/draft-04/schema#',
            'title': 'RPC my.dummy.my_proc() response',
            'type': 'string'
        }
    )


@pytest.mark.run_loop
async def test_validation_event(loop, bus: lightbus.BusNode, dummy_api, mocker):
    """Check validation happens when firing an event"""
    config = Config.load_dict({
        'apis': {
            'default': {'validate': True, 'strict_validation': True}
        }
    })
    bus.bus_client.config = config
    mocker.patch('jsonschema.validate', autospec=True)

    async def co_listener(*a, **kw):
        pass

    await bus.bus_client.schema.add_api(dummy_api)
    await bus.bus_client.schema.save_to_bus()
    await bus.bus_client.schema.load_from_bus()

    listener_task = await bus.bus_client.listen_for_event('my.dummy', 'my_event', co_listener)

    await asyncio.sleep(0.1)
    await bus.my.dummy.my_event.fire_async(field='Hello')

    await cancel(listener_task)

    # Validate gets called
    jsonschema.validate.assert_called_with(
        {'field': 'Hello'},
        {
            '$schema': 'http://json-schema.org/draft-04/schema#',
            'type': 'object',
            'additionalProperties': False,
            'properties': {
                'field': {'type': 'string'}
            },
            'required': ['field'],
            'title': 'Event my.dummy.my_event parameters'
        }
    )


@pytest.mark.run_loop
async def test_listen_to_multiple_events_across_multiple_transports(loop, server, redis_server_b):
    registry.add(ApiA())
    registry.add(ApiB())

    manually_set_plugins(plugins={})

    redis_server_a = server

    port_a = redis_server_a.tcp_address.port
    port_b = redis_server_b.tcp_address.port

    logging.warning(f'Server A port: {port_a}')
    logging.warning(f'Server B port: {port_b}')

    config = Config.load_dict({
        'bus': {
            'schema': {
                'transport': {'redis': {'url': f'redis://localhost:{port_a}'}},
            }
        },
        'apis': {
            'default': {
                'event_transport': {'redis': {'url': f'redis://localhost:{port_a}'}},
            },
            'api_b': {
                'event_transport': {'redis': {'url': f'redis://localhost:{port_b}'}},
            },
        }
    })

    bus = BusNode(name='', parent=None, bus_client=lightbus.BusClient(config=config, loop=loop))
    await asyncio.sleep(0.1)

    calls = 0
    def listener(*args, **kwargs):
        nonlocal calls
        calls += 1

    await bus.listen_multiple_async([
        bus.api_a.event_a,
        bus.api_b.event_b
    ], listener=listener)

    await asyncio.sleep(0.1)
    await bus.api_a.event_a.fire_async()
    await bus.api_b.event_b.fire_async()
    await asyncio.sleep(0.1)

    assert calls == 2
