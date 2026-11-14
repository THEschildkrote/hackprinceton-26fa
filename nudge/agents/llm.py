"""one place to pick models. no model credits at the hackathon, so the default is openrouter's free tier
(langchain's track guidance), and claude when an anthropic key is present.

  NUDGE_MODEL         lead / text agents     (any init_chat_model spec, e.g. openrouter:<id> or anthropic:<id>)
  NUDGE_VISION_MODEL  subagents that look()  (must accept images and tool calls)
"""
import os

from dotenv import load_dotenv
from langchain.chat_models import init_chat_model

from ..mp import ROOT

load_dotenv(ROOT / ".env")

# free on openrouter, with tool calling; the vision ones also take images (checked against /api/v1/models)
FREE_TEXT = "openrouter:nvidia/nemotron-3-ultra-550b-a55b:free"
FREE_VISION = "openrouter:nvidia/nemotron-3.5-super-vl:free"
CLAUDE = "anthropic:claude-opus-5-5"


def _default(role):
    if os.environ.get("ANTHROPIC_API_KEY"):
        return CLAUDE
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
    return True


def model(s=None, role="text", **kw):
    s = s or spec(role)
    if s.startswith("openrouter:"):
        kw.setdefault("app_url", "https://github.com/arkankau/hackprinceton-26fa")
        kw.setdefault("max_retries", 4)  # free tier rate-limits
    return init_chat_model(s, **kw)
