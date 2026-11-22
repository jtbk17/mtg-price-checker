// Google Apps Script web app: lets the GitHub Pages dashboard
// (docs/index.html) record sales — to WTB buyers or from our TCGMP
// listings — and their progress in the two inventory sheets, and log new
// batches of cards from a ManaBox CSV export (Add cards).
// The dashboard is a static page with nowhere safe to keep Google
// credentials, so this runs inside Google as you, and the dashboard only
// ever sends it a passcode.
//
// Deploy (one-time):
//   1. script.google.com -> New project, and paste this file over Code.gs.
//   2. Project Settings -> Script properties, add:
//        PASSCODE      a long random string; the dashboard asks for it once
//                      per device
//        SHEET_SHARED  the shared inventory spreadsheet's ID
//        SHEET_JOEL    Joel's inventory spreadsheet's ID
//   3. Deploy -> New deployment -> type Web app. Execute as: Me. Who has
//      access: Anyone. Authorize when asked (it edits both sheets as you,
//      so your account needs edit access to both).
//   4. Put the web app URL in SHEETS_API_URL in docs/index.html.
// After changing this file: Deploy -> Manage deployments -> edit -> Version:
// New version, which keeps the same URL.
//
// Each spreadsheet gets an "Orders" tab (created on first use; an older
// "WTB orders" tab is renamed and given any newer columns) logging every
// order and its progress. "Not a sale" answers to the dashboard's "Looks
// sold?" listing drops are kept in script properties, not the sheets.
// Inventory's Sold/Pulled cells only change at these steps:
//   buyer paid         Pulled + qty on the row(s) the copies came from (you
//                      pick each row and how many, when they're spread out)
//   dropped off        Pulled - qty, Sold + qty — In box is Qty - Sold -
//                      Pulled, so adding to Sold alone would count the
//                      copies out twice
//   cancelled after    Pulled - qty (the copies go back in the box)
//   being paid
//
// Add cards appends a batch to the bottom of Inventory behind one slot —
// the next empty one unless another is picked, worked out the same way as
// the Dashboard tab's "Next location" — and records it on an "Intake log"
// tab (created on first use), which is how a batch uploaded twice is
// caught and how the latest one can be undone.

const INVENTORY_TAB = "Inventory";
const ORDERS_TAB = "Orders";
const LEGACY_ORDERS_TAB = "WTB orders";
// New columns only ever go on the end, so older tabs can be extended in place.
const ORDER_HEADERS = [
  "Order", "Status", "Card", "Set", "Finish", "Scryfall ID", "Qty", "Price (USD)",
  "Location", "Inventory row", "Offered", "Paid", "Closed", "Type", "Source",
];
const ORDER_TYPES = ["WTB", "Listing"];
const SHEET_PROPERTIES = { Shared: "SHEET_SHARED", Joel: "SHEET_JOEL" };
const RECENT_DAYS = 14;

// Bumped when the dashboard needs to know what this deployment supports
// (3: an order's copies can be pulled from several Inventory rows; 4: the
// list carries each waiting order's box locations, every change answers
// with the fresh list, and cancelled orders can be removed; 5: undo, several
// orders dropped off at once, a sales summary, and a cached list; 6: Add
// cards from a ManaBox CSV).
const VERSION = 6;
// A mis-tap can be stepped back for this long.
const UNDO_HOURS = 24;
// The list is cached this long between changes (each change refreshes it),
// so a hand edit in the sheet can take up to a minute to show.
const LIST_CACHE_SECONDS = 60;

const INTAKE_LOG_TAB = "Intake log";
const INTAKE_LOG_HEADERS = ["Batch", "Logged", "Location", "Cards", "Rows", "First row", "Added", "Fingerprint", "Undone"];
// The Inventory columns a batch fills, from one parsed ManaBox CSV row.
// Sold and Pulled start blank. In box and Value in box are each one formula
// in their header cell that fills every row, so writing there would break
// them for the whole sheet.
const INTAKE_FIELDS = {
  "Location": (r, batch) => batch.location,
  "Name": (r) => r.name,
  "Set code": (r) => r.setCode,
  "Set name": (r) => r.setName,
  "Collector #": (r) => r.collector,
  "Foil": (r) => r.foil,
  "Rarity": (r) => r.rarity,
  "Condition": (r) => r.condition,
  "Language": (r) => r.language,
  "Qty": (r) => r.qty,
  "Price (USD)": (r) => r.price,
  "Scryfall ID": (r) => r.scryfallId,
  "Date logged": (r, batch) => batch.today,
};
const SLOT_PATTERN = /^B(\d{2})-(\d{2})$/;
const SCRYFALL_ID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;
// Only reads, so they skip the lock real changes need (and the fresh list).
const READ_ONLY = ["locations", "intakeStatus"];

