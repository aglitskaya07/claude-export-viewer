#!/usr/bin/env python3
"""Builds demo/claude-export-demo: a made-up claude.ai export with the same layout as a real one.

Everything here is fictional: Sam, a freelance product designer who writes a newsletter and
trains for a half marathon. Run it again after editing the content below.
"""
import io
import json
import os
import uuid
import zipfile

HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(HERE, "claude-export-demo")
ACCOUNT = "5a1d0c2e-0000-4000-8000-00000000demo"
NS = uuid.UUID("12345678-1234-5678-1234-567812345678")


def uid(*parts):
    return str(uuid.uuid5(NS, "/".join(parts)))


# ---------- tiny DSL for messages ----------

def text(t):
    return {"type": "text", "text": t}


def think(t):
    return {"type": "thinking", "thinking": t}


def tool(name, **inp):
    return {"type": "tool_use", "name": name, "input": inp}


def result(name, t):
    return {"type": "tool_result", "name": name, "content": [{"type": "text", "text": t}]}


def chat(title, start, turns, summary=None):
    """turns: list of (sender, time, blocks, attachments)."""
    cid = uid("chat", title)
    msgs = []
    for i, turn in enumerate(turns):
        sender, ts, blocks = turn[:3]
        atts = turn[3] if len(turn) > 3 else []
        when = f"{start}T{ts}:00.000000Z"
        msgs.append({
            "uuid": uid(cid, str(i)),
            "text": "\n\n".join(b["text"] for b in blocks if b["type"] == "text"),
            "content": blocks,
            "sender": sender,
            "created_at": when,
            "updated_at": when,
            "attachments": atts,
            "files": [],
            "parent_message_uuid": uid(cid, str(i - 1)) if i else "00000000-0000-4000-8000-000000000000",
        })
    return {"uuid": cid, "name": title, "summary": summary or "",
            "created_at": msgs[0]["created_at"], "updated_at": msgs[-1]["created_at"],
            "account": {"uuid": ACCOUNT}, "chat_messages": msgs}


H, A = "human", "assistant"

PLAN = """# Half marathon: 10 weeks to sub-1:50

Goal pace: **5:12/km**. Current 10K: 49:30. Three runs a week plus one strength session.

| Week | Long run | Quality session | Easy run |
|---|---|---|---|
| 1 | 12 km easy | 6 × 800 m @ 4:55 | 6 km |
| 2 | 13 km easy | 3 × 2 km @ 5:10 | 6 km |
| 3 | 14 km, last 3 @ goal pace | 8 × 400 m @ 4:40 | 7 km |
| 4 | 11 km easy (recovery week) | 20 min tempo @ 5:05 | 6 km |
| 5 | 16 km easy | 4 × 1.6 km @ 5:00 | 7 km |
| 6 | 17 km, last 5 @ goal pace | 5 × 1 km @ 4:50 | 7 km |
| 7 | 18 km easy | 2 × 4 km @ 5:10 | 8 km |
| 8 | 14 km easy (recovery week) | 25 min tempo @ 5:05 | 6 km |
| 9 | 16 km with 8 @ goal pace | 6 × 800 m @ 4:50 | 6 km |
| 10 | Race week: 8 km easy + strides | 3 × 1 km @ goal pace | 4 km shakeout |

**Rules I keep:** easy means conversational. Skip a quality session before skipping a long run. Any sharp pain — stop.
"""

REPORT = """# AI tools for UX research synthesis — short report

**Question:** which tools help turn 20–40 interview transcripts into themes without losing the evidence trail?

## What I looked at
- **Dovetail** — tagging plus AI summaries per highlight. Strong evidence trail, summaries are generic.
- **Condens** — auto-transcription and clustering; good for teams that already tag manually.
- **A plain LLM + a tagging spreadsheet** — cheapest, most flexible, needs a careful prompt and a human check of every quote.

## Recommendation
For a two-week fintech engagement: plain LLM for the first pass, every theme must cite at least three quotes with participant IDs, then a human pass in the spreadsheet. Buy a tool only if research becomes continuous.

## Caveats
Pricing pages change monthly; numbers above were checked in March 2026.
"""

