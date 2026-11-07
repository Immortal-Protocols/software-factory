# Contributing

This repository describes the Immortal Software Factory’s reusable building blocks, roles, and relationships. Keep it useful to readers who do not have access to the team’s operational records.

## Ownership and coordination

The Software Factory Maintainer maintains this repository. PM owns product direction; Chief coordinates execution. A documentation change records an agreed responsibility or relationship; it does not assign a new operational owner or grant authority.

Before starting an edit, check open pull requests and the current assignment with Chief. If another maintainer already owns the same change, coordinate the handoff or provide scoped support rather than competing edits.

## What belongs here

- Reusable role definitions and the relationships between them.
- Descriptions of shared building blocks and where context belongs.
- Documentation improvements that make those descriptions clear and consistent.

Keep live assignments, thread links, delivery receipts, and internal plans in the team’s operational records. Do not copy private project details, credentials, access information, infrastructure identifiers, or internal discussion into this public repository or its issues and pull requests. Use public-safe role descriptions and examples.

Changes to credentials, access, infrastructure, security settings, or legal terms are outside routine documentation maintenance.

## Making a change

1. Start a focused branch from the current `main` branch.
2. Update all affected descriptions together. In `README.md`, check the “At a glance” table, Mermaid role map, and thread responsibilities for consistency.
3. Keep the existing Markdown style and explain what changed and why in the pull request.
4. Include the checks performed and any remaining uncertainty. Separate the proposed documentation from claims of operational delivery.

## Checking documentation

The repository currently contains documentation only; it has no build command, automated test suite, or CI workflow.

Before submitting:

- Read the diff for accuracy, unintended changes, and private information.
- Check that relative links resolve to committed files or headings.
- Preview Markdown tables and the Mermaid diagram; verify that nodes, edges, labels, and styling agree with the prose.
- Run `git diff --check` in a local checkout to catch whitespace errors.

If a check cannot be run, say which one and why in the pull request. Do not report a proposed role, a created thread, or a source change as proof that operational work has started or shipped.
