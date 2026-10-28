# Chat MHC — Setup Guide (no coding needed)

> **Note:** this public copy doesn't include MHCO's licensed content or the
> automatic-update workflow (Step 3). Deploying it gives you a working app on
> sample data; loading real MHCO content requires your own MHCO membership.

This puts your assistant online at a web address you and your staff can use
from any computer or phone. Plan about 30–40 minutes. **Hosting is $0/month**,
plus a few dollars of AI usage you pre-load.

## What "free" means here (read once)
- The app **sleeps after ~15 minutes** of nobody using it. The next visit
  takes about **1 minute** to wake it up, then it's fast again.
- Your **chat history** (the list of past conversations) **clears** when it
  sleeps. The answers themselves are just as good — you'd occasionally start a
  fresh conversation.
- Your **knowledge base and forms are always there** — laws, articles, and form
  PDFs never disappear, because they're built into the app itself.
- Oregon laws update themselves **once a month**. New MHCO articles come in with
  a **bookmark button** you click on mhco.org now and then (mhco.org blocks
  automated access from GitHub) — set up once in Step 3.

> Want no sleeping and saved chat history? You can switch to Render's paid
> Starter plan (~$7/month) later — it's a one-click change of the instance type,
> no rebuild.

## Before you start: gather 3 things

