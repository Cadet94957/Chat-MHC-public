"""Knowledge-base ingestion CLI.

Usage:
  python -m ingest seed                      # load placeholder seed content
  python -m ingest restore-snapshot          # start from the published kb/ content
  python -m ingest scrape [--only X,Y]       # refresh laws + mhco.org content
  python -m ingest scrape --full             # re-read every article (slow)
  python -m ingest import-json PATH          # import old ChatMHC JSON files
  python -m ingest import-forms              # load forms_library/*.pdf
  python -m ingest import-mhco               # load bookmark-button uploads
  python -m ingest export-snapshot           # bake content into kb/ for deploy
  python -m ingest stats                     # show knowledge-base counts

`scrape` exits with status 1 if the laws could not be refreshed. mhco.org
problems (it blocks GitHub's servers) are only reported in the summary: MHCO
content arrives through the bookmark button (`import-mhco`). Whatever did
refresh is saved, and nothing that failed is deleted.
"""
import argparse
import json
import logging
import sys

from app import db

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")


def main() -> int:
    parser = argparse.ArgumentParser(prog="python -m ingest")
    sub = parser.add_subparsers(dest="cmd", required=True)

    sub.add_parser("seed")
    sub.add_parser("stats")
    restore = sub.add_parser("restore-snapshot")
    restore.add_argument("--force", action="store_true",
                         help="overwrite a data dir that already has content or "
                              "chat history")
    sub.add_parser("import-forms")

    export = sub.add_parser("export-snapshot")
    export.add_argument("--allow-shrink", action="store_true",
                        help="publish even if content shrank a lot (normally "
                             "refused as a sign of a failed scrape)")

    scrape = sub.add_parser("scrape")
    scrape.add_argument("--only", default="articles,laws,members",
                        help="comma list: articles,laws,members,forms "
                             "(laws = official ORS text; forms = legacy MHCO "
                             "forms scrape, normally managed in forms_library/)")
    scrape.add_argument("--full", action="store_true",
                        help="re-read every article instead of only new ones "
                             "(hundreds of requests; may trip mhco.org's rate limit)")
    scrape.add_argument("--limit", type=int, default=None,
                        help="max articles to fetch (for testing)")
    scrape.add_argument("--no-pdfs", action="store_true",
                        help="index forms without downloading PDFs")

    mhco = sub.add_parser("import-mhco")
    mhco.add_argument("--dir", default=None,
                      help="folder of bookmark exports (default: mhco_imports/)")
    mhco.add_argument("--remove-imported", action="store_true",
                      help="delete each export file once processed")

    imp = sub.add_parser("import-json")
    imp.add_argument("path", help="JSON file or directory of JSON files")

    args = parser.parse_args()
    db.init_db()

    if args.cmd == "seed":
        from ingest.seed import load_seed
        with db.connect() as conn:
            load_seed(conn)
        print("Seed content loaded.")

    elif args.cmd == "stats":
        with db.connect() as conn:
            print(json.dumps(db.content_stats(conn), indent=2))

    elif args.cmd == "import-forms":
        from ingest.forms_library import import_forms_library
        with db.connect() as conn:
            n = import_forms_library(conn)
        print(f"Imported {n} forms from forms_library/"
              if n else "No PDFs found in forms_library/ (nothing changed).")

    elif args.cmd == "restore-snapshot":
        from ingest.snapshot import restore_snapshot
        with db.connect() as conn:
            chats = conn.execute("SELECT COUNT(*) FROM chats").fetchone()[0]
            chunks = db.content_stats(conn)["chunks"]
        if (chats or chunks) and not args.force:
            print(f"ERROR: the data dir already has content ({chunks} passages, "
                  f"{chats} chats). Restoring would replace it, chat history "
                  "included. Re-run with --force to do that anyway.",
                  file=sys.stderr)
            return 1
        if restore_snapshot():
            db.init_db()
            with db.connect() as conn:
                print("Restored published content from kb/: "
                      f"{json.dumps(db.content_stats(conn))}")
        else:
            print("No published snapshot in kb/ — starting empty.")

    elif args.cmd == "export-snapshot":
        from ingest.snapshot import SnapshotShrankError, export_snapshot
        try:
            stats = export_snapshot(allow_shrink=args.allow_shrink)
        except SnapshotShrankError as exc:
            print(f"ERROR: {exc}", file=sys.stderr)
            return 1
        print(f"Snapshot written to kb/: {stats}")

    elif args.cmd == "import-mhco":
        from pathlib import Path
        from app import config
        from ingest.mhco_import import import_directory
        from ingest.report import render, write_github_summary
        directory = Path(args.dir) if args.dir else config.MHCO_IMPORTS_DIR
        if not directory.exists():
            print(f"No {directory} folder — nothing to import.")
            return 0
        with db.connect() as conn:
            before = db.content_stats(conn)
            results = import_directory(conn, directory,
                                       remove=args.remove_imported)
            after = db.content_stats(conn)
        if not results:
            print("No bookmark uploads to import.")
            return 0
        from ingest.report import UPLOAD_NOTE
        summary = render(results, before, after,
                         heading="Uploaded MHCO content (bookmark button)",
                         note=UPLOAD_NOTE)
        print(summary)
        write_github_summary(summary)
        return 0 if all(r.ok for r in results) else 1

    elif args.cmd == "import-json":
        from pathlib import Path
        from ingest.importer import import_path
        with db.connect() as conn:
            counts = import_path(conn, Path(args.path))
        print(f"Imported: {counts or 'nothing recognized'}")

    elif args.cmd == "scrape":
        return run_scrape(args)
    return 0


