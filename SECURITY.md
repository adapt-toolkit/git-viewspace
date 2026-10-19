# Security

Git Viewspace is not a sandbox. Agents operating under the same account can
access that account's files. Symlink targets are not confined to the view.
The manager invokes privileged mount and replay helpers; review the privilege
boundary before installing it. Do not expose its local control socket over a network.

Use trusted repositories and trusted cooperating agents. Do not treat views as
tenant isolation or run untrusted repositories with broad sudo permission.

No independent security audit is claimed. Report security concerns privately to
[security@adaptframework.solutions](mailto:security@adaptframework.solutions).
Do not publish credentials or exploitable details in public issue reports.
