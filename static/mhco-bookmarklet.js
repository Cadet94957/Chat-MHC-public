/* "Grab MHCO updates" bookmark button for Chat MHC.
 *
 * mhco.org blocks GitHub's servers, so the scheduled job can't read it. This runs
 * in the member's own browser instead, on mhco.org, with their logged-in
 * session: it reads the newest (or all) community-update articles and the
 * associate members page at a gentle pace, and saves them as one file to
 * upload to the repo's mhco_imports/ folder. The page text is parsed later by
 * the same Python code the scheduled job uses (ingest/mhco_import.py).
 *
 * static/mhco-helper.js turns this file into the bookmark link. Keep comments
 * on their own lines (whole-line comments are stripped when building it).
 */
void (async () => {
  const LISTING = "/news-and-resources/community-updates";
  const MEMBERS = "/membership/associate-members";
  const UPLOAD_URL = "https://github.com/Cadet94957/Chat-MHC/upload/main/mhco_imports";
  const PACE_MS = 2000;
  // "Everything" is ~260 requests from the member's own connection: go slower
  // so mhco.org's rate limiter never starts slowing down their browsing.
  const PACE_ALL_MS = 3500;
  const QUICK_PAGES = 3;
  const MAX_PAGES = 80;
  const SELECTORS = "div.views-field-title h2.field-content a, .article-title a,"
    + " .views-row h2 a, .views-row .field-content a";
  const KEEP_ATTRS = new Set(["class", "id", "href", "datetime", "content",
    "property", "name", "rel", "typeof"]);

  if (!/(^|\.)mhco\.org$/.test(location.hostname)) {
    alert("Open mhco.org and log in first, then click this bookmark again.");
    return;
  }
  if (window.__chatmhcRunning) return;
  window.__chatmhcRunning = true;

  const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
  const box = document.createElement("div");
  box.style.cssText = "position:fixed;z-index:2147483647;top:16px;right:16px;"
    + "width:min(380px,calc(100vw - 32px));background:#fff;color:#1f2937;"
    + "border:1px solid #cbd5e1;border-radius:10px;padding:16px;"
    + "box-shadow:0 10px 30px rgba(0,0,0,.25);font:15px/1.45 system-ui,sans-serif";
  document.body.appendChild(box);

  function show(title, text, buttons) {
    box.textContent = "";
    const h = document.createElement("div");
    h.style.cssText = "font-weight:700;margin-bottom:6px";
    h.textContent = title;
    box.appendChild(h);
    const p = document.createElement("div");
    p.style.cssText = "white-space:pre-line";
    p.textContent = text;
    box.appendChild(p);
    if (!buttons || !buttons.length) return Promise.resolve(null);
    const row = document.createElement("div");
    row.style.cssText = "display:flex;flex-wrap:wrap;gap:8px;margin-top:12px";
    box.appendChild(row);
    return new Promise((resolve) => {
      for (const b of buttons) {
        const el = document.createElement(b.href ? "a" : "button");
        el.textContent = b.label;
        el.style.cssText = "padding:7px 12px;border-radius:7px;cursor:pointer;"
          + "font:inherit;text-decoration:none;border:1px solid #1d4ed8;"
          + (b.primary ? "background:#1d4ed8;color:#fff" : "background:#fff;color:#1d4ed8");
        if (b.href) {
          el.href = b.href;
          el.target = "_blank";
          el.rel = "noopener";
        }
        el.addEventListener("click", () => resolve(b.value));
        row.appendChild(el);
      }
    });
  }

  function finish() {
    window.__chatmhcRunning = false;
    box.remove();
  }

  const loggedIn = !!document.querySelector('a[href*="/user/logout"]');
  const mode = await show(
    "Chat MHC: grab MHCO updates",
    (loggedIn ? "" : "Heads up: you don't seem to be logged in, so "
      + "members-only articles may be missing.\n\n")
      + "Newest: the latest ~30 articles plus the members list (about 2 minutes).\n"
      + "Everything: every article (about 15 minutes); use it once in a while "
      + "to pick up edits and removals.\n\nKeep this tab open while it runs.",
    [{ label: "Newest", value: "quick", primary: true },
      { label: "Everything", value: "all" },
      { label: "Cancel", value: null }]);
  if (!mode) return finish();

  let last = 0;
  let requests = 0;

  // Paced, same-origin fetch that honors "slow down" (429/503) answers.
  // Returns the page HTML, null for a page-level error, and throws "blocked"
  // if mhco.org keeps refusing.
  async function get(path) {
    let wait = 0;
    for (let attempt = 0; attempt < 4; attempt++) {
      const pace = mode === "all" ? PACE_ALL_MS : PACE_MS;
      const gap = Math.max(pace - (Date.now() - last), wait);
      if (gap > 0) await sleep(gap);
      last = Date.now();
      requests++;
      let resp;
      try {
        resp = await fetch(path, { credentials: "same-origin",
          headers: { Accept: "text/html" } });
      } catch (e) {
        // A page that redirects off mhco.org can't be read from here; that's
        // this page's problem, not mhco.org refusing us.
        const probe = await fetch(path, { credentials: "same-origin",
          redirect: "manual" }).catch(() => null);
        if (probe && probe.type === "opaqueredirect") return null;
        wait = 15000 * (attempt + 1);
        continue;
      }
      if (resp.status === 429 || resp.status === 503) {
        const ra = parseInt(resp.headers.get("Retry-After") || "", 10);
        wait = (ra > 0 ? ra : 30 * (attempt + 1)) * 1000;
        if (wait > 300000) break;
        status(`mhco.org asked us to slow down; waiting ${Math.round(wait / 1000)}s…`);
        continue;
      }
      if (!resp.ok) return null;
      return await resp.text();
    }
    throw new Error("blocked");
  }

  let progress = "";
  function status(extra) {
    show("Chat MHC: collecting…", progress + (extra ? "\n" + extra : "")
      + "\n\nKeep this tab open.");
  }

  function clean(html) {
    const doc = new DOMParser().parseFromString(html, "text/html");
    doc.querySelectorAll("script,style,svg,noscript,iframe,img,picture,video,"
      + "audio,source,link,nav,footer,input,select,textarea,button")
      .forEach((e) => e.remove());
    [...doc.head.children].forEach((e) => {
      if (e.tagName !== "TITLE" && e.tagName !== "META") e.remove();
    });
    for (const el of doc.querySelectorAll("*")) {
      for (const a of [...el.attributes]) {
        if (!KEEP_ATTRS.has(a.name)) el.removeAttribute(a.name);
      }
    }
    return "<!DOCTYPE html>" + doc.documentElement.outerHTML.replace(/\s+/g, " ");
  }

  const prefix = location.origin + LISTING + "/";
  const urls = [];
  const articles = [];
  let members = null;
  let listingComplete = false;
  let stoppedEarly = false;
  let unreadable = 0;
  let problem = "";
  try {
    const maxPages = mode === "all" ? MAX_PAGES : QUICK_PAGES;
    for (let page = 0; page < maxPages; page++) {
      progress = `Reading the article list, page ${page + 1}…`;
      status();
      const html = await get(page ? `${LISTING}?page=${page}` : LISTING);
      if (html === null) break;
      const doc = new DOMParser().parseFromString(html, "text/html");
      const inSite = [];
      for (const a of doc.querySelectorAll(SELECTORS)) {
        const href = a.getAttribute("href");
        if (!href) continue;
        let u;
        try {
          u = new URL(href, location.origin).href.split("#")[0];
        } catch (e) {
          continue;
        }
        if (u.startsWith(location.origin) && !inSite.includes(u)) inSite.push(u);
      }
      let found = inSite.filter((u) => u.startsWith(prefix));
      if (!found.length) found = inSite;
      const fresh = found.filter((u) => !urls.includes(u));
      if (!fresh.length) {
        listingComplete = true;
        break;
      }
      urls.push(...fresh);
    }
    progress = "Reading the associate members page…";
    status();
    const mhtml = await get(MEMBERS);
    if (mhtml !== null) members = { url: location.origin + MEMBERS, html: clean(mhtml) };
    for (let i = 0; i < urls.length; i++) {
      progress = `Reading article ${i + 1} of ${urls.length}…`;
      status();
      const html = await get(urls[i]);
      if (html === null) unreadable++;
      else articles.push({ url: urls[i], html: clean(html) });
    }
  } catch (e) {
    stoppedEarly = true;
    if (e.message !== "blocked") problem = String(e);
  }

  if (!articles.length && !members) {
    await show("Chat MHC: nothing collected",
      problem ? `Something went wrong (${problem}). Please try again later.`
        : stoppedEarly ? "mhco.org kept asking us to slow down. Please try again later."
          : "No articles or members could be read. Are you logged in to mhco.org?",
      [{ label: "Close", value: true, primary: true }]);
    return finish();
  }

  const payload = {
    format: "chatmhc-mhco-export", version: 1,
    created_at: new Date().toISOString(), origin: location.origin,
    mode, logged_in: loggedIn,
    listing_complete: mode === "all" && listingComplete && !stoppedEarly,
    stopped_early: stoppedEarly, listing_urls: urls, unreadable,
    articles, members,
  };
  let blob = new Blob([JSON.stringify(payload)], { type: "application/json" });
  let name = `mhco-export-${payload.created_at.slice(0, 19).replace(/[:T]/g, "-")}.json`;
  if (window.CompressionStream) {
    blob = await new Response(blob.stream().pipeThrough(
      new CompressionStream("gzip"))).blob();
    name += ".gz";
  }
  const link = document.createElement("a");
  link.href = URL.createObjectURL(blob);
  link.download = name;
  document.body.appendChild(link);
  link.click();
  link.remove();

  await show("Chat MHC: saved!",
    `Saved "${name}" to your Downloads: ${articles.length} articles`
      + (members ? " and the members list" : "") + ` (${requests} page requests).`
      + (stoppedEarly ? (problem ? `\nIt stopped early (${problem}), ` : "\nmhco.org "
        + "asked us to stop early, ") + "so this is partial; that's fine, just "
        + "upload it." : "")
      + (loggedIn ? "" : "\nYou weren't logged in, so this file can add new "
        + "articles but won't change or remove stored ones.")
      + "\n\nLast step: open the upload page, drag this file in, and click "
      + "\"Commit changes\". Chat MHC updates itself a few minutes later. "
      + "(Upload only the newest file: older ones are skipped.)",
    [{ label: "Open upload page", value: true, primary: true, href: UPLOAD_URL },
      { label: "Close", value: true }]);
  finish();
})();
