/* ACK Stage 2 /daftar: 5-step form, local draft (never passwords/images), in-browser KTP redaction.
   Only the redacted blob is sent as "ktp_redacted"; the original file input has no name and is never read into FormData. */
(function () {
  "use strict";
  var $ = function (s, r) { return (r || document).querySelector(s); };
  var $$ = function (s, r) { return Array.prototype.slice.call((r || document).querySelectorAll(s)); };
  var form = $("#reg"), steps = $$(".step"), cur = 1, KEY = "ack_reg_draft_v1";
  var SKIP = { password: 1, password2: 1, csrf_token: 1, ktp_nik_ok: 1, ktp_addr_ok: 1, ktp_sign_ok: 1 };
  var MAXB = 10 * 1024 * 1024, MAXSIDE = 1600;

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
        else if (!/^image\/(jpeg|png|webp)$/.test(f.type)) fail(k, "Hanya JPG, PNG, atau WEBP.");
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
  var cv = $("#ktp-canvas"), ctx = cv.getContext("2d"), img = null, boxes = [], sel = -1, drag = null, redacted = null, prevURL = null, srcURL = null;
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
    if (file.size > MAXB) { showErr("ktp_redacted", "Maksimal 10 MB."); return; }
    if (srcURL) URL.revokeObjectURL(srcURL);
    srcURL = URL.createObjectURL(file);
    var im = new Image();
    im.onload = function () {
      var k = Math.min(1, MAXSIDE / Math.max(im.naturalWidth, im.naturalHeight));
      img = im; cv.width = Math.round(im.naturalWidth * k); cv.height = Math.round(im.naturalHeight * k);
      var w = cv.width, h = cv.height;
      boxes = [{ label: "NIK", x: w * .3, y: h * .2, w: w * .4, h: h * .1 }, { label: "Alamat", x: w * .05, y: h * .55, w: w * .5, h: h * .2 },
               { label: "Tanda tangan", x: w * .6, y: h * .72, w: w * .3, h: h * .18 }];
      sel = 0; $("#ktp-tools").hidden = false; clearErr(steps[3]); draw();
    };
    im.onerror = function () { showErr("ktp_redacted", "File bukan gambar yang valid."); $("#ktp-tools").hidden = true; };
    im.src = srcURL;
  }
  $("#ktp-src").addEventListener("change", function (e) { load(e.target.files[0]); });
  function pt(e) { var r = cv.getBoundingClientRect(), s = scale(); return { x: (e.clientX - r.left) * s, y: (e.clientY - r.top) * s }; }
  function hit(p) {
    var g = HANDLE * scale() * 1.6;
    for (var i = boxes.length - 1; i >= 0; i--) {
      var b = boxes[i];
      if (p.x >= b.x + b.w - g && p.x <= b.x + b.w + g / 2 && p.y >= b.y + b.h - g && p.y <= b.y + b.h + g / 2) return { i: i, mode: "size" };
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
    c.fillStyle = "#000"; boxes.forEach(function (b) { c.fillRect(b.x, b.y, b.w, b.h); });
    o.toBlob(function (blob) {
      if (!blob) { showErr("ktp_redacted", "Gagal membuat pratinjau."); return; }
      redacted = blob; if (prevURL) URL.revokeObjectURL(prevURL);
      prevURL = URL.createObjectURL(blob); $("#ktp-prev").src = prevURL; $("#prev-wrap").hidden = false; setChecks(true); clearErr(steps[3]);
    }, "image/jpeg", 0.9);
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
    fd.append("photo_face", form.photo_face.files[0]); fd.append("photo_body", form.photo_body.files[0]);
    var btn = $("#send"); btn.disabled = true; btn.textContent = "Mengirim…"; clearErr();
    fetch("/daftar/submit", { method: "POST", body: fd, headers: { "X-CSRF-Token": form.csrf_token.value }, credentials: "same-origin" })
      .then(function (r) { return r.json().catch(function () { return { ok: false, error: "Respons server tidak valid." }; }); })
      .then(function (j) {
        if (j.ok) { clearDraft(); location.href = j.redirect; return; }
        btn.disabled = false; btn.textContent = "Kirim pendaftaran";
        if (j.errors) { Object.keys(j.errors).forEach(function (k) { showErr(k, j.errors[k]); }); go(j.step || 1); }
        else $("#formerr").textContent = j.error || "Gagal mengirim.";
      })
      .catch(function () { btn.disabled = false; btn.textContent = "Kirim pendaftaran"; $("#formerr").textContent = "Koneksi gagal. Coba lagi."; });
  });

  loadDraft(); go(cur);
  $("#intro-n").textContent = $("#intro").value.trim().length;
})();
