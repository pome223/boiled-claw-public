# Contributing

boiled-claw is published as a **reference architecture** for closed-loop AI agent systems.

## Status

This project is **not actively maintained**. It is shared so that others can study, fork, and adapt the design for their own needs.

- Issues and pull requests are **not monitored**.
- There is no commitment to review, merge, or respond to contributions.
- If you find a bug or want a feature, fork the repository and make it your own.

## Using this project

You are free to:

- Fork and modify under the MIT License
- Use the architecture and design patterns in your own projects
- Reference this project in articles, talks, or other work

## Design principles

If you fork this project, the following principles guided its design:

1. **Verify, don't trust** — Every agent execution goes through a verification step. Unverified outputs are not final.
2. **Separate host from agent** — The agent runs in a container; host OS capabilities are accessed through explicit bridges (MCP-based).
3. **Curate memory, don't hoard** — Raw session data is not memory. Only validated, deduplicated facts are promoted to long-term storage.
4. **Approve before acting** — Dangerous tool calls require explicit human approval through the gateway protocol, not silent execution.
5. **Plan before executing** — Every non-trivial task goes through planning and policy evaluation before execution begins.
