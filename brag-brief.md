# Reverie — brag brief

This brief tells `/brag` what the Reverie video should show, say, and feel like. Source of truth for claims: the project README (v0.2.0) and the homepage at https://reverie.muditsarda.com/. Where this brief and the code disagree, follow the code and tell the user.

## Purpose

- **Format:** looping homepage video for https://reverie.muditsarda.com/. It plays muted, so it must make sense without sound.
- **Length:** 15 to 20 seconds.
- **Aspect ratio:** 16:9 for the hero. Keep key text inside a safe area so it still reads on a phone.
- **Audience:** developers who already use a coding agent such as Claude Code, Cursor, Codex CLI or OpenCode, and who are tired of re-explaining their project to every new session.
- **Goal:** a developer should understand in one viewing that Reverie keeps the conclusions an agent reaches, so the agent can pick them up next session without the developer re-explaining. The developer reviews what gets kept. The agent does the remembering.

## Core message

One line, in this order of importance:

1. The agent re-derives the same understanding every session, and the developer re-explains it.
2. Reverie stores the conclusions the agent reaches, on the developer's machine.
3. Nothing is kept until the developer reviews it.
4. In a later session, the agent asks Reverie for past conclusions and checks them against the code.

The homepage headline is "Your coding agent shouldn't start every session as a stranger." You may echo it in the opening or closing card. Do not build the video around it.

## Tone

- Plain, technical, understated. Write like a good engineer explaining a tool to a colleague.
- Short declarative sentences. Concrete nouns and commands. No adjectives that are only there to sell.
- Admit limits where it fits. The README says: "Status: early. Some candidate conclusions are wrong, and your agent doesn't always check memory on its own." It is fine for the video to say the tool is early.

**Banned:** "revolutionary", "game-changing", "supercharge", "10x", "seamless", "magic", "never forgets", "finally", "the future of", "AI that learns you", any rhetorical "imagine…".

**Banned claims:** speed, cost, or productivity gains; accuracy or quality improvements; benchmark results of any kind. The project's own benchmark run did not show an improvement, so the video must not imply one.

## Look and feel

- **Terminal-native.** Monospace text, a dark or neutral background, and real-looking CLI output. Developers should recognize the screens they already use.
- **Palette:** neutral background with a blue accent to match the site. Use solid outlines for reviewed or accepted items and dashed outlines for unreviewed candidates. The site uses this same convention, so it should carry over.
- **Pacing:** calm. Hold each frame long enough to read it on mute. No fast cuts, zooms, or whip transitions.
- **Typography:** high-contrast text. Keep captions short and place them where they won't be covered.
- **Avoid:** glowing brains, neural-network imagery, abstract gradients, 3D, floating orbs, stock "AI" art, glitch effects, and typing sounds.

## Story beats (in order)

**1. The problem (about 4 seconds).**
Show a fresh agent session starting cold. Show the developer typing the same context again: the auth flow, why a decision was made, which module owns what. Keep it concrete and a little dry.

**2. The agent records a conclusion (about 4 seconds).**
During normal work, the agent reaches a real engineering conclusion and records it. Show it as a candidate appearing in a queue, marked as unreviewed (dashed). Use an example that clearly reads as illustrative, such as:
`auth correctness depends on optimistic token refresh`
The README says Reverie records a conclusion when it reaches a real engineering insight, not for every edit. Show that restraint if you can.

**3. The developer reviews (about 4 to 5 seconds).**
Show the review step in the terminal with `reverie review`. Show accepting one candidate and rejecting another. The accepted one turns solid. Make clear that nothing reaches long-term memory without this decision.

**4. The next session (about 4 to 5 seconds).**
A new session starts. The agent reads the relevant code first, then asks Reverie for past conclusions. The result comes back with its confidence and status, and is labelled as past understanding to verify, not as fact. Show the agent checking it against the code.

**5. End card (about 3 seconds).**
- The install command: `pip install -e .` then `reverie install`
- A line stating where data lives: local, one SQLite file at `~/.reverie/reverie.db`, no account
- `github.com/muditsarda1122/Reverie`

## Must show

- Real command names exactly as written: `reverie install`, `reverie review`, `reverie enable`, `/reverie-review`.
- The review gate: accept or reject, and nothing enters long-term memory without that decision.
- Local storage: one SQLite file on the developer's machine.
- At least one visible example of the agent reading the code before trusting a remembered conclusion.

## Must say (on screen or in the end card)

- Reverie is early.
- Extraction uses the LLM provider the developer configures. Retrieval runs locally.
- Nothing is kept without review.

## Must not

- Imply the agent is always right, or that memory is always correct. The README says it isn't.
- Show invented screens, UI, or features. Reverie has no graphical interface. The review step is a terminal interface. Do not draw a web dashboard, graph view, or brain visualization.
- Name agents Reverie does not support. The supported agents are Claude Code, Codex CLI, Cursor and OpenCode.
- Describe the internal math (log-odds, Bayesian updates, decay). It is accurate but not what a developer needs from a homepage.
- Show the waitlist or sign-up flow unless the site itself is being updated to show it.

## Process notes for the brag run

- Present the plan before rendering. Check the plan against this brief, especially the banned-claims and must-not lists.
- Where a real Reverie screen can't be captured, render the real command output as text and say so in the plan. Do not stylize it into something that looks like a different product.
- Keep the video under 20 seconds.
