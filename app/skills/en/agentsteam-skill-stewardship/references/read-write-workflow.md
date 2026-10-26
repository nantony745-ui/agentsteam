# Read, decide, and write

## 1. State the candidate

Establish from current evidence: the situation, the effective method or step needing correction, the mechanism, and the result supporting it. Apply [Eligibility and boundaries](eligibility.md) before choosing a target. Process independent candidates sequentially, completing or stopping one before the next.

## 2. Inspect the complete personal skill inventory

List all `<skill-name>/SKILL.md` files in the personal maintenance source and read their names and descriptions, using the skills actually loaded during this turn to identify relevant classes. Resolve failed or incomplete discovery first; an index subset cannot establish that no existing skill fits.

Choose using the main skill's four-level order: a currently used personal skill, an existing class-level skill, a topic file within that class, then a new class-level skill. Before creating a personal skill, check names of global and project skills visible to the current Harness to avoid exclusion due to a name collision in a new session.

## 3. Reread before this revision

Read the entire target `SKILL.md` from the maintenance source. Reread each existing supporting file to be changed as well; earlier conversation quotes, search excerpts, and runtime snapshots do not replace this fresh read of the maintenance source.

Search the target skill and its `references/` for equivalent or closely related rules. Search locates content; decisions and edits depend on the complete relevant text. Stop the candidate if content is missing or the target cannot be read.

## 4. Make one explicit decision

| Inspection result | Decision |
| --- | --- |
| The same rule already exists in full, with no new condition or improvement | Stop without writing |
| The same rule is wrong, outdated, missing conditions, or missing a key step | Revise the original sentence or affected steps |
| The class is covered and the addition is an independent, supported method | Extend the applicable steps; extend or add a topic file when the branch needs depth |
| Existing classes genuinely do not cover it and lasting value is clear | Create a class-level skill |
| Evidence, applicability, or the target remains uncertain | Stop, or clarify an ambiguity that changes the result with the user |

Place revisions beside the step the lesson affects; add a relative pointer and loading condition in the main file when creating supporting material. Make one meaningful update per candidate rather than repeatedly revising for cosmetic wording.

## 5. Write with current tools

Use file-reading, search, precise-patch, or write tools according to the actual tool list; call only available interfaces. Check that a new target does not exist; base revisions on the text just read and preserve unrelated content.

If a precise patch is rejected because content changed, reread the actual file, compare the candidate again, and retry at most once; stop and report a second conflict. Operate within the tool's actual concurrency capabilities; rereading alone does not provide an atomic concurrency guarantee.

If permissions prevent writing or a file is not writable, preserve the original and report why it was not saved, following the existing permission flow. Begin save verification only after the write actually succeeds.

## 6. Reread and verify

Reread all files actually changed and check that:

- The current discovery mechanism can read the YAML, `name` matches the skill directory, and `description` routes to the right class.
- The new rule is present in its intended location, unrelated steps remain, and no duplicate or contradictory version of the same rule exists.
- Relative references and their loading conditions in the main file match the actual supporting files.
- Commands, parameters, interfaces, and configuration keys come from real sources or actual verification, with prerequisites and platform limits stated.
- New or changed scripts receive execution checks appropriate to the change, with uncovered areas explicitly identified.

Valid structure establishes readability, not method correctness. Correct this update if verification fails; if it cannot be corrected, report the actual file state and failed checks rather than claiming capture is complete.

## 7. Deliver and reuse

Briefly include "Added/revised + skill name + maintenance-source path + concrete method + actual verification" in this turn's result. State source evidence and unexecuted scope for sourced methods; if a candidate was not persisted, report "Not saved" and the reason.

On future use, discover the skill in the index and read its body and required references. Read the personal maintenance source when the current session needs freshly saved experience; new sessions load new versions through the existing setup. Reread bodies lost during context compaction.
