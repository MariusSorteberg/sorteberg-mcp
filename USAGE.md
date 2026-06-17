# Using the KnowledgeForge with Grok

This document contains practical guidance and prompt patterns for getting the most value out of your expert mailing list archives through the MCP.

## Connecting (Grok Web Client)

1. In Grok, open the **Custom Connectors** / MCP settings.
2. Add a new connector:
   - **Name**: `KnowledgeForge MCP`
   - **URL**: `https://your-knowledgeforge-service.a.run.app/mcp`
3. Complete the OAuth flow (the server supports the "none / PKCE only" public client flow). Grok will receive the agent bearer token automatically.
4. (Optional) You can also connect using a raw Bearer token if your client supports it directly.

Once connected you should see tools including:
- `search_mailing_list`
- `get_expert_guidance`
- `get_message` (with `include_full_body=True` support)
- `get_thread` (with `include_full_bodies=True` support)
- `get_attachment`, `get_thread_attachments`
- `list_input_manuals(model=None)` (new — Drive input, car-model subfolders)
- `save_to_guides` (new — publish to output folder)
- `fetch_link` (now extracts PDFs too)
- etc.

## Recommended Workflow for Generating How-Tos

### Best Starting Tool
Use **`get_expert_guidance`** first. It is specifically designed for this use case.

**Example prompt:**
```
Use get_expert_guidance on your expert mailing list label with the topic "overhaul the engine" (max_threads=6). 
Then use the returned threads to create a complete, well-structured step-by-step guide for someone doing a full engine overhaul. 
Include:
- Safety warnings mentioned by multiple people
- Tools and special parts that are commonly needed
- Common mistakes and how to avoid them
- Sequence of operations with approximate time estimates where available
- Attribution: after every major piece of advice, note who said it and roughly when (or link to the thread if possible)
```

### When You Need More Control
Break it down manually:

1. **Discovery**
   ```
   Use search_mailing_list with label="Expert Mailing List", query='engine overhaul OR "engine rebuild" OR "bottom end"', max_results=12, author="" (or a known expert).
   ```

2. **Deep context**
   ```
   Take the most promising thread_ids and call get_thread on them so I have the full conversations.
   ```

3. **Supporting materials**
   ```
   For richly illustrated guides, first call get_thread_attachments on the best thread_id(s).
   Then selectively call get_attachment on the most useful image/PDF items.
   Use extract_links + fetch_link (PDF-aware) for any external references.
   ```

4. **Synthesis**
   Ask Grok to produce the final document with strict attribution rules.

## Prompt Patterns That Work Well

### Attribution Discipline (very important)
Always tell Grok:

> "Every piece of advice must be attributed to the person who posted it. Use their name (or email if only email is available) and the approximate date or message ID."

### Good Query Techniques
- Use quotes for exact phrases: `"headlight hydraulics"`
- Combine with mailing list conventions: `subject:gearbox OR "gear box"`
- Look for experience level: `"I did this on my car"` or `"worked for me"`
- Negative filters: `-problem` when you want solutions rather than complaints.

### Structuring the Final Output
Ask for a specific format:

```
Create the guide in this structure:
1. Overview & Difficulty
2. Tools & Parts Required (with links or part numbers where mentioned)
3. Safety Warnings (consensus from multiple experts)
4. Step-by-Step Procedure
5. Common Pitfalls & How to Avoid Them
6. References & Further Reading (with author + date where possible)
7. Appendix: Variations mentioned on the list (e.g. different engines or years)
```

## Example Real-World Prompts

- "Using only information from the expert mailing list label, produce a complete guide to replacing the hydraulic switch, including photo references, torque specs if mentioned, and who recommended each method."
- "I want to do a full suspension refresh. Search the list for the best order of operations, recommended components, and any gotchas. Build a project plan with estimated time per step."
- "Create a troubleshooting flowchart for when the car won't start, based on what experienced owners have posted over the years. Include the diagnostic steps they recommend and what fixed it for them."

## Tips for Better Results

- Start broad with `get_expert_guidance`, then drill down with `get_thread` on the best hits.
- When an expert is repeatedly mentioned positively, use `search_by_author` on them for a topic.
- For visual procedures, use `get_thread_attachments(thread_id)` on promising threads to efficiently harvest all photos, diagrams and PDF references in one call. Follow up with `get_attachment` on the best ones (images return base64 suitable for vision).
- Keep a running "sources" section. Ask Grok to maintain a list of message IDs or thread IDs used so you can go back and read the originals if needed.
- If results feel thin, try slight variations of the topic wording — mailing list language is not always consistent ("engine rebuild" vs "bottom end overhaul" vs "full rebuild").

## Limitations & Workarounds

- Search is still Gmail keyword-based (no semantic search yet). Use multiple related queries.
- Very long threads can be truncated. Use `get_thread` with a reasonable `max_messages` and ask Grok to summarize the key posts.
- Images in attachments currently return metadata + base64. You can ask Grok (if it has vision) to describe them, or we can add a dedicated description tool later.
- External links sometimes die. `fetch_link` will tell you if something is no longer available.

## Managing the Connection

- Re-auth the Gmail owner anytime via the web UI at the service URL + `/oauth/google/start`.
- If Grok stops seeing the tools, remove and re-add the Custom Connector (sometimes the OAuth token needs refreshing).
- You can run the server locally for development while keeping the same bearer token.

## Pro Tips

- Keep a "canonical how-tos" label or folder and have Grok occasionally post summaries back into Gmail (or export them as Markdown files you can attach to threads).
- Use the `search_by_author` tool when you discover particularly trusted voices on the list — treat their posts with higher weight.
- For big jobs, ask Grok to produce both a "quick reference checklist" version and a "full narrative with rationale and warnings" version.

This setup turns 10+ years of mailing list history into something you can actually use instead of just archiving it. The more you use the tools with good prompting discipline (especially around attribution), the better the results become.