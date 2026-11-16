import csv
import re
import sys
from datetime import date
from hashlib import sha256
from pathlib import Path
from tempfile import TemporaryDirectory
from urllib.parse import quote, urlencode, urlsplit
from keywords import KEYWORDS, LOCAL_KEYWORDS, START_DATE, END_DATE

# KEYWORDS = ["job"]

# GEO = ["BD", "ID"]

# LOCAL_KEYWORDS = {"BD": ["চাকরি"], "pekerjaan"}

OUTPUT_ROOT = Path(__file__).resolve().parent / "output"
HEADLESS = False
USE_CLASSIC = True


def output_path(keyword, geo, start, end, keyword_source):
    slug = re.sub(r"[^\w-]+", "_", keyword).strip("_")[:40] or "keyword"
    return OUTPUT_ROOT / f"{slug}_{geo}_{start}_{end}_{keyword_source}.csv"


def build_url(keywords, start_date, end_date, geo, classic=False):
    params = {"hl": "en-US", "q": ",".join(keywords), "date": f"{start_date} {end_date}"}
    if geo != "WORLD":
        params["geo"] = geo
    path = "/trends/explore" if classic else "/explore"
    return f"https://trends.google.com{path}?" + urlencode(params, quote_via=quote)


def open_explore(page, url):
    for attempt in (1, 2):
        response = page.goto(url, wait_until="domcontentloaded", timeout=60_000)
        if urlsplit(page.url).path.startswith("/sorry/"):
            raise RuntimeError("Google redirected to a verification page. Open Trends in your browser to complete verification.")
        if response is None:
            raise RuntimeError("Google Trends did not return a page.")
        print(f"Explore attempt {attempt}: HTTP {response.status}", flush=True)
        if response.ok:
            return
        if response.status != 429:
            raise RuntimeError(f"Google Trends returned HTTP {response.status}.")
        retry_after = response.headers.get("retry-after", "").strip()
        if retry_after and retry_after != "0":
            raise RuntimeError(f"Google Trends returned HTTP 429 (Retry-After: {retry_after}). Wait before trying again.")
        if attempt == 1:
            print("Retrying once in the same tab.", flush=True)
    raise RuntimeError("Google Trends still returned HTTP 429 on the second attempt. Try again later.")


def download_csv(url, destination, headless):
    try:
        from playwright.sync_api import Error, TimeoutError, sync_playwright
    except ModuleNotFoundError as exc:
        raise RuntimeError("Install Playwright with: python -m pip install playwright") from exc

    try:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(channel="chrome", headless=headless)
            page = browser.new_page(locale="en-US", accept_downloads=True)
            timeline_status = None
            stage = "opening Explore"

            def track_timeline(response):
                nonlocal timeline_status
                address = urlsplit(response.url)
                if address.hostname == "trends.google.com" and address.path.endswith("/widgetdata/multiline"):
                    timeline_status = response.status
                    print(f"Timeline data HTTP status: {timeline_status}", flush=True)

            page.on("response", track_timeline)
            try:
                open_explore(page, url)

                cookie_button = page.get_by_role(
                    "button", name=re.compile(r"^(OK, )?got it$", re.IGNORECASE)
                )
                if cookie_button.is_visible():
                    cookie_button.click()

                if page.get_by_text("Sign in to try the new Explore", exact=True).is_visible():
                    raise RuntimeError("New Explore requires sign-in. Set USE_CLASSIC = True.")

                heading = page.get_by_text(re.compile(r"^Interest over\s+time\b", re.IGNORECASE))
                # Use the nearest chart container so other export buttons are excluded.
                chart = heading.locator(
                    "xpath=ancestor::*[.//button[.//*[normalize-space(.)='file_download'] "
                    "or normalize-space(.)='file_download' or @aria-label='Download' "
                    "or @aria-label='Download CSV' or @title='Download']][1]"
                )
                button = chart.get_by_role(
                    "button", name=re.compile(r"^(file_download|Download( CSV)?)$", re.IGNORECASE)
                ).or_(chart.locator('button[title="Download"]'))
                if urlsplit(page.url).path == "/trends/explore":
                    button = page.locator(
                        '[widget-name="fe_line_chart"] '
                        ':is(button.export, button[aria-label*="CSV" i], button[title*="CSV" i])'
                    ).or_(button)
                stage = "finding the timeline export button"
                button.wait_for(timeout=60_000)
                stage = "waiting for the CSV download"
                with page.expect_download(timeout=60_000) as event:
                    button.click()
                event.value.save_as(destination)
            except TimeoutError as exc:
                status = timeline_status if timeline_status is not None else "no response observed"
                raise RuntimeError(
                    f"Timed out while {stage}. Classic timeline endpoint: {status}. "
                    f"Page: {urlsplit(page.url).path}. {exc}"
                ) from exc
            finally:
                browser.close()
    except Error as exc:
        raise RuntimeError(f"Could not export the Interest over time chart. {exc}") from exc


