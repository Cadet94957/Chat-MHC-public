"""Load MHCO form PDFs that are committed to the repo's forms_library/ folder.

The MHCO forms library is members-only and sits behind Drupal's Antibot wall,
which blocks scripted login, so forms can't be scraped reliably. Instead, a
human downloads the form PDFs once and commits them; this module imports that
folder into the `forms` table and copies the PDFs to DATA_DIR/forms so the app
can serve them for download. To update forms later, drop new PDFs in and re-run.

Filenames drive the displayed title/number, e.g.:
    "Form 01 - Rental Application.pdf"  -> MHCO Form 01 — Rental Application
    "MHCO Form 43 Nonpayment.pdf"       -> MHCO Form 43 — Nonpayment
Optional forms_library/forms.csv (columns: filename,number,title,description)
overrides the parsed values for any file.
"""
import csv
import logging
import re
import shutil

from app import config

log = logging.getLogger("ingest.forms_library")

FORMS_SOURCE_URL = "https://mhco.org/forms-oregon-laws/mhco-forms"


def _parse_form_name(stem: str) -> tuple[str | None, str]:
    s = re.sub(r"\s+", " ", stem.replace("_", " ")).strip()
    m = re.search(r"form\s*#?\s*(\d+[A-Za-z]?)", s, re.I)
    if m:
        number = m.group(1).upper()
        rest = (s[:m.start()] + " " + s[m.end():]).strip(" -:–—\t")
        rest = re.sub(r"^\s*mhco\s*", "", rest, flags=re.I).strip(" -:–—\t")
        return number, f"MHCO Form {number}" + (f" — {rest}" if rest else "")
    # MHCO's on-demand downloads are named "<Park Name> - 01" / "<Park>_01".
    m2 = re.search(r"[-_ ]\s*(\d{1,3}[A-Za-z]?)\s*$", s)
    if m2:
        number = m2.group(1).upper()
        return number, f"MHCO Form {number}"
    return None, s or "MHCO Form"


def _extract_title_from_pdf(pdf_path) -> str | None:
    """Read the form's descriptive name from its PDF page-1 text layer.

    MHCO forms open with a header line "MHCO Form NN: <Title>" immediately
    followed by the license banner, e.g.
        MHCO Form 43: 30-Day Notice to Vacate for Cause (For Continuing Violations Only)
        This form is exclusively licensed to ...
    That first header line is the clean title (later repeats on the page carry
    kerning gaps like "Ag reement"), so match up to its newline. Returns just the
    name, or None when the PDF has no readable header (scanned/corrupt/non-MHCO).
    Never raises."""
    try:
        from pypdf import PdfReader

        reader = PdfReader(str(pdf_path))
        if not reader.pages:
            return None
        text = reader.pages[0].extract_text() or ""
        m = re.search(
            r"MHCO\s*Form\s*(?:No\.?)?\s*\d+[A-Za-z]?\s*[:\-–—]\s*([^\n]+)", text, re.I
        )
        if not m:
            return None
        # `[^\n]+` already stops at the banner; this guards PDFs that extract
        # without line breaks by cutting at the banner/revision marker.
        name = re.split(r"\s+This form is\b|\s*\||\s+Revised\b|\s+Rev\."
                        r"|\s+\d+ of \d+\b",
                        m.group(1), maxsplit=1, flags=re.I)[0]
        name = re.sub(r"\s+", " ", name).strip(" -:–—\t")
        return name or None
    except Exception as exc:  # noqa: BLE001 - never let a bad PDF break ingest
        log.warning("could not extract title from %s: %s", pdf_path, exc)
        return None


def _load_csv_meta(directory) -> dict:
    csv_path = directory / "forms.csv"
    if not csv_path.exists():
        return {}
    meta = {}
    try:
        with open(csv_path, newline="", encoding="utf-8-sig") as f:
            for row in csv.DictReader(f):
                fn = (row.get("filename") or "").strip()
                if fn:
                    meta[fn] = {k: (v or "").strip() for k, v in row.items()}
    except OSError as exc:
        log.warning("could not read forms.csv: %s", exc)
    return meta


def import_forms_library(conn) -> int:
    """Replace the forms table from forms_library/*.pdf. No-op (keeps existing
    forms) when the folder is absent or has no PDFs."""
    directory = config.FORMS_LIBRARY_DIR
    if not directory.exists():
        return 0
    pdfs = sorted(p for p in directory.glob("*.pdf") if p.is_file())
    if not pdfs:
        return 0

    meta = _load_csv_meta(directory)
    config.FORMS_DIR.mkdir(parents=True, exist_ok=True)
    conn.execute("DELETE FROM forms")
    for pdf in pdfs:
        shutil.copy2(pdf, config.FORMS_DIR / pdf.name)
        number, title = _parse_form_name(pdf.stem)
        # For number-only filenames (the raw park files MHCO generates), the
        # filename yields no descriptive name — pull it from the PDF's header.
        if number and title == f"MHCO Form {number}":
            name = _extract_title_from_pdf(config.FORMS_DIR / pdf.name)
            if name:
                title = f"MHCO Form {number} — {name}"
        row = meta.get(pdf.name, {})
        conn.execute(
            "INSERT INTO forms (title, form_number, description, source_url,"
            " file_path) VALUES (?, ?, ?, ?, ?)",
            (row.get("title") or title,
             row.get("number") or number,
             row.get("description") or None,
             FORMS_SOURCE_URL,
             f"forms/{pdf.name}"),
        )
    conn.commit()
    log.info("imported %d forms from %s", len(pdfs), directory)
    return len(pdfs)
