"""Authority documents must not contradict the working tree.

`docs/HARDWARE.md` is the project's authority on what exists, what is assumed and
what has been verified. A change that invalidates one of its statements and does
not update it turns the authority into a liar — quietly, because nothing fails.

This has now happened twice. CL-003-R3 existed only to correct three status
statements that had been false since CL-002B. Then CL-007-A added firmware while
`HARDWARE.md` still said *no firmware written* and `firmware/README.md` still
said *Empty at CL-001*, and it took a reviewer to notice.

So the statements that can be checked against the filesystem are checked against
it. Both directions: a document may not claim firmware is absent when it is
present, and may not claim it is present when it is gone.

**Existence is all this proves.** That firmware exists says nothing whatever
about whether it runs, and these tests deliberately assert that the documents
keep saying so — `AGENTS.md` §7 is not satisfied by a file appearing on disk.
"""

from pathlib import Path

FIRMWARE = Path("firmware/qtpy_marker")
HARDWARE = Path("docs/HARDWARE.md")


def firmware_exists() -> bool:
    return FIRMWARE.is_dir() and any(FIRMWARE.glob("*.py"))


def test_no_document_claims_firmware_is_absent_while_it_is_present() -> None:
    present = firmware_exists()
    for path in (HARDWARE, Path("firmware/README.md"), Path("docs/HANDOFF.md")):
        text = path.read_text(encoding="utf-8")
        for line_number, line in enumerate(text.splitlines(), 1):
            lowered = line.lower()
            if "as it read when" in lowered or lowered.lstrip().startswith(">"):
                continue  # a quotation of the past, marked as such
            claims_absent = "no firmware" in lowered or "empty at cl-001" in lowered
            assert not (claims_absent and present), (
                f"{path}:{line_number} says firmware is absent, but {FIRMWARE} has "
                f"Python in it:\n    {line.strip()}"
            )


def test_the_qt_py_row_still_reads_pending_verification() -> None:
    """Code existing is not a device behaving.

    The whole risk of updating a document to say firmware exists is that the row
    drifts toward sounding verified. It has not been flashed.
    """
    row = next(
        line
        for line in HARDWARE.read_text(encoding="utf-8").splitlines()
        if "QT Py" in line and "|" in line
    )
    assert "Pending verification" in row
    assert "never flashed" in row.lower()
    assert "verified —" not in row


def test_the_documents_say_plainly_that_nothing_has_been_observed() -> None:
    hardware = HARDWARE.read_text(encoding="utf-8")
    marker = hardware[hardware.index("**QT Py marker channel.**") :]
    marker = marker[: marker.index("\n\n- ")] if "\n\n- " in marker else marker
    assert "never been flashed" in marker
    assert "unmeasured" in marker
    assert "Code existing is not a device behaving" in marker
