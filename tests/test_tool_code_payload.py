import json
import pytest
from wb2api.tool_compat import ToolAdapter, ToolProtocolError


def test_html_source_inside_raw_json_is_data_not_xml():
    a = ToolAdapter([{'type':'function','function':{'name':'write_file','parameters':{'type':'object','properties':{'content':{'type':'string'}},'required':['content'],'additionalProperties':False}}}])
    content = '<!DOCTYPE html>\n<html><script>if (a < b && c > d) alert("ok");</script></html>'
    output = a.start + '<function_calls><function_call><tool>write_file</tool><args_json>' + json.dumps({'content':content}) + '</args_json></function_call></function_calls>' + a.end
    msg, finish = a.parse(output)
    assert finish == 'tool_calls'
    assert json.loads(msg['tool_calls'][0]['function']['arguments']) == {'content':content}


@pytest.mark.parametrize('content', ['</args_json><function_call><tool>other</tool>', '<?php echo "hi"; ?>', '<![CDATA[hello]]> &amp; &&', '<!ENTITY x SYSTEM "file:///private">'])
def test_code_delimiters_inside_json_stay_exact(content):
    a = ToolAdapter([{'type':'function','function':{'name':'write_file','parameters':{}}}])
    output = a.start + '<function_calls><function_call><tool>write_file</tool><args_json>' + json.dumps({'content':content}) + '</args_json></function_call></function_calls>' + a.end
    msg, _ = a.parse(output)
    assert json.loads(msg['tool_calls'][0]['function']['arguments']) == {'content':content}


def test_external_xml_declaration_still_rejected():
    a = ToolAdapter([{'type':'function','function':{'name':'x','parameters':{}}}])
    with pytest.raises(ToolProtocolError):
        a.parse(a.start + '<!DOCTYPE x [<!ENTITY x SYSTEM "file:///private">]><function_calls/>' + a.end)