const AWAITING = "Awaiting payment";
const PAID = "Paid";
const SOLD = "Sold";
const CANCELLED = "Cancelled";

function doPost(e) {
  let result;
  try {
    const req = JSON.parse(e.postData.contents);
    const passcode = PropertiesService.getScriptProperties().getProperty("PASSCODE");
    if (!passcode || req.passcode !== passcode) {
      result = { ok: false, auth: true, error: "Wrong passcode" };
    } else if (!ACTIONS.hasOwnProperty(req.action)) {
      result = { ok: false, error: `Unknown action "${req.action}"` };
    } else if (READ_ONLY.indexOf(req.action) >= 0) {
      // Only reads, so they mustn't queue behind (or hold up) the lock
      // real changes need.
      result = Object.assign({ ok: true }, ACTIONS[req.action](req));
    } else {
      // One request at a time: two taps landing together must not both
      // read the same Pulled value and each add to it.
      const lock = LockService.getScriptLock();
      lock.waitLock(20000);
      try {
        result = Object.assign({ ok: true }, ACTIONS[req.action](req));
        // Answer every change with the fresh list, saving the dashboard a
        // second round trip (each one costs seconds of start-up).
        if (req.action !== "list") Object.assign(result, freshList());
      } finally {
        lock.releaseLock();
      }
    }
  } catch (err) {
    result = { ok: false, error: String((err && err.message) || err) };
  }
  return ContentService.createTextOutput(JSON.stringify(result)).setMimeType(ContentService.MimeType.JSON);
}

