"""Capture the README's screenshots from the demo page: python3 demo/screenshots.py URL OUT_DIR (needs Playwright).
Serve the folder demo/render.py wrote, e.g. python3 -m http.server 8765, and pass http://127.0.0.1:8765/index.html."""
import sys
from playwright.sync_api import sync_playwright
url, out = sys.argv[1], sys.argv[2]
with sync_playwright() as p:
    b = p.chromium.launch()
    pg = b.new_page(viewport={"width": 1600, "height": 1000}, device_scale_factor=2, color_scheme="light")
    pg.goto(url, wait_until="domcontentloaded"); pg.wait_for_timeout(1500); pg.add_style_tag(content="#toast{display:none!important}")
    pg.mouse.move(5, 995)
    pg.screenshot(path=f"{out}/desk.png")
    # a board opened under its project row
    pg.click('tbody[data-name="Research desk"] .bt'); pg.wait_for_timeout(400)
    el = pg.locator('#projects'); el.scroll_into_view_if_needed(); pg.wait_for_timeout(200)
    el.screenshot(path=f"{out}/board.png")
    pg.click('tbody[data-name="Research desk"] .bt'); pg.wait_for_timeout(200)
    # New, with an item's hover actions
    pg.locator('#new').scroll_into_view_if_needed(); pg.wait_for_timeout(200)
    pg.hover('#item-8'); pg.wait_for_timeout(300)
    pg.locator('#new').screenshot(path=f"{out}/new.png")
    # Calendar (day plan) and Plans (week plan)
    pg.click('#tab-plan'); pg.wait_for_timeout(400); pg.mouse.move(5, 995)
    pg.screenshot(path=f"{out}/calendar.png")
    pg.click('#tab-week'); pg.wait_for_timeout(400); pg.mouse.move(5, 995)
    pg.locator(".wk").screenshot(path=f"{out}/week-plan.png")
    # Quick create
    pg.click('#tab-desk'); pg.wait_for_timeout(300); pg.mouse.move(5, 995)
    pg.keyboard.press("q"); pg.wait_for_timeout(300)
    pg.keyboard.type("Draft survey questions +sam fri p1", delay=15); pg.wait_for_timeout(400)
    pg.screenshot(path=f"{out}/quick-create.png")
    b.close()
