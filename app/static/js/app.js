(function () {
  "use strict";

  // Confirmação antes de excluir
  document.addEventListener("submit", function (e) {
    var msg = e.target.getAttribute && e.target.getAttribute("data-confirm");
    if (msg && !window.confirm(msg)) e.preventDefault();
  });

  // Mostrar/ocultar senha
  document.querySelectorAll("[data-toggle-password]").forEach(function (btn) {
    btn.addEventListener("click", function () {
      var input = document.querySelector(btn.getAttribute("data-toggle-password"));
      var show = input.type === "password";
      input.type = show ? "text" : "password";
      btn.textContent = show ? "Ocultar" : "Mostrar";
      btn.setAttribute("aria-pressed", String(show));
    });
  });

  // Filtros: aplica ao mudar select/data; texto e valores ao sair do campo ou Enter
  document.querySelectorAll("form[data-autosubmit]").forEach(function (form) {
    form.querySelectorAll("select, input[type=date]").forEach(function (el) {
      el.addEventListener("change", function () { form.submit(); });
    });
  });

  // Mensagens de sucesso somem sozinhas
  document.querySelectorAll("[data-flash].ok").forEach(function (el) {
    setTimeout(function () { el.style.transition = "opacity .4s"; el.style.opacity = "0"; }, 5000);
    setTimeout(function () { el.remove(); }, 5500);
  });

  // Envio de PDFs: arrastar e soltar, lista dos escolhidos, trava contra clique duplo
  var form = document.querySelector("form[data-upload]");
  if (!form) return;
  var input = form.querySelector("input[type=file]");
  var drop = form.querySelector("[data-drop]");
  var list = form.querySelector("[data-picked]");
  var submit = form.querySelector("[data-submit]");

  function size(n) { return n > 1048576 ? (n / 1048576).toFixed(1) + " MB" : Math.max(1, Math.round(n / 1024)) + " KB"; }
  function render() {
    list.textContent = "";
    var files = Array.prototype.slice.call(input.files || []);
    if (!files.length) {
      var li = document.createElement("li"); li.className = "muted"; li.textContent = "Nenhum arquivo escolhido."; list.appendChild(li);
    }
    files.forEach(function (f) {
      var li = document.createElement("li"), a = document.createElement("span"), b = document.createElement("span");
      a.textContent = f.name; b.className = "muted"; b.textContent = size(f.size); li.append(a, b); list.appendChild(li);
    });
    submit.disabled = !files.length;
  }
  input.addEventListener("change", render);
  ["dragenter", "dragover"].forEach(function (t) {
    drop.addEventListener(t, function (e) { e.preventDefault(); drop.classList.add("over"); });
  });
  ["dragleave", "drop"].forEach(function (t) {
    drop.addEventListener(t, function (e) { e.preventDefault(); drop.classList.remove("over"); });
  });
  drop.addEventListener("drop", function (e) {
    var dt = new DataTransfer();
    Array.prototype.forEach.call(e.dataTransfer.files, function (f) {
      if (/\.pdf$/i.test(f.name) || f.type === "application/pdf") dt.items.add(f);
    });
    input.files = dt.files; render();
  });
  // Envio em lotes: o navegador manda 10 PDFs por vez, mostra o andamento e junta o resultado.
  var BATCH = 10;
  var live = document.querySelector("[data-live]");
  form.addEventListener("submit", function (e) {
    var files = Array.prototype.slice.call(input.files || []);
    if (!files.length || !window.fetch || !window.FormData) { submit.disabled = true; return; }
    e.preventDefault();
    var token = form.querySelector("input[name=csrf]").value;
    var title = live.querySelector("[data-live-title]"), bar = live.querySelector("[data-live-bar]");
    var out = live.querySelector("[data-live-list]"), reload = live.querySelector("[data-live-reload]");
    var counts = { ok: 0, duplicate: 0, invalid: 0 };
    var labels = { ok: "Lançada", duplicate: "Duplicada", invalid: "Recusada" };
    var cls = { ok: "ok", duplicate: "warn", invalid: "bad" };
    submit.disabled = true; input.disabled = true; out.textContent = ""; reload.hidden = true;
    live.hidden = false; bar.max = files.length; bar.value = 0;
    live.scrollIntoView({ behavior: "smooth", block: "nearest" });

    function row(status, name, msg) {
      var li = document.createElement("li"), b = document.createElement("span"), n = document.createElement("span"), m = document.createElement("span");
      li.className = "res " + (status === "error" ? "invalid fail" : status);
      b.className = "badge " + (cls[status] || "bad"); b.textContent = labels[status] || "Falhou";
      n.className = "fname"; n.textContent = name; m.className = "rmsg"; m.textContent = msg;
      li.append(b, n, m); out.appendChild(li);
    }
    function finish(stopped) {
      title.textContent = (stopped ? "Interrompido: " : "Concluído: ") + counts.ok + " lançada(s), " + counts.duplicate + " duplicada(s), " + counts.invalid + " recusada(s)";
      reload.hidden = false; input.disabled = false; input.value = ""; render();
    }
    reload.onclick = function () { window.location.reload(); };

    var i = 0;
    (function next() {
      if (i >= files.length) return finish(false);
      var chunk = files.slice(i, i + BATCH), fd = new FormData();
      fd.append("csrf", token);
      chunk.forEach(function (f) { fd.append("files", f, f.name); });
      title.textContent = "Lançando " + Math.min(i + BATCH, files.length) + " de " + files.length + "…";
      fetch("/admin/enviar", { method: "POST", body: fd, headers: { Accept: "application/json" }, credentials: "same-origin" })
        .then(function (res) {
          var ct = res.headers.get("content-type") || "";
          if (!res.ok || res.redirected || ct.indexOf("application/json") < 0) throw new Error(res.status === 403 || res.redirected ? "Sessão expirada. Recarregue a página e entre de novo." : "Erro " + res.status + " no servidor.");
          return res.json();
        })
        .then(function (data) {
          data.results.forEach(function (r) { counts[r.status] = (counts[r.status] || 0) + 1; row(r.status, r.filename, r.message); });
          i += BATCH; bar.value = Math.min(i, files.length); next();
        })
        .catch(function (err) { row("error", chunk[0].name + (chunk.length > 1 ? " e mais " + (chunk.length - 1) : ""), err.message); finish(true); });
    })();
  });
})();
