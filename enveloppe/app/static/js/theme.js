/* Applique le thème avant le premier rendu pour éviter tout clignotement. */
(function () {
  try {
    var saved = localStorage.getItem("enveloppe-theme");
    if (saved === "dark" || saved === "light") {
      document.documentElement.setAttribute("data-theme", saved);
    }
  } catch (e) {
    /* stockage indisponible (navigation privée) : on garde le thème système */
  }
})();
