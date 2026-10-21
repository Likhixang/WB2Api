"""Explicit prompt-level effort hints; no claimed upstream reasoning budget."""
from typing import Literal

ReasoningEffort = Literal['none', 'minimal', 'low', 'medium', 'high', 'xhigh']

_HINTS = {
    'none': 'For straightforward tasks, answer directly without unnecessary analysis. Do not skip safety or correctness checks.',
    'minimal': 'Use a minimal analysis appropriate to the task; answer concisely and check essential facts.',
    'low': 'Use a brief analysis; check the main assumptions and give a concise answer.',
    'medium': 'Analyze at a normal depth. Check important assumptions and summarize the conclusion clearly.',
    'high': 'Analyze carefully, consider important alternatives and edge cases, and verify the conclusion before answering.',
    'xhigh': 'Use a thorough analysis appropriate to the task. Compare plausible approaches, test edge cases and verify the conclusion. Avoid unnecessary external actions.',
}


def effort_hint(effort: ReasoningEffort | None) -> str:
    if effort is None:
        return ''
    return (
        '[Response effort preference: ' + effort + '; prompt compatibility only]\n'
        + _HINTS[effort]
        + ' Follow any required tool-call or output format exactly. '
        'Return only the requested answer and useful explanations, not private internal deliberations.\n\n'
    )
