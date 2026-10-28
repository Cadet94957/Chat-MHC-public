"""Per-source outcome of a knowledge-base refresh, printed to the console and
(on GitHub Actions) to the run's summary page."""
import os
from dataclasses import dataclass

OK_STATUSES = {"updated", "unchanged"}


@dataclass
class SourceResult:
    name: str        # e.g. "Laws: ORS 90", "Articles", "Members"
    status: str      # updated | unchanged | blocked | failed | skipped
    detail: str = ""

    @property
    def ok(self) -> bool:
        return self.status in OK_STATUSES


_ICONS = {"updated": "✅", "unchanged": "✅", "blocked": "⛔",
          "failed": "❌", "skipped": "⏭️"}


KEPT_NOTE = ("Anything not refreshed this run was **kept as it was** — nothing "
             "was deleted. The next run will try again.")
UPLOAD_NOTE = ("Uploaded files are removed once processed, whether or not they "
               "could be used. Existing content was kept. To retry, click **Grab "
               "MHCO updates** on mhco.org again and upload the new file.")


def render(results: list[SourceResult], before: dict, after: dict,
           heading: str = "Knowledge base refresh", note: str = KEPT_NOTE) -> str:
    lines = [f"## {heading}", "",
             "| Source | Result | Details |", "|---|---|---|"]
    for r in results:
        detail = r.detail.replace("|", "\\|").replace("\n", " ")
        lines.append(f"| {r.name} | {_ICONS.get(r.status, '')} {r.status} | {detail} |")
    lines += ["", "| Content | Before | After |", "|---|---|---|"]
    types = sorted(set(before.get("documents", {})) | set(after.get("documents", {})))
    for t in types:
        lines.append(f"| {t} | {before['documents'].get(t, 0)} "
                     f"| {after['documents'].get(t, 0)} |")
    lines.append(f"| forms | {before.get('forms', 0)} | {after.get('forms', 0)} |")
    if any(not r.ok for r in results):
        lines += ["", note]
    return "\n".join(lines) + "\n"


def write_github_summary(markdown: str) -> None:
    path = os.getenv("GITHUB_STEP_SUMMARY")
    if not path:
        return
    try:
        with open(path, "a", encoding="utf-8") as f:
            f.write(markdown)
    except OSError:
        pass
