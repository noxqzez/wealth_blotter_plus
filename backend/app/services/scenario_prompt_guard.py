"""Light relevance gate for WF-02 scenario prompts.

Keyword *allowlists* reject too many real advisor scenarios (politics, weather,
security shocks, etc.). This gate blocks empty / tiny / obvious chitchat,
abuse, and nonsense — everything else is left to the scenario agent.
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
        "who are you",
        "what's up",
        "whats up",
        "hello there",
        "hi there",
    }
)

# Abuse / insults / sexual nonsense — not market scenarios.
_ABUSE_CUES: frozenset[str] = frozenset(
    {
        "your mom",
        "your mum",
        "your mother",
        "yo mama",
        "yo momma",
        "your dad",
        "your face",
        "fuck you",
        "screw you",
        "kill yourself",
        "kys",
        "shut up",
        "idiot",
        "stupid bot",
        "dumbass",
        "asshole",
        "motherfucker",
        "suck my",
        "eat shit",
        "go die",
    }
)

# Crude / leetspeak tokens that are never scenario language (whole-word-ish).
_PROFANITY_RE = re.compile(
    r"\b("
    r"fuck(?:ing|er|ed)?|shit(?:ty)?|bitch|cunt|asshole|"
    r"penis|penus|dick|pussy|cock|slut|whore|"
    r"nigg(?:a|er)|retard(?:ed)?"
    r")\b",
    re.IGNORECASE,
)

INVALID_PROMPT_MESSAGE = (
    "Please enter a scenario prompt (markets, politics, weather, economy, "
    "or other macro shocks). Empty, abusive, or unrelated chat is not supported."
)


def scenario_prompt_in_scope(user_prompt: str) -> bool:
    """Return True unless the prompt is empty, tiny, abusive, or chitchat."""
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
    if any(cue in normalized for cue in _ABUSE_CUES):
        return False
    if _PROFANITY_RE.search(normalized):
        return False
    # Ultra-short personal taunts with no market vocabulary
    tokens = normalized.split()
    if len(tokens) <= 3 and not re.search(
        r"\b(market|equity|equities|rate|fed|oil|inflation|recession|"
        r"rally|crash|gdp|yield|nasdaq|s&p|crypto|bitcoin|tariff|"
        r"geopolit|election|hurricane|shock|volatility|portfolio)\b",
        normalized,
    ):
        # Allow short but market-tagged prompts; block short nonsense/taunts.
        if re.search(
            r"\b(mom|mum|mother|dad|father|joke|hate|love|boring)\b",
            normalized,
        ):
            return False
    return True


def assert_scenario_prompt_in_scope(user_prompt: str) -> None:
    """Raise ValueError with a user-facing message when out of scope."""
    if not scenario_prompt_in_scope(user_prompt):
        raise ValueError(INVALID_PROMPT_MESSAGE)