PROPOSAL = """# Design audit — Northwind Bank mobile app

**Scope:** onboarding, card management, transfers. 3 weeks.

| Phase | What you get | Days |
|---|---|---|
| Heuristic review | Annotated screens, severity-ranked issue list | 4 |
| 6 user interviews | Recordings, highlights, top 5 friction points | 6 |
| Recommendations | Prioritized fixes with effort estimates, 2 redesigned flows | 5 |

**Fee:** €9,600, 40% upfront. One revision round included.
"""

AGENDA = """# Workshop: AI for design teams (3 hours)

1. **Where AI already sits in your process** (20 min) — map one recent project, mark every step a model touched.
2. **Synthesis with receipts** (45 min) — turn 10 interview snippets into themes; every theme cites quotes.
3. **Break** (15 min)
4. **Prototyping prompts** (40 min) — from a written flow to a clickable draft.
5. **Where it breaks** (30 min) — three failure cases from real projects.
6. **Your team’s rules** (30 min) — write five rules the team will actually keep.
"""

CLAUDE_MD = """# How I work — context for any assistant

- I'm Sam, a freelance product designer. Clients: fintech and health.
- Answer in plain English, short paragraphs, no bullet soup.
- When you research, link sources and say what you didn't check.
- My files live in ~/work as markdown, synced to a private GitHub repo every hour.
"""

TEARDOWN = """# Onboarding teardown: Monzo, Revolut, N26

| | Steps to first transfer | Time | Best moment | Weakest moment |
|---|---|---|---|---|
| Monzo | 9 | 6 min | Card arrives with your name printed in the app preview | ID photo retakes give no hint why |
| Revolut | 12 | 8 min | Instant virtual card | Upsell to Premium before the first transfer |
| N26 | 10 | 9 min | Clear progress bar | Video ident queue, no estimate |

**Pattern worth stealing:** show the product working (a card, a balance) before asking for the hardest step.
"""

LANDING = """<!doctype html><html><head><meta charset="utf-8"><title>AI for design teams</title>
<style>body{font:18px/1.5 system-ui;max-width:640px;margin:60px auto;padding:0 20px}h1{font-size:40px;line-height:1.1}
.cta{display:inline-block;background:#111;color:#fff;padding:12px 22px;border-radius:999px;text-decoration:none}</style></head>
<body><h1>AI for design teams</h1><p>A three-hour workshop for product teams who want models in the process without losing the evidence.</p>
<ul><li>Synthesis that cites its quotes</li><li>Prototyping from a written flow</li><li>Five rules your team will keep</li></ul>
<p><a class="cta" href="#">Book a date</a></p></body></html>"""

INTERVIEW = """P04, 34, uses three banking apps.
"I opened the app to send rent and it asked me to verify my address again. I gave up and used my old bank."
"I don't read the notifications anymore, there are too many promos."
"The card freeze button is great, I used it twice when I couldn't find my wallet."
P07, 51, small business owner.
"Transfers over 5,000 always fail the first time and nobody tells me why."
"I like that I can see pending payments, my accountant asks for them."
"""