const ACTIONS = {
  list() {
    const hit = CacheService.getScriptCache().get("list");
    return hit ? JSON.parse(hit) : freshList();
  },

  // Steps an order back one stage, for a mis-tap within UNDO_HOURS: Paid ->
  // Awaiting payment (copies back in the box), Sold -> Paid (back from Sold
  // to Pulled), Cancelled -> where it was before.
  undo(req) {
    const { order, update } = findOrder(req.owner, req.id);
    const when = order.status === PAID ? order.paid : order.closed;
    if (!when || Date.now() - new Date(when).getTime() > UNDO_HOURS * 60 * 60 * 1000) {
      throw new Error(`Only changes from the last ${UNDO_HOURS} hours can be undone`);
    }
    const inv = readInventory(req.owner);
    if (order.status === PAID) {
      pulledRows(inv, order).forEach(({ row, qty }) => inv.add(row, "Pulled", -qty));
      update({ "Status": AWAITING, "Location": "", "Inventory row": "", "Paid": "" });
    } else if (order.status === SOLD) {
      pulledRows(inv, order, "sold").forEach(({ row, qty }) => {
        inv.add(row, "Sold", -qty);
        inv.add(row, "Pulled", qty);
      });
      update({ "Status": PAID, "Closed": "" });
    } else if (order.status === CANCELLED && order.pulls.length) {
      // Cancelled after being paid: its copies went back in the box.
      pulledRows(inv, order, "inBox").forEach(({ row, qty }) => inv.add(row, "Pulled", qty));
      update({ "Status": PAID, "Closed": "" });
    } else if (order.status === CANCELLED) {
      update({ "Status": AWAITING, "Closed": "" });
    } else {
      throw new Error(`Nothing to undo on an order that's ${order.status}`);
    }
    return {};
  },

  // Several paid orders dropped off together. Every one is checked before
  // any changes, including two orders that pulled from the same row.
  droppedOffMany(req) {
    const found = (req.orders || []).map((o) => Object.assign({ owner: o.owner }, findOrder(o.owner, o.id)));
    if (!found.length) throw new Error("No orders picked");
    const invs = {};
    const need = new Map();
    const plans = found.map(({ owner, order }) => {
      if (order.status !== PAID) throw new Error(`${order.name} is ${order.status}, not ${PAID}`);
      const inv = invs[owner] || (invs[owner] = readInventory(owner));
      const rows = pulledRows(inv, order);
      rows.forEach(({ row, qty }) => need.set(row, (need.get(row) || 0) + qty));
      return { inv, rows };
    });
    for (const [row, qty] of need) {
      if (row.pulled < qty) throw new Error(`Pulled is only ${row.pulled} at ${row.location} — fix Sold/Pulled by hand`);
    }
    plans.forEach(({ inv, rows }) => rows.forEach(({ row, qty }) => {
      inv.add(row, "Pulled", -qty);
      inv.add(row, "Sold", qty);
    }));
    const closed = new Date();
    found.forEach(({ update }) => update({ "Status": SOLD, "Closed": closed }));
    return {};
  },

  // Deletes a cancelled order's row from the Orders tab. One that came
  // from a "Looks sold?" listing drop leaves that drop dismissed, so it
  // doesn't come back asking.
  remove(req) {
    const { order, sheet } = findOrder(req.owner, req.id);
    if (order.status !== CANCELLED) throw new Error(`Only cancelled orders can be deleted (this one is ${order.status})`);
    if (order.source) {
      const dismissed = readDismissed();
      dismissed[order.source] = new Date().toISOString();
      PropertiesService.getScriptProperties().setProperty("DISMISSED", JSON.stringify(dismissed));
    }
    sheet.deleteRow(order.sheetRow);
    return {};
  },

  // "Not a sale" for a listing drop the dashboard flagged (its id).
  dismiss(req) {
    if (!req.source) throw new Error("Missing which listing drop to dismiss");
    const dismissed = readDismissed();
    dismissed[req.source] = new Date().toISOString();
    PropertiesService.getScriptProperties().setProperty("DISMISSED", JSON.stringify(dismissed));
    return {};
  },

  // Inventory rows holding this printing with copies still in the box.
  locations(req) {
    return { locations: locationsOf(readInventory(req.owner), req.scryfallId, req.finish) };
  },

  offer(req) {
    const qty = Number(req.qty);
    if (!Number.isInteger(qty) || qty < 1) throw new Error("Quantity must be a whole number of at least 1");
    if (!req.scryfallId || !req.finish) throw new Error("Missing card details");
    const type = req.type || "WTB";
    if (ORDER_TYPES.indexOf(type) < 0) throw new Error(`Unknown order type "${type}"`);
    const sheet = ordersSheet(req.owner);
    // One order per flagged listing drop, even if "record" is tapped twice —
    // checked across both sheets, since either owner's copy can be picked.
    const owners = Object.keys(SHEET_PROPERTIES).filter(sheetIdFor);
    if (req.source && owners.some((o) => readOrders(o).orders.some((order) => order.source === req.source))) {
      throw new Error("That sale is already recorded");
    }
    const id = Utilities.getUuid().slice(0, 8);
    const values = {
      "Order": id, "Status": AWAITING, "Card": req.name || "", "Set": req.set || "", "Finish": req.finish,
      "Scryfall ID": req.scryfallId, "Qty": qty, "Price (USD)": Number(req.price) || "", "Location": "",
      "Inventory row": "", "Offered": new Date(), "Paid": "", "Closed": "", "Type": type, "Source": req.source || "",
    };
    sheet.appendRow(ORDER_HEADERS.map((h) => values[h]));
    return { order: findOrder(req.owner, id).order };
  },

  // req.picks: [{row, qty}] adding up to the order's quantity, since the
  // copies can come from several box locations. (Older dashboards send
  // req.row alone, meaning every copy from that row.)
  paid(req) {
    const { order, update } = findOrder(req.owner, req.id);
    if (order.status !== AWAITING) throw new Error(`Order is ${order.status}, not ${AWAITING}`);
    const picks = (req.picks || [{ row: req.row, qty: order.qty }]).map((p) => ({ row: Number(p.row), qty: Number(p.qty) }));
    if (picks.some((p) => !Number.isInteger(p.qty) || p.qty < 1)) throw new Error("Each location needs a whole number of copies");
    if (new Set(picks.map((p) => p.row)).size !== picks.length) throw new Error("The same row was picked twice");
    const total = picks.reduce((n, p) => n + p.qty, 0);
    if (total !== order.qty) throw new Error(`Picked ${total} of the ${order.qty} copies`);

    // Check every row before changing any, so a bad pick changes nothing.
    const inv = readInventory(req.owner);
    const rows = picks.map((p) => {
      const row = inv.rows.find((r) => r.row === p.row);
      if (!row || row.scryfallId !== order.scryfallId || row.finish !== order.finish) {
        throw new Error("An Inventory row no longer holds this card — reload and pick again");
      }
      if (row.inBox < p.qty) throw new Error(`Only ${row.inBox} in the box at ${row.location}`);
      return row;
    });
    picks.forEach((p, i) => inv.add(rows[i], "Pulled", p.qty));
    update(Object.assign({ "Status": PAID, "Paid": new Date() }, pullsColumns(picks.map((p, i) => ({ row: p.row, location: rows[i].location, qty: p.qty })))));
    return { order: findOrder(req.owner, req.id).order };
  },

  droppedOff(req) {
    const { order, update } = findOrder(req.owner, req.id);
    if (order.status !== PAID) throw new Error(`Order is ${order.status}, not ${PAID}`);
    const inv = readInventory(req.owner);
    pulledRows(inv, order).forEach(({ row, qty }) => {
      inv.add(row, "Pulled", -qty);
      inv.add(row, "Sold", qty);
    });
    update({ "Status": SOLD, "Closed": new Date() });
    return { order: findOrder(req.owner, req.id).order };
  },

  // Where the next batch goes and what's been logged, for each sheet.
  intakeStatus() {
    return { intake: intakeStatusAll() };
  },

  // A ManaBox CSV export, parsed by the dashboard: req.rows (see
  // cleanIntakeRows) logged behind req.location in req.owner's Inventory.
  intake(req) {
    const owners = Object.keys(SHEET_PROPERTIES).filter(sheetIdFor);
    if (owners.indexOf(req.owner) < 0) throw new Error(`No sheet set up for ${req.owner}`);
    const location = String(req.location || "").trim().toUpperCase();
    if (!SLOT_PATTERN.test(location)) throw new Error(`"${req.location}" isn't a slot like B01-10`);
    const rows = cleanIntakeRows(req.rows);
    const cards = rows.reduce((n, r) => n + r.qty, 0);

    // The same export uploaded twice, to either sheet. ManaBox stamps each
    // card with when it was added, so a card left in the Inbox binder and
    // exported again with the next stack is caught too.
    const logged = new Map();
    const fingerprints = new Map();
    owners.forEach((owner) => liveBatches(readIntakeLog(owner)).forEach((e) => {
      e.added.forEach((a) => logged.set(a, Object.assign({ owner }, e)));
      fingerprints.set(e.fingerprint, Object.assign({ owner }, e));
    }));
    const again = rows.filter((r) => r.added && logged.has(r.added));
    const fingerprint = intakeFingerprint(rows);
    const earlier = again.length ? logged.get(again[0].added) : rows.some((r) => r.added) ? null : fingerprints.get(fingerprint);
    if (earlier) {
      const n = again.length ? again.reduce((k, r) => k + r.qty, 0) : cards;
      const where = `behind ${earlier.location}${earlier.owner === req.owner ? "" : ` in ${earlier.owner}'s sheet`} on ${dayOf(earlier.logged, earlier.owner)}`;
      throw new Error(n === cards
        ? `This file was already logged ${where}.`
        : `${n} of these ${cards} cards were already logged ${where}. In ManaBox, move them from Inbox to Collection, then export and upload again.`);
    }

    const inv = inventoryTable(req.owner);
    const first = lastFilledRow(inv) + 1;
    const last = first + rows.length - 1;
    const max = inv.sheet.getMaxRows();
    if (max < last) inv.sheet.insertRowsAfter(max, last - max);
    const batch = { location, today: Utilities.formatDate(new Date(), inv.timeZone, "yyyy-MM-dd") };
    const col = columnsOf(inv.headers, Object.keys(INTAKE_FIELDS), INVENTORY_TAB);
    // One write per run of side-by-side columns (normally just one).
    const indexes = Object.keys(INTAKE_FIELDS).map((name) => col[name]).sort((a, b) => a - b);
    const byIndex = {};
    Object.keys(INTAKE_FIELDS).forEach((name) => (byIndex[col[name]] = INTAKE_FIELDS[name]));
    const runs = [];
    indexes.forEach((i) => {
      const run = runs[runs.length - 1];
      if (run && run[run.length - 1] === i - 1) run.push(i);
      else runs.push([i]);
    });
    runs.forEach((run) => {
      inv.sheet.getRange(first, run[0] + 1, rows.length, run.length)
        .setValues(rows.map((r) => run.map((i) => byIndex[i](r, batch))));
    });

    const id = Utilities.getUuid().slice(0, 8);
    const added = Array.from(new Set(rows.map((r) => r.added).filter(Boolean)));
    intakeLogSheet(req.owner).appendRow([id, new Date(), location, cards, rows.length, first, added.join(" "), fingerprint, ""]);
    return { logged: { batch: id, owner: req.owner, location, cards, rows: rows.length, firstRow: first }, intake: intakeStatusAll() };
  },

  // Takes the latest batch back out, for a wrong sheet or slot — only
  // within UNDO_HOURS, while it's still the last thing on Inventory and
  // none of its cards have been sold or pulled.
  undoIntake(req) {
    const log = readIntakeLog(req.owner);
    const live = liveBatches(log);
    const e = live[live.length - 1];
    if (!e || e.batch !== String(req.batch)) throw new Error("Only the latest batch can be undone");
    if (Date.now() - new Date(e.logged).getTime() > UNDO_HOURS * 60 * 60 * 1000) {
      throw new Error(`Only batches from the last ${UNDO_HOURS} hours can be undone`);
    }
    const inv = inventoryTable(req.owner);
    const col = columnsOf(inv.headers, ["Location", "Sold", "Pulled"], INVENTORY_TAB);
    if (lastFilledRow(inv) !== e.firstRow + e.rows - 1) throw new Error("Rows were added below this batch since — fix it by hand");
    for (let row = e.firstRow; row < e.firstRow + e.rows; row++) {
      const r = inv.values[row - 1];
      if (String(r[col["Location"]]).trim().toUpperCase() !== e.location) throw new Error("This batch's rows have changed since — fix it by hand");
      if (Number(r[col["Sold"]]) || Number(r[col["Pulled"]])) throw new Error("Some of these cards are already sold or pulled — fix it by hand");
    }
    inv.sheet.deleteRows(e.firstRow, e.rows);
    log.sheet.getRange(e.sheetRow, log.col["Undone"] + 1).setValue(new Date());
    return { intake: intakeStatusAll() };
  },

  cancel(req) {
    const { order, update } = findOrder(req.owner, req.id);
    if (order.status === PAID) {
      const inv = readInventory(req.owner);
      pulledRows(inv, order).forEach(({ row, qty }) => inv.add(row, "Pulled", -qty));
    } else if (order.status !== AWAITING) {
      throw new Error(`Order is already ${order.status}`);
    }
    update({ "Status": CANCELLED, "Closed": new Date() });
    return { order: findOrder(req.owner, req.id).order };
  },
};

