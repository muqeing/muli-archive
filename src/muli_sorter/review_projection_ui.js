/* Expand identity stubs for complete draft/decision coverage, never file evidence. */
(function () {
  "use strict";
  window.muliExpandReviewPresentation = function (model, materials) {
    var view = model.presentation;
    if (!view) return;
    if (view.version !== "pending-detail/1" || !Array.isArray(view.hidden_units) || !Array.isArray(model.units)) throw new Error("页面索引格式无效");
    var seen = Object.create(null);
    model.units.forEach(function (u) { if (!u.unit_id || seen[u.unit_id]) throw new Error("页面索引重复"); seen[u.unit_id] = true; });
    if (materials && (!Array.isArray(materials.hidden_roles) || materials.hidden_roles.length !== view.hidden_units.length)) throw new Error("页面来源索引无效");
    view.hidden_units.forEach(function (row, index) {
      if (!Array.isArray(row) || row.length !== 5 || typeof row[0] !== "string" || seen[row[0]] || !Array.isArray(row[4])) throw new Error("页面索引重复或无效");
      seen[row[0]] = true;
      model.units.push({unit_id:row[0],kind:row[1],capture_date:row[2],capture_time:row[3],candidate_project_ids:row[4],files:[],_identityOnly:true});
      if (materials) {
        var role = materials.hidden_roles[index];
        if (!Array.isArray(role) || role.length !== 2 || ["shoot","support","exception","companion","discarded"].indexOf(role[0]) < 0 ||
            (role[1] !== null && typeof role[1] !== "string")) throw new Error("页面来源角色无效");
        materials.units[row[0]] = {category:role[0],parent_unit_id:role[1]};
      }
    });
    if (model.units.length !== view.total_units) throw new Error("页面索引范围不完整");
  };
})();
