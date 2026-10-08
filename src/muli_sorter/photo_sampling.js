/* Six distinct photos distributed across capture time; used again after split/merge. */
(function (root) {
  "use strict";
  function samplePhotos(units) {
    var photos = units.filter(function (u) { return u.kind === "photo"; });
    var timed = photos.every(function (u) { return u.capture_time && Number.isFinite(Date.parse(u.capture_time)); });
    if (timed) photos.sort(function (a, b) { return Date.parse(a.capture_time) - Date.parse(b.capture_time) || String(a.unit_id).localeCompare(String(b.unit_id)); });
    var count = Math.min(6, photos.length);
    if (count === photos.length) return { units: photos, timed: timed, total: photos.length };
    var times = photos.map(function (u) { return Date.parse(u.capture_time); });
    var start = times[0], span = times[times.length - 1] - start;
    var chosen = [], previous = -1;
    for (var i = 0; i < count; i += 1) {
      var target = timed && span > 0 ? start + span * i / (count - 1) : (photos.length - 1) * i / (count - 1);
      var best = previous + 1, distance = Infinity;
      // Reserve enough later photos so sparse times never repeat a picture.
      for (var j = previous + 1; j <= photos.length - count + i; j += 1) {
        var delta = Math.abs((timed && span > 0 ? times[j] : j) - target);
        if (delta < distance) { best = j; distance = delta; }
      }
      chosen.push(photos[best]); previous = best;
    }
    return { units: chosen, timed: timed, total: photos.length };
  }
  if (typeof module !== "undefined" && module.exports) module.exports = samplePhotos;
  else root.muliSamplePhotos = samplePhotos;
}(typeof window !== "undefined" ? window : this));
