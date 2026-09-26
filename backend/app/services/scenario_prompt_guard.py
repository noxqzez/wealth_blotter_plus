"""Light relevance gate for WF-02 scenario prompts.

Keyword *allowlists* reject too many real advisor scenarios (politics, weather,
security shocks, etc.). This gate only blocks empty / tiny / obvious chitchat.
Everything else is left to the scenario agent to interpret.
"""

from __future__ import annotations

import re

_MIN_CHARS = 8

# Obvious off-topic chitchat (substring match on lowercased prompt).
_OFFTOPIC_CUES: frozenset[str] = frozenset(
    {
        "i hate mondays",
        "what should i cook",
        "tell me a joke",
        "joke about cats",
        "how are you",
        "what's for dinner",
        "whats for dinner",
        "good morning",
        "good night",
        "lol",
        "lmao",
    }
)

INVALID_PROMPT_MESSAGE = (
    "Please enter a scenario prompt (markets, politics, weather, economy, "
    "or other macro shocks). Empty or unrelated chat is not supported."
)


def scenario_prompt_in_scope(user_prompt: str) -> bool:
    """Return True unless the prompt is empty, tiny, or obvious chitchat."""
    text = (user_prompt or "").strip()
    if len(text) < _MIN_CHARS:
        return False
    lower = text.lower()
    normalized = re.sub(r"[^\w\s&+-]", " ", lower)
    normalized = re.sub(r"\s+", " ", normalized).strip()
    # Pure gibberish / keyboard mash (no vowels and short)
    letters = re.sub(r"[^a-z]", "", normalized)
    if letters and len(letters) <= 12 and not re.search(r"[aeiou]", letters):
        return False
    if any(cue in normalized for cue in _OFFTOPIC_CUES):
        return False
    return True


def assert_scenario_prompt_in_scope(user_prompt: str) -> None:
    """Raise ValueError with a user-facing message when out of scope."""
    if not scenario_prompt_in_scope(user_prompt):
        raise ValueError(INVALID_PROMPT_MESSAGE)
