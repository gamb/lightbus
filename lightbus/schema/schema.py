import inspect
import json
from json import JSONDecodeError
from pathlib import Path
from typing import Optional, TextIO, Union, ChainMap
import jsonschema

import asyncio

import itertools

import sys

import lightbus
from lightbus.exceptions import InvalidApiForSchemaCreation, InvalidSchema, SchemaNotFound
from lightbus.schema.encoder import json_encode
from lightbus.schema.hints_to_schema import make_response_schema, make_rpc_parameter_schema, make_event_parameter_schema
from lightbus.transports.base import SchemaTransport
from lightbus.utilities.io import make_file_safe_api_name


class Schema(object):
    """ Represents the bus' schema


    Note that the presence of a schema does not necessarily
    indicate that a lightbus process is present or ready to serve
    requests for the API. For that you will need to consume the events
    produced by the state plugin.

    That being said, you should expect old schemas to be dropped
    after max_age_seconds.

    """

    def __init__(self, schema_transport: 'SchemaTransport',
                 max_age_seconds: Optional[int]=60, human_readable: bool=True):
        self.schema_transport = schema_transport
        self.max_age_seconds = max_age_seconds
        self.human_readable = human_readable

        # Schemas which have been provided locally. These will either be locally-available
        # APIs, or schemas which have been loaded from local files
        self.local_schemas = {}

        # Schemas which have been retrieved from the bus. This will also contain local
        # schemas which have been stored onto the bus. The storing and retrieving of
        # remote schemas is mediated by the schema transport.
        self.remote_schemas = {}

    def __contains__(self, item):
        return item in self.local_schemas or item in self.remote_schemas

    async def add_api(self, api: 'Api'):
        """Adds an API locally, and sends to to the transport"""
        schema = api_to_schema(api)
        self.local_schemas[api.meta.name] = schema
        await self.schema_transport.store(api.meta.name, schema, ttl_seconds=self.max_age_seconds)

    def get_api_schema(self, api_name) -> Optional[dict]:
        """Get the schema for the given API"""
        api_schema = self.local_schemas.get(api_name) or self.remote_schemas.get(api_name)
        if not api_schema:
            # TODO: Add link to docs in error message
            raise SchemaNotFound(
                'No schema could be found for API {}. You should ensure that either this '
                'API is being served by another lightbus process, or you can load this schema manually.'
                ''.format(api_name)
            )
        return api_schema

    def get_event_schema(self, api_name, event_name):
        event_schemas = self.get_api_schema(api_name)['events']
        try:
            return event_schemas[event_name]
        except KeyError:
            raise SchemaNotFound(
                "Found schema for API '{}', but it did not contain an event named '{}'"
                "".format(api_name, event_name)
            )

    def get_rpc_schema(self, api_name, rpc_name):
        rpc_schemas = self.get_api_schema(api_name)['rpcs']
        try:
            return rpc_schemas[rpc_name]
        except KeyError:
            raise SchemaNotFound(
                "Found schema for API '{}', but it did not contain a RPC named '{}'"
                "".format(api_name, rpc_name)
            )

    def get_event_or_rpc_schema(self, api_name, name):
        try:
            return self.get_event_schema(api_name, name)
        except SchemaNotFound:
            pass

        try:
            return self.get_rpc_schema(api_name, name)
        except SchemaNotFound:
            pass

        # TODO: Add link to docs in error message
        raise SchemaNotFound(
            "No schema found for '{}' on API '{}'. You should either, a) ensure this "
            "API is being served by another lightbus process, or b) load this schema manually."
            "".format(api_name, name)
        )

    def validate_parameters(self, api_name, event_or_rpc_name, parameters):
        """Validate the parameters for the given event/rpc

        This will raise an `jsonschema.ValidationError` exception on error,
        or return None if valid.
        """
        json_schema = self.get_event_or_rpc_schema(api_name, event_or_rpc_name)['parameters']
        jsonschema.validate(parameters, json_schema)

    def validate_response(self, api_name, rpc_name, response):
        """Validate the parameters for the given event/rpc

        This will raise an `jsonschema.ValidationError` exception on error,
        or return None if valid.

        Note that only RPCs have responses. Accessing this property for an
        event will result in a SchemaNotFound error.
        """
        json_schema = self.get_rpc_schema(api_name, rpc_name)['response']
        jsonschema.validate(response, json_schema)

    @property
    def api_names(self):
        return list(set(itertools.chain(self.local_schemas.keys(), self.remote_schemas.keys())))

    async def save_to_bus(self):
        """Save the schema onto the bus

        This will be done using the `schema_transport` provided to `__init__()`
        """
        for api_name, schema in self.local_schemas.items():
            await self.schema_transport.store(api_name, schema, ttl_seconds=self.max_age_seconds)

    async def load_from_bus(self):
        """Save the schema from the bus

        This will be done using the `schema_transport` provided to `__init__()`
        """
        self.remote_schemas = await self.schema_transport.load()

    async def monitor(self, interval=None):
        """Monitor for remote schema changes and keep any local schemas alive on the bus
        """
        interval = interval or self.max_age_seconds * 0.8
        try:
            while True:
                await asyncio.sleep(interval)
                # Keep alive our local schemas
                for api_name, schema in self.local_schemas.items():
                    await self.schema_transport.ping(api_name, schema, ttl_seconds=self.max_age_seconds)

                # Read the entire schema back from the bus
                await self.load_from_bus()
        except asyncio.CancelledError:
            return

    def save_local(self, destination: Union[str, Path, TextIO]=None):
        """Save all present schemas to a local file

        This will save both local & remote schemas to a local file
        """
        if isinstance(destination, str):
            destination = Path(destination)

        if destination is None:
            self._dump_to_file(sys.stdout)
            sys.stdout.write('\n')
        elif destination.is_dir():
            self._dump_to_directory(destination)
        else:
            with destination.open('w', encoding='utf8') as f:
                self._dump_to_file(f)

    def load_local(self, source: Union[str, Path, TextIO]=None):
        """Load schemas from a local file

        These files will be treated as local schemas, and will not be sent to the bus.
        This can be useful for validation during development and testing.
        """
        if isinstance(source, str):
            source = Path(source)

        def _load_schema(path, file_data):
            try:
                return json.loads(file_data)
            except JSONDecodeError as e:
                raise InvalidSchema('Could not parse schema file {}: {}'.format(path, e.msg))

        if source is None:
            # No source, read from stdin
            schema = _load_schema('[stdin]', sys.stdin.read())
        elif hasattr(source, 'is_dir') and source.is_dir():
            # Read each json file in directory
            schemas = []
            for file_path in source.glob('*.json'):
                schemas.append(_load_schema(file_path, file_path.read_text(encoding='utf8')))
            schema = ChainMap(*schemas)
        elif hasattr(source, 'read'):
            # Read file handle
            schema = _load_schema(source.name, source.read())
        elif hasattr(source, 'read_text'):
            # Read pathlib Path
            schema = _load_schema(source.name, source.read_text())
        else:
            raise InvalidSchema(
                'Did not recognise provided source as either a '
                'directory path, file path, or file handle: {}'.format(source)
            )

        for api_name, api_schema in schema.items():
            self.local_schemas[api_name] = api_schema

        return schema

    def _dump_to_directory(self, destination: Path):
        for api_name in self.api_names:
            file_name = '{}.json'.format(make_file_safe_api_name(api_name))
            (destination / file_name).write_text(self._get_dump(api_name), encoding='utf8')

    def _dump_to_file(self, f):
        f.write(self._get_dump())

    def _get_dump(self, api_name=None):
        if api_name:
            schema = {api_name: self.get_api_schema(api_name)}
        else:
            schema = {api_name: self.get_api_schema(api_name) for api_name in self.api_names}

        indent = 2 if self.human_readable else None
        return json_encode(schema, indent=indent)


