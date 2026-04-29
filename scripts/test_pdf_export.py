"""Trigger generarPDF() in a headless Chromium and capture the resulting PDF.

Saves to data/pdf_test_output.pdf (gitignored). Use this to verify PDF
layout fixes without manually clicking the button in a browser.

Requires:  pip install playwright; python -m playwright install chromium
"""
import os
import sys
import threading
import http.server
import socketserver
from pathlib import Path
from playwright.sync_api import sync_playwright

PORT = 8769
ROOT_DIR = Path(__file__).resolve().parent.parent  # project root
HTML_FILE = "ps_verificacion.html"
OUT_DIR = ROOT_DIR / "data"
OUT_DIR.mkdir(exist_ok=True)
OUT_FILE = OUT_DIR / "pdf_test_output.pdf"
USER_OUT_FILE = Path.home() / "Downloads" / "ps_verificacion_local_test.pdf"


def serve():
    os.chdir(ROOT_DIR)
    handler = http.server.SimpleHTTPRequestHandler
    handler.log_message = lambda *_: None
    with socketserver.TCPServer(("", PORT), handler) as httpd:
        httpd.serve_forever()


def main():
    if not (ROOT_DIR / HTML_FILE).exists():
        print(f"No {HTML_FILE} in {ROOT_DIR} -- run ps_verificacion.py first")
        sys.exit(1)

    server_thread = threading.Thread(target=serve, daemon=True)
    server_thread.start()
    print(f"HTTP server: http://localhost:{PORT}/{HTML_FILE}")

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(
            viewport={"width": 1920, "height": 1080},
            accept_downloads=True,
        )
        page = context.new_page()
        page.goto(f"http://localhost:{PORT}/{HTML_FILE}", wait_until="networkidle")

        page.wait_for_function("typeof html2pdf !== 'undefined'", timeout=20000)
        print("html2pdf.js ready")

        with page.expect_download(timeout=180000) as dl_info:
            page.click("#pdf-download")
            print("PDF button clicked, waiting for download...")

        download = dl_info.value
        if OUT_FILE.exists():
            OUT_FILE.unlink()
        download.save_as(str(OUT_FILE))

        import shutil
        try:
            USER_OUT_FILE.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(OUT_FILE, USER_OUT_FILE)
            print(f"PDF saved:")
            print(f"  - {OUT_FILE} ({OUT_FILE.stat().st_size:,} bytes)")
            print(f"  - {USER_OUT_FILE}  <-- abrir este para revisar")
        except Exception as e:
            print(f"PDF saved to: {OUT_FILE} ({OUT_FILE.stat().st_size:,} bytes)")
            print(f"(could not copy to {USER_OUT_FILE}: {e})")
        browser.close()


if __name__ == "__main__":
    main()
