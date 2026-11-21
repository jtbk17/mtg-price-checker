"""End-to-end check of the dashboard's Orders section: the real
docs/index.html in a real browser, its Apps Script calls answered by the
real google_apps_script.js running against fake spreadsheets
(test_orders_mocks.js) — so the sheet arithmetic (Pulled, Sold, In box),
the order flow and both files' version handling are tested together.

Run with `python test_orders_e2e.py` (needs `pip install playwright` and
`playwright install chromium`); exits non-zero if any check fails.
tests.yml runs it on every push touching either file.
"""
import functools
import http.server
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
          and sell("No Offer").locator("button").count() == 0)
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

    # ---- fewer round trips (Apps Script v4) ----
    n = len(calls); pg.reload(); pg.wait_for_timeout(1200)
    check("loading is one call: box locations come with the list", calls[n:] == ["list"], calls[n:])
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
