# Privacy Reset Export

This directory is a clean, history-free export derived from the reviewed
`main@6d65c038b88ab98003b4eb5c7d24da5852da0753`.

Before creating the new repository, the export was scrubbed so the single new
commit does not inherit the old Git history or tracked plaintext evidence:

- local OCR caches and candidate CSV exports are absent;
- authorised photographs and generated results are absent;
- A report examples containing phone numbers, ID-card values, names, addresses,
  or email addresses were replaced with placeholders;
- team-member and adviser names/emails in public planning documents were
  replaced with role labels;
- project-application Word/PDF documents and official application templates
  were removed because they may contain names, emails, signatures, or other
  personal information and are not required to run or reproduce the software;
- the repository privacy scanner now scans the full source tree rather than only
  top-level generated artifacts.

The pre-rewrite history and old `main` snapshot are stored only in the local
directory `D:\Desktop\codexTEST\PhotoCheck_privacy_backup_20261120`. Never copy
that directory into the new repository.

## Next manual step

After the user creates or recreates the empty remote repository:

```powershell
git init -b main
git add .
git commit -m "Initial clean PhotoCheck repository"
git remote add origin https://github.com/YouLiA000/PhotoCheck.git
git push -u origin main
```

The new repository is `https://github.com/YouLiA000/PhotoCheck.git`. Every
collaborator must clone it again into a new local directory:

```powershell
git clone https://github.com/YouLiA000/PhotoCheck.git PhotoCheck
```

Do not reuse or merge a clone of the old history.
