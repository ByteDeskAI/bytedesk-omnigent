"""Agent entity."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from omnigent.entities.automation import AgentCategory, Automation
from omnigent.spec import AgentSpec


@dataclass
class Agent(Automation):
    """
    A registered employee agent — the default tier (satisfies
    :class:`~omnigent.entities.automation.AgentRole`).

    The fields live on :class:`~omnigent.entities.automation.Automation`; this
    subclass adds the ``employee`` classification and keeps the ``Agent`` name
    every caller already imports. ``SystemAgent`` / ``Workflow`` are its siblings
    (see :mod:`omnigent.entities.automation`).
    """

    @property
    def category(self) -> AgentCategory:
        return "employee"


@dataclass
class LoadedAgent:
    """
    A fully loaded agent — parsed spec plus the extracted working
    directory on disk. Returned by ``AgentCache.load()``.

    :param spec: The parsed agent spec from config.yaml.
    :param workdir: Path to the extracted agent image directory on disk.
    """

    spec: AgentSpec
    workdir: Path
