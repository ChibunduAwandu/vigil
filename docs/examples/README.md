# Example audit report

These are the unedited outputs of the README quick-start command, run on Python
3.12 with the checked-in model and rulebook and no regulatory snapshot:

```bash
vigil simulate --session 2 --scenario rogues --match-id quickstart --no-regulatory
```

- [Readable report](quickstart.md)
- [Canonical JSON](quickstart.json)

The example has 66 alerts and reconciles all 1,393 reported trades. The six abusive
traders are flagged; the three baseline traders have no alerts. The JSON is a
selected generated artifact committed so a reviewer can inspect the schema and
verify its internal consistency without running the demo. It has not been anchored
on a public chain.

SHA-256 of `quickstart.json`:

```text
899390c1b7bf7339b520018762da8fa415d161a6627f739108d52cd7a83734e8
```

Changing dependency versions, the model, rulebook, or other report inputs may change
the generated example. Historical measurements live separately in `docs/results/`.
