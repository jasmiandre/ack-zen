/* ACK Stage 2 /daftar: 5-step form, local draft (never passwords/images), in-browser KTP redaction.
   Only the redacted blob is sent as "ktp_redacted"; the original file input has no name and is never read into FormData. */
(function () {
  "use strict";
  var $ = function (s, r) { return (r || document).querySelector(s); };
  var $$ = function (s, r) { return Array.prototype.slice.call((r || document).querySelectorAll(s)); };
  var form = $("#reg"), steps = $$(".step"), cur = 1, KEY = "ack_reg_draft_v1";
  var SKIP = { password: 1, password2: 1, csrf_token: 1, ktp_nik_ok: 1, ktp_addr_ok: 1, ktp_sign_ok: 1 };
  var MAXB = 10 * 1024 * 1024, MAXSIDE = 1600, QUALITY = 0.85, TARGET = 999999 /* strict: browser output must be < 1,000,000 bytes */, MINSIDE = 500;
  var photos = { photo_face: null, photo_body: null }; /* compressed JPEG blobs; the raw picker files are never sent */
  var HEIC_MSG = "Browser ini tidak bisa membaca HEIC/HEIF. Ekspor foto sebagai JPG, PNG, atau WEBP (iPhone: Pengaturan > Kamera > Format > Paling Kompatibel, atau bagikan sebagai JPEG), lalu pilih lagi.";
  function isHeic(f) { return /\.(heic|heif)$/i.test(f.name) || /^image\/hei[cf]/i.test(f.type); }

  /* ---------- image decode + compress (browser only) ---------- */
  /* Native decode first for every file regardless of extension/MIME (Safari decodes HEIC); no decoder library. */
  function decode(file) {
    var fail = function () { throw new Error(isHeic(file) ? HEIC_MSG : "File bukan gambar yang valid."); };
    if (window.createImageBitmap) return createImageBitmap(file).catch(function () { return viaImg(file); }).catch(fail);
    return viaImg(file).catch(fail);
  }
  function viaImg(file) {
    return new Promise(function (ok, no) {
      var u = URL.createObjectURL(file), im = new Image();
      im.onload = function () { URL.revokeObjectURL(u); ok({ width: im.naturalWidth, height: im.naturalHeight, close: function () {}, src: im }); };
      im.onerror = function () { URL.revokeObjectURL(u); no(new Error("decode")); };
      im.src = u;
    });
  }
  function drawable(d) { return d.src || d; }
  function fitSize(w, h, side) { var k = Math.min(1, side / Math.max(w, h)); return [Math.max(1, Math.round(w * k)), Math.max(1, Math.round(h * k))]; }
  function toJpeg(c) { return new Promise(function (ok, no) { c.toBlob(function (b) { b ? ok(b) : no(new Error("Gagal mengompres gambar.")); }, "image/jpeg", QUALITY); }); }
  /* Longest side <=1600, JPEG q.85. If still >=1,000,000 B, shrink dimensions (quality stays .85) until it fits; below MINSIDE -> reject (never send oversize). */
  function compress(src, w, h) {
    var side = MAXSIDE, n = 0;
    function attempt() {
      var sz = fitSize(w, h, side), c = document.createElement("canvas"), x;
      c.width = sz[0]; c.height = sz[1]; x = c.getContext("2d");
      x.fillStyle = "#fff"; x.fillRect(0, 0, c.width, c.height); /* JPEG has no alpha */
      x.drawImage(src, 0, 0, c.width, c.height);
      return toJpeg(c).then(function (b) {
        n++;
        var m = Math.max(c.width, c.height);
        if (b.size <= TARGET) return { blob: b, w: c.width, h: c.height, tries: n };
        if (m <= MINSIDE) throw new Error("Foto tidak bisa dikompres di bawah 1 MB tanpa merusak keterbacaan; pilih foto lain.");
        side = Math.min(m - 1, Math.max(MINSIDE, Math.floor(m * Math.sqrt(TARGET / b.size) * 0.95)));
        return attempt();
      });
    }
    return attempt();
  }
  function note(k, t) { var p = $("[data-note=" + k + "]"); if (p) p.textContent = t; }
  function prep(k, file) {
    photos[k] = null; clearErr(steps[3]); note(k, "");
    if (!file) return;
    if (file.size > MAXB) { showErr(k, "Maksimal 10 MB."); return; }
    note(k, "Memproses foto…");
    decode(file).then(function (d) {
      if (form[k].files[0] !== file) { if (d.close) d.close(); return; }
      return compress(drawable(d), d.width, d.height).then(function (r) {
        if (d.close) d.close();
        if (form[k].files[0] !== file) return;
        photos[k] = r.blob;
        note(k, "Siap: " + r.w + "×" + r.h + ", " + Math.round(r.blob.size / 1024) + " KB (JPEG 85%" + (r.tries > 1 ? ", dimensi diperkecil" : "") + ")");
      });
    }).catch(function (e) { if (form[k].files[0] === file) { note(k, ""); showErr(k, e.message); } });
  }
  [ "photo_face", "photo_body" ].forEach(function (k) { form[k].addEventListener("change", function (e) { prep(k, e.target.files[0]); }); });

  /* ---------- draft ---------- */
  function saveDraft() {
    var d = { step: cur, f: {} };
    $$("input,select,textarea", form).forEach(function (el) {
      if (!el.name || SKIP[el.name] || el.type === "file") return;
      if (el.type === "radio") { if (el.checked) d.f[el.name] = el.value; }
      else d.f[el.name] = el.type === "checkbox" ? el.checked : el.value;
    });
    try { localStorage.setItem(KEY, JSON.stringify(d)); } catch (e) { /* storage full/blocked: draft is optional */ }
  }
  function loadDraft() {
    var d; try { d = JSON.parse(localStorage.getItem(KEY) || "null"); } catch (e) { d = null; }
    if (!d || !d.f) return;
    $$("input,select,textarea", form).forEach(function (el) {
      if (!el.name || SKIP[el.name] || el.type === "file" || !(el.name in d.f)) return;
      if (el.type === "radio") el.checked = el.value === d.f[el.name];
      else if (el.type === "checkbox") el.checked = !!d.f[el.name];
      else el.value = d.f[el.name];
    });
    cur = Math.min(Math.max(+d.step || 1, 1), 4); /* files/KTP must be redone, so never resume past step 4 */
  }
  function clearDraft() { try { localStorage.removeItem(KEY); } catch (e) { /* ignore */ } }

  /* ---------- steps ---------- */
  function showErr(name, msg) {
    $$("[data-err]", form).forEach(function (p) {
      if (p.getAttribute("data-err").split(" ").indexOf(name) >= 0) p.textContent = msg;
    });
    var el = form.elements[name]; if (el && el.setAttribute) el.setAttribute("aria-invalid", "true");
  }
  function clearErr(scope) {
    $$("[data-err]", scope || form).forEach(function (p) { p.textContent = ""; });
    $$("[aria-invalid]", scope || form).forEach(function (e) { e.removeAttribute("aria-invalid"); });
    $("#formerr").textContent = "";
  }
  function age18(v) {
    var m = /^(\d{4})-(\d{2})-(\d{2})$/.exec(v); if (!m) return false;
    var t = new Date(), y = t.getFullYear() - 18, mo = t.getMonth() + 1, d = t.getDate();
    var b = +m[1] * 10000 + +m[2] * 100 + +m[3];
    return b <= y * 10000 + mo * 100 + d;
  }
  function checkStep(n) {
    var sec = steps[n - 1], bad = null;
    clearErr(sec);
    function fail(name, msg) { showErr(name, msg); if (!bad) bad = form.elements[name] || null; }
    $$("input,select,textarea", sec).forEach(function (el) {
      if (!el.name || el.type === "file" || el.type === "checkbox" || el.type === "radio" || el.disabled) return;
      if (el.hasAttribute("required") && !el.value.trim()) fail(el.name, "Wajib diisi.");
      else if (el.value && !el.checkValidity()) fail(el.name, el.validationMessage || "Isian tidak valid.");
    });
    if (n === 1 && form.password.value !== form.password2.value) fail("password2", "Konfirmasi password tidak sama.");
    if (n === 2 && form.birth_date.value && !age18(form.birth_date.value)) fail("birth_date", "Usia minimal 18 tahun.");
    if (n === 3) {
      if (form.intro.value.trim().length < 30) fail("intro", "Minimal 30 karakter.");
      if (+form.pref_age_min.value > +form.pref_age_max.value) fail("pref_age_max", "Rentang usia terbalik.");
    }
    if (n === 4) {
      if (!redacted) fail("ktp_redacted", "Pilih foto KTP, tutup NIK, alamat, tanda tangan, lalu buat pratinjau.");
      ["ktp_nik_ok", "ktp_addr_ok", "ktp_sign_ok"].forEach(function (k) { if (redacted && !form[k].checked) fail(k, "Centang konfirmasi sensor."); });
      ["photo_face", "photo_body"].forEach(function (k) {
        var f = form[k].files[0];
        if (!f) fail(k, "Pilih file foto.");
        else if (f.size > MAXB) fail(k, "Maksimal 10 MB.");
        else if (!photos[k]) fail(k, "Foto belum selesai diproses atau tidak valid; pilih ulang.");
      });
    }
    if (n === 5) {
      if (!form.program_id.value && !$("input[name=program_id]:checked")) fail("program_id", "Pilih satu program.");
      ["consent_terms", "consent_privacy", "consent_honest"].forEach(function (k) { if (!form[k].checked) fail(k, "Wajib dicentang."); });
    }
    if (bad && bad.focus) bad.focus();
    return !bad;
  }
  function go(n) {
    cur = Math.min(Math.max(n, 1), 5);
    steps.forEach(function (s, i) { s.hidden = i !== cur - 1; });
    $("#bar").style.width = (cur * 20) + "%";
    $("#bar").setAttribute("aria-valuenow", cur);
    $("#stepname").textContent = "Langkah " + cur + " dari 5 — " + steps[cur - 1].querySelector("legend").textContent.replace(/^\d\.\s*/, "");
    $("#prev").hidden = cur === 1; $("#next").hidden = cur === 5; $("#send").hidden = cur !== 5;
    var h = steps[cur - 1].querySelector("input:not([type=hidden]):not([disabled]),select,textarea");
    if (h && document.readyState === "complete") h.focus();
    saveDraft();
  }
  $("#next").addEventListener("click", function () { if (checkStep(cur)) go(cur + 1); });
  $("#prev").addEventListener("click", function () { go(cur - 1); });
  form.addEventListener("input", saveDraft);
  form.addEventListener("change", saveDraft);
  form.addEventListener("keydown", function (e) { if (e.key === "Enter" && e.target.tagName !== "TEXTAREA" && cur < 5) { e.preventDefault(); $("#next").click(); } });
  $("#intro").addEventListener("input", function () { $("#intro-n").textContent = $("#intro").value.trim().length; });

  /* ---------- KTP redaction canvas ---------- */
  var cv = $("#ktp-canvas"), ctx = cv.getContext("2d"), img = null, boxes = [], sel = -1, drag = null, redacted = null, prevURL = null;
  var HANDLE = 22; /* px, display space: >=44px hit area is provided by the 22px grip + padding below */
  function scale() { return cv.width / cv.clientWidth || 1; }
  function draw() {
    if (!img) return;
    ctx.drawImage(img, 0, 0, cv.width, cv.height);
    var s = scale();
    boxes.forEach(function (b, i) {
      ctx.fillStyle = "#000"; ctx.fillRect(b.x, b.y, b.w, b.h);
      ctx.strokeStyle = i === sel ? "#f3d9b1" : "#fff"; ctx.lineWidth = 2 * s; ctx.strokeRect(b.x, b.y, b.w, b.h);
      ctx.fillStyle = "#f3d9b1"; var g = HANDLE * s; ctx.fillRect(b.x + b.w - g, b.y + b.h - g, g, g);
      ctx.fillStyle = "#fff"; ctx.font = (14 * s) + "px sans-serif"; ctx.fillText(b.label, b.x + 6 * s, b.y + 18 * s);
    });
  }
  function load(file) {
    redacted = null; $("#prev-wrap").hidden = true; setChecks(false);
    if (!file) { $("#ktp-tools").hidden = true; return; }
    if (file.size > MAXB) { $("#ktp-tools").hidden = true; showErr("ktp_redacted", "Maksimal 10 MB."); return; }
    clearErr(steps[3]);
    decode(file).then(function (d) {
      if ($("#ktp-src").files[0] !== file) return;
      var sz = fitSize(d.width, d.height, MAXSIDE), w0 = document.createElement("canvas"); /* working copy <=1600 */
      w0.width = sz[0]; w0.height = sz[1]; w0.getContext("2d").drawImage(drawable(d), 0, 0, sz[0], sz[1]); if (d.close) d.close();
      img = w0; cv.width = sz[0]; cv.height = sz[1];
      var w = cv.width, h = cv.height;
      boxes = [{ label: "NIK", x: w * .3, y: h * .2, w: w * .4, h: h * .1 }, { label: "Alamat", x: w * .05, y: h * .55, w: w * .5, h: h * .2 },
               { label: "Tanda tangan", x: w * .6, y: h * .72, w: w * .3, h: h * .18 }];
      sel = 0; $("#ktp-tools").hidden = false; draw();
    }).catch(function (e) { $("#ktp-tools").hidden = true; showErr("ktp_redacted", e.message); });
  }
  $("#ktp-src").addEventListener("change", function (e) { load(e.target.files[0]); });
  function pt(e) { var r = cv.getBoundingClientRect(), s = scale(); return { x: (e.clientX - r.left) * s, y: (e.clientY - r.top) * s }; }
  function hit(p) {
    var g = HANDLE * scale(); /* 44px zone centred on corner; inner reach capped to half the box so small boxes stay movable */
    for (var i = boxes.length - 1; i >= 0; i--) {
      var b = boxes[i], ix = Math.min(g, b.w / 2), iy = Math.min(g, b.h / 2);
      if (p.x >= b.x + b.w - ix && p.x <= b.x + b.w + g && p.y >= b.y + b.h - iy && p.y <= b.y + b.h + g) return { i: i, mode: "size" };
      if (p.x >= b.x && p.x <= b.x + b.w && p.y >= b.y && p.y <= b.y + b.h) return { i: i, mode: "move" };
    }
    return null;
  }
  cv.addEventListener("pointerdown", function (e) {
    var p = pt(e), h = hit(p); if (!h) return;
    sel = h.i; drag = { mode: h.mode, ox: p.x - boxes[sel].x, oy: p.y - boxes[sel].y }; cv.setPointerCapture(e.pointerId); redactedChanged(); draw();
  });
  cv.addEventListener("pointermove", function (e) {
    if (!drag) return;
    var p = pt(e), b = boxes[sel], m = 12 * scale();
    if (drag.mode === "move") { b.x = Math.min(Math.max(p.x - drag.ox, 0), cv.width - b.w); b.y = Math.min(Math.max(p.y - drag.oy, 0), cv.height - b.h); }
    else { b.w = Math.min(Math.max(p.x - b.x, m), cv.width - b.x); b.h = Math.min(Math.max(p.y - b.y, m), cv.height - b.y); }
    draw();
  });
  function end() { drag = null; }
  cv.addEventListener("pointerup", end); cv.addEventListener("pointercancel", end);
  cv.tabIndex = 0;
  cv.addEventListener("keydown", function (e) { /* keyboard: Tab cycles boxes, arrows move, Shift+arrows resize */
    var b = boxes[sel]; if (!b) return;
    var d = { ArrowLeft: [-1, 0], ArrowRight: [1, 0], ArrowUp: [0, -1], ArrowDown: [0, 1] }[e.key];
    if (e.key === "Tab" && !e.shiftKey && sel < boxes.length - 1) { sel++; e.preventDefault(); draw(); return; }
    if (e.key === "Tab" && e.shiftKey && sel > 0) { sel--; e.preventDefault(); draw(); return; }
    if (!d) return; e.preventDefault(); var st = 8 * scale();
    if (e.shiftKey) { b.w = Math.max(st, Math.min(b.w + d[0] * st, cv.width - b.x)); b.h = Math.max(st, Math.min(b.h + d[1] * st, cv.height - b.y)); }
    else { b.x = Math.min(Math.max(b.x + d[0] * st, 0), cv.width - b.w); b.y = Math.min(Math.max(b.y + d[1] * st, 0), cv.height - b.h); }
    redactedChanged(); draw();
  });
  $("#add-box").addEventListener("click", function () {
    if (!img) return; boxes.push({ label: "Kotak " + (boxes.length + 1), x: cv.width * .35, y: cv.height * .35, w: cv.width * .25, h: cv.height * .12 });
    sel = boxes.length - 1; redactedChanged(); draw();
  });
  function setChecks(on) { ["ktp_nik_ok", "ktp_addr_ok", "ktp_sign_ok"].forEach(function (k) { form[k].disabled = !on; if (!on) form[k].checked = false; }); }
  function redactedChanged() { if (redacted) { redacted = null; $("#prev-wrap").hidden = true; setChecks(false); } }
  $("#make-prev").addEventListener("click", function () {
    if (!img) return;
    var o = document.createElement("canvas"); o.width = cv.width; o.height = cv.height;
    var c = o.getContext("2d"); c.drawImage(img, 0, 0, o.width, o.height);
    c.fillStyle = "#000"; boxes.forEach(function (b) { c.fillRect(b.x, b.y, b.w, b.h); }); /* flatten first; only then lossy export/resize */
    $("#make-prev").disabled = true;
    compress(o, o.width, o.height).then(function (r) {
      $("#make-prev").disabled = false;
      var blob = r.blob; redacted = blob; if (prevURL) URL.revokeObjectURL(prevURL);
      prevURL = URL.createObjectURL(blob); $("#ktp-prev").src = prevURL; $("#prev-wrap").hidden = false; setChecks(true); clearErr(steps[3]);
    }).catch(function (e) { $("#make-prev").disabled = false; showErr("ktp_redacted", e && e.message || "Gagal membuat pratinjau."); });
  });

  /* ---------- submit: build FormData by hand so the original KTP can never ride along ---------- */
  form.addEventListener("submit", function (e) {
    e.preventDefault();
    for (var n = 1; n <= 5; n++) if (!checkStep(n)) { go(n); checkStep(n); return; }
    var fd = new FormData();
    $$("input,select,textarea", form).forEach(function (el) {
      if (!el.name || el.name === "csrf_token" || el.type === "file" || el.disabled) return;
      if (el.type === "radio" || el.type === "checkbox") { if (el.checked) fd.append(el.name, el.type === "radio" ? el.value : "on"); }
      else fd.append(el.name, el.value);
    });
    fd.append("ktp_redacted", redacted, "ktp-redacted.jpg");
    fd.append("photo_face", photos.photo_face, "face.jpg"); fd.append("photo_body", photos.photo_body, "body.jpg");
    var btn = $("#send"), pg = $("#up-progress"), st = $("#up-status"), done = false;
    function reset(msg) { btn.disabled = false; btn.textContent = "Kirim pendaftaran"; pg.hidden = true; if (msg) $("#formerr").textContent = msg; }
    btn.disabled = true; btn.textContent = "Mengirim…"; clearErr(); pg.hidden = false; pg.removeAttribute("value"); st.textContent = "Menyiapkan pengiriman…";
    var x = new XMLHttpRequest();
    x.open("POST", "/daftar/submit"); x.withCredentials = true; x.setRequestHeader("X-CSRF-Token", form.csrf_token.value);
    x.upload.addEventListener("progress", function (ev) {
      if (!ev.lengthComputable) return;
      var p = Math.floor(ev.loaded / ev.total * 100); pg.max = 100; pg.value = p; st.textContent = "Mengunggah " + p + "%";
    });
    x.upload.addEventListener("load", function () { pg.value = 100; st.textContent = "Terkirim 100%. Server sedang menyimpan pendaftaran…"; });
    x.addEventListener("load", function () {
      var j; try { j = JSON.parse(x.responseText); } catch (e) { j = { ok: false, error: "Respons server tidak valid" + (x.status === 413 ? " (file terlalu besar, 413)." : " (" + x.status + ").") }; }
      if (j.ok) { done = true; clearDraft(); location.href = j.redirect; return; }
      reset(); st.textContent = "";
      if (j.errors) { Object.keys(j.errors).forEach(function (k) { showErr(k, j.errors[k]); }); go(j.step || 1); }
      else $("#formerr").textContent = j.error || "Gagal mengirim.";
    });
    x.addEventListener("error", function () { reset("Koneksi gagal. Coba lagi."); st.textContent = ""; });
    x.addEventListener("abort", function () { reset("Pengiriman dibatalkan."); st.textContent = ""; });
    x.send(fd); /* single attempt: no automatic retry, so no double create */
  });

  loadDraft(); go(cur);
  $("#intro-n").textContent = $("#intro").value.trim().length;
})();
