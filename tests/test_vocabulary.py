"""One rule, enforced mechanically instead of remembered.

Three consecutive reviews of CL-007-A found the same defect in three different
files: a claim about what a device would sense, asserted as fact, with no
observation behind it. It moved from the firmware README to a source docstring
to a test docstring, surviving each time because the fix was aimed at the file
rather than at the class of mistake.

`docs/DECISIONS.md` D27 records the escalation rule that produced Session Package
v2: when successive rounds keep finding new instances of one defect class, the
defect is not the instances. So this stops being something anyone has to
remember.

**The rule.** Code does not name the physical events a person might produce — not
in a comment, not in a docstring, not in a test. `AGENTS.md` §7 says a statement
about a physical device is verified or assumed, never asserted, and
`docs/HARDWARE.md` currently records every one of these as *Unknown*. Code that
explains itself by what a stream is *for* has already interpreted the device; the
only reason available without interpreting is that the device offers it.

Proposals about what these events might do belong in `HARDWARE.md` as open
questions, which is where they are. **This file is the one place allowed to name
them**, because enforcing a rule requires stating it — and it is excluded from
its own scan for exactly that reason.
"""

import re
from pathlib import Path

#: Physical events a person might produce. There is no legitimate reason for
#: acquisition or verification code to name one: naming it means the code has
#: decided what a signal is for.
BANNED = ("tap", "cough", "strike", "impact", "blink", "clench")

ROOTS = (Path("src"), Path("tests"))
SELF = Path(__file__).name


def names_event(line: str) -> str | None:
    """The physical event this line names, if any.

    Whole words only: `overlapping` contains `tap` and is not a claim about
    anything. Getting that wrong in either direction makes the guard useless —
    too loose and it misses the real thing, too tight and it gets deleted for
    crying wolf, which is how the first version of this check died.
    """
    lowered = line.lower()
    for word in BANNED:
        start = 0
        while (index := lowered.find(word, start)) != -1:
            before = lowered[index - 1] if index else " "
            after_at = index + len(word)
            after = lowered[after_at] if after_at < len(lowered) else " "
            if not (before.isalnum() or before == "_") and not (after.isalnum() or after == "_"):
                return word
            start = index + 1
    return None


def scan() -> list[str]:
    """Every place code names a physical event."""
    hits: list[str] = []
    for root in ROOTS:
        for path in sorted(root.rglob("*.py")):
            if path.name == SELF or "__pycache__" in path.parts:
                continue
            for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
                word = names_event(line)
                if word:
                    hits.append(f"{path}:{number}: {word!r} in {line.strip()[:70]}")
    return hits


def test_code_never_names_a_physical_event_a_person_might_produce() -> None:
    hits = scan()
    assert not hits, (
        "code named a physical event, which asserts what a device would sense — "
        "HARDWARE.md records every one of these as Unknown. Move it there as an open "
        "question:\n  " + "\n  ".join(hits)
    )


def test_the_guard_catches_the_exact_sentence_that_got_through_three_times() -> None:
    """A guard nobody has watched fail is a guard nobody knows works."""
    assert (
        names_event("    # the accelerometer is the channel that carries a tap or a cough") == "tap"
    )
    assert names_event('    """Does a cough show in both accelerometers?"""') == "cough"


def test_the_guard_does_not_cry_wolf() -> None:
    """The first version of this check was deleted for firing on ordinary prose."""
    for innocent in (
        "for packet in overlapping_windows:",
        "run ./scripts/bootstrap-mac.sh",
        "the adapter was impacted_by_nothing",
        "striker = None",
        "# describes what arrives",
    ):
        assert names_event(innocent) is None, innocent


# --- the same rule, applied to precision rather than to vocabulary ------------

#: Phrases that claim a timing precision nobody has measured. The marker polls,
#: so it can only ever report when it OBSERVED an edge; the edge itself was
#: earlier by an unmeasured interval.
OVERCLAIMS = (
    "exactly when",
    "precisely when",
    "the exact moment",
    "the instant it was",
    "at the moment of",
)

