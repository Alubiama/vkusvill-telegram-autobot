"""Capture the 'В корзину' AJAX endpoint from personal page cards."""
import asyncio
import json
from pathlib import Path
from playwright.async_api import async_playwright

ROOT = Path(__file__).resolve().parent.parent
STATE = ROOT / "data/vkusvill_storage_state.json"
PROXY = "socks5://127.0.0.1:1080"
TARGET_URL = "https://vkusvill.ru/personal/"  # has inshop cards with 'В корзину'


async def main():
    xhr: list[dict] = []
    requests_log: list[dict] = []

    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=True,
            proxy={"server": PROXY},
            args=["--no-sandbox"],
        )
        ctx = await browser.new_context(
            storage_state=str(STATE) if STATE.exists() else None,
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        )
        page = await ctx.new_page()

        async def on_request(req):
            # Log only POSTs and XHRs that look like cart/basket actions.
            if req.resource_type in ("image", "stylesheet", "font", "media"):
                return
            if req.method in ("POST", "PUT"):
                try:
                    post = req.post_data
                except Exception:
                    post = None
                requests_log.append({
                    "method": req.method,
                    "url": req.url,
                    "post": post,
                    "headers": dict(req.headers),
                    "resource_type": req.resource_type,
                })

        async def on_response(resp):
            url = resp.url
            if "vkusvill" not in url:
                return
            rt = resp.request.resource_type
            if rt in ("image", "stylesheet", "font", "media"):
                return
            low = url.lower()
            is_interesting = any(k in low for k in ("basket", "cart", "korzin", "add2", "add_to", "korzina"))
            method = resp.request.method
            if method != "POST" and not is_interesting:
                return
            try:
                body = await resp.text()
            except Exception:
                body = ""
            xhr.append({
                "method": method,
                "url": url,
                "status": resp.status,
                "post": resp.request.post_data,
                "req_headers": dict(resp.request.headers),
                "resp_headers": dict(resp.headers),
                "body_len": len(body),
                "body": body[:4000],
            })

        page.on("request", lambda r: asyncio.create_task(on_request(r)))
        page.on("response", lambda r: asyncio.create_task(on_response(r)))

        print(f"→ load {TARGET_URL}")
        await page.goto(TARGET_URL, wait_until="domcontentloaded", timeout=60000)
        await page.wait_for_timeout(5000)

        # Find first 'В корзину' button on any product card.
        candidates = [
            "button:has-text('В корзину')",
            "a:has-text('В корзину')",
            ".js-product-cart button:has-text('В корзину')",
            ".js-product-cart .ProductCart__btn",
            ".ProductCart__btn:has-text('В корзину')",
            ".js-add-to-cart",
            "[data-action='add']",
        ]
        pre = len(xhr)
        clicked = False
        for sel in candidates:
            loc = page.locator(sel).first
            try:
                cnt = await loc.count()
            except Exception:
                cnt = 0
            if cnt > 0:
                try:
                    await loc.scroll_into_view_if_needed(timeout=3000)
                    await loc.click(timeout=5000)
                    print(f"→ clicked: {sel}")
                    clicked = True
                    break
                except Exception as e:
                    print(f"  {sel} click failed: {e}")

        if not clicked:
            print("!! no 'В корзину' button found on /personal/")
            # Dump visible buttons for debug.
            btns_html = await page.evaluate("""() => {
                const out = [];
                document.querySelectorAll('button, a').forEach(el => {
                    const t = (el.innerText || '').trim();
                    if (t && /корзин|basket|cart/i.test(t)) {
                        out.push({tag: el.tagName, text: t.slice(0,60), cls: el.className.slice(0,120), data: el.outerHTML.slice(0,240)});
                    }
                });
                return out.slice(0,20);
            }""")
            (ROOT / "docs/add_to_cart_buttons_debug.json").write_text(
                json.dumps(btns_html, indent=2, ensure_ascii=False), encoding="utf-8"
            )

        # Wait for AJAX to complete.
        await page.wait_for_timeout(6000)

        await browser.close()

    out_xhr = ROOT / "docs/add_to_cart_probe.json"
    out_xhr.parent.mkdir(exist_ok=True)
    out_xhr.write_text(
        json.dumps({"clicked": clicked, "xhr_after": xhr[pre:], "xhr_all_post": requests_log},
                   indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    print(f"\n=== captured {len(xhr) - pre} XHR after click ===")
    for r in xhr[pre:]:
        print(f"  {r['method']} {r['status']} {r['url'][:140]}")
        print(f"    post: {(r['post'] or '')[:200]}")
        print(f"    body: {r['body'][:200]}")


if __name__ == "__main__":
    asyncio.run(main())
