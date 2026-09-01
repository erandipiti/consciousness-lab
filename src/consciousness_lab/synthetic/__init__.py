"""Deterministic synthetic stream source for exercising Session Package v2.

**Scope boundary.** This exists only to drive the storage, session and recorder
paths end to end with no hardware attached. It generates no scientific signal:
the values are a reproducible pattern and nothing may read them as measurements.

The generic asynchronous multi-device recorder with fan-in orchestration is
**CL-003** and lives in `consciousness_lab.session.recorder`. What lives here is
the source that exercises it.
"""
