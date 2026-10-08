"""Build a small, immutable request input from a prepared archive scope.

The review model is allowed to be large because it is a useful page snapshot.
The durable archive request only needs the units and decisions which the
already prepared execution will compile.  This module keeps that projection
separate from the job implementation so the original page snapshot remains
available for its normal freshness checks.
"""
from copy import deepcopy

from .material_triage import companion_links
from .order_feed_io import digest
from .review import identity_digest, validate_decisions, validate_model
from .archive_options import options_for


_SEGMENT_FIELDS = {
    "segment_id", "label", "unit_ids", "project_id", "decision",
    "acknowledge_date_mismatch",
}


def _decision_for_validation(decisions):
    """Return the review-plan part of decisions without archive policy."""
    return {key: value for key, value in decisions.items()
            if key != "archive_options"}


def _prepared_unit_ids(prepared, known_ids):
    selected = prepared.get("selected", ())
    selected_ids = []
    selected_seen = set()
    for item in selected:
        if not isinstance(item, dict) or not isinstance(item.get("unit"), dict):
            raise ValueError("prepared selected scope is invalid")
        uid = item["unit"].get("unit_id")
        if not isinstance(uid, str) or uid not in known_ids:
            raise ValueError("prepared selected scope contains an unknown unit")
        if uid not in selected_seen:
            selected_ids.append(uid)
            selected_seen.add(uid)

    compiled = prepared.get("compiled") or {}
    compiled_ids = []
    compiled_seen = set()
    for assignment in compiled.get("assignments", ()):
        if not isinstance(assignment, dict) or not isinstance(assignment.get("unit_ids"), list):
            raise ValueError("prepared compiled scope is invalid")
        for uid in assignment["unit_ids"]:
            if not isinstance(uid, str) or uid not in known_ids:
                raise ValueError("prepared compiled scope contains an unknown unit")
            if uid not in compiled_seen:
                compiled_ids.append(uid)
                compiled_seen.add(uid)

    if selected_ids and compiled_ids and set(selected_ids) != set(compiled_ids):
        raise ValueError("prepared selected scope does not match compiled scope")
    result = selected_ids or compiled_ids
    if not result:
        raise ValueError("prepared selected scope is empty")
    return result


def _compact_initial_segments(model, scope_ids):
    result = []
    for original in model.get("initial_segments", ()):
        if not isinstance(original, dict) or not isinstance(original.get("unit_ids"), list):
            continue
        unit_ids = [uid for uid in original["unit_ids"] if uid in scope_ids]
        if not unit_ids:
            continue
        result.append({key: deepcopy(original[key]) for key in _SEGMENT_FIELDS
                       if key in original and key != "unit_ids"} | {"unit_ids": unit_ids})
    return result


def _dependency_segments(decisions, scope_ids, selected_ids):
    """Copy selected decisions and add deferred rows for required context."""
    result = []
    covered = set()
    used_segment_ids = set()
    selected_ids = set(selected_ids)
    for original in decisions["segments"]:
        members = [uid for uid in original["unit_ids"] if uid in scope_ids]
        if not members:
            continue
        if original["decision"] == "confirmed":
            # A prepared confirmed assignment is the authority for the
            # executable scope.  Silently trimming it would change the job.
            if set(members) - selected_ids:
                raise ValueError("prepared scope would shrink a confirmed segment")
            segment = deepcopy(original)
        else:
            segment = deepcopy(original)
            segment["unit_ids"] = members
            # Dependencies must never become executable assignments merely
            # because their parent was retained in the compact model.
            segment["decision"] = "deferred"
            segment["project_id"] = None
            segment["acknowledge_date_mismatch"] = False
        result.append(segment)
        used_segment_ids.add(segment["segment_id"])
        covered.update(members)

    for uid in sorted(set(scope_ids) - covered):
        segment_id = "scoped-dependency-" + digest(uid)[:20]
        while segment_id in used_segment_ids:
            segment_id += "-x"
        result.append({"segment_id": segment_id, "label": "归档所需依赖素材",
                       "unit_ids": [uid], "project_id": None,
                       "decision": "deferred", "acknowledge_date_mismatch": False})
        used_segment_ids.add(segment_id)
    return result


