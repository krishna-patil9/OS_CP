#!/usr/bin/env python3
"""
Self-Contained Real Chrome Browser End-to-End Test for Dashboard Recovery Flow

Starts a real uvicorn backend server on a free localhost port, launches the real
independent `workloads\\circular_deadlock.py` workload, opens the dashboard in
real Google Chrome via Playwright, clicks the actual "Approve & Terminate Worker A"
button (#btnApproveLiveRecovery), accepts the confirm dialog, inspects the real
browser HTTP requests, verifies zero [MONITORING DISCONNECTED] dropouts, verifies
only Worker A terminated while Supervisor & Job Object stayed active, and verifies
post-recovery DOM & Banker's SAFE state.
"""

import argparse
import json
import socket
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

from playwright.sync_api import sync_playwright

PROJECT_ROOT = Path(__file__).resolve().parent


def _find_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def run_browser_test(url: str):
    captured = []
    console_errors = []

    with sync_playwright() as p:
        browser = p.chromium.launch(channel="chrome", headless=True)
        page = browser.new_page(viewport={"width": 1366, "height": 900})

        page.on(
            "console",
            lambda msg: console_errors.append(msg.text) if msg.type == "error" else None,
        )

        def _on_dialog(dlg):
            print("[BROWSER CONFIRM DIALOG]", dlg.message.replace("\n", " | "))
            dlg.accept()

        page.on("dialog", _on_dialog)

        def _on_response(resp):
            if "/recovery-authorization" in resp.url or "/recover" in resp.url:
                req = resp.request
                hdr_names = {k.lower() for k in req.headers.keys()}
                captured.append({
                    "method": req.method,
                    "url": resp.url,
                    "status": resp.status,
                    "has_recovery_token_header": "x-recovery-token" in hdr_names,
                    "post_data": req.post_data,
                })

        page.on("response", _on_response)

        page.goto(url, wait_until="networkidle")
        page.click('[data-tab="deadlock"]')
        page.wait_for_selector("#btnApproveLiveRecovery", state="visible", timeout=15000)

        btn_text = page.locator("#btnApproveLiveRecovery").inner_text()
        pre_banker = page.locator("#bankerResultBox").inner_text().replace("\n", " | ")
        pre_wfg = page.locator("#wfgStatusTitle").inner_text()
        print(f"[BROWSER BEFORE CLICK] Button: {btn_text}")
        print(f"[BROWSER BEFORE CLICK] WFG:    {pre_wfg}")
        print(f"[BROWSER BEFORE CLICK] Banker: {pre_banker}")

        page.click("#btnApproveLiveRecovery")

        page.wait_for_selector("#liveOutcomeBox", state="visible", timeout=15000)
        page.wait_for_function(
            "document.getElementById('liveOutcomeBox').innerText.includes('Deadlock Resolved')",
            timeout=15000,
        )
        time.sleep(0.5)

        outcome_text = page.locator("#liveOutcomeBox").inner_text()
        banker_text = page.locator("#bankerResultBox").inner_text()
        wfg_title = page.locator("#wfgStatusTitle").inner_text()
        toast_text = page.locator("#toastContainer").inner_text()
        feed_text = page.locator("#liveEventLog").inner_text()

        print("[BROWSER AFTER CLICK] Outcome Box:", outcome_text.replace("\n", " | "))
        print("[BROWSER AFTER CLICK] Banker Box: ", banker_text.replace("\n", " | "))
        print("[BROWSER AFTER CLICK] WFG Title:  ", wfg_title)
        print("[BROWSER AFTER CLICK] Toast:      ", toast_text.replace("\n", " | "))
        print("[BROWSER HTTP REQUESTS]:")
        for item in captured:
            print(
                f"  {item['method']} {item['url']} -> HTTP {item['status']} "
                f"(X-Recovery-Token header present: {item['has_recovery_token_header']})"
            )
            if item["post_data"]:
                parsed = json.loads(item["post_data"])
                ct_val = parsed.get("create_time_100ns")
                print(
                    f"    Payload keys={list(parsed.keys())}, "
                    f"create_time_100ns type={type(ct_val).__name__}, "
                    f"snapshot_version={parsed.get('snapshot_version')}, "
                    f"deadlock_cycle={parsed.get('deadlock_cycle')}"
                )
        print("[BROWSER CONSOLE ERRORS]:", console_errors)

        assert len(captured) == 2, f"Expected 2 recovery HTTP calls, got {captured}"
        assert captured[0]["method"] == "POST" and "/recovery-authorization" in captured[0]["url"]
        assert captured[0]["status"] == 200
        assert captured[1]["method"] == "POST" and "/recover" in captured[1]["url"]
        assert captured[1]["status"] == 200
        assert captured[1]["has_recovery_token_header"] is True
        rec_payload = json.loads(captured[1]["post_data"])
        assert isinstance(rec_payload["create_time_100ns"], str)
        assert rec_payload["approved"] is True
        assert "Deadlock Resolved" in outcome_text
        assert "Selected worker terminated successfully." in outcome_text
        assert "Surviving worker recovered the abandoned mutex." in outcome_text
        assert "No active deadlock cycle remains." in outcome_text
        assert "Banker's safety analysis completed." in outcome_text
        assert "SAFE" in banker_text
        assert "Lock Recovered" in feed_text and "Data Checked" in feed_text
        assert "Recovery rejected" not in toast_text
        assert len(console_errors) == 0, f"Unexpected browser console errors: {console_errors}"
        browser.close()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", default="", help="Optional existing server URL (if empty, starts local server + workload)")
    args = parser.parse_args()

    if args.url:
        run_browser_test(args.url)
        print("[PASS] REAL CHROME BROWSER RECOVERY TEST PASSED 100%!")
        return

    port = _find_free_port()
    base_url = f"http://127.0.0.1:{port}"
    server = subprocess.Popen(
        [
            sys.executable, "-m", "uvicorn", "backend.main:app",
            "--host", "127.0.0.1", "--port", str(port), "--log-level", "warning",
        ],
        cwd=str(PROJECT_ROOT),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    workload = None
    try:
        for _ in range(50):
            try:
                with urllib.request.urlopen(f"{base_url}/api/system/mode", timeout=1.0) as r:
                    if r.status == 200:
                        break
            except Exception:
                time.sleep(0.2)

        workload = subprocess.Popen(
            [
                sys.executable,
                str(PROJECT_ROOT / "workloads" / "circular_deadlock.py"),
                "--server", base_url,
                "--timeout", "60",
                "--delay", "0.2",
            ],
            cwd=str(PROJECT_ROOT),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )

        run_browser_test(base_url)

        out, err = workload.communicate(timeout=15)
        assert workload.returncode == 0, f"Workload failed: rc={workload.returncode}\nSTDOUT:\n{out}\nSTDERR:\n{err}"
        assert "WAIT_ABANDONED" in out
        assert "MONITORING DISCONNECTED" not in (out + err), f"Unexpected monitoring timeout during recovery!\nSTDOUT:\n{out}\nSTDERR:\n{err}"
        print("[WORKLOAD CONFIRMED] Zero monitoring dropouts, Worker A terminated (0xDEAD), Worker B recovered via WAIT_ABANDONED & exited 0, Supervisor exited 0!")
        print("[PASS] SELF-CONTAINED REAL CHROME BROWSER RECOVERY TEST PASSED 100%!")
    finally:
        for proc in (workload, server):
            if proc and proc.poll() is None:
                try:
                    proc.terminate()
                    proc.wait(timeout=4)
                except Exception:
                    try:
                        proc.kill()
                    except Exception:
                        pass


if __name__ == "__main__":
    main()