MARKER_FILES = (
    Path("firmware/qtpy_marker/code.py"),
    Path("firmware/qtpy_marker/README.md"),
)


def test_the_marker_never_claims_to_time_the_physical_edge() -> None:
    """It times an observation of the edge. Those are two quantities.

    The firmware and its README both opened with "exactly when it was pressed"
    while the same head recorded the polling interval as unmeasured — a claim
    contradicted by a document in the same commit. `TIMING.md` keeps an
    underlying event and an observation of it apart, and so must this.
    """
    for path in MARKER_FILES:
        lowered = path.read_text(encoding="utf-8").lower()
        for phrase in OVERCLAIMS:
            assert phrase not in lowered, (
                f"{path} claims {phrase!r}; the marker reports when it OBSERVED an "
                "edge, and the gap to the physical edge is unmeasured"
            )


def test_the_marker_says_what_its_timestamp_actually_is() -> None:
    """Removing a false claim is not the same as making a true one."""
    for path in MARKER_FILES:
        lowered = path.read_text(encoding="utf-8").lower()
        assert "observed" in lowered, f"{path} must say the mark is an observation"
        assert "unmeasured" in lowered, f"{path} must keep the gap explicit"


def test_the_boot_banner_encodes_no_unmeasured_timing_fact() -> None:
    """`time.monotonic_ns()` printing nanoseconds says nothing about the tick.

    A coarse clock scaled into nanoseconds is indistinguishable from a fine one
    at this end, so the banner reports the REPRESENTATION as a fact and the
    resolution as unmeasured.
    """
    code = Path("firmware/qtpy_marker/code.py").read_text(encoding="utf-8")
    banner = next(line for line in code.splitlines() if 'emit(f"B ' in line)
    assert "time_unit=ns" in banner, "the format is knowable and is stated"
    assert "resolution=unmeasured" in banner, "the clock's resolution is not, and says so"
    assert "ns_per_tick" not in code, "a per-tick number would assert what nobody measured"


# --- the physical edge and an observation of it are two things -----------------

#: Verbs that say the device *tells you* or *keeps* something. Widened after a
#: round where "records when the thing happened" and "true to first contact"
#: both slipped past a narrower list: the defect is semantic, so a guard built
#: on one phrasing catches one phrasing.
REPORTING = (
    "report",
    "says",
    "record",
    "emit",
    "timestamp",
    "true to",
    "tied to",
    "keeps the mark",
    "marks when",
)
#: Names for the physical event, as opposed to seeing it happen.
PHYSICAL_EDGE = (
    "pressed",
    "closed",
    "the press",
    "closure",
    "first contact",
    "thing happened",
    "something happened",
    "when it happened",
)

#: Where a claim about this device is load-bearing. CHANGELOG.md is deliberately
#: absent: it is a history, and quoting a past error while describing its fix is
#: what a history is for.
CLAIM_SURFACES = (
    Path("firmware/qtpy_marker/code.py"),
    Path("firmware/qtpy_marker/README.md"),
    Path("firmware/README.md"),
    Path("docs/HARDWARE.md"),
)


def prose_of(path: Path) -> str:
    """Only the prose. Code is not prose, and splitting it on periods is nonsense.

    A first version scanned `.py` files whole and joined a trailing comment to the
    next `def` because neither ended in a period, inventing a sentence nobody
    wrote. Comments and docstrings are where claims live; attribute access is not
    a claim.
    """
    if path.suffix != ".py":
        return path.read_text(encoding="utf-8")
    import ast

    source = path.read_text(encoding="utf-8")
    parts: list[str] = []
    for line in source.splitlines():
        _, marker, comment = line.partition("#")
        if marker and not line.strip().startswith(("'", '"')):
            parts.append(comment.strip() + ".")
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            doc = ast.get_docstring(node)
            if doc:
                parts.append(doc)
    return "\n".join(parts)


