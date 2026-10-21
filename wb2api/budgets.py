"""Gateway budgets aligned to the public Sol sizes, not verified upstream limits.

The upstream tokenizer is unknown. o200k_base is an explicit local approximation;
these counts must never be presented as provider usage or billing.
"""
from functools import lru_cache
import time

import tiktoken

CONTEXT_TOKENS = 1_050_000
INPUT_TOKENS = 922_000
OUTPUT_TOKENS = 128_000
# Transport protection, not context size. Allows JSON escaping and tool histories.
REQUEST_BYTES = 64 * 1024 * 1024


class BudgetError(ValueError):
    status = 400
    code = 'context_length_exceeded'


@lru_cache(maxsize=1)
def encoding():
    return tiktoken.get_encoding('o200k_base')


def count_tokens(text):
    # Client text resembling special tokens is ordinary untrusted data.
    return len(encoding().encode_ordinary(text))


def check_input(prompt, requested_output=None):
    output = OUTPUT_TOKENS if requested_output is None else requested_output
    if type(output) is not int or not 1 <= output <= OUTPUT_TOKENS:
        raise BudgetError('Output budget must be between 1 and 128000 tokens (gateway estimate)')
    count = count_tokens(prompt)
    if count > INPUT_TOKENS or count + output > CONTEXT_TOKENS:
        raise BudgetError('Prompt exceeds the gateway estimated token budget: input <= 922000; input + output <= 1050000')
    return count, output


class OutputBudget:
    """Count across chunk boundaries; clip only visible text, never execute tools.

    Coalesce small deltas to avoid repeatedly tokenizing a long answer on each
    character. Keep one accumulated prefix so estimates are chunk-independent.
    """
    def __init__(self, limit):
        self.limit = limit
        self.text = ''
        self.pending = ''
        self.truncated = False
        self.last_flush = time.monotonic()

    def push(self, text, *, final=False):
        self.pending += text
        if not final and self.text and len(self.pending) < 2048 and time.monotonic() - self.last_flush < 0.1:
            return ''
        self.last_flush = time.monotonic()
        candidate = self.text + self.pending
        tokens = encoding().encode_ordinary(candidate)
        if len(tokens) <= self.limit:
            result = self.pending
            self.text = candidate
            self.pending = ''
            return result
        # UTF-8 safe: a token boundary can split a multibyte character.
        clipped = b''.join(encoding().decode_single_token_bytes(t) for t in tokens[:self.limit]).decode('utf-8', errors='ignore')
        if not clipped.startswith(self.text):
            # Retokenization at the previous chunk boundary can revise its tail.
            # Already-delivered bytes cannot be revoked; emit no further bytes.
            clipped = self.text
        result = clipped[len(self.text):]
        self.text, self.pending, self.truncated = clipped, '', True
        return result

    async def stream(self, source):
        try:
            async for text in source:
                value = self.push(text)
                if value:
                    yield value
                if self.truncated:
                    return
            value = self.push('', final=True)
            if value:
                yield value
        finally:
            await source.aclose()
