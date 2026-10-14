// Pole wyszukiwania: podpowiedzi nazw od 3 liter i skanowanie kodów aparatem telefonu.
(() => {
  const input = document.getElementById("q");
  if (!input) return;
  const form = input.form;
  const list = document.getElementById("suggestions");
  const prefixes = input.dataset.prefixes || "@";
  const MIN_CHARS = 3;
  let timer = null, active = -1, lastQuery = "", controller = null;

  // Kod (GTIN, Data Matrix) nie wymaga podpowiedzi: prefiks skanera, "]d2", "(01)", separator GS albo
  // co najmniej 8 cyfr na początku. Nazwy zaczynające się od cyfry ("0,9% Sodium…") dostają podpowiedzi.
  const looksLikeCode = (v) => v.length > 0 &&
    (prefixes.includes(v[0]) || /^(\]|\(01\)|\d{8})/.test(v) || v.includes("\x1d"));

  function hide() {
    list.hidden = true;
    list.innerHTML = "";
    input.setAttribute("aria-expanded", "false");
    input.removeAttribute("aria-activedescendant");
    active = -1;
  }

  function choose(name) {
    input.value = name;
    hide();
    form.submit();
  }

  function render(names) {
    list.innerHTML = "";
    names.forEach((name, i) => {
      const li = document.createElement("li");
      li.id = "suggestion-" + i;
      li.setAttribute("role", "option");
      li.textContent = name;
      li.addEventListener("mousedown", (e) => { e.preventDefault(); choose(name); });
      list.appendChild(li);
    });
    const show = names.length > 0;
    list.hidden = !show;
    input.setAttribute("aria-expanded", String(show));
    active = -1;
  }

  async function fetchSuggestions(value) {
    if (controller) controller.abort();
    controller = new AbortController();
    try {
      const url = input.dataset.suggestUrl + "?q=" + encodeURIComponent(value);
      const res = await fetch(url, { signal: controller.signal });
      if (!res.ok) return;
      const names = await res.json();
      if (input.value.trim() === value) render(names);
    } catch (err) {
      if (err.name !== "AbortError") hide();
    }
  }

  input.addEventListener("input", () => {
    const value = input.value.trim();
    clearTimeout(timer);
    if (value.length < MIN_CHARS || looksLikeCode(value)) { hide(); return; }
    if (value === lastQuery && !list.hidden) return;
    lastQuery = value;
    timer = setTimeout(() => fetchSuggestions(value), 200);
  });

  input.addEventListener("keydown", (e) => {
    const items = list.querySelectorAll("li");
    if (list.hidden || !items.length) return;
    if (e.key === "ArrowDown" || e.key === "ArrowUp") {
      e.preventDefault();
      active = (active + (e.key === "ArrowDown" ? 1 : -1) + items.length) % items.length;
      items.forEach((li, i) => li.setAttribute("aria-selected", String(i === active)));
      input.setAttribute("aria-activedescendant", items[active].id);
    } else if (e.key === "Enter" && active >= 0) {
      e.preventDefault();
      choose(items[active].textContent);
    } else if (e.key === "Escape") {
      hide();
    }
  });
  input.addEventListener("blur", () => setTimeout(hide, 150));

  // --- skanowanie aparatem (biblioteka ZXing ładowana dopiero na żądanie) ---
  const scanButton = document.getElementById("scan-button");
  const overlay = document.getElementById("scanner");
  const video = document.getElementById("scanner-video");
  const status = document.getElementById("scanner-status");
  let scanControls = null;

  if (scanButton && window.isSecureContext && navigator.mediaDevices && navigator.mediaDevices.getUserMedia) {
    scanButton.hidden = false;
  }

  function loadLibrary() {
    if (window.ZXingBrowser) return Promise.resolve();
    return new Promise((resolve, reject) => {
      const s = document.createElement("script");
      s.src = scanButton.dataset.lib;
      s.onload = resolve;
      s.onerror = () => reject(new Error("Nie udało się wczytać biblioteki skanera."));
      document.head.appendChild(s);
    });
  }

  function stopScan() {
    if (scanControls) { scanControls.stop(); scanControls = null; }
    overlay.hidden = true;
  }

  async function startScan() {
    overlay.hidden = false;
    status.textContent = "Uruchamiam aparat…";
    try {
      await loadLibrary();
      const reader = new window.ZXingBrowser.BrowserMultiFormatReader();
      scanControls = await reader.decodeFromConstraints(
        { video: { facingMode: { ideal: "environment" } } }, video,
        (result) => {
          if (!result) return;
          stopScan();
          input.value = result.getText();
          form.submit();
        });
      status.textContent = "Skieruj aparat na kod Data Matrix lub kod kreskowy opakowania.";
    } catch (err) {
      status.textContent = err && err.name === "NotAllowedError"
        ? "Brak zgody na użycie aparatu. Możesz wpisać kod ręcznie."
        : "Nie udało się uruchomić aparatu. Możesz wpisać kod ręcznie.";
    }
  }

  if (scanButton) scanButton.addEventListener("click", startScan);
  document.getElementById("scanner-close")?.addEventListener("click", stopScan);
  document.addEventListener("keydown", (e) => { if (e.key === "Escape" && !overlay.hidden) stopScan(); });
})();
