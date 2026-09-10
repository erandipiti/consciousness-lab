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

#: Verbs that say the device *tells you* something.
REPORTING = ("report", "says", "record", "emit")
#: Words for the physical event, as opposed to seeing it.
PHYSICAL_EDGE = ("pressed", "closed", "the press", "closure")

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
