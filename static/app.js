const searchForm = document.getElementById("search-form");
const searchInput = document.getElementById("search-input");
const searchSuggestions = document.getElementById("search-suggestions");
const searchResults = document.getElementById("search-results");
const searchError = document.getElementById("search-error");
const watchlistEl = document.getElementById("watchlist");
const watchlistError = document.getElementById("watchlist-error");
const refreshBtn = document.getElementById("refresh-btn");
const importInput = document.getElementById("import-input");
const importStatus = document.getElementById("import-status");
const currentOwnerInput = document.getElementById("current-owner");
const knownOwnersList = document.getElementById("known-owners");
const ownerFilter = document.getElementById("owner-filter");
const portfolioValueEl = document.getElementById("portfolio-value");
const historyDialog = document.getElementById("history-dialog");
const historyTitle = document.getElementById("history-title");
const historyCanvas = document.getElementById("history-chart");
const closeHistoryBtn = document.getElementById("close-history");

function showError(el, message) {
  el.textContent = message;
  el.hidden = !message;
}

function formatPrice(price) {
  if (price === null || price === undefined) return "—";
  return `$${Number(price).toFixed(2)}`;
}

function pickDefaultVariant(variants) {
  return variants.find((v) => v.printing === "Normal") || variants[0];
}

function priceSectionHtml(variant) {
  return `
    <div class="price">${formatPrice(variant.cardKingdomPrice)}</div>
    <div class="meta ck-buylist">Card Kingdom buylist: ${formatPrice(variant.cardKingdomBuylist)}</div>
  `;
}

function cardMeta(card) {
  return card.setName || card.set || "";
}

function currentOwner() {
  return currentOwnerInput.value.trim();
}

async function loadOwners() {
  try {
    const owners = await fetchJSON("/api/owners");
    knownOwnersList.innerHTML = owners.map((o) => `<option value="${escapeHtml(o)}">`).join("");
    const selected = ownerFilter.value;
    ownerFilter.innerHTML =
      '<option value="">All owners</option>' +
      owners.map((o) => `<option value="${escapeHtml(o)}">${escapeHtml(o)}</option>`).join("");
    ownerFilter.value = selected;
  } catch (err) {
    // Non-critical — the owner dropdown just stays empty.
  }
}

async function fetchJSON(url, options) {
  const resp = await fetch(url, options);
  const data = await resp.json().catch(() => ({}));
  if (!resp.ok) {
    throw new Error(data.error || `Request failed (${resp.status})`);
  }
  return data;
}

let autocompleteTimer = null;
let autocompleteRequestId = 0;
let activeSuggestionIndex = -1;

function hideSuggestions() {
  searchSuggestions.hidden = true;
  searchSuggestions.innerHTML = "";
  activeSuggestionIndex = -1;
}

function scheduleAutocomplete() {
  clearTimeout(autocompleteTimer);
  const q = searchInput.value.trim();
  if (q.length < 2) {
    hideSuggestions();
    return;
  }
  // A native <datalist> doesn't reliably re-render its dropdown while the
  // user is still typing — it only seems to pick up fresh options after a
  // pause, which reads as "waits for a whole word." A hand-rolled dropdown
  // re-renders synchronously on every fetch, so suggestions actually track
  // each keystroke instead of just the finished word.
  autocompleteTimer = setTimeout(() => fetchAutocomplete(q), 150);
}

async function fetchAutocomplete(q) {
  const requestId = ++autocompleteRequestId;
  try {
    const names = await fetchJSON(`/api/autocomplete?q=${encodeURIComponent(q)}`);
    if (requestId !== autocompleteRequestId) return; // a newer keystroke already superseded this
    renderSuggestions(names);
  } catch (err) {
    // Non-critical — suggestions just stay as they were.
  }
}

function renderSuggestions(names) {
  activeSuggestionIndex = -1;
  if (!names.length) {
    hideSuggestions();
    return;
  }
  searchSuggestions.innerHTML = names.map((n) => `<div class="item">${escapeHtml(n)}</div>`).join("");
  searchSuggestions.hidden = false;
  searchSuggestions.querySelectorAll(".item").forEach((el, i) => {
    // mousedown (not click) + preventDefault stops the input from blurring
    // before the click registers, so selecting a suggestion doesn't race
    // against the blur handler that closes this dropdown.
    el.addEventListener("mousedown", (event) => {
      event.preventDefault();
      selectSuggestion(names[i]);
    });
  });
}

function selectSuggestion(name) {
  searchInput.value = name;
  hideSuggestions();
  searchInput.focus();
  // Suggestions come from Scryfall's own name list, so we already know the
  // exact name — search for exactly that card (Scryfall's !"..." syntax)
  // instead of the plain-text search, which does a broad fuzzy/oracle-text
  // match and would pull in dozens of loosely related cards.
  performSearch(`!"${name.replace(/"/g, '\\"')}"`);
}

