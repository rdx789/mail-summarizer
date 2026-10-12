# Prompt for Summarizing Technical Content

You will be given one article, post or document that has already been checked as in scope. Summarize it (Step 2), unless it is clearly cut off (Step 1).

## Step 1: Completeness check

Reply `SKIPPED: incomplete` only if the text is clearly cut off: it stops mid-sentence, or it is a preview that ends with "continue reading", "subscribe to read" or similar. Footers, author bios, "more from" lists and related-post teasers after the article do not make it incomplete. Do not skip a post for any other reason, including its topic, format or lack of code.

A skip reply is exactly one line, `SKIPPED: incomplete`, with nothing after it.

## Step 2: Summary rules

1. **Only what the source says.** Every claim, name, number and code line must come from the text. Do not add background knowledge, opinions, caveats, trade-offs or conclusions the author did not state. If the author does not say it, leave it out.
2. **Numbers.** Copy numbers exactly as stated, with what they measure and their unit. Do not add up, round, estimate or convert them, and do not attach a number to a different quantity than the one the author gave. If a number is spelled out in words, copy it as the author gave it.
3. **Code.** Include a code snippet, function, script, command or configuration only if it appears in the source, copied in full inside a formatted code block, without omissions, shortcuts or placeholders such as `// rest of the code`. Never write your own example, sample config, command sequence or illustrative code, even to explain an idea. If the source describes a technique only in prose, describe it in prose.
4. **Structure and order.** Keep the author's own names, steps and order (for example the author's passes, phases or numbered items). Do not merge, rename, add or reorder them.
5. **Terminology.** Keep technical terms, library names, protocols and tool names in English exactly as the source writes them.
6. **Concise.** Focus on the technical core and omit introductions, promotion and filler. Do not pad a section: a short section is fine.
7. **Metrics.** Include performance numbers, percentages and benchmark results from the source explicitly.

## Required Summary Structure

Use all six sections in this order, with these exact names. If the source has nothing for a section (for example no code, no metrics), write `None in source` and nothing else.

- **TL;DR:** 1–2 short sentences with the bottom line.
- **Problem & Context:** 1–2 sentences on the challenge or situation the author addresses.
- **Solution & Architecture:** Bullet points on the approach, architecture, tools or practices, in the author's order.
- **Code & Implementation:** Only code and commands that appear in the source (see Step 2, rule 3).
- **Results & Trade-offs:** Only numbers, results, limitations and trade-offs the author states.
- **Key Takeaways:** The author's own conclusions or recommendations, as short points.
