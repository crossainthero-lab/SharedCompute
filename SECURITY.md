# Security policy

SharedCompute is an early-stage MVP with no hosted service. Please still report vulnerabilities privately,
especially anything affecting:
- provider machines (the agent, device opt-in, TLS handling);
- token handling;
- result verification;
- the ledger.

**How to report:** use GitHub's private vulnerability reporting (*Security → Report a vulnerability* on this
repository). Do not open a public issue for security problems.

Please include the version or commit, steps to reproduce, and the impact. Never include real node tokens,
passwords or private hostnames.

Known, documented limitations (not vulnerabilities in themselves) are listed in [README.md](README.md#current-limitations)
and [docs/ARCHITECTURE.md §9](docs/ARCHITECTURE.md#9-security-model).
