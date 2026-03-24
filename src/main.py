"""
boiled-claw メインエントリーポイント
CLI / Web 両対応
"""

import asyncio
import os
from dotenv import load_dotenv
from rich.console import Console
from rich.panel import Panel
from rich.prompt import Prompt
import click

load_dotenv()

console = Console()


def _require_api_key():
    """GOOGLE_API_KEY が設定されていなければエラー終了する。"""
    if not os.getenv("GOOGLE_API_KEY"):
        console.print(
            "[red]Error: GOOGLE_API_KEY is not set.[/red]\n"
            "Copy .env.example to .env and set your API key."
        )
        raise SystemExit(1)


@click.group(invoke_without_command=True)
@click.pass_context
def cli(ctx):
    """boiled-claw — Your personal AI agent powered by Gemini."""
    if ctx.invoked_subcommand is None:
        click.echo(ctx.get_help())


# ── chat (default interactive REPL) ──────────────────────────────


@cli.command()
def chat():
    """Start an interactive chat session (REPL)."""
    _require_api_key()
    asyncio.run(_run_cli())


def _setup_readline():
    """readline 履歴を設定する。"""
    import readline
    from pathlib import Path

    history_file = Path.home() / ".boiled_claw_history"
    try:
        readline.read_history_file(history_file)
    except FileNotFoundError:
        pass
    readline.set_history_length(1000)

    import atexit
    atexit.register(readline.write_history_file, str(history_file))


async def _run_cli():
    """CLIモードでエージェントを実行する"""
    from google.adk.runners import Runner
    from google.adk.sessions import InMemorySessionService
    from google.genai import types
    from src.agents.root_agent import root_agent
    from src.config.settings import get_settings
    from src.memory_lifecycle.adk_memory_service import get_promoted_memory_service
    from src.skills.runtime import ensure_skills_loaded
    from src.cli.repl import handle_slash_command

    _setup_readline()

    settings = get_settings()
    await ensure_skills_loaded()

    session_service = InMemorySessionService()
    memory_service = get_promoted_memory_service()
    runner = Runner(
        agent=root_agent,
        app_name="boiled-claw",
        session_service=session_service,
        memory_service=memory_service,
    )

    session = await session_service.create_session(
        app_name="boiled-claw",
        user_id="local_user",
    )

    console.print(Panel(
        "[bold cyan]boiled-claw[/bold cyan] 🦀\n"
        "Your personal AI agent powered by Gemini\n"
        f"[dim]Model: {settings.agent_model}[/dim]\n"
        "[dim]Type /help for commands, 'exit' to quit[/dim]",
        border_style="cyan"
    ))

    repl_ctx = dict(settings=settings, session=session, root_agent=root_agent)

    while True:
        try:
            user_input = Prompt.ask("\n[bold green]You[/bold green]")

            if user_input.lower() in ("exit", "quit", "q"):
                console.print("[dim]Goodbye! 👋[/dim]")
                break

            if not user_input.strip():
                continue

            # Slash commands
            if handle_slash_command(user_input, **repl_ctx):
                continue

            content = types.Content(
                role="user",
                parts=[types.Part(text=user_input)]
            )

            console.print("\n[bold blue]boiled-claw[/bold blue] 🦀", end=" ")

            async for event in runner.run_async(
                user_id="local_user",
                session_id=session.id,
                new_message=content,
            ):
                if event.is_final_response():
                    if event.content and event.content.parts:
                        for part in event.content.parts:
                            if part.text:
                                console.print(part.text)

        except KeyboardInterrupt:
            console.print("\n[dim]Interrupted. Type 'exit' to quit.[/dim]")
        except Exception as e:
            console.print(f"[red]Error: {e}[/red]")


# ── web (Gateway server) ────────────────────────────────────────


@cli.command()
@click.option("--host", default=None, help="Bind host (default: 127.0.0.1)")
@click.option("--port", default=None, type=int, help="Bind port (default: 18789)")
def web(host, port):
    """Start the WebSocket Gateway server."""
    _require_api_key()
    from src.gateway.server import create_gateway

    console.print(Panel(
        "[bold cyan]boiled-claw Gateway Server[/bold cyan] 🦀\n"
        "WebSocket endpoint: ws://{host}:{port}/ws/{{user_id}}\n"
        "[dim]Press Ctrl+C to stop[/dim]",
        border_style="cyan"
    ))

    gateway = create_gateway()
    gateway.run(host=host, port=port)


# ── channels (Telegram / Discord) ───────────────────────────────


@cli.command()
def channels():
    """Start multi-channel mode (Telegram, Discord)."""
    _require_api_key()
    asyncio.run(_run_channels())