function moveActiveSuggestion(delta) {
  const items = searchSuggestions.querySelectorAll(".item");
  if (!items.length) return;
  activeSuggestionIndex = (activeSuggestionIndex + delta + items.length) % items.length;
  items.forEach((el, i) => el.classList.toggle("active", i === activeSuggestionIndex));
  items[activeSuggestionIndex].scrollIntoView({ block: "nearest" });
}

function handleSearchInputKeydown(event) {
  const items = searchSuggestions.querySelectorAll(".item");
  if (searchSuggestions.hidden || !items.length) return;
  if (event.key === "ArrowDown") {
    event.preventDefault();
    moveActiveSuggestion(1);
  } else if (event.key === "ArrowUp") {
    event.preventDefault();
    moveActiveSuggestion(-1);
  } else if (event.key === "Enter" && activeSuggestionIndex >= 0) {
    event.preventDefault();
    selectSuggestion(items[activeSuggestionIndex].textContent);
  } else if (event.key === "Escape") {
    hideSuggestions();
  }
}

async function performSearch(query) {
  showError(searchError, "");
  searchResults.innerHTML = "<p class=\"empty\">Searching…</p>";
  try {
    const cards = await fetchJSON(`/api/search?${new URLSearchParams({ q: query }).toString()}`);
    renderSearchResults(cards);
  } catch (err) {
    searchResults.innerHTML = "";
    showError(searchError, err.message);
  }
}

async function runSearch(event) {
  event.preventDefault();
  hideSuggestions();
  await performSearch(searchInput.value.trim());
}

function renderSearchResults(cards) {
  searchResults.innerHTML = "";
  if (!cards.length) {
    searchResults.innerHTML = "<p class=\"empty\">No cards found.</p>";
    return;
  }

  cards.forEach((card) => {
    const variants = card.variants;
    let selected = pickDefaultVariant(variants);

    const el = document.createElement("div");
    el.className = "card";
    el.innerHTML = `
      ${card.imageUrl ? `<img class="card-image" src="${escapeHtml(card.imageUrl)}" alt="${escapeHtml(card.name || "")}">` : ""}
      <div class="name">${escapeHtml(card.name || "Unknown card")}</div>
      <div class="meta">${escapeHtml(cardMeta(card))}</div>
      ${
        variants.length > 1
          ? `<select class="variant-select"></select>`
          : `<div class="meta">${escapeHtml(selected.printing)}</div>`
      }
      <div class="price-section"></div>
      <div class="actions">
        <input type="number" class="qty-input" min="1" value="1" title="Quantity owned">
        <button type="button" data-action="track">Track</button>
        ${card.mtgjsonId ? `<button type="button" class="secondary" data-action="history">History</button>` : ""}
      </div>
    `;

    const priceSection = el.querySelector(".price-section");
    const renderPrice = () => {
      priceSection.innerHTML = priceSectionHtml(selected);
    };
    renderPrice();

    if (variants.length > 1) {
      const select = el.querySelector(".variant-select");
      variants.forEach((v, i) => {
        const opt = document.createElement("option");
        opt.value = String(i);
        opt.textContent = v.printing;
        if (v === selected) opt.selected = true;
        select.appendChild(opt);
      });
      select.addEventListener("change", () => {
        selected = variants[Number(select.value)];
        renderPrice();
      });
    }

    const qtyInput = el.querySelector(".qty-input");
    el.querySelector('[data-action="track"]').addEventListener("click", () =>
      trackCard(card, selected, Number(qtyInput.value) || 1)
    );
    const historyBtn = el.querySelector('[data-action="history"]');
    if (historyBtn) {
      historyBtn.addEventListener("click", () => showCardHistory(card));
    }
    searchResults.appendChild(el);
  });
}

async function trackCard(card, variant, quantity) {
  if (!variant.variantId) {
    showError(searchError, "This card is missing a variant id and can't be tracked.");
    return;
  }
  try {
    await fetchJSON("/api/watchlist", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        variantId: variant.variantId,
        cardId: card.scryfallId,
        name: card.name,
        setName: card.setName,
        printing: variant.printing,
        tcgplayerId: card.tcgplayerId,
        imageUrl: card.imageUrl,
        mtgjsonId: card.mtgjsonId,
        cardKingdomPrice: variant.cardKingdomPrice,
        cardKingdomBuylist: variant.cardKingdomBuylist,
        owner: currentOwner(),
        quantity: quantity || 1,
      }),
    });
    await Promise.all([loadWatchlist(), loadOwners()]);
  } catch (err) {
    showError(searchError, err.message);
  }
}