def run_scrape(args) -> int:
    from ingest.report import SourceResult, render, write_github_summary

    targets = {t.strip() for t in args.only.split(",") if t.strip()}
    unknown = targets - {"articles", "laws", "members", "forms"}
    if unknown:
        print(f"ERROR: unknown --only value(s): {', '.join(sorted(unknown))}",
              file=sys.stderr)
        return 2
    results: list[SourceResult] = []
    with db.connect() as conn:
        before = db.content_stats(conn)

        # Official statutes first: they don't depend on mhco.org at all.
        if "laws" in targets:
            from ingest.ors import update_laws
            try:
                results.extend(update_laws(conn))
            except Exception as exc:  # noqa: BLE001 - report, keep going
                conn.rollback()
                logging.exception("laws update crashed")
                results.append(SourceResult("Laws", "failed",
                                            f"{type(exc).__name__}: {exc}"))

        mhco = [t for t in ("articles", "members", "forms") if t in targets]
        if mhco:
            try:
                results.extend(_scrape_mhco(conn, mhco, args))
            except Exception as exc:  # noqa: BLE001 - still print the summary
                conn.rollback()
                logging.exception("mhco.org scrape crashed")
                results.append(SourceResult("mhco.org", "failed",
                                            f"{type(exc).__name__}: {exc}"))
        after = db.content_stats(conn)

    summary = render(results, before, after)
    print(summary)
    write_github_summary(summary)
    # Only the laws decide the exit status: mhco.org blocks GitHub's servers
    # and its content comes in through the bookmark button, so MHCO results
    # are reported in the summary without failing the run.
    return 0 if all(r.ok for r in results if r.name.startswith("Laws")) else 1


def _scrape_mhco(conn, targets, args):
    import requests
    from ingest.polite import SiteBlocked
    from ingest.report import SourceResult
    from ingest.scraper import MHCOScraper

    names = {"articles": "Articles", "members": "Members", "forms": "Forms"}
    scraper = MHCOScraper()
    try:
        if not scraper.login():
            print("WARNING: not logged in to mhco.org — members-only content "
                  "may be missing.", file=sys.stderr)
    except SiteBlocked as exc:
        return [SourceResult(names[t], "blocked",
                             f"mhco.org refused the login page ({exc}); kept "
                             "existing content") for t in targets]
    except requests.RequestException as exc:
        # e.g. a 403/404 on the login page: articles and members are public,
        # so carry on without logging in.
        print(f"WARNING: mhco.org login page failed ({exc}); continuing "
              "without login.", file=sys.stderr)

    jobs = {
        "articles": lambda: scraper.scrape_articles(conn, limit=args.limit,
                                                    full=args.full),
        "members": lambda: scraper.scrape_members(conn),
        "forms": lambda: scraper.scrape_forms(conn, download_pdfs=not args.no_pdfs),
    }
    results = []
    for target in targets:
        if scraper.blocked:
            results.append(SourceResult(
                names[target], "skipped",
                "mhco.org refused earlier requests this run; kept existing content"))
            continue
        try:
            results.append(jobs[target]())
        except SiteBlocked as exc:
            conn.commit()  # keep anything already saved
            results.append(SourceResult(names[target], "blocked",
                                        f"{exc}; kept existing content"))
        except Exception as exc:  # noqa: BLE001 - one source must not sink the rest
            conn.rollback()
            logging.exception("%s scrape crashed", target)
            results.append(SourceResult(names[target], "failed",
                                        f"{type(exc).__name__}: {exc}"))
    return results


if __name__ == "__main__":
    sys.exit(main())
