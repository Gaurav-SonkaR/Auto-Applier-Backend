#!/usr/bin/env python3
"""Check the outside world before a real run: portals, resume storage, email.

Nothing here applies to a job, submits a form or emails anyone else.

    python scripts/preflight.py portals                  # logged out: search + job page selectors
    python scripts/preflight.py portals --portal naukri --login --headed
                                                         # signs in with .env credentials, saves
                                                         # the session, checks the Apply button
    python scripts/preflight.py storage                  # S3 put / presigned get / delete
    python scripts/preflight.py email                    # SMTP sign-in only
    python scripts/preflight.py email --send-test        # one message to your own address

`--login` signs in to your real account, one portal at a time. Portals notice
new sign-ins and may ask for an OTP - run it headed so you can answer. The Apply
button is only looked for, never clicked.

Exit status is 1 if anything configured is broken; "not configured" is not a failure.
"""
from __future__ import annotations

import argparse
import asyncio
import re
import sys
import tempfile
import urllib.request
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from backend.app.config import get_settings  # noqa: E402

failures: list[str] = []


def report(state: str, label: str, detail: str = "") -> None:
    """state: ok | fail | skip."""
    if state == "fail":
        failures.append(label)
    mark = {"ok": "  OK  ", "fail": " FAIL ", "skip": " ---- "}[state]
    print(f"[{mark}] {label}" + (f" - {detail}" if detail else ""))


# ── Portals ─────────────────────────────────────────────────────────────────

_BLOCKED = re.compile(r"access denied|just a moment|attention required|are you a robot|"
                      r"security check|unusual traffic", re.I)


async def _count(page, selector: str, within=None) -> int:
    if not selector:
        return -1
    try:
        return len(await (within or page).query_selector_all(selector))
    except Exception:
        return 0


def _selector_report(portal: str, what: str, selector: str, count: int) -> None:
    if count < 0:
        report("skip", f"{portal}: {what}", "no selector defined")
    else:
        report("ok" if count else "fail", f"{portal}: {what}",
               f"{count} match(es) for {selector!r}")


async def check_portal(pw, portal: str, query: str, location: str, login: bool,
                       headed: bool) -> None:
    from playwright_stealth import Stealth

    from backend.app.services import applier, scraper
    from backend.app.services.portal_templates import get_template

    settings = get_settings()
    template = get_template(portal)
    sel = template.selectors
    print(f"\n== {portal} ({'signed in' if login else 'logged out'}) ==")

    browser = await pw.chromium.launch(headless=not headed)
    try:
        ctx = await scraper._make_context(browser, not headed)
        page = await ctx.new_page()
        await Stealth().apply_stealth_async(page)

        if login:
            if not template.requires_login():
                report("skip", f"{portal}: sign-in", "this template has no login form")
            else:
                auth_path = applier._auth_state_path(portal, settings)
                signed_in = await applier._ensure_logged_in(page, ctx, template, settings,
                                                            portal, auth_path)
                report("ok" if signed_in else "fail", f"{portal}: sign-in",
                       f"session saved to {auth_path}" if signed_in
                       else f"still signed out at {page.url} (credentials, OTP or selectors)")
                if not signed_in:
                    return

        url = template.build_search_url(query, location)
        response = await page.goto(url, wait_until="domcontentloaded", timeout=30_000)
        await page.wait_for_timeout(4_000)
        title = await page.title()
        status = response.status if response else "?"
        blocked = _BLOCKED.search(title) or await applier._has_captcha(page)
        report("fail" if blocked or status == "?" or int(status) >= 400 else "ok",
               f"{portal}: search page", f"HTTP {status}, title {title[:60]!r}, at {page.url[:90]}")
        if blocked:
            report("fail", f"{portal}: bot check", "the portal is challenging this browser")
            return

        cards = await page.query_selector_all(sel.job_card) if sel.job_card else []
        _selector_report(portal, "job cards", sel.job_card, len(cards) if sel.job_card else -1)
        links = [await c.query_selector(sel.job_link_in_card) for c in cards[:10]]
        links = [link for link in links if link]
        _selector_report(portal, "job link in card", sel.job_link_in_card,
                         len(links) if sel.job_link_in_card else -1)
        if cards:
            _selector_report(portal, "job title in card", sel.job_title_in_card,
                             await _count(page, sel.job_title_in_card, cards[0]))
        if not links:
            return

        href = await links[0].get_attribute("href") or ""
        job_url = scraper._resolve_url(href, template.base_url)
        await page.goto(job_url, wait_until="domcontentloaded", timeout=30_000)
        await page.wait_for_timeout(4_000)
        print(f"   job page: {page.url[:110]}")
        for what, selector in (("job description", sel.job_description_container),
                               ("job title", sel.job_title),
                               ("company name", sel.company_name)):
            _selector_report(portal, what, selector, await _count(page, selector))
        # Only looked for. Signed out, most portals show a sign-in prompt instead.
        apply_selector = sel.easy_apply_button or sel.apply_button
        count = await _count(page, apply_selector)
        if login:
            _selector_report(portal, "apply button (not clicked)", apply_selector, count)
        else:
            report("ok" if count > 0 else "skip", f"{portal}: apply button (not clicked)",
                   f"{count} match(es)" + ("" if count > 0 else " - often hidden until signed in"))
    except Exception as exc:
        report("fail", f"{portal}: check crashed", str(exc)[:200])
    finally:
        await browser.close()