async function loadWatchlist() {
  showError(watchlistError, "");
  try {
    const params = ownerFilter.value ? `?owner=${encodeURIComponent(ownerFilter.value)}` : "";
    const items = await fetchJSON(`/api/watchlist${params}`);
    renderWatchlist(items);
  } catch (err) {
    showError(watchlistError, err.message);
  }
}

function renderWatchlist(items) {
  watchlistEl.innerHTML = "";
  renderPortfolioValue(items);
  if (!items.length) {
    watchlistEl.innerHTML = "<p class=\"empty\">Nothing tracked yet — search above and hit Track.</p>";
    return;
  }

  items.forEach((item) => {
    const delta = deltaInfo(item.latest_price, item.previous_price);
    const qty = item.quantity || 1;
    const el = document.createElement("div");
    el.className = "card";
    el.innerHTML = `
      ${item.image_url ? `<img class="card-image" src="${escapeHtml(item.image_url)}" alt="${escapeHtml(item.name || "")}">` : ""}
      <div class="name">${escapeHtml(item.name)}${qty > 1 ? ` <span class="meta">×${qty}</span>` : ""}</div>
      <div class="meta">${escapeHtml(item.set_name || "")}</div>
      <div class="meta">${escapeHtml(item.printing || "")}${item.owner ? ` · ${escapeHtml(item.owner)}` : ""}</div>
      <div class="price">${formatPrice(item.latest_price)}${qty > 1 && item.latest_price != null ? ` <span class="meta">(${formatPrice(item.latest_price * qty)} total)</span>` : ""}</div>
      ${delta ? `<div class="delta ${delta.direction}">${delta.text}</div>` : ""}
      <div class="meta ck-buylist">Card Kingdom buylist: ${formatPrice(item.cardkingdom_buylist_price)}</div>
      <div class="actions">
        <button type="button" data-action="history">History</button>
        <button type="button" class="secondary" data-action="remove">Remove</button>
      </div>
    `;
    el.querySelector('[data-action="history"]').addEventListener("click", () =>
      showHistory(item)
    );
    el.querySelector('[data-action="remove"]').addEventListener("click", () =>
      removeCard(item.id)
    );
    watchlistEl.appendChild(el);
  });
}

function renderPortfolioValue(items) {
  const total = items.reduce(
    (sum, item) => sum + (item.latest_price != null ? item.latest_price * (item.quantity || 1) : 0),
    0
  );
  const totalCards = items.reduce((sum, item) => sum + (item.quantity || 1), 0);
  portfolioValueEl.textContent = `Portfolio value: ${formatPrice(total)} (${totalCards} card${totalCards === 1 ? "" : "s"})`;
}

function deltaInfo(latest, previous) {
  if (latest === null || latest === undefined || previous === null || previous === undefined) {
    return null;
  }
  const diff = latest - previous;
  if (Math.abs(diff) < 0.005) return null;
  const pct = (diff / previous) * 100;
  const direction = diff > 0 ? "up" : "down";
  const sign = diff > 0 ? "+" : "";
  return { direction, text: `${sign}${diff.toFixed(2)} (${sign}${pct.toFixed(1)}%)` };
}

async function removeCard(id) {
  try {
    await fetchJSON(`/api/watchlist/${id}`, { method: "DELETE" });
    await loadWatchlist();
  } catch (err) {
    showError(watchlistError, err.message);
  }
}

async function refreshPrices() {
  refreshBtn.disabled = true;
  refreshBtn.textContent = "Refreshing…";
  try {
    const items = await fetchJSON("/api/refresh", { method: "POST" });
    renderWatchlist(items);
  } catch (err) {
    showError(watchlistError, err.message);
  } finally {
    refreshBtn.disabled = false;
    refreshBtn.textContent = "Refresh prices";
  }
}

async function importCsv(file) {
  importStatus.hidden = false;
  importStatus.className = "status-info";
  importStatus.textContent = `Importing ${file.name}… this can take a minute for large collections.`;

  const formData = new FormData();
  formData.append("file", file);
  formData.append("owner", currentOwner());

  try {
    const result = await fetchJSON("/api/watchlist/import", { method: "POST", body: formData });
    importStatus.className = "status-info";
    importStatus.textContent = `Imported ${result.imported} card(s)${result.skipped ? `, skipped ${result.skipped}` : ""}.`;
    await Promise.all([loadWatchlist(), loadOwners()]);
  } catch (err) {
    importStatus.className = "error";
    importStatus.textContent = err.message;
  }
}

async function showHistory(item) {
  try {
    const [market, buylist] = await Promise.all([
      fetchJSON(`/api/watchlist/${item.id}/history`),
      fetchJSON(`/api/watchlist/${item.id}/history?kind=buylist`),
    ]);
    historyTitle.innerHTML = `${escapeHtml(item.name)} — ${escapeHtml(item.printing || "")}
      <span class="meta"><span style="color:#5b8cff">■</span> Market &nbsp; <span style="color:#d4a24c">■</span> Buylist</span>`;
    drawSparkline(historyCanvas, [
      { data: market, color: "#5b8cff" },
      { data: buylist, color: "#d4a24c" },
    ]);
    historyDialog.showModal();
  } catch (err) {
    showError(watchlistError, err.message);
  }
}

