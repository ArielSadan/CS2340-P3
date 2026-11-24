(function () {
  "use strict";

  var isSignup = !!document.querySelector("input[name=password1]");
  document.querySelectorAll("select[data-detect-timezone]").forEach(function (select) {
    if (!isSignup) return;
    var detected;
    try { detected = Intl.DateTimeFormat().resolvedOptions().timeZone; } catch (e) { return; }
    if (!detected) return;
    for (var i = 0; i < select.options.length; i++) {
      if (select.options[i].value === detected) { select.value = detected; break; }
    }
  });

  document.querySelectorAll("[data-times]").forEach(function (box) {
    var button = box.querySelector("[data-add-time]");
    if (!button) return;
    function update() {
      button.hidden = !box.querySelector("[data-time].is-hidden");
    }
    button.addEventListener("click", function () {
      var next = box.querySelector("[data-time].is-hidden");
      if (next) {
        next.classList.remove("is-hidden");
        var input = next.querySelector("input");
        if (input) input.focus();
      }
      update();
    });
    update();
  });

  document.querySelectorAll("form[data-confirm]").forEach(function (form) {
    form.addEventListener("submit", function (event) {
      if (!window.confirm(form.dataset.confirm)) event.preventDefault();
    });
  });

  document.querySelectorAll("[data-copy]").forEach(function (button) {
    button.addEventListener("click", function () {
      var input = document.querySelector(button.dataset.copy);
      if (!input) return;
      input.select();
      var done = function () { button.textContent = "Copied"; };
      if (navigator.clipboard) {
        navigator.clipboard.writeText(input.value).then(done, function () { document.execCommand("copy"); done(); });
      } else {
        document.execCommand("copy");
        done();
      }
    });
  });

  document.querySelectorAll("form[data-busy-text]").forEach(function (form) {
    form.addEventListener("submit", function () {
      var button = form.querySelector("button[type=submit]");
      if (button) {
        button.disabled = true;
        button.textContent = form.dataset.busyText;
      }
    });
  });
})();