CHATS = [
    chat("Half marathon plan: 10 weeks to sub-1:50", "2026-08-03", [
        (H, "07:41", [text("I want to run the Lisbon half on Oct 12 under 1:50. My 10K is 49:30, I run 3 times a week. Can you make a plan?")]),
        (A, "07:42", [think("49:30 10K predicts roughly 1:49–1:51 for a half with proper long runs. Three runs a week: long, quality, easy. Ten weeks with two recovery weeks."),
                      text("Your 10K says sub-1:50 is realistic but tight, so the plan leans on long runs. Three runs a week: one long, one quality, one easy, plus a strength session."),
                      tool("create_file", path="/mnt/user-data/outputs/training_plan.md", file_text=PLAN.replace("5:12/km", "5:10/km"), description="Writing the 10-week plan"),
                      result("create_file", "File created successfully"),
                      tool("str_replace", path="/mnt/user-data/outputs/training_plan.md", old_str="5:10/km", new_str="5:12/km", description="Fixing goal pace"),
                      result("str_replace", "Edited"),
                      tool("present_files", filepaths=["/mnt/user-data/outputs/training_plan.md"]),
                      text("Goal pace is **5:12/km**. Weeks 4 and 8 are recovery weeks — keep them easy even if you feel great.")]),
        (H, "07:50", [text("Week 3 has 8×400 at 4:40, isn't that too fast for me?")]),
        (A, "07:51", [text("It's about your 5K effort, which is right for short reps. If the last two feel ragged, run them at 4:50 — the point is turnover, not suffering.")]),
        (H, "19:12", [text("Ok. Last question: what do I eat before the long runs?")]),
        (A, "19:13", [text("Under 75 minutes you can go fasted if your stomach prefers it. Longer: a banana or toast with honey 60–90 minutes before, and one gel every 40 minutes once runs pass 90 minutes. Practise the race-morning breakfast on weeks 6 and 9.")]),
    ], summary="Sam asked for a 10-week half marathon plan targeting sub-1:50 in Lisbon; Claude built a three-runs-a-week plan with goal pace 5:12/km and fueling advice."),

    chat("Newsletter #12: why I stopped using Notion", "2026-09-14", [
        (H, "10:05", [text("Here's my draft for issue 12. It's too long and the ending is weak. Tighten it, keep my voice.\n\nI moved 400 pages out of Notion last month. Not because Notion is bad...")]),
        (A, "10:06", [text("The draft has two essays in it: why you left, and what plain files gave you back. I kept the second one as the spine and cut the tool comparison to one paragraph."),
                      tool("create_file", path="/mnt/user-data/outputs/issue-12.md", file_text="# Why I stopped using Notion\n\nI moved 400 pages out of Notion last month. Not because Notion is bad, but because my assistant can't read it.\n\nEvery morning I ask a model to plan my day. It reads my calendar, my notes, my client folders. It reads files. It doesn't read databases behind a login.\n\nSo my notes became markdown in a folder, synced to git. The surprise wasn't speed. It was that the folder started answering questions: what did I promise this client, when did I last change the pricing, which interviews mentioned rent.\n\nThe tool didn't get smarter. My context became reachable.\n", description="Tightened draft"),
                      tool("present_files", filepaths=["/mnt/user-data/outputs/issue-12.md"]),
                      text("New ending: *“The tool didn't get smarter. My context became reachable.”* It closes the loop with the opening line instead of summarising.")]),
        (H, "10:20", [text("Love the ending. Can you suggest 3 subject lines?")]),
        (A, "10:20", [text("1. My notes started answering questions\n2. 400 pages out of Notion\n3. The tool didn't get smarter")]),
    ]),

    chat("Research: AI tools for UX research synthesis", "2026-03-09", [
        (H, "14:00", [text("I have 30 interview transcripts for a fintech client. Which tools help synthesize them without losing the quotes? Two-week project, small budget.")]),
        (A, "14:01", [tool("web_search", query="Dovetail AI synthesis pricing 2026"),
                      result("web_search", "Dovetail pricing page: Professional plan per seat per month; AI highlights and summaries included…"),
                      tool("web_search", query="Condens research repository AI clustering"),
                      result("web_search", "Condens: auto transcription, AI-assisted clustering of highlights, per-seat pricing…"),
                      tool("web_fetch", url="https://example.com/ux-synthesis-llm-guide"),
                      result("web_fetch", "A guide to using general-purpose LLMs for interview synthesis with citation checks…"),
                      tool("create_file", path="/mnt/user-data/outputs/synthesis-tools-report.md", file_text=REPORT, description="Short report"),
                      tool("present_files", filepaths=["/mnt/user-data/outputs/synthesis-tools-report.md"]),
                      text("Short version: for two weeks, a plain LLM plus a tagging spreadsheet beats buying a tool — as long as every theme cites quotes with participant IDs. The report has the comparison and caveats.")]),
        (H, "14:15", [text("What prompt would you use for the first pass?")]),
        (A, "14:16", [text("```\nYou are helping synthesize user interviews.\nFor each theme: a one-line name, a two-sentence explanation,\nand at least three verbatim quotes with participant IDs.\nIf fewer than three quotes support it, mark it as weak.\nDo not invent quotes. Do not merge participants.\n```")]),
    ]),

    chat("Lisbon weekend: where to eat near Alfama", "2026-10-02", [
        (H, "18:30", [text("Going to Lisbon for the half marathon. Where should I eat near Alfama the night before? Nothing heavy.")]),
        (A, "18:31", [tool("web_search", query="Alfama restaurants grilled fish light dinner"),
                      result("web_search", "Several family-run tascas around Largo de São Miguel serve grilled fish and salads…"),
                      text("Night before a race: grilled fish, rice or potatoes, salad. The small tascas around Largo de São Miguel do exactly that. Book early — race weekend fills up — and skip the vinho verde until after.")]),
        (H, "18:40", [text("And breakfast on race morning near the start?")]),
        (A, "18:41", [text("Most cafés open at 7; the race starts at 8:30. Safer to bring your own: the toast-and-honey breakfast you practised in weeks 6 and 9.")]),
    ]),

    chat("Pricing a design audit for a fintech client", "2026-04-21", [
        (H, "09:10", [text("Northwind Bank wants a design audit of their app: onboarding, cards, transfers. I usually charge €650/day. How do I scope and price it?")]),
        (A, "09:11", [think("Three phases: heuristic review, interviews, recommendations. ~15 days. 15 × 650 = 9,750; round to 9,600 for a clean number, upfront deposit standard for new clients."),
                      text("Scope it in three phases so they can see where the days go. At your rate it lands just under €10K."),
                      tool("create_file", path="/mnt/user-data/outputs/northwind-proposal.md", file_text=PROPOSAL, description="Proposal draft"),
                      tool("present_files", filepaths=["/mnt/user-data/outputs/northwind-proposal.md"]),
                      text("I rounded to €9,600 and added a 40% deposit — standard for a new client, and it filters out tyre-kickers.")]),
        (H, "09:30", [text("They'll ask for a discount. What do I say?")]),
        (A, "09:31", [text("Offer scope, not price: drop the interviews (–€3,900) and they get a heuristic review with recommendations. Keeping the rate intact keeps the next project at the same rate.")]),
    ]),

    chat("Interview notes → insights", "2026-04-29", [
        (H, "16:02", [text("Here are notes from two interviews. Pull out insights with quotes.")],
         [{"file_name": "interviews_p04_p07.txt", "file_size": 812, "file_type": "txt", "extracted_content": INTERVIEW}]),
        (A, "16:03", [text("**1. Re-verification at the moment of need drives people away.** P04 abandoned a rent transfer when asked to verify the address again: *“I gave up and used my old bank.”*\n\n**2. Large transfers fail silently.** P07: *“Transfers over 5,000 always fail the first time and nobody tells me why.”*\n\n**3. Control features earn trust.** Card freeze (P04) and pending payments (P07) are mentioned positively without prompting.\n\n**Weak signal:** notification fatigue — one participant only.")]),
        (H, "16:10", [text("Which one would you fix first?")]),
        (A, "16:10", [text("Silent failures on large transfers. It hits the most valuable users, and the fix is mostly copy: say why it failed and what to do next.")]),
    ]),

    chat("Rewrite my LinkedIn headline", "2026-06-11", [
        (H, "12:00", [text("My headline is 'Product Designer | UX | UI | Fintech'. Make it less generic.")]),
        (A, "12:00", [text("Options:\n1. Product designer for fintech teams who ship weekly\n2. I make banking apps people finish onboarding in\n3. Fintech product design · research with receipts")]),
        (H, "12:02", [text("Second one, but shorter")]),
        (A, "12:02", [text("Banking apps people actually finish")]),
    ]),

    chat("Sourdough starter rescue", "2026-02-15", [
        (H, "21:00", [text("My starter smells like nail polish remover. Dead?")]),
        (A, "21:00", [text("Not dead, just hungry — that's acetone from starvation. Discard all but a spoonful, feed 1:5:5 twice a day for two days.")]),
    ]),

    chat("Workshop agenda: AI for design teams", "2026-07-07", [
        (H, "11:20", [text("A design agency booked me for a 3-hour workshop on AI in the design process. 12 people, mixed seniority. Draft an agenda.")]),
        (A, "11:21", [tool("create_file", path="/mnt/user-data/outputs/workshop-agenda.md", file_text=AGENDA, description="Agenda"),
                      tool("present_files", filepaths=["/mnt/user-data/outputs/workshop-agenda.md"]),
                      text("The agenda moves from their own process to hands-on work and ends with rules they write themselves — that's what survives after the workshop.")]),
        (H, "11:35", [text("Make a landing page for it too")]),
        (A, "11:36", [tool("artifacts", id="workshop-landing", command="create", type="text/html", title="Workshop landing page", content=LANDING.replace("Book a date", "Get in touch")),
                      tool("artifacts", id="workshop-landing", command="update", old_str="Get in touch", new_str="Book a date"),
                      text("Here's a one-screen page with the promise, three outcomes and one button.")]),
    ]),

    chat("Explain vector embeddings like I'm a designer", "2026-01-19", [
        (H, "20:10", [text("Explain embeddings to me like I'm a designer, no math")]),
        (A, "20:11", [text("Imagine every piece of text gets a spot on a huge moodboard. Things that mean similar things are pinned close together, even if they share no words: “rent payment failed” sits next to “couldn't pay my landlord”.\n\nAn embedding is just the coordinates of that spot. Search by meaning is: pin your question on the board and grab whatever is nearby.")]),
        (H, "20:15", [text("So that's how 'search my notes' tools work?")]),
        (A, "20:15", [text("Mostly, yes. They pin every chunk of your notes, then pin your question and return the neighbours. That's also why they sometimes return something related but wrong — near on the board isn't the same as correct.")]),
    ]),

    chat("Moving my notes into plain markdown files", "2026-09-02", [
        (H, "08:30", [text("I want my context to live in files, not in chat history. How do I set it up so any assistant can use it?")]),
        (A, "08:31", [text("Three parts: one folder for everything, one file that explains how you work, and git so it's never only on one laptop."),
                      tool("create_file", path="/mnt/user-data/outputs/CLAUDE.md", file_text=CLAUDE_MD, description="Starter context file"),
                      tool("present_files", filepaths=["/mnt/user-data/outputs/CLAUDE.md"]),
                      text("Put this at the root as CLAUDE.md (Claude Code reads it) and symlink it as AGENTS.md for other agents. Then a private GitHub repo and an hourly commit.")]),
        (H, "08:45", [text("How do I do the hourly commit on a Mac?")]),
        (A, "08:46", [text("A launchd job that runs `git add -A && git commit -m sync && git push` every hour. Keep secrets in a `.env` that's in `.gitignore`.")]),
    ]),

    chat("Knee pain after the long run", "2026-09-21", [
        (H, "17:00", [text("Outer knee hurts after the 17 km run, worse going downstairs. Race in 3 weeks.")]),
        (A, "17:01", [text("Outer knee, worse on stairs, after a long run — that pattern often points to the IT band, but I can't diagnose it. See a physio this week; three weeks is enough time if you act now.\n\nUntil then: no running through pain, swap the quality session for cycling, and don't stretch it aggressively.")]),
        (H, "17:05", [text("Should I skip the 18 km next weekend?")]),
        (A, "17:05", [text("Let the physio decide. If you do run, make it a flat route and stop at the first twinge — one missed long run costs far less than a forced DNS.")]),
    ]),

    chat("Competitor teardown: three onboarding flows", "2026-05-06", [
        (H, "13:00", [text("Northwind wants to see how Monzo, Revolut and N26 onboard. Compare them.")]),
        (A, "13:02", [tool("web_fetch", url="https://example.com/monzo-onboarding-walkthrough"),
                      result("web_fetch", "Walkthrough of Monzo sign-up: phone number, ID photo, selfie video, card design…"),
                      tool("web_fetch", url="https://example.com/revolut-onboarding-walkthrough"),
                      result("web_fetch", "Revolut sign-up steps…"),
                      tool("web_search", query="N26 video ident waiting time 2026"),
                      result("web_search", "Users report video identification queues of 5–20 minutes…"),
                      tool("create_file", path="/mnt/user-data/outputs/onboarding-teardown.md", file_text=TEARDOWN, description="Teardown table"),
                      tool("present_files", filepaths=["/mnt/user-data/outputs/onboarding-teardown.md"]),
                      text("The pattern worth stealing: show the product working before the hardest step. Note: step counts are from public walkthroughs, not my own sign-ups.")]),
    ]),

    chat("Books on decision making", "2026-02-02", [
        (H, "22:10", [text("5 books on decision making that aren't pop psychology")]),
        (A, "22:11", [text("1. *Sources of Power* — Gary Klein, how experts decide under pressure\n2. *Thinking in Bets* — Annie Duke, separating decisions from outcomes\n3. *The Decision Book* — compact models, good for workshops\n4. *Superforecasting* — Tetlock and Gardner\n5. *Noise* — Kahneman, Sibony and Sunstein")]),
    ]),

    chat("Q3 review: what worked", "2026-10-05", [
        (H, "09:00", [text("Help me review Q3. Projects: Northwind audit, 3 workshops, newsletter (8 issues, 1,200 → 1,650 subscribers), half marathon training. What do I keep, drop, change?")]),
        (A, "09:02", [think("Revenue concentrated in Northwind; workshops are repeatable and scalable; newsletter grew 37%; training competes for morning time."),
                      text("**Keep:** workshops — three sold from one landing page, and each produces material for the newsletter.\n\n**Change:** client concentration. Northwind was most of the quarter; one cancelled contract would hurt. Aim for two audit clients in Q4.\n\n**Drop:** nothing yet, but cap the newsletter at two hours per issue — growth came from topics, not polish.\n\n**Watch:** training took the mornings that used to be deep-work time. After the race, decide deliberately what gets them back.")]),
        (H, "09:20", [text("Turn that into 3 goals for Q4")]),
        (A, "09:20", [text("1. Sign a second audit client by Nov 30.\n2. Run two workshops and record one as a paid replay.\n3. Newsletter: 2,000 subscribers at ≤2 hours per issue.")]),
    ]),
]

