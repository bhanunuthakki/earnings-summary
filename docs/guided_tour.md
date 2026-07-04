# A guided tour — what it's like to actually use this

*Read this one first. It's the demo, not the manual. Where the QA walkthrough is a checklist of every button, this is the story of a normal week — sit down, open the app, and let me walk you through what you're looking at and why each piece is trying to earn its place. When you want the exhaustive click-by-click, `qa_walkthrough.md` is next door.*

---

## First, why any of this exists

You built a very good research library and then didn't live in it. That's the whole story. The DCFs were sound, the KPIs were tracked, the theses were written — and the number of times you actually reviewed a position before selling was zero. A tool that knows everything about your companies and nothing about your habits can't make you a better investor; it just makes you a better-informed one, which is not the same thing.

So the recent work wasn't about adding more analysis. It was about pulling you back into the building. Almost everything in this tour is a door — placed where you'd naturally walk, at the moment you'd naturally need it. Keep that in mind as you go: when something feels like it's *nudging* you, that's on purpose.

## The two-minute setup (do this once, or the tour is a lie)

The app you'll open is served by a small local web server, and your Telegram bot is a second little program that listens for your messages. Both of them run the *code as it was when they last started*. So the first time you sit down after new work has landed:

1. Pull the latest code into your main folder.
2. Restart the two background services (the dashboard and the poller).
3. Rebuild a report or two so the per-company pages reflect the new build.

If you skip this, you'll be touring last month's app and wondering why nothing I describe is there. That's not a bug — it's just that a running program can't change its own mind mid-thought. (The exact commands are in the QA doc's "Deployment reality check.")

Then open your browser to **localhost:7421**. Here we go.

---

## Monday morning: the app tells you what's waiting

The first thing you see isn't a stock price. That's deliberate — a terminal opens on the market; a partner opens on *you*.

Across the top of the main column is a thin band of **open loops** — the things the system is quietly holding for you. It reads like a colleague's sticky notes: *"Reconcile: 4 waiting · oldest 12d," "Tenets proposed: 2," "Ungraded decisions: 3."* Each line is a link straight to the place you'd go to clear it, and each one carries an age, because a thing waiting twelve days is a different kind of nag than a thing waiting since this morning. On the day you've handled everything, the band says so out loud — *"Ritual clear — nothing waiting on you."* — rather than just vanishing, so you can tell the difference between "all clear" and "the feature broke."

That band is the single most important change in the whole program. It's the app admitting that its job is to be walked, and volunteering the map.

Below it sits the **cockpit** — your holdings as a dense table. Ticker, whether the thesis is intact or breached, the notable KPI moves this quarter, the price, how it sits against your DCF fair value, its PEG, when it next reports, and a little cluster of pills if there's an alert or a new document waiting on that name. This is your one-glance "is anything on fire" scan. Nothing here is decoration — every number that has a deeper story behind it is clickable, and clicking a ticker anywhere in the app opens that company without a page reload.

Down the right side is the **inbox** — a single ranked stream, not five separate piles. Alerts, draft actions the system wants your yes/no on, notes waiting to be filed, synthesized observations. New-since-you-looked items carry a faint accent. You approve, or you dismiss — and when you dismiss, you can leave a one-word reason, because *why* you passed on something is exactly the signal the system is trying to learn from. A shrug is data too.

That's the morning: read the band, act on the chips, and you've done the daily ritual in about the time it takes to finish a coffee.

---

## A thought strikes: catching it before it evaporates

Mid-afternoon you have a half-formed hunch — *"Nu's deposit costs feel stickier than the bears think."* The old failure mode was that this thought lived in your head, informed a trade three weeks later, and never got written down anywhere the system could learn from. So capture is now everywhere and costs nothing.

At your desk, hit **Ctrl + . (control-period)** from any screen. A small box drops down — type the thought, Ctrl+Enter, done. If you had a company open, it even pre-fills the ticker so the note lands *attached* to Nu instead of floating free. On your phone, just message the Telegram bot — type it, or send a voice memo and it transcribes. Either way it answers *"Captured. (NU)"* so you know it landed and knew which name you meant.