1. **An Anthropic AI key** (the app's "brain")
   - Go to **console.anthropic.com**, create an account.
   - Add a little credit (Billing → add ~$5 to start).
   - Open **API Keys → Create Key**, and copy the long key it shows you
     (starts with `sk-ant-`). Paste it somewhere safe for a few minutes.

2. **Your MHCO member login** — the username and password you use on mhco.org.
   These let the app pull in real articles, laws, and forms.

3. **A login you invent for your own app** — make up a username and a strong
   password. This is how you'll sign in to Chat MHC. Write it down.

## Step 1 — Create a Render account
- Go to **render.com** and sign up (you can sign in with your GitHub account).

## Step 2 — Deploy the app (Render does the work)
- Click **New ＋** (top right) → **Blueprint**.
- Connect your GitHub and choose the **Chat-MHC** repository.
- Render reads the included blueprint, picks the **Free** instance
  automatically, and shows a short list of blanks to fill. Enter these exactly:

  | Field Render shows | What to paste |
  |---|---|
  | `ANTHROPIC_API_KEY` | your `sk-ant-...` key from before |
  | `CHATBOT_USERNAME` | the app username you invented |
  | `CHATBOT_PASSWORD` | the app password you invented |
  | `SESSION_SECRET` | leave it — Render fills this automatically |
  | `MODEL_NAME` | leave the default (`claude-sonnet-5-5`) |

  (Your MHCO login is **not** entered here — it goes on GitHub in Step 3.)

- Click **Apply** / **Create**. Render builds the app — this takes about
  5 minutes. When it's done you'll see a web link like
  `https://chat-mhc.onrender.com`.

At this point the app already works, using a small set of built-in sample
Oregon laws and forms. Step 3 swaps in the real MHCO content and keeps it fresh.

## Step 3 — Turn on updates (one-time setup)
Updates run on a free helper that lives in your GitHub repository.

**3a. Give it your MHCO login (stored privately on GitHub):**
- On **github.com**, open your **Chat-MHC** repository.
- Go to **Settings → Secrets and variables → Actions → New repository secret**.
- Add two secrets:
  - Name `MHCO_USERNAME`, value = your mhco.org username → **Add secret**.
  - Name `MHCO_PASSWORD`, value = your mhco.org password → **Add secret**.

(mhco.org currently blocks GitHub, so these are only used if MHCO ever lifts
that block. Your MHCO content comes in through Step 3c instead.)

**3b. Run the first refresh:**
- Click the **Actions** tab (top of the repo).
- In the left list, click **Update knowledge base**.
- Click **Run workflow → Run workflow** (green button).
- Wait a few minutes. It pulls the official ORS 90/91 laws from the Oregon
  Legislature's website, and Render then updates your live site automatically.
  Click the finished run to see a summary table of what was refreshed.

From now on the laws refresh **once a month on their own** (or press
**Run workflow** any time).

**3c. Set up the MHCO bookmark button (Chrome or Edge):**
- In Chat MHC, click **Update MHCO content** at the bottom of the left sidebar
  (or open your Chat MHC address followed by `/mhco-helper.html`, e.g.
  `https://chat-mhc.onrender.com/mhco-helper.html`, if the window is too narrow
  to show the sidebar) and follow the page: drag the **Grab MHCO updates**
  button onto your bookmarks bar (one time).
- Whenever you want the latest MHCO articles: log in to mhco.org, click
  **Grab MHCO updates**, choose **Newest**, and when it says **Saved!** click
  **Open upload page**, drag the file in, and click **Commit changes**. Chat
  MHC updates itself about 5 minutes later.
- The first time, choose **Everything** instead (about 15 minutes). That also
  re-reads the older articles, so Chat MHC can pick up their publication dates
  where MHCO's pages show them, which helps the assistant prefer the newest
  guidance.

## Step 4 — Add your MHCO forms (one-time)
MHCO generates each form on demand, personalized with your park's name, and
serves it as a temporary link that expires within an hour — there's no stored
file to download automatically. So the forms are added by hand. They rarely
change, so this is a one-time job, and the PDFs come pre-filled with your park
name as a bonus.

1. Signed in to mhco.org, open each form so it generates, and save the PDF.
2. On **github.com**, open your **Chat-MHC** repo and click into the
   **`forms_library`** folder (it already exists, with a README inside).
3. Click **Add file → Upload files**, drag in all the PDFs at once, and
   **Commit**. The names MHCO gives them (like
   `Example_Mobile_Home_Park_-_01.pdf`) work as-is; rename to
   `Form 01 - Rental Application.pdf` style for nicer titles if you like.
4. The forms appear in the app on its next deploy. To make them go live right
   away, in Render click **Manual Deploy → Deploy latest commit**.

To update later, just drop new PDFs in that folder (same name replaces a form;
delete a file to remove it). Full details are in `forms_library/README.md`.

## Step 5 — Use it
- Open your web link, sign in with the **app** username/password you invented.
- Ask questions, browse the Forms and Oregon Laws tabs, download form PDFs.

## Keep MHCO content private
MHCO content is members-only. If you deploy your own copy and add MHCO
articles or forms to it, make that repository **Private** first
(GitHub → repo → Settings → General).

## If something looks wrong
- **"AI key missing"** in the corner → the `ANTHROPIC_API_KEY` wasn't saved; in
  Render open **Environment**, check it, and **Save** (the app restarts).
- **Laws look like sample data** → Step 3b hasn't finished yet. Check the
  **Actions** tab — a green check means it ran.
- **An update run shows a red ❌** → open the run and read its summary table.
  **Nothing was lost:** whatever couldn't be refreshed was kept exactly as it
  was. (mhco.org blocking GitHub does *not* turn it red; that's expected.) It
  turns red when:
  - the Oregon laws couldn't be refreshed (the Legislature's website may have
    changed) — they'll be retried on the next run;
  - an uploaded file couldn't be used: it was older than one already imported,
    damaged, not a bookmark file, none of its articles could be read, or its
    members list was much shorter than the stored one. The file has been
    removed; click **Grab MHCO updates** again (logged in) and upload the new
    file.
- **Want to re-check every article** (e.g. MHCO edited old posts)? Use the
  bookmark button and choose **Everything**.
- **First visit of the day is slow** → that's the ~1-minute wake-up; normal on
  the free plan.

## Note for whoever deploys it
Automatic monthly updates only run from the repository's **main** branch, so
make sure the app code is merged into `main` before relying on the schedule.
(The manual **Run workflow** button works from any branch.)
