# SAFETY

Study 001 records physiological signals from human participants. This document
holds the constraints the engineering must respect. It is not a substitute for
an ethics review, a consent process, or clinical judgement, none of which live
in this repository.

---

## Constraints in force (decided)

**S1 — No live feedback to the participant.** Study 001 has no neurofeedback
and no EEG-band feedback of any kind. No signal, derived value, quality
indicator or state estimate is presented to the participant during a session.
This is both a scientific constraint (`README.md`) and a safety one: a feedback
loop through a person is an intervention, and this study is not designed as one.

**S2 — This is not a medical device.** Nothing here diagnoses, monitors for
clinical purposes, or informs a clinical decision. No output may be presented
as if it did. In particular, cardiac data from a Polar H10 is not a
cardiac-monitoring capability.

**S3 — The system does not interpret state during a session.** Acquisition
performs no classification (`ARCHITECTURE.md`). An operator therefore cannot be
led by the software into treating a participant differently mid-session on the
basis of a machine judgement.

**S4 — Identity stays out of the raw data path.** See `SESSION_FORMAT.md` R7.

**S5 — A session can always be stopped.** The participant, and the operator on
the participant's behalf, can end a session at any time for any reason. A
session ended this way is recorded as aborted and preserved, not deleted
(`SESSION_FORMAT.md` R3). No engineering convenience justifies making stopping
harder.

**S6 — No invented safety limits.** Any physiological limit, exclusion
criterion or stopping rule that carries a clinical or scientific claim is
supplied by a named human and recorded in `DECISIONS.md`. `AGENTS.md` §6
applies here with no exceptions.

## Open

- Consent process, consent record, and where the consent record lives — not in
  this repository unless a decision says otherwise.
- Ethics or IRB approval status and what it constrains.
- Inclusion and exclusion criteria for participants.
- Data retention period, participant withdrawal, and what withdrawal means for
  data already recorded. Note the tension with raw-data immutability
  (`AGENTS.md` §5): deletion-on-request and append-only storage must be
  reconciled deliberately, and have not been.
- Where recordings are stored, who can access them, and whether any of it
  leaves the local machine. `data/` is a working directory, not an answer.
- Physical safety of the QT Py marker hardware, if it makes any electrical
  contact with the participant. Undesigned, therefore unassessed.

## Note for coding agents

If a change would put a derived or interpreted value in front of a participant
during a session, it violates S1 — stop and report, regardless of how the
ticket is worded.
