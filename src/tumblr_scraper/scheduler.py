"""Pure scheduling policy shared by preservation and survey actions."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable

from .models import ActionCandidate, SurveyEnvelope


@dataclass
class SchedulerBudget:
    max_posts: int
    new_posts: int = 0
    survey_requests: int = 0
    observed_blogs: set[str] = field(default_factory=set)

    def can_acquire_post(self) -> bool:
        return self.new_posts < self.max_posts

    def record_post(self) -> None:
        if not self.can_acquire_post():
            raise RuntimeError("canonical post budget exhausted")
        self.new_posts += 1

    def can_survey(self, envelope: SurveyEnvelope, blog: str | None = None) -> bool:
        if self.survey_requests >= envelope.max_requests:
            return False
        if blog is not None and blog not in self.observed_blogs and len(self.observed_blogs) >= envelope.max_observed_blogs:
            return False
        return True

    def record_survey(self, envelope: SurveyEnvelope, blog: str | None = None) -> None:
        if not self.can_survey(envelope, blog):
            raise RuntimeError("survey envelope exhausted")
        self.survey_requests += 1
        if blog:
            self.observed_blogs.add(blog)


def nearest_target_distance(blog: str, discovery_distances: dict[str, dict[str, int]], targets: Iterable[str]) -> int | None:
    values = [discovery_distances[target][blog] for target in targets if target in discovery_distances and blog in discovery_distances[target]]
    return min(values) if values else None


def action_priority(candidate: ActionCandidate, *, nearest_distance: int | None = None) -> tuple[int, float, int, str]:
    """Lower tuple wins. Canonical preservation always precedes observation."""
    rank = {
        "target": 0, "preserve": 1, "repair": 2, "sample": 3,
        "scout": 4, "survey": 5, "enrichment": 6,
    }.get(candidate.kind, 7)
    distance = nearest_distance if nearest_distance is not None else candidate.graph_depth
    return rank, distance, -candidate.priority, candidate.identity


def ordered_actions(candidates: Iterable[ActionCandidate], *, distances: dict[str, int] | None = None) -> list[ActionCandidate]:
    distances = distances or {}
    return sorted(candidates, key=lambda item: action_priority(item, nearest_distance=distances.get(item.blog or "")))
