# MHCO uploads

Drop the files saved by the **Grab MHCO updates** bookmark button here
(`mhco-export-….json.gz`). mhco.org blocks GitHub's servers, so this is how new
MHCO articles and the associate members list reach Chat MHC.

## How to update

1. In Chat MHC, click **Update MHCO content** (bottom of the sidebar) for the
   one-time bookmark setup — or open your Chat MHC address followed by
   `/mhco-helper.html` (e.g. `https://chat-mhc.onrender.com/mhco-helper.html`).
2. Log in to mhco.org, click **Grab MHCO updates** in your bookmarks bar, and
   choose **Newest** (or **Everything** once in a while).
3. When it says **Saved!**, click **Open upload page** (or, on GitHub, open
   this folder and choose **Add file → Upload files**), drag the file in, and
   click **Commit changes**.

That's it. Uploading starts the **Update knowledge base** workflow, which reads
the file, adds new articles, refreshes changed ones and the members list,
publishes the result, and then deletes the file from this folder. Chat MHC
updates a few minutes later. The run's summary page (Actions tab) lists what
was added.

Nothing is lost if a file is partial: articles are only ever added or
refreshed one at a time, and a stored article is removed only when a
logged-in **Everything** upload shows it's gone from mhco.org. A file from a
logged-out browser only adds new articles. A file **older** than one already
imported is skipped (so it can't undo newer content) — always upload the file
you just saved.
