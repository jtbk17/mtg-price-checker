"""End-to-end check of the dashboard's Orders section: the real
docs/index.html in a real browser, its Apps Script calls answered by the
real google_apps_script.js running against fake spreadsheets
(test_orders_mocks.js) — so the sheet arithmetic (Pulled, Sold, In box),
the order flow and both files' version handling are tested together.

Run with `python test_orders_e2e.py` (needs `pip install playwright` and
`playwright install chromium`); exits non-zero if any check fails.
tests.yml runs it on every push touching either file.
"""
import csv
import functools
import http.server
import io
import json
import re
import sys
import tempfile
import threading
from pathlib import Path

from playwright.sync_api import sync_playwright

sys.stdout.reconfigure(encoding="utf-8")
REPO = Path(__file__).parent
SHOTS = Path(tempfile.mkdtemp())  # screenshots, for looking at a failure locally
API = "https://script.google.com/macros/s/TEST/exec"
html = (REPO / "docs" / "index.html").read_text(encoding="utf-8")
real = re.search(r'const SHEETS_API_URL = "([^"]*)";', html).group(1)
assert real.startswith("https://script.google.com/macros/s/")
test_html = html.replace(real, API)
off_html = html.replace(real, "")


def item(**k):
    base = {"name": "", "set_name": "Set", "printing": "Normal", "condition": "Near Mint", "owner": "Shared", "quantity": 1,
            "image_url": None, "latest_price": 10.0, "previous_price": 10.0, "cardkingdom_buylist_price": 3.0,
            "tcgmarketplace_price": 9.5, "tcgmarketplace_wtb_price": None, "history": [], "buylist_history": []}
    return {**base, **k}


watchlist = {"generated_at": "2026-11-15T00:00:00Z", "items": [
    item(name="Psychic Frog", card_id="frog", quantity=2, tcgmarketplace_wtb_price=9.0),
    item(name="Snap", card_id="snap", printing="Foil", owner="Joel", tcgmarketplace_wtb_price=5.0, latest_price=5.0, tcgmarketplace_price=5.2),
    item(name="No Offer", card_id="none"),
    item(name="Bulk Card", card_id="bulk", quantity=18, tcgmarketplace_wtb_price=1.0, latest_price=1.1, tcgmarketplace_price=1.2),
    item(name="Sol Ring", card_id="sol", printing="Foil", latest_price=28.0, tcgmarketplace_price=27.5, tcgmarketplace_listing_key="1|1|en|NM"),
]}
my_listings = {"generated_at": "2026-11-15T00:00:00Z", "listings": {"1|1|en|NM": {"quantity": 1, "price": 27.9}}}


def card(owner, cid, name, printing="Normal"):
    return {"owner": owner, "card_id": cid, "name": name, "set_name": "Set", "printing": printing, "condition": "Near Mint", "image_url": None}


sales = {"drops": [
    {"id": "2|1|en|NM@2026-11-15T10:00:00+00:00", "key": "2|1|en|NM", "qty_before": 1, "qty_after": 0, "price": 27.9,
     "detected_at": "2026-11-15T10:00:00+00:00", "cards": [card("Shared", "sol", "Sol Ring", "Foil"), card("Joel", "sol", "Sol Ring", "Foil")]},
    {"id": "3|0|en|NM@2026-11-15T11:00:00+00:00", "key": "3|0|en|NM", "qty_before": 2, "qty_after": 0, "price": 21.9,
     "detected_at": "2026-11-15T11:00:00+00:00", "cards": [card("Shared", "waste", "Wasteland")]},
    {"id": "9|0|en|NM@2026-11-15T12:00:00+00:00", "key": "9|0|en|NM", "qty_before": 1, "qty_after": 0, "price": 5.0,
     "detected_at": "2026-11-15T12:00:00+00:00", "cards": []},
]}

http.server.SimpleHTTPRequestHandler.log_message = lambda *a: None
srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), functools.partial(http.server.SimpleHTTPRequestHandler, directory=str(REPO / "docs")))
BASE = f"http://127.0.0.1:{srv.server_address[1]}"
threading.Thread(target=srv.serve_forever, daemon=True).start()
results = []


def check(name, cond, extra=""):
    results.append(bool(cond))
    print(("PASS " if cond else "FAIL ") + name + (f"  [{extra}]" if extra and not cond else ""))


