"""one place to pick models. no model credits at the hackathon, so the order is:
claude if ANTHROPIC_API_KEY is set, gemini if GEMINI_API_KEY is set, else openrouter's free tier
(langchain's track guidance).

  NUDGE_MODEL         lead / text agents     (any init_chat_model spec, e.g. google_genai:<id>, openrouter:<id>)
  NUDGE_VISION_MODEL  subagents that look()  (must accept images and tool calls)
"""
import os

from dotenv import load_dotenv
from langchain.chat_models import init_chat_model

from ..mp import ROOT

load_dotenv(ROOT / ".env")
# langchain's gemini integration reads GOOGLE_API_KEY
if os.environ.get("GEMINI_API_KEY") and not os.environ.get("GOOGLE_API_KEY"):
    os.environ["GOOGLE_API_KEY"] = os.environ["GEMINI_API_KEY"]

# openrouter's free router picks a free model that supports what the request needs (tools, images).
# it works with default account privacy settings, but is slow and sometimes routes images to text models.
FREE_TEXT = "openrouter:openrouter/free"
FREE_VISION = "openrouter:openrouter/free"
CLAUDE = "anthropic:claude-opus-5-5"
# gemini 3.5 flash-lite: tool calls ~1 s, images ~2 s, and roomier rate limits than gemini-4-hydrogen,
# which hit 429 when the lead started several subagents at once (checked live on the venue)
GEMINI = "google_genai:gemini-3.5-flash-lite"


def _default(role):
    if os.environ.get("ANTHROPIC_API_KEY"):
        return CLAUDE
    if os.environ.get("GOOGLE_API_KEY"):
        return GEMINI
    return FREE_VISION if role == "vision" else FREE_TEXT


def spec(role="text"):
    env = "NUDGE_VISION_MODEL" if role == "vision" else "NUDGE_MODEL"
    return os.environ.get(env) or _default(role)


def has_credentials(s=None):
    s = s or spec()
    if s.startswith("anthropic:"):
        return bool(os.environ.get("ANTHROPIC_API_KEY"))
    if s.startswith("openrouter:"):
        return bool(os.environ.get("OPENROUTER_API_KEY"))
    if s.startswith("google_genai:"):
        return bool(os.environ.get("GOOGLE_API_KEY"))
    return True


def model(s=None, role="text", **kw):
    s = s or spec(role)
    if s.startswith("openrouter:"):
        kw.setdefault("app_url", "https://github.com/arkankau/hackprinceton-26fa")
        # the free router sometimes lands on a slow model; fail fast and let the agent move on
        # (a real run saw one call take 23 min with 4 retries x 180 s)
        kw.setdefault("max_retries", 2)  # the sdk backs off on 429s
        kw.setdefault("timeout", 120_000)  # ms
        kw.setdefault("max_tokens", 8000)  # caps rambling; a full places file is ~1.5k tokens
    elif s.startswith("google_genai:"):
        kw.setdefault("timeout", 120)  # seconds
        kw.setdefault("max_retries", 6)  # backs off on 429s
        # the lead and every subagent share one limiter: parallel subagents queue instead of tripping the
        # trial key's per-minute quota (a real run hit 429 RESOURCE_EXHAUSTED after 5 parallel calls)
        kw.setdefault("rate_limiter", _gemini_limiter())
    return init_chat_model(s, **kw)


_LIMITER = None


def _gemini_limiter():
    global _LIMITER
    if _LIMITER is None:
        from langchain_core.rate_limiters import InMemoryRateLimiter
        rps = float(os.environ.get("NUDGE_GEMINI_RPS", "0.2"))  # 12 calls a minute
        _LIMITER = InMemoryRateLimiter(requests_per_second=rps, check_every_n_seconds=0.1, max_bucket_size=2)
    return _LIMITER
