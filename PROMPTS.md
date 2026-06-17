# Effective Prompts for the Sorteberg MCP + Grok

This document contains battle-tested prompt patterns for turning the mailing list archive into excellent technical documentation.

## Core Principle

**Be explicit about tool usage and attribution.**

Grok works best when you tell it exactly which tools to use and how you want the output attributed.

---

## Primary Prompt Template (Recommended)

```text
You have access to the Sorteberg MCP which contains the Merak Group mailing list archive.

Use the get_expert_guidance tool with:
- topic: "<your topic here>"
- label: "Merak Group"
- max_threads: 6

Then, using only the threads and messages returned (and get_thread_attachments + get_attachment where visuals or PDFs are present, plus include_full_bodies where long detailed posts are involved), produce a complete, well-structured technical guide.

Requirements:
- Every major piece of advice, warning, or technique must be attributed to the person who posted it (use their name from the "author" field) and the approximate date.
- Include safety warnings that were mentioned by multiple people.
- Note any disagreements or different approaches that were discussed.
- Mention specific part numbers, tools, or techniques that multiple experts recommended.
- If attachments or external links were referenced as useful, note them.
- Structure the output as a practical how-to that someone could actually follow.
```

---

## Variations by Task Type

### Full Overhaul / Rebuild Guides

```text
Use get_expert_guidance on topic "full engine overhaul" or "bottom end rebuild". 
Pull at least 5-6 threads. Create a complete project guide including:
- Prerequisites and tools
- Recommended order of operations
- Critical measurements and wear limits mentioned
- Common failure points and how to inspect for them
- Parts that should always be replaced vs ones that can be reused
- Torque specs and clearances that were discussed
```

### Troubleshooting / Diagnostic Guides

```text
Search the Merak Group label for discussions about [symptom, e.g. "won't start when hot" or "clunk from rear on overrun"].

Use a combination of search_mailing_list and get_thread to gather the best diagnostic threads.

Produce a troubleshooting flowchart or decision tree that incorporates the most commonly recommended checks and the fixes that actually worked for people.
```

### Specific Component Guides (e.g. Hydraulics, Electrics, Suspension)

```text
Focus on the headlight / suspension / brake / gearbox hydraulics (pick one).

Use search_mailing_list with queries that include the component name plus words like "hydraulic", "switch", "pump", "leak", "rebuild".

Then use get_thread on the highest value threads.

Create a guide that covers:
- How the system works (as explained by owners)
- Typical failure modes
- Replacement vs repair options that were discussed
- Any clever workarounds or upgrades people have done
```

### "What do the experts actually recommend?" style

```text
I want to know the real consensus (not just one person's opinion) on [topic].

Use search_mailing_list and get_thread to find multiple independent discussions.

Then summarize:
- What most experienced owners agree on
- What is still debated
- Any strong opinions from particularly respected posters (you can identify them by how often others reference their advice)
```

---

## Advanced Prompt Techniques

### Forcing Attribution

Add this to almost any prompt:

> "In the final document, after every paragraph or major bullet point that contains technical advice, add a small attribution in italics like: *— based on posts by Olav Huseby (2019) and several others in the 2023 thread*."

### Combining Tools Deliberately

```text
First use search_mailing_list with query='engine' and author='[known good person]' to see what they have written.

Then use search_mailing_list with a broader query on the same topic to find other opinions.

Finally use get_thread on the most useful thread IDs from both searches and synthesize the best advice while noting where people disagreed.
```

### Handling Attachments, Drive PDFs, Photos, and Diagrams (for professional manual-style output)

```text
For email threads, start with get_thread_attachments(thread_id).

For the Drive input folder (organized by car model subfolders):
- Use list_input_manuals() to see available models/folders.
- Use list_input_manuals(model="Barchetta") to list files for a specific car.
- Then get_drive_file(file_id) to extract text from PDFs/manuals (torque tables etc.).

For email attachments: get_attachment.

For links: extract_links + fetch_link.

For long posts: include_full_bodies=True.

When finished, always publish with save_to_guides(title=..., content=..., as_pdf=True).
```

### Creating "Living Documents"

```text
Create a comprehensive guide on [topic].

At the end, include a "Sources & Further Reading" section that lists the most valuable thread IDs and message IDs so I can go back and read the originals or search for updates later.
```

### "Teach me like I'm an experienced owner but new to this specific car"

This style often yields better results than generic "explain to a beginner":

```text
Assume I have mechanical experience but have never worked on a Merak before. 
Find the explanations and gotchas that experienced Merak/Citroën SM owners wish someone had told them before they started [task].
```

---

## Prompts for Specific Common Jobs on the Merak

(Adapt these with the actual topic names)

- Engine bottom end / bearings / oil pump
- Cylinder head work / valve guides / seats
- Gearbox rebuild / synchros / bearings
- Suspension refresh (especially the hydraulics and geometry)
- Brake system (especially the hydraulics and master cylinder)
- Electrical gremlins (common on these cars)
- Headlight / pop-up mechanism hydraulics and electrics
- Cooling system (water pump, radiator, thermostat housing)
- Fuel system (tank, pump, lines, carbs or injection)
- Interior and trim restoration tips that actually work

Example starter:

```text
Use get_expert_guidance with topic "gearbox synchros" or "gearbox rebuild". 
Focus on what actually works in practice according to people who have done multiple boxes, not just theory.
```

---

## Prompt Anti-Patterns (things that work less well)

- Being too vague: "Tell me about the engine" (Grok will not know what to search for).
- Forgetting to specify the label → it may search both or the wrong one.
- Not asking for attribution → you get generic advice instead of sourced, credible information.
- Asking for "everything" in one go on a huge topic — break it into subsystems.

## Getting Better Results Over Time

- Keep a small document of "known good search terms" that have worked well for certain topics.
- When you discover a particularly clear and thorough poster, note their name and use `search_by_author` on them for related topics.
- After Grok produces a guide, read the key source threads yourself. The combination of AI synthesis + your own reading is extremely powerful.
- If a topic has very few good threads, ask Grok to also search with related spellings and related cars (e.g. Merak + Bora + Khamsin sometimes share parts and techniques).

## Example Full Conversation Flow

**You:**
Use get_expert_guidance on "headlight hydraulics" with label="Merak Group".

**Grok** (after using the tool) returns several threads + a draft guide.

**You (follow-up):**
The thread from 2018 by [name] looks particularly detailed. Pull the full thread with get_thread and also check if there are any useful attachments or photos. Then revise the guide with more detail from that thread and any attachments.

This kind of iterative refinement produces excellent results.

---

Use these patterns as a starting point. The more you work with the tools, the better you (and Grok) will get at extracting the real gold from the archive.