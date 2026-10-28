/* Builds the "Grab MHCO updates" bookmark link from /mhco-bookmarklet.js,
   so the bookmark always matches the code in the repo. */
(function () {
  const link = document.getElementById("bookmarklet");

  // The bookmark's code: everything from the `void (` line on, minus the
  // whole-line comments (a bookmark is a single javascript: URL).
  function buildBookmarklet(src) {
    const lines = src.split("\n");
    const start = lines.findIndex((l) => l.startsWith("void ("));
    return lines.slice(start)
      .map((l) => l.trim())
      .filter((l) => l && !l.startsWith("//"))
      .join("\n");
  }

  link.addEventListener("click", (e) => {
    e.preventDefault();
    alert("Drag this button onto your bookmarks bar (don't click it here). "
      + "Then use it while you're on mhco.org.");
  });

  fetch("/mhco-bookmarklet.js", { cache: "no-store" })
    .then((r) => {
      if (!r.ok) throw new Error(`HTTP ${r.status}`);
      return r.text();
    })
    .then((src) => {
      link.href = "javascript:" + encodeURIComponent(buildBookmarklet(src));
    })
    .catch(() => {
      document.getElementById("bookmarklet-error").classList.remove("hidden");
    });
})();
