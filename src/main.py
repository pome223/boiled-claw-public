"""
boiled-claw メインエントリーポイント
"""

import asyncio
import os
from dotenv import load_dotenv
from rich.console import Console
from rich.panel import Panel
from rich.prompt import Prompt

load_dotenv()

console = Console()


async def run_cli():
    """CLIモードでエージェントを実行する"""
    from google.adk.runners import Runner
    from google.adk.sessions import InMemorySessionService
    from google.genai import types
    from src.agents.root_agent import root_agent

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
        "Your personal AI agent powered by Google ADK\n"
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


def main():
    """メイン関数"""
    api_key = os.getenv("GOOGLE_API_KEY")
    if not api_key:
        console.print(
            "[red]Error: GOOGLE_API_KEY is not set.[/red]\n"
            "Copy .env.example to .env and set your API key."
        )
        return

    asyncio.run(run_cli())


if __name__ == "__main__":
    main()
