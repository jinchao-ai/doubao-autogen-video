"""login_once.py — 首次/重新登录：有头窗口打开豆包，轮询探测登录态，成功后落盘 profile。

探测条件（布局无关）：存在 tiptap 编辑器 且 页面无独立「登录」按钮/登录弹层。
"""
import asyncio
import time

from playwright.async_api import async_playwright

ACCOUNT = {"name": "main", "user_data_dir": "./profiles/main"}
TIMEOUT_MIN = 15


async def main():
    async with async_playwright() as p:
        ctx = await p.chromium.launch_persistent_context(
            ACCOUNT["user_data_dir"], headless=False,
            args=["--disable-blink-features=AutomationControlled",
                  "--no-first-run", "--no-default-browser-check"],
            ignore_default_args=["--enable-automation"],
            viewport={"width": 1200, "height": 1000})
        page = ctx.pages[0] if ctx.pages else await ctx.new_page()
        await page.goto("https://www.doubao.com/chat/")
        print("请在弹出的浏览器窗口中登录豆包（扫码/手机号均可），登录成功后自动保存。")
        deadline = time.time() + TIMEOUT_MIN * 60
        while time.time() < deadline:
            await page.wait_for_timeout(3000)
            try:
                ok = await page.evaluate("""() => {
                    if (!document.querySelector('[contenteditable=true]')) return false;
                    // 无独立「登录」按钮（登出态欢迎页有）
                    const els = document.querySelectorAll('button, a, span, div');
                    for (const el of els) {
                        if (el.children.length === 0 &&
                            (el.textContent || '').trim() === '登录') return false;
                    }
                    const t = document.body.innerText;
                    return !t.includes('扫码登录') && !t.includes('手机号登录');
                }""")
                if ok:
                    print("检测到登录成功，保存登录态…")
                    await ctx.close()
                    print("登录态已落盘 profiles/main。")
                    return
            except Exception as e:
                print(f"探测中: {e}")
        print("超时未检测到登录，profile 数据保留，重新运行本脚本再试。")
        await ctx.close()

asyncio.run(main())
