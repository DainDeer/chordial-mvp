# custom notification sounds (optional drop-ins)

the focus view plays a sound with its desktop notifications *if* a matching
file exists here; otherwise the OS default notification sound is used. no
config, no restart — the frontend probes this directory once per page load.

| moment              | filename    |
|---------------------|-------------|
| pomodoro complete   | `pom-end`   |
| break over          | `break-end` |

extensions tried, in order: `.mp3`, `.ogg`, `.wav` — e.g. `pom-end.mp3`.
files here are served at `/static/sounds/` like everything else in static/.

this README is also the .gitkeep: the directory ships empty on purpose.