def sentences(text: str) -> list[str]:
    """Sentences, with wrapping removed.

    Whitespace is normalised first because every check in this file that matched
    a raw substring has been broken at least once by a line wrap — including one
    introduced by the very edit that was fixing the sentence. A guard that a
    reflow can defeat is not a guard.
    """
    flat = " ".join(text.split())
    return [part.strip() for part in flat.replace("—", ".").split(".") if part.strip()]


def claims_the_edge(text: str) -> list[str]:
    """Sentences that say the device reports the physical event itself.

    The device polls. It can report when it OBSERVED a transition; the closure
    was earlier by an unmeasured amount. A sentence that pairs a reporting verb
    with the physical event and never says "observed" has collapsed the two,
    which `TIMING.md` exists to prevent and `AGENTS.md` §7 forbids.
    """
    guilty = []
    for sentence in sentences(text):
        lowered = sentence.lower()
        if "observ" in lowered:
            continue
        if any(verb in lowered for verb in REPORTING) and any(
            word in lowered for word in PHYSICAL_EDGE
        ):
            guilty.append(sentence)
    return guilty


def test_no_document_says_the_device_reports_the_physical_edge() -> None:
    hits = [
        f"{path}: {sentence[:90]}"
        for path in CLAIM_SURFACES
        for sentence in claims_the_edge(prose_of(path))
    ]
    assert not hits, (
        "a reporting verb paired with the physical event, with no mention of "
        "observing it — the marker times an observation, and the edge was earlier by "
        "an unmeasured amount:\n  " + "\n  ".join(hits)
    )


def test_that_check_catches_both_sentences_that_got_through() -> None:
    """Both were real, and one survived a round because a line wrap hid it."""
    assert claims_the_edge("This firmware reports when its switch closed, on its own clock.")
    assert claims_the_edge("a switch that reports, over USB serial, when it was pressed")
    # The wrapped form must be caught identically; normalising is the whole point.
    assert claims_the_edge("This firmware reports when its switch\nclosed, on its own clock.")


def test_that_check_does_not_fire_on_the_honest_wording() -> None:
    for innocent in (
        "This firmware reports the device-clock time at which its polling loop first "
        "observed the input go low, not when the switch physically closed.",
        "The physical instant the switch closed is earlier by an unknown amount.",
        "A switch that reports the first observed high-low transition.",
    ):
        assert not claims_the_edge(innocent), innocent


# --- a superseded rule may survive in prose long after the decision ------------

#: What D42 says is gated: the design. Never the instrument that measures.
GATED_BY_D42 = ("mechanism", "alignment", "electrical")
#: What D42 says may precede the measurement, because the measurement needs it.
NOT_GATED = ("firmware", "probe", "instrument")
#: Phrasings that assert something is blocked until a measurement exists.
GATING = (
    "cannot be designed",
    "must not start",
    "do not start it before",
    "is gated on",
    "gates cl-007",
    "gated until",
)


def test_no_source_still_states_the_rule_d42_superseded() -> None:
    """A decision changes; prose that stated the old rule does not follow it.

    `docs/HANDOFF.md`'s gate said the marker could not be designed until serial
    round-trip latency was measured. D42 split that: the INSTRUMENT — firmware
    that answers a ping, and the probe that times it — is a prerequisite for the
    measurement and had to exist first; only the mechanism and its electrical
    interface stay gated.

    The amendment was written in this pull request, and two docstrings in `src/`
    went on stating the superseded rule anyway, so the repository gave two
    incompatible instructions in the same commit. This is the class R6 caught
    between documents and the tree, one level up: between prose and a decision.
    """
    offences: list[str] = []
    for path in sorted(Path("src").rglob("*.py")):
        for sentence in sentences(prose_of(path)):
            lowered = sentence.lower()
            if not any(phrase in lowered for phrase in GATING):
                continue
            if any(word in lowered for word in NOT_GATED) and not any(
                word in lowered for word in GATED_BY_D42
            ):
                offences.append(f"{path}: {sentence[:100]}")
    assert not offences, (
        "source states that the instrument is gated on the measurement it produces, "
        "which D42 superseded — the gate falls on the mechanism, not the firmware "
        "or the probe:\n  " + "\n  ".join(offences)
    )


