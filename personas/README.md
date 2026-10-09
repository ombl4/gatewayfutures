# Persona library

A persona is *how a caller talks and what their line sounds like*, never *who they are*: the
name and the facts stay with the session, because they belong to the order. Three groups of
four, kept even on purpose so a matrix run compares like with like:

- `standard`: cooperative callers on a clean line (the control group)
- `hard-line`: cooperative callers on bad audio (noise, phone line, packet loss, poor mic, slow speech)
- `difficult`: clean line, hard behaviour (angry and interrupting, pushy, rambling, suspicious)

A session can reference one with `caller.persona_ref: <name>`; the persona fills style, accent,
voice, pace and line conditions for every field the session does not set itself, and the
session's content hash covers the resolved values, so editing a persona changes the id of every
session using it. `gf run --persona-group hard-line` (or `--persona a,b`) runs each selected
session once per persona as a derived session (same facts, goal and expectations, the persona's
voice and line); the run page shows the session × persona grid. `gf personas` lists the library.
