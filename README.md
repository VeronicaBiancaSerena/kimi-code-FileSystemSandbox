# kimi-code-FileSystemSandbox
A filesystem sandbox launcher for Kimi Code, built on bubblewrap. It runs the existing kimi CLI inside a restricted filesystem view: your project mounted read-write at /workspace, an isolated KIMI_CODE_HOME, read-only system directories, and tmpfs HOME / /tmp.