def test_that_check_catches_the_superseded_wording() -> None:
    superseded = (
        "docs/HANDOFF.md gates CL-007 on this: the marker firmware cannot be "
        "designed until serial round-trip latency is measured."
    )
    lowered = superseded.lower()
    assert any(phrase in lowered for phrase in GATING)
    assert any(word in lowered for word in NOT_GATED)
    assert not any(word in lowered for word in GATED_BY_D42)


def test_that_check_accepts_the_amended_wording() -> None:
    amended = (
        "The gate falls on the marker MECHANISM and its electrical interface, "
        "never on the instrument (DECISIONS.md D42)."
    )
    lowered = amended.lower()
    assert any(word in lowered for word in GATED_BY_D42), "naming what IS gated clears it"


# --- claims about a board nobody has connected --------------------------------

#: Universal or version claims about hardware. No board has been connected and
#: `HARDWARE.md` does not even record which model this is, so "every variant"
#: and "boards ship X" are assertions about devices this repository has never met.
BOARD_CLAIM = re.compile(
    r"\b(every|all|any)\s+(qt\s*py|variant|board)"
    r"|\bboards\s+ship\b"
    r"|\bexists\s+on\s+(every|all)\b",
    re.IGNORECASE,
)


def test_the_marker_asserts_nothing_universal_about_boards_it_has_never_met() -> None:
    """AGENTS.md §7 applies to pinouts and firmware versions too.

    `code.py` said A0 exists on every QT Py variant and named a subset, and that
    current boards ship CircuitPython 9.x. Nobody has connected a board, and
    `HARDWARE.md` does not record which model this even is. A pinout is a device
    fact like any other.
    """
    offences = [
        f"{path}: {match.group(0)!r}"
        for path in MARKER_FILES
        for match in BOARD_CLAIM.finditer(prose_of(path))
    ]
    assert not offences, (
        "a universal claim about hardware this repository has never connected; say what "
        "to read off the board in hand instead:\n  " + "\n  ".join(offences)
    )


def test_that_board_check_separates_a_claim_from_an_instruction() -> None:
    assert BOARD_CLAIM.search("A0 exists on every QT Py variant")
    assert BOARD_CLAIM.search("current QT Py boards ship 9.x")
    # Telling someone to read their own board asserts nothing.
    assert not BOARD_CLAIM.search("Read the pinout for the model in your hand")
    assert not BOARD_CLAIM.search("Set BUTTON_PIN to a free GPIO on your board")


# --- a magnitude nobody measured, and a quantity called something else ---------

#: Vague or ranged magnitudes. A configuration value is always ONE exact number —
#: `every 10 s` matches a constant in the firmware — so a range or a "tens of" is
#: never our own setting and always a claim about how something behaves.
VAGUE_MAGNITUDE = re.compile(
    r"\b(tens|hundreds|thousands) of \s*(milli|micro|nano)?seconds\b"
    # en dash and hyphen both, written as escapes so no ambiguous glyph sits in source
    r"|\b\d+\s*(to|\u2013|-)\s*\d+\s*(ms|milliseconds|\u00b5s|microseconds|ns|nanoseconds|s\b)",
    re.IGNORECASE,
)


