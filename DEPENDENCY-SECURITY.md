# Dependency security

Apply these constraints when upgrading the matching project dependencies:

```sh
python -m pip install --upgrade certifi -c security-constraints.txt
python -m pip check
```

Constraints do not install packages by themselves. Verify TLS-backed metadata requests after upgrades. Preserve a rollback copy of the working environment. No credentials, library contents or deployment-specific configuration belong in this repository.
