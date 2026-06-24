#!/usr/bin/env python3
"""Playwright smoke test for the static overlay viewer.

Serves viewer/ over HTTP and checks: page loads with no console errors, the
uPlot canvas renders, metric/window/legend controls work, and it behaves on a
mobile viewport. Saves screenshots to viewer-shot-*.png.
"""

import http.server
import os
import socketserver
import threading

from playwright.sync_api import sync_playwright

HERE = os.path.dirname(os.path.realpath(__file__))
ROOT = os.path.join(HERE, "viewer")


def serve():
    os.chdir(ROOT)
    handler = http.server.SimpleHTTPRequestHandler
    httpd = socketserver.TCPServer(("127.0.0.1", 0), handler)
    port = httpd.server_address[1]
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return httpd, port


def main():
    httpd, port = serve()
    url = f"http://127.0.0.1:{port}/viewer.html"
    errors = []
    fails = []

    def check(cond, msg):
        print(("PASS" if cond else "FAIL") + ": " + msg)
        if not cond:
            fails.append(msg)

    with sync_playwright() as p:
        browser = p.chromium.launch()

        # --- Desktop ---
        page = browser.new_page(viewport={"width": 1200, "height": 800})
        page.on("console", lambda m: errors.append(m.text) if m.type == "error" else None)
        page.on("pageerror", lambda e: errors.append(str(e)))
        page.goto(url, wait_until="networkidle")
        page.wait_for_selector("canvas", timeout=5000)

        canvas = page.query_selector("canvas")
        box = canvas.bounding_box()
        check(box["width"] > 600, f"canvas wide on desktop ({box['width']:.0f}px)")

        n_series = page.eval_on_selector_all(".u-legend .u-series", "els => els.length")
        check(n_series >= 9, f"legend has all series ({n_series} incl. x)")

        # window switch -> coarse resolution (1y)
        page.click("#window button:has-text('1y')")
        page.wait_for_timeout(300)
        check(page.query_selector("#window button.active").inner_text() == "1y",
              "1y window active")

        # metric switch -> humidity
        page.click("#metric button:has-text('Humidity')")
        page.wait_for_timeout(300)
        yaxis = page.eval_on_selector_all(
            ".u-axis", "els => els.map(e => e.textContent).join(' ')")
        check(page.query_selector("#metric button.active").inner_text() == "Humidity",
              "Humidity metric active")

        # legend toggle: click 'none' then 'all'
        page.click("#none")
        page.wait_for_timeout(200)
        hidden = page.eval_on_selector_all(
            ".u-legend .u-series.u-off", "els => els.length")
        check(hidden >= 9, f"'none' hides all series ({hidden} off)")
        page.click("#all")
        page.wait_for_timeout(200)
        hidden2 = page.eval_on_selector_all(
            ".u-legend .u-series.u-off", "els => els.length")
        check(hidden2 == 0, "'all' re-shows all series")

        # toggle a single series by clicking its legend entry
        page.click("#metric button:has-text('Temperature')")
        page.click("#window button:has-text('24h')")
        page.wait_for_timeout(200)
        page.click(".u-legend .u-series:nth-child(2)")  # first data series
        page.wait_for_timeout(150)
        off = page.eval_on_selector_all(".u-legend .u-series.u-off", "els => els.length")
        check(off == 1, f"single legend toggle works ({off} off)")

        page.screenshot(path=os.path.join(HERE, "viewer-shot-desktop.png"))

        # --- Mobile ---
        m = browser.new_page(viewport={"width": 390, "height": 844},
                             device_scale_factor=2, is_mobile=True)
        m.goto(url, wait_until="networkidle")
        m.wait_for_selector("canvas", timeout=5000)
        mbox = m.query_selector("canvas").bounding_box()
        check(mbox["width"] <= 390, f"canvas fits mobile width ({mbox['width']:.0f}px)")
        check(mbox["width"] > 300, "canvas uses most of mobile width")
        m.screenshot(path=os.path.join(HERE, "viewer-shot-mobile.png"), full_page=True)

        browser.close()

    httpd.shutdown()
    check(not errors, f"no console/page errors ({errors[:3]})")
    print("\n" + ("ALL PASSED" if not fails else f"{len(fails)} FAILURES"))
    raise SystemExit(1 if fails else 0)


if __name__ == "__main__":
    main()