# ---------- memory, projects, routines, artifacts ----------

PROJECTS = [
    {"uuid": uid("project", "newsletter"), "name": "Newsletter", "description": "Weekly issues and drafts",
     "prompt_template": "Edit for clarity, keep my voice: short sentences, first person, no hype words. Endings return to the opening image.",
     "docs": [{"uuid": uid("doc", "style"), "filename": "style_guide.md", "content": "# Style\n- First person, short sentences\n- One idea per issue\n- No 'game-changer', no 'unlock'\n", "created_at": "2026-02-01T10:00:00Z"},
              {"uuid": uid("doc", "issues"), "filename": "past_issues.md", "content": "# Past issues\n- #10 The one-folder rule\n- #11 Research with receipts\n- #12 Why I stopped using Notion\n", "created_at": "2026-09-14T10:00:00Z"}]},
    {"uuid": uid("project", "northwind"), "name": "Client: Northwind Bank", "description": "Design audit, spring 2026",
     "prompt_template": "Context: Northwind Bank, mobile app audit. Stakeholders: Priya (product), Tom (compliance). Always cite interview IDs.",
     "docs": [{"uuid": uid("doc", "brief"), "filename": "brief.md", "content": "# Brief\nAudit onboarding, cards and transfers. Deadline: May 30. Budget approved: €9,600.\n", "created_at": "2026-04-20T10:00:00Z"}]},
    {"uuid": uid("project", "running"), "name": "Running", "description": "", "prompt_template": "",
     "docs": [{"uuid": uid("doc", "pbs"), "filename": "personal_bests.md", "content": "# PBs\n- 5K 23:40\n- 10K 49:30\n- Half: first one in October\n", "created_at": "2026-08-01T10:00:00Z"}]},
    {"uuid": uid("project", "empty"), "name": "Untitled", "description": "", "prompt_template": "", "docs": []},
]
for p in PROJECTS:
    p.update({"is_private": True, "is_starter_project": False, "created_at": "2026-02-01T10:00:00Z",
              "updated_at": "2026-09-30T10:00:00Z", "creator": {"uuid": ACCOUNT, "full_name": "Sam"}})

