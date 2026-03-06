"""
boiled-claw メインエントリーポイント
CLI / Web 両対応
"""

import asyncio
import os
import sys
from typing import Optional
from dotenv import load_dotenv
from rich.console import Console
from rich.panel import Panel
from rich.prompt import Prompt
import argparse

load_dotenv()

console = Console()


async def run_cli():
    """CLIモードでエージェントを実行する"""
    from google.adk.runners import Runner
    from google.adk.sessions import InMemorySessionService
    from google.genai import types
    from src.agents.root_agent import root_agent
    from src.config.settings import get_settings
    from src.skills.runtime import ensure_skills_loaded

    settings = get_settings()
    await ensure_skills_loaded()

    session_service = InMemorySessionService()
    runner = Runner(
        agent=root_agent,
        app_name="boiled-claw",
        session_service=session_service,
    )

    session = await session_service.create_session(
        app_name="boiled-claw",
        user_id="local_user",
    )

    console.print(Panel(
        "[bold cyan]boiled-claw[/bold cyan] 🦀\n"
        "Your personal AI agent powered by Gemini 3.0 Flash\n"
        f"[dim]Model: {settings.agent_model}[/dim]\n"
        "[dim]Type 'exit' or 'quit' to stop[/dim]",
        border_style="cyan"
    ))

    while True:
        try:
            user_input = Prompt.ask("\n[bold green]You[/bold green]")

            if user_input.lower() in ("exit", "quit", "q"):
                console.print("[dim]Goodbye! 👋[/dim]")
                break

            if not user_input.strip():
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


def run_web(host: Optional[str] = None, port: Optional[int] = None):
    """Webサーバーモードで実行する"""
    from src.gateway.server import create_gateway

    console.print(Panel(
        "[bold cyan]boiled-claw Gateway Server[/bold cyan] 🦀\n"
        "WebSocket endpoint: ws://{host}:{port}/ws/{{user_id}}\n"
        "[dim]Press Ctrl+C to stop[/dim]",
        border_style="cyan"
    ))

    gateway = create_gateway()
    gateway.run(host=host, port=port)


async def run_channels():
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

    settings = get_settings()
    await ensure_skills_loaded()
    registry = get_channel_registry()

    # セッションとランナー
    session_service = InMemorySessionService()
    runner = Runner(
        agent=root_agent,
        app_name="boiled-claw",
        session_service=session_service,
    )

    # メッセージハンドラー
    async def handle_message(msg):
        """チャネルメッセージ処理"""
        # セッション取得または作成
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

    # Telegram設定
    if settings.telegram_bot_token:
        try:
            telegram = TelegramChannel({"bot_token": settings.telegram_bot_token})
            telegram.set_message_handler(handle_message)
            registry.register_channel(telegram)
            console.print("[green]✓[/green] Telegram channel registered")
        except Exception as e:
            console.print(f"[yellow]![/yellow] Telegram channel failed: {e}")

    # Discord設定
    if settings.discord_bot_token:
        try:
            discord_ch = DiscordChannel({"bot_token": settings.discord_bot_token})
            discord_ch.set_message_handler(handle_message)
            registry.register_channel(discord_ch)
            console.print("[green]✓[/green] Discord channel registered")
        except Exception as e:
            console.print(f"[yellow]![/yellow] Discord channel failed: {e}")

    # 全チャネル起動
    console.print(Panel(
        "[bold cyan]boiled-claw Channels[/bold cyan] 🦀\n"
        f"Active channels: {len(registry.list_channels())}\n"
        "[dim]Press Ctrl+C to stop[/dim]",
        border_style="cyan"
    ))

    await registry.start_all_channels()

    # 無限ループ (Ctrl+Cまで)
    try:
        while True:
            await asyncio.sleep(1)
    except KeyboardInterrupt:
        console.print("\n[dim]Stopping channels...[/dim]")
        await registry.stop_all_channels()


def main():
    """メイン関数"""
    parser = argparse.ArgumentParser(description="boiled-claw - Your personal AI agent")
    parser.add_argument(
        "mode",
        nargs="?",
        default="cli",
        choices=["cli", "web", "channels"],
        help="Run mode: cli (default), web, or channels"
    )
    parser.add_argument("--host", type=str, help="Web server host (default: 127.0.0.1)")
    parser.add_argument("--port", type=int, help="Web server port (default: 18789)")

    args = parser.parse_args()

    # API key check
    api_key = os.getenv("GOOGLE_API_KEY")
    if not api_key:
        console.print(
            "[red]Error: GOOGLE_API_KEY is not set.[/red]\n"
            "Copy .env.example to .env and set your API key."
        )
        return

    # モード別実行
    if args.mode == "cli":
        asyncio.run(run_cli())
    elif args.mode == "web":
        run_web(host=args.host, port=args.port)
    elif args.mode == "channels":
        asyncio.run(run_channels())


if __name__ == "__main__":
    main()
