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
