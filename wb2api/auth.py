"""Parse pasted Google callbacks without exposing authorization values in errors."""
from html import unescape
import re
from urllib.parse import parse_qs, urlsplit


def google_callback(value: str) -> tuple[str, str]:
    value = value.strip()
    markdown = re.fullmatch(r'\[[\s\S]*\]\((https?://[\s\S]+)\)', value)
    if markdown:
        value = markdown.group(1)
    if value.startswith('<') and value.endswith('>'):
        value = value[1:-1]
    value = unescape(value.replace('\\&', '&'))
    if '://' not in value and not value.startswith(('?', 'code=', 'state=', 'error=')):
        if not value or any(character.isspace() for character in value):
            raise ValueError('授权 code 格式无效，请粘贴完整回调网址或单独的 code。')
        return value, ''
    try:
        parsed = urlsplit(value)
        query = parsed.query or parsed.fragment or value.lstrip('?')
        parameters = parse_qs(query, keep_blank_values=True)
    except ValueError:
        raise ValueError('授权回调网址格式无效。') from None
    if 'error' in parameters:
        raise ValueError('Google 授权未完成或被取消，请重新打开授权页面。')
    if len(parameters.get('code', [])) != 1 or not parameters['code'][0].strip():
        raise ValueError('回调网址必须包含唯一的 code，请复制授权完成后的完整网址。')
    if 'state' in parameters and (len(parameters['state']) != 1 or not parameters['state'][0]):
        raise ValueError('回调网址中的 state 无效，请复制授权完成后的完整网址。')
    return parameters['code'][0], parameters.get('state', [''])[0]
