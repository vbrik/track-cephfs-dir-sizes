# Agent
- Ask clarifying questions until you are certain you understand the problem
- Flag design contradictions
- Flag logical contradictions
- Flag inconsistencies
- Flag scope creep (e.g. one tool does too many things that are not particularly related)
- Flag logical simplification opportunities
- Flag code reuse opportunities
- Don't push git commits
- Use best practices of the programming language you are using
- Follow project-specific instructions in files in `AGENTS.md.d` directory, if it exists.

# Git commit messages
- In *addition* to normal stuff that goes into commit message bodies
  - Include info about the reasoning that led to the changes, so that later we could flag contradictory design decisions:
    - In commit messages include design choices that resulted in the changes
    - In commit messages include intentions behind the changes