with sync_playwright() as p:
    try:
        b = p.chromium.launch()
    except Exception:
        b = p.chromium.launch(channel="msedge")  # no bundled Chromium locally: use Edge
    ctx = b.new_context(viewport={"width": 1280, "height": 1000})
    gas = ctx.new_page()
    gas.set_content("<html><body></body></html>")
    gas.add_script_tag(path=str(REPO / "test_orders_mocks.js"))
    gas.add_script_tag(path=str(REPO / "google_apps_script.js"))
    calls = []

    def api(route):
        body = route.request.post_data
        calls.append(json.loads(body)["action"])
        out = gas.evaluate("body => doPost({ postData: { contents: body } }).s", body)
        route.fulfill(status=200, content_type="application/json", body=out, headers={"Access-Control-Allow-Origin": "*"})

    def open_dashboard(page_html):
        pg = ctx.new_page()
        errs = []
        pg.on("pageerror", lambda e: errs.append(str(e)))
        pg.on("dialog", lambda d: d.accept())
        pg.route(API, api)
        pg.route("**/index.html", lambda r: r.fulfill(status=200, content_type="text/html", body=page_html))
        pg.route("**/watchlist.json", lambda r: r.fulfill(json=watchlist))
        pg.route("**/my_listings.json", lambda r: r.fulfill(json=my_listings))
        pg.route("**/listing_sales.json", lambda r: r.fulfill(json=sales))
        pg.add_init_script("localStorage.setItem('sellPlanOwner','')")
        pg.goto(f"{BASE}/index.html")
        pg.wait_for_timeout(800)
        return pg, errs

    pg, errs = open_dashboard(test_html)

    def cell(book, row, field):
        return gas.evaluate("([b,r,f]) => { const s=__books[b].Inventory; return s.getDataRange().getValues()[r][s.rows[0].indexOf(f)]; }", [book, row, field])

    def orders(book):
        return gas.evaluate("b => __books[b].Orders ? __books[b].Orders.getDataRange().getValues().map(r=>r.map(v=>v instanceof Date?'DATE':v)) : []", book)

    def tabs(book):
        return gas.evaluate("b => Object.keys(__books[b])", book)

    def sell(name):
        return pg.locator("#sell-plan .card", has_text=name)

    def order_card(text):
        return pg.locator("#orders-list .card", has_text=text)

    def looks_sold():
        return pg.locator("#orders-list .card", has_text="Looks sold?")

    frog = lambda: sell("Psychic Frog")

    # ---- connecting ----
    check("section + nav link shown", pg.is_visible("#orders-section") and pg.is_visible("#orders-link"))
    check("connect form shown, no sale buttons yet", pg.is_visible("#orders-connect") and pg.locator("text=Sold to WTB buyer").count() == 0)
    pg.fill("#orders-passcode", "nope"); pg.click("#orders-connect button"); pg.wait_for_timeout(400)
    check("wrong passcode rejected", pg.is_visible("#orders-connect") and "Wrong passcode" in pg.inner_text("#orders-connect-error"))
    pg.fill("#orders-passcode", "pw"); pg.click("#orders-connect button"); pg.wait_for_timeout(600)
    check("connected", not pg.is_visible("#orders-connect"))

    # ---- old "WTB orders" tab upgraded in place ----
    o = orders("S")
    check("legacy tab renamed to Orders, columns added, old row kept",
          "WTB orders" not in tabs("S") and "Orders" in tabs("S") and o[0][-2:] == ["Type", "Source"] and o[1][0] == "legacy01", (tabs("S"), o[:2]))
    check("legacy order shows as WTB", "WTB · Shared" in order_card("Old Card").inner_text())

    # ---- buttons per card ----
    check("WTB button only on cards with an offer",
          frog().locator("text=Sold to WTB buyer").count() == 1 and sell("Snap").locator("text=Sold to WTB buyer").count() == 1
          and sell("No Offer").locator("text=Sold to WTB buyer").count() == 0
          and [b.strip() for b in sell("No Offer").locator("button").all_inner_texts()] == ["Sold to GOG"])
    check("listed card gets Sold on TCGMP instead",
          sell("Sol Ring").locator("text=Sold on TCGMP").count() == 1 and sell("Sol Ring").locator("text=Sold to WTB buyer").count() == 0)

    # ---- WTB flow ----
    check("copies box up front", frog().locator("input").count() == 1 and sell("Snap").locator("input").count() == 0)
    frog().locator("input").fill("3"); frog().locator("text=Sold to WTB buyer").click()
    check("qty above copies rejected before sending", "Enter 1–2" in frog().inner_text())
    frog().locator("input").fill("2"); frog().locator("text=Sold to WTB buyer").click()
    check("confirm shows count and total", frog().locator("text=Record 2 at $9.00 = $18.00").count() == 1)
    frog().locator("text=Back").click()
    check("back keeps the chosen count", frog().locator("input").input_value() == "2")
    frog().locator("text=Sold to WTB buyer").click(); frog().locator("text=Record 2 at $9.00 = $18.00").click(); pg.wait_for_timeout(500)
    o = orders("S")
    check("WTB order row written with Type WTB", o[-1][1] == "Awaiting payment" and o[-1][6] == 2 and o[-1][13] == "WTB" and o[-1][14] == "", o[-1])
    check("sell card shows 2 in orders, no button left", "2 in orders" in frog().inner_text() and frog().locator("text=Sold to WTB buyer").count() == 0)
    pg.wait_for_timeout(400)
    where = order_card("Psychic Frog ×2").inner_text()
    check("awaiting order lists every box location up front, in box order",
          "In the box at B01-01 ×1 (row 2) · B01-02 ×1 (row 3)" in where and "B01-03" not in where, where)
    check("card not in the box says so", "None left in the box" in order_card("Old Card").inner_text())
    two = order_card("Psychic Frog ×2")
    two.locator("text=Buyer paid").click(); pg.wait_for_timeout(400)
    boxes = two.locator('.pick [type="checkbox"]')
    check("2 copies over two boxes: both pre-ticked with 1 each, button ready",
          boxes.count() == 2 and boxes.nth(0).is_checked() and boxes.nth(1).is_checked()
          and two.locator('.pick [type="number"]').nth(0).input_value() == "1" and "Picking 2 of 2" in two.inner_text()
          and two.locator("text=Pull 2 & mark paid").is_enabled())
    boxes.nth(1).uncheck()
    check("untick one: short of the order, button off", "Picking 1 of 2" in two.inner_text() and not two.locator("text=Pull 2 & mark paid").is_enabled())
    two.locator("text=Back").click(); pg.wait_for_timeout(300)
    order_card("Psychic Frog ×2").locator("text=Buyer cancelled").click(); pg.wait_for_timeout(500)
    check("awaiting order cancelled, Inventory untouched",
          "Cancelled" in order_card("Psychic Frog ×2").inner_text() and cell("S", 1, "Pulled") == "" and cell("S", 2, "Pulled") == "")
    frog().locator("input").fill("1"); frog().locator("text=Sold to WTB buyer").click(); frog().locator("text=Record 1 at $9.00").click(); pg.wait_for_timeout(500)
    c = order_card("Psychic Frog").filter(has_text="Awaiting payment")
    c.locator("text=Buyer paid").click(); pg.wait_for_timeout(400)
    labels = [" ".join(t.split()) for t in c.locator(".pick label").all_inner_texts()]
    check("picker lists both normal rows with row numbers, not the foil row",
          labels == ["B01-01 row 2 · 1 in box", "B01-02 row 3 · 1 in box"], labels)
    boxes = c.locator('.pick [type="checkbox"]')
    check("one location holds it all: just that one ticked",
          boxes.nth(0).is_checked() and not boxes.nth(1).is_checked() and c.locator('.pick [type="number"]').nth(1).is_disabled())
    boxes.nth(0).uncheck(); boxes.nth(1).check()
    c.locator("text=Pull 1 & mark paid").click(); pg.wait_for_timeout(500)
    check("paid: Pulled=1 on B01-02 only", cell("S", 2, "Pulled") == 1 and cell("S", 2, "In box") == 0 and cell("S", 1, "Pulled") == "")
    check("paid order shows where it came from, with row", order_card("pulled from B01-02 (row 3)").count() == 1)
    order_card("pulled from B01-02").locator("text=Dropped off").click(); pg.wait_for_timeout(500)
    check("dropped off: Pulled -> Sold", cell("S", 2, "Pulled") == "" and cell("S", 2, "Sold") == 1 and cell("S", 2, "In box") == 0)

    # ---- one order, copies spread over several boxes ----
    bulk = lambda: sell("Bulk Card")
    bulk().locator("input").fill("10"); bulk().locator("text=Sold to WTB buyer").click()
    bulk().locator("text=Record 10 at $1.00 = $10.00").click(); pg.wait_for_timeout(600)
    bc = order_card("Bulk Card ×10")
    check("10 copies: every box location listed up front",
          "In the box at B05-01 ×6 (row 7) · B05-02 ×9 (row 8) · B05-03 ×3 (row 9)" in bc.inner_text(), bc.inner_text())
    bc.locator("text=Buyer paid").click(); pg.wait_for_timeout(400)
    qty = bc.locator('.pick [type="number"]')
    boxes = bc.locator('.pick [type="checkbox"]')
    check("no box has 10: pre-filled box by box (6 + 4), third unticked",
          [qty.nth(i).input_value() for i in range(2)] == ["6", "4"] and not boxes.nth(2).is_checked() and "Picking 10 of 10" in bc.inner_text())
    pg.set_viewport_size({"width": 390, "height": 900}); pg.wait_for_timeout(200)
    bc.scroll_into_view_if_needed(); bc.screenshot(path=SHOTS / "picker_phone.png")
    pg.set_viewport_size({"width": 1280, "height": 1000}); pg.wait_for_timeout(200)
    qty.nth(0).fill("1"); qty.nth(1).fill("7")
    check("totals follow edits", "Picking 8 of 10" in bc.inner_text() and not bc.locator("text=Pull 10 & mark paid").is_enabled())
    boxes.nth(2).check(); qty.nth(2).fill("4")
    check("more than that box holds is refused",
          "up to what's in the box" in bc.inner_text() and not bc.locator("text=Pull 10 & mark paid").is_enabled())
    qty.nth(2).fill("2")
    check("1 + 7 + 2 = 10: ready", "Picking 10 of 10" in bc.inner_text() and bc.locator("text=Pull 10 & mark paid").is_enabled())
    bc.locator("text=Pull 10 & mark paid").click(); pg.wait_for_timeout(600)
    check("each row gets its own Pulled", [cell("S", r, "Pulled") for r in (6, 7, 8)] == [1, 7, 2])
    o = orders("S")[-1]
    check("order records every location and row", o[8] == "B05-01 ×1, B05-02 ×7, B05-03 ×2" and o[9] == "7 ×1, 8 ×7, 9 ×2", o[8:10])
    check("order card shows the split",
          order_card("pulled from B05-01 ×1 (row 7), B05-02 ×7 (row 8), B05-03 ×2 (row 9)").count() == 1)
    order_card("Bulk Card ×10").locator("text=Dropped off").click(); pg.wait_for_timeout(600)
    check("dropped off: each row's Pulled moved to its Sold",
          [cell("S", r, "Pulled") for r in (6, 7, 8)] == ["", "", ""] and [cell("S", r, "Sold") for r in (6, 7, 8)] == [1, 7, 2])
    bulk().locator("input").fill("7"); bulk().locator("text=Sold to WTB buyer").click()
    bulk().locator("text=Record 7 at $1.00 = $7.00").click(); pg.wait_for_timeout(600)
    bc = order_card("Bulk Card ×7")
    bc.locator("text=Buyer paid").click(); pg.wait_for_timeout(400)
    bc.locator("text=Pull 7 & mark paid").click(); pg.wait_for_timeout(600)
    check("second split (5 + 2) pulled", [cell("S", r, "Pulled") for r in (6, 7, 8)] == [5, 2, ""])
    order_card("Bulk Card ×7").locator("text=Cancelled — put back").click(); pg.wait_for_timeout(600)
    check("cancel after a split pull puts every row back", [cell("S", r, "Pulled") for r in (6, 7, 8)] == ["", "", ""])

    # ---- manual listing sale ----
    sell("Sol Ring").locator("text=Sold on TCGMP").click(); sell("Sol Ring").locator("text=Record 1 at $27.90").click(); pg.wait_for_timeout(500)
    o = orders("S")
    check("manual listing order: Type Listing, listed price, no source",
          o[-1][2] == "Sol Ring" and o[-1][7] == 27.9 and o[-1][13] == "Listing" and o[-1][14] == "", o[-1])
    check("order card labelled Listing", "Listing · Shared" in order_card("Sol Ring").filter(has_text="Awaiting payment").inner_text())

    # ---- "Looks sold?" drops ----
    check("three drops shown, newest first", looks_sold().count() == 3 and "Unmatched listing" in looks_sold().nth(0).inner_text())
    sol = looks_sold().filter(has_text="Sol Ring")
    check("drop warns when the same card was already recorded by hand", "Already recorded by hand" in sol.inner_text(), sol.inner_text())
    check("drops without a hand-recorded twin carry no warning", "Already recorded by hand" not in looks_sold().filter(has_text="Wasteland").inner_text())
    check("two owners -> whose-copy picker", sol.locator("select option").all_inner_texts() == ["Shared's copy", "Joel's copy"])
    sol.locator("select").select_option(label="Joel's copy"); sol.locator("text=Sold — record it").click(); pg.wait_for_timeout(600)
    oj = orders("J")
    check("recorded into Joel's sheet with the drop as source",
          oj[-1][2] == "Sol Ring" and oj[-1][13] == "Listing" and oj[-1][14] == sales["drops"][0]["id"], oj[-1:])
    check("recorded drop no longer asked about", looks_sold().filter(has_text="Sol Ring").count() == 0)
    dup = gas.evaluate(
        "s => JSON.parse(doPost({postData:{contents: JSON.stringify({action:'offer',passcode:'pw',owner:'Joel',scryfallId:'sol',finish:'foil',qty:1,type:'Listing',source:s})}}).s)",
        sales["drops"][0]["id"])
    check("same drop can't be recorded twice", not dup["ok"] and "already recorded" in dup["error"], dup)
    dup2 = gas.evaluate(
        "s => JSON.parse(doPost({postData:{contents: JSON.stringify({action:'offer',passcode:'pw',owner:'Shared',scryfallId:'sol',finish:'foil',qty:1,type:'Listing',source:s})}}).s)",
        sales["drops"][0]["id"])
    check("...not even into the other owner's sheet", not dup2["ok"] and "already recorded" in dup2["error"], dup2)
    waste = looks_sold().filter(has_text="Wasteland")
    check("multi-copy drop defaults to copies gone", waste.locator("input").input_value() == "2")
    waste.locator("input").fill("1"); waste.locator("text=Sold — record it").click(); pg.wait_for_timeout(600)
    check("drop recorded with chosen copies", orders("S")[-1][2] == "Wasteland" and orders("S")[-1][6] == 1)
    wc = order_card("Wasteland").filter(has_text="Awaiting payment")
    pg.wait_for_timeout(400)
    check("single location shown as Take from", "Take from B04-02 ×2 (row 6)" in wc.inner_text(), wc.inner_text())
    wc.locator("text=Buyer paid").click(); pg.wait_for_timeout(400); wc.locator("text=Pull 1 & mark paid").click(); pg.wait_for_timeout(500)
    check("listing order runs the same paid flow", cell("S", 5, "Pulled") == 1 and cell("S", 5, "In box") == 1)
    unmatched = order_card("Unmatched listing")
    check("unmatched drop explains, offers only Not a sale",
          "record it from the Sell plan" in unmatched.inner_text() and unmatched.locator("button").all_inner_texts() == ["Not a sale"])
    unmatched.locator("text=Not a sale").click(); pg.wait_for_timeout(500)
    check("Not a sale: gone and remembered",
          looks_sold().count() == 0 and sales["drops"][2]["id"] in gas.evaluate("() => __props.DISMISSED"))
    pg.reload(); pg.wait_for_timeout(1000)
    check("after reload: answered drops stay answered", looks_sold().count() == 0)
    not_sale = pg.locator("#orders-list .card", has_text="Marked not a sale")
    check("Not a sale stays in view for a day, with Undo", not_sale.count() == 1 and not_sale.locator("text=Undo").count() == 1)
    not_sale.locator("text=Undo").click(); pg.wait_for_timeout(600)
    check("Undo brings back Looks sold?", looks_sold().count() == 1 and not_sale.count() == 0
          and sales["drops"][2]["id"] not in gas.evaluate("() => __props.DISMISSED"))
    looks_sold().locator("text=Not a sale").click(); pg.wait_for_timeout(500)
    gas.evaluate("""id => { const d = JSON.parse(__props.DISMISSED); d[id] = new Date(Date.now() - 25 * 3600 * 1000).toISOString();
        __props.DISMISSED = JSON.stringify(d); }""", sales["drops"][2]["id"])
    pg.reload(); pg.wait_for_timeout(1000)
    late = json.loads(gas.evaluate("id => doPost({postData:{contents: JSON.stringify({action:'undismiss',passcode:'pw',source:id})}}).s", sales["drops"][2]["id"]))
    check("no Undo after 24 hours", not_sale.count() == 0 and looks_sold().count() == 0 and not late["ok"], late)

    # ---- fewer round trips (Apps Script v4) ----
    n = len(calls); pg.reload(); pg.wait_for_timeout(1200)
    # (v6 adds one read for Add cards, which needs no lock and no list.)
    check("loading: box locations come with the list, no call per order", calls[n:] == ["list", "intakeStatus"], calls[n:])
    check("waiting orders still show where to take them", "Take from B02-01 ×1 (row 3)" in order_card("Sol Ring").filter(has_text="Joel").inner_text())

    # ---- deleting cancelled orders ----
    joel_sol = lambda: order_card("Sol Ring").filter(has_text="Joel")
    n = len(calls); joel_sol().locator("text=Buyer cancelled").click(); pg.wait_for_timeout(600)
    check("a change is one call (its answer carries the list)", calls[n:] == ["cancel"], calls[n:])
    check("only cancelled orders get Delete",
          pg.locator("#orders-list .card", has_text="Awaiting payment").locator("text=Delete").count() == 0
          and pg.locator("#orders-list .card", has_text="pulled from").locator("text=Delete").count() == 0
          and joel_sol().locator("text=Delete").count() == 1)
    rows_before = len(orders("J"))
    joel_sol().locator("text=Delete").click(); pg.wait_for_timeout(600)
    check("Delete removes the order's row", len(orders("J")) == rows_before - 1 and joel_sol().count() == 0)
    check("its Looks sold? flag stays answered", looks_sold().count() == 0 and sales["drops"][0]["id"] in gas.evaluate("() => __props.DISMISSED"))
    pg.reload(); pg.wait_for_timeout(1000)
    check("...after a reload too", looks_sold().count() == 0 and joel_sol().count() == 0)
    waiting = order_card("Old Card")
    refused = gas.evaluate(
        "id => JSON.parse(doPost({postData:{contents: JSON.stringify({action:'remove',passcode:'pw',owner:'Shared',id})}}).s)", "legacy01")
    check("the script refuses to delete an order that isn't cancelled", not refused["ok"] and "Only cancelled" in refused["error"], refused)

    # ---- Apps Script v5: undo, fee, editable price, both sale kinds ----
    def card_for(text, status):
        return order_card(text).filter(has_text=status)

    bulk().locator("input").fill("1"); bulk().locator("text=Sold to WTB buyer").click()
    check("confirm shows what you get after TCGMP's 4%", "You get $0.96 after TCGMP's 4%" in bulk().inner_text())
    bulk().locator(".price-input").fill("2.5")
    check("price can be changed before recording", bulk().locator("text=Record 1 at $2.50").count() == 1
          and "You get $2.40 after TCGMP's 4%" in bulk().inner_text())
    bulk().locator("text=Record 1 at $2.50").click(); pg.wait_for_timeout(600)
    o = orders("S")[-1]
    check("edited price is what's recorded", o[2] == "Bulk Card" and o[7] == 2.5, o)
    check("order card shows the after-fee amount", "You get $2.40 after TCGMP's 4%" in card_for("Bulk Card", "Awaiting payment").inner_text())
    bc = card_for("Bulk Card", "Awaiting payment")
    bc.locator("text=Buyer paid").click(); pg.wait_for_timeout(400); bc.locator("text=Pull 1 & mark paid").click(); pg.wait_for_timeout(600)
    row_pulled = lambda: [cell("S", r, "Pulled") for r in (6, 7, 8)]
    after_paid = row_pulled()
    card_for("Bulk Card", "pulled from").locator("text=Undo paid").click(); pg.wait_for_timeout(600)
    check("Undo paid: back to awaiting payment, copies back in the box",
          card_for("Bulk Card", "Awaiting payment").count() == 1 and row_pulled() == ["", "", ""] and after_paid != ["", "", ""])
    bc = card_for("Bulk Card", "Awaiting payment")
    bc.locator("text=Buyer paid").click(); pg.wait_for_timeout(400); bc.locator("text=Pull 1 & mark paid").click(); pg.wait_for_timeout(600)
    sold_before = [cell("S", r, "Sold") for r in (6, 7, 8)]
    card_for("Bulk Card", "pulled from").locator("text=Dropped off").click(); pg.wait_for_timeout(600)
    card_for("Bulk Card ", "Sold · dropped off").filter(has_text="$2.50").locator("text=Undo drop-off").click(); pg.wait_for_timeout(600)
    check("Undo drop-off: back to paid, Sold moved back to Pulled",
          [cell("S", r, "Sold") for r in (6, 7, 8)] == sold_before and row_pulled() == after_paid
          and card_for("Bulk Card", "pulled from").filter(has_text="$2.50").count() == 1)
    card_for("Bulk Card", "pulled from").filter(has_text="$2.50").locator("text=Cancelled — put back").click(); pg.wait_for_timeout(600)
    check("cancel after paid puts it back", row_pulled() == ["", "", ""])
    card_for("Bulk Card", "Cancelled").filter(has_text="$2.50").locator("text=Undo cancel").click(); pg.wait_for_timeout(600)
    check("Undo cancel (was paid): pulled again from the same rows", row_pulled() == after_paid
          and card_for("Bulk Card", "pulled from").filter(has_text="$2.50").count() == 1)
    old_undo = gas.evaluate("""() => { const s = __books.S.Orders; const h = s.rows[0];
        const r = s.rows.find((x) => x[h.indexOf("Card")] === "Old Card"); return r[h.indexOf("Status")]; }""")
    gas.evaluate("""() => { const s = __books.S.Orders; const h = s.rows[0];
        const r = s.rows.find((x) => x[h.indexOf("Card")] === "Psychic Frog" && x[h.indexOf("Status")] === "Sold");
        r[h.indexOf("Closed")] = new Date(Date.now() - 25 * 3600 * 1000); }""")
    pg.reload(); pg.wait_for_timeout(1200)
    check("no Undo once a change is over 24 hours old",
          card_for("Psychic Frog", "Sold · dropped off").locator("text=Undo drop-off").count() == 0 and old_undo == "Awaiting payment")

    # ---- paid orders: pick list in box order, several dropped off at once ----
    panel = pg.locator("#orders-paid")
    lines = [" ".join(t.split()) for t in panel.locator(".pick-list li").all_inner_texts()]
    check("paid panel lists every paid order in box order",
          len(lines) >= 2 and lines == sorted(lines) and any("Bulk Card" in t for t in lines), lines)
    check("drop-off button waits for a tick", not panel.locator("text=Tick the ones you dropped off").is_enabled())
    panel.locator("text=Select all").click()
    n_paid = len(lines)
    check("select all ticks every one", panel.locator(f"text=Mark {n_paid} dropped off").is_enabled())
    n = len(calls)
    panel.locator(f"text=Mark {n_paid} dropped off").click(); pg.wait_for_timeout(800)
    check("all dropped off in one call", calls[n:] == ["droppedOffMany"] and panel.is_hidden()
          and order_card("pulled from").count() == 0, calls[n:])
    check("their copies moved from Pulled to Sold", row_pulled() == ["", "", ""])

    # ---- sales summary ----
    tiles = [" ".join(t.split()) for t in pg.locator("#orders-summary > div").all_inner_texts()]
    gross = gas.evaluate("""() => { let g = 0; for (const b of Object.values(__books)) { const s = b.Orders; if (!s) continue;
        const h = s.rows[0]; s.rows.slice(1).forEach((r) => { if (r[h.indexOf("Status")] === "Sold") g += r[h.indexOf("Price (USD)")] * r[h.indexOf("Qty")]; }); }
        return Math.round(g * 100) / 100; }""")
    net = f"${gross * 0.96:,.2f}"
    check("summary: all-time sales after the 4% fee", any(t.startswith("All time") and net in t for t in tiles), (net, tiles))
    check("summary: nothing paid but not dropped off now", any(t.startswith("Paid, not dropped off $0.00") for t in tiles), tiles)

    # ---- a listed card that also has a WTB offer ----
    watchlist["items"].append(item(name="Both Ways", card_id="both", printing="Foil", latest_price=28.0, tcgmarketplace_price=27.5,
                                   tcgmarketplace_wtb_price=20.0, tcgmarketplace_listing_key="5|1|en|NM"))
    my_listings["listings"]["5|1|en|NM"] = {"quantity": 1, "price": 26.9}
    pg.reload(); pg.wait_for_timeout(1200)
    both = sell("Both Ways")
    check("listed card with a WTB offer can be recorded either way",
          both.locator("text=Sold on TCGMP").count() == 1 and both.locator("text=Sold to WTB buyer").count() == 1)
    both.locator("text=Sold to WTB buyer").click()
    check("...and the WTB route uses the offer's price", both.locator(".price-input").input_value() == "20.00")
    both.locator("text=Back").click()

    # ---- the list cache: served between changes, refreshed by every change ----
    def api_list():
        return json.loads(gas.evaluate("() => doPost({postData:{contents: JSON.stringify({action:'list',passcode:'pw'})}}).s"))

    gas.evaluate("() => { __cacheOn = true; __cache = {}; }")
    api_list()
    gas.evaluate("() => { const s = __books.S.Orders; s.rows.push(s.rows[1].slice()); s.rows[s.rows.length - 1][0] = 'byhand01'; }")
    cached_ids = [o["id"] for o in api_list()["orders"]]
    gas.evaluate("() => { const s = __books.S.Orders; const h = s.rows[0]; s.rows[s.rows.length - 1][h.indexOf('Status')] = 'Cancelled'; s.rows[s.rows.length - 1][h.indexOf('Closed')] = new Date(); }")
    changed = json.loads(gas.evaluate("() => doPost({postData:{contents: JSON.stringify({action:'remove',passcode:'pw',owner:'Shared',id:'byhand01'})}}).s"))
    check("list is cached between changes (a hand edit shows within the minute, not instantly)",
          "byhand01" not in cached_ids and changed["ok"] and "byhand01" not in [o["id"] for o in changed["orders"]], changed.get("error"))
    gas.evaluate("() => { __cacheOn = false; }")

    # ---- Sold to GOG: GOG Cash, no fee, pull then sold at the store ----
    watchlist["items"].append(item(name="Wasteland", card_id="waste", quantity=2, cardkingdom_buylist_price=10.0,
                                   latest_price=14.0, tcgmarketplace_price=13.5))
    pg.reload(); pg.wait_for_timeout(1500)

    def waste_row(field):
        return gas.evaluate("""f => { const s = __books.S.Inventory; const h = s.rows[0];
            const r = s.getDataRange().getValues().find((x) => x[h.indexOf("Scryfall ID")] === "waste" && x[h.indexOf("Location")] === "B04-02");
            return Number(r[h.indexOf(f)]) || 0; }""", field)

    waste = sell("Wasteland")
    check("Sold to GOG on a card GOG buys", waste.locator("text=Sold to GOG").count() == 1)
    check("...and on every card with a GOG price, not just 'Sell to GOG' ones", sell("No Offer").locator("text=Sold to GOG").count() == 1)
    waste.locator("text=Sold to GOG").click()
    check("GOG Cash price, no TCGMP fee", waste.locator(".price-input").input_value() == "11.50" and "GOG Cash, no fee" in waste.inner_text())
    waste.locator("text=Record 1 at $11.50").click(); pg.wait_for_timeout(700)
    o = orders("S")[-1]
    check("recorded as a GOG order", o[2] == "Wasteland" and o[7] == 11.5 and o[13] == "GOG" and o[1] == "Awaiting payment", o)
    wc = lambda status: order_card("Wasteland").filter(has_text=status)
    card_text = wc("To pull for GOG").inner_text()
    check("order says where to take it from", "To pull for GOG" in card_text and "B04-02" in card_text and "GOG Cash, no fee" in card_text, card_text)
    pulled0, sold0 = waste_row("Pulled"), waste_row("Sold")
    wc("To pull for GOG").locator("text=Pull it").click(); pg.wait_for_timeout(400)
    wc("To pull for GOG").locator("text=Pull 1 for GOG").click(); pg.wait_for_timeout(700)
    check("pulled: Pulled +1 on its row", waste_row("Pulled") == pulled0 + 1)
    check("then: bring to GOG", wc("bring to GOG").count() == 1 and wc("bring to GOG").locator("text=Sold at GOG").count() == 1)
    check("pick list marks it as going to GOG", "to GOG" in pg.inner_text("#orders-paid"))
    wc("bring to GOG").locator("text=Sold at GOG").click(); pg.wait_for_timeout(700)
    check("sold at GOG: Pulled back, Sold +1", waste_row("Pulled") == pulled0 and waste_row("Sold") == sold0 + 1)
    check("shows as sold at GOG, can be undone", wc("Sold at GOG").count() == 1 and wc("Sold at GOG").locator("text=Undo sold").count() == 1)
    tiles = [" ".join(t.split()) for t in pg.locator("#orders-summary > div").all_inner_texts()]
    by_type = gas.evaluate("""() => { const g = { GOG: 0, other: 0 }; for (const b of Object.values(__books)) { const s = b.Orders; if (!s) continue;
        const h = s.rows[0]; s.rows.slice(1).forEach((r) => { if (r[h.indexOf("Status")] === "Sold")
          g[r[h.indexOf("Type")] === "GOG" ? "GOG" : "other"] += r[h.indexOf("Price (USD)")] * r[h.indexOf("Qty")]; }); }
        return g; }""")
    net = f"${by_type['other'] * 0.96 + by_type['GOG']:,.2f}"
    check("summary: GOG sales count in full, no 4% off them", by_type["GOG"] == 11.5 and any(t.startswith("All time") and net in t for t in tiles), (net, tiles))

    # ---- Add cards: a ManaBox CSV logged behind the next empty slot ----
    MANABOX = ["Name", "Set code", "Set name", "Collector number", "Foil", "Rarity", "Quantity", "ManaBox ID", "Scryfall ID",
               "Purchase price", "Misprint", "Altered", "Signed", "Condition", "Language", "Proxy", "Purchase price currency", "Added"]

    def uid(n):
        return f"{n:08x}-0000-4000-8000-000000000000"

    def mb(name, n, qty=1, price="1.50", foil="normal", added="2026-11-21T03:04:34.722382Z"):
        return [name, "msh", "Marvel Super Heroes", str(100 + n), foil, "rare", str(qty), str(n), uid(n), price,
                "FALSE", "FALSE", "FALSE", "near_mint", "en", "FALSE", "USD", added]

    def csv_bytes(rows, header=MANABOX):
        buf = io.StringIO()
        csv.writer(buf, lineterminator="\r\n").writerows([header] + rows)
        return ("\ufeff" + buf.getvalue()).encode("utf-8")

    def pick(rows, name="ManaBox_Inbox.csv", header=MANABOX):
        pg.set_input_files("#intake input[type=file]", files=[{"name": name, "mimeType": "text/csv", "buffer": csv_bytes(rows, header)}])
        pg.wait_for_timeout(300)

    def intake_api(**params):
        body = json.dumps({"passcode": "pw", **params})
        return json.loads(gas.evaluate("b => doPost({ postData: { contents: b } }).s", body))

    def inv_rows(book):
        return gas.evaluate("b => __books[b].Inventory.rows.map(r => r.slice())", book)

    def log_rows(book):
        return gas.evaluate("b => __books[b]['Intake log'] ? __books[b]['Intake log'].rows.map(r => r.map(v => v instanceof Date ? 'DATE' : v)) : []", book)

    intake = pg.locator("#intake")
    pg.reload(); pg.wait_for_timeout(1500)
    check("Add cards: section and nav link shown once connected", pg.is_visible("#intake-section") and pg.is_visible("#intake-link"))
    check("Add cards: Shared's next empty slot follows the highest one (B05-03)", "Next empty slot: B05-04" in intake.inner_text(), intake.inner_text())

    batch_a = [mb('Thor, God of Thunder', 1, price="16.99", foil="foil"), mb('Avengers "Assemble!"', 2, qty=2), mb("Castle Doom", 3, price="")]
    s_before = len(inv_rows("S"))
    pick(batch_a)
    check("preview: cards, rows and what they cost", "4 cards in 3 rows" in intake.inner_text() and "bought for $19.99" in intake.inner_text(), intake.inner_text())
    check("preview: logs behind the next empty slot", intake.locator("text=Log 4 cards → B05-04").count() == 1)
    n = len(calls)
    intake.locator("text=Log 4 cards → B05-04").click(); pg.wait_for_timeout(800)
    rows = inv_rows("S")
    new = rows[s_before:]
    H = rows[0]
    c = lambda name: H.index(name)
    check("one call logs the batch", calls[n:] == ["intake"], calls[n:])
    check("rows added at the bottom, behind B05-04", len(new) == 3 and all(r[c("Location")] == "B05-04" for r in new), new)
    check("name with a comma and quotes kept whole", new[0][c("Name")] == "Thor, God of Thunder" and new[1][c("Name")] == 'Avengers "Assemble!"')
    check("columns mapped like the Staging tab",
          new[0][c("Foil")] == "foil" and new[1][c("Qty")] == 2 and new[0][c("Price (USD)")] == 16.99 and new[2][c("Price (USD)")] == ""
          and new[0][c("Scryfall ID")] == uid(1) and new[0][c("Set name")] == "Marvel Super Heroes" and new[0][c("Collector #")] == "101"
          and new[0][c("Condition")] == "near_mint" and re.fullmatch(r"\d{4}-\d{2}-\d{2}", new[0][c("Date logged")]), new[0])
    check("Sold/Pulled left blank, In box and Value never written (they're formulas)",
          all(len(r) <= c("In box") for r in new) and all((r + [""] * 20)[c("Sold")] == "" and (r + [""] * 20)[c("Pulled")] == "" for r in new), new)
    check("In box counts the new copies", cell("S", s_before + 1, "In box") == 2)
    lg = log_rows("S")
    check("Intake log tab records the batch",
          lg and lg[0][:3] == ["Batch", "Logged", "Location"] and lg[1][2] == "B05-04" and lg[1][3] == 4 and lg[1][4] == 3
          and lg[1][5] == s_before + 1 and lg[1][6] == "2026-11-21T03:04:34.722382Z", lg)
    check("what now: write the slot on the divider, move Inbox to Collection",
          "Logged 4 cards behind B05-04" in intake.inner_text() and "move Inbox to Collection" in intake.inner_text())
    check("next empty slot moves on", "Next empty slot: B05-05" in intake.inner_text() and "Last batch: 4 cards → B05-04" in intake.inner_text())

    pick(batch_a)
    check("same file again: flagged, Log disabled", "This file was already logged" in intake.inner_text()
          and not intake.locator("text=Log 4 cards").is_enabled(), intake.inner_text())
    again = intake_api(action="intake", owner="Shared", location="B05-05",
                       rows=[{"name": "Castle Doom", "scryfallId": uid(3), "qty": 1, "added": "2026-11-21T03:04:34.722382Z"}])
    check("...and the script refuses it too", not again["ok"] and "already logged behind B05-04" in again["error"], again)
    pick([mb("Castle Doom", 3), mb("New Card", 4, added="2026-11-22T09:00:00.000000Z")])
    check("Inbox not emptied in ManaBox: says which cards and what to do",
          "1 card in this file was already logged" in intake.inner_text() and "move them from Inbox to Collection" in intake.inner_text(), intake.inner_text())
    intake.locator("text=Cancel").click()

    pg.select_option("#intake .intake-owner select", "Joel"); pg.wait_for_timeout(200)
    check("Joel's sheet: its own next slot", "Next empty slot: B02-02" in intake.inner_text(), intake.inner_text())
    pick(batch_a)
    check("a file logged in the other sheet is caught too", "already logged in Shared's sheet" in intake.inner_text(), intake.inner_text())
    big = [mb("Big Stack", 5, qty=120, added="2026-11-22T10:00:00.000000Z")]
    pick(big)
    check("more than a slot holds: warned, still allowed", "more than a slot holds (100)" in intake.inner_text()
          and intake.locator("text=Log 120 cards → B02-02").is_enabled(), intake.inner_text())
    opts = pg.locator("#intake .intake-slot option").all_inner_texts()
    check("slot choices: next empty, then part-full slots with room", opts[0] == "B02-02 — next empty slot" and "B01-01 — 99 free" in opts, opts)
    pg.select_option("#intake .intake-slot", "B01-01"); pg.wait_for_timeout(200)
    check("picked slot: room left shown, button follows", "B01-01 has room for 99 more" in intake.inner_text()
          and intake.locator("text=Log 120 cards → B01-01").count() == 1, intake.inner_text())
    intake.locator("text=Cancel").click()
    pg.select_option("#intake .intake-owner select", "Shared"); pg.wait_for_timeout(200)

    pick([["Foo", "Bar"], ["1", "2"]], name="notes.csv", header=["Card", "Count"])
    check("not a ManaBox export: says so", "doesn't look like a ManaBox CSV export" in intake.inner_text(), intake.inner_text())
    bad = intake_api(action="intake", owner="Shared", location="box 5", rows=[{"name": "X", "scryfallId": uid(9), "qty": 1}])
    bad_id = intake_api(action="intake", owner="Shared", location="B05-05", rows=[{"name": "X", "scryfallId": "nope", "qty": 1}])
    check("script checks the slot and every row", "isn't a slot like B01-10" in bad["error"] and "has no Scryfall ID" in bad_id["error"], (bad, bad_id))
    check("...and writes nothing when it refuses", len(inv_rows("S")) == s_before + 3)

    # undo: only the latest batch, only while untouched
    last_row = s_before + 3
    gas.evaluate(f"() => {{ const s = __books.S.Inventory; s.rows[{last_row - 1}][s.rows[0].indexOf('Sold')] = 1; }}")
    intake.locator("text=Undo").click(); pg.wait_for_timeout(700)
    check("undo refused once a card is sold", "already sold or pulled" in intake.inner_text() and len(inv_rows("S")) == last_row, intake.inner_text())
    gas.evaluate(f"() => {{ const s = __books.S.Inventory; s.rows[{last_row - 1}][s.rows[0].indexOf('Sold')] = ''; }}")
    intake.locator("text=Undo").click(); pg.wait_for_timeout(700)
    check("undo takes the batch back out", len(inv_rows("S")) == s_before and "Next empty slot: B05-04" in intake.inner_text()
          and "Last batch" not in intake.inner_text(), intake.inner_text())
    check("...and marks it undone in the log", log_rows("S")[1][8] == "DATE")
    pick(batch_a)
    check("an undone file can be logged again", intake.locator("text=Log 4 cards → B05-04").is_enabled())
    intake.locator("text=Log 4 cards → B05-04").click(); pg.wait_for_timeout(800)
    gas.evaluate("() => { const s = __books.S.Inventory; s.rows.push(s.rows[1].slice()); }")
    st = intake_api(action="intakeStatus")["intake"]["Shared"]
    refused = intake_api(action="undoIntake", owner="Shared", batch=st["undoable"])
    check("undo refused once rows are added below the batch", not refused["ok"] and "added below" in refused["error"], refused)
    gas.evaluate("() => { __books.S.Inventory.rows.pop(); }")
    gas.evaluate("""() => { const s = __books.S['Intake log']; const h = s.rows[0];
        s.rows[s.rows.length - 1][h.indexOf('Logged')] = new Date(Date.now() - 25 * 3600 * 1000); }""")
    st = intake_api(action="intakeStatus")["intake"]["Shared"]
    old_undo = intake_api(action="undoIntake", owner="Shared", batch=st["recent"][0]["batch"])
    check("no undo after 24 hours", st["undoable"] is None and not old_undo["ok"], old_undo)

    # a bigger batch than the tab has rows for, and the last slot of a box
    gas.evaluate("() => { const s = __books.J.Inventory; s.rows.push(s.rows[2].slice()); s.rows[s.rows.length - 1][0] = 'B02-10'; s.maxRows = s.rows.length; }")
    check("after a box's last slot: the next box", intake_api(action="intakeStatus")["intake"]["Joel"]["next"] == "B03-01")
    j_before = len(inv_rows("J"))
    many = [{"name": f"Card {i}", "scryfallId": uid(100 + i), "qty": 1, "added": "2026-11-22T11:00:00Z"} for i in range(12)]
    out = intake_api(action="intake", owner="Joel", location="B03-01", rows=many)
    check("rows inserted when the tab runs out", out["ok"] and len(inv_rows("J")) == j_before + 12 and out["logged"]["firstRow"] == j_before + 1, out.get("error"))
    check("the answer carries the fresh status and orders", out["intake"]["Joel"]["next"] == "B03-02" and "orders" in out)
    gas.evaluate("() => { const s = __books.J.Inventory; s.rows.splice(" + str(j_before - 1) + "); }")
    pg.reload(); pg.wait_for_timeout(1500)
    pick([mb("Phone Card", 7, added="2026-11-22T12:00:00.000000Z")])
    pg.set_viewport_size({"width": 390, "height": 900}); pg.wait_for_timeout(300)
    check("Add cards at phone width: no horizontal scroll", not pg.evaluate("document.documentElement.scrollWidth > innerWidth"))
    pg.locator("#intake-section").screenshot(path=SHOTS / "intake_phone.png")
    pg.set_viewport_size({"width": 1280, "height": 1000}); pg.wait_for_timeout(200)
    intake.locator("text=Cancel").click()

    # ---- safety ----
    frog().locator("text=Sold to WTB buyer").click(); frog().locator("text=Record 1 at $9.00").click(); pg.wait_for_timeout(500)
    c = order_card("Psychic Frog").filter(has_text="Awaiting payment")
    c.locator("text=Buyer paid").click(); pg.wait_for_timeout(400); c.locator("text=Pull 1 & mark paid").click(); pg.wait_for_timeout(500)
    gas.evaluate("() => { const r = __books.S.Inventory.rows; [r[1], r[3]] = [r[3], r[1]]; }")
    before = gas.evaluate("() => JSON.stringify(__books.S.Inventory.rows)")
    order_card("pulled from B01-01 (row 2)").locator("text=Dropped off").click(); pg.wait_for_timeout(500)
    check("moved row detected, nothing written",
          "fix Sold/Pulled by hand" in pg.inner_text("#orders-list") and gas.evaluate("() => JSON.stringify(__books.S.Inventory.rows)") == before)
    gas.evaluate("() => { __props.PASSCODE = 'new'; }")
    order_card("pulled from B01-01 (row 2)").locator("text=Dropped off").click(); pg.wait_for_timeout(500)
    check("revoked passcode -> connect form back, sale buttons gone",
          pg.is_visible("#orders-connect") and pg.locator("text=Sold to WTB buyer").count() == 0 and pg.locator("text=Sold on TCGMP").count() == 0)
    check("no page errors", not errs, errs)
    gas.evaluate("() => { __props.PASSCODE = 'pw'; }")
    pg.fill("#orders-passcode", "pw"); pg.click("#orders-connect button"); pg.wait_for_timeout(600)
    pg.locator("#orders-section").screenshot(path=SHOTS / "orders_desktop.png")
    pg.set_viewport_size({"width": 390, "height": 900}); pg.wait_for_timeout(300)
    check("phone width: no horizontal scroll", not pg.evaluate("document.documentElement.scrollWidth > innerWidth"))
    pg.locator("#orders-section").screenshot(path=SHOTS / "orders_phone.png")

    # ---- the old script still deployed (no listing support) ----
    gas.evaluate("() => { const list = ACTIONS.list; ACTIONS.list = (r) => { const out = list(r); delete out.dismissed; delete out.version; return out; }; }")
    # ...and, like v3, changes answer without the list.
    gas.evaluate("""() => { const real = doPost; doPost = (e) => { const out = JSON.parse(real(e).s);
        if (JSON.parse(e.postData.contents).action !== "list") { delete out.orders; delete out.dismissed; delete out.version; }
        return { s: JSON.stringify(out) }; }; }""")
    old, old_errs = open_dashboard(test_html)
    ob = old.locator("#sell-plan .card", has_text="Bulk Card")
    ob.locator("input").fill("7"); ob.locator("text=Sold to WTB buyer").click()
    ob.locator("text=Record 7 at $1.00 = $7.00").click(); old.wait_for_timeout(600)
    oc = old.locator("#orders-list .card", has_text="Bulk Card ×7").filter(has_text="Awaiting payment")
    oc.locator("text=Buyer paid").click(); old.wait_for_timeout(400)
    before = gas.evaluate("() => JSON.stringify(__books.S.Inventory.rows)")
    oc.locator("text=Pull 7 & mark paid").click(); old.wait_for_timeout(400)
    check("old script: no Delete button", old.locator("#orders-list .card", has_text="Cancelled").count() > 0
          and old.locator("#orders-list .card", has_text="Cancelled").locator("text=Delete").count() == 0)
    check("old script + split: asks to update the script, writes nothing",
          "Update the Apps Script to pull from several" in oc.inner_text() and gas.evaluate("() => JSON.stringify(__books.S.Inventory.rows)") == before)
    n = len(calls); oc.locator("text=Back").click(); oc.locator("text=Buyer cancelled").click(); old.wait_for_timeout(700)
    check("old script: a change falls back to fetching the list", calls[n:] == ["cancel", "list"]
          and "Cancelled" in old.locator("#orders-list .card", has_text="Bulk Card ×7").first.inner_text(), calls[n:])
    check("old script: Add cards asks for the script update", "update the Apps Script" in old.inner_text("#intake"))
    check("old script: no Sold to GOG button", old.locator("text=Sold to GOG").count() == 0)
    check("old script: update note, no drops, no listing button, WTB still works",
          "update the Apps Script" in old.inner_text("#orders-list") and old.locator("#orders-list .card", has_text="Looks sold?").count() == 0
          and old.locator("text=Sold on TCGMP").count() == 0 and old.locator("text=Sold to WTB buyer").count() >= 1 and not old_errs, old_errs)

    off = ctx.new_page()
    off.route("**/index.html", lambda r: r.fulfill(status=200, content_type="text/html", body=off_html))
    off.route("**/watchlist.json", lambda r: r.fulfill(json=watchlist))
    off.goto(f"{BASE}/index.html"); off.wait_for_timeout(600)
    check("blank SHEETS_API_URL: section hidden, no buttons",
          not off.is_visible("#orders-section") and not off.is_visible("#orders-link") and off.locator("text=Sold to WTB buyer").count() == 0)
    print("api calls:", len(calls))
    b.close()
srv.shutdown()
print(f"{sum(results)}/{len(results)} passed (screenshots in {SHOTS})")
sys.exit(0 if all(results) else 1)
