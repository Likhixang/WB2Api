import json
import pytest
from wb2api.tool_compat import ToolAdapter, ToolProtocolError


@pytest.mark.parametrize('schema', [{}, None, {'properties': {'q': {'type': 'string'}}},
    {'$schema': 'http://json-schema.org/draft-07/schema#', 'type': 'object', 'properties': {'q': {'type': 'string'}}},
    {'$schema': 'https://json-schema.org/draft/2019-09/schema', 'type': 'object'}])
def test_client_schema_dialects_and_optional_nulls(schema):
    adapter = ToolAdapter([{'type': 'function', 'function': {'name': 'lookup', 'parameters': schema, 'strict': None, 'description': None}}], 'required')
    assert json.loads(adapter.validate_arguments('lookup', '{"q":"ok"}')) == {'q': 'ok'}


def test_draft7_constraints_not_weakened():
    a = ToolAdapter([{'type': 'function', 'function': {'name': 'lookup', 'parameters': {
        '$schema': 'http://json-schema.org/draft-07/schema#', 'properties': {'q': {'type': 'integer'}}, 'required': ['q']}}}])
    with pytest.raises(ToolProtocolError):
        a.validate_arguments('lookup', '{"q":"wrong"}')


@pytest.mark.parametrize('schema', [
    {'type': 'object', '$ref': 'https://example.invalid/private'},
    {'type': 'object', '$id': 'https://example.invalid/private'},
    {'type': 'object', '$schema': 'https://example.invalid/schema'},
])
def test_external_schemas_rejected_without_echo(schema):
    with pytest.raises(ToolProtocolError) as e:
        ToolAdapter([{'type': 'function', 'function': {'name': 'lookup', 'parameters': schema}}])
    assert 'example.invalid' not in str(e.value)
    assert ':' in str(e.value)
