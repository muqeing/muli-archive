"""Refresh directory candidates from verified metadata, without reading media."""
from .review import identity_digest


def refresh(model, projects):
    # Discovery uses no device/time bindings. A date is only a candidate, never
    # an automatic project assignment. Keep the grouping and file evidence intact.
    dates = {u.get('capture_date') for u in model['units']} - {None}
    scoped = [p for p in projects if dates.intersection(p.get('dates', []))]
    units = []
    changed = model['projects'] != scoped
    for unit in model['units']:
        ids = [p['project_id'] for p in scoped if unit.get('capture_date') in p.get('dates', [])]
        if ids != unit.get('candidate_project_ids', []):
            unit = {**unit, 'candidate_project_ids': ids}
            changed = True
        units.append(unit)
    if not changed:
        return model
    updated = {**model, 'projects': scoped, 'units': units}
    updated['report_id'] = identity_digest(updated)
    return updated