// ---- Add cards ----

function intakeStatusAll() {
  const out = {};
  Object.keys(SHEET_PROPERTIES).filter(sheetIdFor).forEach((owner) => (out[owner] = intakeStatusOf(owner)));
  return out;
}

function intakeStatusOf(owner) {
  const inv = inventoryTable(owner);
  const settings = slotSettings(owner);
  const col = columnsOf(inv.headers, ["Location", "In box"], INVENTORY_TAB);
  const slots = {};
  inv.values.slice(1).forEach((r) => {
    const location = String(r[col["Location"]]).trim().toUpperCase();
    if (SLOT_PATTERN.test(location)) slots[location] = (slots[location] || 0) + (Number(r[col["In box"]]) || 0);
  });
  const live = liveBatches(readIntakeLog(owner));
  const latest = live[live.length - 1];
  return {
    next: nextSlot(Object.keys(slots), settings.slotsPerBox),
    cardsPerSlot: settings.cardsPerSlot,
    slots: Object.keys(slots).sort().map((location) => ({ location, inBox: slots[location] })),
    added: Array.from(new Set([].concat(...live.map((e) => e.added)))),
    recent: live.slice(-5).reverse().map((e) => ({ batch: e.batch, logged: e.logged, location: e.location, cards: e.cards })),
    undoable: latest && Date.now() - new Date(latest.logged).getTime() <= UNDO_HOURS * 60 * 60 * 1000 ? latest.batch : null,
  };
}