MEMORY = {
    "account_uuid": ACCOUNT,
    "conversations_memory": "**Work context**\n\nSam is a freelance product designer working mostly with fintech and health clients. Runs a weekly newsletter (~1,650 subscribers) about design and AI, and teaches workshops on AI in the design process.\n\n**Personal context**\n\nTraining for the Lisbon half marathon on October 12, 2026, goal under 1:50. Bakes sourdough.\n\n**Top of mind**\n\nFinishing the Northwind Bank audit follow-ups and finding a second audit client for Q4.",
    "project_memories": {
        uid("project", "newsletter"): "Sam prefers short sentences and endings that return to the opening image. Issue #12 was about moving notes out of Notion.",
        uid("project", "northwind"): "Audit delivered on time. Key finding: large transfers fail silently. Priya wants a follow-up on card management.",
    },
    "memory_files": [
        {"path": "/profile.md", "content": "---\nname: profile\n---\n- [stated] Freelance product designer, fintech and health\n- [stated] Writes a weekly newsletter\n- [inferred] Prefers evidence-backed recommendations\n"},
        {"path": "/preferences.md", "content": "---\nname: preferences\n---\n- Short paragraphs, no bullet soup\n- Link sources, say what wasn't checked\n- Offer scope changes rather than discounts\n"},
        {"path": "/areas/running.md", "content": "---\nname: running\n---\n- Lisbon half, Oct 12 2026, goal sub-1:50\n- Outer knee pain in September, seeing a physio\n"},
        {"path": "/areas/workshops.md", "content": "---\nname: workshops\n---\n- 3-hour format, 12 people\n- Ends with team-written rules\n"},
        {"path": "/people/priya.md", "content": "---\nname: priya\n---\n- Product lead at Northwind Bank\n- Wants follow-up on card management\n"},
    ],
}

