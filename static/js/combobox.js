// Searchable FK / M2M combobox behaviour. HTMX swaps result <button>s into
// .combobox-results; this wires selection (FK or M2M chip), removal, and close.
//
// Container variants:
//   .combobox         — single-FK: result click sets a hidden <input> value.
//   .m2m-combobox     — multi: result click APPENDS a chip + hidden input.
//                       data-name="<field>" on the wrapper specifies the form
//                       field name to use for the hidden inputs.
(function () {
  document.addEventListener("click", function (e) {
    // Chip remove (M2M)
    if (e.target.classList.contains("m2m-remove")) {
      const chip = e.target.closest("[data-id]");
      if (chip) chip.remove();
      return;
    }

    const option = e.target.closest(".combobox-option");
    if (option) {
      const m2m = option.closest(".m2m-combobox");
      if (m2m) {
        // M2M: append a chip unless this id is already selected, then clear search.
        const id = option.dataset.id;
        const label = option.dataset.label;
        const chips = m2m.querySelector(".m2m-chips");
        const name = m2m.dataset.name;
        if (!chips.querySelector(`[data-id="${id}"]`)) {
          const chip = document.createElement("span");
          chip.className = "badge badge-lg gap-1";
          chip.dataset.id = id;
          chip.appendChild(document.createTextNode(label));
          const btn = document.createElement("button");
          btn.type = "button";
          btn.className = "m2m-remove ml-1";
          btn.setAttribute("aria-label", "Remove");
          btn.textContent = "×";
          chip.appendChild(btn);
          const hidden = document.createElement("input");
          hidden.type = "hidden";
          hidden.name = name;
          hidden.value = id;
          chip.appendChild(hidden);
          chips.appendChild(chip);
        }
        const search = m2m.querySelector(".combobox-search");
        if (search) search.value = "";
        const results = m2m.querySelector(".combobox-results");
        if (results) results.innerHTML = "";
        return;
      }

      // Single-FK combobox: store the pk + show its label.
      const box = option.closest(".combobox");
      if (box) {
        const hidden = box.querySelector('input[type="hidden"]');
        if (hidden) hidden.value = option.dataset.id;
        const search = box.querySelector(".combobox-search");
        if (search) search.value = option.dataset.label;
        const results = box.querySelector(".combobox-results");
        if (results) results.innerHTML = "";
      }
      return;
    }

    // Click outside any combobox closes open result lists.
    if (!e.target.closest(".combobox") && !e.target.closest(".m2m-combobox")) {
      document.querySelectorAll(".combobox-results").forEach((ul) => (ul.innerHTML = ""));
    }
  });

  // Typing in a single-FK combobox invalidates the prior selection until a
  // result is picked again. For the M2M variant, typing only searches — chips
  // stay until explicitly removed.
  document.addEventListener("input", function (e) {
    if (!e.target.classList.contains("combobox-search")) return;
    if (e.target.closest(".m2m-combobox")) return;
    const box = e.target.closest(".combobox");
    const hidden = box && box.querySelector('input[type="hidden"]');
    if (hidden) hidden.value = "";
  });
})();
