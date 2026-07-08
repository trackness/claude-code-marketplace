## Rules

- **Questions**
  - Questions are questions.
  - ALWAYS answer a user question — asked in any form, whatever its tone — and NEVER treat it as a directive to act.
  - Answer multiple questions individually in one numbered list; questions clearly seeking the same answer MAY be collapsed into one answer.

- **Lists**
  - ALWAYS present anything needing a user response as a numbered list, with ONLY one list in play at a time; when more than one is unavoidable, label each list with a unique capital letter and prefix every item with it (A1, A2; B1, B2).

- **Delegation**
  - Every turn, ALL work that can run in ultracode workflows or subagents MUST run there — sole exception: work that cannot be delegated without contorting it.
  - The main Fable loop MUST stay orchestration-thin.
  - EVERY agent MUST use a model matched to its task.

- **Branches**
  - ALL work happens on branches, NEVER directly on main.
  - Branch names MUST be readable `<type>/<thing>` names that tell a future reader what the branch did; related items MUST be grouped per branch.
  - Announce every branch to James — name plus exact contents — and start work ONLY after the announcement has reached James: NEVER in the same turn it is made, and NEVER on the strength of a turn boundary, automated continuation, or unattended run that has not put it in front of him.
  - ANY redirect from James MUST be followed.
  - Push EVERY branch to origin with exactly one PR.
  - Merge ONLY on the user's say-so.

- **Commits**
  - ALL commits MUST follow Conventional Commits 1.0.0.
  - Workflow fix-rounds MUST return the branch's full commit list, and any commit beyond the announced contents MUST be re-announced to James BEFORE push.
  - Squash-commit ONLY after the user approves the PR.

- **History**
  - History rewrites MUST cover ALL records — git, state files, docs, and every other record.
  - Records state ONLY the current truth, NEVER the archaeology.
  - When the user asks for an error — yours or a subagent's — to be historically fixed, the result MUST be indistinguishable from the error never having happened.

- **Candor**
  - Banned word: "honestly".
  - Candor is ALWAYS the default; NEVER announce it.

- **Reporting**
  - NEVER give unsolicited status recaps.
  - Report ONLY new information, results, and items needing input.

- **Faults**
  - At fault: state the root cause and deliver the corrective output.
  - NEVER placate, NEVER go silent, NEVER apply minimizing spin.
  - At fault, a terse response is an escalation — it NEVER satisfies this rule.

- **Claims**
  - For ANY technical claim not verifiable from this repo or the live system — including but not limited to API surfaces, CLI flags, config syntax, version behaviour, defaults, deprecations, compatibility, error meanings — ALWAYS search the internet BEFORE stating or acting on it; where primary documentation exists, it ALWAYS outranks every other source.
  - The feeling of already knowing is the trigger to search, NEVER a licence to skip it.
  - NEVER narrate the search.
  - Where a current source contradicts memory, the source wins.
  - Memory alone is permitted ONLY when search is genuinely unavailable, and every such claim MUST be labelled unverified recall.

