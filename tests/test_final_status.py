import json
import pytest
from wb2api.protocol import ProtocolError, ProtocolMapper


@pytest.mark.parametrize('status', ['failed', 'error', 'cancelled'])
def test_failed_final_message_is_not_success(status):
    mapper = ProtocolMapper({'delta_events': ['message_delta'], 'done_events': ['message_finished']})
    event = {'event': 'message', 'data': json.dumps({'type': 'message_finished', 'thread_id': 'thread', 'turn_id': 'turn',
        'message': {'role': 'assistant', 'status': status, 'blocks': [{'type': 'text', 'text': 'partial'}]}})}
    with pytest.raises(ProtocolError) as exc:
        mapper.consume(event, 'thread', 'turn')
    assert exc.value.code == 'upstream_turn_error'
