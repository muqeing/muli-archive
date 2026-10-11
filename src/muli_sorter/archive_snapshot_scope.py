"""Prove an opened page's selected scope still exists in the current model.

Never rewrite a user's plan or pull new units into it. This is only a model
comparison; the normal live source/target checks still follow in preflight.
"""
from .archive_diagnostics import diagnostic_context
from .archive_options import options_for
from .material_triage import companion_links
from .review import compile_plan, validate_model


def _unit_evidence(unit):
    # Candidate additions outside the chosen project are presentation changes.
    # Everything describing the actual unit and its provenance remains exact.
    return {k: v for k, v in unit.items() if k != 'candidate_project_ids'}


def _project_evidence(project):
    result = dict(project)
    if isinstance(result.get('order_evidence'), dict):
        # Re-fetching the same live order changes its observation time only.
        result['order_evidence'] = {k: v for k, v in result['order_evidence'].items()
                                    if k != 'fetched_at'}
    return result


def validate_selected_snapshot(opened, current, decisions):
    old_units, old_projects = validate_model(opened)
    new_units, new_projects = validate_model(current)
    compiled = compile_plan(opened, {k: v for k, v in decisions.items()
                                   if k != 'archive_options'})
    if opened['example_data'] != current['example_data']:
        raise ValueError('页面清单与当前服务的运行模式不一致')
    approved = options_for(decisions).get('proxy_only_unit_ids', ())
    old_links, _ = companion_links(opened, approved_proxy_ids=approved)
    new_links, _ = companion_links(current, approved_proxy_ids=approved)
    manual = {p['project_id']: p for p in decisions.get('manual_projects', ())}
    for assignment in compiled['assignments']:
        pid = assignment['project']['project_id']
        with diagnostic_context(segment_id=assignment['segment_id'], project_id=pid):
            if pid not in manual:
                if (pid not in new_projects or
                        _project_evidence(old_projects[pid]) != _project_evidence(new_projects[pid])):
                    raise ValueError('本次选中的项目资料已变化或已不在当前清单，请重新核对该拍摄段归属')
            scope = set(assignment['unit_ids'])
            if pid in manual:
                scope.update(manual[pid]['unit_ids'])
            for uid in assignment['unit_ids']:
                with diagnostic_context(unit_id=uid):
                    if old_links.get(uid) != new_links.get(uid):
                        raise ValueError('本次选中素材的主文件或附属关系已变化，请重新核对该拍摄段')
                if old_links.get(uid):
                    scope.add(old_links[uid])
            for uid in sorted(scope):
                with diagnostic_context(unit_id=uid):
                    if uid not in new_units:
                        raise ValueError('本次选中素材或必要依赖已不在当前清单，请刷新后核对该拍摄段')
                    if _unit_evidence(old_units[uid]) != _unit_evidence(new_units[uid]):
                        raise ValueError('本次选中素材或必要依赖的清单记录已变化，请重新核对该拍摄段')
            if pid not in manual:
                # Match the segment-wide candidate rule used by ArchiveJobs.
                candidates = {p for uid in assignment['unit_ids']
                              for p in new_units[uid].get('candidate_project_ids', ())}
                if any(uid not in old_links for uid in assignment['unit_ids']) and pid not in candidates:
                    raise ValueError('所选项目已不属于该拍摄段的当前候选，请重新选择')
    return compiled
