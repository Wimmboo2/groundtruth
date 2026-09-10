/* Groundtruth — identify page.
 *
 * Four views share one screen: upload, the wait, the result, and failure.
 * The rule that shapes all of them: status is the hero, and errors never
 * borrow a status colour, or red stops meaning invasive.
 */
(function () {
  "use strict";

  var CFG = window.GT_CONFIG;
  var API = window.GT_API;

  var el = function (id) { return document.getElementById(id); };
  var show = function (node) { node.classList.remove("gt-hidden"); };
  var hide = function (node) { node.classList.add("gt-hidden"); };

  /* Everything interpolated into innerHTML goes through this. Species names
     and explainer text come from a model and three third-party APIs. */
  function esc(value) {
    return String(value == null ? "" : value).replace(/[&<>"']/g, function (c) {
      return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c];
    });
  }

  // ── State ───────────────────────────────────────────────────────────────
  var state = {
    file: null,
    previewUrl: null,
    coords: null,          // {lat, lng} once granted
    locationOn: false,
    result: null,
    logged: false,
    retryCount: 0
  };

  // ── Status vocabulary ───────────────────────────────────────────────────
  // Colour is never alone: every status carries its own glyph and its own word.
  var STATUS = {
    invasive: {
      word: "Invasive", prep: "in", icon: "i-alert",
      band: "bg-invasive", bandText: "text-white", bandSub: "text-invasive-tint",
      lede: "A recognised problem species here, and it is worth doing something about.",
      whyLabel: "Why it matters", doLabel: "What to do", logStyle: "solid"
    },
    introduced: {
      word: "Introduced", prep: "in", icon: "i-eye",
      band: "bg-introduced", bandText: "text-white", bandSub: "text-introduced-tint",
      lede: "Not native here, and not on any problem list. Nothing to fix.",
      whyLabel: "What this means", doLabel: "Worth knowing", logStyle: "outline"
    },
    native: {
      word: "Native", prep: "to", icon: "i-check",
      band: "bg-native", bandText: "text-white", bandSub: "text-native-tint",
      lede: "This one belongs here. Nothing to do, nothing to report.",
      whyLabel: "Where it fits", doLabel: "Good to know", logStyle: "none"
    },
    unknown: {
      word: "Not confirmed", prep: "for", icon: "i-help",
      band: "bg-unknown", bandText: "text-white", bandSub: "text-unknown-tint",
      lede: "We can name it. We cannot honestly say whether it belongs here.",
      whyLabel: "Why we are unsure", doLabel: "What would help", logStyle: "none"
    }
  };

  function statusMeta(status) { return STATUS[status] || STATUS.unknown; }

  function sourceLabel(source) {
    if (source === "plantnet") return "Pl@ntNet";
    if (source === "gemini") return "backup vision model";
    return "";
  }

  function formatBytes(bytes) {
    if (bytes >= 1024 * 1024) return (bytes / (1024 * 1024)).toFixed(1) + " MB";
    return Math.max(1, Math.round(bytes / 1024)) + " KB";
  }

  // ── View switching ──────────────────────────────────────────────────────
  var views = {};
  function switchTo(name) {
    Object.keys(views).forEach(function (key) {
      if (key === name) { show(views[key]); } else { hide(views[key]); }
    });
    window.scrollTo({ top: 0, behavior: "smooth" });
  }

  // ── Location ────────────────────────────────────────────────────────────
  function setLocationUI(on, label) {
    state.locationOn = on;
    el("locToggle").setAttribute("aria-checked", String(on));
    el("locToggle").className =
      "w-[46px] h-[27px] rounded-full relative flex-none transition-colors " +
      (on ? "bg-clay" : "bg-mute-500");
    el("locKnob").style.transform = on ? "translateX(19px)" : "none";

    el("locChipIcon").className = "ico w-[13px] h-[13px] " + (on ? "i-crosshair text-clay-600" : "i-locate-off");
    el("locChipText").textContent = on ? (label || "Location on") : "No location";
    el("locRowIcon").className = "ico w-5 h-5 " + (on ? "i-crosshair text-clay-600" : "i-locate-off text-mute-700");
    el("locRowSub").textContent = on
      ? (label || "Using your coordinates")
      : "Makes the verdict specific to where you are standing";

    if (on) { hide(el("locDenied")); }
    updatePhotoCoords();
  }

  function updatePhotoCoords() {
    var node = el("photoCoords");
    if (!node) return;
    if (state.coords) {
      node.textContent = state.coords.lat.toFixed(4) + ", " + state.coords.lng.toFixed(4);
    } else {
      node.textContent = "No location — the check will cover the whole United States";
    }
  }

  function requestLocation() {
    if (!navigator.geolocation) {
      state.coords = null;
      setLocationUI(false);
      show(el("locDenied"));
      return;
    }
    el("locRowSub").textContent = "Finding you…";
    navigator.geolocation.getCurrentPosition(
      function (pos) {
        state.coords = { lat: pos.coords.latitude, lng: pos.coords.longitude };
        setLocationUI(true, state.coords.lat.toFixed(3) + ", " + state.coords.lng.toFixed(3));
      },
      function () {
        // Denied or unavailable. Not an error, and nothing is blocked.
        state.coords = null;
        setLocationUI(false);
        show(el("locDenied"));
      },
      { enableHighAccuracy: true, timeout: 10000, maximumAge: 300000 }
    );
  }

  // ── Upload ──────────────────────────────────────────────────────────────
  function showUploadError(title, body, hint) {
    el("uploadErrorTitle").textContent = title;
    el("uploadErrorBody").innerHTML = body;
    el("uploadErrorHint").textContent = hint || "";
    show(el("uploadError"));
    hide(el("photoCard"));
    el("shutterLabel").textContent = "Try another photo";
  }

  function clearUploadError() {
    hide(el("uploadError"));
    el("shutterLabel").textContent = "Take a photo";
  }

  function acceptFile(file) {
    if (!file) return;

    // Checked in the browser first, so nobody waits out a 14 MB round trip
    // just to be told no.
    var type = (file.type || "").toLowerCase();
    if (CFG.ALLOWED_TYPES.indexOf(type) === -1) {
      showUploadError(
        "That file is not a photo",
        "Groundtruth reads JPEG, PNG and WebP. You picked <strong>" + esc(file.name) + "</strong>.",
        "Screenshots and downloads are usually PNG, so they work fine."
      );
      return;
    }
    if (file.size > CFG.MAX_UPLOAD_BYTES) {
      showUploadError(
        "That photo is too large",
        "Photos need to be under 10&nbsp;MB. This one is <strong>" + formatBytes(file.size) + "</strong>.",
        "Sharing from your camera roll usually sends a smaller copy than the original file."
      );
      return;
    }

    clearUploadError();
    if (state.previewUrl) URL.revokeObjectURL(state.previewUrl);
    state.file = file;
    state.previewUrl = URL.createObjectURL(file);

    el("photoThumb").src = state.previewUrl;
    el("photoName").textContent = file.name || "photo.jpg";
    el("photoSize").textContent = formatBytes(file.size);
    updatePhotoCoords();
    show(el("photoCard"));
    el("identifyBtn").focus();
  }

  // ── The wait ────────────────────────────────────────────────────────────
  var waitTimers = [];
  function clearWaitTimers() {
    waitTimers.forEach(clearInterval);
    waitTimers.forEach(clearTimeout);
    waitTimers = [];
  }

  function setStep(index, mode, detail) {
    var li = document.querySelector('#waitSteps [data-step="' + index + '"]');
    if (!li) return;
    var dot = li.querySelector("[data-dot]");
    var det = li.querySelector("[data-detail]");

    if (mode === "done") {
      dot.className = "w-[26px] h-[26px] rounded-full flex-none flex items-center justify-center bg-clay text-paper";
      dot.innerHTML = '<span class="ico i-check w-3.5 h-3.5" aria-hidden="true"></span>';
    } else if (mode === "active") {
      dot.className = "w-[26px] h-[26px] rounded-full flex-none flex items-center justify-center text-[12px] font-bold bg-clay-200 text-clay-700";
      dot.textContent = String(index + 1);
    } else {
      dot.className = "w-[26px] h-[26px] rounded-full flex-none flex items-center justify-center text-[12px] font-bold bg-mute-300 text-mute-700";
      dot.textContent = String(index + 1);
    }
    if (detail !== undefined) det.textContent = detail || " ";
  }

  function startWait() {
    switchTo("loading");
    hide(el("coldStart"));
    el("waitTitle").textContent = "Working on it";
    el("waitSub").textContent = "Most answers take 5 to 15 seconds.";
    [0, 1, 2].forEach(function (i) { setStep(i, "idle", ""); });
    setStep(0, "active", "Sending your photo…");

    var started = Date.now();
    clearWaitTimers();

    waitTimers.push(setInterval(function () {
      var secs = Math.floor((Date.now() - started) / 1000);
      el("waitElapsed").textContent = secs + "s";
    }, 250));

    // The steps advance on a timer because the backend answers in one call.
    // Real returned values replace these the moment the response lands.
    waitTimers.push(setTimeout(function () { setStep(0, "done"); setStep(1, "active", "Matching your coordinates to a place"); }, 3500));
    waitTimers.push(setTimeout(function () { setStep(1, "done"); setStep(2, "active", "Native range, then the state problem list"); }, 7000));

    // At ten seconds, not at zero.
    waitTimers.push(setTimeout(function () {
      el("waitTitle").textContent = "Still working";
      el("waitSub").textContent = "Your photo is safe. Nothing is lost.";
      show(el("coldStart"));
      setStep(0, "active", "Waiting for the server to wake up");
    }, CFG.COLD_START_NOTICE_MS));
  }

  /* Fill the steps with what actually came back, hold for a beat so it can be
     read, then show the verdict. */
  function finishWait(result) {
    clearWaitTimers();
    var place = result.place_name || "the United States";
    setStep(0, "done", sourceLabel(result.id_source)
      ? sourceLabel(result.id_source) + " matched: " + result.scientific_name
      : result.scientific_name);
    setStep(1, "done", place);
    setStep(2, "done", result.establishment_means
      ? "Recorded as " + result.establishment_means + " here"
      : "Checked against the problem list");
    return new Promise(function (resolve) { setTimeout(resolve, 650); });
  }

  // ── Result card ─────────────────────────────────────────────────────────
  function confidenceBlock(result) {
    var pct = Math.round((result.confidence || 0) * 100);
    var filled = Math.min(5, Math.max(1, Math.round((result.confidence || 0) * 5)));
    var low = (result.confidence || 0) < 0.6;
    var segs = "";
    for (var i = 0; i < 5; i++) {
      segs += '<span class="w-[22px] h-[9px] rounded-full ' +
        (i < filled ? (low ? "bg-mute-600" : "bg-clay-700") : "bg-mute-300") + '"></span>';
    }
    var src = sourceLabel(result.id_source);
    return '' +
      '<div class="flex items-center gap-3.5 mt-3.5 pt-3 border-t border-ink/10">' +
        '<div class="flex-none">' +
          '<div class="flex gap-[5px] mb-1.5">' + segs + '</div>' +
          '<div class="text-[12.5px] font-bold ' + (low ? "text-unknown-ink" : "") + '">' +
            (low ? "Low confidence" : "Confident match") + '</div>' +
        '</div>' +
        '<div class="text-[11.5px] text-mute-700 font-semibold leading-[1.45] text-right flex-1">' +
          pct + '%' + (src ? '<br>' + esc(src) : '') +
        '</div>' +
      '</div>' +
      (low
        ? '<p class="mt-2.5 text-[12.5px] leading-[1.5] text-unknown-ink bg-unknown-tint border border-unknown-line rounded-gt px-3 py-2">Treat the name as a guess.</p>'
        : '');
  }

  function actionsBlock(result, meta) {
    var actions = (result.explainer && result.explainer.what_to_do) || [];
    if (!actions.length) return "";

    // Native gets a rounded sage panel with checks, not a numbered task list.
    // It is an answer, not an alert.
    if (result.status === "native") {
      var items = actions.map(function (a) {
        return '<li class="flex gap-2.5"><span class="ico i-check w-4 h-4 text-native-ink mt-0.5 flex-none" aria-hidden="true"></span>' +
          '<span class="text-[13.5px] leading-[1.45]">' + esc(a) + '</span></li>';
      }).join("");
      return '' +
        '<div class="mt-5">' +
          '<div class="text-[12px] font-bold uppercase tracking-[.08em] text-mute-700 mb-2.5">' + meta.doLabel + '</div>' +
          '<ul class="bg-native-tint border border-native-line rounded-gt-lg p-4 space-y-2.5">' + items + '</ul>' +
        '</div>';
    }

    var visible = actions.slice(0, 3);
    var rest = actions.slice(3);
    var list = visible.map(function (a, i) {
      return '<div class="flex gap-3">' +
        '<span class="w-[23px] h-[23px] rounded-full bg-clay-200 text-clay-700 text-[12px] font-bold flex items-center justify-center flex-none">' + (i + 1) + '</span>' +
        '<span class="text-[13.5px] leading-[1.45]">' + esc(a) + '</span></div>';
    }).join("");
    var hidden = rest.map(function (a, i) {
      return '<div class="flex gap-3">' +
        '<span class="w-[23px] h-[23px] rounded-full bg-clay-200 text-clay-700 text-[12px] font-bold flex items-center justify-center flex-none">' + (i + 4) + '</span>' +
        '<span class="text-[13.5px] leading-[1.45]">' + esc(a) + '</span></div>';
    }).join("");

    // curated_match earns the sage chip, so people can tell vetted guidance
    // from a model improvising about herbicides.
    var chip = result.curated_match
      ? '<span class="inline-flex items-center gap-1.5 bg-native-tint border border-native-line text-native-ink rounded-full pl-2 pr-2.5 py-1 text-[11px] font-bold">' +
        '<span class="ico i-shield w-3.5 h-3.5" aria-hidden="true"></span>Vetted, not generated</span>'
      : "";

    return '' +
      '<div class="mt-5">' +
        '<div class="flex items-center justify-between gap-2.5 mb-2.5">' +
          '<span class="text-[12px] font-bold uppercase tracking-[.08em] text-mute-700">' + meta.doLabel + '</span>' + chip +
        '</div>' +
        '<div class="space-y-2.5">' + list + '</div>' +
        (rest.length
          ? '<div id="moreSteps" class="gt-hidden space-y-2.5 mt-2.5">' + hidden + '</div>' +
            '<button type="button" id="moreStepsBtn" class="flex items-center gap-1.5 mt-3 text-clay-700 font-display text-[14px]">' +
              (rest.length === 1 ? "One more step" : rest.length + " more steps") +
              '<span class="ico i-chev-d w-4 h-4" aria-hidden="true"></span></button>'
          : "") +
      '</div>';
  }

  function logButton(result, meta) {
    if (meta.logStyle === "none") {
      if (result.status === "native") {
        return '<p class="mt-6 font-display text-[17px] text-native-ink">Nothing to report. Enjoy the walk.</p>' +
          '<button type="button" data-restart class="mt-4 w-full h-[54px] rounded-full border border-ink/20 font-display text-[16px]">Identify something else</button>';
      }
      return '<button type="button" data-restart class="mt-6 w-full h-[54px] rounded-full bg-clay text-paper font-display text-[17px] shadow-gt-md">Try another photo</button>';
    }

    // No coordinates means no sighting: the table requires a point.
    if (!state.coords) {
      return '<div class="mt-6 rounded-gt bg-surface border border-mute-300 p-4">' +
          '<p class="text-[13px] leading-[1.5] text-mute-800">Turn on location to log this sighting. The map needs coordinates, and we do not have any for this photo.</p>' +
          '<button type="button" data-enable-loc class="mt-3 h-[42px] px-5 rounded-full border border-ink/20 font-display text-[14px]">Use my location</button>' +
        '</div>' +
        '<button type="button" data-restart class="mt-3 w-full h-[50px] rounded-full font-display text-[15px] text-mute-700">Identify something else</button>';
    }

    // Introduced gets an outlined button: logging is welcome, not urged.
    var solid = meta.logStyle === "solid";
    return '<button type="button" data-log class="mt-6 w-full h-[54px] rounded-full font-display text-[17px] flex items-center justify-center gap-2.5 ' +
        (solid ? "bg-clay text-paper shadow-gt-md" : "border border-ink/25 text-ink") + '">' +
        '<span class="ico i-map-pin w-[19px] h-[19px]" aria-hidden="true"></span>Log this sighting</button>' +
      '<button type="button" data-restart class="mt-3 w-full h-[50px] rounded-full font-display text-[15px] text-mute-700">Identify something else</button>';
  }

  function renderResult(result) {
    state.result = result;
    state.logged = false;

    var meta = statusMeta(result.status);
    var place = result.place_name || "the United States";
    var low = (result.confidence || 0) < 0.6;
    var summary = (result.explainer && result.explainer.summary) || "";

    var html = '' +
      // The band is the headline and it always names the place. Full bleed on
      // mobile, above the species name.
      '<div class="-mx-5 md:mx-0 md:rounded-t-gt-lg ' + meta.band + ' px-5 py-6 md:px-7 md:py-7">' +
        '<div class="flex items-start gap-3.5">' +
          '<span class="ico ' + meta.icon + ' w-7 h-7 ' + meta.bandText + ' mt-0.5 flex-none" aria-hidden="true"></span>' +
          '<div class="min-w-0">' +
            '<h1 class="font-display text-[26px] md:text-[40px] leading-[1.12] tracking-[-0.015em] ' + meta.bandText + '">' +
              esc(meta.word) + ' ' + meta.prep + ' ' + esc(place) + '</h1>' +
            '<p class="mt-2 text-[14px] md:text-[16px] leading-[1.5] ' + meta.bandSub + '">' + esc(meta.lede) + '</p>' +
          '</div>' +
        '</div>' +
      '</div>' +

      '<div class="pt-4 md:px-7 md:pb-7 md:bg-paper md:rounded-b-gt-lg md:shadow-gt-md">' +
        '<div class="flex items-center gap-3.5">' +
          (state.previewUrl
            ? '<img src="' + state.previewUrl + '" alt="" class="w-[62px] h-[62px] rounded-[18px] object-cover flex-none">'
            : '') +
          '<div class="min-w-0">' +
            '<div class="font-display ' + (low ? 'text-[20px]' : 'text-[21px] md:text-[26px]') + ' leading-[1.15]">' +
              esc(result.common_name || "Unknown") + '</div>' +
            (result.scientific_name
              ? '<div class="text-[13px] text-mute-700 italic mt-0.5">' + esc(result.scientific_name) + '</div>'
              : '') +
          '</div>' +
        '</div>' +

        confidenceBlock(result) +

        (summary
          ? '<div class="mt-5">' +
              '<div class="text-[12px] font-bold uppercase tracking-[.08em] text-mute-700 mb-2">' + meta.whyLabel + '</div>' +
              '<p class="text-[14px] md:text-[15px] leading-[1.55]">' + esc(summary) + '</p>' +
            '</div>'
          : '') +

        actionsBlock(result, meta) +
        logButton(result, meta) +
      '</div>';

    var view = views.result;
    view.innerHTML = html;
    switchTo("result");

    var moreBtn = el("moreStepsBtn");
    if (moreBtn) {
      moreBtn.addEventListener("click", function () {
        show(el("moreSteps"));
        moreBtn.remove();
      });
    }
    view.querySelectorAll("[data-restart]").forEach(function (b) {
      b.addEventListener("click", restart);
    });
    var logBtn = view.querySelector("[data-log]");
    if (logBtn) logBtn.addEventListener("click", function () { logSighting(logBtn); });
    var locBtn = view.querySelector("[data-enable-loc]");
    if (locBtn) locBtn.addEventListener("click", function () { restart(); requestLocation(); });
  }

  // ── Logging ─────────────────────────────────────────────────────────────
  function logSighting(button) {
    if (!state.result || !state.coords || state.logged) return;
    button.disabled = true;
    button.textContent = "Logging…";

    API.createSighting({
      common_name: state.result.common_name,
      scientific_name: state.result.scientific_name,
      status: state.result.status,
      latitude: state.coords.lat,
      longitude: state.coords.lng
    }).then(function (row) {
      state.logged = true;
      renderLogged(row);
    }).catch(function (err) {
      button.disabled = false;
      button.innerHTML = '<span class="ico i-map-pin w-[19px] h-[19px]" aria-hidden="true"></span>Log this sighting';
      if (err.kind === "ratelimit") { renderRateLimited(err.retryAfter); return; }
      var note = document.createElement("p");
      note.className = "mt-3 text-[13px] text-mute-800 bg-surface border border-mute-300 rounded-gt px-3 py-2";
      note.textContent = "Could not save that just now. " + (err.message || "") + " Your result is still here — try again.";
      button.parentNode.insertBefore(note, button.nextSibling);
    });
  }

  function renderLogged(row) {
    var meta = statusMeta(row.status);
    // The confirmation is clay, not green. Green means native, and a logged
    // invasive is not a success in that sense.
    views.result.innerHTML = '' +
      '<div class="pt-8 text-center">' +
        '<span class="inline-flex w-[68px] h-[68px] rounded-full bg-clay items-center justify-center shadow-gt-md">' +
          '<span class="ico i-check w-8 h-8 text-paper" aria-hidden="true"></span></span>' +
        '<h1 class="font-display text-[30px] mt-5">Logged.</h1>' +
        '<p class="text-[14.5px] leading-[1.55] text-mute-800 mt-2 max-w-[340px] mx-auto">' +
          'It is on the community map now, where anyone walking the same trail can see it.</p>' +
        '<div class="mt-6 rounded-gt-lg bg-surface p-4 flex items-center gap-3.5 text-left">' +
          '<span class="gt-pin flex-none" style="background:' + pinColor(row.status) + '">' +
            '<span class="ico ' + meta.icon + '" aria-hidden="true"></span></span>' +
          '<div class="min-w-0">' +
            '<div class="font-display text-[17px]">' + esc(row.common_name) + '</div>' +
            '<div class="text-[12.5px] text-mute-700">' + esc(meta.word) + ' &middot; ' +
              Number(row.latitude).toFixed(4) + ', ' + Number(row.longitude).toFixed(4) + '</div>' +
          '</div>' +
        '</div>' +
        '<a href="map.html" class="mt-6 w-full h-[54px] rounded-full bg-clay text-paper font-display text-[17px] flex items-center justify-center gap-2.5 shadow-gt-md">' +
          '<span class="ico i-map w-5 h-5" aria-hidden="true"></span>See it on the map</a>' +
        '<button type="button" data-restart class="mt-3 w-full h-[50px] rounded-full font-display text-[15px] text-mute-700">Identify something else</button>' +
      '</div>';
    views.result.querySelectorAll("[data-restart]").forEach(function (b) {
      b.addEventListener("click", restart);
    });
    window.scrollTo({ top: 0, behavior: "smooth" });
  }

  function pinColor(status) {
    return { invasive: "#a32a1e", introduced: "#b0770b", native: "#5b7c3a", unknown: "#6b6b74" }[status] || "#6b6b74";
  }

  // ── Failure screens ─────────────────────────────────────────────────────
  // Three failures that look nothing like each other, because they are
  // nothing like each other.

  function renderUnidentified(result) {
    var actions = (result && result.explainer && result.explainer.what_to_do) || [
      "Take another photo in good light, filling the frame with the organism",
      "Focus on distinctive features: leaves and flowers for plants, markings for insects and animals",
      "Avoid photographing several different species at once"
    ];
    views.fail.innerHTML = '' +
      '<span class="inline-flex w-[60px] h-[60px] rounded-full bg-surface items-center justify-center">' +
        '<span class="ico i-search-x w-7 h-7 text-mute-700" aria-hidden="true"></span></span>' +
      '<h1 class="font-display text-[28px] leading-[1.12] mt-4">We could not tell what this is</h1>' +
      '<p class="text-[14.5px] leading-[1.55] text-mute-800 mt-2.5">' +
        esc((result && result.explainer && result.explainer.summary) ||
        "We could not find a clear plant or animal in this photo. Try getting closer, with the organism filling most of the frame.") + '</p>' +
      '<div class="mt-6 rounded-gt-lg bg-clay-100 border border-clay-200 p-4">' +
        '<div class="text-[12px] font-bold uppercase tracking-[.08em] text-mute-700 mb-2.5">What usually fixes it</div>' +
        '<div class="space-y-2.5">' +
          actions.slice(0, 3).map(function (a, i) {
            return '<div class="flex gap-3"><span class="w-[23px] h-[23px] rounded-full bg-clay-200 text-clay-700 text-[12px] font-bold flex items-center justify-center flex-none">' +
              (i + 1) + '</span><span class="text-[13.5px] leading-[1.45]">' + esc(a) + '</span></div>';
          }).join("") +
        '</div>' +
      '</div>' +
      '<p class="mt-4 text-[12.5px] leading-[1.55] text-mute-700">This one is on us as much as the photo. Some species simply are not in the reference collections yet.</p>' +
      '<button type="button" data-restart class="mt-6 w-full h-[54px] rounded-full bg-clay text-paper font-display text-[17px] shadow-gt-md">Take another photo</button>';
    bindFail();
    switchTo("fail");
  }

  var countdownTimer = null;

  // Retry-After is always seconds, but it carries three very different waits:
  // the per-minute limit (under a minute), the per-day limit, and the global
  // identify ceiling - the last two are measured in hours. Rendering 86400 as
  // a five-digit second count under a headline about "one minute" reads as a
  // bug, so both the unit and the wording follow the size of the wait.
  function waitParts(s) {
    if (s < 60) return { n: s, u: "s" };
    if (s < 3600) return { n: Math.ceil(s / 60), u: "m" };
    return { n: Math.ceil(s / 3600), u: "h" };
  }

  function numeralHTML(s) {
    var p = waitParts(s);
    return p.n + '<span class="text-[34px]">' + p.u + '</span>';
  }

  function renderRateLimited(seconds) {
    clearInterval(countdownTimer);
    var remaining = Math.max(1, seconds || 60);
    // Only a wait someone will actually sit through gets a live countdown.
    var ticks = remaining <= 120;

    // The countdown numeral is the signature. Nothing else in the app looks
    // remotely like this, so a rate limit can never be read as a crash.
    views.fail.innerHTML = '' +
      '<div class="text-center pt-2">' +
        '<div class="font-display text-[82px] leading-none tabular-nums" id="rlNum">' + numeralHTML(remaining) + '</div>' +
        '<div class="text-[13px] text-mute-700 mt-1">until you can try again</div>' +
      '</div>' +
      '<h1 class="font-display text-[26px] leading-[1.12] mt-7">' +
        (ticks ? "That is enough checks for one minute" : "That is enough checks for today") + '</h1>' +
      '<p class="text-[14px] leading-[1.55] text-mute-800 mt-2.5">' +
        'Groundtruth allows five identifications a minute and twenty a day, which is what keeps the services it calls inside their free tiers.</p>' +
      '<p class="mt-4 text-[12.5px] leading-[1.55] text-mute-700 bg-surface rounded-gt px-3.5 py-3">' +
        'The limit counts by network, not by person. On shared wifi everyone around you counts as one.</p>' +
      '<a href="map.html" class="mt-6 w-full h-[54px] rounded-full border border-ink/25 font-display text-[16px] flex items-center justify-center gap-2.5">' +
        '<span class="ico i-map w-5 h-5" aria-hidden="true"></span>Browse the map meanwhile</a>';
    switchTo("fail");

    // A day-long timer would tick 86,400 times to no purpose, and silently
    // resuming an upload the user abandoned hours ago is not a kindness.
    if (!ticks) return;

    countdownTimer = setInterval(function () {
      remaining -= 1;
      var num = el("rlNum");
      if (!num) { clearInterval(countdownTimer); return; }
      if (remaining <= 0) {
        clearInterval(countdownTimer);
        // Keep the photo. Being throttled is not a reason to make someone
        // pick their picture again.
        resumeUpload();
        return;
      }
      num.innerHTML = numeralHTML(remaining);
    }, 1000);
  }

  function renderUnreachable() {
    state.retryCount += 1;
    var attempt = state.retryCount;
    var willRetry = attempt < 3;
    var wait = 6;

    // No countdown numeral here, so it cannot be confused with a 429.
    views.fail.innerHTML = '' +
      '<span class="inline-flex w-[60px] h-[60px] rounded-full bg-surface items-center justify-center">' +
        '<span class="ico i-wifi-off w-7 h-7 text-mute-700" aria-hidden="true"></span></span>' +
      '<h1 class="font-display text-[28px] leading-[1.12] mt-4">We cannot reach Groundtruth</h1>' +
      '<p class="text-[14.5px] leading-[1.55] text-mute-800 mt-2.5">' +
        'The request did not get through. Trail signal comes and goes, and our server occasionally restarts. Both usually clear within a minute.</p>' +
      '<div class="mt-5 rounded-gt-lg bg-surface p-4">' +
        '<div class="text-[13.5px] font-bold">Your photo is still here</div>' +
        '<p class="text-[12.5px] leading-[1.5] text-mute-700 mt-1">Nothing was uploaded, and nothing was lost. Retrying sends the same photo.</p>' +
      '</div>' +
      (willRetry
        ? '<div class="mt-4 flex items-center gap-2.5 text-[12.5px] text-mute-700">' +
            '<span class="ico i-retry w-4 h-4" aria-hidden="true"></span>' +
            '<span>Attempt ' + attempt + ' of 3 &middot; retrying in <span id="netWait">' + wait + '</span>s</span></div>'
        : '<p class="mt-4 text-[12.5px] text-mute-700">Three attempts did not get through. Worth checking your signal.</p>') +
      '<button type="button" data-retry class="mt-6 w-full h-[54px] rounded-full bg-clay text-paper font-display text-[17px] shadow-gt-md">Try now</button>' +
      '<button type="button" data-restart class="mt-3 w-full h-[50px] rounded-full font-display text-[15px] text-mute-700">Use a different photo</button>';
    bindFail();
    switchTo("fail");

    if (willRetry) {
      clearInterval(countdownTimer);
      countdownTimer = setInterval(function () {
        wait -= 1;
        var node = el("netWait");
        if (!node) { clearInterval(countdownTimer); return; }
        if (wait <= 0) { clearInterval(countdownTimer); runIdentify(); return; }
        node.textContent = wait;
      }, 1000);
    }
  }

  function bindFail() {
    views.fail.querySelectorAll("[data-restart]").forEach(function (b) {
      b.addEventListener("click", restart);
    });
    var retry = views.fail.querySelector("[data-retry]");
    if (retry) retry.addEventListener("click", function () { clearInterval(countdownTimer); runIdentify(); });
  }

  // ── Run ─────────────────────────────────────────────────────────────────
  function runIdentify() {
    if (!state.file) { restart(); return; }
    startWait();

    API.identify(state.file, state.coords).then(function (result) {
      // "Could not identify" arrives as a 200 with id_source "none", so it is
      // designed as a normal outcome rather than an error.
      if (!result || result.id_source === "none" || !result.scientific_name) {
        clearWaitTimers();
        renderUnidentified(result);
        return;
      }
      state.retryCount = 0;
      return finishWait(result).then(function () { renderResult(result); });
    }).catch(function (err) {
      clearWaitTimers();
      if (err.kind === "ratelimit") { renderRateLimited(err.retryAfter); return; }
      if (err.kind === "network" || err.kind === "server") { renderUnreachable(); return; }
      if (err.kind === "upload") {
        switchTo("upload");
        showUploadError("That photo was rejected", esc(err.message), "Try a different photo.");
        return;
      }
      renderUnreachable();
    });
  }

  /* Back to the upload screen, photo discarded. Use this when the user is
     deliberately starting over. */
  function restart() {
    clearWaitTimers();
    clearInterval(countdownTimer);
    state.result = null;
    state.logged = false;
    state.retryCount = 0;
    switchTo("upload");
    clearUploadError();
    hide(el("photoCard"));
    if (state.previewUrl) { URL.revokeObjectURL(state.previewUrl); state.previewUrl = null; }
    state.file = null;
    el("fileInput").value = "";
  }

  /* Back to the upload screen with the photo intact. Use this when the app
     interrupted the user rather than the other way round. */
  function resumeUpload() {
    clearWaitTimers();
    clearInterval(countdownTimer);
    state.result = null;
    state.logged = false;
    switchTo("upload");
    clearUploadError();
    if (state.file) { show(el("photoCard")); } else { hide(el("photoCard")); }
  }

  // ── Desktop rail ────────────────────────────────────────────────────────
  function loadRail() {
    var list = el("railList");
    if (!list) return;
    API.listSightings().then(function (rows) {
      if (!rows || !rows.length) {
        list.innerHTML = '<li class="text-[13px] text-mute-700 leading-[1.5]">No sightings logged yet. The first pin can be yours.</li>';
        return;
      }
      list.innerHTML = rows.slice(0, 5).map(function (row) {
        var meta = statusMeta(row.status);
        return '<li class="flex items-center gap-3">' +
          '<span class="w-2.5 h-2.5 rounded-full flex-none" style="background:' + pinColor(row.status) + '"></span>' +
          '<span class="text-[13.5px] font-semibold truncate flex-1">' + esc(row.common_name) + '</span>' +
          '<span class="text-[12px] text-mute-700 flex-none">' + relativeTime(row.created_at) + '</span></li>';
      }).join("");
    }).catch(function () {
      list.innerHTML = '<li class="text-[13px] text-mute-700">Could not load recent sightings.</li>';
    });
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

  // ── Wire up ─────────────────────────────────────────────────────────────
  document.addEventListener("DOMContentLoaded", function () {
    views = {
      upload: el("viewUpload"),
      loading: el("viewLoading"),
      result: el("viewResult"),
      fail: el("viewFail")
    };

    // Render's free tier sleeps; /api/health is rate-limit exempt precisely so
    // this is free. Most people then never see the cold-start notice.
    API.warmUp();
    loadRail();
    setLocationUI(false);

    // Camera vs library.
    //
    // Delegated from the whole dashed zone rather than bound to the button,
    // so the entire target stays tappable the way it was when it was a single
    // <button>. #shutterCam is still a real button underneath, purely so it
    // remains keyboard-reachable - its click bubbles up to here rather than
    // opening a picker itself, which is what stops it firing twice.
    //
    // matchMedia is read at click time rather than cached, so rotating a
    // tablet across the breakpoint cannot strand the wrong input.
    el("shutter").addEventListener("click", function (e) {
      if (e.target.closest("#shutterSub")) return;   // library handles its own
      var desktop = window.matchMedia("(min-width: 768px)").matches;
      el(desktop ? "fileInput" : "cameraInput").click();
    });
    el("shutterSub").addEventListener("click", function () { el("fileInput").click(); });
    el("photoChange").addEventListener("click", function () { el("fileInput").click(); });

    // Clearing the value matters here: a camera hands back the same filename
    // every time, and without the reset a second shot of the same name fires
    // no change event at all. The File object stays valid after the reset.
    function onPicked(e) {
      var file = e.target.files[0];
      e.target.value = "";
      acceptFile(file);
    }
    el("fileInput").addEventListener("change", onPicked);
    el("cameraInput").addEventListener("change", onPicked);
    el("identifyBtn").addEventListener("click", runIdentify);

    el("locToggle").addEventListener("click", function () {
      if (state.locationOn) {
        state.coords = null;
        setLocationUI(false);
      } else {
        requestLocation();
      }
    });
    el("locChip").addEventListener("click", function () {
      if (!state.locationOn) requestLocation();
    });

    // Desktop: the drop zone replaces the shutter, because there is no camera
    // to open on a laptop.
    if (window.matchMedia("(min-width: 768px)").matches) {
      el("shutterLabel").textContent = "Drop a photo here";
      el("shutterSub").textContent = "or click to choose one";
    }
    ["dragenter", "dragover"].forEach(function (evt) {
      el("shutter").addEventListener(evt, function (e) {
        e.preventDefault();
        el("shutter").classList.add("bg-clay-200");
      });
    });
    ["dragleave", "drop"].forEach(function (evt) {
      el("shutter").addEventListener(evt, function (e) {
        e.preventDefault();
        el("shutter").classList.remove("bg-clay-200");
      });
    });
    el("shutter").addEventListener("drop", function (e) {
      if (e.dataTransfer && e.dataTransfer.files && e.dataTransfer.files[0]) {
        acceptFile(e.dataTransfer.files[0]);
      }
    });
  });
})();