// The slot after the highest one in use, rolling over to the next box after
// its last slot — the Dashboard tab's "Next location (auto)".
function nextSlot(locations, slotsPerBox) {
  const highest = locations.filter((l) => SLOT_PATTERN.test(l)).sort().pop();
  if (!highest) return "B01-01";
  const m = highest.match(SLOT_PATTERN);
  const box = Number(m[1]);
  const slot = Number(m[2]);
  const pad = (n) => String(n).padStart(2, "0");
  return slot >= slotsPerBox ? `B${pad(box + 1)}-01` : `B${pad(box)}-${pad(slot + 1)}`;
}

// The Dashboard tab's "Cards per slot" and "Slots per box" settings.
function slotSettings(owner) {
  const settings = { cardsPerSlot: 100, slotsPerBox: 10 };
  const sheet = spreadsheet(owner).getSheetByName("Dashboard");
  if (!sheet) return settings;
  sheet.getDataRange().getValues().forEach((r) => {
    const label = String(r[0]).trim();
    const n = Number(r[1]);
    if (!(n > 0)) return;
    if (label === "Cards per slot") settings.cardsPerSlot = n;
    if (label === "Slots per box") settings.slotsPerBox = n;
  });
  return settings;
}

// Checks every row of a parsed ManaBox export before anything is written.
function cleanIntakeRows(rows) {
  if (!Array.isArray(rows) || !rows.length) throw new Error("The file has no cards in it");
  const text = (v) => (v == null ? "" : String(v).trim());
  return rows.map((r, i) => {
    const qty = Number(r.qty);
    const name = text(r.name);
    const scryfallId = text(r.scryfallId).toLowerCase();
    const where = `Row ${i + 2}${name ? ` (${name})` : ""}`;
    if (!name) throw new Error(`${where} has no card name`);
    if (!SCRYFALL_ID.test(scryfallId)) throw new Error(`${where} has no Scryfall ID`);
    if (!Number.isInteger(qty) || qty < 1) throw new Error(`${where} has no quantity`);
    const price = text(r.price) === "" ? "" : Number(r.price);
    if (price !== "" && !(price >= 0)) throw new Error(`${where} has a price that isn't a number`);
    return {
      name, scryfallId, qty, price,
      setCode: text(r.setCode), setName: text(r.setName), collector: text(r.collector),
      foil: text(r.foil).toLowerCase() || "normal", rarity: text(r.rarity),
      condition: text(r.condition) || "near_mint", language: text(r.language) || "en",
      added: text(r.added),
    };
  });
}

