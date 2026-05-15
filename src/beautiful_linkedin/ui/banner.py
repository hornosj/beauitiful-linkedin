from __future__ import annotations

from rich.console import Console
from rich.panel import Panel
from rich.text import Text


def print_banner(console: Console) -> None:
    art = r"""
 ____                   _   _  __       _ 
| __ )  ___  __ _ _   _| |_(_)/ _|_   _| |
|  _ \ / _ \/ _` | | | | __| | |_| | | | |
| |_) |  __/ (_| | |_| | |_| |  _| |_| | |
|____/ \___|\__,_|\__,_|\__|_|_|  \__,_|_|
        _      _       _            _ ___       
       | |    (_)_ __ | | _____  __| |_ _|_ __  
       | |    | | '_ \| |/ / _ \/ _` || || '_ \ 
       | |____| | | | |   <  __/ (_| || || | | |
       |______|_|_| |_|_|\_\___|\__,_|___|_| |_|
""".strip("\n")
    lines = art.splitlines()
    palette = ["#5EEAD4", "#38BDF8", "#0A66C2", "#2563EB", "#7DD3FC"]
    title = Text()
    for index, line in enumerate(lines):
        title.append(line, style=f"bold {palette[index % len(palette)]}")
        if index < len(lines) - 1:
            title.append("\n")
    subtitle = Text(
        "API PROVIDERS  |  SERP-ONLY SEM COOKIES  |  LINKEDIN COM COOKIE ATUAL",
        style="white",
    )
    glow = Text("scan -> normalize -> dedupe -> export csv/xlsx", style="dim cyan")
    console.print(
        Panel.fit(
            Text.assemble(title, "\n", subtitle, "\n", glow),
            border_style="#38BDF8",
            padding=(1, 2),
        )
    )
