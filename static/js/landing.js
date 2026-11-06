/* ACK Stage 1 landing. Everything here is progressive enhancement: the page is complete without it.
   Countdown + dialogs always run (tiny). Three.js / GSAP / ScrollTrigger / Lenis load only if motion is allowed. */
(function () {
  "use strict";
  var doc = document, root = doc.documentElement, body = doc.body;
  var reduce = matchMedia("(prefers-reduced-motion: reduce)").matches;
  var lowHW = (navigator.hardwareConcurrency || 8) <= 4;
  var $ = function (s, r) { return (r || doc).querySelector(s); };
  var $$ = function (s, r) { return Array.prototype.slice.call((r || doc).querySelectorAll(s)); };

  /* ---------- dialogs (demo notices, legal drafts) ---------- */
  $$("[data-dialog]").forEach(function (a) {
    a.addEventListener("click", function (e) {
      var d = doc.getElementById(a.getAttribute("data-dialog"));
      if (d && d.showModal) { e.preventDefault(); d.showModal(); }
    });
  });
  $$(".menu nav a").forEach(function (a) {
    a.addEventListener("click", function () { var m = $(".menu"); if (m && matchMedia("(max-width:899px)").matches) m.open = false; });
  });
  /* desktop: nav always visible; mobile: starts closed */
  var menu = $(".menu");
  function syncMenu() { if (menu && matchMedia("(min-width:900px)").matches) menu.open = true; }
  syncMenu();

  /* ---------- exact countdown to 14 Nov 2026 00:00 Asia/Jakarta ---------- */
  var target = Date.parse(body.getAttribute("data-event-iso"));
  var cd = {}; $$("[data-cd]").forEach(function (n) { cd[n.getAttribute("data-cd")] = n; });
  var grid = $(".cd-grid"), exp = $("[data-expired-msg]");
  function pad(n) { return (n < 10 ? "0" : "") + n; }
  function tick() {
    var left = Math.floor((target - Date.now()) / 1000);
    if (isNaN(left) || left <= 0) { if (grid) grid.hidden = true; if (exp) exp.hidden = false; return false; }
    var v = { d: Math.floor(left / 86400), h: Math.floor(left % 86400 / 3600), m: Math.floor(left % 3600 / 60), s: left % 60 };
    for (var k in v) if (cd[k]) cd[k].textContent = pad(v[k]);
    if (grid) grid.hidden = false; if (exp) exp.hidden = true;
    return true;
  }
  if (tick()) { var iv = setInterval(function () { if (!tick()) clearInterval(iv); }, 1000); }

  /* ---------- carousel buttons ---------- */
  $$("[data-car]").forEach(function (b) {
    b.addEventListener("click", function () {
      var s = $(".slides"); s.scrollBy({ left: +b.getAttribute("data-car") * s.clientWidth, behavior: reduce ? "auto" : "smooth" });
    });
  });

  /* ---------- static mode: stop here ---------- */
  var staticNote = $("[data-static-note]");
  function goStatic(why) {
    root.classList.add("static");
    if (staticNote && why) staticNote.hidden = false;
  }
  if (reduce) { goStatic(false); return; }

  /* ---------- progressive enhancement ---------- */
  var state = { p: 0, mx: 0, my: 0, tx: 0, ty: 0 };
  var libs = {
    gsap: ["https://cdn.jsdelivr.net/npm/gsap@3.12.5/dist/gsap.min.js", "sha384-g4NTh/Iv5PPU4xPyhEWqPcwtNXOvdaDI8LLnyYfyNZOjKJeYQyjzQ9X5275eBjpt"],
    st: ["https://cdn.jsdelivr.net/npm/gsap@3.12.5/dist/ScrollTrigger.min.js", "sha384-Z3REaz79l2IaAZqJsSABtTbhjgOUYyV3p90XNnAPCSHg3EMTz1fouunq9WZRtj3d"],
    lenis: ["https://cdn.jsdelivr.net/npm/lenis@1.1.13/dist/lenis.min.js", "sha384-B2WBjDzEjJpYvhmi2UyEn7rektqkf5suS6sNoyyrf0EBAwBHdkiXxIlU0V5Ru2ed"]
  };
  var THREE_URL = "https://cdn.jsdelivr.net/npm/three@0.160.0/build/three.module.min.js";
  function loadScript(l) {
    return new Promise(function (ok, no) {
      var s = doc.createElement("script");
      s.src = l[0]; s.integrity = l[1]; s.crossOrigin = "anonymous"; s.async = true;
      s.onload = ok; s.onerror = no; doc.head.appendChild(s);
    });
  }

  function heroProgress() {
    var h = $(".hero"); if (!h) return 0;
    var r = h.getBoundingClientRect(), span = h.offsetHeight - innerHeight;
    return span > 0 ? Math.min(1, Math.max(0, -r.top / span)) : 0;
  }
  var stageEls = $$(".stage");
  function paintStages() {
    var i = state.p < 0.3 ? 0 : state.p < 0.62 ? 1 : 2;
    stageEls.forEach(function (el, k) { el.classList.toggle("on", k <= i); });
  }
  paintStages();
  var rafScroll = 0, useST = false;
  addEventListener("scroll", function () {
    if (useST || rafScroll) return;
    rafScroll = requestAnimationFrame(function () { rafScroll = 0; state.p = heroProgress(); paintStages(); });
  }, { passive: true });
  state.p = heroProgress(); paintStages();

  /* pointer + gyroscope parallax */
  addEventListener("pointermove", function (e) {
    if (e.pointerType === "touch") return;
    state.tx = e.clientX / innerWidth * 2 - 1; state.ty = e.clientY / innerHeight * 2 - 1;
  }, { passive: true });
  if (typeof DeviceOrientationEvent !== "undefined" && typeof DeviceOrientationEvent.requestPermission !== "function") {
    addEventListener("deviceorientation", function (e) {
      if (e.gamma == null) return;
      state.tx = Math.max(-1, Math.min(1, e.gamma / 30)); state.ty = Math.max(-1, Math.min(1, (e.beta - 45) / 30));
    }, { passive: true });
  }

  /* ---------- Three.js hero: two lights orbit -> heart-knot ---------- */
  var glOff = null;
  function startThree() {
    var canvas = $(".hero-gl"); if (!canvas) return;
    import(THREE_URL).then(function (T) {
      var renderer;
      try { renderer = new T.WebGLRenderer({ canvas: canvas, alpha: true, antialias: false, powerPreference: "low-power" }); }
      catch (e) { goStatic(true); return; }
      var N = lowHW ? 380 : 900;
      renderer.setPixelRatio(Math.min(devicePixelRatio || 1, lowHW ? 1 : 1.75));
      var scene = new T.Scene(), cam = new T.PerspectiveCamera(45, 1, 0.1, 50); cam.position.z = 5;
      var tex = (function () {
        var c = doc.createElement("canvas"); c.width = c.height = 64; var g = c.getContext("2d");
        var gr = g.createRadialGradient(32, 32, 0, 32, 32, 32);
        gr.addColorStop(0, "rgba(255,255,255,1)"); gr.addColorStop(.25, "rgba(255,255,255,.55)"); gr.addColorStop(1, "rgba(255,255,255,0)");
        g.fillStyle = gr; g.fillRect(0, 0, 64, 64); return new T.CanvasTexture(c);
      })();
      var group = new T.Group(); scene.add(group);
      var cols = [new T.Color("#f3d9b1"), new T.Color("#ff9a86")];
      var sets = [1, -1].map(function (sgn, k) {
        var pos = new Float32Array(N * 3), col = new Float32Array(N * 3), tailPos = new Float32Array(3);
        for (var i = 0; i < N; i++) { var f = Math.pow(1 - i / N, 1.1); col[i * 3] = cols[k].r * f; col[i * 3 + 1] = cols[k].g * f; col[i * 3 + 2] = cols[k].b * f; }
        var geo = new T.BufferGeometry();
        geo.setAttribute("position", new T.BufferAttribute(pos, 3)); geo.setAttribute("color", new T.BufferAttribute(col, 3));
        var pts = new T.Points(geo, new T.PointsMaterial({ size: lowHW ? .13 : .1, map: tex, vertexColors: true, transparent: true, depthWrite: false, blending: T.AdditiveBlending, sizeAttenuation: true }));
        group.add(pts);
        var head = new T.Sprite(new T.SpriteMaterial({ map: tex, color: cols[k], transparent: true, blending: T.AdditiveBlending, depthWrite: false }));
        head.scale.set(.55, .55, 1); group.add(head);
        return { sgn: sgn, pos: pos, geo: geo, pts: pts, head: head, k: k };
      });
      function path(u, sgn, m, out, o) {
        /* orbit: tilted circle; heart: parametric heart with twisting ribbon offset (matches SVG fallback) */
        var tilt = .9 * sgn, r = 1.5, c = Math.cos(u), s = Math.sin(u);
        var ox = r * c, oy = r * s * Math.cos(tilt), oz = r * s * Math.sin(tilt);
        var t = u + 1e-4, st = Math.sin(t), ct = Math.cos(t);
        var hx = 16 * st * st * st, hy = 13 * ct - 5 * Math.cos(2 * t) - 2 * Math.cos(3 * t) - Math.cos(4 * t);
        var dx = 48 * st * st * ct, dy = -13 * st + 10 * Math.sin(2 * t) + 6 * Math.sin(3 * t) + 4 * Math.sin(4 * t);
        var n = Math.hypot(dx, dy) || 1, w = sgn * .5 * Math.sin(3 * t);
        var X = (hx + w * dy / n) * .095, Y = (hy - w * dx / n) * .095 + .08, Z = sgn * .2 * Math.cos(3 * t);
        out[o] = ox + (X - ox) * m; out[o + 1] = oy + (Y - oy) * m; out[o + 2] = oz + (Z - oz) * m;
      }
      function size() {
        var w = canvas.clientWidth || 400, h = canvas.clientHeight || 400;
        renderer.setSize(w, h, false); cam.aspect = w / h; cam.updateProjectionMatrix();
      }
      size(); addEventListener("resize", size);
      var visible = true, t0 = performance.now(), frames = 0, slow = 0, last = t0, dead = false, tmp = new Float32Array(3);
      new IntersectionObserver(function (e) { visible = e[0].isIntersecting; }).observe(canvas);
      function kill() { dead = true; root.classList.remove("gl-on"); renderer.dispose(); goStatic(true); }
      canvas.addEventListener("webglcontextlost", function (e) { e.preventDefault(); kill(); });
      function frame(now) {
        if (dead) return; requestAnimationFrame(frame);
        if (!visible || doc.hidden) { last = now; return; }
        var dt = now - last; last = now; frames++;
        if (frames > 20 && frames <= 80) { slow += dt; if (frames === 80 && slow / 60 > 90) return kill(); } /* avg < ~11fps over 60 frames */
        var time = (now - t0) / 1000;
        var m = state.p; m = m < .1 ? 0 : m > .6 ? 1 : (m - .1) / .5; m = m * m * (3 - 2 * m);
        state.mx += (state.tx - state.mx) * .06; state.my += (state.ty - state.my) * .06;
        root.style.setProperty("--mx", state.mx.toFixed(3)); root.style.setProperty("--my", state.my.toFixed(3));
        var speed = (1 - m) * .9 + .25, head = time * speed;
        for (var k = 0; k < 2; k++) {
          var S = sets[k], ph = k * Math.PI;
          for (var i = 0; i < N; i++) {
            var u = head + ph - (i / N) * (m > .5 ? 5.9 : 5.2);
            path(u, S.sgn, m, S.pos, i * 3);
          }
          S.geo.attributes.position.needsUpdate = true;
          S.head.position.set(S.pos[0], S.pos[1], S.pos[2]);
        }
        group.rotation.y = state.mx * .35 + Math.sin(time * .3) * .08 * (1 - m);
        group.rotation.x = state.my * .2;
        group.scale.setScalar(1 + m * .12 * Math.sin(time * 2.2));
        renderer.render(scene, cam);
        if (frames === 2) root.classList.add("gl-on");
      }
      requestAnimationFrame(frame);
      glOff = kill;
    }).catch(function () { goStatic(true); });
  }

  /* ---------- GSAP / ScrollTrigger / Lenis ---------- */
  function startMotion() {
    var g = window.gsap, ST = window.ScrollTrigger; if (!g || !ST) return;
    g.registerPlugin(ST);
    useST = true;
    if (window.Lenis && !lowHW) {
      var lenis = new window.Lenis({ lerp: .1 });
      lenis.on("scroll", ST.update);
      g.ticker.add(function (t) { lenis.raf(t * 1000); });
      g.ticker.lagSmoothing(0);
    }
    ST.create({ trigger: ".hero", start: "top top", end: "bottom bottom", scrub: .6,
      onUpdate: function (s) { state.p = s.progress; paintStages(); } });
    /* split-text reveal: words slide up; text stays visible (transform only) */
    var h1 = $(".split");
    if (h1) {
      var words = h1.textContent.trim().split(/\s+/); h1.textContent = "";
      words.forEach(function (w, i) {
        var s = doc.createElement("span"); s.className = "w"; s.textContent = w; h1.appendChild(s);
        if (i < words.length - 1) h1.appendChild(doc.createTextNode(" "));
      });
      g.from(".split .w", { yPercent: 40, duration: 1.1, ease: "expo.out", stagger: .09 });
    }
    g.from(".why-list li, .prog, .priv-list li", { y: 28, duration: 1, ease: "expo.out", stagger: .06,
      scrollTrigger: { trigger: ".sec-why", start: "top 85%", once: true } });
    var tl = $(".timeline");
    if (tl) g.fromTo(tl, { "--tl": 0 }, { "--tl": 1, ease: "none", scrollTrigger: { trigger: tl, start: "top 80%", end: "bottom 60%", scrub: true } });
    var sh = $(".shield path");
    if (sh) g.from(".shield", { opacity: .2, scale: .9, duration: 1.2, ease: "expo.out", scrollTrigger: { trigger: ".sec-priv", start: "top 80%", once: true } });
  }

  /* ---------- micro-interactions (fine pointer only) ---------- */
  function startPointerFx() {
    if (!matchMedia("(hover:hover) and (pointer:fine)").matches) return;
    $$(".magnetic").forEach(function (b) {
      b.addEventListener("pointermove", function (e) {
        var r = b.getBoundingClientRect();
        b.style.transform = "translate(" + ((e.clientX - r.left - r.width / 2) * .18).toFixed(1) + "px," + ((e.clientY - r.top - r.height / 2) * .25).toFixed(1) + "px)";
      });
      b.addEventListener("pointerleave", function () { b.style.transform = ""; });
    });
    $$(".tilt").forEach(function (c) {
      c.addEventListener("pointermove", function (e) {
        var r = c.getBoundingClientRect(), x = (e.clientX - r.left) / r.width - .5, y = (e.clientY - r.top) / r.height - .5;
        c.style.transform = "rotateY(" + (x * 10).toFixed(1) + "deg) rotateX(" + (-y * 10).toFixed(1) + "deg) translateZ(0)";
      });
      c.addEventListener("pointerleave", function () { c.style.transform = ""; });
    });
  }

  /* lazy: wait for load + idle so text/CTA paint first and Three never blocks LCP/TBT */
  function boot() {
    startPointerFx();
    var chain = loadScript(libs.gsap).then(function () { return loadScript(libs.st); })
      .then(function () { return loadScript(libs.lenis).catch(function () {}); })
      .then(startMotion).catch(function () { /* CDN blocked: page stays fully readable */ });
    /* Three.js is the heaviest asset: wait for first interaction or 4s so it never competes with load/TBT */
    var fire = function () { ["scroll", "pointerdown", "keydown", "touchstart"].forEach(function (ev) { removeEventListener(ev, fire); }); clearTimeout(tm); chain.then(startThree); };
    var tm = setTimeout(fire, 4000);
    ["scroll", "pointerdown", "keydown", "touchstart"].forEach(function (ev) { addEventListener(ev, fire, { passive: true, once: true }); });
  }
  var go = function () { (window.requestIdleCallback || function (f) { setTimeout(f, 300); })(boot, { timeout: 2500 }); };
  if (doc.readyState === "complete") go(); else addEventListener("load", go);
})();
