# Contribution Workflow

`main` is a protected branch. Direct pushes, force pushes, and branch deletion are
disabled. Every change must go through a pull request with at least one approval
from another collaborator.

## Before opening a pull request

1. Branch from the current target branch:

   ```powershell
   git switch main
   git pull --ff-only
   git switch -c feature/<short-name>
   ```

2. Run the relevant tests plus the full privacy scan:

   ```powershell
   python -m pytest contract\tests dataset\tools\tests ui\tests eval\tests tools\tests app\tests -q
   python -m pytest photocheck\tests photocheck\tests_integration -q
   python tools\privacy_scan.py
   ```

3. Confirm the change does not add raw OCR text, QR payloads, full phone/ID
   numbers, raw EXIF values, name/address candidate exports, or authorised
   photographs to the repository or its release artifacts.

## Review and merge

- Open the pull request against `main`; do not push commits directly to `main`.
- Request a review from another collaborator.
- Resolve all review conversations and wait for CI/tests to pass.
- Use a normal merge after approval. Do not bypass branch protection or force
  push `main`.

For stacked work, open the follow-up pull request against the active review
branch. Merge the lower pull request first, then retarget and re-run the upper
pull request before merging it into `main`.
