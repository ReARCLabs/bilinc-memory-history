# Bilinc Memory History

Version history and undo for the memory files your AI agents write.

Agents now rewrite their own memory as they work, and several of them rewrite it in the background. None of them keep a lasting history of it. Bilinc records every version, shows you what changed, and puts an old version back with one command.

| Agent | Memory it tracks | Install |
|---|---|---|
| [Claude Code](#claude-code) | Auto memory (`~/.claude/projects/*/memory/`) and your global `~/.claude/CLAUDE.md` | `claude plugin marketplace add ReARCLabs/bilinc-memory-history` |
| [Hermes Agent](#hermes-agent) | `MEMORY.md` and `USER.md` of every profile | `hermes plugins install ReARCLabs/bilinc-memory-history#hermes` |

Everything is local by default. Each agent keeps its own history in `~/.bilinc/memory-history/<agent>/`, readable by you alone.

## Claude Code

Claude Code rewrites its memory files as you work. It works for Claude's auto memory (`~/.claude/projects/*/memory/`) and for your global `~/.claude/CLAUDE.md`. Project `CLAUDE.md` files stay in git.

### 30-second demo

No account needed.

1. Claude rewrites a memory note.
2. `/bilinc:history` shows exactly what changed, line by line.
3. `/bilinc:undo` puts the old version back.

The undo is a new version in the history, so you can undo the undo.

### Install

In your shell:

```
claude plugin marketplace add ReARCLabs/bilinc-memory-history
claude plugin install bilinc@bilinc
```

Inside a Claude Code session, `/plugin marketplace add ReARCLabs/bilinc-memory-history` works too. `/plugin install` opens a panel there instead of installing straight away.

You need Python 3.9 or newer on your PATH. On macOS, install the Command Line Tools first if you have not. Until then the plugin stays quiet and does nothing.

### Privacy

Everything is local. The history lives in `~/.bilinc/memory-history/claude-code/`, readable by you alone. It stays when you uninstall the plugin. Nothing leaves your machine unless you turn Cloud on.

Delete the history any time with `/bilinc:wipe-local`. That deletes the journal. Your memory files and `CLAUDE.md` are not touched.

### Cloud is opt-in, per project

Cloud lets you move memory between machines. It is off for every project until you turn it on for that project.

Before anything is sent, `/bilinc:cloud on` shows you what would be uploaded, which account it goes to, and that each version counts as one write on your plan's quota. It stops there until you say yes.

- Every version is scanned for secrets on your machine first. A version that looks like it holds one is skipped whole and listed by name. It is never sent.
- Your global `CLAUDE.md` is a separate choice: `/bilinc:cloud on --global`.
- If a Cloud project with the same git remote already exists, the plugin offers to join it.

To use Cloud you need a Bilinc API key. The commands that skills run cannot read the key saved in the plugin settings. Set `BILINC_API_KEY` in your shell, or run `bilinc login`. Without a key, `cloud on`, `push` and `pull` tell you so and change nothing.

### Commands

| Command | What it does |
|---|---|
| `/bilinc:history [file]` | Lists versions, newest first, with a diff for each change |
| `/bilinc:undo [file] [n]` | Shows a preview, then writes the old version back after you confirm |
| `/bilinc:undo --session` | Rolls back everything this session wrote to memory |
| `/bilinc:restore [file]` | Lists deleted files and brings one back |
| `/bilinc:status` | Journal size, Cloud state, pending uploads, and `MEMORY.md` size against the load limit |
| `/bilinc:cloud on\|off` | Turns Cloud on or off for this project |
| `/bilinc:push` | Uploads pending versions now |
| `/bilinc:pull` | Gets memory from another machine, after a preview |
| `/bilinc:link <id>` | Links this project to an existing Cloud project |
| `/bilinc:wipe-local` | Deletes the local history after you confirm. `--object <sha>` purges one version |

Every command that writes shows a preview first and waits for your confirmation before it writes. `undo` and `restore` write the file themselves, so they also work in plan mode and when file edits are denied.

### Moving to another machine

1. On the first machine: `/bilinc:cloud on`.
2. On the second machine: `/bilinc:pull`, pick the project, read the preview, confirm.

The pull links the second machine to the same Cloud project. If a file changed on both sides, you see both versions and nothing is overwritten. Writing your global `CLAUDE.md` needs its own, separate confirmation. The preview makes hidden characters and HTML comments visible, so you see what you are about to accept.

### Questions

**Why not just use git?**
Git is the right tool for project files, and Bilinc leaves those to it. Memory files sit outside your repository, change while you work, and are rewritten by Claude without a commit. Bilinc records each change as it happens, needs no setup, and works per project without a repository.

**Is my memory sent anywhere?**
No, unless you turn Cloud on for a project. The history is local, in a folder that you alone can read. Cloud is a per-project choice with a preview, a secret scan, and an off switch.

**Am I locked in?**
No. Your memory files stay plain Markdown in Claude's own folder. The history is plain files too: each version is stored under its hash, and the log is one JSON line per change. Remove the plugin and your files stay as they are.

### How long is history kept?

The newest 200 versions of each file are kept. `/bilinc:status` shows the journal size.

## Hermes Agent

Hermes keeps what it learns in `MEMORY.md` and `USER.md` under `~/.hermes/memories/`, and each profile has its own pair under `~/.hermes/profiles/<name>/memories/`. Bilinc records every change in the profile you enable it for: writes by the memory tool, `/memory approve`, and edits you make by hand. Other profiles are left alone.

### Install

```
hermes plugins install ReARCLabs/bilinc-memory-history#hermes
hermes plugins enable bilinc-memory-history
```

For a profile, add `-p <name>` to both commands. The plugin is named `bilinc-memory-history`, so it sits next to any other Bilinc plugin you use.

If the Hermes gateway is running, restart it so it loads the plugin: `hermes gateway restart`. Your chats go through the gateway, and a gateway that started before the install does not know the new commands (you see "Unknown command /bilinc-history"). Then start a new session. The first session records the current files as the starting point.

### Commands

The commands run directly, without a model call, so they work the same in the terminal and in the messaging gateway.

| Command | What it does |
|---|---|
| `/bilinc-history [MEMORY.md\|USER.md]` | Lists versions, newest first, with a diff for each change |
| `/bilinc-undo <file> [n]` | Shows a preview of putting the file back to before change `n`. Add `--yes` to apply it |
| `/bilinc-restore [file]` | Lists deleted memory files and brings one back (`--yes` to apply) |
| `/bilinc-status` | Journal size and what is recorded for the active profile |
| `/bilinc-wipe-local` | Deletes the local history after you confirm |

Commands work on the active profile (`hermes -p <name>`).

Hermes loads memory into the session when it starts, so an undo or restore takes effect in the next session (`/new`). Bilinc writes the file back byte for byte, keeping Hermes's `§` separators, and takes the same file lock Hermes uses while it writes.

Cloud is not available for Hermes yet. Everything stays on your machine, in `~/.bilinc/memory-history/hermes/`.

## Repository layout

```
claude-code/   the Claude Code plugin
hermes/        the Hermes plugin
core/          the shared code (bilinc_memory/) and its tests
```

Each agent installs its own folder and nothing else, so each plugin folder carries a copy of `core/bilinc_memory`. Make every change in `core/`, then run `python3 core/sync.py`; the tests fail when a copy is out of date. Run the tests with `python3 -m unittest discover -s core/tests -t core`.

## License

MIT. Copyright 2026 ReARC Labs. See `LICENSE`.
