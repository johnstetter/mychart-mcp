# mychart-mcp: Terms of Use and Privacy

Effective October 30, 2026

mychart-mcp is free, open-source software (MIT License) that you run on your own computer. It
lets you download your own health records from MyChart and ask questions about them with an
AI assistant you choose. It is published by John Stetter (stetter.io) and is not affiliated
with Epic Systems, Apple, or your health care provider.

## What happens to your data

- **It stays on your computer.** When you sign in with MyChart, your records are downloaded
  directly from your health system to a database file on your own device. There is no
  mychart-mcp server, account, or cloud service.
- **The developer never receives it.** No health data, sign-in credentials, usage analytics,
  or other information is sent to the developer or anyone acting for them.
- **It is never sold, shared, or used for advertising or marketing,** and it is not used to
  learn about your family or anyone other than you.
- **Read-only access.** The app requests read-only permission. It cannot change anything in
  your medical record.
- **Sign-in is not stored.** The access token from MyChart is held in memory only while a sync
  runs and is discarded afterwards.

## AI assistants you connect

mychart-mcp includes a server that lets an AI assistant answer questions using your records.
Which assistant to connect is your choice:

- With a **model running on your own computer**, your records stay on your device.
- With a **hosted assistant** (for example Claude or ChatGPT), the records used to answer
  each question are sent to that provider and handled under its own terms and privacy
  policy, not these. HIPAA generally does not apply to data you hold yourself or share with
  such a provider.

## Keeping and deleting your data

Your data is kept until you delete it. Run `mychart-mcp forget` to remove imported records,
or delete the database file to remove everything. To stop future access, remove mychart-mcp
under Linked Apps (or the equivalent) in MyChart.

## Your responsibilities

You are responsible for securing the computer where your records are stored. Full-disk
encryption is recommended.

## No medical advice, no warranty

mychart-mcp does not provide medical advice. Summaries produced by an AI assistant can be
wrong or incomplete; confirm anything important with your care team. The software is
provided "as is", without warranty of any kind, as described in the [LICENSE](LICENSE).

## Changes and contact

Changes to these terms are published in this file, with its history visible in the
repository. Questions: open an issue at https://github.com/johnstetter/mychart-mcp/issues.
