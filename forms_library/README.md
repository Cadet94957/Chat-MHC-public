# MHCO Forms Library

Put the official **MHCO form PDFs** in this folder. They become the Forms tab in
the app (browse, search, and download), and the assistant cites them in answers.

MHCO forms can't be auto-downloaded: the site generates each one on demand,
personalized with your park's name, and serves it as a temporary link that
expires within the hour (there's no permanent file to fetch). So forms are
managed here by hand. They rarely change — a one-time setup with occasional
top-ups. Bonus: the PDFs you download already have your park's name filled in.

## How to add forms

1. Sign in to mhco.org and download the form PDFs you want available.
2. Upload them into this folder (on GitHub: **Add file → Upload files**, then
   drag them all in at once and commit).
3. The files MHCO gives you (e.g. `Example_Mobile_Home_Park_-_01.pdf`) work
   as-is — the trailing number is read as the form number. For nicer titles,
   rename them to include the form name, e.g.:
   - `Form 01 - Rental Application.pdf`
   - `Form 43 - Notice of Nonpayment.pdf`
   …or use the optional `forms.csv` below for exact titles.

That's it. The app picks them up automatically on its next deploy, and the
"Update knowledge base" workflow bakes them into the published content.

## Updating later

Drop new or replacement PDFs in here (same filename replaces an existing form).
Delete a PDF to remove that form.

## Optional: precise titles via forms.csv

For full control, add a `forms.csv` in this folder with a header row and one
line per file:

```csv
filename,number,title,description
Form 01 - Rental Application.pdf,01,MHCO Form 01 — Rental Application,Application for prospective residents and in-place buyers.
```

Any column you leave out falls back to what's parsed from the filename.
