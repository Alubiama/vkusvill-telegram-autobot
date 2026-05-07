"""Capture the 'Наличие в магазинах' modal AJAX endpoint — v2."""
import asyncio, json
from pathlib import Path
from playwright.async_api import async_playwright

ROOT = Path(__file__).resolve().parent.parent
STATE = ROOT / "data/vkusvill_storage_state.json"
SLUG = "/goods/ris-s-kurinoy-brizolyu-67492.html"
PROXY = "socks5://127.0.0.1:1080"


async def main():
    reqs: list[dict] = []
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True, proxy={"server": PROXY}, args=["--no-sandbox"])
        ctx = await browser.new_context(storage_state=str(STATE) if STATE.exists() else None)
        page = await ctx.new_page()

        async def handle_response(resp):
            url = resp.url
            if "vkusvill" not in url:
                return
            rt = resp.request.resource_type
            if rt in ("image", "stylesheet", "font", "media"):
                return
            try:
                body = await resp.text()
            except Exception:
                body = ""
            reqs.append({
                "t": asyncio.get_event_loop().time(),
                "method": resp.request.method,
                "url": url,
                "post": resp.request.post_data,
                "status": resp.status,
                "body_len": len(body),
                "body": body[:3000],
            })

        page.on("response", lambda r: asyncio.create_task(handle_response(r)))

        print("→ load page…")
        await page.goto(f"https://vkusvill.ru{SLUG}", wait_until="domcontentloaded", timeout=60000)
        await page.wait_for_timeout(4000)
        pre = len(reqs)

        # Try multiple candidate buttons
        candidates = [
            "text=Посмотреть наличие",
            "text=Наличие в магазинах",
            ".js-show-shops-list",
            ".js-product-shops",
            ".js-product-availability",
            ".js-shopselect-list",
            "[data-modal*='shop']",
            "[data-modal*='nalich']",
            ".ShopListWidget__toggle",
        ]
        clicked = False
        for sel in candidates:
            loc = page.locator(sel).first
            if await loc.count() > 0:
                try:
                    await loc.scroll_into_view_if_needed(timeout=3000)
                    await loc.click(timeout=5000)
                    print(f"→ clicked: {sel}")
                    clicked = True
                    break
                except Exception as e:
                    print(f"  {sel} click failed: {e}")
        if not clicked:
            print("!! no matching button found — will dump all initial requests only")

        await page.wait_for_timeout(6000)
        # Inspect DOM for modal after click
        modal_html = await page.evaluate("""() => {
            const sels = ['.Modal.show', '.modal.show', '.ShopListWidget', '[class*=Shop][class*=List]', '.js-modal-active'];
            for (const s of sels) { const el = document.querySelector(s); if (el) return s+'\\n'+el.outerHTML.slice(0,8000); }
            return '';
        }""")
        (ROOT / "docs/stock_modal_dom.html").write_text(modal_html or "(no modal matched)", encoding="utf-8")
        print("modal dom length:", len(modal_html or ""))
        # Try to click inside the opened modal on shop-list tab / "В магазине" tab
        try:
            tab = page.locator("text=В магазине").first
            if await tab.count() > 0:
                await tab.click(timeout=3000)
                print("→ clicked 'В магазине' tab")
                await page.wait_for_timeout(6000)
        except Exception as e:
            print(f"tab click failed: {e}")

        html = await page.content()
        (ROOT / "docs/stock_page_dump.html").write_text(html, encoding="utf-8")
        await browser.close()

    out = ROOT / "docs/stock_probe_v2.json"
    out.parent.mkdir(exist_ok=True)
    # Keep only post-click items
    out.write_text(json.dumps(reqs[pre:], indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"post-click XHR captured: {len(reqs)-pre} → {out}")
    for r in reqs[pre:]:
        print(f"  {r['method']} {r['status']} {r['url'][:120]}  body_len={r['body_len']}")


if __name__ == "__main__":
    asyncio.run(main())