def test_no_claim_surface_quotes_a_magnitude_nobody_measured() -> None:
    """AGENTS.md §7: a number about a device is sourced and labelled, or absent.

    The firmware asserted that BLE arrives with "tens to hundreds of milliseconds"
    of latency. No device has been connected, `HARDWARE.md` records that timing as
    unmeasured, and there is no source — the number was there to sound
    authoritative. Saying "unmeasured" in the same sentence does not license it;
    the number IS the claim.

    A range or a vague order of magnitude is never a configuration value, which is
    always one exact number. That is the line this draws.
    """
    offences = [
        f"{path}: {match.group(0)!r}"
        for path in CLAIM_SURFACES
        for match in VAGUE_MAGNITUDE.finditer(prose_of(path))
    ]
    assert not offences, (
        "a magnitude about device or transport behaviour, with no source and no "
        "measurement behind it:\n  " + "\n  ".join(offences)
    )


def test_that_magnitude_check_knows_a_claim_from_a_setting() -> None:
    assert VAGUE_MAGNITUDE.search("arrive with tens to hundreds of milliseconds of latency")
    assert VAGUE_MAGNITUDE.search("between 20 - 400 ms, variable")
    # Our own configuration is one exact number and must stay allowed.
    assert not VAGUE_MAGNITUDE.search("| `H <seq> <device_ns>` | every 10 s | heartbeat |")
    assert not VAGUE_MAGNITUDE.search("Ignore further edges for 25 ms after a press.")


#: Names for quantities this probe does not produce. TIMING.md defines its terms
#: narrowly; a serial round trip is none of them.
NOT_WHAT_RTT_IS = ("jitter", "uncertainty", "error bar", "trustworthy")


def test_the_serial_probe_does_not_rename_what_it_measured() -> None:
    """Round-trip time is host scheduling plus USB plus firmware plus the return.

    Calling that spread "device jitter", or the uncertainty of a mark, collapses
    four quantities into one — which is the thing `TIMING.md` exists to forbid.
    Deriving either from this number would need a stated procedure and evidence,
    and neither exists, so the result keeps the only name it has earned.
    """
    for path in (
        Path("src/consciousness_lab/verification/devices.py"),
        Path("src/consciousness_lab/verification/probe.py"),
    ):
        prose = prose_of(path)
        for sentence in sentences(prose):
            lowered = sentence.lower()
            if "round" not in lowered and "rtt" not in lowered and "spread" not in lowered:
                continue
            for word in NOT_WHAT_RTT_IS:
                assert word not in lowered or "not " in lowered or "never" in lowered, (
                    f"{path} calls the round trip {word!r}: {sentence[:100]}"
                )


# --- the rule applied to the documents this ticket had to correct -------------


def test_the_firmware_readme_designs_no_alignment_mechanism() -> None:
    """D42 gates that design, and this ticket wrote D42. It must obey it."""
    readme = Path("firmware/qtpy_marker/README.md").read_text(encoding="utf-8")
    # Same word-boundary rule the scanner uses, so this cannot cry wolf where
    # the scanner would not. `double-tap reset` is a flashing instruction and
    # was reworded rather than exempted: a guard with exceptions accumulates
    # exceptions until it means nothing.
    for number, line in enumerate(readme.splitlines(), 1):
        named = names_event(line)
        assert named is None, (
            f"README:{number} names {named!r}, which is alignment design that "
            "DECISIONS.md D42 gates on a measurement that has not been taken"
        )
    lowered = readme.lower()
    for phrase in ("striking", "alignment chain", "cross-check"):
        assert phrase not in lowered, f"{phrase!r} is alignment design"
    assert "undesigned" in lowered and "d42" in lowered


def test_the_proposal_survives_as_a_question_rather_than_a_design() -> None:
    """A good idea must not be lost, and must not be promoted either."""
    hardware = Path("docs/HARDWARE.md").read_text(encoding="utf-8")
    questions = hardware[
        hardware.index("## Open questions") : hardware.index("## Producing the evidence")
    ]
    assert any(word in questions for word in BANNED), "the proposal is recorded"
    assert "Unknown" in questions, "and recorded as unknown"
    assert "not as a design" in questions
    assert "genuinely sensed by both" not in hardware
    assert "exactly the right instrument" not in hardware
