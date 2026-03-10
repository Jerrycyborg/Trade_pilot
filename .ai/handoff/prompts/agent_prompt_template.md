# AAHP Agent Prompt Template

Read only the files listed in the task brief and the manifest.

Task:

{{TASK}}

Required context:

{{CONTEXT}}

Expected output:

{{OUTPUT}}

Constraints:

{{CONSTRAINTS}}

When finished:

1. update or create the relevant summary in `.ai/handoff/summaries/`
2. record any architectural decision in `.ai/handoff/decisions/`
3. regenerate checksums with `python3 tools/aahp.py generate-checksums`