// For an export without ManaBox's Added column: the same cards again.
function intakeFingerprint(rows) {
  const key = rows.map((r) => [r.scryfallId, r.foil, r.condition, r.language, r.qty].join("|")).sort().join("\n");
  let h = 0x811c9dc5;
  for (let i = 0; i < key.length; i++) {
    h ^= key.charCodeAt(i);
    h = Math.imul(h, 0x01000193) >>> 0;
  }
  return ("0000000" + h.toString(16)).slice(-8);
}

function dayOf(when, owner) {
  const d = new Date(when);
  return isNaN(d) ? String(when) : Utilities.formatDate(d, spreadsheet(owner).getSpreadsheetTimeZone(), "d MMM");
}

function inventoryTable(owner) {
  const ss = spreadsheet(owner);
  const sheet = ss.getSheetByName(INVENTORY_TAB);
  if (!sheet) throw new Error(`${owner}'s sheet has no ${INVENTORY_TAB} tab`);
  const values = sheet.getDataRange().getValues();
  return { sheet, values, headers: values[0].map(String), timeZone: ss.getSpreadsheetTimeZone() };
}

// The last row with a card in it (the header row when there are none). The
// formula columns fill every row, so only the card's own cells count.
function lastFilledRow(inv) {
  const col = columnsOf(inv.headers, ["Location", "Name", "Scryfall ID"], INVENTORY_TAB);
  for (let i = inv.values.length - 1; i >= 1; i--) {
    const r = inv.values[i];
    if ([col["Location"], col["Name"], col["Scryfall ID"]].some((c) => String(r[c]).trim() !== "")) return i + 1;
  }
  return 1;
}

function intakeLogSheet(owner) {
  const ss = spreadsheet(owner);
  let sheet = ss.getSheetByName(INTAKE_LOG_TAB);
  if (!sheet) {
    sheet = ss.insertSheet(INTAKE_LOG_TAB);
    sheet.appendRow(INTAKE_LOG_HEADERS);
    sheet.setFrozenRows(1);
  }
  return sheet;
}

// Every batch logged so far (none before the tab exists — reading never
// creates it).
function readIntakeLog(owner) {
  const sheet = spreadsheet(owner).getSheetByName(INTAKE_LOG_TAB);
  if (!sheet) return { sheet: null, col: {}, entries: [] };
  const values = sheet.getDataRange().getValues();
  const col = columnsOf(values[0].map(String), INTAKE_LOG_HEADERS, INTAKE_LOG_TAB);
  const iso = (v) => (v instanceof Date ? v.toISOString() : v || null);
  const entries = values.slice(1).map((r, i) => ({
    sheetRow: i + 2,
    batch: String(r[col["Batch"]]),
    logged: iso(r[col["Logged"]]),
    location: String(r[col["Location"]]),
    cards: Number(r[col["Cards"]]) || 0,
    rows: Number(r[col["Rows"]]) || 0,
    firstRow: Number(r[col["First row"]]) || 0,
    added: String(r[col["Added"]]).split(/\s+/).filter(Boolean),
    fingerprint: String(r[col["Fingerprint"]]),
    undone: iso(r[col["Undone"]]),
  })).filter((e) => e.batch);
  return { sheet, col, entries };
}

function liveBatches(log) {
  return log.entries.filter((e) => !e.undone);
}

// ---- Orders ----

// Recent and open orders (waiting ones with where their copies are), the
// sales summary, and what this deployment supports — cached until the
// next change or LIST_CACHE_SECONDS.
function freshList() {
  const orders = [];
  const everySold = [];
  const cutoff = Date.now() - RECENT_DAYS * 24 * 60 * 60 * 1000;
  for (const owner of Object.keys(SHEET_PROPERTIES)) {
    if (!sheetIdFor(owner)) continue;
    const all = readOrders(owner).orders;
    everySold.push(...all.filter((o) => o.status === SOLD || o.status === PAID));
    const mine = all.filter((order) => {
      const open = order.status === AWAITING || order.status === PAID;
      return open || (order.closed && new Date(order.closed).getTime() >= cutoff);
    });
    // Where each waiting order's copies are, read once per sheet here
    // rather than in a call per order.
    const waiting = mine.filter((o) => o.status === AWAITING);
    if (waiting.length) {
      const inv = readInventory(owner);
      waiting.forEach((o) => (o.locations = locationsOf(inv, o.scryfallId, o.finish)));
    }
    orders.push(...mine);
  }
  const list = { orders, dismissed: Object.keys(readDismissed()), version: VERSION, sales: salesSummary(everySold) };
  try {
    CacheService.getScriptCache().put("list", JSON.stringify(list), LIST_CACHE_SECONDS);
  } catch (err) {
    // Over the cache's 100KB limit: just serve it uncached.
  }
  return list;
}

