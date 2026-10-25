# dev-tools

Small development and test tools for hardware / product work, kept independent
of any company or project. Each tool lives in its own folder with its own
README.

| Tool | What it does |
|---|---|
| [fit-lab](fit-lab/README.md) | Local web app to log CAD design values vs. measured values of printed parts per lot and clearance variant, with annotated pictures and a knowledge base over lots (voice entry included). |
| [ad-bench](ad-bench/README.md) | Digilent Analog Discovery bench helper over the WaveForms SDK: monitor / record / impedance from the command line, a browser scope with an HTTP API (`adscope`), guided probing on a board picture, and voice prompts for the person holding the probes (`guide.py`). |
| [kicad-review](kicad-review/README.md) | Local page to review a KiCad 10 PCB with a coding agent: review items marked on the board in KiCad colours (per-layer SVG via kicad-cli), severity / owner / status per item, a comment thread with the agent, and questions asked by clicking the board. |
| [talkpage](talkpage/README.md) | Base for local pages the user and a coding agent talk on: items declared in HTML (`data-talk`) or JSON get status buttons (OK / NG …) and a comment thread kept in a JSON beside the page; a wait command run in the background wakes the agent when the user acts, and its replies appear in place. |

## Common requirements

- Python 3.9+ (standard library unless a tool's README says otherwise).
- A Chromium browser (Chrome / Edge) for the web pages; voice features use the
  browser's Web Speech API.
- Third-party software (e.g. Digilent WaveForms, KiCad) is installed
  separately; no third-party binaries are kept here.

## Project data stays outside this repo

The tools hold no project data. Keep campaigns, board maps, procedures and
measurement results in the workspace of the project that uses them, and pass
that folder to the tool (`--root` / `--data` or the environment variable named
in each README). Only neutral samples are kept here.

## For other people

These were written for my own work and are mostly driven by an AI agent
(Claude Code / Codex), so the READMEs are short. Ask your agent to read a
tool's README and run it for you; that is the intended way to use them.
Issues and requests are not tracked. License: MIT (see `LICENSE`).

## Location on a PC

Agents find the clone through `~/.agent-core/tools.conf`:

```
dev_tools=<path to this clone>
```

```bash
git clone https://github.com/yus988/dev-tools.git <path>
```