async def check_portals(args) -> None:
    from playwright.async_api import async_playwright

    from backend.app.services.applier import MANUAL_APPLY_PORTAL
    from backend.app.services.portal_templates import list_portals

    portals = [args.portal] if args.portal else [p for p in list_portals()
                                                 if p != MANUAL_APPLY_PORTAL]
    async with async_playwright() as pw:
        for portal in portals:
            await check_portal(pw, portal, args.query, args.location, args.login, args.headed)


# ── Storage ─────────────────────────────────────────────────────────────────

def check_storage(_args) -> None:
    from backend.app.services.storage import StorageConfigError, get_resume_store, parse_s3_ref

    settings = get_settings()
    print("\n== resume storage ==")
    try:
        store = get_resume_store()
    except StorageConfigError as exc:
        report("fail", "storage configuration", str(exc))
        return

    resumes_dir = Path(settings.resumes_dir)
    try:
        resumes_dir.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(dir=resumes_dir):
            pass
        report("ok", "local resume folder writable", str(resumes_dir.resolve()))
    except OSError as exc:
        report("fail", "local resume folder writable", str(exc))

    if store.name == "local":
        report("skip", "S3", "not configured - set STORAGE_BACKEND=s3, S3_BUCKET, S3_REGION "
                             "and AWS_ACCESS_KEY_ID / AWS_SECRET_ACCESS_KEY in .env")
        return

    payload = f"autoapply preflight {uuid.uuid4()}".encode()
    key = f"_preflight/{uuid.uuid4().hex}.pdf"
    ref = None
    with tempfile.TemporaryDirectory() as tmp:
        probe = Path(tmp) / "probe.pdf"
        probe.write_bytes(payload)
        try:
            ref = store.put(str(probe), key)
            report("ok", "S3 upload", ref)
            with urllib.request.urlopen(store.download_url(ref, "probe.pdf"), timeout=20) as r:
                same = r.read() == payload
            report("ok" if same else "fail", "S3 presigned download",
                   "content matches" if same else "content differs")
        except Exception as exc:
            report("fail", "S3 round trip", f"{type(exc).__name__}: {str(exc)[:200]}")
        finally:
            parsed = parse_s3_ref(ref) if ref else None
            if parsed:
                try:
                    store.client.delete_object(Bucket=parsed[0], Key=parsed[1])
                    report("ok", "S3 cleanup", "probe object deleted")
                except Exception as exc:
                    report("fail", "S3 cleanup",
                           f"delete {ref} by hand - {type(exc).__name__}: {str(exc)[:150]}")


# ── Email ───────────────────────────────────────────────────────────────────

def check_email(args) -> None:
    import smtplib

    from backend.app.services.email_sender import SMTP_HOST, SMTP_PORT, get_email_sender

    settings = get_settings()
    address = settings.gmail_address or ""
    password = (settings.gmail_app_password or "").replace(" ", "")
    print("\n== email ==")
    if not address:
        report("skip", "Gmail", "GMAIL_ADDRESS is not set")
        return
    # Gmail refuses account passwords over SMTP, and repeated failed sign-ins
    # get noticed - so don't try one with something that cannot work.
    if not re.fullmatch(r"[A-Za-z]{16}", password):
        report("fail", "Gmail app password",
               f"GMAIL_APP_PASSWORD is {len(password)} characters; a Gmail app password is 16 "
               "letters (Google Account > Security > App passwords). Not signing in.")
        return
    try:
        with smtplib.SMTP_SSL(SMTP_HOST, SMTP_PORT, timeout=30) as smtp:
            smtp.login(address, password)
        report("ok", "Gmail SMTP sign-in", address)
    except (smtplib.SMTPException, OSError) as exc:
        report("fail", "Gmail SMTP sign-in", f"{type(exc).__name__}: {str(exc)[:200]}")
        return

    if args.send_test:
        sent = get_email_sender()._smtp_send(
            address, "AutoApply preflight",
            "This is the AutoApply preflight test. Sending works.", None, None,
        )
        report("ok" if sent else "fail", "test email",
               f"sent to {address} - check the inbox and the spam folder" if sent
               else "Gmail refused the message; see the log for the SMTP error")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="check", required=True)

    portals = sub.add_parser("portals", help="search and job-page selectors on each portal")
    portals.add_argument("--portal", help="one portal (default: all job portals)")
    portals.add_argument("--query", default="python developer")
    portals.add_argument("--location", default="India")
    portals.add_argument("--login", action="store_true",
                         help="sign in with the .env credentials first (needs --portal)")
    portals.add_argument("--headed", action="store_true", help="show the browser")

    sub.add_parser("storage", help="S3 upload, presigned download and delete")

    email = sub.add_parser("email", help="Gmail SMTP sign-in")
    email.add_argument("--send-test", action="store_true",
                       help="also send one message to GMAIL_ADDRESS itself")

    args = parser.parse_args()
    if args.check == "portals":
        if args.login and not args.portal:
            parser.error("--login signs in to a real account; name one --portal")
        asyncio.run(check_portals(args))
    elif args.check == "storage":
        check_storage(args)
    else:
        check_email(args)

    print(f"\n{len(failures)} problem(s)" + (": " + "; ".join(failures) if failures else ""))
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
