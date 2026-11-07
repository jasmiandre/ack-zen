// Camera scanner via native BarcodeDetector only (no third-party code). Falls back to the manual form.
(function () {
  var form = document.getElementById('manual'), res = document.getElementById('result'),
      note = document.getElementById('camnote'), start = document.getElementById('start'),
      cam = document.getElementById('cam'), video = document.getElementById('video'),
      stream = null, busy = false, timer = null;
  function stop() {
    if (timer) { clearInterval(timer); timer = null; }
    if (stream) { stream.getTracks().forEach(function (t) { t.stop(); }); stream = null; }
    video.srcObject = null; cam.hidden = true; start.hidden = !supported;
  }
  var supported = 'BarcodeDetector' in window && navigator.mediaDevices && window.isSecureContext;
  if (!supported) { note.textContent = 'Kamera/pemindai tidak tersedia di peramban ini (butuh HTTPS + BarcodeDetector). Gunakan entri manual.'; }
  else { start.hidden = false; }
  function send(tok) {
    busy = true;
    var body = new URLSearchParams({ csrf_token: form.csrf_token.value, token: tok });
    return fetch(form.action, { method: 'POST', body: body, credentials: 'same-origin', headers: { Accept: 'application/json' } })
      .then(function (r) { return r.json(); })
      .then(function (j) { res.textContent = j.message + (j.member ? ' (' + j.member + ')' : ''); })
      .catch(function () { res.textContent = 'Gagal menghubungi server.'; })
      .then(function () { setTimeout(function () { busy = false; }, 2000); });
  }
  form.addEventListener('submit', function (ev) {
    ev.preventDefault();
    var t = form.token.value; form.token.value = ''; send(t);
  });
  start.addEventListener('click', function () {
    var det;
    try { det = new BarcodeDetector({ formats: ['qr_code'] }); } catch (e) { note.textContent = 'Pemindai tidak tersedia. Gunakan entri manual.'; return; }
    navigator.mediaDevices.getUserMedia({ video: { facingMode: 'environment' }, audio: false }).then(function (s) {
      stream = s; video.srcObject = s; video.play(); cam.hidden = false; start.hidden = true;
      timer = setInterval(function () {
        if (busy) return;
        det.detect(video).then(function (codes) { if (codes.length && !busy) send(codes[0].rawValue); }).catch(function () {});
      }, 400);
    }).catch(function () { note.textContent = 'Izin kamera ditolak/tidak tersedia. Gunakan entri manual.'; });
  });
  document.getElementById('stop').addEventListener('click', stop);
  window.addEventListener('pagehide', stop);
})();