def scoped_request_inputs(model, decisions, prepared):
    """Return compact request model/decisions and an immutable binding summary.

    ``model``, ``decisions`` and ``prepared`` are never modified.  The returned
    decisions use the compact model's report ID; the binding retains the IDs
    needed to relate this request to the original confirmation page.
    """
    if not isinstance(model, dict) or not isinstance(decisions, dict) or not isinstance(prepared, dict):
        raise ValueError("archive request inputs are invalid")

    units, _ = validate_model(model)
    options_for(decisions)
    # Validate the complete decision object before projecting it.  The public
    # schema deliberately remains strict even though archive_options is an
    # execution policy consumed outside the review plan.
    validate_decisions(model, _decision_for_validation(decisions))

    selected_ids = _prepared_unit_ids(prepared, set(units))
    selected_id_set = set(selected_ids)
    confirmed_ids = {
        uid for segment in decisions["segments"]
        if segment["decision"] == "confirmed"
        for uid in segment["unit_ids"]
    }
    if selected_id_set != confirmed_ids:
        raise ValueError("prepared selected scope does not match confirmed decisions")

    links, _ = companion_links(model)
    scope_ids = set(selected_ids)
    prepared_selected = {
        item["unit"]["unit_id"]: item["unit"]
        for item in prepared.get("selected", ())
        if isinstance(item, dict) and isinstance(item.get("unit"), dict)
    }
    for uid in selected_ids:
        parent = links.get(uid)
        if parent:
            scope_ids.add(parent)
        parent_row = prepared_selected.get(uid, {}).get("_companion_parent")
        if isinstance(parent_row, dict) and isinstance(parent_row.get("unit_id"), str):
            if parent_row["unit_id"] in units:
                scope_ids.add(parent_row["unit_id"])

    selected_project_ids = {
        segment["project_id"] for segment in decisions["segments"]
        if segment["decision"] == "confirmed" and segment["unit_ids"]
        and set(segment["unit_ids"]) & selected_id_set
        and segment["project_id"] is not None
    }
    original_manual = decisions.get("manual_projects", [])
    manual_projects = []
    for item in original_manual:
        if item["project_id"] not in selected_project_ids:
            continue
        manual_projects.append(deepcopy(item))
        scope_ids.update(item["unit_ids"])

    if not scope_ids <= set(units):
        raise ValueError("archive dependency is outside the review model")

    scoped_model = {
        "schema_version": "0.2",
        "snapshot_at": deepcopy(model["snapshot_at"]),
        "example_data": deepcopy(model["example_data"]),
        # Project catalog entries are small and preserve project identity and
        # all target path/order evidence needed by the compiler.
        "projects": deepcopy(model["projects"]),
        "units": [deepcopy(unit) for unit in model["units"] if unit["unit_id"] in scope_ids],
        "initial_segments": _compact_initial_segments(model, scope_ids),
        # Excluded batches are page history, not execution dependencies.  They
        # can be large in a real model and are intentionally omitted.
        "excluded_batches": [],
    }
    scoped_model["report_id"] = identity_digest(scoped_model)

    scoped_decisions = {
        "schema_version": decisions["schema_version"],
        "mode": decisions["mode"],
        "media_write_authorized": decisions["media_write_authorized"],
        "report_id": scoped_model["report_id"],
        "created_at": decisions["created_at"],
        "segments": _dependency_segments(decisions, scope_ids, selected_ids),
    }
    if decisions["schema_version"] == "0.3":
        scoped_decisions["manual_projects"] = manual_projects
    if "archive_options" in decisions:
        scoped_decisions["archive_options"] = deepcopy(decisions["archive_options"])

    validate_model(scoped_model)
    validate_decisions(scoped_model, _decision_for_validation(scoped_decisions))

    summary = prepared.get("summary", {})
    summary = ({key: deepcopy(value) for key, value in summary.items()
                if isinstance(key, str) and isinstance(value, (str, int, float, bool))}
               if isinstance(summary, dict) else {})
    binding = {
        "report_id": model["report_id"],
        "decisions_digest": digest(decisions),
        "selected_unit_ids": sorted(selected_ids),
        "summary": summary,
    }
    return scoped_model, scoped_decisions, binding