async def _run_channels():
    """チャネルモードで実行する"""
    from src.config.settings import get_settings
    from src.channels.registry import get_channel_registry
    from src.channels.telegram import TelegramChannel
    from src.channels.discord_ch import DiscordChannel
    from google.adk.runners import Runner
    from google.adk.sessions import InMemorySessionService
    from src.agents.root_agent import root_agent
    from google.genai import types
    from src.skills.runtime import ensure_skills_loaded
    from src.memory_lifecycle.adk_memory_service import get_promoted_memory_service

    settings = get_settings()
    await ensure_skills_loaded()
    registry = get_channel_registry()

    session_service = InMemorySessionService()
    memory_service = get_promoted_memory_service()
    runner = Runner(
        agent=root_agent,
        app_name="boiled-claw",
        session_service=session_service,
        memory_service=memory_service,
    )

    async def handle_message(msg):
        session = await session_service.create_session(
            app_name="boiled-claw",
            user_id=msg.user_id,
        )
        content = types.Content(
            role="user",
            parts=[types.Part(text=msg.content)]
        )
        response_text = ""
        async for event in runner.run_async(
            user_id=msg.user_id,
            session_id=session.id,
            new_message=content,
        ):
            if event.is_final_response():
                if event.content and event.content.parts:
                    for part in event.content.parts:
                        if part.text:
                            response_text += part.text
        return response_text

    if settings.telegram_bot_token:
        try:
            telegram = TelegramChannel({"bot_token": settings.telegram_bot_token})
            telegram.set_message_handler(handle_message)
            registry.register_channel(telegram)
            console.print("[green]✓[/green] Telegram channel registered")
        except Exception as e:
            console.print(f"[yellow]![/yellow] Telegram channel failed: {e}")

    if settings.discord_bot_token:
        try:
            discord_ch = DiscordChannel({"bot_token": settings.discord_bot_token})
            discord_ch.set_message_handler(handle_message)
            registry.register_channel(discord_ch)
            console.print("[green]✓[/green] Discord channel registered")
        except Exception as e:
            console.print(f"[yellow]![/yellow] Discord channel failed: {e}")

    console.print(Panel(
        "[bold cyan]boiled-claw Channels[/bold cyan] 🦀\n"
        f"Active channels: {len(registry.list_channels())}\n"
        "[dim]Press Ctrl+C to stop[/dim]",
        border_style="cyan"
    ))

    await registry.start_all_channels()

    try:
        while True:
            await asyncio.sleep(1)
    except KeyboardInterrupt:
        console.print("\n[dim]Stopping channels...[/dim]")
        await registry.stop_all_channels()


# ── bridge group (host / desktop) ───────────────────────────────


@cli.group()
def bridge():
    """Manage bridge services (host, desktop)."""
    pass


@bridge.command("host")
@click.option("--host", default=None, help="Bind host (default: 127.0.0.1)")
@click.option("--port", default=None, type=int, help="Bind port (default: 8766)")
@click.option(
    "--transport", type=click.Choice(["sse", "stdio"]), default="sse",
    help="Transport mode (default: sse)",
)
def bridge_host(host, port, transport):
    """Start the Host Bridge MCP server."""
    from src.mcp_servers.host_bridge_server import create_server

    bind_host = host or "127.0.0.1"
    bind_port = port or 8766

    if transport == "stdio":
        console.print(Panel(
            "[bold cyan]boiled-claw Host Bridge[/bold cyan] 🦀\n"
            "Transport: stdio\n"
            "[dim]Use from a local MCP stdio client[/dim]",
            border_style="cyan",
        ))
        create_server(host="stdio").run(transport="stdio")
        return

    console.print(Panel(
        "[bold cyan]boiled-claw Host Bridge[/bold cyan] 🦀\n"
        f"SSE endpoint: http://{bind_host}:{bind_port}/sse\n"
        "[dim]Run this on the host OS, outside Docker[/dim]",
        border_style="cyan",
    ))
    create_server(host=bind_host, port=bind_port).run(transport="sse")


@bridge.command("desktop")
@click.option("--host", default=None, help="Bind host (default: 127.0.0.1)")
@click.option("--port", default=None, type=int, help="Bind port (default: 8767)")
@click.option(
    "--transport", type=click.Choice(["sse", "stdio"]), default="sse",
    help="Transport mode (default: sse)",
)
def bridge_desktop(host, port, transport):
    """Start the Desktop Bridge MCP server."""
    from src.mcp_servers.desktop_bridge_server import create_server

    bind_host = host or "127.0.0.1"
    bind_port = port or 8767

    if transport == "stdio":
        console.print(Panel(
            "[bold cyan]boiled-claw Desktop Bridge[/bold cyan] 🦀\n"
            "Transport: stdio\n"
            "[dim]Desktop client adapter. Control capabilities are still incomplete.[/dim]",
            border_style="cyan",
        ))
        create_server(host="stdio").run(transport="stdio")
        return

    console.print(Panel(
        "[bold cyan]boiled-claw Desktop Bridge[/bold cyan] 🦀\n"
        f"SSE endpoint: http://{bind_host}:{bind_port}/sse\n"
        "[dim]Run on the host OS. View-only desktop capabilities can be enabled first.[/dim]",
        border_style="cyan",
    ))
    create_server(host=bind_host, port=bind_port).run(transport="sse")


# ── entry point ──────────────────────────────────────────────────


def main():
    """メイン関数"""
    cli()


if __name__ == "__main__":
    main()
