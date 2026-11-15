// Google Apps Script web app: lets the GitHub Pages dashboard
// (docs/index.html) record WTB order progress in the two inventory sheets.
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
// Each spreadsheet gets a "WTB orders" tab (created on first use) logging
// every order and its progress. Inventory's Sold/Pulled cells only change
// at these steps:
//   buyer paid         Pulled + qty on the row the copies came from
//   dropped off        Pulled - qty, Sold + qty — In box is Qty - Sold -
//                      Pulled, so adding to Sold alone would count the
//                      copies out twice
//   cancelled after    Pulled - qty (the copies go back in the box)
//   being paid

const INVENTORY_TAB = "Inventory";
const ORDERS_TAB = "WTB orders";
const ORDER_HEADERS = [
  "Order", "Status", "Card", "Set", "Finish", "Scryfall ID", "Qty", "Price (USD)",
  "Location", "Inventory row", "Offered", "Paid", "Closed",
];
const SHEET_PROPERTIES = { Shared: "SHEET_SHARED", Joel: "SHEET_JOEL" };
const RECENT_DAYS = 14;

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
    } else {
      // One request at a time: two taps landing together must not both
      // read the same Pulled value and each add to it.
      const lock = LockService.getScriptLock();
      lock.waitLock(20000);
      try {
        result = Object.assign({ ok: true }, ACTIONS[req.action](req));
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
    const orders = [];
    const cutoff = Date.now() - RECENT_DAYS * 24 * 60 * 60 * 1000;
    for (const owner of Object.keys(SHEET_PROPERTIES)) {
      if (!sheetIdFor(owner)) continue;
      for (const order of readOrders(owner).orders) {
        const open = order.status === AWAITING || order.status === PAID;
        if (open || (order.closed && new Date(order.closed).getTime() >= cutoff)) orders.push(order);
      }
    }
    return { orders };
  },

  // Inventory rows holding this printing with copies still in the box.
  locations(req) {
    const inv = readInventory(req.owner);
    const rows = inv.rows
      .filter((r) => r.scryfallId === req.scryfallId && r.finish === req.finish && r.inBox > 0)
      .map((r) => ({ row: r.row, location: r.location, condition: r.condition, inBox: r.inBox }));
    return { locations: rows };
  },

  offer(req) {
    const qty = Number(req.qty);
    if (!Number.isInteger(qty) || qty < 1) throw new Error("Quantity must be a whole number of at least 1");
    if (!req.scryfallId || !req.finish) throw new Error("Missing card details");
    const sheet = ordersSheet(req.owner);
    const id = Utilities.getUuid().slice(0, 8);
    const values = {
      "Order": id, "Status": AWAITING, "Card": req.name || "", "Set": req.set || "", "Finish": req.finish,
      "Scryfall ID": req.scryfallId, "Qty": qty, "Price (USD)": Number(req.price) || "", "Location": "",
      "Inventory row": "", "Offered": new Date(), "Paid": "", "Closed": "",
    };
    sheet.appendRow(ORDER_HEADERS.map((h) => values[h]));
    return { order: findOrder(req.owner, id).order };
  },

  paid(req) {
    const { order, update } = findOrder(req.owner, req.id);
    if (order.status !== AWAITING) throw new Error(`Order is ${order.status}, not ${AWAITING}`);
    const inv = readInventory(req.owner);
    const row = inv.rows.find((r) => r.row === Number(req.row));
    if (!row || row.scryfallId !== order.scryfallId || row.finish !== order.finish) {
      throw new Error("That Inventory row no longer holds this card — reload and pick again");
    }
    if (row.inBox < order.qty) throw new Error(`Only ${row.inBox} in the box at ${row.location}`);
    inv.add(row, "Pulled", order.qty);
    update({ "Status": PAID, "Location": row.location, "Inventory row": row.row, "Paid": new Date() });
    return { order: findOrder(req.owner, req.id).order };
  },

  droppedOff(req) {
    const { order, update } = findOrder(req.owner, req.id);
    if (order.status !== PAID) throw new Error(`Order is ${order.status}, not ${PAID}`);
    const inv = readInventory(req.owner);
    const row = pulledRow(inv, order);
    inv.add(row, "Pulled", -order.qty);
    inv.add(row, "Sold", order.qty);
    update({ "Status": SOLD, "Closed": new Date() });
    return { order: findOrder(req.owner, req.id).order };
  },

  cancel(req) {
    const { order, update } = findOrder(req.owner, req.id);
    if (order.status === PAID) {
      const inv = readInventory(req.owner);
      inv.add(pulledRow(inv, order), "Pulled", -order.qty);
    } else if (order.status !== AWAITING) {
      throw new Error(`Order is already ${order.status}`);
    }
    update({ "Status": CANCELLED, "Closed": new Date() });
    return { order: findOrder(req.owner, req.id).order };
  },
};

// The row a paid order pulled from, checked against what it recorded —
// rows are never deleted, but a sort of the Inventory tab would move them.
function pulledRow(inv, order) {
  const row = inv.rows.find((r) => r.row === order.inventoryRow);
  if (!row || row.scryfallId !== order.scryfallId || row.finish !== order.finish || row.location !== order.location) {
    throw new Error(`Inventory row ${order.inventoryRow} no longer holds this card at ${order.location} — fix Sold/Pulled by hand`);
  }
  if (row.pulled < order.qty) {
    throw new Error(`Pulled is only ${row.pulled} at ${row.location} — fix Sold/Pulled by hand`);
  }
  return row;
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
    sheet = ss.insertSheet(ORDERS_TAB);
    sheet.appendRow(ORDER_HEADERS);
    sheet.setFrozenRows(1);
  }
  return sheet;
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
  }));
  return { sheet, col, orders };
}

function findOrder(owner, id) {
  const { sheet, col, orders } = readOrders(owner);
  const order = orders.find((o) => o.id === String(id));
  if (!order) throw new Error(`No order ${id} in ${owner}'s sheet`);
  return {
    order,
    update(fields) {
      for (const [name, value] of Object.entries(fields)) {
        sheet.getRange(order.sheetRow, col[name] + 1).setValue(value);
      }
    },
  };
}
