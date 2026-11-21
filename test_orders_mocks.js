// Fake Apps Script services and spreadsheets for test_orders_e2e.py, which
// runs the real google_apps_script.js against them in a browser.
window.__props = { PASSCODE: "pw", SHEET_SHARED: "S", SHEET_JOEL: "J" };
const PropertiesService = { getScriptProperties: () => ({ getProperty: (k) => (k in __props ? __props[k] : null), setProperty: (k, v) => { __props[k] = String(v); } }) };
const LockService = { getScriptLock: () => ({ waitLock() {}, releaseLock() {} }) };
// Off by default so a check that edits the fake sheets directly sees it at
// once; the real script caches the list for a minute.
window.__cacheOn = false;
window.__cache = {};
const CacheService = { getScriptCache: () => ({
  get: (k) => (__cacheOn && k in __cache ? __cache[k] : null),
  put: (k, v) => { __cache[k] = v; },
}) };
const ContentService = { MimeType: { JSON: "json" }, createTextOutput: (s) => ({ s, setMimeType() { return this; } }) };
let __uuid = 0;
const Utilities = { getUuid: () => "ord" + String(++__uuid).padStart(5, "0") + "-x" };
class Sheet {
  constructor(name, rows) { this.name = name; this.rows = rows; }
  // In box is a formula in the real sheet: Qty - Sold - Pulled.
  computed(r, i) {
    if (this.name !== "Inventory" || i === 0) return r.slice();
    const h = this.rows[0], c = (n) => h.indexOf(n), out = r.slice();
    out[c("In box")] = (Number(r[c("Qty")]) || 0) - (Number(r[c("Sold")]) || 0) - (Number(r[c("Pulled")]) || 0);
    return out;
  }
  // Like the real thing: every row padded with "" to the widest one.
  getDataRange() {
    const width = Math.max(...this.rows.map((r) => r.length));
    return { getValues: () => this.rows.map((r, i) => this.computed(r, i).concat(Array(width - r.length).fill(""))) };
  }
  getRange(row, col) { return { setValue: (v) => { this.rows[row - 1][col - 1] = v; } }; }
  appendRow(a) { this.rows.push(a.slice()); }
  setFrozenRows() {}
  deleteRow(n) { this.rows.splice(n - 1, 1); }
  setName(n) { const b = Object.values(__books).find((bk) => Object.values(bk).includes(this)); for (const k of Object.keys(b)) if (b[k] === this) delete b[k]; b[n] = this; this.name = n; }
}
const H = ["Location", "Name", "Set code", "Set name", "Collector #", "Foil", "Rarity", "Condition", "Language", "Qty", "Price (USD)",
  "Scryfall ID", "Date logged", "Sold", "Pulled", "In box", "Value in box (USD)", "Notes"];
const inv = (loc, name, foil, qty, id) => [loc, name, "X", "Set", "1", foil, "rare", "near_mint", "en", qty, "$1", id, "2026-11-01", "", "", "", "", ""];
window.__books = {
  S: { Inventory: new Sheet("Inventory", [H,
    inv("B01-01", "Psychic Frog", "normal", 1, "frog"),
    inv("B01-02", "Psychic Frog", "normal", 1, "frog"),
    inv("B01-03", "Psychic Frog", "foil", 3, "frog"),
    inv("B04-01", "Sol Ring", "foil", 1, "sol"),
    inv("B04-02", "Wasteland", "normal", 2, "waste"),
    inv("B05-01", "Bulk Card", "normal", 6, "bulk"),
    inv("B05-02", "Bulk Card", "normal", 9, "bulk"),
    inv("B05-03", "Bulk Card", "normal", 3, "bulk")]),
    // Made by the first version of the script: old name, no Type/Source.
    "WTB orders": new Sheet("WTB orders", [
      ["Order", "Status", "Card", "Set", "Finish", "Scryfall ID", "Qty", "Price (USD)", "Location", "Inventory row", "Offered", "Paid", "Closed"],
      ["legacy01", "Awaiting payment", "Old Card", "Set", "normal", "old", 1, 3, "", "", new Date(), "", ""]]) },
  J: { Inventory: new Sheet("Inventory", [H, inv("B01-01", "Snap", "foil", 1, "snap"), inv("B02-01", "Sol Ring", "foil", 1, "sol")]) },
};
const SpreadsheetApp = {
  openById: (id) => {
    const b = __books[id];
    if (!b) throw new Error("no sheet " + id);
    return { getSheetByName: (n) => b[n] || null, insertSheet: (n) => (b[n] = new Sheet(n, [])) };
  },
};
