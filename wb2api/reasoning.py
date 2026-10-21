"""Explicit prompt-level effort hints; no claimed upstream reasoning budget."""
from typing import Literal

ReasoningEffort = Literal['none', 'minimal', 'low', 'medium', 'high', 'xhigh', 'max']

GATEWAY_PROMPT = (
    '[Gateway response policy]\n'
    'You are a general-purpose assistant serving the client through a 2api gateway. '
    'Reasoning-effort settings and client function calling are supplied by this gateway compatibility layer, '
    'not by the upstream application. Do not introduce yourself as Wabi, act as its branded app assistant, '
    'or add Wabi branding to the answer, explanations, or any reasoning summary. '
    'Do not invent a foundation-model provider or identity; gateway model names are routing aliases. '
    'Follow the client task and required output/tool protocol. Do not reveal private internal deliberations; '
    'return the requested result and only useful concise explanations.\n\n'
)

_HINTS = {
    'none': 'For straightforward tasks, answer directly without unnecessary analysis. Do not skip safety or correctness checks.',
    'minimal': 'Use a minimal analysis appropriate to the task; answer concisely and check essential facts.',
    'low': 'Use a brief analysis; check the main assumptions and give a concise answer.',
    'medium': 'Analyze at a normal depth. Check important assumptions and summarize the conclusion clearly.',
    'high': 'Analyze carefully, consider important alternatives and edge cases, and verify the conclusion before answering.',
    'xhigh': 'Use a thorough analysis appropriate to the task. Compare plausible approaches, test edge cases and verify the conclusion. Avoid unnecessary external actions.',
    'max': 'Use the most comprehensive analysis appropriate to the task. Independently check assumptions and conclusions, consider counterexamples and reconcile competing evidence. Prefer accuracy over speed while respecting the requested answer length and avoiding unnecessary external actions.',
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