function pointDate(point) {
  return point.date || point.recorded_at;
}

function formatDate(dateStr) {
  const d = new Date(dateStr);
  if (isNaN(d)) return "";
  return d.toLocaleDateString(undefined, { month: "short", day: "numeric" });
}

function drawSparkline(canvas, series) {
  const ctx = canvas.getContext("2d");
  const { width, height } = canvas;
  ctx.clearRect(0, 0, width, height);

  const usable = series.filter((s) => s.data.length >= 2);
  if (!usable.length) {
    ctx.fillStyle = "#9096a3";
    ctx.font = "14px sans-serif";
    ctx.fillText("Not enough data yet — check back after another refresh.", 12, height / 2);
    return;
  }

  const allPrices = usable.flatMap((s) => s.data.map((h) => h.price)).filter((p) => p != null);
  const min = Math.min(...allPrices);
  const max = Math.max(...allPrices);
  const padLeft = 12;
  const padRight = 12;
  const padTop = 16;
  const padBottom = 22;
  const chartHeight = height - padTop - padBottom;
  const range = max - min || 1;

  usable.forEach(({ data, color }) => {
    ctx.strokeStyle = color;
    ctx.lineWidth = 2;
    ctx.beginPath();
    data.forEach((point, i) => {
      const x = padLeft + (i / (data.length - 1)) * (width - padLeft - padRight);
      const y = padTop + chartHeight - ((point.price - min) / range) * chartHeight;
      if (i === 0) ctx.moveTo(x, y);
      else ctx.lineTo(x, y);
    });
    ctx.stroke();
  });

  ctx.fillStyle = "#9096a3";
  ctx.font = "12px sans-serif";
  ctx.textAlign = "left";
  ctx.fillText(`$${min.toFixed(2)}`, 4, padTop + chartHeight);
  ctx.fillText(`$${max.toFixed(2)}`, 4, padTop);

  // Date ticks along the x-axis, positioned using whichever series has the
  // most points — series can differ slightly in length (e.g. a night with
  // no buylist price recorded), so the longest one is the closest proxy
  // for the chart's actual date range.
  const reference = usable.reduce((a, b) => (b.data.length > a.data.length ? b : a));
  const dates = reference.data.map(pointDate);
  const tickCount = Math.min(4, dates.length);
  for (let t = 0; t < tickCount; t++) {
    const idx = tickCount === 1 ? 0 : Math.round((t / (tickCount - 1)) * (dates.length - 1));
    const x = padLeft + (idx / (dates.length - 1 || 1)) * (width - padLeft - padRight);
    // Left/right-align the end ticks instead of centering, so the label
    // text stays inside the canvas instead of clipping past its edges.
    ctx.textAlign = t === 0 ? "left" : t === tickCount - 1 ? "right" : "center";
    ctx.fillText(formatDate(dates[idx]), x, height - 4);
  }
  ctx.textAlign = "left";
}

async function showCardHistory(card) {
  try {
    const data = await fetchJSON(`/api/lookup/${encodeURIComponent(card.mtgjsonId)}`);
    historyTitle.innerHTML = `${escapeHtml(data.name)} — ${escapeHtml(data.set || "")}
      <span class="meta"><span style="color:#5b8cff">■</span> Market</span>`;
    drawSparkline(historyCanvas, [{ data: data.history, color: "#5b8cff" }]);
    historyDialog.showModal();
  } catch (err) {
    showError(searchError, err.message);
  }
}

function escapeHtml(str) {
  if (str === null || str === undefined) return "";
  return String(str)
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");
}

searchForm.addEventListener("submit", runSearch);
searchInput.addEventListener("input", scheduleAutocomplete);
searchInput.addEventListener("keydown", handleSearchInputKeydown);
searchInput.addEventListener("blur", hideSuggestions);
refreshBtn.addEventListener("click", refreshPrices);
closeHistoryBtn.addEventListener("click", () => historyDialog.close());
importInput.addEventListener("change", () => {
  if (importInput.files.length) {
    importCsv(importInput.files[0]);
    importInput.value = "";
  }
});
ownerFilter.addEventListener("change", loadWatchlist);

try {
  currentOwnerInput.value = localStorage.getItem("currentOwner") || "";
} catch (err) {
  // localStorage can throw in some browser contexts — just skip remembering it.
}
currentOwnerInput.addEventListener("change", () => {
  try {
    localStorage.setItem("currentOwner", currentOwnerInput.value.trim());
  } catch (err) {
    // Non-critical.
  }
});

loadOwners();
loadWatchlist();