// Sales by when they were dropped off (gross, before TCGMP's commission,
// which the dashboard takes off), plus what paid orders will bring in.
function salesSummary(orders) {
  const now = new Date();
  const monthStart = new Date(now.getFullYear(), now.getMonth(), 1).getTime();
  const lastMonthStart = new Date(now.getFullYear(), now.getMonth() - 1, 1).getTime();
  const weekAgo = now.getTime() - 7 * 24 * 60 * 60 * 1000;
  const periods = {
    week: (t) => t >= weekAgo,
    month: (t) => t >= monthStart,
    lastMonth: (t) => t >= lastMonthStart && t < monthStart,
    all: () => true,
  };
  const blank = () => ({ orders: 0, copies: 0, gross: 0, WTB: 0, Listing: 0 });
  const out = { pending: blank() };
  Object.keys(periods).forEach((name) => (out[name] = blank()));
  const add = (bucket, o) => {
    const gross = (o.price || 0) * o.qty;
    bucket.orders += 1;
    bucket.copies += o.qty;
    bucket.gross += gross;
    bucket[o.type === "Listing" ? "Listing" : "WTB"] += gross;
  };
  for (const o of orders) {
    if (o.status === PAID) {
      add(out.pending, o);
    } else if (o.closed) {
      const t = new Date(o.closed).getTime();
      Object.entries(periods).forEach(([name, inPeriod]) => inPeriod(t) && add(out[name], o));
    }
  }
  for (const bucket of Object.values(out)) {
    ["gross", "WTB", "Listing"].forEach((k) => (bucket[k] = Math.round(bucket[k] * 100) / 100));
  }
  return out;
}

function locationsOf(inv, scryfallId, finish) {
  return inv.rows
    .filter((r) => r.scryfallId === scryfallId && r.finish === finish && r.inBox > 0)
    .map((r) => ({ row: r.row, location: r.location, condition: r.condition, inBox: r.inBox }));
}

// How a paid order records where its copies came from. One row keeps the
// plain form ("B01-02" / 3); several are listed with their counts
// ("B01-01 ×3, B01-02 ×7" / "2 ×3, 3 ×7") — "×" so Sheets never reads them
// as a number or a time.
function pullsColumns(pulls) {
  if (pulls.length === 1) return { "Location": pulls[0].location, "Inventory row": pulls[0].row };
  return {
    "Location": pulls.map((p) => `${p.location} ×${p.qty}`).join(", "),
    "Inventory row": pulls.map((p) => `${p.row} ×${p.qty}`).join(", "),
  };
}

function parsePulls(location, inventoryRow, qty) {
  const rows = String(inventoryRow).match(/\d+\s*×\s*\d+/g);
  if (!rows) {
    const row = Number(inventoryRow);
    return row ? [{ row, location: String(location), qty }] : [];
  }
  const locations = String(location).split(",").map((l) => l.replace(/×\s*\d+\s*$/, "").trim());
  return rows.map((r, i) => {
    const [row, n] = r.split("×").map((x) => Number(x.trim()));
    return { row, location: locations[i] || "", qty: n };
  });
}

// The rows a paid order pulled from, each checked against what it recorded
// — rows are never deleted, but a sort of the Inventory tab would move them
// — and that each still has the copies about to be moved out of `from`
// (pulled, sold, or inBox when putting cancelled copies back).
const FROM_LABELS = { pulled: "Pulled", sold: "Sold", inBox: "In box" };
function pulledRows(inv, order, from = "pulled") {
  if (!order.pulls.length) throw new Error("This order doesn't say which rows it pulled from — fix Sold/Pulled by hand");
  return order.pulls.map((p) => {
    const row = inv.rows.find((r) => r.row === p.row);
    if (!row || row.scryfallId !== order.scryfallId || row.finish !== order.finish || row.location !== p.location) {
      throw new Error(`Inventory row ${p.row} no longer holds this card at ${p.location} — fix Sold/Pulled by hand`);
    }
    if (row[from] < p.qty) {
      throw new Error(`${FROM_LABELS[from]} is only ${row[from]} at ${row.location} — fix Sold/Pulled by hand`);
    }
    return { row, qty: p.qty };
  });
}

function sheetIdFor(owner) {
  const key = SHEET_PROPERTIES[owner];
  return key ? PropertiesService.getScriptProperties().getProperty(key) : null;
}

function spreadsheet(owner) {
  const id = sheetIdFor(owner);
  if (!id) throw new Error(`No sheet set up for ${owner}`);
  return SpreadsheetApp.openById(id);
}