Here's the part that matters: a captured thought isn't just filed. If it *sounds like a question you're wondering about*, the system quietly flags it as something worth researching later. If it sounds like *you're about to act* — "thinking about adding to Nu" — it treats that differently, which brings us to the most important door in the app.

---

## You're about to trade: the moment the coach exists for

This is the whole ballgame. Your own graded record shows a specific, expensive habit: you sell your winners too early. Nvidia, Google, Amazon, TSMC, Micron — trimmed or sold, and wrong to. A coach that can't do anything about *that* is a decoration.

So there are two moments the system tries to stand in front of.

**Before you buy something meaningful,** you send a quick pledge to the bot — *"buying more Nu ~$2k."* It doesn't just log it. It sends back a challenge — the catalyst test, in your own words, the rules *you* set: what's the near-term catalyst, is it already priced in, what would make this a value trap. You answer honestly or you notice you can't, and either way you've thought before you clicked. (The bot now recognizes the natural way you actually talk about selling, too — "taking profits," "lightening up," "exiting" — not just the textbook verbs.)

**Before you trim or sell,** you run **`/review NU`** — type it in the Ask box, or send it to the bot. Instantly, no waiting, it hands you a grounded read of the position: your weight, where price sits versus fair value, which of your break-rules are tripped, and — this is the point — *your own base rate*, in plain numbers: your graded record on selling winners, right there next to the sell you're contemplating. If your history and this trade rhyme, you're looking at the rhyme before you act, not in a post-mortem.

