/* Groundtruth — community map.
 *
 * Pins use exactly the colours and glyphs from the result cards, so the
 * legend is the same object twice. The legend also *is* the filter, so the
 * colour key is never a separate thing to find.
 *
 * Empty and broken are deliberately different screens: an empty map is fully
 * drawn and visibly working; a failed one shows no map at all.
 */
(function () {
  "use strict";

  var CFG = window.GT_CONFIG;
  var API = window.GT_API;

  var el = function (id) { return document.getElementById(id); };
  var show = function (n) { n.classList.remove("gt-hidden"); };
  var hide = function (n) { n.classList.add("gt-hidden"); };

  function esc(v) {
    return String(v == null ? "" : v).replace(/[&<>"']/g, function (c) {
      return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c];
    });
  }

  var STATUS = {
    invasive:   { word: "Invasive",   here: "Invasive here",       color: "#a32a1e", icon: "i-alert" },
    introduced: { word: "Introduced", here: "Introduced here",     color: "#b0770b", icon: "i-eye" },
    native:     { word: "Native",     here: "Native here",         color: "#5b7c3a", icon: "i-check" },
    unknown:    { word: "Unknown",    here: "Not confirmed here",  color: "#6b6b74", icon: "i-help" }
  };
  var ORDER = ["invasive", "introduced", "native", "unknown"];
  function meta(status) { return STATUS[status] || STATUS.unknown; }

  var map = null;
  var layer = null;
  var sightings = [];
  var active = { invasive: true, introduced: true, native: true, unknown: true };
  var tileErrors = 0;
  var tilesOk = false;

  // ── Map ─────────────────────────────────────────────────────────────────
  function initMap() {
    map = L.map("map", {
      zoomControl: true,
      attributionControl: true,
      center: CFG.MAP_DEFAULT_CENTER,
      zoom: CFG.MAP_DEFAULT_ZOOM
    });
    map.zoomControl.setPosition("bottomright");

    var tiles = L.tileLayer("https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png", {
      maxZoom: 19,
      attribution: '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors'
    });

    tiles.on("tileload", function () { tilesOk = true; });
    // One failed tile is normal at the edge of the world; a wall of them is not.
    tiles.on("tileerror", function () {
      tileErrors += 1;
      if (tileErrors >= 6 && !tilesOk) showFailed();
    });
    tiles.addTo(map);

    layer = L.layerGroup().addTo(map);

    // If nothing has rendered after a reasonable wait, treat it as a failure
    // rather than leaving a blank rectangle that looks like an empty map.
    setTimeout(function () { if (!tilesOk) showFailed(); }, 9000);
  }

  function pinIcon(status) {
    var m = meta(status);
    return L.divIcon({
      className: "gt-pin-wrap",
      html: '<span class="gt-pin" style="background:' + m.color + '">' +
            '<span class="ico ' + m.icon + '"></span></span>',
      iconSize: [28, 28],
      iconAnchor: [14, 26]
    });
  }

  function draw() {
    if (!layer) return;
    layer.clearLayers();

    var visible = sightings.filter(function (s) { return active[s.status] !== false; });

    visible.forEach(function (s) {
      var lat = Number(s.latitude), lng = Number(s.longitude);
      if (!isFinite(lat) || !isFinite(lng)) return;
      L.marker([lat, lng], { icon: pinIcon(s.status), keyboard: true, title: s.common_name })
        .addTo(layer)
        .on("click", function () { openSheet(s); });
    });

    el("mapCount").textContent = visible.length === 1 ? "1 pin" : visible.length + " pins";

    // Empty only counts when nothing has been logged at all — a filter that
    // hides everything is the user's own doing, not an empty map.
    if (!sightings.length) { show(el("mapEmpty")); } else { hide(el("mapEmpty")); }
  }

  function fitToSightings() {
    if (!sightings.length || !map) return;
    var points = sightings
      .map(function (s) { return [Number(s.latitude), Number(s.longitude)]; })
      .filter(function (p) { return isFinite(p[0]) && isFinite(p[1]); });
    if (!points.length) return;
    map.fitBounds(L.latLngBounds(points), { padding: [60, 60], maxZoom: 12 });
  }

  // ── Legend, which is also the filter ────────────────────────────────────
  function buildLegend() {
    var wrap = el("legend");
    wrap.innerHTML = ORDER.map(function (key) {
      var m = STATUS[key];
      return '<button type="button" role="switch" aria-checked="true" data-status="' + key + '" ' +
        'class="inline-flex items-center gap-2 rounded-full pl-2 pr-3 py-1.5 text-[12.5px] font-bold border transition-opacity" ' +
        'style="background:' + m.color + '1a;border-color:' + m.color + '66;color:#201e1d">' +
        '<span class="w-3.5 h-3.5 rounded-full flex-none" style="background:' + m.color + '"></span>' +
        esc(m.word) + '</button>';
    }).join("");

    wrap.querySelectorAll("[data-status]").forEach(function (btn) {
      btn.addEventListener("click", function () {
        var key = btn.getAttribute("data-status");
        active[key] = !active[key];
        btn.setAttribute("aria-checked", String(active[key]));
        btn.style.opacity = active[key] ? "1" : "0.4";
        btn.style.textDecoration = active[key] ? "none" : "line-through";
        draw();
      });
    });
  }

  function buildRecent() {
    var list = el("recentList");
    if (!list) return;
    if (!sightings.length) {
      list.innerHTML = '<li class="text-[12.5px] text-mute-700">Nothing logged yet.</li>';
      return;
    }
    list.innerHTML = sightings.slice(0, 4).map(function (s) {
      return '<li><button type="button" data-id="' + esc(s.id) + '" class="w-full flex items-center gap-2.5 text-left">' +
        '<span class="w-2.5 h-2.5 rounded-full flex-none" style="background:' + meta(s.status).color + '"></span>' +
        '<span class="text-[13px] font-semibold truncate flex-1">' + esc(s.common_name) + '</span>' +
        '<span class="text-[11.5px] text-mute-700 flex-none">' + relativeTime(s.created_at) + '</span>' +
        '</button></li>';
    }).join("");

    list.querySelectorAll("[data-id]").forEach(function (btn) {
      btn.addEventListener("click", function () {
        var found = sightings.filter(function (s) { return String(s.id) === btn.getAttribute("data-id"); })[0];
        if (!found) return;
        map.setView([Number(found.latitude), Number(found.longitude)], 13);
        openSheet(found);
      });
    });
  }

  // ── Pin detail ──────────────────────────────────────────────────────────
  function openSheet(s) {
    var m = meta(s.status);
    el("sheetPin").style.background = m.color;
    el("sheetPin").querySelector(".ico").className = "ico " + m.icon;
    el("sheetStatus").textContent = m.here;
    el("sheetStatus").style.color = m.color;
    el("sheetName").textContent = s.common_name || "Unknown";
    el("sheetSci").textContent = s.scientific_name || "";
    el("sheetDate").textContent = fullDate(s.created_at);
    el("sheetCoords").textContent = Number(s.latitude).toFixed(4) + ", " + Number(s.longitude).toFixed(4);
    show(el("sheet"));
  }

  function relativeTime(iso) {
    var then = new Date(iso);
    if (isNaN(then.getTime())) return "";
    var mins = Math.round((Date.now() - then.getTime()) / 60000);
    if (mins < 60) return Math.max(1, mins) + "m ago";
    if (mins < 1440) return Math.round(mins / 60) + "h ago";
    var days = Math.round(mins / 1440);
    return days === 1 ? "Yesterday" : days + " days ago";
  }

  function fullDate(iso) {
    var d = new Date(iso);
    if (isNaN(d.getTime())) return "recently";
    return relativeTime(iso) + ", " +
      d.toLocaleDateString(undefined, { day: "numeric", month: "long", year: "numeric" });
  }

  // ── Failure states ──────────────────────────────────────────────────────
  function showFailed() {
    show(el("mapFailed"));
    hide(el("mapChrome"));
    hide(el("mapEmpty"));
    hide(el("sheet"));
  }

  function showRateLimited(seconds) {
    var remaining = Math.max(1, seconds || 60);
    hide(el("mapChrome"));
    hide(el("mapEmpty"));
    show(el("mapLimited"));
    el("mapRlNum").innerHTML = remaining + '<span class="text-[34px]">s</span>';
    var timer = setInterval(function () {
      remaining -= 1;
      if (remaining <= 0) { clearInterval(timer); window.location.reload(); return; }
      el("mapRlNum").innerHTML = remaining + '<span class="text-[34px]">s</span>';
    }, 1000);
  }

  // ── Load ────────────────────────────────────────────────────────────────
  function load() {
    API.listSightings().then(function (rows) {
      sightings = Array.isArray(rows) ? rows : [];
      draw();
      buildRecent();
      fitToSightings();
    }).catch(function (err) {
      if (err.kind === "ratelimit") { showRateLimited(err.retryAfter); return; }
      // The tiles may be fine while the API is not. Say so rather than
      // showing an empty map, which would read as "nothing logged".
      el("mapCount").textContent = "—";
      var list = el("recentList");
      if (list) list.innerHTML = '<li class="text-[12.5px] text-mute-700">Could not load sightings.</li>';
      var empty = el("mapEmpty");
      empty.querySelector("h2").textContent = "Could not load sightings";
      empty.querySelector("p").textContent =
        "The map is working, but we could not reach Groundtruth to fetch the pins. Nothing has been lost.";
      show(empty);
    });
  }

  // ── Wire up ─────────────────────────────────────────────────────────────
  document.addEventListener("DOMContentLoaded", function () {
    API.warmUp();
    initMap();
    buildLegend();
    load();

    el("sheetClose").addEventListener("click", function () { hide(el("sheet")); });
    map.on("click", function () { hide(el("sheet")); });

    el("mapRetry").addEventListener("click", function () { window.location.reload(); });

    el("zoomOut").addEventListener("click", function () {
      map.setView(CFG.MAP_DEFAULT_CENTER, CFG.MAP_DEFAULT_ZOOM);
    });

    var recentre = el("recentre");
    if (recentre) {
      recentre.addEventListener("click", function () {
        if (!navigator.geolocation) return;
        recentre.textContent = "Finding you…";
        navigator.geolocation.getCurrentPosition(function (pos) {
          map.setView([pos.coords.latitude, pos.coords.longitude], 12);
          recentre.innerHTML = '<span class="ico i-crosshair w-4 h-4" aria-hidden="true"></span>Recentre on me';
        }, function () {
          recentre.textContent = "Location unavailable";
        });
      });
    }
  });
})();
