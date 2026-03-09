### CORE PLAN REQUEST RULES (never break them)
- For any [plan] request I make, follow this exact flow. Never write code first. 

Before creating any plan:
1. Run listDirectory on the project root.
2. Batch-read **every** file in the project.
3. In your internal thinking, write one dedicated paragraph titled “Full Codebase Analysis” that answers:
   - How does the user proposal / change affect every file?
   - What ripple effects or breaking changes happen across the whole system?
   - How does the overall architecture change?
   Be bold: if the feature goal makes any file obsolete, redundant, or better replaced, explicitly propose full rewrites or deletions. The feature is the top priority - never preserve legacy code just to minimize changes. The whole project must remain coherent, but legacy files are not sacred.

PLAN WORKFLOW: (iterative)
Output ONLY the new file "plan-[feature-X]-vN.html".
The "N" means an iteration number of the plan, a plan may need a refinement and new file N + 1 must be generated.

The HTML must be dark-themed and clean. Include these exact sections:
- “Full Codebase Re-analysis” (summary from your thinking)
- “System-wide File Impact” (table or list showing for every file: keep unchanged / modify / fully rewrite / delete / new file — be explicit and bold)
- Numbered plan items (max 12–15 total)
- Every item must have: Approved / Rejected / Pending radio buttons + "Comment" textarea + previous-feedback quote (on vN+)
- Big **Save & Export JSON** button that downloads exactly "plan-[feature-X]-vN.json`

### PHASE 2 — ITERATIONS
1. Do the full Full Codebase Re-analysis thinking (bold style).
2. Read my latest JSON comments.
3. Output `plan-[feature]-vN.html` (increment version).
4. Carry forward approved items, apply my comments exactly, simplify when requested.
5. Update the System-wide File Impact section with the latest bold view.
6. Keep the same Save JSON button.

### PHASE 3 — IMPLEMENTATION (only on final trigger)
When I say "final approved" or "plan approved and start coding":
- First show a short "File fate" summary (create / modify / rewrite / delete list for the whole codebase — be bold).
- Then generate code changes one file at a time.
- Never generate code before this trigger.