# Don't perform dangerous operations
## Host system is sacrosanct
- You must not reconfigure the host system: don't install packages, don't configure services, don't modify configuration files, don't modify files except in the project root and its subdirectories and temporary locations like /tmp.
- If you need to install something in the project's virtual environment, ask first.
- Never run `sudo`.
- Never escalate your privileges.