def write_time_series(source, destination, keywords):
    with source.open(encoding="utf-8-sig", newline="") as file:
        rows = csv.reader(file)
        for header in rows:
            if header and header[0].strip() in {"Time", "Month", "Week", "Day", "Date", "Hour", "Minute"}:
                break
        else:
            raise ValueError("Missing Google Trends timeline header.")

        expected_columns = len(keywords) + 1
        if len(header) != expected_columns:
            raise ValueError("The timeline does not contain the requested number of series.")

        with destination.open("w", encoding="utf-8", newline="") as output:
            writer = csv.writer(output, quoting=csv.QUOTE_NONNUMERIC)
            writer.writerow(["Time", *keywords])
            count = 0
            for row in rows:
                if not row:
                    continue
                if len(row) != expected_columns or not row[0]:
                    raise ValueError(f"Invalid timeline row at CSV line {rows.line_num}.")
                timestamp, *scores = row
                if re.fullmatch(r"\d{4}-\d{2}", timestamp):
                    timestamp += "-01"
                writer.writerow([timestamp, *(int(score) if score.isdecimal() else score for score in scores)])
                count += 1
        if not count:
            # writer.writerow(["Time", *keywords])
            # raise ValueError("Google Trends returned an empty timeline.")
            pass
        return count

def write_row_count_report():
    OUTPUT_ROOT.mkdir(parents=True, exist_ok=True)
    report_path = OUTPUT_ROOT / "row_counts.csv"

    with TemporaryDirectory(dir=OUTPUT_ROOT) as folder:
        temporary_report = Path(folder) / report_path.name

        with temporary_report.open("w", encoding="utf-8", newline="") as output:
            writer = csv.writer(output)
            writer.writerow(["file_name", "row_count"])

            for path in sorted(OUTPUT_ROOT.glob("*.csv")):
                if not path.is_file() or not path.name.endswith(("_EN.csv", "_LOCAL.csv")):
                    continue

                with path.open(encoding="utf-8-sig", newline="") as source:
                    rows = csv.reader(source)
                    header = next(rows, [])
                    if not header or header[0].strip() != "Time":
                        print(f"Missing Time header in {path.name}", file=sys.stderr)
                        writer.writerow([path.stem, ""])
                        continue

                    count = sum(1 for row in rows if row and row[0].strip())
                    writer.writerow([path.stem, count])

        temporary_report.replace(report_path)

    print(f"Saved row count report to {report_path}", flush=True)

def main():
    try:
        start = date.fromisoformat(START_DATE)
        end = date.fromisoformat(END_DATE)
        if not date(2004, 1, 1) <= start <= end:
            raise ValueError("Dates must satisfy 2004-01-01 <= START_DATE <= END_DATE.")

        keywords = [term.strip() for term in KEYWORDS]
        geos = [geo.strip().upper() for geo in GEOS]
        if not keywords or any(not term or "," in term for term in keywords):
            raise ValueError("KEYWORDS must contain nonempty terms without commas.")
        if not geos or any(
            not re.fullmatch(r"WORLD|[A-Z]{2}(?:-[A-Z0-9]{1,3})?", geo)
            for geo in geos
        ):
            raise ValueError("GEOS must contain region codes such as MY, MY-10, or WORLD.")

        # English keywords
        for keyword in keywords:
            for geo in geos:
                destination = output_path(keyword, geo, start, end, "EN")
                if destination.is_file() and destination.stat().st_size > 0:
                    print(f"Skipping existing CSV: {destination}", flush=True)
                    continue

                url = build_url([keyword], start, end, geo, classic=USE_CLASSIC)
                print(f"Opening {url}", flush=True)
                destination.parent.mkdir(parents=True, exist_ok=True)
                with TemporaryDirectory(dir=destination.parent) as folder:
                    raw_csv = Path(folder) / "timeline.csv"
                    formatted_csv = Path(folder) / "formatted.csv"
                    download_csv(url, raw_csv, HEADLESS)
                    count = write_time_series(raw_csv, formatted_csv, [keyword])
                    formatted_csv.replace(destination)
                print(f"Saved {count} rows to {destination}", flush=True)

        # Local keywords
        for idx, keyword in enumerate(keywords):
            for geo in geos:
                key = LOCAL_KEYWORDS[geo][idx]
                print(key)
                destination = output_path(keyword, geo, start, end, "LOCAL")
                if destination.is_file() and destination.stat().st_size > 0:
                    print(f"Skipping existing CSV: {destination}", flush=True)
                    continue

                url = build_url([key], start, end, geo, classic=USE_CLASSIC)
                print(f"Opening {url}", flush=True)
                destination.parent.mkdir(parents=True, exist_ok=True)
                with TemporaryDirectory(dir=destination.parent) as folder:
                    raw_csv = Path(folder) / "timeline.csv"
                    formatted_csv = Path(folder) / "formatted.csv"
                    download_csv(url, raw_csv, HEADLESS)
                    count = write_time_series(raw_csv, formatted_csv, [key])
                    formatted_csv.replace(destination)
                print(f"Saved {count} rows to {destination}", flush=True)

        write_row_count_report()
    
    except (RuntimeError, OSError, ValueError, csv.Error) as exc:
        print(f"Batch stopped: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())