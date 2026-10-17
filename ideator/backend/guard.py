"""Input checks and rate limits for the public chat. Plain rules, no AI: fast, predictable, and cannot be talked around."""
import re
import time
from collections import defaultdict, deque
from typing import Deque, Dict, Optional

MAX_CHARS = 600
# Common English profanity and slurs, matched as whole words (and simple leetspeak).
_BAD = ["fuck", "fucking", "fucker", "motherfucker", "shit", "bullshit", "bitch", "cunt", "asshole", "dick", "dickhead",
        "bastard", "slut", "whore", "pussy", "cock", "wanker", "twat", "retard", "faggot", "nigger", "nigga"]
_BAD_RE = re.compile(r"\b(" + "|".join(_BAD) + r")s?\b", re.I)
_LEET = str.maketrans("013457@$", "oieastas")
# Markup and script-ish payloads: the app never needs them, so refuse instead of trying to clean them.
_CODE_RE = re.compile(r"<\s*/?\s*[a-z!]|javascript:|data:text/html|\bon\w+\s*=|\{\{|\$\{|```", re.I)


def check_message(text: str, new_idea: bool) -> Optional[str]:
    """Return a user-facing reason to refuse the message, or None if it is fine."""
    t = text.strip()
    if not t:
        return "Type a message first."
    if len(t) > MAX_CHARS:
        return f"Keep it under {MAX_CHARS} characters."
    if _CODE_RE.search(t):
        return "Please describe your idea in plain words. Code, HTML and scripts are not accepted."
    if _BAD_RE.search(t) or _BAD_RE.search(t.translate(_LEET)):
        return "Please keep it clean. Describe your idea without profanity."
    words = re.findall(r"[^\W\d_]{2,}", t)
    if new_idea and len(words) < 3:
        return "Describe the idea in at least 3 words, for example \"meal planner for busy parents\"."
    if not words:
        return "That doesn't look like words. Describe your idea or a change to it."
    return None


class RateLimiter:
    """Sliding window per key. shortcut: in-process memory, resets on restart and is per worker; use Redis/FalkorDB for multi-instance."""

    def __init__(self, limit: int, window_s: float):
        self.limit, self.window = limit, window_s
        self.hits: Dict[str, Deque[float]] = defaultdict(deque)

    def retry_after(self, key: str, now: Optional[float] = None) -> int:
        """Record a hit and return 0, or return seconds to wait (and do not record)."""
        now = time.time() if now is None else now
        if len(self.hits) > 5000:  # drop idle keys so many distinct callers cannot grow memory forever
            for k in [k for k, v in self.hits.items() if not v or v[-1] <= now - self.window]:
                del self.hits[k]
        q = self.hits[key]
        while q and q[0] <= now - self.window:
            q.popleft()
        if len(q) >= self.limit:
            return int(q[0] + self.window - now) + 1
        q.append(now)
        return 0


new_ideas = RateLimiter(3, 3600)  # 3 new idea analyses per user per hour (each one costs web searches + LLM calls)
messages = RateLimiter(20, 600)   # 20 messages per user per 10 minutes


if __name__ == "__main__":
    assert check_message("meal planner for parents", True) is None
    assert check_message("go cheaper", False) is None
    assert check_message("hi", True)
    assert check_message("<script>alert(1)</script>", False)
    assert check_message("this is f*ck shit idea", False)
    assert check_message("sh1t app for kids", False)
    rl = RateLimiter(3, 60)
    assert [rl.retry_after("a", 0) for _ in range(4)][-1] > 0 and rl.retry_after("a", 61) == 0
    print("ok")