And behind that instant read sits the full version — a deeper LLM review with a behavioral guard that will actually *override* a "trim" back to "hold" when the setup matches your worst pattern (thesis intact, not oversized, not overvalued — i.e., a winner you're bailing on out of nerves). That fuller review costs a real model call, so it's one click past the instant one rather than automatic. Run it deliberately on a name you're genuinely wrestling with — and know that every time you do, it leaves a graded record behind, which is the raw material the coach uses to get sharper about *you* over time.

The honest truth, and you should hold it: none of this can grab your hand. There's no broker wired in; the app can't stop the order. What it can do is be *one tap away at the exact moment*, and be honest with you when you get there. That's the trade the design makes on purpose — a coach you can always overrule, standing where you'll pass by anyway.

---

## A company reports: going deep without drowning

Novo reports Thursday. You open it — click the ticker anywhere, or search it with **Ctrl + K** — and you land on its research page. This is where the "deeply understand a company" goal lives, and it's built to match how you actually re-engage with a name you already know.

At the very top, before any number, is your own layer: the thesis, and a **verdict badge** — Intact, or breached — that now carries a date. And it's honest about that date: if your last evaluation happened before this quarter's print was actually ingested, the badge goes *grey*, not confident green. A stale "all good" that hasn't seen the new numbers is exactly the lie that erodes trust over years, so it refuses to tell it.

Right under that is a **"what changed" line** — the lede of the system's own reread of the name, hoisted to the top so you don't have to hunt. (Fair warning, and it's in the honest-seams section below: this reflects the last time the *reread* was generated, not the last time *you personally* looked — the app doesn't yet track your visits. It's "what's new in this build," which for a fresh quarter is usually exactly what you want.)

Then the six tabs, in the order an investor with an existing thesis actually thinks: **Overview** (thesis + valuation), **Quarter** (earnings, say-vs-did, news), **Financials**, **Research** (the business, bear case, exec comp), **Position** (what you own, and your decision history on it), **Sources**. The KPI tiles across the top aren't dead numbers — click one and it drops you into a chat about that exact fact, sourced. Same with the cells and quotes throughout: a number with a story behind it is a door, not a decoration. Select any passage and you can save it straight to your journal, attached to the name.

The **Position tab** is where the coach shows up inside the research view: how many times the guard has actually run on this name (honestly zero, if it has), your graded-sells record, and a button straight into a review. So even when you're just reading, the moment you drift toward "should I trim this," the door is right there.

If you want to interrogate rather than read, there's a chat on every company — ask it a question in plain English and it answers from that company's filings and transcripts, with citations you can click back to.

---

## Sunday: the half-hour that keeps it alive

Once a week you sit down with the **Ledger** — it's under Companies, and it's the one surface that's entirely *yours*: your captured thoughts, your beliefs, your open questions. A row of jump-chips at the top lets you skip straight to a section.

Three things earn the visit:

**Reconcile.** Over the week the system has been catching things — a falsifier you wrote that's now armed and watching a position, a belief that needs your sign-off before it's allowed to influence anything. You ratify the ones that are real, rewrite the ones that need sharpening (right in place — a proper text box, not a cramped browser prompt), drop the ones that don't hold up. When you ratify a falsifier, it tells you it's now armed and watching — the action has a visible consequence, so the ritual doesn't feel like shouting into a void. And there's a standing table of everything currently armed on your book, so you can always see what's watching for you.

**Research proposals.** The wonderings you captured during the week, that the system went and researched, come back here as proposals you can approve, reject, ask it to dig further, or steer in a new direction.

**Worldview.** Your durable beliefs — Tenets — distilled from your own musings, waiting for your accept/edit/reject. Nothing you haven't blessed is allowed to whisper in the system's ear. This is how the thing slowly comes to argue from *your* framework instead of a generic one.

None of it builds anything. It's a ritual, not a feature. But it's the mechanism by which a pile of captured thoughts becomes a partner that actually knows what you think.

---

## Checking the coach's own report card

Under **Portfolio → Decisions** is the mirror. At the very top, your calibration vitals — how many decisions, how many graded, your hit rate, how often you reversed yourself. Below that, the sizing audit and the reversals, where "sold too early" stops being a feeling and becomes a row.

And then the **Coach's P&L** — the system keeping honest score on *itself*. How many reviews you've actually run, how many times the guard fired, how often it was right. It shows you zeros when they're zeros — *"the guard has never been exercised"* — because a coach that inflates its own record is worse than none. There's a literal counter for the one bar that matters: has the coach changed a real decision of yours yet, against a target of one by end of Q3. (That counter was recently tightened so it can't quietly declare victory through your inaction, or count a test run as the real thing — it waits for a genuine, attested change.)

This page is the long game. The coach only becomes worth listening to if it can show you, in graded numbers, that listening to it has paid — including the times it was wrong. That's how trust compounds across years instead of evaporating on the first bad call.

---

## When something's quietly broken

Up in the corner is a small **▦** icon — that's System, and it wears a status dot. Green means the overnight data pipeline ran clean; if it turns, something upstream needs a look. (It now checks its own freshness, so a dead watchdog can't sit there glowing green forever — the one failure mode that used to hide everything.) Click in and you get the provenance console: data quality, cron health, the model-cost optimizer, settings. This is the two-minute weekly glance that catches silent failures before they rot a week of data underneath you.

---

## What's honest to know (so nothing surprises you)

A good demo doesn't oversell. A few seams worth holding in mind:

- **The coach is opt-in by design.** It stands where you'll walk and it's honest when you get there, but it cannot intercept a trade you don't bring to it. The pledge, the review — you initiate them. The whole bet is that making them one-tap-easy and genuinely useful is enough to change the habit. Whether it does is the open question the Q3 counter is honestly tracking.
- **"What changed" is per-build, not per-visit.** The app doesn't yet know how long you were gone — it can show you what's new in the latest rebuild, but it can't yet greet you with "here's what moved in the eleven days since you last looked." That's a known next step, not a hidden failure.
- **The richest flows cost real model calls** — the full review, chat, researching a wondering, the pledge's decision-extraction. They're deliberately not automatic. Spend them where they earn it.
- **Nothing reaches you until you pull and restart.** Worth repeating because it's the one thing that makes the whole tour evaporate if skipped.

---

## The shortest possible version

Sit down. The app shows you what's waiting (Home). You catch thoughts as they come, from anywhere (Ctrl+. or the bot). Before you trade, the coach is one tap away with your own track record in hand (pledge / `/review`). You go deep on a name the way you actually think (the report). Once a week you tend the loop (the Ledger). And you can always check whether the coach has earned its keep (Decisions). Everything else is depth beneath those six moves.

The building has doors now. The only thing left that no amount of building can do — is for you to walk in.
