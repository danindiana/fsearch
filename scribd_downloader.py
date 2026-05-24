# scribd_downloader.py
import os
import re
import asyncio
import argparse
from pathlib import Path
from urllib.parse import urlparse, parse_qs

from playwright.async_api import async_playwright

async def download_scribd_pdf(url: str, output_path: str = None, headless: bool = True):
    """
    Downloads a Scribd document as a PDF.
    Uses async Playwright to load the document and print it to PDF.
    """
    # 1. Validate and parse the URL to get a clean document ID
    try:
        parsed_url = urlparse(url)
        if 'scribd.com' not in parsed_url.netloc:
            print("Error: The provided URL does not appear to be from Scribd.")
            return False

        # Extract document ID from the path (e.g., /document/12345/Title)
        match = re.search(r'/document/(\d+)', url)
        if not match:
            print("Error: Could not find a document ID in the URL.")
            return False
        doc_id = match.group(1)
        print(f"Document ID: {doc_id}")

        # Construct the direct embed URL, which loads the document more cleanly
        embed_url = f"https://www.scribd.com/embeds/{doc_id}/content"
        print(f"Using embed URL: {embed_url}")
    except Exception as e:
        print(f"Error parsing URL: {e}")
        return False

    # 2. Define the output file name
    if output_path is None:
        # Use the document ID and a timestamp to create a default name
        from datetime import datetime
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        output_path = Path(f"scribd_document_{doc_id}_{timestamp}.pdf")
    else:
        output_path = Path(output_path)

    # 3. Launch the browser and navigate to the document
    async with async_playwright() as p:
        print(f"Launching {'headless' if headless else 'visible'} Chromium browser...")
        browser = await p.chromium.launch(headless=headless)
        context = await browser.new_context()
        page = await context.new_page()

        # Navigate to the embed URL
        print(f"Navigating to document...")
        try:
            await page.goto(embed_url, wait_until="networkidle")
        except Exception as e:
            print(f"Error loading page: {e}")
            await browser.close()
            return False

        # Handle the cookie consent pop-up if it appears
        try:
            await page.wait_for_selector('button:has-text("Accept"), button:has-text("Accept All"), button:has-text("Agree")', timeout=5000)
            await page.click('button:has-text("Accept"), button:has-text("Accept All"), button:has-text("Agree")')
            print("Cookie consent handled.")
            await page.wait_for_timeout(1000)  # Wait for the popup to disappear
        except:
            print("No cookie consent popup detected or already handled.")

        # Handle the initial "Sign in or sign up" modal if it appears
        try:
            # Look for a common close button or overlay
            await page.wait_for_selector('[aria-label="Close"], [data-testid="close-button"], .closeButton', timeout=3000)
            # Click the first matching element found
            close_button = await page.query_selector('[aria-label="Close"], [data-testid="close-button"], .closeButton')
            if close_button:
                await close_button.click()
                print("Sign-up modal closed.")
        except:
            print("No sign-up modal detected.")

        # Wait a moment for the document's core structure to load
        await page.wait_for_selector('.outer_page', timeout=10000)
        print("Document loaded. Found the outer_page structure.")

        # 4. Scroll to lazy-load all pages.
        # A common technique is to repeatedly scroll down to the end of the document.
        print("Scrolling to load all pages...")
        previous_height = await page.evaluate('document.body.scrollHeight')
        while True:
            # Scroll to the bottom of the document
            await page.evaluate('window.scrollTo(0, document.body.scrollHeight)')
            # Wait for new content to load
            await page.wait_for_timeout(2000)
            # Check if the scroll height has changed (new content loaded)
            new_height = await page.evaluate('document.body.scrollHeight')
            if new_height == previous_height:
                # No change, assume all content is loaded
                print("Finished loading content.")
                break
            previous_height = new_height

        # Optional: Scroll back to the top and then slowly scroll to ensure all pages are rendered.
        await page.evaluate('window.scrollTo(0, 0)')
        print("Rendering pages...")
        await page.wait_for_timeout(2000)

        # 5. Remove UI elements that interfere with the PDF output.
        print("Cleaning up the UI...")
        # Common selectors for toolbars, headers, and footers
        ui_selectors = [
            '.pdf_toolbar', '.header_container', '.footer_container', '.annotations',
            '.top_bar', '.toolbar', '.page_toolbar', '.mobile_header', '.global_header'
        ]
        for selector in ui_selectors:
            try:
                elements = await page.query_selector_all(selector)
                for element in elements:
                    await element.evaluate('element => element.remove()')
            except:
                pass

        # 6. Generate and save the PDF.
        print(f"Saving PDF to {output_path}...")
        await page.pdf(path=str(output_path), print_background=True, prefer_css_page_size=True, margin={"top": "0", "right": "0", "bottom": "0", "left": "0"})
        print(f"PDF successfully saved to {output_path}")

        await browser.close()
        return True

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Download a document from Scribd as a PDF.")
    parser.add_argument("url", help="The full Scribd document URL (e.g., https://www.scribd.com/document/123456/Document-Title)")
    parser.add_argument("-o", "--output", help="The output path for the PDF file. Default is 'scribd_document_[doc_id]_[timestamp].pdf'.", default=None)
    parser.add_argument("--visible", action="store_false", dest="headless", help="Run the browser in visible mode (not headless). Useful for debugging.")
    args = parser.parse_args()

    asyncio.run(download_scribd_pdf(args.url, args.output, headless=args.headless))
