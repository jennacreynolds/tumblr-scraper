# Temporary Linux Native Messaging host

`native_host.py` accepts only the `open` action. It uses the existing project
bootstrap and starts the existing browser host with `--serve-only` when needed.

`install-linux.py` writes a per-user manifest under
`~/.mozilla/native-messaging-hosts/` and generates the absolute host path at
install time. No sudo or system-wide installation is required.
