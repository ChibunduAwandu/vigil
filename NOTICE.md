# Attribution and licensing

Vigil is a personal project by Chibundu Awandu. No open-source license has been
granted for Vigil's own code. Publishing this repository for inspection does not
grant a general license to reuse, modify, or redistribute it.

Vigil uses **Ready Trader Go**, copyright Optiver Asia Pacific Pty. Ltd., as an
external dependency. Its source notices specify **AGPL-3.0-or-later**. That code
and its sample market data are excluded from this repository. The setup script
downloads them separately, including the original license and copyright notices.
This separation does not relicense the dependency or resolve licensing obligations
for uses that combine it with Vigil.

The dependency is fetched from a [community mirror](https://github.com/yusuf-wadi/pyready_trader_go/tree/8b98b0e02436e24e3d6acc2446288d0388837501),
pinned by commit and by per-file SHA-256 hashes in [config/rtg-source.json](config/rtg-source.json).
The mirror is not an official Optiver repository. The selected Python source and
four market sessions match the local exchange distribution used for this project's
tests; the mirror's exchange configuration is a template that Vigil replaces when
starting a live match.

Vigil is not affiliated with or endorsed by Optiver. Python packages, Kafka,
Foundry, and forge-std retain their respective upstream licenses. Test captures,
model files, and result summaries are project artifacts; the historical full-match
recordings are not included.
