"""Hardware verification: recording what a device actually does.

**This is not a layer, and nothing in the data path may import it.** It is a
diagnostic that sits beside the system, not inside it. `ARCHITECTURE.md`'s five
layers describe the path data takes from a device to an analysis; this package
takes no data anywhere. It exists so that the rows in `docs/HARDWARE.md` can
stop saying *pending verification* on the strength of something measured.

**It observes and it does not conclude.** That distinction is the whole point.
"The `timestamp` column increased monotonically across 1,183 packets" is an
observation. "The device provides a device clock" is a conclusion, and a wrong
one is unrecoverable: `HARDWARE.md` warns in as many words that *BrainFlow
producing a timestamp column does not establish that the timestamp came from
the device*. A library can synthesise on the host what looks like a device fact.
So nothing here names a quantity, decides what a column means, or promotes a
claim. It writes down what arrived and what the host was, and a human reads it.

**Its output is never study data.** Reports are written outside
`data/sessions/`. Putting a device on to see whether it streams is not a
recording session, and treating it as one would put unconsented, unprotocolled
data into the study (`SAFETY.md`).

**Promotion to verified is a human act.** `AGENTS.md` §7 requires someone who
connected the physical unit and observed it. This package can produce the
evidence for a `HARDWARE.md` entry; it cannot write one, and it cannot flip a
`ClaimStatus` to `verified`.
"""