function columnsOf(headers, names, tab) {
  const cols = {};
  for (const name of names) {
    const i = headers.indexOf(name);
    if (i < 0) throw new Error(`${tab} tab has no "${name}" column`);
    cols[name] = i;
  }
  return cols;
}

function readInventory(owner) {
  const sheet = spreadsheet(owner).getSheetByName(INVENTORY_TAB);
  if (!sheet) throw new Error(`${owner}'s sheet has no ${INVENTORY_TAB} tab`);
  const values = sheet.getDataRange().getValues();
  const col = columnsOf(
    values[0].map(String),
    ["Location", "Scryfall ID", "Foil", "Condition", "Sold", "Pulled", "In box"],
    INVENTORY_TAB
  );
  const num = (v) => Number(v) || 0;
  const rows = values.slice(1).map((r, i) => ({
    row: i + 2,
    location: String(r[col["Location"]]),
    scryfallId: String(r[col["Scryfall ID"]]).trim(),
    finish: String(r[col["Foil"]]).trim().toLowerCase(),
    condition: String(r[col["Condition"]]),
    sold: num(r[col["Sold"]]),
    pulled: num(r[col["Pulled"]]),
    inBox: num(r[col["In box"]]),
  }));
  return {
    rows,
    add(row, field, delta) {
      const key = field === "Sold" ? "sold" : "pulled";
      const next = row[key] + delta;
      // Blank rather than 0, matching rows nobody has sold or pulled from.
      sheet.getRange(row.row, col[field] + 1).setValue(next === 0 ? "" : next);
      row[key] = next;
    },
  };
}

function ordersSheet(owner) {
  const ss = spreadsheet(owner);
  let sheet = ss.getSheetByName(ORDERS_TAB);
  if (!sheet) {
    sheet = ss.getSheetByName(LEGACY_ORDERS_TAB);
    if (sheet) sheet.setName(ORDERS_TAB);
  }
  if (!sheet) {
    sheet = ss.insertSheet(ORDERS_TAB);
    sheet.appendRow(ORDER_HEADERS);
    sheet.setFrozenRows(1);
    return sheet;
  }
  // A tab made by an older version of this script lacks the newest columns.
  const headers = sheet.getDataRange().getValues()[0].map(String);
  ORDER_HEADERS.filter((h) => headers.indexOf(h) < 0).forEach((h, i) => {
    sheet.getRange(1, headers.length + i + 1).setValue(h);
  });
  return sheet;
}

// {listing drop id: when dismissed}, pruned to the dashboard's window.
function readDismissed() {
  let dismissed = {};
  try {
    dismissed = JSON.parse(PropertiesService.getScriptProperties().getProperty("DISMISSED") || "{}");
  } catch (err) {
    dismissed = {};
  }
  const cutoff = Date.now() - 2 * RECENT_DAYS * 24 * 60 * 60 * 1000;
  for (const [id, when] of Object.entries(dismissed)) {
    if (new Date(when).getTime() < cutoff) delete dismissed[id];
  }
  return dismissed;
}

function readOrders(owner) {
  const sheet = ordersSheet(owner);
  const values = sheet.getDataRange().getValues();
  const col = columnsOf(values[0].map(String), ORDER_HEADERS, ORDERS_TAB);
  const iso = (v) => (v instanceof Date ? v.toISOString() : v || null);
  const orders = values.slice(1).map((r, i) => ({
    sheetRow: i + 2,
    owner,
    id: String(r[col["Order"]]),
    status: String(r[col["Status"]]),
    name: String(r[col["Card"]]),
    set: String(r[col["Set"]]),
    finish: String(r[col["Finish"]]),
    scryfallId: String(r[col["Scryfall ID"]]),
    qty: Number(r[col["Qty"]]) || 0,
    price: r[col["Price (USD)"]] === "" ? null : Number(r[col["Price (USD)"]]),
    location: String(r[col["Location"]]),
    inventoryRow: Number(r[col["Inventory row"]]) || null,
    offered: iso(r[col["Offered"]]),
    paid: iso(r[col["Paid"]]),
    closed: iso(r[col["Closed"]]),
    type: String(r[col["Type"]]) || "WTB",
    source: String(r[col["Source"]]),
    pulls: parsePulls(r[col["Location"]], r[col["Inventory row"]], Number(r[col["Qty"]]) || 0),
  }));
  return { sheet, col, orders };
}

function findOrder(owner, id) {
  const { sheet, col, orders } = readOrders(owner);
  const order = orders.find((o) => o.id === String(id));
  if (!order) throw new Error(`No order ${id} in ${owner}'s sheet`);
  return {
    order,
    sheet,
    update(fields) {
      for (const [name, value] of Object.entries(fields)) {
        sheet.getRange(order.sheetRow, col[name] + 1).setValue(value);
      }
    },
  };
}