ROUTINES = {"claude_code_cloud": [
    {"routine_id": "trig_demo_morning", "name": "Morning brief", "enabled": True, "schedule": {"cron": "0 7 * * 1-5", "run_once_at": None},
     "prompt": "Read today's calendar and my notes folder. Give me: three priorities, meetings to prepare for, and anything I promised someone this week.",
     "created_at": "2026-06-01T08:00:00Z"},
    {"routine_id": "trig_demo_news", "name": "Newsletter ideas", "enabled": False, "schedule": {"cron": "0 9 * * 1", "run_once_at": None},
     "prompt": "Look through last week's chats and notes. Suggest three newsletter topics with one concrete example each.",
     "created_at": "2026-07-01T08:00:00Z"},
]}

PACE_V1 = """<!doctype html><html><head><meta charset="utf-8"><title>Pace calculator</title>
<style>body{font:16px system-ui;max-width:420px;margin:50px auto}input{font:inherit;width:80px}</style></head>
<body><h2>Pace calculator</h2><p>Target time (h:mm): <input id="t" value="1:50"></p><p id="o"></p>
<script>function c(){const [h,m]=t.value.split(':').map(Number);const s=(h*60+m)*60/21.0975;o.textContent='Pace: '+Math.floor(s/60)+':'+String(Math.round(s%60)).padStart(2,'0')+' /km'}t.oninput=c;c()</script></body></html>"""
PACE_V2 = PACE_V1.replace("<p id=\"o\"></p>", "<p id=\"o\"></p><p style=\"color:#888\">Half marathon, 21.1 km</p>")

