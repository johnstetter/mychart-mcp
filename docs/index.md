# mychart-mcp

**Ask an AI assistant about your own health records, kept on your own computer.**

mychart-mcp copies your medical records from MyChart, the patient portal many US health
systems run on Epic, into a private file on your computer. An AI assistant you choose can
then answer plain-English questions about them, with the dates and records behind each
answer.

[Get started](getting-started.md){ .md-button .md-button--primary }
[How it works](how-it-works.md){ .md-button }

## What you can ask

- "When was I first diagnosed with seasonal allergies, and what's been prescribed for it since?"
- "How has my A1c changed over the last five years?"
- "What did the doctor write after my visit in March?"
- "Which medications did I start or stop this year?"
- "Summarize my last three lab results and draft a short message to my care team about them."

The assistant looks things up in your records and cites them. Writing the summary or message
is the assistant's job. Whether to send anything is always up to you.

## Your records stay yours

- **On your computer.** Records go straight from your health system to a file on your
  computer. There's no mychart-mcp account, server or cloud, and the developer never sees
  your data.
- **Read-only.** It asks MyChart for read-only access and can't change anything in your
  chart.
- **Your choice of assistant.** With an AI model that runs on your own computer, nothing
  leaves it. With a hosted assistant (Claude, ChatGPT, …), the records used for each answer
  are sent to that company. [Privacy & security](privacy.md) explains the difference.
- **Delete any time.** One command or deleting one file removes everything.

## Who it's for

People who are comfortable running commands in a terminal, or who have someone to help. Setup
takes about an hour: you register a free app with Epic (the company behind MyChart), wait up
to a day for Epic to approve it, then sign in to MyChart once to download your records.
After that, one command brings them up to date.

> **Status: early.** Direct download from Epic works with Epic's test system and one real
> health system. Others may send data it doesn't handle yet. It runs on macOS; Linux and
> Windows are untested.

> **Not medical advice.** An AI summary of your chart can be wrong or incomplete. Check
> anything important with your care team. mychart-mcp is not affiliated with Epic Systems
> (MyChart is Epic's trademark) or Apple.

## Free and open source

mychart-mcp is MIT-licensed. The code is on
[GitHub](https://github.com/johnstetter/mychart-mcp).
