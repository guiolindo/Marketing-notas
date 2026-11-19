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
  form.addEventListener("submit", function () { submit.disabled = true; submit.textContent = "Lançando…"; });
})();
