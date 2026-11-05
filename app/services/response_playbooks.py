"""Small, versioned coordination checklists for verified incidents.

These templates are workflow scaffolding, not emergency SOPs or operational advice.
They never call authorities, publish warnings, dispatch resources, or perform field actions.
"""
from __future__ import annotations

from copy import deepcopy

PLAYBOOK_VERSION = 1
SUPPORTED_TYPES = ("flood", "earthquake", "fire", "cyclone", "landslide")


def _build_template(disaster_type: str) -> dict:
    label = disaster_type.replace("_", " ").title()
    return {
        "template_key": f"{disaster_type}_coordination_v1",
        "template_version": PLAYBOOK_VERSION,
        "disaster_type": disaster_type,
        "title": f"{label} incident coordination plan",
        "description": (
            "A coordination checklist for documenting the shared situation, evidence, "
            "constraints, resource requests and draft public information. It is not an "
            "official emergency procedure and must be adapted by authorized personnel."
        ),
        "policy": {
            "human_review_required": True,
            "external_side_effects": False,
            "official_sop": False,
            "approval_gated_action_types": ["public_warning", "evacuation", "resource_dispatch", "medical_intervention"],
        },
        "steps": [
            {
                "step_key": "situation_picture",
                "title": "Establish a shared situation picture",
                "description": (
                    "Consolidate currently verified incident reports, observed timestamps, "
                    "locations and source provenance into the incident record. Note material "
                    "uncertainty rather than treating missing information as confirmed."
                ),
                "action_type": "monitoring",
                "priority": "urgent",
                "depends_on": [],
            },
            {
                "step_key": "evidence_coverage",
                "title": f"Review {label.lower()} evidence coverage and gaps",
                "description": (
                    "Review supporting reports and available evidence. Record which areas, "
                    "time periods or source types are not represented; do not infer absence "
                    "of impact from absence of reports."
                ),
                "action_type": "monitoring",
                "priority": "high",
                "depends_on": ["situation_picture"],
            },
            {
                "step_key": "impact_constraints",
                "title": "Record reported impact locations and access constraints",
                "description": (
                    "Document only reported or independently verified affected locations, "
                    "known access constraints and the source/time supporting each entry."
                ),
                "action_type": "logistics",
                "priority": "high",
                "depends_on": ["situation_picture"],
            },
            {
                "step_key": "resource_requests",
                "title": "Coordinate documented resource requests",
                "description": (
                    "Maintain a traceable record of requests, requesting organization, "
                    "status and responsible contact. This task does not allocate or dispatch resources."
                ),
                "action_type": "logistics",
                "priority": "normal",
                "depends_on": ["impact_constraints"],
            },
            {
                "step_key": "public_information_review",
                "title": "Prepare public information for authorized review",
                "description": (
                    "Prepare a factual draft with source attribution, geographic scope, "
                    "issue time and uncertainty for review by the competent authority. "
                    "This platform does not publish or issue the warning."
                ),
                "action_type": "public_warning",
                "priority": "high",
                "depends_on": ["evidence_coverage", "impact_constraints"],
            },
        ],
    }


PLAYBOOKS = {f"{kind}_coordination_v1": _build_template(kind) for kind in SUPPORTED_TYPES}


def validate_dependency_graph(steps: list[dict]) -> list[str]:
    """Validate unique step keys, references and acyclicity; return topological order."""
    keys = [str(item.get("step_key") or "") for item in steps]
    if not keys or any(not key for key in keys) or len(keys) != len(set(keys)):
        raise ValueError("Playbook steps require unique, non-empty step_key values")
    known = set(keys)
    outgoing = {key: set() for key in keys}
    indegree = {key: 0 for key in keys}
    for step in steps:
        key = step["step_key"]
        for dependency in step.get("depends_on", []):
            if dependency not in known or dependency == key:
                raise ValueError(f"Invalid dependency {dependency!r} for step {key!r}")
            if key not in outgoing[dependency]:
                outgoing[dependency].add(key)
                indegree[key] += 1
    ready = sorted(key for key, degree in indegree.items() if degree == 0)
    ordered = []
    while ready:
        current = ready.pop(0)
        ordered.append(current)
        for neighbor in sorted(outgoing[current]):
            indegree[neighbor] -= 1
            if indegree[neighbor] == 0:
                ready.append(neighbor)
                ready.sort()
    if len(ordered) != len(keys):
        raise ValueError("Playbook dependency graph contains a cycle")
    return ordered


for _template in PLAYBOOKS.values():
    validate_dependency_graph(_template["steps"])


def list_playbooks(disaster_type: str | None = None) -> list[dict]:
    """Return safe catalogue metadata and immutable step definitions."""
    templates = list(PLAYBOOKS.values())
    if disaster_type:
        normalized = disaster_type.strip().lower()
        templates = [item for item in templates if item["disaster_type"] == normalized]
    return [deepcopy(item) for item in templates]


def get_playbook(template_key: str) -> dict | None:
    template = PLAYBOOKS.get(template_key)
    return deepcopy(template) if template is not None else None
