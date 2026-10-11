# For coding agents working in this folder

This folder is an unofficial, throwaway demo. Every change stays inside it.

Read these docs before any change:

- [README.md](README.md): what the demo is, and how to run it.
- [how-it-works.md](how-it-works.md): the services, the configuration and the security
  choices.

Keys come from the environment or from `.env`. A key is never written into a tracked file,
and never printed.

`.env`, `.state/` and `agents-fs/` are ignored by git. They stay out of every commit.
