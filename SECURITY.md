# Security

## Reporting a vulnerability

Report it privately through this repository's Security tab: choose **Report a vulnerability**. Please do not open a public issue. Include what an attacker could do, and the steps to show it.

## What the harness protects

- **Agent tokens.** Each websocket agent presents its own token, and `omegaeval.toml` holds them. The file is in `.gitignore`; keep it private, or write tokens as `"${NAME}"` to read them from the environment.
- **The console.** It listens on 127.0.0.1 by default and has no login. It refuses requests whose Host header is not this machine, which blocks DNS rebinding. It refuses writes from other origins, and queuing episodes requires a JSON request, so a web page you visit cannot start episodes. Binding it to another address exposes it to that network.
- **The gateway.** It listens on every interface by default, so agents in containers can reach it, and accepts only known tokens.
- **Evidence.** Docker logs are held by the Docker daemon. A log file, and any trace an agent reports itself, can be written by the agent; every manifest records which kind each run used.
- **Registry entries.** A config block from the registry can only name a built-in adapter or one from the package its entry installs, and the harness loads nothing but benchmark classes from it. Installing a package still runs its code, so maintainers review every entry.
- **Bundles.** Unpacking accepts only plain files inside the bundle, up to four times the bundle size limit. Raw agent logs can carry prompts from other conversations; `omegaeval bundle --withhold-logs` leaves them out.

## What it does not protect against

- An agent's own tools. The harness records what an agent does and can void an episode that breaks a tool rule, but it does not sandbox the agent.
- Python and command agents. They run as your user, with your permissions. A command agent runs in its own process group, which the harness stops at the end of each episode; a python agent runs inside the harness process.
