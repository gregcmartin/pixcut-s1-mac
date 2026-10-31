# Security and local data

Images are processed locally. The tool does not send artwork to a cloud API.
Setup downloads dependencies and a pinned upstream driver from public sources.

`create/`, `jobs/`, `.state/`, `.venv/`, USB logs, and the upstream checkout are
excluded from Git. Job manifests can contain local file paths; USB logs can
contain device identity and print payloads. Review them before sharing a bug
report. Do not attach private artwork or full USB traces to public issues.

Printing requires an interactive terminal and an exact confirmation phrase.
The control socket is local, permission-restricted, and exists only during an
active print session. It permits status, job listing, pause/resume, and cancel,
not arbitrary protocol commands. A failed or interrupted submission is never
automatically retried.

To report a security issue, use GitHub's private vulnerability reporting for
this repository if available. Avoid posting secrets or private runtime data
in public issues.
