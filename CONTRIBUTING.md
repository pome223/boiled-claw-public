# Contributing

boiled-claw is a **maintainer-led reference implementation** of closed-loop AI agent architecture.

## Status

Upstream is curated for design coherence. The repository is updated at the maintainer's pace to reflect architectural evolution — but there is no support or review commitment.

- Issues and pull requests are **not guaranteed a response**.
- The maintainer may incorporate ideas or fixes without notice.
- If you want to take the design in a different direction, fork freely under MIT License.

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
