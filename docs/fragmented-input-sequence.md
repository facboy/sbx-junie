# A fragmented input sequence is typed into the prompt

Junie CLI 3659.7, Linux, in a sandbox whose terminal is reached through an extra
PTY hop. Hovering the mouse over the prompt leaves fragments such as `;20;8M` as
literal text in the input buffer, and typing keys the kitty keyboard protocol
encodes — Enter among them — can leave `13u` behind while the key itself does
nothing.

## Reproduction

1. Run the TUI in a terminal whose PTY delivers input in more than one chunk,
   with mouse reporting active (`ESC[?1003h`, which the TUI enables) or the kitty
   keyboard protocol in use.
2. Move the mouse over the prompt, or press Enter.
3. An incomplete sequence is occasionally read, and its tail is inserted as
   typing.

Not deterministic: the failure needs an inter-byte gap that exceeds the reader's
timeout, which happens when the PTY fragments under load.

## Observed

From the log with `-Djunie.logging.debug.enabled=true`, one event:

```
17:36:51.242  <35;19;8M                                complete report, parsed
17:36:51.243  MouseMove(position=TerminalPoint(x=19, y=8))
17:36:51.270  <35                                       reader stopped after 5 bytes
17:36:51.271  Parsed key: null                          parse returned nothing
17:36:51.274  InsertText(text=;, origin=Typed)          tail arrives as typing
17:36:51.277  InsertText(text=2, origin=Typed)
17:36:51.281  InsertText(text=0, origin=Typed)
17:36:51.295  InsertText(text=;, origin=Typed)
17:36:51.296  InsertText(text=8, origin=Typed)
17:36:51.298  InsertText(text=M, origin=Typed)
```

The report was `ESC[<35;20;8M`. The reader consumed `ESC[<35`, timed out, and the
remaining `;20;8M` was read as six typed characters.

Enter reaches the prompt the same way. The TUI turns the kitty keyboard protocol
on, so a key press arrives as `ESC[<unicode>;<modifiers>u` rather than as a byte:
Enter is `ESC[13u`. Split as `ESC[` and `13u`, the prefix is read as a bare
Escape, which clears the prompt, and `13u` is inserted as text — the key itself
never arrives.

## Cause

`KittyKeyboardHandler.readCSISequence` reads the sequence with a 50 ms timeout for
the first byte and **10 ms for each subsequent byte**, capped at 50 characters.
A 10 ms budget per byte is exceeded by a PTY that fragments.

The collected string is then handed to `parseStandardCSI` and
`tryParseNonStandardCSI`, and both return `null` for a truncated fragment like
`<35`. `parseCSI` returns that `null` to `parseKey`, which returns it to the input
loop. The bytes already consumed are dropped, and the bytes that arrive afterwards
are no longer part of any sequence, so they are inserted as text.

The same sequence read in one chunk (the common case) parses correctly, which is
why this only appears intermittently.

## Suggested fixes

- On a null parse, consume the remainder of the sequence rather than leaving it to
  be read as input. The reader knows it abandoned a sequence mid-way, so it can
  drain to the terminating byte.
- Raise or extend the intra-sequence timeout: keep waiting while bytes continue to
  arrive, instead of a fixed 10 ms per byte.
- Optionally, do not insert printable bytes as text when the preceding read ended
  in a timeout partway through a CSI sequence.

## Secondary symptom

Each abandoned read blocks the input thread for up to 50 ms before giving up, and
while hover reporting is active the dispatcher receives a steady stream of
`MouseMove` events. Measured here: 688 `MouseMove` dispatches, each walking all
five event layers with `No handlers found for event=MouseMove`, and a
`static=4272 ms` transcript rebuild during one such burst. Disabling motion
reporting removes both the corruption and the stalls, which points at the same
input path rather than the renderer.

## Workaround in this kit

`junie-tty-filter.py` runs the CLI on a PTY of its own, holds every byte the
terminal sends until the input sequence it belongs to ends, and writes the
sequence to the CLI in one piece. That closes the corruption: pieces up to 60 ms
apart are rejoined, against the CLI's own 10 ms budget.

It also rewrites `ESC[?1003h` to `ESC[?1002h`. Reporting modes 1000, 1002 and 1003
are one mutually exclusive setting in the terminal, so this drops the report per
mouse movement while press, release and drag reports keep arriving. That is the
load half of the problem rather than the corruption half: the hovering above cost
6628 dispatches that found no handler — 6 per report, bursts of 546/s — where no
mouse event ever matched a handler.
