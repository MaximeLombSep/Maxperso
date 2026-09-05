/* Interactions de l'interface.
   Pas de dépendance externe, pas de code en ligne : la politique CSP
   n'autorise que les scripts servis par l'application elle-même. */

(function () {
  "use strict";

  function csrf() {
    var match = document.cookie.match(/(?:^|;\s*)enveloppe_csrf=([^;]+)/);
    return match ? decodeURIComponent(match[1]) : "";
  }

  function postJSON(url, payload) {
    return fetch(url, {
      method: "POST",
      headers: { "Content-Type": "application/json", "X-CSRF-Token": csrf() },
      body: JSON.stringify(payload),
      credentials: "same-origin",
    }).then(function (response) {
      if (!response.ok) throw new Error("HTTP " + response.status);
      return response.json();
    });
  }

  function flash(message, level) {
    var node = document.createElement("div");
    node.className = "flash " + (level || "success");
    node.textContent = message;
    document.body.appendChild(node);
    setTimeout(function () { node.remove(); }, 3200);
  }

  /* --- Thème -------------------------------------------------------- */

  function initTheme() {
    document.querySelectorAll("[data-theme-toggle]").forEach(function (button) {
      button.addEventListener("click", function () {
        var root = document.documentElement;
        var isDark = root.getAttribute("data-theme") === "dark" ||
          (!root.getAttribute("data-theme") &&
            window.matchMedia("(prefers-color-scheme: dark)").matches);
        var next = isDark ? "light" : "dark";
        root.setAttribute("data-theme", next);
        try { localStorage.setItem("enveloppe-theme", next); } catch (e) { /* ignoré */ }
      });
    });
  }

  /* --- Feuille de navigation (téléphone) ----------------------------- */

  function initSheet() {
    var sheet = document.querySelector("[data-sheet]");
    if (!sheet) return;

    function close() { sheet.classList.add("hidden"); }
    function open() { sheet.classList.remove("hidden"); }

    document.querySelectorAll("[data-sheet-open]").forEach(function (button) {
      button.addEventListener("click", open);
    });
    sheet.querySelectorAll("[data-sheet-close]").forEach(function (node) {
      node.addEventListener("click", close);
    });
    document.addEventListener("keydown", function (event) {
      if (event.key === "Escape") close();
    });
  }

  /* --- Dotation des enveloppes (édition en ligne) -------------------- */

  function initAllocations() {
    var inputs = document.querySelectorAll("[data-alloc]");
    if (!inputs.length) return;
    var endpoint = document.body.getAttribute("data-alloc-url");

    inputs.forEach(function (input) {
      var initial = input.value;

      function save() {
        if (input.value === initial) return;
        var payload = {
          envelope_id: input.getAttribute("data-envelope"),
          period: input.getAttribute("data-period"),
          amount: input.value || "0",
        };
        input.disabled = true;
        postJSON(endpoint, payload)
          .then(function (data) {
            initial = input.value;
            input.disabled = false;
            input.classList.add("saved");
            setTimeout(function () { input.classList.remove("saved"); }, 900);

            var available = document.querySelector(
              '[data-available="' + payload.envelope_id + '"]'
            );
            if (available) {
              available.textContent = data.available;
              ["funded", "underfunded", "overspent", "none", "income"].forEach(
                function (name) { available.classList.remove(name); }
              );
              available.classList.add(data.funding || "none");
            }

            // « Il manque X » doit disparaître dès que la dotation le couvre.
            var missing = document.querySelector(
              '[data-missing="' + payload.envelope_id + '"]'
            );
            if (missing) {
              missing.textContent = " · il manque " + data.missing;
              missing.hidden = data.missing_cents <= 0;
            }

            var toBudget = document.querySelector("[data-to-budget]");
            if (toBudget) {
              toBudget.textContent = data.to_budget;
            }
            var banner = document.querySelector(".assign");
            if (banner) {
              ["ready", "zero", "over"].forEach(function (name) {
                banner.classList.remove(name);
              });
              banner.classList.add(data.to_budget_state);
              var state = banner.querySelector(".assign-state");
              if (state) state.textContent = data.to_budget_label;
            }
          })
          .catch(function () {
            input.disabled = false;
            input.value = initial;
            flash("Dotation non enregistrée : rechargez la page.", "error");
          });
      }

      input.addEventListener("blur", save);
      input.addEventListener("keydown", function (event) {
        if (event.key === "Enter") { event.preventDefault(); input.blur(); }
        if (event.key === "Escape") { input.value = initial; input.blur(); }
      });
    });
  }

  /* --- Affectation d'une opération ---------------------------------- */

  function initAssign() {
    var selects = document.querySelectorAll("[data-assign]");
    if (!selects.length) return;
    var endpoint = document.body.getAttribute("data-assign-url");

    selects.forEach(function (select) {
      select.addEventListener("change", function () {
        var row = select.closest("tr");
        var learn = row && row.querySelector("[data-learn]");
        postJSON(endpoint, {
          transaction_id: select.getAttribute("data-transaction"),
          envelope_id: select.value,
          learn: learn ? learn.checked : false,
        })
          .then(function (data) {
            if (row) row.classList.add("is-selected");
            setTimeout(function () { if (row) row.classList.remove("is-selected"); }, 700);
            if (data.learned) flash('Règle apprise : « ' + data.learned + ' »');
          })
          .catch(function () { flash("Affectation non enregistrée.", "error"); });
      });
    });
  }

  /* --- Sélection multiple ------------------------------------------- */

  function initBulk() {
    var master = document.querySelector("[data-check-all]");
    var boxes = document.querySelectorAll("[data-row-check]");
    var bar = document.querySelector("[data-bulk-bar]");
    if (!boxes.length) return;

    function refresh() {
      var count = 0;
      boxes.forEach(function (box) {
        if (box.checked) count++;
        var row = box.closest("tr");
        if (row) row.classList.toggle("is-selected", box.checked);
      });
      if (bar) {
        bar.classList.toggle("hidden", count === 0);
        var label = bar.querySelector("[data-bulk-count]");
        if (label) label.textContent = count;
      }
    }

    if (master) {
      master.addEventListener("change", function () {
        boxes.forEach(function (box) { box.checked = master.checked; });
        refresh();
      });
    }
    boxes.forEach(function (box) { box.addEventListener("change", refresh); });
    refresh();
  }

  /* --- Formulaires repliables et confirmations ---------------------- */

  function initToggles() {
    document.querySelectorAll("[data-toggle]").forEach(function (button) {
      button.addEventListener("click", function () {
        var target = document.querySelector(button.getAttribute("data-toggle"));
        if (!target) return;
        target.classList.toggle("hidden");
        if (!target.classList.contains("hidden")) {
          var first = target.querySelector("input, select, textarea");
          if (first) first.focus();
        }
      });
    });

    // Repli propre au téléphone : la classe n'a aucun effet sur grand écran,
    // où le bloc reste affiché en permanence.
    document.querySelectorAll("[data-mtoggle]").forEach(function (button) {
      button.addEventListener("click", function () {
        var target = document.querySelector(button.getAttribute("data-mtoggle"));
        if (target) target.classList.toggle("m-open");
      });
    });

    document.querySelectorAll("[data-confirm]").forEach(function (form) {
      form.addEventListener("submit", function (event) {
        if (!window.confirm(form.getAttribute("data-confirm"))) event.preventDefault();
      });
    });
  }

  /* --- Couvrir un dépassement --------------------------------------- */

  function initCover() {
    var form = document.querySelector("#move-money");
    if (!form) return;

    document.querySelectorAll("[data-cover]").forEach(function (button) {
      button.addEventListener("click", function () {
        var target = form.querySelector("[name=target_id]");
        var amount = form.querySelector("[name=amount]");
        if (target) target.value = button.getAttribute("data-cover");
        if (amount) amount.value = button.getAttribute("data-cover-amount");
        form.classList.remove("hidden");
        form.scrollIntoView({ behavior: "smooth", block: "center" });
        var source = form.querySelector("[name=source_id]");
        if (source) source.focus();
      });
    });
  }

  /* --- Filtres auto-soumis ------------------------------------------ */

  function initFilters() {
    document.querySelectorAll("[data-autosubmit]").forEach(function (control) {
      control.addEventListener("change", function () {
        var form = control.closest("form");
        if (form) form.submit();
      });
    });
  }

  document.addEventListener("DOMContentLoaded", function () {
    initTheme();
    initSheet();
    initAllocations();
    initAssign();
    initBulk();
    initToggles();
    initCover();
    initFilters();
  });

  // ----- Rapprochement : cocher toute la liste d'un geste ------------------
  document.addEventListener("click", function (event) {
    var trigger = event.target.closest("[data-check-all]");
    if (!trigger) return;
    var list = document.querySelector(trigger.getAttribute("data-check-all"));
    if (!list) return;
    var boxes = list.querySelectorAll('input[type="checkbox"]');
    var allChecked = Array.prototype.every.call(boxes, function (box) {
      return box.checked;
    });
    Array.prototype.forEach.call(boxes, function (box) {
      box.checked = !allChecked;
    });
    trigger.textContent = allChecked ? "Tout cocher" : "Tout décocher";
  });

})();
