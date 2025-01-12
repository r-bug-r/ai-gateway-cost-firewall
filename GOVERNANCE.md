# Governance

## Model

This is a maintainer-led project with a small committer group. There is no foundation, no
funding, and no roadmap committee. Decisions are made in public issues and recorded in
[`decisions/`](decisions/) when they change a documented contract or an architectural
invariant.

## Roles

| Role | Responsibility |
| --- | --- |
| Maintainer | Merge rights, release tags, security response, final call on scope. |
| Committer | Triage issues, review pull requests, merge with maintainer approval. |
| Contributor | Any pull request, issue, or documentation change. |

Roles are earned through sustained, careful contributions — especially ones that found
real failure modes. There is no application form and no vote.

## How decisions are made

1. Open an issue describing the problem, the constraints, and the alternatives.
2. Maintainers state a decision in the issue. If the change affects a documented contract,
   the decision is also written as an ADR in `decisions/`.
3. Disagreement is resolved by evidence: a failing test, a benchmark, a provider document,
   or a demonstrated user impact. Volume of opinion does not decide.
4. A maintainer may reject a change for scope, even when it is correct.

## Scope discipline

The project is deliberately narrow: enforce spend and routing for chat completions, keep
the ledger honest, and stay deployable on one machine. Proposals that would turn it into an
observability platform, a prompt-management product, or a multi-tenant SaaS are out of
scope and will be closed with a pointer to this section.

## Releases

- Versions follow SemVer. Pre-1.0, minor releases may change the API.
- Every release updates `CHANGELOG.md` and passes CI on Linux and Windows.
- Release tags are signed by a maintainer.
- Security fixes may be released without prior notice; the advisory is published after the
  fix is available.

## Security

Security reports are handled privately as described in [SECURITY.md](SECURITY.md). A
maintainer who is the subject of a report must recuse themselves.

## Changing this document

Open a pull request. Governance changes require approval from two maintainers.
