"""Render captured command output as a terminal-style SVG (brand/*.svg in the README).

Usage: uvx --with rich python brand/make_terminal.py "<command>" <captured.txt> <out.svg> <title>
The text is the real output of the command; only colour is added.
"""
import re
import sys
from pathlib import Path

from rich.console import Console
from rich.terminal_theme import TerminalTheme
from rich.text import Text

THEME = TerminalTheme(
    (10, 13, 17), (233, 237, 241),
    [(10, 13, 17), (255, 107, 107), (126, 211, 140), (253, 165, 43), (56, 200, 232), (190, 150, 255), (56, 200, 232), (200, 207, 214)],
    [(90, 98, 108), (255, 140, 140), (160, 230, 170), (255, 200, 120), (130, 220, 240), (210, 180, 255), (130, 220, 240), (255, 255, 255)],
)


def style(line: str) -> Text:
    text = Text(line)
    for pattern, colour in ((r"^ok\b", "green"), (r"^pass\b", "green"), (r"^FAIL\b", "bold red"), (r"\[[A-Z][0-9/A-Z]*\]", "cyan"),
                            (r"^── .* ──$", "bright_black"), (r"tautological assertion", "yellow"),
                            (r"all proofs hold", "bold green"), (r"^[\w/.-]+\.py:\d+", "bright_white")):
        for match in re.finditer(pattern, line):
            text.stylize(colour, match.start(), match.end())
    return text


def main() -> None:
    command, captured, out, title = sys.argv[1:5]
    console = Console(record=True, width=int(sys.argv[5]) if len(sys.argv) > 5 else 100, force_terminal=True, color_system="truecolor")
    console.print(Text.assemble(("$ ", "bright_black"), (command, "bold bright_white")))
    for line in Path(captured).read_text(encoding="utf-8").rstrip("\n").splitlines():
        console.print(style(line))
    console.save_svg(out, title=title, theme=THEME)


if __name__ == "__main__":
    main()
