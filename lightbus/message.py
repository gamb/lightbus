import traceback
from typing import Optional, Dict, Any, Sequence
from uuid import uuid1

from base64 import b64encode

__all__ = ['Message', 'RpcMessage', 'ResultMessage', 'EventMessage']


class Message(object):
    required_metadata: Sequence

    def get_metadata(self) -> dict:
        """Get the non-kwarg fields of this message

        Will be used by the serializers
        """
        raise NotImplementedError()

    def get_kwargs(self) -> dict:
        """Get the kwarg fields of this message

        Will be used by the serializers
        """
        raise NotImplementedError()

    @classmethod
    def from_dict(cls, metadata: dict, kwargs: dict) -> 'Message':
        """Create a message instance given the metadata and kwargs

        Will be used by the serializers
        """
        raise NotImplementedError()


class RpcMessage(Message):
    required_metadata = ['rpc_id', 'api_name', 'procedure_name', 'return_path']

    def __init__(self, *, api_name: str, procedure_name: str, kwargs: Optional[dict]=None,
                 return_path: Any=None, rpc_id: str=''):

        self.rpc_id = rpc_id or b64encode(uuid1().bytes).decode('utf8')
        self.api_name = api_name
        self.procedure_name = procedure_name
        self.kwargs = kwargs
        self.return_path = return_path

    def __repr__(self):
        return '<{}: {}>'.format(self.__class__.__name__, self)

    def __str__(self):
        return '{}({})'.format(
            self.canonical_name,
            ', '.join('{}={}'.format(k, v) for k, v in self.kwargs.items())
        )

    @property
    def canonical_name(self):
        return "{}.{}".format(self.api_name, self.procedure_name)

    def get_metadata(self) -> dict:
        return {
            'rpc_id': self.rpc_id,
            'api_name': self.api_name,
            'procedure_name': self.procedure_name,
            'return_path': self.return_path or '',
        }

    def get_kwargs(self):
        return self.kwargs

    @classmethod
    def from_dict(cls, metadata: Dict[str, str], kwargs: Dict[str, Any]) -> 'RpcMessage':
        return cls(**metadata, kwargs=kwargs)


class ResultMessage(Message):
    required_metadata = ['rpc_id']

    def __init__(self, *, result, rpc_id, error: bool=False, trace: str=None):
        self.rpc_id = rpc_id

        if isinstance(result, BaseException):
            self.result = str(result)
            self.error = True
            self.trace = ''.join(traceback.format_exception(
                etype=type(result),
                value=result,
                tb=result.__traceback__
            ))
        else:
            self.result = result
            self.error = error
            self.trace = trace

    def __repr__(self):
        if self.error:
            return '<{} (ERROR): {}>'.format(self.__class__.__name__, self.result)
        else:
            return '<{} (SUCCESS): {}>'.format(self.__class__.__name__, self.result)

    def __str__(self):
        return str(self.result)

    def get_metadata(self) -> dict:
        metadata = {
            'rpc_id': self.rpc_id,
            'error': self.error,
        }
        if self.error:
            metadata['trace'] = self.trace
        return metadata

    def get_kwargs(self):
        return {
            'result': self.result
        }

    @classmethod
    def from_dict(cls, metadata: Dict[str, str], kwargs: Dict[str, Any]) -> 'ResultMessage':
        return cls(**metadata, result=kwargs.get('result'))


class EventMessage(Message):
    required_metadata = ['api_name', 'event_name']

    def __init__(self, *, api_name: str, event_name: str, kwargs: Optional[dict]=None):
        self.api_name = api_name
        self.event_name = event_name
        self.kwargs = kwargs or {}

    def __repr__(self):
        return '<{}: {}>'.format(self.__class__.__name__, self)

    def __str__(self):
        return '{}({})'.format(
            self.canonical_name,
            ', '.join('{}={}'.format(k, v) for k, v in self.kwargs.items())
        )

    @property
    def canonical_name(self):
        return "{}.{}".format(self.api_name, self.event_name)

    def get_metadata(self) -> dict:
        return {
            'api_name': self.api_name,
            'event_name': self.event_name,
        }

    def get_kwargs(self):
        return self.kwargs

    @classmethod
    def from_dict(cls, metadata: Dict[str, str], kwargs: Dict[str, Any]) -> 'EventMessage':
        return cls(**metadata, kwargs=kwargs)
