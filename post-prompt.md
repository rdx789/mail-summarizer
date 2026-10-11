# Prompt for Summarizing Technical Content

Please summarize the attached article/document/post/code based on the following principles:
1. **Complete Posts Only:** Summarize only full and complete posts that satisfy at least one of rules 2, 3, 4. If a post is incomplete (paywalled preview, truncated) or off-topic, do not summarize it; reply with a single line: `SKIPPED: incomplete` or `SKIPPED: off-topic`.
2. **Posts that summarization is needed are: ai related posts with reference to llm, rag, mcp, local models, ai workflow, improvements to ai-engineering, and all related topics that can relate to AI. 
3. **Posts that summarization is needed are: posts that relate to macbook, like tools or new applications that facilitate working with mac, running llm on mac or mac related posts.
4. **Posts that summarization is needed are: posts related to investment strategies in stocks and options.
5. **Conciseness and Accuracy:** The summary should be concise, focused strictly on the technical core, and omit generic introductions or fluff.
6. **Preserve Terminology:** Keep technical terms, library names, protocols, and technologies in English as originally referenced.
7. **Focus on Metrics and Data:** If performance metrics, percentage improvements, or benchmark results are present in the source, include them explicitly.
8. **Preserve Full Code Snippets:** If the source contains code snippets, functions, scripts, or configuration files, include them in full inside formatted code blocks without omissions, shortcuts, or placeholders like `// rest of the code`.

## Required Summary Structure

Use all six sections in this order. If the source has nothing for a section (e.g. no code, no metrics), write `None in source` instead of inventing content.

- **TL;DR:** 1–2 sentences delivering the bottom line and core value.
- **Problem & Context:** 1–2 sentences defining the technical challenge being addressed.
- **Solution & Architecture:** Bullet points highlighting the technical approach, architecture, tools, or best practices used.
- **Code & Implementation:** Full, unedited code snippets and technical implementation examples from the source.
- **Results & Trade-offs:** Measurable data, performance numbers, system limitations, or engineering trade-offs mentioned.
- **Key Takeaways:** Practical next steps or actionable conclusions to apply in production.