class Parameter(inspect.Parameter):
    """Describes the name and type of an event parameter"""
    empty = inspect.Parameter.empty

    def __init__(self, name, annotation=empty, *, default=empty):
        super(Parameter, self).__init__(name, inspect.Parameter.KEYWORD_ONLY,
                                        default=default,
                                        annotation=annotation
                                        )


class WildcardParameter(inspect.Parameter):
    """Describes a **kwargs style parameter to an event
    """
    # TODO: Consider removing if not found to be useful
    empty = inspect.Parameter.empty

    def __init__(self):
        super(WildcardParameter, self).__init__(
            name='kwargs',
            kind=inspect.Parameter.VAR_KEYWORD,
            default={},
            annotation=dict
        )


def api_to_schema(api: 'lightbus.Api') -> dict:
    schema = {
        'rpcs': {},
        'events': {},
    }

    if isinstance(api, type):
        raise InvalidApiForSchemaCreation(
            "An attempt was made to derive an API schema from a type/class, rather than "
            "from an instance of an API. This is probably because you are passing an API "
            "class to api_to_schema(), rather than an instance of the API class."
        )

    for member_name, member in inspect.getmembers(api):
        if member_name.startswith('_'):
            # Don't create schema from private methods
            continue
        if hasattr(lightbus.Api, member_name):
            # Don't create schema for methods defined on Api class
            continue

        if inspect.ismethod(member):
            schema['rpcs'][member_name] = {
                'parameters': make_rpc_parameter_schema(api.meta.name, member_name, method=member),
                'response': make_response_schema(api.meta.name, member_name, method=member),
            }
        elif isinstance(member, lightbus.Event):
            schema['events'][member_name] = {
                'parameters': make_event_parameter_schema(api.meta.name, member_name, event=member),
            }

    return schema


def _parameter_names(parameters) -> set:
    """Take a list of parameters (as strings or Parameter) and return a list of parameter names"""
    return {
        p.name if isinstance(p, Parameter) else p
        for p
        in parameters
    }
