# Python static-analysis fixture

This fixture deliberately contains an unused import. Python compilation succeeds, while the project-declared Ruff `F` rule fails. It demonstrates that `--with-static-analysis` adds an independent acceptance gate instead of replacing the existing build.

Run from the repository root after installing Ruff in the active environment:

```powershell
python -m pip install ruff
autofix analyze .\examples\static-analysis\python-unused-import
autofix run .\examples\static-analysis\python-unused-import --with-static-analysis --no-save
autofix repair .\examples\static-analysis\python-unused-import --with-static-analysis --model qwen2.5-coder:7b --max-attempts 3
```

The first build step should pass and Ruff should report `F401`. Repair remains preview-only unless `--apply` is supplied. AutoFix itself never installs Ruff.