ARTIFACTS = [
    ("pace-calculator", [("v1", "Pace calculator", "2026-08-03T08:00:00+00:00", PACE_V1),
                         ("v2", "Pace calculator", "2026-08-04T08:00:00+00:00", PACE_V2)],
     "Half marathon pace from a target time"),
    ("workshop-landing", [("v1", "AI for design teams", "2026-07-07T12:00:00+00:00", LANDING)],
     "One-screen landing page for the workshop"),
]


# ---------- writing the zips ----------

def write_zip(name, files):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for path, data in files.items():
            z.writestr(path, data if isinstance(data, (bytes, str)) else json.dumps(data, ensure_ascii=False, indent=1))
    with open(os.path.join(OUT, name), "wb") as f:
        f.write(buf.getvalue())


def main():
    os.makedirs(OUT, exist_ok=True)
    for f in os.listdir(OUT):
        if f.endswith(".zip") or f.endswith(".db"):
            os.remove(os.path.join(OUT, f))
    write_zip("conversations-000.zip", {"conversations.json": CHATS})
    write_zip("memories-000.zip", {f"memories/{ACCOUNT}.json": MEMORY})
    write_zip("projects-000.zip", {f"projects/{p['uuid']}.json": p for p in PROJECTS})
    write_zip("claude_code_cloud-000.zip", {"claude_code_cloud/routines.json": ROUTINES})
    frames = {}
    for aid, versions, descr in ARTIFACTS:
        art_id = uid("artifact", aid)
        frames[f"artifacts/{art_id}/artifact.json"] = {
            "id": art_id, "kind": "artifact", "visibility": "private",
            "versions": [{"id": v, "title": t, "description": descr, "created_at": c} for v, t, c, _ in reversed(versions)]}
        for v, _, _, body in versions:
            frames[f"artifacts/{art_id}/versions/{v}.html"] = body
    write_zip("frames-000.zip", frames)
    print(f"Demo export written to {OUT}: {len(CHATS)} chats")


if __name__ == "__main__":
    main()
