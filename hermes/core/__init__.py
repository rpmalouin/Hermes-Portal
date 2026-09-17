"""Core building blocks of the Hermes-Dashboard skill framework.

Re-exports the public API so callers can write::

    from hermes.core import Runtime, Skill, SkillRegistry, load_skills, run_skill

Loader-style components (:func:`load_skills`, :class:`Runtime`) collect per-item
errors instead of raising, so one bad skill directory cannot break startup.
"""

from __future__ import annotations

from .executor import build_argv, run_skill
from .loader import load_skills
from .models import Skill, SkillResult
from .registry import SkillRegistry
from .runtime import Runtime, load_profile

__all__ = [
    "Runtime",
    "Skill",
    "SkillRegistry",
    "SkillResult",
    "build_argv",
    "load_profile",
    "load_skills",
    "run_skill",
]
