# Chat MHC

**An AI assistant for Oregon manufactured-home park owners and managers.**

Ask a question the way you'd ask an experienced colleague, such as *"How much
notice do I need to give before a rent increase?"* or *"Which form do I use
for a 30-day notice?"* Chat MHC answers in plain language. It cites exactly
where each answer came from and hands you the right MHCO form to download.

> **About this repository.** This is the public copy of a private, deployed
> project. MHCO's articles and form PDFs are licensed, members-only content, so
> they aren't included here. Out of the box, the app runs on a small set of
> sample data instead, and the numbers below describe the live deployment.

---

## Why it exists

Running a manufactured-home community in Oregon means working from two kinds
of source material:

- **The law:** Oregon Revised Statutes chapters 90 and 91, the landlord-tenant
  rules that govern parks.
- **Industry guidance:** years of articles, legislative updates, and forms
  published by Manufactured Housing Communities of Oregon (MHCO).

Finding the right answer usually means digging through both. Chat MHC reads
all of it for you, so you get a grounded answer in seconds and can still
check the original sources yourself.

## What it does

| | |
|---|---|
| 💬 **Answers questions** | Plain-English answers grounded in Oregon law and MHCO guidance, written as they're generated so you aren't left waiting. |
| 📎 **Shows its sources** | Every answer has a **Sources** button listing the exact statutes and articles it drew on. |
| 📄 **Hands you the forms** | A searchable library of 77 MHCO forms you can download as PDFs. |
| ⚖️ **Lets you browse the law** | A built-in reader for the official text of ORS 90 and 91. |
| 🕑 **Remembers your conversations** | Pick up where you left off, or archive old chats. |
| 🔄 **Stays current** | Oregon law refreshes itself monthly. New MHCO articles come in with one click of a bookmark button on mhco.org. |

### What's inside the knowledge base (live deployment)

- The **official text of ORS chapters 90 and 91**, taken directly from
  oregonlegislature.gov
- **310 MHCO articles** and community updates
- The **MHCO associate members directory**, with 33 listed vendors and
  service providers
- **77 MHCO form PDFs**

The assistant gives priority to the law over commentary, and newer guidance
over older articles. That way, outdated advice doesn't outrank the
current rules.

## Built to be trusted

- **Grounded, not guessed.** Answers come from the knowledge base above, and
  sources are always one click away.
- **Private by design.** MHCO content is for members only, so everything sits
  behind a login, including the form downloads.
- **Security reviewed.** It has hardened sign-in and limits on repeated login
  attempts, and an automated test suite runs on every change. See
  [SECURITY.md](SECURITY.md) for the full security and privacy overview.
- **Low cost to run.** It runs on Render's free hosting tier. The only ongoing
  cost is a few dollars of AI usage.

## How it works

```
  Your question
        │
        ▼
  Search the knowledge base ──► Oregon law · MHCO articles · forms · members
        │
        ▼
  Pick the most relevant passages
  (law first, recent guidance over old)
        │
        ▼
  Claude writes the answer ──► streamed to your screen, with sources
```

Under the hood, Chat MHC is a lightweight Python web app (FastAPI and SQLite)
using Anthropic's Claude to write answers. It runs as a single small server,
with no specialized infrastructure needed.

## Getting started

| If you want to… | Read |
|---|---|
| Run it locally, change the code, or understand the content pipeline | [DEVELOPMENT.md](DEVELOPMENT.md) |
| Review security, privacy, and data handling | [SECURITY.md](SECURITY.md) |

## Note

Chat MHC is a research aid, not legal advice. For decisions with legal
consequences, confirm with the statute text (linked in every answer) or an
attorney. MHCO content is licensed to members and is not part of this public
repository; never commit MHCO articles or form PDFs here.
