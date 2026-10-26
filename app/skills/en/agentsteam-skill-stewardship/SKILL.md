---
name: agentsteam-skill-stewardship
description: Proactively capture employee skills when a reusable method, fix or debugging technique is verified, a skill proves wrong or incomplete, or the user asks to learn a method.
---

# Experience capture

Actively put evidence-supported experience from the current work into skills so the next task of the same class can use an effective method directly. A skill holds the procedure, concrete tools and commands, decision points, verification, and recurring pitfalls for a class of work.

Identify the signal, check evidence, read skills, write, and verify within the current agent loop, then resume the original task or deliver its result. Write captured content and outcome reports in the user's language, regardless of the language of this skill package.

## Experience signals

When any of these signals appears, assess and handle the corresponding experience immediately:

- A non-trivial technique, fix, workaround, debugging path, or tool-usage pattern has worked and would help a future task of the same class.
- Tests, tool results, or actual code show that a skill used in this task is wrong, missing a step, incomplete in its conditions, or outdated.
- Verification improves an existing method, for example by finding a shorter reliable debugging sequence or establishing a necessary precondition for a step.
- The user explicitly asks to learn and save a method from the recent work, code, material, or specified sources.

Act on a valid signal rather than merely promising to do better next time. Continue the original task when there is no new experience; evidence determines the number of updates.

## Target library

Maintain **class-level skills**: the main file holds the shared procedure and decisions for that class, with a small set of supporting files for topical depth. An incident earns its place by improving how that class of task is performed.

Choose the earliest suitable location in this order:

1. **Update a currently used personal skill.** It was involved in this task; if it covers the learning, reread its maintenance source and complete or correct the affected step directly.
2. **Update an existing class-level skill.** If no currently used skill fits, find another personal skill covering the class and improve its steps, decision points, or triggers.
3. **Add a topical support file.** If the class exists but a branch needs depth, extend an existing topic first, then consider a new reference, template, or script and add a pointer in the main file.
4. **Create a new class-level skill.** Only after confirming that existing skills do not cover it, name the skill for the class of task; the name should still make sense on the next similar task.

## Procedure

1. **Locate evidence and the maintenance source.** Form a candidate from actual operations, code inspection, or verification results in the current collaboration, and read [Eligibility and boundaries](references/eligibility.md). Use the personal skill maintenance directory supplied by the identity context or explicitly designated by the user.
2. **State one lesson.** Identify the conditions, effective action, mechanism, and verification. Stop that candidate if evidence is insufficient; handle multiple independent lessons in sequence, assessing and deduplicating each separately.
3. **Read and decide.** Select a location using the priority above and follow [Read, decide, and write](references/read-write-workflow.md): inspect the complete personal skill inventory, reread relevant main files and supporting files to be changed, then stop, revise, or add.
4. **Author the smallest useful update.** Apply [Skill authoring](references/authoring.md) for procedures, pitfalls, and file organization. Correct the original rule, preserve unrelated content, and make one meaningful update for this candidate.
5. **Persist and verify.** Use file tools actually available in the current Harness to write the personal maintenance source, reread the files on disk, and check structure, references, and changes; run appropriate verification for scripts. Match reporting to the evidence and verification scope.
6. **Report the actual outcome.** Briefly include the skill name, maintenance-source path, improved method, and verification in this turn's result. If a candidate was formed but not persisted, report "Not saved" and the reason; no separate capture report is needed when nothing adds value.

When the user asks to learn from current experience or specified material, first inventory all sources and requirements with [Explicit learning](references/learn.md), then return to this procedure.

## Reuse

Native skill indexes provide discovery; the body and required references provide instructions for execution. An index or history excerpt is insufficient for revision; reread bodies missing after context compaction.

The personal maintenance source and the runtime's frozen snapshot serve different purposes: write the source and let new sessions load the new version; read the source directly when the current session needs new experience, without assuming its index has refreshed.
