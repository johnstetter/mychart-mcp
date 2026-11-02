# FAQ

**Does it cost anything?**
No. mychart-mcp is free and open source (MIT), and Epic's developer registration is free. A
hosted AI assistant may have its own subscription.

**Will it work with my health system?**
If you use MyChart, your health system almost certainly runs Epic, and it's likely listed in
[Epic's endpoint directory](https://open.epic.com/MyApps/Endpoints). Your app reaches a
health system automatically once Epic delivers it, which can take up to a day. It's been
tested with Epic's sandbox and one real health system so far.

**Do I need an iPhone?**
No. Direct download from Epic needs only a computer. The iPhone's Health app is an optional
alternative source.

**Can it change my medical record?**
No. It asks MyChart for read-only access, and the part your assistant talks to can only read
the local file.

**Does the developer see my data?**
No. There's no mychart-mcp server or account; records go from your health system to your
computer. See [Privacy & security](privacy.md).

**Which AI assistant should I use?**
For privacy, one with a model running on your own computer, such as Hermes with Ollama. Hosted
assistants like Claude work well too, but the records used for each answer are sent to that
company. See [Connect an assistant](connect.md).

**Does it update on its own?**
Not yet. Run `mychart-mcp sync` when you want fresh records; it signs you in through the
browser each time. Unattended syncs are written but Epic doesn't allow them for this kind of
app yet.

**How long does a sync take?**
The first one can take several minutes for a large chart, because each visit note's text is a
separate download. Later syncs fetch only what changed and reuse note text that hasn't.

**Why don't my notes have text?**
Apple Health exports don't include note text, only each note's type, date and author. Direct
download from Epic includes it. If a note's text failed to download, the next sync tries again.

**How do I delete everything?**
Delete the database file ([where it lives](reference.md#where-the-database-lives)) and remove
mychart-mcp under MyChart → Linked Apps.

**Does it work on Windows or Linux?**
It's built and tested on macOS. The code has Windows and Linux paths, but they're untested,
and saved sign-ins are macOS-only.

**Something went wrong during sync.**
The [troubleshooting table](epic-setup.md#troubleshooting) covers the common messages.

**Is this medical advice?**
No. An AI summary of your chart can be wrong or incomplete. Check anything important with your
care team.
