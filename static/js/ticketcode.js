// Explicit reveal/copy of the check-in code (manual fallback). Fetched on click only; never stored, logged or put in a URL.
(function () {
  var b = document.getElementById('showcode'), box = document.getElementById('codebox'), inp = document.getElementById('code'),
      msg = document.getElementById('codemsg');
  if (!b) return;
  b.addEventListener('click', function () {
    fetch(b.dataset.url, { credentials: 'same-origin', cache: 'no-store' }).then(function (r) { if (!r.ok) throw 0; return r.text(); })
      .then(function (t) { inp.value = t; box.hidden = false; inp.focus(); inp.select(); })
      .catch(function () { msg.textContent = 'Gagal menampilkan kode.'; box.hidden = false; });
  });
  document.getElementById('copycode').addEventListener('click', function () {
    inp.focus(); inp.select();
    var ok = function () { msg.textContent = 'Tersalin.'; }, manual = function () { msg.textContent = 'Salin manual: kode sudah terpilih.'; };
    if (navigator.clipboard && window.isSecureContext) navigator.clipboard.writeText(inp.value).then(ok, manual);
    else { try { document.execCommand('copy') ? ok() : manual(); } catch (e) { manual(); } }
  });
})();
