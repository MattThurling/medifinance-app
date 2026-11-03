// Searchable FK / M2M combobox behaviour. HTMX swaps result <button>s into
// .combobox-results; this wires selection (FK or M2M chip), removal, and close.
//
// Container variants:
//   .combobox         — single-FK: result click sets a hidden <input> value.
//   .m2m-combobox     — multi: result click APPENDS a chip + hidden input.
//                       data-name="<field>" on the wrapper specifies the form
//                       field name to use for the hidden inputs.
//
// Quick-create ("+ New" beside a single-FK combobox, see _combobox.html
// `create_url` and _quick_create_modal.html): the button opens the
// <dialog class="quick-create" data-for="<field>"> and HTMX loads the slim
// form into it. On success the server answers 204 + HX-Trigger
// {"quickCreated": {id, label}}; we select that record and close the dialog.
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

  // "+ New" opens its dialog (the form fragment arrives via the button's own
  // hx-get); Cancel inside the dialog closes it.
  document.addEventListener("click", function (e) {
    const opener = e.target.closest(".combobox-new");
    if (opener) {
      const dialog = document.getElementById(opener.dataset.dialog);
      if (dialog && !dialog.open) dialog.showModal();
      return;
    }
    const cancel = e.target.closest(".quick-create-cancel");
    if (cancel) {
      const dialog = cancel.closest("dialog");
      if (dialog) dialog.close();
    }
  });

  // HX-Trigger fires `quickCreated` on the dialog's form; it bubbles here.
  document.addEventListener("quickCreated", function (e) {
    const dialog = e.target.closest("dialog.quick-create");
    if (!dialog) return;
    const hidden = document.querySelector(
      `.combobox input[type="hidden"][name="${dialog.dataset.for}"]`
    );
    const box = hidden && hidden.closest(".combobox");
    if (box) {
      hidden.value = e.detail.id;
      const search = box.querySelector(".combobox-search");
      if (search) search.value = e.detail.label;
      const results = box.querySelector(".combobox-results");
      if (results) results.innerHTML = "";
    }
    dialog.close();
  });

  // `close` doesn't bubble, so capture it: reset the body so a cancelled
  // attempt's errors don't flash on the next open.
  document.addEventListener(
    "close",
    function (e) {
      const dialog = e.target;
      if (!(dialog instanceof HTMLDialogElement) || !dialog.classList.contains("quick-create")) return;
      const body = dialog.querySelector(".quick-create-body");
      if (body) body.innerHTML = '<span class="loading loading-spinner"></span>';
    },
    true
  );
})();
